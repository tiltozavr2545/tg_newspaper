"""Сбор истории каналов через Telethon за последние сутки."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from .config import LOOKBACK_HOURS, Config
from .storage import Post

logger = logging.getLogger(__name__)


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


async def collect_channel(
    client: TelegramClient, channel: str, since: datetime
) -> list[Post]:
    posts = []
    async for message in client.iter_messages(channel):
        if message.date <= since:
            break
        if message.action is not None:
            continue
        posts.append(_to_post(channel, message))

    logger.info(
        "channel=%s собрано=%d период=(%s, now]", channel, len(posts), since.isoformat()
    )
    return posts


async def collect_all(config: Config, since: datetime) -> list[Post]:
    posts: list[Post] = []
    async with TelegramClient(
        config.session_name, config.api_id, config.api_hash
    ) as client:
        for channel in config.channels:
            posts.extend(await collect_channel(client, channel, since))
    return posts


def collection_since(run_started_at: datetime) -> datetime:
    return run_started_at - timedelta(hours=LOOKBACK_HOURS)
