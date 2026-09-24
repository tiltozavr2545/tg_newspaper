"""Единый прогон полного пайплайна ("Собрать газету"): сбор постов за
последние сутки через Telethon, эвристики, дедупликация, LLM-классификация
— с детальным результатом по каждому посту: на каком этапе он отсеялся и
почему, или что дошёл до печати. Используется и scripts/run_pipeline.py
(запуск из терминала), и локальной консолью (кнопка "Собрать газету") —
чтобы не дублировать логику пайплайна в нескольких местах.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .classifier import BATCH_SIZE, GeminiClassifier
from .collector import collect_all, collection_since
from .config import Config
from .dedup import find_copypaste_clusters
from .filtering import filter_posts
from .history_dedup import HISTORY_RUNS_WINDOW, filter_against_history
from .paraphrase_dedup import GeminiEmbedder, find_paraphrase_clusters
from .storage import (
    Post,
    connect,
    create_run,
    load_outcomes,
    load_posts,
    load_recent_included_posts,
    save_outcomes,
    save_posts,
)

Key = tuple[str, int]


@dataclass(frozen=True)
class PostOutcome:
    post: Post
    included: bool
    stage: str  # "heuristic" | "copypaste" | "classification" | "paraphrase" | "final"
    reason: str
    # LLM решила, что без фото/видео текст поста неполон или непонятен
    # (см. _CLASSIFICATION_INSTRUCTION в classifier.py) — сигнал для будущей
    # вёрстки (Этап 3), что этому посту точно нужна картинка, а не просто
    # можно её добавить. Известно только для постов, дошедших до
    # LLM-классификации; для остальных — False по умолчанию.
    needs_photo: bool = False


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
) -> list[Post]:
    kept: list[Post] = []
    multi = [c for c in clusters if len(c) > 1]
    for c in clusters:
        if len(c) == 1:
            kept.append(c[0])

    for start in range(0, len(multi), BATCH_SIZE):
        batch = multi[start : start + BATCH_SIZE]
        decisions = classifier.resolve_paraphrase_batch(batch)
        by_cluster = {d.cluster_index: d for d in decisions}
        for local_idx, cluster in enumerate(batch):
            decision = by_cluster.get(local_idx)
            if decision is None:
                kept.extend(cluster)
                continue
            valid_indices = sorted(
                {i for i in decision.keep_indices if 0 <= i < len(cluster)}
            )
            if not valid_indices:
                kept.extend(cluster)
                continue
            kept_in_cluster = [cluster[i] for i in valid_indices]
            kept.extend(kept_in_cluster)
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
    return kept


def _classify_posts(
    config: Config, posts: list[Post], history: list[Post] | None = None
) -> list[PostOutcome]:
    """Прогоняет эвристики → копипаст-дедуп → LLM-классификацию → дедуп
    пересказов → (если передан history) дедуп против уже опубликованного за
    последние прогоны, над уже готовым списком постов (одного окна), и
    возвращает результат по каждому из них."""
    outcomes: dict[Key, PostOutcome] = {}

    candidates, filter_results = filter_posts(posts)
    for r in filter_results:
        if not r.is_news_candidate:
            outcomes[_key(r.post)] = PostOutcome(r.post, False, "heuristic", r.reason or "")

    classifier = GeminiClassifier(config)

    copypaste_clusters = find_copypaste_clusters(candidates)
    after_copypaste = _resolve_copypaste(classifier, copypaste_clusters, outcomes)

    decisions = classifier.classify(after_copypaste)
    news: list[Post] = []
    needs_photo_by_key: dict[Key, bool] = {}
    for p in after_copypaste:
        d = decisions[_key(p)]
        if d.is_news:
            news.append(p)
            needs_photo_by_key[_key(p)] = d.needs_photo
        else:
            outcomes[_key(p)] = PostOutcome(p, False, "classification", d.reason)

    embedder = GeminiEmbedder(config)
    paraphrase_clusters = find_paraphrase_clusters(news, embedder)
    final_news = _resolve_paraphrase(classifier, paraphrase_clusters, outcomes)

    if history:
        final_news, excluded_by_history = filter_against_history(
            final_news, history, classifier, embedder
        )
        for p, reason in excluded_by_history:
            outcomes[_key(p)] = PostOutcome(p, False, "history_dedup", reason)

    for p in final_news:
        outcomes[_key(p)] = PostOutcome(
            p, True, "final", "прошёл все этапы отбора",
            needs_photo=needs_photo_by_key.get(_key(p), False),
        )

    return [outcomes[_key(p)] for p in posts]


def run_pipeline_for_last_24h(config: Config) -> RunResult:
    """Полный прогон по кнопке "Собрать газету": забирает через Telethon
    посты за последние сутки от момента вызова, сохраняет их, прогоняет
    эвристики/дедуп/LLM-классификацию и сохраняет результат как новый
    прогон. Период сбора — всегда последние сутки от момента нажатия
    кнопки, без наверстывания пропущенных дней (см. AGENTS.md)."""
    run_started_at = datetime.now(timezone.utc)
    since = collection_since(run_started_at)

    conn = connect(config.db_path)
    collected = asyncio.run(collect_all(config, since))
    save_posts(conn, collected)

    # Читаем окно заново из БД, а не берём collected напрямую — так в окно
    # попадают и посты, уже сохранённые предыдущим прогоном (не создаёт
    # дублей благодаря уникальности (channel, message_id) в save_posts).
    posts = load_posts(conn, since=since, until=run_started_at)

    history = load_recent_included_posts(conn, HISTORY_RUNS_WINDOW)
    outcomes = _classify_posts(config, posts, history=history)
    run_id = save_run(conn, outcomes, run_started_at, since, run_started_at)
    return RunResult(run_id, since, run_started_at, outcomes)


def save_run(
    conn: sqlite3.Connection,
    outcomes: list[PostOutcome],
    run_started_at: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> int:
    """Сохраняет результат прогона в pipeline_runs/pipeline_outcomes и
    возвращает run_id — чтобы консоль могла позже открыть этот прогон без
    повторного обращения к LLM."""
    run_started_at = run_started_at or datetime.now(timezone.utc)
    run_id = create_run(conn, run_started_at, period_start, period_end)
    save_outcomes(
        conn,
        run_id,
        [
            (o.post.channel, o.post.message_id, o.included, o.stage, o.reason, o.needs_photo)
            for o in outcomes
        ],
    )
    return run_id


def load_run(conn: sqlite3.Connection, run_id: int) -> list[PostOutcome]:
    """Восстанавливает PostOutcome сохранённого прогона (без обращения к LLM)."""
    posts_by_key = {_key(p): p for p in load_posts(conn)}
    result = []
    for channel, message_id, included, stage, reason, needs_photo in load_outcomes(conn, run_id):
        post = posts_by_key.get((channel, message_id))
        if post is None:
            continue  # пост удалён из posts — не должно происходить, но не валим отчёт
        result.append(PostOutcome(post, included, stage, reason, needs_photo=needs_photo))
    return result
