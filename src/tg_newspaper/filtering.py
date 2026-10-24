"""Heuristic filtering of "non-news" — before calling the LLM.

Only catches what doesn't require understanding meaning: empty posts, media
with a very short caption, a bare URL with no text, obvious ads (the erid=
marker is legally required for ad posts in Russia). Borderline cases (a meme
with no media, a chat question, musing) are not covered here — that's
Stage 2 item 2, decided by the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .storage import Post

# Threshold taken from the example in the brief: "a video with a 5-word caption" —
# the boundary past which a caption is no longer considered substantive.
MEDIA_CAPTION_MIN_WORDS = 5

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+")
# erid= — ad-link marker legally required in Russia;
# "INN <10-12 digits>" — legally required disclosure of the advertiser in the
# text itself (e.g. "Реклама. ООО «Ромашка». ИНН 7715744096");
# "устанавливайте/скачайте приложение" ("install/download the app") — a typical
# call to action in mobile app ads, also seen without erid (found by the LLM on
# real data — an unmarked bank app ad);
# short redirect links (ya.cc, clck.ru, vk.cc, goo.gl) — a typical format for ad
# links on these channels, extra safety net in case erid is missing.
_AD_MARKER_RE = re.compile(
    r"erid[=%]"
    r"|инн\W{0,10}\d{10}(?:\d{2})?"
    r"|(?:устанавлива|скач)\w*\s+приложени"
    r"|\b(?:ya\.cc|clck\.ru|vk\.cc|goo\.gl)/",
    re.IGNORECASE | re.UNICODE,
)


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


@dataclass(frozen=True)
class FilterResult:
    post: Post
    is_news_candidate: bool
    reason: str | None  # None if the post passed filtering


def classify(post: Post) -> FilterResult:
    text = post.text.strip()

    if _AD_MARKER_RE.search(text):
        return FilterResult(post, False, "реклама (эвристический маркер)")

    if not text:
        return FilterResult(post, False, "пустой текст")

    text_without_urls = _URL_RE.sub("", text).strip()
    if not text_without_urls:
        return FilterResult(post, False, "голый URL без текста")

    if post.has_media and _word_count(text) < MEDIA_CAPTION_MIN_WORDS:
        return FilterResult(post, False, "медиа со слишком короткой подписью")

    return FilterResult(post, True, None)


def filter_posts(posts: list[Post]) -> tuple[list[Post], list[FilterResult]]:
    """Returns (news candidates, all classification results)."""
    results = [classify(post) for post in posts]
    candidates = [r.post for r in results if r.is_news_candidate]
    return candidates, results
