"""Единый прогон полного пайплайна ("Собрать газету"): сбор постов за
последние сутки через Telethon, эвристики, дедупликация, LLM-классификация
— с детальным результатом по каждому посту: на каком этапе он отсеялся и
почему, или что дошёл до печати. Используется и scripts/run_pipeline.py
(запуск из терминала), и локальной консолью (кнопка "Собрать газету") —
чтобы не дублировать логику пайплайна в нескольких местах.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .classifier import BATCH_SIZE, GeminiClassifier
from .collector import collect_all, collection_since
from .config import Config
from .dedup import find_copypaste_clusters
from .filtering import filter_posts
from .history_dedup import HISTORY_RUNS_WINDOW, filter_against_history
from .layout import render_pages
from .paraphrase_dedup import GeminiEmbedder, find_paraphrase_clusters
from .personalization import (
    ReaderContext,
    final_importance,
    load_reader_context,
    summary_due,
)
from .storage import (
    Post,
    connect,
    create_run,
    load_outcomes,
    load_posts,
    load_recent_issue_posts,
    save_issue,
    save_outcomes,
    save_posts,
    save_profile_summary,
)

logger = logging.getLogger(__name__)

Key = tuple[str, int]


@dataclass(frozen=True)
class PostOutcome:
    post: Post
    included: bool
    stage: str  # "heuristic" | "copypaste" | "classification" | "paraphrase" | "story_merge" | "final"
    reason: str
    # Оценка LLM, насколько посту помогло бы именно фото (не видео) — три
    # степени (см. _CLASSIFICATION_INSTRUCTION в classifier.py): 1 — не
    # нужно, 2 — уместно, но смысл не теряется, 3 — без фото теряется
    # существенная часть смысла. Известна только для постов, дошедших до
    # LLM-классификации; для остальных — 1 (не нужно) по умолчанию.
    photo_relevance: int = 1
    # Оценка значимости от LLM (1-5, см. classifier.py) — Этап 3 использует
    # её, чтобы решить, что печатать в первую очередь, что сокращать, а что
    # выбросить, если всё не помещается в фиксированный номер полос.
    # Известна только для постов, дошедших до LLM-классификации; 0 —
    # неизвестно (пост отсеян раньше, либо прогон сделан до появления поля).
    importance: int = 0
    # Персональная оценка (1-5) из того же вызова классификации и итоговая —
    # смесь с generic по весу, растущему с числом отзывов (Этап 6 п.4,
    # personalization.final_importance). 0 — неизвестно: пустой профиль (тогда
    # personal не запрашивалась) либо прогон сделан до появления полей.
    personal_importance: int = 0
    final_importance: int = 0
    # Путь к скачанному фото поста (collector.py._download_photo), если оно
    # есть — при склейке (_merge_story_arcs) переносится на статью-якорь.
    # Пустая строка — фото нет, не скачалось, либо photo_relevance = 1 (не
    # нужно) и незачем было его искать.
    photo_path: str = ""
    # Текст статьи, если он СИНТЕЗИРОВАН пайплайном (сейчас — только
    # результат _merge_story_arcs, склейка развивающейся истории одного
    # канала в одну статью), а не совпадает с сырым текстом поста из БД.
    # Пустая строка — post.text это и есть настоящий текст поста. Нужно
    # отдельным полем, а не просто post.text, потому что save_run/load_run
    # проходят через БД: posts хранит только сырые исходники, а
    # pipeline_outcomes.merged_text — единственное место, где переживает
    # синтезированный текст между прогоном и повторным открытием в консоли.
    merged_text: str = ""


def effective_importance(outcome: PostOutcome) -> int:
    """ЕДИНСТВЕННОЕ место выбора оценки для отбора/сокращения/размера плитки и
    опросов: итоговая, а у старых прогонов (final_importance == 0) — generic."""
    return outcome.final_importance or outcome.importance


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
) -> list[list[Post]]:
    """Для каждого кластера пересказов возвращает подмножество постов,
    которые LLM сочла НЕ избыточными (несущими хотя бы один уникальный
    факт) — группировка по кластеру сохраняется в возврате (не сплющивается
    в общий список), чтобы _merge_story_arcs мог затем разобрать её по
    каналам и схлопнуть однокан­альную развивающуюся историю в одну статью."""
    kept_groups: list[list[Post]] = []
    multi = [c for c in clusters if len(c) > 1]
    for c in clusters:
        if len(c) == 1:
            kept_groups.append([c[0]])

    for start in range(0, len(multi), BATCH_SIZE):
        batch = multi[start : start + BATCH_SIZE]
        decisions = classifier.resolve_paraphrase_batch(batch)
        by_cluster = {d.cluster_index: d for d in decisions}
        for local_idx, cluster in enumerate(batch):
            decision = by_cluster.get(local_idx)
            if decision is None:
                kept_groups.append(cluster)
                continue
            valid_indices = sorted(
                {i for i in decision.keep_indices if 0 <= i < len(cluster)}
            )
            if not valid_indices:
                kept_groups.append(cluster)
                continue
            kept_in_cluster = [cluster[i] for i in valid_indices]
            kept_groups.append(kept_in_cluster)
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
    return kept_groups


def _merge_story_arcs(
    classifier: GeminiClassifier,
    kept_groups: list[list[Post]],
    outcomes: dict[Key, PostOutcome],
    importance_by_key: dict[Key, int],
    photo_relevance_by_key: dict[Key, int],
    photo_path_by_key: dict[Key, str],
    merged_anchor_keys: set[Key],
    personal_by_key: dict[Key, int] | None = None,
) -> list[Post]:
    """Схлопывает в одну статью посты ОДНОГО канала, которые _resolve_paraphrase
    оставил внутри одного кластера пересказов как несущие уникальные факты —
    типичный случай развивающейся истории за день (происшествие -> уточнение
    -> опровержение), которую иначе напечатали бы как N разрозненных статей.
    Кросс-канальные случаи (разные каналы сообщают разные факты одного
    события) не трогаем и печатаем как раньше отдельными постами — решение
    пользователя от 2026-09-27 сузило склейку до одного канала: развитие
    истории поперёк каналов — не то же самое, что уточнение в одном канале,
    и заслуживает отдельного решения, а не автоматической склейки.

    merged_anchor_keys заполняется ключами постов-якорей (см. ниже) — по
    ним вызывающий код помечает PostOutcome.merged_text, иначе синтезированный
    текст статьи потеряется при сохранении в БД (pipeline_outcomes не хранит
    текст, только channel/message_id — save_run/load_run восстанавливают его
    из сырой таблицы posts, где лежит только досклеечный оригинал)."""
    final: list[Post] = []
    to_merge: list[list[Post]] = []
    for group in kept_groups:
        by_channel: dict[str, list[Post]] = {}
        for p in group:
            by_channel.setdefault(p.channel, []).append(p)
        for channel_posts in by_channel.values():
            if len(channel_posts) > 1:
                to_merge.append(sorted(channel_posts, key=lambda p: p.posted_at))
            else:
                final.extend(channel_posts)

    for start in range(0, len(to_merge), BATCH_SIZE):
        batch = to_merge[start : start + BATCH_SIZE]
        items = classifier.merge_story_batch(batch)
        by_group = {item.group_index: item for item in items}
        for local_idx, group in enumerate(batch):
            item = by_group.get(local_idx)
            if item is None or not item.is_same_story:
                # LLM не ответила, либо явно решила, что это разные, не связанные
                # друг с другом новости одного канала (см. _STORY_MERGE_INSTRUCTION) —
                # не теряем посты и не выдумываем историю, печатаем как раньше отдельно
                final.extend(group)
                continue
            anchor = group[-1]  # самый свежий пост несёт актуальное состояние истории
            merged_text = f"**{item.headline.strip()}**\n\n{item.summary_text.strip()}"
            final.append(replace(anchor, text=merged_text))
            merged_anchor_keys.add(_key(anchor))
            importance_by_key[_key(anchor)] = max(
                importance_by_key.get(_key(p), 0) for p in group
            )
            if personal_by_key is not None:
                personal_by_key[_key(anchor)] = max(
                    personal_by_key.get(_key(p), 0) for p in group
                )
            photo_relevance_by_key[_key(anchor)] = max(
                photo_relevance_by_key.get(_key(p), 1) for p in group
            )
            # Фото берём у поста группы с наибольшей собственной оценкой
            # photo_relevance (а не обязательно у anchor — самый свежий пост
            # истории не всегда тот, у которого было фото), первое попавшееся
            # при равенстве оценок.
            by_relevance = sorted(group, key=lambda p: -photo_relevance_by_key.get(_key(p), 1))
            photo_path_by_key[_key(anchor)] = next(
                (photo_path_by_key[_key(p)] for p in by_relevance if photo_path_by_key.get(_key(p))),
                "",
            )
            for p in group[:-1]:
                outcomes[_key(p)] = PostOutcome(
                    p, False, "story_merge",
                    f"объединено с {anchor.channel}/{anchor.message_id} "
                    "в одну статью о развитии истории за день",
                )

    return final


def refresh_profile_summary(
    conn: sqlite3.Connection, classifier: GeminiClassifier
) -> bool:
    """Лениво обновляет краткий профиль читателя (Этап 6 п.4), если с прошлого
    обновления отправлено >= SUMMARY_EVERY_SURVEYS новых опросов. Вызывается из
    прогона ПЕРЕД классификацией: Gemini там и так используется, а из обработчиков
    опросов консоль Gemini не зовёт. Сбой не валит прогон — логируем и
    продолжаем со старым summary. Возвращает True, если summary обновлён."""
    try:
        if not summary_due(conn):
            return False
        summary = classifier.summarize_profile(load_reader_context(conn))
        if not summary:
            logger.warning("LLM вернула пустой краткий профиль — оставляю прежний")
            return False
        save_profile_summary(conn, summary)
        logger.info("краткий профиль читателя обновлён")
        return True
    except Exception:  # noqa: BLE001 — summary вторичен, прогон важнее
        logger.exception("не удалось обновить краткий профиль читателя — продолжаю со старым")
        return False


def _classify_posts(
    config: Config,
    posts: list[Post],
    history: list[Post] | None = None,
    reader: ReaderContext | None = None,
    classifier: GeminiClassifier | None = None,
    photo_path_by_key: dict[Key, str] | None = None,
) -> list[PostOutcome]:
    """Прогоняет эвристики → копипаст-дедуп → LLM-классификацию → дедуп
    пересказов → (если передан history) дедуп против уже опубликованного за
    последние собранные номера, над уже готовым списком постов (одного окна), и
    возвращает результат по каждому из них."""
    outcomes: dict[Key, PostOutcome] = {}
    photo_path_by_key = dict(photo_path_by_key or {})

    candidates, filter_results = filter_posts(posts)
    for r in filter_results:
        if not r.is_news_candidate:
            outcomes[_key(r.post)] = PostOutcome(r.post, False, "heuristic", r.reason or "")

    classifier = classifier or GeminiClassifier(config)
    weight = reader.weight if reader is not None else 0.0

    copypaste_clusters = find_copypaste_clusters(candidates)
    after_copypaste = _resolve_copypaste(classifier, copypaste_clusters, outcomes)

    decisions = classifier.classify(after_copypaste, reader)
    news: list[Post] = []
    photo_relevance_by_key: dict[Key, int] = {}
    importance_by_key: dict[Key, int] = {}
    personal_by_key: dict[Key, int] = {}
    for p in after_copypaste:
        d = decisions[_key(p)]
        if d.is_news:
            news.append(p)
            photo_relevance_by_key[_key(p)] = d.photo_relevance
            importance_by_key[_key(p)] = d.importance
            personal_by_key[_key(p)] = d.personal_importance if weight > 0 else 0
        else:
            outcomes[_key(p)] = PostOutcome(p, False, "classification", d.reason)

    embedder = GeminiEmbedder(config)
    paraphrase_clusters = find_paraphrase_clusters(news, embedder)
    kept_groups = _resolve_paraphrase(classifier, paraphrase_clusters, outcomes)
    merged_anchor_keys: set[Key] = set()
    final_news = _merge_story_arcs(
        classifier, kept_groups, outcomes, importance_by_key, photo_relevance_by_key,
        photo_path_by_key, merged_anchor_keys, personal_by_key,
    )

    if history:
        final_news, excluded_by_history = filter_against_history(
            final_news, history, classifier, embedder
        )
        for p, reason in excluded_by_history:
            outcomes[_key(p)] = PostOutcome(
                p, False, "history_dedup", reason,
                merged_text=p.text if _key(p) in merged_anchor_keys else "",
            )

    for p in final_news:
        generic = importance_by_key.get(_key(p), 0)
        personal = personal_by_key.get(_key(p), 0)
        outcomes[_key(p)] = PostOutcome(
            p, True, "final", "прошёл все этапы отбора",
            photo_relevance=photo_relevance_by_key.get(_key(p), 1),
            importance=generic,
            personal_importance=personal,
            final_importance=final_importance(generic, personal, weight),
            merged_text=p.text if _key(p) in merged_anchor_keys else "",
            photo_path=photo_path_by_key.get(_key(p), ""),
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
    collected, photo_path_by_key_raw = asyncio.run(collect_all(config, since))
    save_posts(conn, collected)
    photo_path_by_key = {key: str(path) for key, path in photo_path_by_key_raw.items()}

    # Читаем окно заново из БД, а не берём collected напрямую — так в окно
    # попадают и посты, уже сохранённые предыдущим прогоном (не создаёт
    # дублей благодаря уникальности (channel, message_id) в save_posts).
    posts = load_posts(conn, since=since, until=run_started_at)

    history = load_recent_issue_posts(conn, HISTORY_RUNS_WINDOW)

    classifier = GeminiClassifier(config)
    # Сначала summary (по накопленным отзывам), потом контекст читателя для
    # классификации — чтобы свежий summary сразу попал в промпт этого прогона.
    refresh_profile_summary(conn, classifier)
    reader = load_reader_context(conn)
    outcomes = _classify_posts(
        config, posts, history=history, reader=reader, classifier=classifier,
        photo_path_by_key=photo_path_by_key,
    )
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
            (
                o.post.channel, o.post.message_id, o.included, o.stage, o.reason,
                o.photo_relevance, o.importance, o.personal_importance,
                o.final_importance, o.merged_text, o.photo_path,
            )
            for o in outcomes
        ],
    )
    return run_id


def load_run(conn: sqlite3.Connection, run_id: int) -> list[PostOutcome]:
    """Восстанавливает PostOutcome сохранённого прогона (без обращения к LLM).

    Если у outcome есть merged_text (склейка развивающейся истории, см.
    _merge_story_arcs), восстановленный Post получает этот синтезированный
    текст вместо сырого текста из posts — иначе якорь склейки после
    перезагрузки прогона показывал бы досклеечный оригинал."""
    posts_by_key = {_key(p): p for p in load_posts(conn)}
    result = []
    for (
        channel, message_id, included, stage, reason, photo_relevance, importance,
        personal_importance, final_importance_, merged_text, photo_path,
    ) in load_outcomes(conn, run_id):
        post = posts_by_key.get((channel, message_id))
        if post is None:
            continue  # пост удалён из posts — не должно происходить, но не валим отчёт
        if merged_text:
            post = replace(post, text=merged_text)
        result.append(
            PostOutcome(
                post, included, stage, reason,
                photo_relevance=photo_relevance, importance=importance,
                personal_importance=personal_importance,
                final_importance=final_importance_,
                merged_text=merged_text, photo_path=photo_path,
            )
        )
    return result


# Печатная газета — фиксированный номер, не бесконечная лента (Этап 3, по
# итогам ревью пользователя: "четыре листа А4, повёрнутые горизонтально").
NEWSPAPER_MAX_PAGES = 4
# Не пытаемся сократить нейронкой то, что и так значимо ниже среднего —
# такое просто выбрасывается из номера, если не влезло.
IMPORTANCE_DROP_THRESHOLD = 2
SHORTEN_TARGET_SENTENCES = 3
# Короче этого сокращать почти нечего — смысла звать LLM нет, такой пост
# либо влезает как есть, либо выбрасывается наравне с неважными.
SHORTEN_MIN_CHARS = 400


@dataclass(frozen=True)
class NewspaperResult:
    pages: list[Path]
    dropped: list[Post]  # не поместились в фиксированный номер и не были сокращены
    shortened_count: int  # сколько новостей сократила нейронка ради места
    # Что реально ушло в последний успешный render_pages — с итоговым (после
    # сокращения нейронкой) текстом. Не то же самое, что included-исходы
    # прогона: часть из них выброшена (dropped). Именно этот список
    # сохраняется как состав номера (save_newspaper_issue) и служит историей
    # для дедупа следующих прогонов.
    published: list[Post]


def build_newspaper(
    config: Config,
    outcomes: list[PostOutcome],
    out_dir: Path,
    basename: str,
    run_date: datetime,
    max_pages: int = NEWSPAPER_MAX_PAGES,
) -> NewspaperResult:
    """Собирает печатный номер фиксированного объёма (не больше max_pages
    альбомных полос A4) из уже прошедших отбор новостей (Этапы 1-2).

    Раскладка — по итоговой оценке значимости (effective_importance: смесь
    generic importance из classifier.py и персональной оценки; у старых
    прогонов — generic): самое важное печатается в первую очередь
    и получает более крупную плитку (см. layout._build_bands). То, что не
    поместилось в отведённый объём:
    - если оно достаточно значимо (оценка > IMPORTANCE_DROP_THRESHOLD)
      и достаточно длинное, чтобы сокращение было осмысленным — сокращается
      нейронкой (GeminiClassifier.shorten) и полоса собирается заново;
    - иначе — выбрасывается из номера целиком.

    Каждая попытка — полный проход layout.render_pages с нуля (не только
    "довёрстка" последней полосы): дешевле по коду и, поскольку номер
    ограничен четырьмя полосами, достаточно быстро на практике."""
    included = [o for o in outcomes if o.included]
    if not included:
        return NewspaperResult([], [], 0, [])

    importance_by_key: dict[Key, int] = {_key(o.post): effective_importance(o) for o in included}
    photo_relevance_by_key: dict[Key, int] = {_key(o.post): o.photo_relevance for o in included}
    photo_path_by_key: dict[Key, str] = {
        _key(o.post): o.photo_path for o in included if o.photo_path
    }
    posts = [o.post for o in included]

    classifier: GeminiClassifier | None = None
    already_shortened: set[Key] = set()
    dropped: list[Post] = []
    shortened_count = 0

    while True:
        out_paths, leftover = render_pages(
            posts, out_dir, basename=basename, run_date=run_date,
            page_size="A4", landscape=True, max_pages=max_pages,
            importance_by_key=importance_by_key,
            photo_relevance_by_key=photo_relevance_by_key,
            photo_path_by_key=photo_path_by_key,
        )
        if not leftover:
            if dropped:
                logger.info(
                    "номер собран: %d стр., сокращено=%d, выброшено из-за нехватки места=%d",
                    len(out_paths), shortened_count, len(dropped),
                )
            return NewspaperResult(out_paths, dropped, shortened_count, posts)

        to_shorten_keys = {
            _key(p)
            for p in leftover
            if importance_by_key.get(_key(p), 0) > IMPORTANCE_DROP_THRESHOLD
            and _key(p) not in already_shortened
            and len(p.text) > SHORTEN_MIN_CHARS
        }

        if to_shorten_keys:
            to_shorten = [p for p in leftover if _key(p) in to_shorten_keys]
            if classifier is None:
                classifier = GeminiClassifier(config)
            shortened_texts = classifier.shorten(to_shorten, target_sentences=SHORTEN_TARGET_SENTENCES)
            already_shortened.update(to_shorten_keys)
            shortened_count += len(shortened_texts)
            logger.info("сокращено нейронкой %d новостей ради места в номере", len(shortened_texts))
            posts = [
                replace(p, text=shortened_texts[_key(p)]) if _key(p) in shortened_texts else p
                for p in posts
            ]
            continue

        # Сокращать больше нечего (недостаточно значимо или уже пробовали) —
        # оставшееся выбрасывается из номера.
        dropped.extend(leftover)
        leftover_keys = {_key(p) for p in leftover}
        posts = [p for p in posts if _key(p) not in leftover_keys]
        if not posts:
            return NewspaperResult(out_paths, dropped, shortened_count, [])


def save_newspaper_issue(conn: sqlite3.Connection, run_id: int, result: NewspaperResult) -> None:
    """Фиксирует состав только что собранного номера прогона run_id как
    "напечатанное" — историю для дедупа следующих прогонов. Вызывается после
    каждой сборки номера (сейчас scripts/render_preview.py, позже — печать,
    Этап 4); повторная сборка по тому же run_id заменяет состав, а не
    дописывает (см. storage.save_issue)."""
    save_issue(conn, run_id, result.published)
