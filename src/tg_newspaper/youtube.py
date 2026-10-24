"""YouTube channels for the "New videos" block at the end of the issue (Stage 7 item 2).

Source — the channel's public RSS (`/feeds/videos.xml?channel_id=UC...`), no
API key needed. The module doesn't import config.py (config.py imports
YoutubeChannel from here), paths are passed as parameters."""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class YoutubeChannel:
    """A tracked YouTube channel from channels.yaml (key `youtube`)."""

    channel_id: str  # UC... — used to build the RSS URL
    title: str  # channel name at the time it was added (for the console and the issue)
    source: str  # how the user entered it (@handle, link, UC...) — shown in the wizard


@dataclass(frozen=True)
class YoutubeVideo:
    """A new video from the run period — what gets printed in the "New videos" block."""

    video_id: str
    channel_id: str
    channel_title: str
    title: str
    published_at: datetime  # aware, UTC
    thumbnail_path: str | None  # local thumbnail file; None if it didn't download


logger = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
THUMB_URL = "https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"  # 320x180: enough for printing in the block
TIMEOUT_SECONDS = 10
# Without a browser-like User-Agent and the SOCS cookie, the channel page
# returns a cookie-consent screen instead of the channel markup — the id
# can't be extracted from it.
_PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Cookie": "SOCS=CAI",
    "Accept-Language": "en-US,en;q=0.9",
}

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_HOST = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(.+)$", re.I)
_HANDLE = re.compile(r"@[A-Za-z0-9_.\-]{3,}")


class YoutubeError(RuntimeError):
    """A resolve/fetch error with a human-ready message."""


def _get(url: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": _PAGE_HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 (https, our own URLs)
        return resp.read()


def parse_youtube_input(raw: str) -> tuple[str | None, str | None]:
    """User input -> (channel_id, page_url). For UC... and /channel/UC..., the
    id is known right away (page_url=None); for @handle, /c/, /user/ the
    channel page is needed. (None, None) means it wasn't recognized."""
    item = raw.strip().strip("<>").strip()
    if _CHANNEL_ID.fullmatch(item):
        return item, None
    if _HANDLE.fullmatch(item):
        return None, f"https://www.youtube.com/{item}"
    m = _HOST.match(item)
    if not m:
        return None, None
    parts = [p for p in re.split(r"[?#]", m.group(1))[0].split("/") if p]
    if not parts:
        return None, None
    if parts[0] == "channel" and len(parts) > 1 and _CHANNEL_ID.fullmatch(parts[1]):
        return parts[1], None
    if _HANDLE.fullmatch(parts[0]):
        return None, f"https://www.youtube.com/{parts[0]}"
    if parts[0] in ("c", "user") and len(parts) > 1:
        return None, f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None, None


def _id_from_page(url: str) -> str:
    html = _get(url, _PAGE_HEADERS).decode("utf-8", "replace")
    m = re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', html) or re.search(
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', html
    )
    if not m:
        raise YoutubeError("не удалось найти id канала на странице")
    return m.group(1)


def _fetch_feed(channel_id: str) -> ET.Element:
    return ET.fromstring(_get(FEED_URL.format(channel_id=channel_id)))


def resolve_channel(raw: str) -> YoutubeChannel:
    """User input -> YoutubeChannel (id + title from RSS). Raises YoutubeError
    with a clear message; the RSS fetch also confirms the channel exists."""
    channel_id, page_url = parse_youtube_input(raw)
    if channel_id is None and page_url is None:
        raise YoutubeError("не похоже на YouTube-канал (нужны @handle, ссылка или UC...)")
    try:
        if channel_id is None:
            channel_id = _id_from_page(page_url)
        feed = _fetch_feed(channel_id)
    except urllib.error.HTTPError as exc:
        raise YoutubeError("канал не найден" if exc.code == 404 else f"YouTube ответил {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise YoutubeError(f"нет связи с YouTube ({exc})") from exc
    except ET.ParseError as exc:
        raise YoutubeError("YouTube вернул не RSS") from exc
    title = (feed.findtext("a:title", default="", namespaces=_NS) or "").strip()
    return YoutubeChannel(channel_id=channel_id, title=title or channel_id, source=raw.strip())


def _download_thumb(video_id: str, thumbs_dir: Path) -> str | None:
    target = thumbs_dir / f"{video_id}.jpg"
    if target.exists() and target.stat().st_size > 0:
        return str(target)
    try:
        data = _get(THUMB_URL.format(video_id=video_id))
        if not data:
            return None
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)
    except Exception as exc:  # noqa: BLE001 (the thumbnail is optional: the video works without it)
        logger.warning("YouTube: превью %s не скачалось: %s", video_id, exc)
        return None


def _channel_videos(channel: YoutubeChannel, since: datetime, until: datetime, thumbs_dir: Path) -> list[YoutubeVideo]:
    root = _fetch_feed(channel.channel_id)
    channel_title = (root.findtext("a:title", default="", namespaces=_NS) or "").strip() or channel.title
    videos: list[YoutubeVideo] = []
    for entry in root.findall("a:entry", _NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=_NS) or "").strip()
        published = (entry.findtext("a:published", default="", namespaces=_NS) or "").strip()
        if not video_id or not published:
            continue
        # Shorts aren't needed in the newspaper: their link looks like /shorts/<id>.
        if any("/shorts/" in (link.get("href") or "") for link in entry.findall("a:link", _NS)):
            continue
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        published_at = published_at.astimezone(timezone.utc)
        if not since <= published_at <= until:
            continue
        videos.append(YoutubeVideo(
            video_id=video_id,
            channel_id=channel.channel_id,
            channel_title=channel_title,
            title=(entry.findtext("a:title", default="", namespaces=_NS) or "").strip(),
            published_at=published_at,
            thumbnail_path=_download_thumb(video_id, thumbs_dir),
        ))
    return videos


def fetch_new_videos(
    channels: list[YoutubeChannel], since: datetime, until: datetime, thumbs_dir: Path
) -> tuple[list[YoutubeVideo], list[str]]:
    """New videos from the channels within [since, until] (newest first) and
    per-channel errors. A failure in one channel doesn't affect the others
    and doesn't raise an exception: the video block is secondary, and the
    issue shouldn't fail to assemble because of it."""
    videos: list[YoutubeVideo] = []
    errors: list[str] = []
    for channel in channels:
        try:
            videos.extend(_channel_videos(channel, since, until, thumbs_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube: канал %s (%s): %s", channel.title, channel.channel_id, exc)
            errors.append(f"{channel.title}: {exc}")
    videos.sort(key=lambda v: v.published_at, reverse=True)
    return videos, errors
