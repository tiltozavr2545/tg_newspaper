"""SQLite storage for raw posts and run results."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .youtube import YoutubeVideo

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    text TEXT NOT NULL,
    has_media INTEGER NOT NULL,
    media_type TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

-- Result of a single run (the "Assemble newspaper" button) — not raw posts,
-- but a decision for each post in the window: went into the newspaper or
-- was filtered out, at which stage, why. Filled in from the run's results
-- (pipeline.run_pipeline_for_last_24h) so the console can browse run
-- history without calling the LLM again.
--
-- period_start/period_end — the bounds of this run's collection window
-- (the last LOOKBACK_HOURS hours as of the button press). Stored because
-- the run is started manually at an arbitrary moment: without a recorded
-- window there's no way to later tell which period the posts were
-- collected for. NULL — a run made before switching to button-triggered
-- runs, when the pipeline ran over the whole database.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_outcomes (
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    included INTEGER NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    -- LLM's assessment of how much a photo specifically (not video) would
    -- help this post, three degrees (see _CLASSIFICATION_INSTRUCTION in
    -- classifier.py): 1 — not needed, 2 — nice to have, but the text's
    -- meaning isn't lost, 3 — a significant part of the meaning is lost
    -- without a photo. Default 1 — both for posts that didn't reach LLM
    -- classification and for runs made before this field existed.
    photo_relevance INTEGER NOT NULL DEFAULT 1,
    -- Importance score from the LLM classifier (1-5, see classifier.py) —
    -- Stage 3: print important items first, shorten/drop minor ones when
    -- everything doesn't fit on the fixed-size issue. 0 — undetermined
    -- (post didn't reach LLM classification, or the run predates this field).
    importance INTEGER NOT NULL DEFAULT 0,
    -- Personal score (Stage 6 item 4): how interesting the post is to this
    -- specific reader (1-5), from the same classification call. 0 — wasn't
    -- requested (empty profile, post didn't reach classification, old run).
    personal_importance INTEGER NOT NULL DEFAULT 0,
    -- Final score = a blend of importance and personal_importance weighted
    -- by the number of ratings (personalization.final_importance). The issue
    -- is assembled and surveys are built based on it. 0 — old run: then
    -- readers fall back to the generic importance (pipeline.effective_importance).
    final_importance INTEGER NOT NULL DEFAULT 0,
    -- Article text if it was SYNTHESIZED by the pipeline, rather than
    -- matching the raw post from the posts table — currently the only such
    -- case: LLM-based merging of several posts from one channel into a
    -- single article about a story developing over the day
    -- (classifier.merge_story_batch, pipeline._merge_story_arcs).
    -- Empty string (default) — use the text as-is from posts; without this
    -- field, load_run would restore the pre-merge original text for the
    -- merge anchor, instead of the combined article the pipeline actually
    -- selected for print.
    merged_text TEXT NOT NULL DEFAULT '',
    -- Path to the post's downloaded photo (collector.py), if any and if it
    -- belongs to an article selected for print — the file itself lives
    -- outside the DB (Config.media_dir), only the path is stored here, so
    -- layout.py can find it when reopening a saved run without contacting
    -- Telegram again.
    -- Empty string (default) — no photo, or the run predates this field.
    photo_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, channel, message_id)
);

-- Composition of the ACTUALLY assembled issue: which posts, with what text,
-- went into the last successful page render (pipeline.build_newspaper).
-- This isn't the same as pipeline_outcomes.included=1: included only means
-- "passed Stage 2 selection", while in a fixed-size issue some of those
-- posts don't fit and are dropped, some are shortened by the LLM.
-- Deduplication against what's already published (history_dedup) must rely
-- on exactly what was printed, otherwise a news item that didn't fit in the
-- issue would be blocked the next day as "already in the newspaper".
--
-- text — the printed text (after LLM shortening, if any), not the original
-- from posts: this is what the reader actually saw on paper, and it's what
-- new candidates are compared against.
--
-- An issue for a single run_id can be assembled several times
-- (render_preview is run repeatedly), so a repeat assembly REPLACES this
-- run's issue composition (storage.save_issue) rather than appending to it.
-- A run with no rows in this table — no issue was assembled for it, it's
-- not part of dedup history. An issue that ended up with no posts leaves no
-- rows and isn't considered "assembled".
CREATE TABLE IF NOT EXISTS issue_posts (
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    assembled_at TEXT NOT NULL,
    PRIMARY KEY (run_id, channel, message_id)
);
-- Reader profile (Stage 6 item 1). There's only one reader for now, so the
-- table holds exactly one row with id=1 (the CHECK prevents a second one)
-- — reader_id is deliberately not introduced, multi-user is out of scope
-- for this project; but nothing about a specific person is hardcoded in
-- the code either: interests live only here.
--
-- interests/disinterests — free-form text from onboarding ("what's
-- interesting" / "what's not interesting"), as-is: the LLM interprets it
-- during the personal scoring stage (Stage 6 item 4), there's nothing to
-- structure ahead of time.
-- onboarded_at — when onboarding was completed OR skipped via the "Skip"
-- button (in which case the texts are empty): NULL means "hasn't been
-- asked yet" — the console won't let a run start while it's NULL.
-- summary/summary_updated_at — the short profile that the LLM will
-- summarize from the texts and accumulated feedback (part of item 4).
-- Currently only stored: personal scoring writes them later, there's no
-- one to read them before that.
CREATE TABLE IF NOT EXISTS reader_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    interests TEXT NOT NULL DEFAULT '',
    disinterests TEXT NOT NULL DEFAULT '',
    onboarded_at TEXT,
    summary TEXT,
    summary_updated_at TEXT
);

-- Survey "rank the posts by importance" (Stage 6 items 2-3). Two kinds:
--   'onboarding' — a ranking round from onboarding; there are three per
--                  questionnaire (three rows, run_id NULL), each round is
--                  a separate survey with its own agreement metric;
--   'issue'      — a survey after an assembled issue, run_id is the run
--                  whose issue is being rated (one per run, see feedback.py).
-- Survey items are recorded in the DB when the survey is created
-- (feedback_items), so reopening the page shows the same posts in the same
-- order instead of resampling them.
-- submitted_at NULL — the survey was created but not submitted (the reader
-- left the page): the console treats it as unanswered and will show it again.
-- agreement — the metric for how well the reader's order matches the
-- model's order (Kendall tau-b, -1..1, see feedback.agreement). NULL both
-- for an unsubmitted survey and when the metric is undefined (the model
-- gave all posts the same score).
CREATE TABLE IF NOT EXISTS feedback_surveys (
    survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('onboarding', 'issue')),
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    agreement REAL
);

-- Survey posts and the reader's answer for each.
-- in_issue — whether the post was in the printed issue (always 0 for
-- onboarding: there's no issue there). Needed both for the text (in an
-- issue we show the printed version, not the original) and as a signal
-- for part B: "dropped but interesting" is the strongest signal that the
-- model's score is wrong.
-- model_score — the model's score AT THE TIME of the survey
-- (feedback.model_score). We record a copy rather than a reference to
-- pipeline_outcomes.importance: the personal score changes and is
-- recalculated later, while the metric and examples for the LLM must
-- describe what the reader actually compared.
-- shown_position — the order the posts are shown on screen (random, not by
-- score: we don't want to nudge the reader). user_rank — the rank they
-- gave it (1 — most important to them), NULL until submitted; ranks are
-- unique within a survey (checked on form submission, not by the schema).
CREATE TABLE IF NOT EXISTS feedback_items (
    survey_id INTEGER NOT NULL REFERENCES feedback_surveys(survey_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    in_issue INTEGER NOT NULL,
    model_score INTEGER NOT NULL,
    shown_position INTEGER NOT NULL,
    user_rank INTEGER,
    PRIMARY KEY (survey_id, channel, message_id)
);
"""


@dataclass(frozen=True)
class Post:
    channel: str
    message_id: int
    posted_at: datetime
    text: str
    has_media: bool
    media_type: str | None


@dataclass(frozen=True)
class Run:
    run_id: int
    started_at: datetime
    # The run's collection window; None for runs made before switching to button-triggered runs.
    period_start: datetime | None
    period_end: datetime | None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns that were introduced after the first runs to an already
    created DB (CREATE TABLE IF NOT EXISTS doesn't change an existing table)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)")}
    for column in ("period_start", "period_end"):
        if column not in columns:
            conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {column} TEXT")

    outcome_columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_outcomes)")}
    if "importance" not in outcome_columns:
        conn.execute("ALTER TABLE pipeline_outcomes ADD COLUMN importance INTEGER NOT NULL DEFAULT 0")

    # Stage 6 item 4: for old runs, 0 means "unknown" (we don't backfill a guess).
    for column in ("personal_importance", "final_importance"):
        if column not in outcome_columns:
            conn.execute(
                f"ALTER TABLE pipeline_outcomes ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )

    if "photo_relevance" not in outcome_columns:
        conn.execute(
            "ALTER TABLE pipeline_outcomes ADD COLUMN photo_relevance INTEGER NOT NULL DEFAULT 1"
        )
        # The older needs_photo column (boolean) — roughly carry it over to
        # the new three-degree scale so we don't lose already-collected
        # data: needs_photo=1 used to mean "meaning is lost without a photo" (degree 3).
        if "needs_photo" in outcome_columns:
            conn.execute(
                "UPDATE pipeline_outcomes SET photo_relevance = 3 WHERE needs_photo = 1"
            )

    if "merged_text" not in outcome_columns:
        conn.execute(
            "ALTER TABLE pipeline_outcomes ADD COLUMN merged_text TEXT NOT NULL DEFAULT ''"
        )

    if "photo_path" not in outcome_columns:
        conn.execute(
            "ALTER TABLE pipeline_outcomes ADD COLUMN photo_path TEXT NOT NULL DEFAULT ''"
        )

    # Stage 7: new videos are tied to a run, so rebuilding the issue reuses
    # the same videos without network access (like photo_path for posts).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS youtube_videos (
            run_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            channel_title TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TEXT NOT NULL,
            thumbnail_path TEXT,
            PRIMARY KEY (run_id, video_id)
        )"""
    )

    conn.commit()


def save_run_videos(conn: sqlite3.Connection, run_id: int, videos: list[YoutubeVideo]) -> None:
    """Videos collected in this run (saving again overwrites)."""
    conn.executemany(
        "INSERT OR REPLACE INTO youtube_videos (run_id, video_id, channel_id, channel_title, "
        "title, published_at, thumbnail_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, v.video_id, v.channel_id, v.channel_title, v.title,
             v.published_at.astimezone(timezone.utc).isoformat(), v.thumbnail_path)
            for v in videos
        ],
    )
    conn.commit()


def load_run_videos(conn: sqlite3.Connection, run_id: int) -> list[YoutubeVideo]:
    """A run's videos, newest first."""
    rows = conn.execute(
        "SELECT video_id, channel_id, channel_title, title, published_at, thumbnail_path "
        "FROM youtube_videos WHERE run_id = ? ORDER BY published_at DESC",
        (run_id,),
    ).fetchall()
    return [
        YoutubeVideo(vid, cid, ctitle, title, datetime.fromisoformat(pub), thumb)
        for vid, cid, ctitle, title, pub, thumb in rows
    ]


def load_posts(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Post]:
    """Posts from the window (since, until]. With no bounds — the whole DB (for debugging)."""
    where, params = [], []
    if since is not None:
        where.append("posted_at > ?")
        params.append(since.astimezone(timezone.utc).isoformat())
    if until is not None:
        where.append("posted_at <= ?")
        params.append(until.astimezone(timezone.utc).isoformat())
    sql = "SELECT channel, message_id, posted_at, text, has_media, media_type FROM posts"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows = conn.execute(sql, params).fetchall()
    return [
        Post(
            channel=channel,
            message_id=message_id,
            posted_at=datetime.fromisoformat(posted_at),
            text=text,
            has_media=bool(has_media),
            media_type=media_type,
        )
        for channel, message_id, posted_at, text, has_media, media_type in rows
    ]


def create_run(
    conn: sqlite3.Connection,
    run_started_at: datetime,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> int:
    def iso(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    cur = conn.execute(
        "INSERT INTO pipeline_runs (run_started_at, period_start, period_end) VALUES (?, ?, ?)",
        (iso(run_started_at), iso(period_start), iso(period_end)),
    )
    conn.commit()
    return cur.lastrowid


def save_outcomes(
    conn: sqlite3.Connection,
    run_id: int,
    rows: list[tuple[str, int, bool, str, str, int, int, int, int, str, str]],
) -> None:
    """rows: (channel, message_id, included, stage, reason, photo_relevance,
    importance, personal_importance, final_importance, merged_text, photo_path)."""
    conn.executemany(
        "INSERT INTO pipeline_outcomes "
        "(run_id, channel, message_id, included, stage, reason, photo_relevance, importance, "
        "personal_importance, final_importance, merged_text, photo_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, ch, mid, int(inc), stage, reason, photo_relevance, importance, personal, final,
             merged_text, photo_path)
            for (ch, mid, inc, stage, reason, photo_relevance, importance, personal, final,
                 merged_text, photo_path) in rows
        ],
    )
    conn.commit()


def list_runs(conn: sqlite3.Connection) -> list[Run]:
    rows = conn.execute(
        "SELECT run_id, run_started_at, period_start, period_end "
        "FROM pipeline_runs ORDER BY run_id DESC"
    ).fetchall()

    def parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    return [
        Run(run_id, datetime.fromisoformat(started_at), parse(period_start), parse(period_end))
        for run_id, started_at, period_start, period_end in rows
    ]


def load_outcomes(
    conn: sqlite3.Connection, run_id: int
) -> list[tuple[str, int, bool, str, str, int, int, int, int, str, str]]:
    rows = conn.execute(
        "SELECT channel, message_id, included, stage, reason, photo_relevance, importance, "
        "personal_importance, final_importance, merged_text, photo_path "
        "FROM pipeline_outcomes WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [
        (ch, mid, bool(inc), stage, reason, photo_relevance, importance, personal, final,
         merged_text, photo_path)
        for (ch, mid, inc, stage, reason, photo_relevance, importance, personal, final,
             merged_text, photo_path) in rows
    ]


def save_issue(conn: sqlite3.Connection, run_id: int, posts: list[Post]) -> None:
    """Records the composition of run_id's assembled issue (text is what was
    actually printed, see issue_posts in SCHEMA). Reassembling the same
    run_id entirely replaces the previous composition: old rows are deleted
    and new ones inserted in a single transaction, so a post dropped during
    a rebuild doesn't stay in dedup history as "printed". An empty posts
    list simply wipes the composition."""
    assembled_at = datetime.now(timezone.utc).isoformat()
    with conn:  # one transaction: either the old composition or the new one, never a mix
        conn.execute("DELETE FROM issue_posts WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO issue_posts (run_id, channel, message_id, text, assembled_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, p.channel, p.message_id, p.text, assembled_at) for p in posts],
        )


def load_recent_issue_posts(conn: sqlite3.Connection, limit_issues: int) -> list[Post]:
    """Posts from the last limit_issues ASSEMBLED issues (runs that have a
    record in issue_posts) with the printed text — history for deduping
    against what's already been published (Stage 2 item 4). We count issues,
    not runs and not days: the project is run via a button, and runs for
    which an issue was never assembled shouldn't push actually-printed
    content out of the window. "Last" is by run_id (run order); rebuilding
    an old issue again doesn't change its place in the window. Post
    metadata (posted_at, media) comes from posts, text comes from
    issue_posts. If one post ended up in several issues, it's returned
    once — with the text from the most recent one."""
    rows = conn.execute(
        """
        SELECT i.channel, i.message_id, p.posted_at, i.text, p.has_media, p.media_type
        FROM issue_posts i
        JOIN posts p ON p.channel = i.channel AND p.message_id = i.message_id
        WHERE i.run_id IN (
            SELECT DISTINCT run_id FROM issue_posts ORDER BY run_id DESC LIMIT ?
        )
        ORDER BY i.run_id DESC
        """,
        (limit_issues,),
    ).fetchall()
    seen: set[tuple[str, int]] = set()
    result: list[Post] = []
    for channel, message_id, posted_at, text, has_media, media_type in rows:
        if (channel, message_id) in seen:
            continue
        seen.add((channel, message_id))
        result.append(
            Post(
                channel=channel,
                message_id=message_id,
                posted_at=datetime.fromisoformat(posted_at),
                text=text,
                has_media=bool(has_media),
                media_type=media_type,
            )
        )
    return result


def save_posts(conn: sqlite3.Connection, posts: list[Post]) -> None:
    collected_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO posts "
        "(channel, message_id, posted_at, text, has_media, media_type, collected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(channel, message_id) DO NOTHING",
        [
            (
                post.channel,
                post.message_id,
                post.posted_at.astimezone(timezone.utc).isoformat(),
                post.text,
                int(post.has_media),
                post.media_type,
                collected_at,
            )
            for post in posts
        ],
    )
    conn.commit()


# --- Stage 6: reader profile and feedback -------------------------------------


@dataclass(frozen=True)
class ReaderProfile:
    interests: str = ""
    disinterests: str = ""
    # None — onboarding hasn't been completed or skipped yet.
    onboarded_at: datetime | None = None
    # Filled in by pipeline.refresh_profile_summary (Stage 6 item 4).
    summary: str | None = None
    summary_updated_at: datetime | None = None

    @property
    def onboarded(self) -> bool:
        return self.onboarded_at is not None


@dataclass(frozen=True)
class Survey:
    survey_id: int
    kind: str  # "onboarding" | "issue"
    run_id: int | None
    created_at: datetime
    submitted_at: datetime | None
    agreement: float | None

    @property
    def submitted(self) -> bool:
        return self.submitted_at is not None


@dataclass(frozen=True)
class SurveyItem:
    channel: str
    message_id: int
    in_issue: bool
    model_score: int
    shown_position: int
    user_rank: int | None


@dataclass(frozen=True)
class FeedbackExample:
    """One submitted reader answer — an example for the LLM in part B: what
    the post was (the original text from posts), how the model scored it,
    and what rank the reader gave it among the other posts of the same
    survey (rank_of is their count)."""
    survey_id: int
    kind: str
    run_id: int | None
    channel: str
    message_id: int
    text: str
    in_issue: bool
    model_score: int
    user_rank: int
    rank_of: int


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_profile(conn: sqlite3.Connection) -> ReaderProfile:
    """The reader's profile; if the row doesn't exist yet — empty, not onboarded."""
    row = conn.execute(
        "SELECT interests, disinterests, onboarded_at, summary, summary_updated_at "
        "FROM reader_profile WHERE id = 1"
    ).fetchone()
    if row is None:
        return ReaderProfile()
    interests, disinterests, onboarded_at, summary, summary_updated_at = row
    return ReaderProfile(
        interests, disinterests, _parse_dt(onboarded_at), summary, _parse_dt(summary_updated_at)
    )


def _write_profile(
    conn: sqlite3.Connection, interests: str, disinterests: str, onboarded: bool
) -> None:
    # No commit — the caller decides which transaction this happens in.
    # summary is NOT touched: only personal scoring writes it, and saving
    # the texts again shouldn't wipe the accumulated short profile.
    conn.execute("INSERT OR IGNORE INTO reader_profile (id) VALUES (1)")
    conn.execute(
        "UPDATE reader_profile SET interests = ?, disinterests = ?, "
        "onboarded_at = CASE WHEN ? THEN COALESCE(onboarded_at, ?) ELSE onboarded_at END "
        "WHERE id = 1",
        (interests, disinterests, int(onboarded), _iso_now()),
    )


def save_profile(
    conn: sqlite3.Connection, interests: str, disinterests: str, onboarded: bool = True
) -> None:
    """Saves the profile's texts. onboarded=True sets onboarded_at (if it's
    not already set — the first stamp isn't overwritten); empty texts +
    onboarded is exactly what "Skip" does."""
    with conn:
        _write_profile(conn, interests, disinterests, onboarded)


def save_profile_summary(conn: sqlite3.Connection, summary: str) -> None:
    """Short profile from the LLM (Stage 6 item 4). Only writing happens
    here — called by pipeline.refresh_profile_summary before classification."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO reader_profile (id) VALUES (1)")
        conn.execute(
            "UPDATE reader_profile SET summary = ?, summary_updated_at = ? WHERE id = 1",
            (summary, _iso_now()),
        )


def create_survey(
    conn: sqlite3.Connection,
    kind: str,
    run_id: int | None,
    items: list[tuple[str, int, bool, int, int]],
) -> int:
    """Creates a survey and records its items. items: (channel, message_id,
    in_issue, model_score, shown_position). Returns survey_id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback_surveys (kind, run_id, created_at) VALUES (?, ?, ?)",
            (kind, run_id, _iso_now()),
        )
        survey_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO feedback_items "
            "(survey_id, channel, message_id, in_issue, model_score, shown_position) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(survey_id, ch, mid, int(inc), score, pos) for ch, mid, inc, score, pos in items],
        )
    return survey_id


_SURVEY_COLUMNS = "survey_id, kind, run_id, created_at, submitted_at, agreement"


def _survey_from_row(row: tuple) -> Survey:
    survey_id, kind, run_id, created_at, submitted_at, agreement = row
    return Survey(
        survey_id, kind, run_id, datetime.fromisoformat(created_at),
        _parse_dt(submitted_at), agreement,
    )


def load_survey(conn: sqlite3.Connection, survey_id: int) -> Survey | None:
    row = conn.execute(
        f"SELECT {_SURVEY_COLUMNS} FROM feedback_surveys WHERE survey_id = ?", (survey_id,)
    ).fetchone()
    return _survey_from_row(row) if row else None


def list_surveys(
    conn: sqlite3.Connection,
    kind: str,
    run_id: int | None = None,
    only_pending: bool = False,
) -> list[Survey]:
    """Surveys of kind kind (for 'issue' — only the given run, if run_id is
    passed) in ascending survey_id order. only_pending — unsubmitted only:
    this is how the console finds a survey that's still awaiting an answer."""
    sql = f"SELECT {_SURVEY_COLUMNS} FROM feedback_surveys WHERE kind = ?"
    params: list = [kind]
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    if only_pending:
        sql += " AND submitted_at IS NULL"
    sql += " ORDER BY survey_id"
    return [_survey_from_row(r) for r in conn.execute(sql, params)]


def load_survey_items(conn: sqlite3.Connection, survey_id: int) -> list[SurveyItem]:
    """A survey's items in display order (shown_position)."""
    rows = conn.execute(
        "SELECT channel, message_id, in_issue, model_score, shown_position, user_rank "
        "FROM feedback_items WHERE survey_id = ? ORDER BY shown_position",
        (survey_id,),
    ).fetchall()
    return [SurveyItem(ch, mid, bool(inc), score, pos, rank) for ch, mid, inc, score, pos, rank in rows]


def _write_answers(
    conn: sqlite3.Connection,
    survey_id: int,
    ranks: dict[tuple[str, int], int],
    agreement: float | None,
) -> None:
    # No commit — see _write_profile.
    conn.executemany(
        "UPDATE feedback_items SET user_rank = ? "
        "WHERE survey_id = ? AND channel = ? AND message_id = ?",
        [(rank, survey_id, ch, mid) for (ch, mid), rank in ranks.items()],
    )
    conn.execute(
        "UPDATE feedback_surveys SET submitted_at = ?, agreement = ? WHERE survey_id = ?",
        (_iso_now(), agreement, survey_id),
    )


def save_survey_answers(
    conn: sqlite3.Connection,
    survey_id: int,
    ranks: dict[tuple[str, int], int],
    agreement: float | None,
) -> None:
    """Records the reader's ranks ((channel, message_id) -> 1..n) and the
    agreement metric, marks the survey as submitted. Checking that ranks are
    unique happens on the form-submission side (feedback.validate_ranks),
    only writing happens here."""
    with conn:
        _write_answers(conn, survey_id, ranks, agreement)


def complete_onboarding(
    conn: sqlite3.Connection,
    interests: str,
    disinterests: str,
    answers: list[tuple[int, dict[tuple[str, int], int], float | None]],
) -> None:
    """Completes onboarding in a single transaction: profile texts + answers
    for all rounds (survey_id, ranks, agreement). A single one — so that a
    failure midway doesn't leave "onboarding completed" without answers for
    the rounds (or vice versa)."""
    with conn:
        _write_profile(conn, interests, disinterests, True)
        for survey_id, ranks, agreement in answers:
            _write_answers(conn, survey_id, ranks, agreement)


def latest_issue_run_id(conn: sqlite3.Connection) -> int | None:
    """The most recent run for which an assembled issue is recorded (issue_posts)."""
    row = conn.execute("SELECT MAX(run_id) FROM issue_posts").fetchone()
    return row[0]


def load_issue_texts(conn: sqlite3.Connection, run_id: int) -> dict[tuple[str, int], str]:
    """The printed texts of run_id's issue: (channel, message_id) -> text."""
    rows = conn.execute(
        "SELECT channel, message_id, text FROM issue_posts WHERE run_id = ?", (run_id,)
    ).fetchall()
    return {(ch, mid): text for ch, mid, text in rows}


def load_post_texts(
    conn: sqlite3.Connection, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    """Original post texts from posts by key (missing ones are skipped)."""
    result: dict[tuple[str, int], str] = {}
    for ch, mid in keys:
        row = conn.execute(
            "SELECT text FROM posts WHERE channel = ? AND message_id = ?", (ch, mid)
        ).fetchone()
        if row:
            result[(ch, mid)] = row[0]
    return result


def load_submitted_feedback(conn: sqlite3.Connection) -> list[FeedbackExample]:
    """All submitted reader answers — examples for personal scoring
    (part B). Most recent surveys first; within a survey — by the reader's rank."""
    rows = conn.execute(
        """
        SELECT s.survey_id, s.kind, s.run_id, i.channel, i.message_id,
               COALESCE(p.text, ''), i.in_issue, i.model_score, i.user_rank,
               (SELECT COUNT(*) FROM feedback_items x WHERE x.survey_id = s.survey_id)
        FROM feedback_surveys s
        JOIN feedback_items i ON i.survey_id = s.survey_id
        LEFT JOIN posts p ON p.channel = i.channel AND p.message_id = i.message_id
        WHERE s.submitted_at IS NOT NULL AND i.user_rank IS NOT NULL
        ORDER BY s.survey_id DESC, i.user_rank
        """
    ).fetchall()
    return [
        FeedbackExample(sid, kind, run_id, ch, mid, text, bool(inc), score, rank, n)
        for sid, kind, run_id, ch, mid, text, inc, score, rank, n in rows
    ]
