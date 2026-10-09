"""Обратная связь читателя (Этап 6 п.2-3): подбор постов для опросов
"расставь по важности" и метрика совпадения порядка читателя с порядком модели.

Идея опроса: читатель расставляет 5 постов по порядку (1 — самое важное для
него, 5 — наименее). Сравнивая этот порядок с порядком оценок модели, видим,
где модель ошибается именно для него. Чтобы сравнение было информативным,
пять постов берутся с РАЗНЫМИ оценками модели (по возможности по одному на
каждый уровень 1..5): если у всех пяти оценка одинаковая, порядок читателя
не с чем сравнивать.

Слой отделён от консоли (scripts/console.py) и HTML (feedback_html.py): здесь
только выбор, метрика и работа с БД через storage, без вёрстки и без сети.
"""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass

from .pipeline import PostOutcome, effective_importance, load_run
from .storage import (
    create_survey,
    list_runs,
    list_surveys,
    load_issue_texts,
    load_post_texts,
)

# Сколько постов в одном опросе и раунде онбординга.
SURVEY_SIZE = 5
# Опрос после номера: сколько постов из номера и сколько — выброшенных из него
# (included=1, но не попавших в issue_posts: не хватило места в номере).
# Выброшенные нужны затем, что именно по ним видно, не зря ли модель их
# отодвинула: в номере все посты уже "прошли" оценку модели.
ISSUE_SURVEY_IN_ISSUE = 3
ISSUE_SURVEY_DROPPED = 2
# Онбординг: 3 раунда по 5 постов = 15 постов с реальными оценками.
ONBOARDING_ROUNDS = 3
# Из скольких самых свежих постов (с importance>0) выбирать раунды
# онбординга: свежие читателю понятнее (он помнит контекст), но пул должен
# быть шире 15, иначе "разнесение по уровням" будет выбирать не из чего.
ONBOARDING_POOL_LIMIT = 60


def model_score(outcome: PostOutcome) -> int:
    """ЕДИНСТВЕННОЕ место, откуда в опросах берётся "оценка модели" поста.

    Это итоговая оценка (Этап 6 п.4: смесь generic и персональной, у старых
    прогонов — generic, 0 — неизвестно) через pipeline.effective_importance.
    Подбор постов, метрика совпадения и фиксация model_score в feedback_items
    берут её отсюда и нигде больше outcome.importance напрямую не читают.
    """
    return effective_importance(outcome)


@dataclass(frozen=True)
class Candidate:
    """Пост-кандидат в опрос: исход прогона + оценка модели на сейчас."""
    outcome: PostOutcome
    in_issue: bool
    score: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.outcome.post.channel, self.outcome.post.message_id)


def _targets(count: int) -> list[int]:
    """Целевые уровни оценки для count постов, равномерно по шкале 5..1.
    Для 5 постов — ровно 5,4,3,2,1; для меньшего числа (мало постов в базе) —
    растянутые на всю шкалу, чтобы всё равно покрыть и верх, и низ."""
    if count <= 0:
        return []
    if count == 1:
        return [3]
    return [round(5 - i * 4 / (count - 1)) for i in range(count)]


def pick_spread(
    pools: list[tuple[list[Candidate], int]], rng: random.Random
) -> list[Candidate]:
    """Жадный подбор постов с разнесёнными по шкале оценками модели.

    pools — группы кандидатов с квотой: [(кандидаты, сколько взять), ...]
    (для опроса после номера — две группы: из номера (3) и выброшенные (2);
    для онбординга — одна на 5). Для целевых уровней 5,4,3,2,1 по очереди
    берётся ближайший по оценке ещё не выбранный пост из любой группы, у
    которой квота не исчерпана; при равной близости — случайный (rng), чтобы
    повторные опросы не показывали одни и те же посты. Жадность не оптимальна,
    но проста и предсказуема; оптимум тут не нужен — важно лишь не получить
    пять одинаковых оценок, когда в базе есть разные.

    Если кандидатов меньше квоты — берётся сколько есть. Результат в порядке
    выбора (по убыванию целевого уровня), НЕ в порядке показа — перемешивает
    вызывающий (shuffle_for_display).
    """
    available = [list(cands) for cands, _ in pools]
    quota = [q for _, q in pools]
    total = min(sum(quota), sum(len(a) for a in available))
    chosen: list[Candidate] = []
    for target in _targets(total):
        best: tuple[float, float, int, int] | None = None
        for gi, group in enumerate(available):
            if quota[gi] <= 0:
                continue
            for ci, cand in enumerate(group):
                # Третий компонент — случайный разрыв ничьих: сравнение
                # кортежей берёт его, только если расстояния равны.
                rank = (abs(cand.score - target), rng.random(), gi, ci)
                if best is None or rank < best:
                    best = rank
        if best is None:
            break  # кандидаты в группах с остатком квоты кончились
        _, _, gi, ci = best
        chosen.append(available[gi].pop(ci))
        quota[gi] -= 1
    return chosen


def shuffle_for_display(cands: list[Candidate], rng: random.Random) -> list[Candidate]:
    """Случайный порядок показа: не по оценке, чтобы не подталкивать читателя."""
    shuffled = list(cands)
    rng.shuffle(shuffled)
    return shuffled


def _issue_quotas(in_count: int, dropped_count: int) -> tuple[int, int]:
    """Квоты (из номера, выброшенные) для опроса. Целевая 3/2; если выброшенных
    меньше двух — недостающее добивается из номера (спецификация опроса); если
    и из номера меньше трёх — берём сколько есть, а свободные места отдаём
    остальным выброшенным (опрос лучше полный, чем из двух-трёх постов)."""
    dropped = min(ISSUE_SURVEY_DROPPED, dropped_count)
    in_issue = min(SURVEY_SIZE - dropped, in_count)
    dropped = min(dropped_count, SURVEY_SIZE - in_issue)
    return in_issue, dropped


def issue_candidates(
    conn: sqlite3.Connection, run_id: int
) -> tuple[list[Candidate], list[Candidate]]:
    """(посты номера, выброшенные из номера) прогона run_id. Выброшенные —
    outcomes с included=1, которых нет в issue_posts. Оценка модели — через
    model_score, не напрямую из importance."""
    printed = load_issue_texts(conn, run_id)
    in_issue: list[Candidate] = []
    dropped: list[Candidate] = []
    for o in load_run(conn, run_id):
        if not o.included:
            continue
        key = (o.post.channel, o.post.message_id)
        cand = Candidate(o, key in printed, model_score(o))
        (in_issue if cand.in_issue else dropped).append(cand)
    return in_issue, dropped


def create_issue_survey(
    conn: sqlite3.Connection, run_id: int, rng: random.Random | None = None
) -> int | None:
    """Опрос по номеру прогона run_id; создаётся лениво при первом открытии и
    дальше тот же (элементы зафиксированы в БД — повторное открытие не
    пересэмплирует и не плодит опросы). None — постов слишком мало (<2), чтобы
    было что расставлять."""
    existing = list_surveys(conn, "issue", run_id=run_id)
    if existing:
        return existing[0].survey_id
    rng = rng or random.Random()
    in_issue, dropped = issue_candidates(conn, run_id)
    q_in, q_out = _issue_quotas(len(in_issue), len(dropped))
    picked = pick_spread([(in_issue, q_in), (dropped, q_out)], rng)
    if len(picked) < 2:
        return None
    return _create(conn, "issue", run_id, shuffle_for_display(picked, rng))


def _create(
    conn: sqlite3.Connection, kind: str, run_id: int | None, ordered: list[Candidate]
) -> int:
    return create_survey(
        conn, kind, run_id,
        [(c.key[0], c.key[1], c.in_issue, c.score, pos) for pos, c in enumerate(ordered, start=1)],
    )


def onboarding_pool(conn: sqlite3.Connection) -> list[Candidate]:
    """Пул для онбординга: посты с оценкой модели > 0 из всех прогонов, без
    повторов одного поста (берётся из самого свежего прогона), самые свежие по
    времени публикации — первыми, не больше ONBOARDING_POOL_LIMIT."""
    seen: set[tuple[str, int]] = set()
    pool: list[Candidate] = []
    for run in list_runs(conn):  # новые прогоны первыми
        for o in load_run(conn, run.run_id):
            cand = Candidate(o, False, model_score(o))
            if cand.score <= 0 or cand.key in seen:
                continue
            seen.add(cand.key)
            pool.append(cand)
    pool.sort(key=lambda c: c.outcome.post.posted_at, reverse=True)
    return pool[:ONBOARDING_POOL_LIMIT]


def create_onboarding_surveys(
    conn: sqlite3.Connection, rng: random.Random | None = None
) -> list[int]:
    """Раунды ранжирования онбординга (до 3 по 5 постов, без повторов между
    раундами), каждый — отдельный опрос. Идемпотентно: если онбординговые
    опросы уже созданы, возвращает их (читатель не получает другие посты при
    повторном открытии формы). Пустая база / слишком мало постов — меньше
    раундов (в пределе пустой список); раунд из <2 постов не создаётся."""
    existing = list_surveys(conn, "onboarding")
    if existing:
        return [s.survey_id for s in existing]
    rng = rng or random.Random()
    pool = onboarding_pool(conn)
    ids: list[int] = []
    for _ in range(ONBOARDING_ROUNDS):
        picked = pick_spread([(pool, SURVEY_SIZE)], rng)
        if len(picked) < 2:
            break
        picked_keys = {c.key for c in picked}
        pool = [c for c in pool if c.key not in picked_keys]  # без повторов между раундами
        ids.append(_create(conn, "onboarding", None, shuffle_for_display(picked, rng)))
    return ids


def validate_ranks(ranks: dict[tuple[str, int], int | None]) -> str | None:
    """Проверка ответа: у каждого поста место, места — ровно 1..n без
    повторов (читатель РАССТАВЛЯЕТ посты, а не оценивает каждый отдельно:
    две единицы не несут информации о порядке). Возвращает текст ошибки
    для читателя или None, если всё в порядке."""
    n = len(ranks)
    values = list(ranks.values())
    if any(v is None for v in values):
        return "Расставьте места для всех постов."
    if sorted(values) != list(range(1, n + 1)):
        return f"Места должны быть уникальными — от 1 до {n}, каждое по одному разу."
    return None


def agreement(user_ranks: list[int], scores: list[int]) -> float | None:
    """Совпадение порядка читателя с порядком модели — Kendall tau-b, -1..1
    (1 — порядок совпал полностью, -1 — прямо противоположный).

    user_ranks[i] — место читателя (1 — самое важное), scores[i] — оценка
    модели (больше — важнее) для одного и того же поста.

    Почему Kendall, а не Spearman: у нас 5 элементов и часть оценок модели
    совпадает (шкала 1..5 на пяти постах — коллизии неизбежны). Kendall
    считает пары постов и одинаково честно описывается словами ("из 10 пар
    читатель и модель согласны в стольких-то"), а tau-b корректно
    обрабатывает ничьи: пары с равной оценкой модели не считаются ни
    согласием, ни разногласием, но уменьшают знаменатель, поэтому при ничьих
    даже идеальное совпадение даёт меньше 1 — это осознанно: ничья в оценках
    честно означает "модель различает посты хуже, чем читатель".
    У читателя ничьих нет (места уникальны). None — метрика не определена:
    меньше двух постов или у модели все оценки одинаковы (сравнивать нечего).
    """
    n = len(user_ranks)
    if n < 2 or n != len(scores):
        return None
    concordant = discordant = model_ties = 0
    for i in range(n):
        for j in range(i + 1, n):
            ds = scores[i] - scores[j]
            if ds == 0:
                model_ties += 1
                continue
            # Меньшее место читателя = важнее, поэтому знак разности мест
            # переворачиваем: "i важнее j" — это rank_i < rank_j.
            du = user_ranks[j] - user_ranks[i]
            if du == 0:
                continue  # ничьих у читателя быть не должно; защита от деления/знака
            if (ds > 0) == (du > 0):
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) // 2
    denominator = math.sqrt((pairs - model_ties) * pairs)
    if denominator == 0:
        return None
    return (concordant - discordant) / denominator


def model_ranks(scores: list[int]) -> list[int]:
    """Места по оценке модели (1 — самая высокая), при равных оценках — одно
    и то же место (стандартная "соревновательная" нумерация 1,2,2,4)."""
    return [1 + sum(1 for other in scores if other > s) for s in scores]


def load_item_texts(
    conn: sqlite3.Connection, survey_kind: str, run_id: int | None, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    """Тексты постов опроса для показа читателю. Для постов номера — тот
    текст, что напечатан (issue_posts, после сокращения нейронкой): читатель
    оценивает то, что видел на бумаге, а не исходник, которого не видел.
    Для остальных (выброшенные, онбординг) — исходный из posts."""
    printed = load_issue_texts(conn, run_id) if survey_kind == "issue" and run_id else {}
    originals = load_post_texts(conn, [k for k in keys if k not in printed])
    return {k: printed.get(k, originals.get(k, "")) for k in keys}


# Окно для показателя "учится ли газета": среднее совпадение последних N
# опросов против предыдущих N. Пять — как размер опроса: достаточно, чтобы
# сгладить шум одного ранжирования, и набирается за пару недель.
LEARNING_WINDOW = 5


@dataclass(frozen=True)
class LearningStats:
    """Сводка для главной консоли: сколько опросов отправлено, среднее
    совпадение (Kendall tau-b) последних LEARNING_WINDOW опросов и предыдущих
    LEARNING_WINDOW (None — таких опросов нет или совпадение не определено)."""
    submitted: int
    recent_avg: float | None
    previous_avg: float | None


def learning_stats(conn: sqlite3.Connection) -> LearningStats:
    """Считает LearningStats по всем отправленным опросам (онбординг и после
    номера), по порядку отправки. Опросы без agreement (метрика не определена:
    у модели все оценки равны) считаются в submitted, но не в средних."""
    surveys = [
        s for kind in ("onboarding", "issue") for s in list_surveys(conn, kind) if s.submitted
    ]
    surveys.sort(key=lambda s: (s.submitted_at, s.survey_id))
    values = [s.agreement for s in surveys if s.agreement is not None]
    recent = values[-LEARNING_WINDOW:]
    previous = values[-2 * LEARNING_WINDOW:-LEARNING_WINDOW]

    def avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    return LearningStats(len(surveys), avg(recent), avg(previous))
