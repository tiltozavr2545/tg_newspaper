"""Deduplicating the current run's candidates against what's already been
published — Stage 2 item 4. "Already published" means what ACTUALLY ended up
in assembled issues (storage.issue_posts, with the printed text after any
shortening), not what merely passed selection (pipeline_outcomes.included=1):
some posts that pass selection don't fit in the issue and get dropped, and
such news shouldn't be blocked tomorrow as "already in the paper". The window
is the last several ASSEMBLED ISSUES (not days, not runs: the project is
started by a button, so a calendar window makes no sense, see AGENTS.md
"Running" — and runs with no assembled issue shouldn't push out what was
actually printed) — this catches news that a channel reposts or retells a
few issues later. In a DB created before issue_posts existed, history is
empty until the first issue is assembled.

Asymmetric by design: already-published content (history) is never changed
or excluded — it's already been printed. A story continuing with new facts
passes through: a candidate is excluded only if ALL of its facts were
already printed (decided by the LLM, resolve_paraphrase_batch). Only a new
candidate can be excluded, and only if it duplicates something from history.

Reuses exactly the same mechanisms as deduplication within a single run
(dedup.py, paraphrase_dedup.py, classifier.resolve_paraphrase_batch) — the
same prompt, the same "minimal covering set of facts" logic, just applied to
mixed groups [candidate, ...similar items from history].
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .classifier import BATCH_SIZE, GeminiClassifier
from .dedup import COPYPASTE_SIMILARITY_THRESHOLD, normalize
from .paraphrase_dedup import SIMILARITY_THRESHOLD, GeminiEmbedder, _cosine_similarity
from .storage import Post

# This many of the latest ASSEMBLED ISSUES are compared against — see
# implementation-plan.md, Stage 2 item 4 (the brief said "5 days", after
# switching to button-triggered runs it became "5 runs", and after review,
# 5 actually assembled issues: runs for which no issue was assembled don't
# count toward the window).
HISTORY_RUNS_WINDOW = 5


def _copypaste_match(candidate_text: str, history: list[Post]) -> Post | None:
    normalized_candidate = normalize(candidate_text)
    if not normalized_candidate:
        return None
    for h in history:
        normalized_history = normalize(h.text)
        if not normalized_history:
            continue
        if fuzz.token_sort_ratio(normalized_candidate, normalized_history) >= (
            COPYPASTE_SIMILARITY_THRESHOLD
        ):
            return h
    return None


def filter_against_history(
    candidates: list[Post],
    history: list[Post],
    classifier: GeminiClassifier,
    embedder: GeminiEmbedder,
) -> tuple[list[Post], list[tuple[Post, str]]]:
    """Returns (candidates with no duplicates of already-published content,
    [(excluded candidate, reason), ...]). history is never changed and never
    participates in exclusions — only candidates do."""
    if not history or not candidates:
        return candidates, []

    excluded: list[tuple[Post, str]] = []

    # 1. Copy-paste heuristic — cheap, no LLM.
    remaining: list[Post] = []
    for c in candidates:
        match = _copypaste_match(c.text, history)
        if match is not None:
            excluded.append(
                (c, f"копипаст-дубль уже опубликованного {match.channel}/{match.message_id}")
            )
        else:
            remaining.append(c)

    if not remaining:
        return [], excluded

    # 2. Rough embedding-based filter + LLM confirmation — the same logic as
    #    for retelling-duplicates within a single run (paraphrase_dedup.py):
    #    embedding similarity confuses "the same fact" with "just a similar
    #    topic", the final decision is up to the LLM (resolve_paraphrase_batch).
    texts = [p.text.strip() for p in remaining] + [p.text.strip() for p in history]
    vectors = embedder.embed(texts)
    candidate_vectors = vectors[: len(remaining)]
    history_vectors = vectors[len(remaining) :]

    # group[0] is the candidate, group[1:] are its similar posts from history.
    groups: list[list[Post]] = []
    group_candidates: list[Post] = []
    for i, cvec in enumerate(candidate_vectors):
        matches = [
            history[j]
            for j, hvec in enumerate(history_vectors)
            if _cosine_similarity(cvec, hvec) >= SIMILARITY_THRESHOLD
        ]
        if matches:
            groups.append([remaining[i], *matches])
            group_candidates.append(remaining[i])

    if not groups:
        return remaining, excluded

    excluded_from_groups: set[tuple[str, int]] = set()
    for start in range(0, len(groups), BATCH_SIZE):
        batch = groups[start : start + BATCH_SIZE]
        batch_candidates = group_candidates[start : start + BATCH_SIZE]
        decisions = classifier.resolve_paraphrase_batch(batch)
        by_cluster = {d.cluster_index: d for d in decisions}
        for local_idx, group in enumerate(batch):
            decision = by_cluster.get(local_idx)
            if decision is None:
                continue  # no answer — as everywhere in classifier.py, keep it
            # The candidate is always position 0 within its group. If the LLM
            # didn't include it in keep_indices — all its facts are already in history.
            if 0 not in decision.keep_indices:
                candidate = batch_candidates[local_idx]
                matched = group[1]
                excluded.append(
                    (
                        candidate,
                        f"дубль/пересказ уже опубликованного {matched.channel}/"
                        f"{matched.message_id}: {decision.reason}",
                    )
                )
                excluded_from_groups.add((candidate.channel, candidate.message_id))

    final = [c for c in remaining if (c.channel, c.message_id) not in excluded_from_groups]
    return final, excluded
