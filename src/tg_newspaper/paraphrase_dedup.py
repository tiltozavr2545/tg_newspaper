"""Дедупликация новостей, пересказанных разными словами — вторая половина
Этапа 2 п.2 плана.

Копипаст-эвристика (dedup.py) ловит только текстуальное совпадение; здесь —
семантическое: одна и та же новость, сформулированная разными каналами
своими словами. Пример на реальных данных: два поста про возврат денег за
концерт Канье Уэста — схожесть текста (rapidfuzz) всего 77%, ниже порога
копипасты, но по смыслу это одна новость.

Эмбеддинги — тот же провайдер и прокси (Cloudflare Worker), что и
LLM-классификация в classifier.py.

Важно: этот модуль только предварительно группирует ПОХОЖИЕ по теме посты —
это черновой фильтр, не окончательное решение. На реальных данных выяснилось,
что чистое косинусное сходство эмбеддингов не отличает "тот же факт другими
словами" от "разные факты одной развивающейся истории" — обе ситуации дают
схожесть в диапазоне ~0.86-0.90 (пример: два пересказа новости про возврат
денег за концерт дают 0.99, а "пожар — причина/жертвы/фото" — три РАЗНЫХ
факта одного события — дают 0.78-0.90, то есть чисто по числу неотличимы).
Поэтому порог здесь занижен нарочно (лучше лишний раз ошибочно предложить
группу LLM, чем пропустить настоящий дубль) — окончательное решение "это
правда один факт или нет" принимает classifier.GeminiClassifier.resolve_paraphrase_clusters.
"""

from __future__ import annotations

import logging
import math

from google import genai
from google.genai import types

from .config import Config
from .storage import Post

logger = logging.getLogger(__name__)

# text-embedding-004 снят с поддержки (проверено на реальном ключе,
# 404 NOT_FOUND) — gemini-embedding-001 актуальная GA-модель на замену.
EMBEDDING_MODEL = "gemini-embedding-001"

# Лимит Gemini API на batchEmbedContents — максимум текстов в одном запросе.
EMBED_BATCH_SIZE = 100

# Порог косинусного сходства эмбеддингов для черновой группировки кандидатов
# в дубли (не финальное решение — см. предупреждение в шапке файла).
# Откалиброван на реальных данных: настоящие пересказы дают 0.87-0.99, чисто
# не связанные посты — 0.68-0.75. Взят с запасом ниже самого слабого
# найденного настоящего пересказа (0.873), чтобы не потерять кандидата;
# ложные срабатывания (0.78-0.90) отсеивает LLM в resolve_paraphrase_clusters.
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
            raise RuntimeError("GEMINI_API_KEY не задан в .env — эмбеддинги недоступны.")
        http_options = (
            types.HttpOptions(base_url=config.gemini_base_url)
            if config.gemini_base_url
            else None
        )
        self._client = genai.Client(api_key=config.gemini_api_key, http_options=http_options)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Возвращает вектор для каждого текста, в том же порядке."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            response = self._client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=batch,
                config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
            )
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                raise RuntimeError(
                    f"batchEmbedContents вернул {len(embeddings)} из {len(batch)}"
                )
            vectors.extend(e.values for e in embeddings)
            logger.info("эмбеддинги готовы: %d-%d из %d", start, start + len(batch), len(texts))
        return vectors


def find_paraphrase_clusters(
    posts: list[Post], embedder: GeminiEmbedder
) -> list[list[Post]]:
    """Группирует посты по семантическому сходству (пересказ той же новости).

    Каждый пост попадает ровно в один кластер; посты без дублей — в
    кластер размера 1.
    """
    if not posts:
        return []
    texts = [post.text.strip() for post in posts]
    vectors = embedder.embed(texts)

    # Одна связь (single-linkage): пост входит в кластер, если похож хотя бы
    # на ОДНОГО текущего участника — сознательно, после двух туров правки на
    # реальных данных:
    #
    # 1. Изначально было single-linkage — сломалось на "хабе": пост с общей
    #    "техно-лексикой" утащил в один кластер вообще не связанные посты
    #    (Vivo/RTX/реклама в играх), похожие на него по отдельности, но не
    #    друг на друга.
    # 2. Заменили на complete-linkage (похож на ВСЕХ участников) — это
    #    исправило хаб, но внесло новую, более тихую поломку: порядок
    #    обработки решает судьбу поста. Реальный случай — два поста про
    #    изъятие кокаина (схожесть между ними 0.98!) не попали в один
    #    кластер, потому что один из них раньше по списку прибился к чужому
    #    кластеру из трёх слабо связанных политических историй (0.80-0.81
    #    с каждой по отдельности) и тем самым поднял требования для входа
    #    в него выше, чем настоящий дубль мог дать по двум из трёх членов
    #    (0.796, 0.797 — чуть ниже порога).
    #
    # К этому моменту жёсткий промпт LLM (_PARAPHRASE_RESOLUTION_INSTRUCTION
    # в classifier.py — требует называть конкретный общий факт, а не тему)
    # уже неоднократно доказал на реальных данных, что верно разбирает даже
    # смешанные группы (мигранты vs укрытия склада, растрата vs мошенничество,
    # кластер из 4 новостей про Дудя — оставил нужное, остальное разложил по
    # разным фактам). Поэтому решили вернуться к single-linkage и держать
    # кластеры пошире: реже теряем настоящий дубль, а разбор смешанной группы
    # отдаём LLM, а не порогу схожести.
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
