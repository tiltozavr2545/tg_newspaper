"""SQLite-хранилище сырых постов и результатов прогонов."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    text TEXT NOT NULL,
    has_media INTEGER NOT NULL,
    media_type TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

-- Результат одного прогона (кнопка "Собрать газету") — не сырые посты, а
-- решение по каждому посту окна: пошёл в газету или отсеян, на каком этапе,
-- почему. Заполняется по итогам прогона (pipeline.run_pipeline_for_last_24h), чтобы
-- консоль могла листать историю прогонов без повторных вызовов LLM.
--
-- period_start/period_end — границы окна сбора этого прогона (последние
-- LOOKBACK_HOURS часов от нажатия кнопки). Хранятся потому, что прогон
-- запускается вручную и в произвольный момент: без записанного окна потом
-- не понять, за какой период отбирались посты. NULL — прогон, сделанный до
-- перехода на запуск по кнопке, когда пайплайн гонялся по всей базе.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_outcomes (
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    included INTEGER NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    -- Оценка LLM, насколько посту помогло бы именно фото (не видео),
    -- три степени (см. _CLASSIFICATION_INSTRUCTION в classifier.py):
    -- 1 — не нужно, 2 — уместно, но смысл текста не теряется, 3 — без
    -- фото теряется существенная часть смысла. 1 по умолчанию — как для
    -- постов, не дошедших до LLM-классификации, так и для прогонов,
    -- сделанных до появления этого поля.
    photo_relevance INTEGER NOT NULL DEFAULT 1,
    -- Оценка значимости от LLM-классификатора (1-5, см. classifier.py) —
    -- Этап 3: печатать в первую очередь важное, сокращать/выбрасывать
    -- проходное, когда всё не помещается в фиксированный номер полос. 0 —
    -- не определено (пост не дошёл до LLM-классификации либо прогон сделан
    -- до появления этого поля).
    importance INTEGER NOT NULL DEFAULT 0,
    -- Персональная оценка (Этап 6 п.4): насколько пост интересен именно
    -- читателю (1-5), из того же вызова классификации. 0 — не запрашивалась
    -- (пустой профиль, пост не дошёл до классификации, старый прогон).
    personal_importance INTEGER NOT NULL DEFAULT 0,
    -- Итоговая оценка = смесь importance и personal_importance с весом,
    -- зависящим от числа отзывов (personalization.final_importance). По ней
    -- отбирается номер и строятся опросы. 0 — старый прогон: тогда читатели
    -- берут generic importance (pipeline.effective_importance).
    final_importance INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, channel, message_id)
);

-- Состав РЕАЛЬНО собранного номера: какие посты и с каким текстом ушли в
-- последний успешный рендер полос (pipeline.build_newspaper). Это не то же
-- самое, что pipeline_outcomes.included=1: included значит лишь "прошёл
-- отбор Этапа 2", а в номер фиксированного объёма часть таких постов не
-- влезает и выбрасывается, часть сокращается нейронкой. Дедупликация против
-- уже опубликованного (history_dedup) должна опираться именно на напечатанное,
-- иначе новость, не влезшая в номер, на следующий день блокировалась бы как
-- "уже была в газете".
--
-- text — напечатанный текст (после сокращения нейронкой, если сокращали), а
-- не исходный из posts: именно его читатель видел на бумаге, с ним и
-- сравниваем новых кандидатов.
--
-- Номер по одному run_id можно собрать несколько раз (render_preview гоняют
-- повторно), поэтому повторная сборка ЗАМЕНЯЕТ состав номера этого прогона
-- (storage.save_issue), а не дописывает. Прогон без строк в этой таблице —
-- номер по нему не собирали, в историю дедупа он не входит. Номер, в который
-- не вошло ни одного поста, строк не оставляет и "собранным" не считается.
CREATE TABLE IF NOT EXISTS issue_posts (
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    assembled_at TEXT NOT NULL,
    PRIMARY KEY (run_id, channel, message_id)
);
-- Профиль читателя (Этап 6 п.1). Читатель пока один, поэтому таблица хранит
-- ровно одну строку с id=1 (CHECK не даст завести вторую) — reader_id
-- сознательно не вводим, multi-user вне задач проекта; но и под конкретного
-- человека в коде ничего не зашито: интересы живут только здесь.
--
-- interests/disinterests — свободный текст из онбординга ("что интересно" /
-- "что неинтересно"), как есть: интерпретирует его LLM на этапе персональной
-- оценки (Этап 6 п.4), структурировать заранее нечем.
-- onboarded_at — когда онбординг пройден ИЛИ пропущен кнопкой "Пропустить"
-- (тогда тексты пустые): NULL значит "ещё не спрашивали" — консоль не даёт
-- запустить прогон, пока он NULL.
-- summary/summary_updated_at — краткий профиль, который LLM будет сводить из
-- текстов и накопленных отзывов (часть п.4). Сейчас только хранятся: пишет
-- их позже персональная оценка, читать их до этого некому.
CREATE TABLE IF NOT EXISTS reader_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    interests TEXT NOT NULL DEFAULT '',
    disinterests TEXT NOT NULL DEFAULT '',
    onboarded_at TEXT,
    summary TEXT,
    summary_updated_at TEXT
);

-- Опрос "расставь посты по важности" (Этап 6 п.2-3). Два вида:
--   'onboarding' — раунд ранжирования из онбординга; на одну анкету их три
--                  (три строки, run_id NULL), каждый раунд — отдельный опрос
--                  со своей метрикой совпадения;
--   'issue'      — опрос после собранного номера, run_id — прогон, чей номер
--                  оценивают (по одному на прогон, см. feedback.py).
-- Элементы опроса фиксируются в БД при создании (feedback_items), поэтому
-- повторное открытие страницы показывает те же посты в том же порядке, а не
-- пересэмплирует их.
-- submitted_at NULL — опрос создан, но не отправлен (читатель ушёл со
-- страницы): консоль считает его неотвеченным и покажет снова.
-- agreement — метрика совпадения порядка читателя с порядком модели (Kendall
-- tau-b, -1..1, см. feedback.agreement). NULL и у неотправленного опроса, и
-- когда метрика не определена (у модели у всех постов одинаковая оценка).
CREATE TABLE IF NOT EXISTS feedback_surveys (
    survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('onboarding', 'issue')),
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    agreement REAL
);

-- Посты опроса и ответ читателя на каждый.
-- in_issue — был ли пост в напечатанном номере (для онбординга всегда 0: там
-- номера нет). Нужен и для текста (в номере показываем напечатанный вариант,
-- не исходный), и как признак для части B: "выброшенный, но интересный" —
-- сильнее всего сигнал, что оценка модели ошибается.
-- model_score — оценка модели на МОМЕНТ опроса (feedback.model_score). Фиксируем
-- копией, а не ссылкой на pipeline_outcomes.importance: персональная оценка
-- позже изменится и пересчитается, а метрика и примеры для LLM должны
-- описывать то, что читатель реально сравнивал.
-- shown_position — порядок, в котором посты показаны на экране (случайный,
-- не по оценке: не подталкиваем читателя). user_rank — место, которое он
-- дал (1 — самое важное для него), NULL до отправки; внутри опроса места
-- уникальны (проверяется при приёме формы, не схемой).
CREATE TABLE IF NOT EXISTS feedback_items (
    survey_id INTEGER NOT NULL REFERENCES feedback_surveys(survey_id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    in_issue INTEGER NOT NULL,
    model_score INTEGER NOT NULL,
    shown_position INTEGER NOT NULL,
    user_rank INTEGER,
    PRIMARY KEY (survey_id, channel, message_id)
);
"""


@dataclass(frozen=True)
class Post:
    channel: str
    message_id: int
    posted_at: datetime
    text: str
    has_media: bool
    media_type: str | None


@dataclass(frozen=True)
class Run:
    run_id: int
    started_at: datetime
    # Окно сбора прогона; None у прогонов, сделанных до перехода на запуск по кнопке.
    period_start: datetime | None
    period_end: datetime | None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Досыпает колонки, появившиеся после первых прогонов, в уже созданную БД
    (CREATE TABLE IF NOT EXISTS существующую таблицу не меняет)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)")}
    for column in ("period_start", "period_end"):
        if column not in columns:
            conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {column} TEXT")

    outcome_columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_outcomes)")}
    if "importance" not in outcome_columns:
        conn.execute("ALTER TABLE pipeline_outcomes ADD COLUMN importance INTEGER NOT NULL DEFAULT 0")

    # Этап 6 п.4: у старых прогонов 0 — "неизвестно" (не выдумываем задним числом).
    for column in ("personal_importance", "final_importance"):
        if column not in outcome_columns:
            conn.execute(
                f"ALTER TABLE pipeline_outcomes ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )

    if "photo_relevance" not in outcome_columns:
        conn.execute(
            "ALTER TABLE pipeline_outcomes ADD COLUMN photo_relevance INTEGER NOT NULL DEFAULT 1"
        )
        # Более старая колонка needs_photo (булева) — грубо переносим её в
        # новую трёхстепенную шкалу, чтобы не терять уже собранные данные:
        # needs_photo=1 раньше означало "без фото теряется смысл" (степень 3).
        if "needs_photo" in outcome_columns:
            conn.execute(
                "UPDATE pipeline_outcomes SET photo_relevance = 3 WHERE needs_photo = 1"
            )

    conn.commit()


def load_posts(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Post]:
    """Посты из окна (since, until]. Без границ — вся база (для отладки)."""
    where, params = [], []
    if since is not None:
        where.append("posted_at > ?")
        params.append(since.astimezone(timezone.utc).isoformat())
    if until is not None:
        where.append("posted_at <= ?")
        params.append(until.astimezone(timezone.utc).isoformat())
    sql = "SELECT channel, message_id, posted_at, text, has_media, media_type FROM posts"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows = conn.execute(sql, params).fetchall()
    return [
        Post(
            channel=channel,
            message_id=message_id,
            posted_at=datetime.fromisoformat(posted_at),
            text=text,
            has_media=bool(has_media),
            media_type=media_type,
        )
        for channel, message_id, posted_at, text, has_media, media_type in rows
    ]


def create_run(
    conn: sqlite3.Connection,
    run_started_at: datetime,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> int:
    def iso(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    cur = conn.execute(
        "INSERT INTO pipeline_runs (run_started_at, period_start, period_end) VALUES (?, ?, ?)",
        (iso(run_started_at), iso(period_start), iso(period_end)),
    )
    conn.commit()
    return cur.lastrowid


def save_outcomes(
    conn: sqlite3.Connection,
    run_id: int,
    rows: list[tuple[str, int, bool, str, str, int, int, int, int]],
) -> None:
    """rows: (channel, message_id, included, stage, reason, photo_relevance,
    importance, personal_importance, final_importance)."""
    conn.executemany(
        "INSERT INTO pipeline_outcomes "
        "(run_id, channel, message_id, included, stage, reason, photo_relevance, importance, "
        "personal_importance, final_importance) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, ch, mid, int(inc), stage, reason, photo_relevance, importance, personal, final)
            for ch, mid, inc, stage, reason, photo_relevance, importance, personal, final in rows
        ],
    )
    conn.commit()


def list_runs(conn: sqlite3.Connection) -> list[Run]:
    rows = conn.execute(
        "SELECT run_id, run_started_at, period_start, period_end "
        "FROM pipeline_runs ORDER BY run_id DESC"
    ).fetchall()

    def parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    return [
        Run(run_id, datetime.fromisoformat(started_at), parse(period_start), parse(period_end))
        for run_id, started_at, period_start, period_end in rows
    ]


def load_outcomes(
    conn: sqlite3.Connection, run_id: int
) -> list[tuple[str, int, bool, str, str, int, int, int, int]]:
    rows = conn.execute(
        "SELECT channel, message_id, included, stage, reason, photo_relevance, importance, "
        "personal_importance, final_importance "
        "FROM pipeline_outcomes WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [
        (ch, mid, bool(inc), stage, reason, photo_relevance, importance, personal, final)
        for ch, mid, inc, stage, reason, photo_relevance, importance, personal, final in rows
    ]


def save_issue(conn: sqlite3.Connection, run_id: int, posts: list[Post]) -> None:
    """Записывает состав собранного номера прогона run_id (текст — тот, что
    реально напечатан, см. issue_posts в SCHEMA). Повторная сборка по тому же
    run_id заменяет прежний состав целиком: старые строки удаляются и вставляются
    новые в одной транзакции, чтобы пост, выпавший при пересборке, не остался в
    истории дедупа как "напечатанный". Пустой posts просто стирает состав."""
    assembled_at = datetime.now(timezone.utc).isoformat()
    with conn:  # одна транзакция: либо старый состав, либо новый, не смесь
        conn.execute("DELETE FROM issue_posts WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO issue_posts (run_id, channel, message_id, text, assembled_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, p.channel, p.message_id, p.text, assembled_at) for p in posts],
        )


def load_recent_issue_posts(conn: sqlite3.Connection, limit_issues: int) -> list[Post]:
    """Посты из последних limit_issues СОБРАННЫХ номеров (прогонов, у которых
    есть запись в issue_posts) с напечатанным текстом — история для
    дедупликации против уже опубликованного (Этап 2 п.4). Считаем номера, а не
    прогоны и не дни: проект запускается по кнопке, и прогоны, по которым номер
    так и не собирали, не должны вытеснять реально напечатанное из окна. "Последние"
    — по run_id (порядок прогонов), повторная пересборка старого номера его
    место в окне не меняет. Метаданные поста (posted_at, медиа) берутся из
    posts, текст — из issue_posts. Если один пост попал в несколько номеров,
    возвращается один раз — с текстом из самого свежего."""
    rows = conn.execute(
        """
        SELECT i.channel, i.message_id, p.posted_at, i.text, p.has_media, p.media_type
        FROM issue_posts i
        JOIN posts p ON p.channel = i.channel AND p.message_id = i.message_id
        WHERE i.run_id IN (
            SELECT DISTINCT run_id FROM issue_posts ORDER BY run_id DESC LIMIT ?
        )
        ORDER BY i.run_id DESC
        """,
        (limit_issues,),
    ).fetchall()
    seen: set[tuple[str, int]] = set()
    result: list[Post] = []
    for channel, message_id, posted_at, text, has_media, media_type in rows:
        if (channel, message_id) in seen:
            continue
        seen.add((channel, message_id))
        result.append(
            Post(
                channel=channel,
                message_id=message_id,
                posted_at=datetime.fromisoformat(posted_at),
                text=text,
                has_media=bool(has_media),
                media_type=media_type,
            )
        )
    return result


def save_posts(conn: sqlite3.Connection, posts: list[Post]) -> None:
    collected_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO posts "
        "(channel, message_id, posted_at, text, has_media, media_type, collected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(channel, message_id) DO NOTHING",
        [
            (
                post.channel,
                post.message_id,
                post.posted_at.astimezone(timezone.utc).isoformat(),
                post.text,
                int(post.has_media),
                post.media_type,
                collected_at,
            )
            for post in posts
        ],
    )
    conn.commit()


# --- Этап 6: профиль читателя и отзывы -------------------------------------


@dataclass(frozen=True)
class ReaderProfile:
    interests: str = ""
    disinterests: str = ""
    # None — онбординг ещё не пройден и не пропущен.
    onboarded_at: datetime | None = None
    # Заполняются pipeline.refresh_profile_summary (Этап 6 п.4).
    summary: str | None = None
    summary_updated_at: datetime | None = None

    @property
    def onboarded(self) -> bool:
        return self.onboarded_at is not None


@dataclass(frozen=True)
class Survey:
    survey_id: int
    kind: str  # "onboarding" | "issue"
    run_id: int | None
    created_at: datetime
    submitted_at: datetime | None
    agreement: float | None

    @property
    def submitted(self) -> bool:
        return self.submitted_at is not None


@dataclass(frozen=True)
class SurveyItem:
    channel: str
    message_id: int
    in_issue: bool
    model_score: int
    shown_position: int
    user_rank: int | None


@dataclass(frozen=True)
class FeedbackExample:
    """Один отправленный ответ читателя — пример для LLM в части B: что за
    пост (исходный текст из posts), как его оценила модель и какое место дал
    читатель среди остальных постов того же опроса (rank_of — их число)."""
    survey_id: int
    kind: str
    run_id: int | None
    channel: str
    message_id: int
    text: str
    in_issue: bool
    model_score: int
    user_rank: int
    rank_of: int


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_profile(conn: sqlite3.Connection) -> ReaderProfile:
    """Профиль читателя; если строки ещё нет — пустой, не онбордингованный."""
    row = conn.execute(
        "SELECT interests, disinterests, onboarded_at, summary, summary_updated_at "
        "FROM reader_profile WHERE id = 1"
    ).fetchone()
    if row is None:
        return ReaderProfile()
    interests, disinterests, onboarded_at, summary, summary_updated_at = row
    return ReaderProfile(
        interests, disinterests, _parse_dt(onboarded_at), summary, _parse_dt(summary_updated_at)
    )


def _write_profile(
    conn: sqlite3.Connection, interests: str, disinterests: str, onboarded: bool
) -> None:
    # Без commit — вызывающий решает, в какой транзакции это происходит.
    # summary НЕ трогаем: его пишет только персональная оценка, а повторное
    # сохранение текстов не должно стирать накопленный краткий профиль.
    conn.execute("INSERT OR IGNORE INTO reader_profile (id) VALUES (1)")
    conn.execute(
        "UPDATE reader_profile SET interests = ?, disinterests = ?, "
        "onboarded_at = CASE WHEN ? THEN COALESCE(onboarded_at, ?) ELSE onboarded_at END "
        "WHERE id = 1",
        (interests, disinterests, int(onboarded), _iso_now()),
    )


def save_profile(
    conn: sqlite3.Connection, interests: str, disinterests: str, onboarded: bool = True
) -> None:
    """Сохраняет тексты профиля. onboarded=True ставит onboarded_at (если ещё
    не стоит — первая отметка не перезаписывается); пустые тексты + onboarded —
    это и есть "Пропустить"."""
    with conn:
        _write_profile(conn, interests, disinterests, onboarded)


def save_profile_summary(conn: sqlite3.Connection, summary: str) -> None:
    """Краткий профиль от LLM (Этап 6 п.4). Здесь только запись — вызывает
    pipeline.refresh_profile_summary перед классификацией."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO reader_profile (id) VALUES (1)")
        conn.execute(
            "UPDATE reader_profile SET summary = ?, summary_updated_at = ? WHERE id = 1",
            (summary, _iso_now()),
        )


def create_survey(
    conn: sqlite3.Connection,
    kind: str,
    run_id: int | None,
    items: list[tuple[str, int, bool, int, int]],
) -> int:
    """Создаёт опрос и фиксирует его элементы. items: (channel, message_id,
    in_issue, model_score, shown_position). Возвращает survey_id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback_surveys (kind, run_id, created_at) VALUES (?, ?, ?)",
            (kind, run_id, _iso_now()),
        )
        survey_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO feedback_items "
            "(survey_id, channel, message_id, in_issue, model_score, shown_position) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(survey_id, ch, mid, int(inc), score, pos) for ch, mid, inc, score, pos in items],
        )
    return survey_id


_SURVEY_COLUMNS = "survey_id, kind, run_id, created_at, submitted_at, agreement"


def _survey_from_row(row: tuple) -> Survey:
    survey_id, kind, run_id, created_at, submitted_at, agreement = row
    return Survey(
        survey_id, kind, run_id, datetime.fromisoformat(created_at),
        _parse_dt(submitted_at), agreement,
    )


def load_survey(conn: sqlite3.Connection, survey_id: int) -> Survey | None:
    row = conn.execute(
        f"SELECT {_SURVEY_COLUMNS} FROM feedback_surveys WHERE survey_id = ?", (survey_id,)
    ).fetchone()
    return _survey_from_row(row) if row else None


def list_surveys(
    conn: sqlite3.Connection,
    kind: str,
    run_id: int | None = None,
    only_pending: bool = False,
) -> list[Survey]:
    """Опросы вида kind (для 'issue' — только указанного прогона, если run_id
    задан) по возрастанию survey_id. only_pending — только неотправленные: так
    консоль находит опрос, который ещё ждёт ответа."""
    sql = f"SELECT {_SURVEY_COLUMNS} FROM feedback_surveys WHERE kind = ?"
    params: list = [kind]
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    if only_pending:
        sql += " AND submitted_at IS NULL"
    sql += " ORDER BY survey_id"
    return [_survey_from_row(r) for r in conn.execute(sql, params)]


def load_survey_items(conn: sqlite3.Connection, survey_id: int) -> list[SurveyItem]:
    """Элементы опроса в порядке показа (shown_position)."""
    rows = conn.execute(
        "SELECT channel, message_id, in_issue, model_score, shown_position, user_rank "
        "FROM feedback_items WHERE survey_id = ? ORDER BY shown_position",
        (survey_id,),
    ).fetchall()
    return [SurveyItem(ch, mid, bool(inc), score, pos, rank) for ch, mid, inc, score, pos, rank in rows]


def _write_answers(
    conn: sqlite3.Connection,
    survey_id: int,
    ranks: dict[tuple[str, int], int],
    agreement: float | None,
) -> None:
    # Без commit — см. _write_profile.
    conn.executemany(
        "UPDATE feedback_items SET user_rank = ? "
        "WHERE survey_id = ? AND channel = ? AND message_id = ?",
        [(rank, survey_id, ch, mid) for (ch, mid), rank in ranks.items()],
    )
    conn.execute(
        "UPDATE feedback_surveys SET submitted_at = ?, agreement = ? WHERE survey_id = ?",
        (_iso_now(), agreement, survey_id),
    )


def save_survey_answers(
    conn: sqlite3.Connection,
    survey_id: int,
    ranks: dict[tuple[str, int], int],
    agreement: float | None,
) -> None:
    """Записывает места читателя ((channel, message_id) -> 1..n) и метрику
    совпадения, помечает опрос отправленным. Проверка уникальности мест — на
    стороне приёма формы (feedback.validate_ranks), здесь только запись."""
    with conn:
        _write_answers(conn, survey_id, ranks, agreement)


def complete_onboarding(
    conn: sqlite3.Connection,
    interests: str,
    disinterests: str,
    answers: list[tuple[int, dict[tuple[str, int], int], float | None]],
) -> None:
    """Завершает онбординг одной транзакцией: тексты профиля + ответы на все
    раунды (survey_id, ranks, agreement). Одной — чтобы обрыв посередине не
    оставил "онбординг пройден", но без ответов на раунды (или наоборот)."""
    with conn:
        _write_profile(conn, interests, disinterests, True)
        for survey_id, ranks, agreement in answers:
            _write_answers(conn, survey_id, ranks, agreement)


def latest_issue_run_id(conn: sqlite3.Connection) -> int | None:
    """Последний прогон, по которому записан собранный номер (issue_posts)."""
    row = conn.execute("SELECT MAX(run_id) FROM issue_posts").fetchone()
    return row[0]


def load_issue_texts(conn: sqlite3.Connection, run_id: int) -> dict[tuple[str, int], str]:
    """Напечатанные тексты номера прогона run_id: (channel, message_id) -> text."""
    rows = conn.execute(
        "SELECT channel, message_id, text FROM issue_posts WHERE run_id = ?", (run_id,)
    ).fetchall()
    return {(ch, mid): text for ch, mid, text in rows}


def load_post_texts(
    conn: sqlite3.Connection, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], str]:
    """Исходные тексты постов из posts по ключам (отсутствующие пропускаются)."""
    result: dict[tuple[str, int], str] = {}
    for ch, mid in keys:
        row = conn.execute(
            "SELECT text FROM posts WHERE channel = ? AND message_id = ?", (ch, mid)
        ).fetchone()
        if row:
            result[(ch, mid)] = row[0]
    return result


def load_submitted_feedback(conn: sqlite3.Connection) -> list[FeedbackExample]:
    """Все отправленные ответы читателя — примеры для персональной оценки
    (часть B). Свежие опросы первыми; внутри опроса — по месту читателя."""
    rows = conn.execute(
        """
        SELECT s.survey_id, s.kind, s.run_id, i.channel, i.message_id,
               COALESCE(p.text, ''), i.in_issue, i.model_score, i.user_rank,
               (SELECT COUNT(*) FROM feedback_items x WHERE x.survey_id = s.survey_id)
        FROM feedback_surveys s
        JOIN feedback_items i ON i.survey_id = s.survey_id
        LEFT JOIN posts p ON p.channel = i.channel AND p.message_id = i.message_id
        WHERE s.submitted_at IS NOT NULL AND i.user_rank IS NOT NULL
        ORDER BY s.survey_id DESC, i.user_rank
        """
    ).fetchall()
    return [
        FeedbackExample(sid, kind, run_id, ch, mid, text, bool(inc), score, rank, n)
        for sid, kind, run_id, ch, mid, text, inc, score, rank, n in rows
    ]
