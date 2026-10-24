"""Reader feedback (Stage 6 items 2-3): selecting posts for "rank by
importance" surveys and the metric for how well the reader's order matches
the model's order.

The survey's idea: the reader ranks 5 posts in order (1 — most important to
them, 5 — least). Comparing this order to the model's score order shows
where the model is wrong specifically for them. For the comparison to be
informative, the five posts are picked with DIFFERENT model scores (one per
level 1..5 where possible): if all five have the same score, there's
nothing to compare the reader's order against.

This layer is separate from the console (scripts/console.py) and the HTML
(feedback_html.py): here there's only selection, the metric, and DB access
through storage, no layout and no network.
"""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass

from .pipeline import PostOutcome, effective_importance, load_run
from .storage import (
    create_survey,
    list_runs,
    list_surveys,
    load_issue_texts,
    load_post_texts,
)

# How many posts in one survey and one onboarding round.
SURVEY_SIZE = 5
# Survey after an issue: how many posts from the issue and how many —
# dropped from it (included=1, but not in issue_posts: not enough room in
# the issue). Dropped posts are needed because they're exactly where you
# can see whether the model was right to push them aside: in the issue, all
# posts have already "passed" the model's scoring.
ISSUE_SURVEY_IN_ISSUE = 3
ISSUE_SURVEY_DROPPED = 2
# Onboarding: 3 rounds of 5 posts = 15 posts with real scores.
ONBOARDING_ROUNDS = 3
# How many of the most recent posts (with importance>0) to pick onboarding
# rounds from: recent posts are easier for the reader to recall context
# for, but the pool must be wider than 15, otherwise "spreading across
# levels" would have nothing to choose from.
ONBOARDING_POOL_LIMIT = 60


def model_score(outcome: PostOutcome) -> int:
    """The ONE place from which surveys get a post's "model score".

    This is the final score (Stage 6 item 4: a blend of generic and
    personal, for old runs — generic, 0 — unknown) via
    pipeline.effective_importance. Post selection, the agreement metric, and
    recording model_score in feedback_items all take it from here, and
    nowhere else is outcome.importance read directly.
    """
    return effective_importance(outcome)


@dataclass(frozen=True)
class Candidate:
    """A post candidate for a survey: a run outcome + the model's current score."""
    outcome: PostOutcome
    in_issue: bool
    score: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.outcome.post.channel, self.outcome.post.message_id)


def _targets(count: int) -> list[int]:
    """Target score levels for count posts, evenly spread over the 5..1 scale.
    For 5 posts — exactly 5,4,3,2,1; for fewer (few posts in the DB) —
    stretched over the whole scale, to still cover both the top and the bottom."""
    if count <= 0:
        return []
    if count == 1:
        return [3]
    return [round(5 - i * 4 / (count - 1)) for i in range(count)]


def pick_spread(
    pools: list[tuple[list[Candidate], int]], rng: random.Random
) -> list[Candidate]:
    """Greedy selection of posts with model scores spread across the scale.

    pools — groups of candidates with a quota: [(candidates, how many to
    take), ...] (for a post-issue survey — two groups: from the issue (3)
    and dropped (2); for onboarding — one group of 5). For target levels
    5,4,3,2,1 in turn, the closest-scoring not-yet-chosen post is taken from
    any group whose quota isn't exhausted; ties in closeness are broken
    randomly (rng), so repeated surveys don't show the same posts. Greedy
    isn't optimal, but it's simple and predictable; optimality isn't needed
    here — all that matters is not ending up with five identical scores
    when the DB has different ones.

    If there are fewer candidates than the quota, whatever is available is
    taken. The result is in selection order (descending target level), NOT
    in display order — the caller shuffles it (shuffle_for_display).
    """
    available = [list(cands) for cands, _ in pools]
    quota = [q for _, q in pools]
    total = min(sum(quota), sum(len(a) for a in available))
    chosen: list[Candidate] = []
    for target in _targets(total):
        best: tuple[float, float, int, int] | None = None
        for gi, group in enumerate(available):
            if quota[gi] <= 0:
                continue
            for ci, cand in enumerate(group):
                # The third component is a random tiebreaker: tuple
                # comparison only uses it when the distances are equal.
                rank = (abs(cand.score - target), rng.random(), gi, ci)
                if best is None or rank < best:
                    best = rank
        if best is None:
            break  # candidates ran out in groups that still had quota left
        _, _, gi, ci = best
        chosen.append(available[gi].pop(ci))
        quota[gi] -= 1
    return chosen


def shuffle_for_display(cands: list[Candidate], rng: random.Random) -> list[Candidate]:
    """Random display order: not by score, so as not to nudge the reader."""
    shuffled = list(cands)
    rng.shuffle(shuffled)
    return shuffled


def _issue_quotas(in_count: int, dropped_count: int) -> tuple[int, int]:
    """Quotas (from the issue, dropped) for the survey. Target is 3/2; if
    there are fewer than two dropped posts, the shortfall is made up from
    the issue (the survey's spec); if the issue also has fewer than three,
    we take however many there are and give the free slots to the
    remaining dropped posts (a full survey is better than one with two or
    three posts)."""
    dropped = min(ISSUE_SURVEY_DROPPED, dropped_count)
    in_issue = min(SURVEY_SIZE - dropped, in_count)
    dropped = min(dropped_count, SURVEY_SIZE - in_issue)
    return in_issue, dropped


def issue_candidates(
    conn: sqlite3.Connection, run_id: int
) -> tuple[list[Candidate], list[Candidate]]:
    """(issue posts, posts dropped from the issue) of run_id. Dropped are
    outcomes with included=1 that aren't in issue_posts. The model score
    comes via model_score, not directly from importance."""
    printed = load_issue_texts(conn, run_id)
    in_issue: list[Candidate] = []
    dropped: list[Candidate] = []
    for o in load_run(conn, run_id):
        if not o.included:
            continue
        key = (o.post.channel, o.post.message_id)
        cand = Candidate(o, key in printed, model_score(o))
        (in_issue if cand.in_issue else dropped).append(cand)
    return in_issue, dropped


def create_issue_survey(
    conn: sqlite3.Connection, run_id: int, rng: random.Random | None = None
) -> int | None:
    """Survey for run_id's issue; created lazily on first open and stays the
    same afterward (items are recorded in the DB — reopening doesn't
    resample or spawn new surveys). None — too few posts (<2) to have
    anything to rank."""
    existing = list_surveys(conn, "issue", run_id=run_id)
    if existing:
        return existing[0].survey_id
    rng = rng or random.Random()
    in_issue, dropped = issue_candidates(conn, run_id)
    q_in, q_out = _issue_quotas(len(in_issue), len(dropped))
    picked = pick_spread([(in_issue, q_in), (dropped, q_out)], rng)
    if len(picked) < 2:
        return None
    return _create(conn, "issue", run_id, shuffle_for_display(picked, rng))


def _create(
    conn: sqlite3.Connection, kind: str, run_id: int | None, ordered: list[Candidate]
) -> int:
    return create_survey(
        conn, kind, run_id,
        [(c.key[0], c.key[1], c.in_issue, c.score, pos) for pos, c in enumerate(ordered, start=1)],
    )


def onboarding_pool(conn: sqlite3.Connection) -> list[Candidate]:
    """The onboarding pool: posts with a model score > 0 from all runs, with
    no repeats of the same post (taken from its most recent run), most
    recently published first, no more than ONBOARDING_POOL_LIMIT."""
    seen: set[tuple[str, int]] = set()
    pool: list[Candidate] = []
    for run in list_runs(conn):  # newest runs first
        for o in load_run(conn, run.run_id):
            cand = Candidate(o, False, model_score(o))
            if cand.score <= 0 or cand.key in seen:
                continue
            seen.add(cand.key)
            pool.append(cand)
    pool.sort(key=lambda c: c.outcome.post.posted_at, reverse=True)
    return pool[:ONBOARDING_POOL_LIMIT]


def create_onboarding_surveys(
    conn: sqlite3.Connection, rng: random.Random | None = None
) -> list[int]:
    """Onboarding ranking rounds (up to 3 of 5 posts, no repeats between
    rounds), each a separate survey. Idempotent: if onboarding surveys
    already exist, returns them (the reader doesn't get different posts on
    reopening the form). An empty DB / too few posts means fewer rounds (an
    empty list in the limit); a round with <2 posts isn't created."""
    existing = list_surveys(conn, "onboarding")
    if existing:
        return [s.survey_id for s in existing]
    rng = rng or random.Random()
    pool = onboarding_pool(conn)
    ids: list[int] = []
    for _ in range(ONBOARDING_ROUNDS):
        picked = pick_spread([(pool, SURVEY_SIZE)], rng)
        if len(picked) < 2:
            break
        picked_keys = {c.key for c in picked}
        pool = [c for c in pool if c.key not in picked_keys]  # no repeats between rounds
        ids.append(_create(conn, "onboarding", None, shuffle_for_display(picked, rng)))
    return ids


def validate_ranks(ranks: dict[tuple[str, int], int | None]) -> str | None:
    """Validates the answer: every post has a rank, ranks are exactly 1..n
    with no repeats (the reader RANKS the posts rather than scoring each
    one separately: two 1's carry no information about order). Returns the
    error text for the reader, or None if everything's fine."""
    n = len(ranks)
    values = list(ranks.values())
    if any(v is None for v in values):
        return "Расставьте места для всех постов."
    if sorted(values) != list(range(1, n + 1)):
        return f"Места должны быть уникальными — от 1 до {n}, каждое по одному разу."
    return None


def agreement(user_ranks: list[int], scores: list[int]) -> float | None:
    """How well the reader's order matches the model's order — Kendall
    tau-b, -1..1 (1 — orders match completely, -1 — exactly opposite).

    user_ranks[i] — the reader's rank (1 — most important), scores[i] — the
    model's score (higher — more important) for the same post.

    Why Kendall and not Spearman: we have 5 items and some of the model's
    scores coincide (a 1..5 scale over five posts makes collisions
    unavoidable). Kendall counts pairs of posts and can be described in
    words just as faithfully ("the reader and the model agree on so-and-so
    many out of 10 pairs"), and tau-b handles ties correctly: pairs with
    equal model scores are counted as neither agreement nor disagreement,
    but they shrink the denominator, so with ties even a perfect match
    gives less than 1 — this is deliberate: a tie in scores honestly means
    "the model distinguishes between posts worse than the reader does".
    The reader has no ties (ranks are unique). None — the metric is
    undefined: fewer than two posts, or the model gave all of them the same
    score (nothing to compare).
    """
    n = len(user_ranks)
    if n < 2 or n != len(scores):
        return None
    concordant = discordant = model_ties = 0
    for i in range(n):
        for j in range(i + 1, n):
            ds = scores[i] - scores[j]
            if ds == 0:
                model_ties += 1
                continue
            # A lower reader rank = more important, so the sign of the rank
            # difference is flipped: "i more important than j" means rank_i < rank_j.
            du = user_ranks[j] - user_ranks[i]
            if du == 0:
                continue  # the reader shouldn't have ties; a safeguard against division/sign issues
            if (ds > 0) == (du > 0):
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) // 2
    denominator = math.sqrt((pairs - model_ties) * pairs)
    if denominator == 0:
        return None
    return (concordant - discordant) / denominator


def model_ranks(scores: list[int]) -> list[int]:
    """Ranks by model score (1 — highest), equal scores get the same rank
    (standard "competition" numbering 1,2,2,4)."""
    return [1 + sum(1 for other in scores if other > s) for s in scores]


def load_item_texts(
    conn: sqlite3.Connection, survey_kind: str, run_id: int | None, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    """Post texts for a survey, to show the reader. For issue posts — the
    text as printed (issue_posts, after LLM shortening): the reader rates
    what they saw on paper, not an original they never saw. For the rest
    (dropped, onboarding) — the original from posts."""
    printed = load_issue_texts(conn, run_id) if survey_kind == "issue" and run_id else {}
    originals = load_post_texts(conn, [k for k in keys if k not in printed])
    return {k: printed.get(k, originals.get(k, "")) for k in keys}


# Window for the "is the newspaper learning" indicator: average agreement
# of the last N surveys versus the previous N. Five, same as the survey
# size: enough to smooth out the noise of a single ranking, and it builds
# up over a couple of weeks.
LEARNING_WINDOW = 5


@dataclass(frozen=True)
class LearningStats:
    """Summary for the main console: how many surveys were submitted, the
    average agreement (Kendall tau-b) of the last LEARNING_WINDOW surveys
    and the previous LEARNING_WINDOW (None — no such surveys, or agreement
    is undefined)."""
    submitted: int
    recent_avg: float | None
    previous_avg: float | None


def learning_stats(conn: sqlite3.Connection) -> LearningStats:
    """Computes LearningStats over all submitted surveys (onboarding and
    post-issue), in submission order. Surveys without agreement (metric
    undefined: the model gave all posts equal scores) are counted in
    submitted, but not in the averages."""
    surveys = [
        s for kind in ("onboarding", "issue") for s in list_surveys(conn, kind) if s.submitted
    ]
    surveys.sort(key=lambda s: (s.submitted_at, s.survey_id))
    values = [s.agreement for s in surveys if s.agreement is not None]
    recent = values[-LEARNING_WINDOW:]
    previous = values[-2 * LEARNING_WINDOW:-LEARNING_WINDOW]

    def avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    return LearningStats(len(surveys), avg(recent), avg(previous))
