"""Single run of the full pipeline ("Assemble newspaper"): collecting posts
for the last 24 hours via Telethon, heuristics, deduplication, LLM
classification — with a detailed result for each post: at which stage it
was filtered out and why, or that it made it to print. Used both by
scripts/run_pipeline.py (run from the terminal) and by the local console
(the "Assemble newspaper" button) — so the pipeline logic isn't duplicated
in several places.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .classifier import BATCH_SIZE, GeminiClassifier
from .collector import collect_all, collection_since
from .config import Config
from .dedup import find_copypaste_clusters
from .filtering import filter_posts
from .history_dedup import HISTORY_RUNS_WINDOW, filter_against_history
from .layout import COVER_MIN_ARTICLES, brief_keys, render_pages
from .paraphrase_dedup import GeminiEmbedder, find_paraphrase_clusters
from .personalization import (
    ReaderContext,
    final_importance,
    load_reader_context,
    summary_due,
)
from .storage import (
    Post,
    Run,
    connect,
    create_run,
    load_outcomes,
    load_issue_texts,
    load_posts,
    load_recent_issue_posts,
    load_run_videos,
    save_issue,
    save_outcomes,
    save_posts,
    save_profile_summary,
    save_run_videos,
)
from .youtube import YoutubeVideo, fetch_new_videos

logger = logging.getLogger(__name__)

Key = tuple[str, int]


@dataclass(frozen=True)
class PostOutcome:
    post: Post
    included: bool
    stage: str  # "heuristic" | "copypaste" | "classification" | "paraphrase" | "story_merge" | "final"
    reason: str
    # LLM's assessment of how much a photo specifically (not video) would
    # help this post — three degrees (see _CLASSIFICATION_INSTRUCTION in
    # classifier.py): 1 — not needed, 2 — nice to have, but meaning isn't
    # lost, 3 — a significant part of the meaning is lost without a photo.
    # Known only for posts that reached LLM classification; for the rest —
    # 1 (not needed) by default.
    photo_relevance: int = 1
    # Importance score from the LLM (1-5, see classifier.py) — Stage 3 uses
    # it to decide what to print first, what to shorten, and what to drop
    # if everything doesn't fit on the fixed-size issue. Known only for
    # posts that reached LLM classification; 0 — unknown (post filtered out
    # earlier, or the run predates this field).
    importance: int = 0
    # Personal score (1-5) from the same classification call, and the final
    # one — a blend with the generic score, weighted increasingly with the
    # number of ratings (Stage 6 item 4, personalization.final_importance).
    # 0 — unknown: empty profile (personal wasn't requested then) or the run
    # predates these fields.
    personal_importance: int = 0
    final_importance: int = 0
    # Path to the post's downloaded photo (collector.py._download_photo), if
    # any — carried over to the anchor article when merging
    # (_merge_story_arcs). Empty string — no photo, it didn't download, or
    # photo_relevance = 1 (not needed) so there was no point looking for it.
    photo_path: str = ""
    # Article text if it was SYNTHESIZED by the pipeline (currently only the
    # result of _merge_story_arcs, merging one channel's developing story
    # into a single article), rather than matching the post's raw text from
    # the DB. Empty string — post.text is the actual post text. Needs its
    # own field rather than just post.text because save_run/load_run go
    # through the DB: posts stores only the raw originals, and
    # pipeline_outcomes.merged_text is the only place where the synthesized
    # text survives between a run and reopening it in the console.
    merged_text: str = ""


def effective_importance(outcome: PostOutcome) -> int:
    """The ONE place that picks the score for selection/shortening/tile size and
    surveys: the final one, or for old runs (final_importance == 0) — the generic one."""
    return outcome.final_importance or outcome.importance


@dataclass(frozen=True)
class RunResult:
    run_id: int
    period_start: datetime
    period_end: datetime
    outcomes: list[PostOutcome]


def _key(post: Post) -> Key:
    return (post.channel, post.message_id)


def _resolve_copypaste(
    classifier: GeminiClassifier, clusters: list[list[Post]], outcomes: dict[Key, PostOutcome]
) -> list[Post]:
    kept: list[Post] = []
    multi = [c for c in clusters if len(c) > 1]
    for c in clusters:
        if len(c) == 1:
            kept.append(c[0])

    for start in range(0, len(multi), BATCH_SIZE):
        batch = multi[start : start + BATCH_SIZE]
        choices = classifier.choose_best_batch(batch)
        by_cluster = {c.cluster_index: c for c in choices}
        for local_idx, cluster in enumerate(batch):
            choice = by_cluster.get(local_idx)
            post_idx = choice.index if choice and 0 <= choice.index < len(cluster) else 0
            best = cluster[post_idx]
            kept.append(best)
            reason = choice.reason if choice else "LLM не дала ответ, оставлен первый пост"
            for p in cluster:
                if p is not best:
                    outcomes[_key(p)] = PostOutcome(
                        p, False, "copypaste",
                        f"копипаст-дубль {best.channel}/{best.message_id}: {reason}",
                    )
    return kept


def _resolve_paraphrase(
    classifier: GeminiClassifier, clusters: list[list[Post]], outcomes: dict[Key, PostOutcome]
) -> list[list[Post]]:
    """For each paraphrase cluster, returns the subset of posts that the LLM
    judged NOT redundant (carrying at least one unique fact) — grouping by
    cluster is preserved in the return value (not flattened into a single
    list) so that _merge_story_arcs can later break it down by channel and
    collapse a single-channel developing story into one article."""
    kept_groups: list[list[Post]] = []
    multi = [c for c in clusters if len(c) > 1]
    for c in clusters:
        if len(c) == 1:
            kept_groups.append([c[0]])

    for start in range(0, len(multi), BATCH_SIZE):
        batch = multi[start : start + BATCH_SIZE]
        decisions = classifier.resolve_paraphrase_batch(batch)
        by_cluster = {d.cluster_index: d for d in decisions}
        for local_idx, cluster in enumerate(batch):
            decision = by_cluster.get(local_idx)
            if decision is None:
                kept_groups.append(cluster)
                continue
            valid_indices = sorted(
                {i for i in decision.keep_indices if 0 <= i < len(cluster)}
            )
            if not valid_indices:
                kept_groups.append(cluster)
                continue
            kept_in_cluster = [cluster[i] for i in valid_indices]
            kept_groups.append(kept_in_cluster)
            kept_ids = {_key(p) for p in kept_in_cluster}
            for p in cluster:
                if _key(p) not in kept_ids:
                    others = ", ".join(
                        f"{k.channel}/{k.message_id}" for k in kept_in_cluster
                    )
                    outcomes[_key(p)] = PostOutcome(
                        p, False, "paraphrase",
                        f"дубль/подмножество фактов поста(ов) {others}: {decision.reason}",
                    )
    return kept_groups


def _merge_story_arcs(
    classifier: GeminiClassifier,
    kept_groups: list[list[Post]],
    outcomes: dict[Key, PostOutcome],
    importance_by_key: dict[Key, int],
    photo_relevance_by_key: dict[Key, int],
    photo_path_by_key: dict[Key, str],
    merged_anchor_keys: set[Key],
    personal_by_key: dict[Key, int] | None = None,
) -> list[Post]:
    """Collapses into one article the posts of a SINGLE channel that
    _resolve_paraphrase kept within one paraphrase cluster as carrying
    unique facts — the typical case of a story developing over the day
    (incident -> clarification -> retraction), which would otherwise be
    printed as N separate articles. Cross-channel cases (different channels
    reporting different facts of the same event) are left untouched and
    printed as separate posts as before — the user's decision from
    2026-09-27 narrowed the merge to a single channel: a story developing
    across channels isn't the same as a clarification within one channel,
    and deserves a separate decision rather than automatic merging.

    merged_anchor_keys is filled with the keys of anchor posts (see below) —
    the calling code uses them to mark PostOutcome.merged_text, otherwise
    the synthesized article text would be lost when saving to the DB
    (pipeline_outcomes doesn't store text, only channel/message_id —
    save_run/load_run restore it from the raw posts table, which holds only
    the pre-merge original)."""
    final: list[Post] = []
    to_merge: list[list[Post]] = []
    for group in kept_groups:
        by_channel: dict[str, list[Post]] = {}
        for p in group:
            by_channel.setdefault(p.channel, []).append(p)
        for channel_posts in by_channel.values():
            if len(channel_posts) > 1:
                to_merge.append(sorted(channel_posts, key=lambda p: p.posted_at))
            else:
                final.extend(channel_posts)

    for start in range(0, len(to_merge), BATCH_SIZE):
        batch = to_merge[start : start + BATCH_SIZE]
        items = classifier.merge_story_batch(batch)
        by_group = {item.group_index: item for item in items}
        for local_idx, group in enumerate(batch):
            item = by_group.get(local_idx)
            if item is None or not item.is_same_story:
                # The LLM didn't respond, or explicitly decided these are separate,
                # unrelated news items from the same channel (see _STORY_MERGE_INSTRUCTION) —
                # we don't lose posts or invent a story, print them separately as before
                final.extend(group)
                continue
            anchor = group[-1]  # the most recent post carries the story's current state
            merged_text = f"**{item.headline.strip()}**\n\n{item.summary_text.strip()}"
            final.append(replace(anchor, text=merged_text))
            merged_anchor_keys.add(_key(anchor))
            importance_by_key[_key(anchor)] = max(
                importance_by_key.get(_key(p), 0) for p in group
            )
            if personal_by_key is not None:
                personal_by_key[_key(anchor)] = max(
                    personal_by_key.get(_key(p), 0) for p in group
                )
            photo_relevance_by_key[_key(anchor)] = max(
                photo_relevance_by_key.get(_key(p), 1) for p in group
            )
            # We take the photo from the group's post with the highest own
            # photo_relevance score (not necessarily from anchor — the most
            # recent post in the story isn't always the one with a photo),
            # the first one found when scores are equal.
            by_relevance = sorted(group, key=lambda p: -photo_relevance_by_key.get(_key(p), 1))
            photo_path_by_key[_key(anchor)] = next(
                (photo_path_by_key[_key(p)] for p in by_relevance if photo_path_by_key.get(_key(p))),
                "",
            )
            for p in group[:-1]:
                outcomes[_key(p)] = PostOutcome(
                    p, False, "story_merge",
                    f"объединено с {anchor.channel}/{anchor.message_id} "
                    "в одну статью о развитии истории за день",
                )

    return final


def refresh_profile_summary(
    conn: sqlite3.Connection, classifier: GeminiClassifier
) -> bool:
    """Lazily refreshes the reader's short profile summary (Stage 6 item 4) if
    >= SUMMARY_EVERY_SURVEYS new surveys have been submitted since the last
    update. Called from the run BEFORE classification: Gemini is already used
    there, and the console doesn't call Gemini from survey handlers. A failure
    doesn't fail the run — we log it and continue with the old summary.
    Returns True if the summary was refreshed."""
    try:
        if not summary_due(conn):
            return False
        summary = classifier.summarize_profile(load_reader_context(conn))
        if not summary:
            logger.warning("LLM вернула пустой краткий профиль — оставляю прежний")
            return False
        save_profile_summary(conn, summary)
        logger.info("краткий профиль читателя обновлён")
        return True
    except Exception:  # noqa: BLE001 — summary is secondary, the run matters more
        logger.exception("не удалось обновить краткий профиль читателя — продолжаю со старым")
        return False


def _classify_posts(
    config: Config,
    posts: list[Post],
    history: list[Post] | None = None,
    reader: ReaderContext | None = None,
    classifier: GeminiClassifier | None = None,
    photo_path_by_key: dict[Key, str] | None = None,
) -> list[PostOutcome]:
    """Runs heuristics → copypaste dedup → LLM classification → paraphrase
    dedup → (if history is passed) dedup against what's already been
    published in recent issues, over an already-assembled list of posts
    (one window), and returns a result for each of them."""
    outcomes: dict[Key, PostOutcome] = {}
    photo_path_by_key = dict(photo_path_by_key or {})

    candidates, filter_results = filter_posts(posts)
    for r in filter_results:
        if not r.is_news_candidate:
            outcomes[_key(r.post)] = PostOutcome(r.post, False, "heuristic", r.reason or "")

    classifier = classifier or GeminiClassifier(config)
    weight = reader.weight if reader is not None else 0.0

    copypaste_clusters = find_copypaste_clusters(candidates)
    after_copypaste = _resolve_copypaste(classifier, copypaste_clusters, outcomes)

    decisions = classifier.classify(after_copypaste, reader)
    news: list[Post] = []
    photo_relevance_by_key: dict[Key, int] = {}
    importance_by_key: dict[Key, int] = {}
    personal_by_key: dict[Key, int] = {}
    for p in after_copypaste:
        d = decisions[_key(p)]
        if d.is_news:
            news.append(p)
            photo_relevance_by_key[_key(p)] = d.photo_relevance
            importance_by_key[_key(p)] = d.importance
            personal_by_key[_key(p)] = d.personal_importance if weight > 0 else 0
        else:
            outcomes[_key(p)] = PostOutcome(p, False, "classification", d.reason)

    embedder = GeminiEmbedder(config)
    paraphrase_clusters = find_paraphrase_clusters(news, embedder)
    kept_groups = _resolve_paraphrase(classifier, paraphrase_clusters, outcomes)
    merged_anchor_keys: set[Key] = set()
    final_news = _merge_story_arcs(
        classifier, kept_groups, outcomes, importance_by_key, photo_relevance_by_key,
        photo_path_by_key, merged_anchor_keys, personal_by_key,
    )

    if history:
        final_news, excluded_by_history = filter_against_history(
            final_news, history, classifier, embedder
        )
        for p, reason in excluded_by_history:
            outcomes[_key(p)] = PostOutcome(
                p, False, "history_dedup", reason,
                merged_text=p.text if _key(p) in merged_anchor_keys else "",
            )

    for p in final_news:
        generic = importance_by_key.get(_key(p), 0)
        personal = personal_by_key.get(_key(p), 0)
        outcomes[_key(p)] = PostOutcome(
            p, True, "final", "прошёл все этапы отбора",
            photo_relevance=photo_relevance_by_key.get(_key(p), 1),
            importance=generic,
            personal_importance=personal,
            final_importance=final_importance(generic, personal, weight),
            merged_text=p.text if _key(p) in merged_anchor_keys else "",
            photo_path=photo_path_by_key.get(_key(p), ""),
        )

    return [outcomes[_key(p)] for p in posts]


def run_pipeline_for_last_24h(config: Config) -> RunResult:
    """Full run triggered by the "Assemble newspaper" button: fetches via
    Telethon the posts from the last 24 hours as of the call, saves them,
    runs heuristics/dedup/LLM classification, and saves the result as a new
    run. The collection period is always the last 24 hours as of the button
    press, with no catching up on missed days (see AGENTS.md)."""
    run_started_at = datetime.now(timezone.utc)
    since = collection_since(run_started_at)

    conn = connect(config.db_path)
    collected, photo_path_by_key_raw = asyncio.run(collect_all(config, since))
    save_posts(conn, collected)
    photo_path_by_key = {key: str(path) for key, path in photo_path_by_key_raw.items()}

    # Re-read the window from the DB rather than using collected directly —
    # this way the window also includes posts already saved by a previous
    # run (doesn't create duplicates thanks to the (channel, message_id)
    # uniqueness in save_posts).
    posts = load_posts(conn, since=since, until=run_started_at)

    history = load_recent_issue_posts(conn, HISTORY_RUNS_WINDOW)

    classifier = GeminiClassifier(config)
    # Summary first (based on accumulated feedback), then the reader context
    # for classification — so the fresh summary makes it into this run's
    # prompt right away.
    refresh_profile_summary(conn, classifier)
    reader = load_reader_context(conn)
    outcomes = _classify_posts(
        config, posts, history=history, reader=reader, classifier=classifier,
        photo_path_by_key=photo_path_by_key,
    )
    run_id = save_run(conn, outcomes, run_started_at, since, run_started_at)
    _collect_run_videos(config, conn, run_id, since, run_started_at)
    return RunResult(run_id, since, run_started_at, outcomes)


def _collect_run_videos(
    config: Config, conn: sqlite3.Connection, run_id: int, since: datetime, until: datetime
) -> None:
    """New videos from YouTube channels for the run's window -> youtube_videos.
    This block is secondary: any failure is only logged and doesn't fail
    the run. With no channels configured, there's no network call at all."""
    if not config.youtube_channels:
        return
    try:
        videos, errors = fetch_new_videos(
            config.youtube_channels, since, until, config.media_dir / "youtube"
        )
        save_run_videos(conn, run_id, videos)
        logger.info("YouTube: видео %d, ошибок каналов %d", len(videos), len(errors))
    except Exception:  # noqa: BLE001
        logger.warning("YouTube: сбор видео не удался", exc_info=True)


def save_run(
    conn: sqlite3.Connection,
    outcomes: list[PostOutcome],
    run_started_at: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> int:
    """Saves the run's result into pipeline_runs/pipeline_outcomes and
    returns run_id — so the console can later reopen this run without
    calling the LLM again."""
    run_started_at = run_started_at or datetime.now(timezone.utc)
    run_id = create_run(conn, run_started_at, period_start, period_end)
    save_outcomes(
        conn,
        run_id,
        [
            (
                o.post.channel, o.post.message_id, o.included, o.stage, o.reason,
                o.photo_relevance, o.importance, o.personal_importance,
                o.final_importance, o.merged_text, o.photo_path,
            )
            for o in outcomes
        ],
    )
    return run_id


def load_run(conn: sqlite3.Connection, run_id: int) -> list[PostOutcome]:
    """Restores the PostOutcomes of a saved run (without calling the LLM).

    If an outcome has merged_text (merging of a developing story, see
    _merge_story_arcs), the restored Post gets this synthesized text instead
    of the raw text from posts — otherwise the merge anchor would show the
    pre-merge original after the run is reloaded."""
    posts_by_key = {_key(p): p for p in load_posts(conn)}
    result = []
    for (
        channel, message_id, included, stage, reason, photo_relevance, importance,
        personal_importance, final_importance_, merged_text, photo_path,
    ) in load_outcomes(conn, run_id):
        post = posts_by_key.get((channel, message_id))
        if post is None:
            continue  # post was removed from posts — shouldn't happen, but don't fail the report
        if merged_text:
            post = replace(post, text=merged_text)
        result.append(
            PostOutcome(
                post, included, stage, reason,
                photo_relevance=photo_relevance, importance=importance,
                personal_importance=personal_importance,
                final_importance=final_importance_,
                merged_text=merged_text, photo_path=photo_path,
            )
        )
    return result


# The printed newspaper is a fixed-size issue, not an infinite feed (Stage 3,
# following the user's review: "four A4 sheets, rotated to landscape").
# Limit on MOSAIC pages (the cover is 2 more pages on top of this; one A3
# sheet = SHEET_UNITS = 2 pages). User decision 2026-10-14: 4 → 6, i.e.
# +1 A3 sheet — in a real run 4 pages weren't enough and too much
# significant content was being dropped.
NEWSPAPER_MAX_PAGES = 6
# We don't try to shorten with the LLM something that's already below-average
# in importance — such a post is simply dropped from the issue if it doesn't fit.
IMPORTANCE_DROP_THRESHOLD = 2
# Below this many news items that made it to print, we don't assemble an
# issue: this many are needed to fill the cover page's billboard completely
# (lead + secondary + strips above the masthead + third row,
# layout.COVER_MIN_ARTICLES); fewer is a sign of a collection failure or an
# empty day, not an issue worth printing. In Stage 8 the threshold will
# become configurable.
MIN_ISSUE_NEWS = COVER_MIN_ARTICLES
SHORTEN_TARGET_SENTENCES = 3
# Below this length there's almost nothing to shorten — no point calling the
# LLM, such a post either fits as-is or gets dropped along with unimportant ones.
SHORTEN_MIN_CHARS = 400
# Future brief tiles longer than this are shortened ahead of time, before the
# first layout pass, regardless of importance (user decision 2026-10-14:
# maximize the number of news items in the issue). Brief is a third of the
# page width (~80mm of text, ~3.5 characters per mm at 2.9mm type size), and
# the height of a row of three tiles is set by the longest one: 600
# characters ≈ 8 lines of ~50 characters ≈ 8*2.9*1.4*1.1/5.2 ≈ 7 row units
# (+ headline), whereas 1200+ characters give 20-38 row units out of ~74 per
# sheet.
BRIEF_SHORTEN_MIN_CHARS = 600


@dataclass(frozen=True)
class NewspaperResult:
    pages: list[Path]
    dropped: list[Post]  # didn't fit in the fixed-size issue and weren't shortened
    shortened_count: int  # how many news items the LLM shortened to save space
    # What actually went into the last successful render_pages — with the
    # final (post-LLM-shortening) text. Not the same as the run's included
    # outcomes: some of those were dropped. This exact list is saved as the
    # issue's composition (save_newspaper_issue) and serves as history for
    # deduping subsequent runs.
    published: list[Post]


def _significance_key(importance_by_key: dict[Key, int]) -> Callable[[Post], tuple[int, datetime, int]]:
    """Significance ordering key: ascending — from the least significant news
    item to the most (score, ties broken by older first, then shorter).
    The single place for deciding what to keep in the issue when space is tight."""
    return lambda p: (importance_by_key.get(_key(p), 0), p.posted_at, len(p.text))


def build_newspaper(
    config: Config,
    outcomes: list[PostOutcome],
    out_dir: Path,
    basename: str,
    run_date: datetime,
    max_pages: int = NEWSPAPER_MAX_PAGES,
    videos: list[YoutubeVideo] | None = None,
    known_shortened: dict[Key, str] | None = None,
) -> NewspaperResult:
    """Assembles a fixed-size printed issue (no more than max_pages landscape
    A4 pages) from news that has already passed selection (Stages 1-2).

    Layout follows the final importance score (effective_importance: a blend
    of the generic importance from classifier.py and the personal score; for
    old runs — generic): the most important items are printed first and get
    a larger tile (see layout._build_bands).

    All intermediate layout passes are fit-only (render_pages(fit_only=True):
    no billboard, no LLM, no disk writes), with exactly one full render into
    out_dir, right at the end. This way out_dir never keeps pages from a
    longer earlier attempt (issue_pages globs the whole directory). The
    order is:
    - if the issue fits as-is, the LLM isn't called at all;
    - otherwise, texts longer than BRIEF_SHORTEN_MIN_CHARS are shortened
      first for posts that will become briefs per the tier rules
      (layout.brief_keys), regardless of importance: a row's height is set
      by its longest tile, and shortening the leftover doesn't affect tiles
      already placed on a sheet;
    - what still doesn't fit and is important enough (score >
      IMPORTANCE_DROP_THRESHOLD) and long enough for shortening to make
      sense gets shortened by the LLM (GeminiClassifier.shorten), and the
      layout pass is repeated;
    - if there's nothing left to shorten, a binary search finds the largest
      number k of the most significant news items of the WHOLE issue that
      fit entirely; the rest are dropped. The choice is by significance
      across the whole issue, not the leftover: with rubrics (sections),
      "didn't fit" is the tail of the last section, not the least
      important overall. Search instead of dropping len(leftover) in one
      go: that kind of drop freed up far more space than needed, and the
      last pages stayed empty.

    known_shortened — texts already shortened in a previous assembly of this
    same run (assemble_issue takes them from the printed composition):
    shortening checks there first and calls Gemini only for the rest, so a
    rebuild doesn't pay for the same calls and produces the same text.

    videos — the "New videos" block at the end of the last sheet
    (layout.render_pages): space for it is reserved, so news items don't
    push it out.

    The layout guarantee rests on the fit pass, but if the final render
    still produces a leftover, those posts aren't counted as published and
    go into dropped instead — otherwise subsequent runs' dedup would discard
    news the reader never saw."""
    included = [o for o in outcomes if o.included]
    if not included:
        return NewspaperResult([], [], 0, [])

    importance_by_key: dict[Key, int] = {_key(o.post): effective_importance(o) for o in included}
    photo_relevance_by_key: dict[Key, int] = {_key(o.post): o.photo_relevance for o in included}
    photo_path_by_key: dict[Key, str] = {
        _key(o.post): o.photo_path for o in included if o.photo_path
    }
    posts = [o.post for o in included]

    classifier: GeminiClassifier | None = None
    already_shortened: set[Key] = set()
    dropped: list[Post] = []
    shortened_count = 0
    known = known_shortened or {}

    # Bold teasers for the cover page's billboard (layout._cover_page_html).
    # We hand them to layout as a callback rather than a ready-made dict:
    # who actually ends up on the billboard is only known after pagination —
    # inside render_pages. The callback is only called from the full render,
    # and only once, so the cache is just a safeguard against requesting a
    # teaser for the same news item twice.
    teaser_cache: dict[Key, tuple[str, str]] = {}

    def teaser_provider(cover_posts: list[Post]) -> dict[Key, tuple[str, str]]:
        nonlocal classifier
        missing = [p for p in cover_posts if _key(p) not in teaser_cache]
        if missing:
            if classifier is None:
                classifier = GeminiClassifier(config)
            teaser_cache.update(classifier.teasers(missing))
        return {_key(p): teaser_cache[_key(p)] for p in cover_posts if _key(p) in teaser_cache}

    def render(chosen: list[Post], *, fit_only: bool) -> tuple[list[Path], list[Post]]:
        # The only place that calls render_pages. Teasers (LLM) are only
        # needed for the full render: the billboard doesn't affect layout
        # during the mosaic fit pass.
        return render_pages(
            chosen, out_dir, basename=basename, run_date=run_date,
            page_size="A4", landscape=True, max_pages=max_pages,
            importance_by_key=importance_by_key,
            photo_relevance_by_key=photo_relevance_by_key,
            photo_path_by_key=photo_path_by_key,
            teaser_provider=None if fit_only else teaser_provider,
            rubric_by_channel=config.rubric_by_channel,
            rubric_order=config.rubric_order,
            videos=videos,
            fit_only=fit_only,
        )

    def shorten(candidates: list[Post]) -> dict[Key, str]:
        # Ready-made texts from the previous assembly first, Gemini gets
        # only the rest (the classifier is created lazily: nothing to
        # shorten means no key is needed).
        nonlocal classifier
        result = {_key(p): known[_key(p)] for p in candidates if _key(p) in known}
        rest = [p for p in candidates if _key(p) not in result]
        if rest:
            if classifier is None:
                classifier = GeminiClassifier(config)
            result.update(classifier.shorten(rest, target_sentences=SHORTEN_TARGET_SENTENCES))
        return result

    def with_texts(items: list[Post], texts: dict[Key, str]) -> list[Post]:
        return [replace(p, text=texts[_key(p)]) if _key(p) in texts else p for p in items]

    _, leftover = render(posts, fit_only=True)

    # Shorten long texts of future brief tiles ahead of time, but only if the
    # issue didn't fit as-is: one such tile inflates the whole row, and
    # shortening the leftover doesn't touch it — it's already on the sheet.
    # Lead and feature are left untouched: a long text suits them.
    if leftover:
        brief = brief_keys(posts, importance_by_key)
        to_prepare = [p for p in posts if _key(p) in brief and len(p.text) > BRIEF_SHORTEN_MIN_CHARS]
        if to_prepare:
            prepared = shorten(to_prepare)
            # Mark only what actually came back: if Gemini skipped part of
            # the batch, the main loop will give those posts a second try.
            already_shortened.update(prepared)
            shortened_count += len(prepared)
            logger.info(
                "заранее сокращено нейронкой %d из %d длинных brief-новостей", len(prepared), len(to_prepare)
            )
            posts = with_texts(posts, prepared)
            _, leftover = render(posts, fit_only=True)

    probes = 0
    chosen: list[Post] | None = None  # set only by binary search; otherwise we print all posts
    while leftover:
        to_shorten_keys = {
            _key(p)
            for p in leftover
            if importance_by_key.get(_key(p), 0) > IMPORTANCE_DROP_THRESHOLD
            and _key(p) not in already_shortened
            and len(p.text) > SHORTEN_MIN_CHARS
        }

        if to_shorten_keys:
            to_shorten = [p for p in leftover if _key(p) in to_shorten_keys]
            shortened_texts = shorten(to_shorten)
            # Here we mark all sent keys: otherwise a model that stubbornly
            # doesn't return a post would loop the layout pass forever.
            already_shortened.update(to_shorten_keys)
            shortened_count += len(shortened_texts)
            logger.info("сокращено нейронкой %d новостей ради места в номере", len(shortened_texts))
            posts = with_texts(posts, shortened_texts)
            _, leftover = render(posts, fit_only=True)
            continue

        # Nothing left to shorten (not important enough, or already tried) —
        # we search for the largest k for which the k most significant news
        # items of the issue fit entirely. Invariant: lo fits (k=1 always
        # fits — layout._fit_bands guarantees this), hi doesn't (the current
        # number of posts). Monotonicity isn't strict (dropping items
        # changes tile tiers), so only a k that's actually been verified
        # without a leftover is valid; the answer is the best of those.
        by_significance = sorted(posts, key=_significance_key(importance_by_key), reverse=True)

        def top(k: int) -> list[Post]:
            keep = {_key(p) for p in by_significance[:k]}
            return [p for p in posts if _key(p) in keep]  # original post order

        # k=1 isn't checked in the search (considered to fit); if the
        # guarantee is still violated, the final render will return a
        # leftover, and it won't make it into the issue's composition
        # (see below).
        lo, hi = 1, len(posts)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            probes += 1
            _, probe_leftover = render(top(mid), fit_only=True)
            if probe_leftover:
                hi = mid
            else:
                lo = mid
        chosen = top(lo)
        chosen_keys = {_key(p) for p in chosen}
        dropped.extend(p for p in posts if _key(p) not in chosen_keys)
        break

    if chosen is None:
        chosen = posts
    # The one full render — into the issue directory, with billboard teasers.
    out_paths, final_leftover = render(chosen, fit_only=False)
    published = chosen
    if final_leftover:
        # The fit guarantee was violated: what didn't fit wasn't printed, so
        # it must not be included in the issue's composition (otherwise
        # dedup would discard news items the reader never saw).
        lost = {_key(p) for p in final_leftover}
        published = [p for p in chosen if _key(p) not in lost]
        dropped.extend(p for p in chosen if _key(p) in lost)
        logger.warning(
            "финальный рендер не вместил %d новостей, хотя подгонка прошла — исключены из состава номера",
            len(lost),
        )
    logger.info(
        "номер собран: %d стр., сокращено=%d, выброшено из-за нехватки места=%d, проб подгонки=%d",
        len(out_paths), shortened_count, len(dropped), probes,
    )
    return NewspaperResult(out_paths, dropped, shortened_count, published)


def save_newspaper_issue(conn: sqlite3.Connection, run_id: int, result: NewspaperResult) -> None:
    """Records the composition of the just-assembled issue for run_id as
    "printed" — history for deduping subsequent runs. Called from
    assemble_issue after every issue assembly (button, CLI, render_preview.py;
    later — printing, Stage 4); reassembling the same run_id replaces the
    composition rather than appending to it (see storage.save_issue)."""
    save_issue(conn, run_id, result.published)


ISSUE_BASENAME = "page"


def issue_dir(config: Config, run_id: int) -> Path:
    """Directory of a run's assembled issue files: page_N.png pages and the
    PDF cache (a4.pdf/a3.pdf). Lives next to the DB, not in the repo — it's data."""
    return config.db_path.parent / "issues" / f"run_{run_id}"


def issue_pages(config: Config, run_id: int) -> list[Path]:
    """The issue's pages in order. Sorting is numeric: by string, page_10
    would come before page_2."""
    found: list[tuple[int, Path]] = []
    for path in issue_dir(config, run_id).glob(f"{ISSUE_BASENAME}_*.png"):
        suffix = path.stem[len(ISSUE_BASENAME) + 1:]
        if suffix.isdigit():
            found.append((int(suffix), path))
    return [p for _, p in sorted(found)]


def enough_news_for_issue(included_count: int) -> bool:
    """Whether enough news items made it to print (`included`) for an issue."""
    return included_count >= MIN_ISSUE_NEWS


def few_news_message(included_count: int) -> str:
    """Message to the user when the issue isn't assembled due to lack of news."""
    return (
        f"За сутки набралось мало новостей: {included_count} "
        f"(нужно хотя бы {MIN_ISSUE_NEWS}) — номер не собран."
    )


def assemble_issue(
    config: Config,
    conn: sqlite3.Connection,
    run: Run,
    max_pages: int = NEWSPAPER_MAX_PAGES,
) -> NewspaperResult | None:
    """Assembles the issue from a saved run and records its composition for
    dedup. The single entry point for the console button, the CLI, and
    render_preview.py. None — news items that made it to print are fewer
    than MIN_ISSUE_NEWS: the issue isn't assembled, old pages and the DB
    composition stay as they were."""
    outcomes = load_run(conn, run.run_id)
    if not enough_news_for_issue(sum(1 for o in outcomes if o.included)):
        return None
    videos = load_run_videos(conn, run.run_id)

    # Take texts shortened by the LLM in this run's previous assembly from
    # the printed composition — BEFORE save_newspaper_issue, which will
    # overwrite it. A text is considered shortened if it differs from the original.
    original = {_key(o.post): o.post.text for o in outcomes}
    known_shortened = {
        k: text for k, text in load_issue_texts(conn, run.run_id).items()
        if k in original and text != original[k]
    }

    out_dir = issue_dir(config, run.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A rebuild may produce fewer pages than the previous one, and the PDF is
    # stitched from old images: without cleanup the directory would keep
    # stale pages and an outdated cache that the console would serve as the
    # current issue.
    for stale in [*out_dir.glob(f"{ISSUE_BASENAME}_*.png"), *out_dir.glob("*.pdf")]:
        stale.unlink()

    result = build_newspaper(
        config, outcomes, out_dir, basename=ISSUE_BASENAME,
        run_date=run.period_end or run.started_at, max_pages=max_pages,
        videos=videos, known_shortened=known_shortened,
    )
    # The issue's composition is "already printed" for deduping subsequent runs.
    save_newspaper_issue(conn, run.run_id, result)
    return result
