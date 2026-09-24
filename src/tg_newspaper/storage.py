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
    needs_photo INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, channel, message_id)
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
    rows: list[tuple[str, int, bool, str, str, bool]],
) -> None:
    """rows: (channel, message_id, included, stage, reason, needs_photo)."""
    conn.executemany(
        "INSERT INTO pipeline_outcomes "
        "(run_id, channel, message_id, included, stage, reason, needs_photo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, ch, mid, int(inc), stage, reason, int(needs_photo))
            for ch, mid, inc, stage, reason, needs_photo in rows
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
) -> list[tuple[str, int, bool, str, str, bool]]:
    rows = conn.execute(
        "SELECT channel, message_id, included, stage, reason, needs_photo "
        "FROM pipeline_outcomes WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [
        (ch, mid, bool(inc), stage, reason, bool(needs_photo))
        for ch, mid, inc, stage, reason, needs_photo in rows
    ]


def load_recent_included_posts(conn: sqlite3.Connection, limit_runs: int) -> list[Post]:
    """Посты, дошедшие до печати (included=1) в последних limit_runs
    сохранённых прогонах — окно сравнения для дедупликации против уже
    опубликованного (Этап 2 п.4). "Последние прогоны", не "последние дни":
    проект запускается по кнопке, а не по расписанию, календарное окно не
    имеет смысла (см. AGENTS.md, "Запуск")."""
    rows = conn.execute(
        """
        SELECT DISTINCT p.channel, p.message_id, p.posted_at, p.text, p.has_media, p.media_type
        FROM pipeline_outcomes o
        JOIN posts p ON p.channel = o.channel AND p.message_id = o.message_id
        WHERE o.included = 1
          AND o.run_id IN (SELECT run_id FROM pipeline_runs ORDER BY run_id DESC LIMIT ?)
        """,
        (limit_runs,),
    ).fetchall()
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
