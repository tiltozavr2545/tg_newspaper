"""SQLite-хранилище сырых постов."""

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
"""


@dataclass(frozen=True)
class Post:
    channel: str
    message_id: int
    posted_at: datetime
    text: str
    has_media: bool
    media_type: str | None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


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
