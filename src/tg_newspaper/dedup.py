"""Эвристика на дословные/близкие копипасты — до обращения к LLM.

Ловит только текстуальное совпадение (репост, копипаста слово-в-слово
или с косметическими правками). Пересказ той же новости другими
словами эта эвристика не поймает — это paraphrase_dedup.py (эмбеддинги).
Окно сравнения — только кандидаты одного прогона (одних суток);
скользящее окно в 5 дней уже опубликованного — Этап 2 п.4, отдельно.

Только группирует похожие посты — какой из них оставить, решает
classifier.GeminiClassifier.choose_best_versions (см. там же, почему
"самый ранний"/"самый длинный" не подошли).
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .storage import Post

# Порог похожести нормализованного текста (rapidfuzz.token_sort_ratio, 0-100),
# выше которого пара постов считается копипастой, а не разными новостями.
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
    """Группирует посты по текстуальному сходству (копипаста/репост).

    Каждый пост попадает ровно в один кластер; посты без дублей — в
    кластер размера 1.
    """
    normalized = [normalize(post.text) for post in posts]
    clusters: list[list[int]] = []  # индексы posts

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
