"""Heuristic for verbatim/near-verbatim copy-pastes — runs before calling the LLM.

Only catches textual matches (reposts, word-for-word copy-pastes, or ones
with cosmetic edits). A retelling of the same news in different words is
not caught by this heuristic — that's paraphrase_dedup.py (embeddings).
The comparison window covers only candidates from a single run (one day);
the 5-day rolling window over already-published posts is Stage 2 item 4,
handled separately.

Only groups similar posts — which one to keep is decided by
classifier.GeminiClassifier.choose_best_versions (see there for why
"earliest"/"longest" didn't work).
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .storage import Post

# Normalized-text similarity threshold (rapidfuzz.token_sort_ratio, 0-100)
# above which a pair of posts is considered a copy-paste rather than distinct news.
COPYPASTE_SIMILARITY_THRESHOLD = 85

_URL_RE = re.compile(r"https?://\S+")
_MD_RE = re.compile(r"[*_`\[\]()]")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = _URL_RE.sub("", text)
    text = _MD_RE.sub("", text)
    text = text.lower()
    text = _WS_RE.sub(" ", text).strip()
    return text


def find_copypaste_clusters(posts: list[Post]) -> list[list[Post]]:
    """Groups posts by textual similarity (copy-paste/repost).

    Each post ends up in exactly one cluster; posts without duplicates
    end up in a cluster of size 1.
    """
    normalized = [normalize(post.text) for post in posts]
    clusters: list[list[int]] = []  # indices into posts

    for i, text in enumerate(normalized):
        placed = False
        for cluster in clusters:
            representative = normalized[cluster[0]]
            if not text or not representative:
                continue
            score = fuzz.token_sort_ratio(text, representative)
            if score >= COPYPASTE_SIMILARITY_THRESHOLD:
                cluster.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])

    return [[posts[i] for i in cluster] for cluster in clusters]
