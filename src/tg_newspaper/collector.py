"""Сбор истории каналов через Telethon за последние сутки."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageMediaPhoto

from .config import LOOKBACK_HOURS, Config
from .storage import Post

logger = logging.getLogger(__name__)

Key = tuple[str, int]


def _to_post(channel: str, message: Message) -> Post:
    media_type = type(message.media).__name__ if message.media else None
    return Post(
        channel=channel,
        message_id=message.id,
        posted_at=message.date,
        text=message.text or "",
        has_media=message.media is not None,
        media_type=media_type,
    )


async def _download_photo(
    client: TelegramClient, channel: str, message: Message, media_dir: Path
) -> Path | None:
    """Скачивает фото поста (не видео/документ — их в номер не ставим) в
    media_dir под предсказуемым именем. Возвращает None молча при отсутствии
    фото или при сбое скачивания — отсутствие фото никогда не должно ронять
    весь прогон сбора, просто эта новость напечатается без иллюстрации
    (см. layout.py — рендер уже устойчив к article.photo_data_uri = None)."""
    if not isinstance(message.media, MessageMediaPhoto):
        return None
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{channel}_{message.id}.jpg"
    try:
        saved = await message.download_media(file=str(dest))
    except Exception:
        logger.exception("не удалось скачать фото %s/%s", channel, message.id)
        return None
    return Path(saved) if saved else None


async def collect_channel(
    client: TelegramClient, channel: str, since: datetime, media_dir: Path
) -> tuple[list[Post], dict[Key, Path]]:
    posts = []
    photo_paths: dict[Key, Path] = {}
    async for message in client.iter_messages(channel):
        if message.date <= since:
            break
        if message.action is not None:
            continue
        posts.append(_to_post(channel, message))
        photo_path = await _download_photo(client, channel, message, media_dir)
        if photo_path is not None:
            photo_paths[(channel, message.id)] = photo_path

    logger.info(
        "channel=%s собрано=%d фото=%d период=(%s, now]",
        channel, len(posts), len(photo_paths), since.isoformat(),
    )
    return posts, photo_paths


async def collect_all(
    config: Config, since: datetime
) -> tuple[list[Post], dict[Key, Path]]:
    posts: list[Post] = []
    photo_paths: dict[Key, Path] = {}
    async with TelegramClient(
        config.session_name, config.api_id, config.api_hash
    ) as client:
        for channel in config.channels:
            channel_posts, channel_photos = await collect_channel(
                client, channel, since, config.media_dir
            )
            posts.extend(channel_posts)
            photo_paths.update(channel_photos)
    return posts, photo_paths


def collection_since(run_started_at: datetime) -> datetime:
    return run_started_at - timedelta(hours=LOOKBACK_HOURS)
