"""Эвристический отсев "не новостей" — до обращения к LLM.

Ловит только то, что не требует понимания смысла: пустые посты, медиа
с совсем короткой подписью, голый URL без текста, явную рекламу
(маркировка erid= обязательна по закону для рекламных постов в РФ).
Спорные случаи (мем без медиа, вопрос в чат, философствование) сюда
не входят — это Этап 2 п.2, решает LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .storage import Post

# Порог взят из примера в брифе: "видео с подписью в 5 слов" — граница,
# после которой подпись уже не считается содержательной.
MEDIA_CAPTION_MIN_WORDS = 5

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+")
# erid= — обязательная по закону РФ маркировка рекламной ссылки;
# "ИНН <10-12 цифр>" — обязательное указание рекламодателя в самом тексте
# (например: "Реклама. ООО «Ромашка». ИНН 7715744096");
# "устанавливайте/скачайте приложение" — типовой призыв в рекламе мобильных
# приложений, встречается и без erid (найдено LLM на реальных данных — банковское
# приложение без маркировки);
# короткие ссылки-редиректы (ya.cc, clck.ru, vk.cc, goo.gl) — типичный формат
# рекламных ссылок в этих каналах, доп. подстраховка на случай отсутствия erid.
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
    reason: str | None  # None, если пост прошёл отсев


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
    """Возвращает (кандидаты в новости, все результаты классификации)."""
    results = [classify(post) for post in posts]
    candidates = [r.post for r in results if r.is_news_candidate]
    return candidates, results
