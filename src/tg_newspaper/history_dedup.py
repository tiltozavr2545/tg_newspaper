"""Дедупликация кандидатов текущего прогона против уже опубликованного —
Этап 2 п.4. Окно сравнения — последние несколько ПРОГОНОВ (не дней:
проект запускается по кнопке, а не по расписанию, календарное окно не
имеет смысла, см. AGENTS.md "Запуск") — так ловятся новости, которые
канал репостит или пересказывает спустя несколько запусков.

Асимметрично по конструкции: уже опубликованное (history) никогда не
меняется и не исключается — оно уже напечатано. Может исключаться только
новый кандидат, если он дублирует что-то из history.

Переиспользует ровно те же механизмы, что дедупликация внутри одного
прогона (dedup.py, paraphrase_dedup.py, classifier.resolve_paraphrase_batch)
— тот же промпт, та же логика "минимального покрывающего набора фактов",
просто применённая к смешанным группам [кандидат, ...похожие из истории].
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .classifier import BATCH_SIZE, GeminiClassifier
from .dedup import COPYPASTE_SIMILARITY_THRESHOLD, normalize
from .paraphrase_dedup import SIMILARITY_THRESHOLD, GeminiEmbedder, _cosine_similarity
from .storage import Post

# Столько последних прогонов сравниваем — см. implementation-plan.md, Этап 2 п.4
# (в брифе было "5 дней", после перехода на запуск по кнопке — "5 прогонов").
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
    """Возвращает (кандидаты без дублей уже опубликованного, [(исключённый
    кандидат, причина), ...]). history не меняется и не участвует в
    исключениях — только кандидаты."""
    if not history or not candidates:
        return candidates, []

    excluded: list[tuple[Post, str]] = []

    # 1. Копипаст-эвристика — дёшево, без LLM.
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

    # 2. Черновой фильтр по эмбеддингам + подтверждение LLM — та же логика,
    #    что для дублей-пересказов внутри одного прогона (paraphrase_dedup.py):
    #    сходство эмбеддингов путает "тот же факт" с "просто похожая тема",
    #    окончательное решение — за LLM (resolve_paraphrase_batch).
    texts = [p.text.strip() for p in remaining] + [p.text.strip() for p in history]
    vectors = embedder.embed(texts)
    candidate_vectors = vectors[: len(remaining)]
    history_vectors = vectors[len(remaining) :]

    # group[0] — кандидат, group[1:] — похожие на него посты из history.
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
                continue  # нет ответа — как и везде в classifier.py, оставляем
            # Кандидат — всегда позиция 0 внутри своей группы. Если LLM не
            # включила его в keep_indices — все его факты уже есть в history.
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
