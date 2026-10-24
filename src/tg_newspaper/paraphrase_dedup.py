"""Deduplicating news retold in different words — the second half of
plan Stage 2 item 2.

The copy-paste heuristic (dedup.py) only catches textual matches; this is
semantic: the same news item phrased in each channel's own words. Real-data
example: two posts about refunds for the Kanye West concert — text similarity
(rapidfuzz) only 77%, below the copy-paste threshold, but they're the same
news by meaning.

Embeddings use the same provider and proxy (Cloudflare Worker) as the LLM
classification in classifier.py.

Important: this module only does a preliminary grouping of posts that are
SIMILAR by topic — it's a rough filter, not the final decision. Real data
showed that pure cosine similarity of embeddings can't distinguish "the same
fact in different words" from "different facts of one unfolding story" — both
situations land in the ~0.86-0.90 similarity range (example: two retellings of
the concert-refund news score 0.99, while "fire — cause/victims/photo" — three
DIFFERENT facts of the same event — score 0.78-0.90, i.e. indistinguishable by
the number alone). That's why the threshold here is deliberately set low
(better to occasionally offer the LLM an extra group than to miss a real
duplicate) — the final call on "is this really one fact or not" is made by
classifier.GeminiClassifier.resolve_paraphrase_clusters.
"""

from __future__ import annotations

import logging
import math
import time

from google import genai
from google.genai import types

from .config import Config
from .gemini_retry import is_transient, retry_delay_seconds
from .storage import Post

logger = logging.getLogger(__name__)

# text-embedding-004 is no longer supported (confirmed with a real key,
# 404 NOT_FOUND) — gemini-embedding-001 is the current GA model replacing it.
# gemini-embedding-2 has shipped (GA 2026-04-22), but SIMILARITY_THRESHOLD is
# calibrated for 001; switching requires recalibrating the threshold on live
# data — deliberately deferred.
EMBEDDING_MODEL = "gemini-embedding-001"

# Gemini API limit on batchEmbedContents — max texts per request.
EMBED_BATCH_SIZE = 100

# embed_content lives under a separate, much tighter free quota than
# generateContent (100 requests/min vs. a much higher text limit) — in a real
# run with two back-to-back calls to find_paraphrase_clusters +
# filter_against_history, this reliably hit 429 without a single retry
# (the google-genai library itself only retries network failures, not quota).
# There's no replacement model (only one embedding model exists), so we just
# wait and retry the same request — the same retryDelay-waiting logic as in
# classifier.py._generate_structured.
_MAX_RETRIES = 5

# Cosine similarity threshold for the rough grouping of candidates into
# duplicates (not the final decision — see the warning in the file header).
# Calibrated on real data: genuine retellings score 0.87-0.99, truly unrelated
# posts score 0.68-0.75. Set with a margin below the weakest genuine retelling
# found (0.873), to avoid losing a candidate; false positives (0.78-0.90) are
# filtered out by the LLM in resolve_paraphrase_clusters.
SIMILARITY_THRESHOLD = 0.80


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class GeminiEmbedder:
    def __init__(self, config: Config) -> None:
        if not config.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env — embeddings are unavailable.")
        http_options = (
            types.HttpOptions(base_url=config.gemini_base_url)
            if config.gemini_base_url
            else None
        )
        self._client = genai.Client(api_key=config.gemini_api_key, http_options=http_options)

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
                )
            except Exception as exc:  # noqa: BLE001
                if is_transient(exc):
                    delay = retry_delay_seconds(exc)
                    logger.warning(
                        "embed_content unavailable (%s), waiting %.0fs and retrying",
                        str(exc)[:70], delay,
                    )
                    last_exc = exc
                    time.sleep(delay)
                    continue
                raise
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                raise RuntimeError(
                    f"batchEmbedContents returned {len(embeddings)} of {len(batch)}"
                )
            return [e.values for e in embeddings]
        raise last_exc or RuntimeError("embed_content never responded")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns a vector for each text, in the same order."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            vectors.extend(self._embed_batch(batch))
            logger.info("embeddings ready: %d-%d of %d", start, start + len(batch), len(texts))
        return vectors


def find_paraphrase_clusters(
    posts: list[Post], embedder: GeminiEmbedder
) -> list[list[Post]]:
    """Groups posts by semantic similarity (retellings of the same news item).

    Every post ends up in exactly one cluster; posts with no duplicates get
    a cluster of size 1.
    """
    if not posts:
        return []
    texts = [post.text.strip() for post in posts]
    vectors = embedder.embed(texts)

    # Single-linkage: a post joins a cluster if it's similar to at least ONE
    # current member — a deliberate choice, after two rounds of fixes against
    # real data:
    #
    # 1. Originally this was single-linkage — it broke on a "hub": a post
    #    with generic "tech jargon" dragged completely unrelated posts
    #    (Vivo/RTX/in-game ads) into one cluster, each individually similar
    #    to it, but not to each other.
    # 2. Switched to complete-linkage (similar to ALL members) — this fixed
    #    the hub problem, but introduced a new, quieter one: processing
    #    order decides a post's fate. A real case: two posts about a cocaine
    #    seizure (0.98 similarity between them!) didn't end up in the same
    #    cluster, because one of them had earlier in the list latched onto an
    #    unrelated cluster of three loosely-connected political stories
    #    (0.80-0.81 with each individually), which raised the bar for joining
    #    it higher than a genuine duplicate could clear against two of the
    #    three members (0.796, 0.797 — just under the threshold).
    #
    # By this point, the strict LLM prompt (_PARAPHRASE_RESOLUTION_INSTRUCTION
    # in classifier.py — it requires naming a specific shared fact, not just a
    # topic) had already repeatedly proven, on real data, that it correctly
    # sorts out even mixed groups (migrants vs. warehouse break-ins,
    # embezzlement vs. fraud, a 4-post cluster about Dud (the YouTuber) that
    # kept what belonged together and split the rest out by fact). So we
    # decided to go back to single-linkage and keep clusters wider: we lose a
    # genuine duplicate less often, and leave sorting out a mixed group to the
    # LLM rather than to a similarity threshold.
    clusters: list[list[int]] = []
    for i, vec in enumerate(vectors):
        placed = False
        for cluster in clusters:
            if any(_cosine_similarity(vec, vectors[j]) >= SIMILARITY_THRESHOLD for j in cluster):
                cluster.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])

    return [[posts[i] for i in cluster] for cluster in clusters]
