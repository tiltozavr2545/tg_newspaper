"""Configuration loading: environment variables and the channel list."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .youtube import YoutubeChannel

REPO_ROOT = Path(__file__).resolve().parents[2]
# Base directory for user data: .env, channels.yaml, DB, photos, session.
# A program installed from a release gets replaced on update (and on macOS it
# may sit on a read-only volume due to App Translocation), so release
# launchers point TG_NEWSPAPER_HOME at the user's directory. Without the
# variable it's the repo, so dev behavior is unchanged.
HOME_ENV = "TG_NEWSPAPER_HOME"


def home_dir() -> Path:
    """User data directory: TG_NEWSPAPER_HOME or the repo root.
    Read on every call (not cached as a constant) so tests and launchers can
    change the environment after import."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else REPO_ROOT


# Default paths in dev mode (HOME not set). For real use, call
# env_path()/channels_path()/db_path()/media_dir() instead — they account for
# TG_NEWSPAPER_HOME and the individual overrides.
ENV_PATH = REPO_ROOT / ".env"
CHANNELS_PATH = REPO_ROOT / "config" / "channels.yaml"
DB_PATH = REPO_ROOT / "data" / "tg_newspaper.db"
# Environment variable overriding the DB path. Needed for manually testing the
# console against a copy of the database (so experimenting with surveys doesn't
# touch the live data/tg_newspaper.db); not set in normal operation.
DB_PATH_ENV = "TG_NEWSPAPER_DB"
# Similar overrides for the setup wizard (console, scripts/console.py):
# where .env, the channel list, and the Telethon session file live. Needed to
# test the wizard in a temp directory without touching real secrets; not set
# in normal operation.
#   TG_NEWSPAPER_ENV          — path to the .env file (default: <repo>/.env)
#   TG_NEWSPAPER_CHANNELS     — path to channels.yaml (default: config/channels.yaml)
#   TG_NEWSPAPER_SESSION_DIR  — directory for the session file (default: TG_NEWSPAPER_HOME,
#                               or without it, the current directory, same as Telethon's
#                               default when only a session name is given)
ENV_FILE_ENV = "TG_NEWSPAPER_ENV"
CHANNELS_PATH_ENV = "TG_NEWSPAPER_CHANNELS"
SESSION_DIR_ENV = "TG_NEWSPAPER_SESSION_DIR"
# Post photos are downloaded into media_dir() during collection (collector.py) —
# the file path then lives in pipeline_outcomes.photo_path (storage.py); the
# files themselves stay outside the DB.

# Collection period is always a fixed last-24-hours window from the run's start.
# Catching up on missed days is deliberately not done: if it didn't get printed,
# it didn't get printed — the next run will still only pick up the last 24 hours.
LOOKBACK_HOURS = 24


def env_path() -> Path:
    """Path to .env, honoring the TG_NEWSPAPER_ENV override."""
    override = os.environ.get(ENV_FILE_ENV)
    return Path(override) if override else home_dir() / ".env"


def channels_path() -> Path:
    """Path to channels.yaml, honoring the TG_NEWSPAPER_CHANNELS override."""
    override = os.environ.get(CHANNELS_PATH_ENV)
    return Path(override) if override else home_dir() / "config" / "channels.yaml"


def db_path() -> Path:
    """Path to the DB, honoring the TG_NEWSPAPER_DB override."""
    override = os.environ.get(DB_PATH_ENV)
    return Path(override) if override else home_dir() / "data" / "tg_newspaper.db"


def media_dir() -> Path:
    """Directory for downloaded post photos (alongside the user's data)."""
    return home_dir() / "data" / "media"


def session_base(session_name: str) -> str:
    """The `session` argument for TelegramClient: the session name, or, when
    TG_NEWSPAPER_SESSION_DIR is set, the full path without the `.session`
    suffix (Telethon adds that itself). Without the override, returns the
    name as-is — unchanged behavior (the file is created in the current
    directory)."""
    override = os.environ.get(SESSION_DIR_ENV)
    if override:
        return str(Path(override) / session_name)
    # Release build: the session lives in the user's directory, not cwd (cwd
    # there is the app directory, which gets replaced on update).
    if os.environ.get(HOME_ENV):
        return str(home_dir() / session_name)
    return session_name


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    channels: list[str]
    db_path: Path
    media_dir: Path
    # Gemini — needed only for LLM classification (Stage 2 item 2), not for
    # collection/heuristics. An empty api_key is fine for the rest of the
    # pipeline; GeminiClassifier itself fails with a clear error if created
    # without a key.
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str  # proxy to Gemini (Cloudflare Worker); empty = direct
    # Stage 7: rubrics (mosaic sections) and YouTube channels for the "New
    # videos" block. A channel with no rubric is absent from rubric_by_channel;
    # rubric_order is the order of sections in the issue (as in channels.yaml).
    # Empty — issue with no sections, as before.
    rubric_by_channel: dict[str, str] = field(default_factory=dict)
    rubric_order: list[str] = field(default_factory=list)
    youtube_channels: list[YoutubeChannel] = field(default_factory=list)


@dataclass
class ChannelsFile:
    """Contents of channels.yaml: rubrics (in file order), channels without a
    rubric, and YouTube channels. The single place that parses the format —
    used by load_config, the setup wizard, and compute_status."""

    rubrics: list[tuple[str, list[str]]] = field(default_factory=list)
    loose: list[str] = field(default_factory=list)
    youtube: list[YoutubeChannel] = field(default_factory=list)

    @property
    def channels(self) -> list[str]:
        """All Telegram channels without duplicates: rubrics in order first, then unrubricked ones."""
        seen: set[str] = set()
        out: list[str] = []
        for ch in [c for _, chs in self.rubrics for c in chs] + self.loose:
            if ch.lower() not in seen:
                seen.add(ch.lower())
                out.append(ch)
        return out

    @property
    def rubric_by_channel(self) -> dict[str, str]:
        result: dict[str, str] = {}
        taken: set[str] = set()
        for name, chs in self.rubrics:
            for ch in chs:
                if ch.lower() not in taken:  # channel in two rubrics — the first one wins
                    taken.add(ch.lower())
                    result[ch] = name
        return result

    @property
    def rubric_order(self) -> list[str]:
        return [name for name, _ in self.rubrics]


def parse_channels_data(data: object) -> ChannelsFile:
    """Parses the loaded YAML. The old format (just `channels: [...]`) has no
    rubrics. Duplicate channels (case-insensitive) collapse: the first
    mention is kept. Malformed entries are silently skipped — the file is
    also edited by hand."""
    if not isinstance(data, dict):
        return ChannelsFile()
    seen: set[str] = set()

    def take(items: object) -> list[str]:
        out: list[str] = []
        for item in items if isinstance(items, list) else []:
            ch = str(item).strip().lstrip("@")
            if ch and ch.lower() not in seen:
                seen.add(ch.lower())
                out.append(ch)
        return out

    rubrics: list[tuple[str, list[str]]] = []
    for r in data.get("rubrics") or []:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        name = str(r["name"]).strip()
        chs = take(r.get("channels"))
        existing = next((i for i, (n, _) in enumerate(rubrics) if n == name), None)
        if existing is None:
            rubrics.append((name, chs))
        else:  # merge rubrics with the same name
            rubrics[existing][1].extend(chs)
    loose = take(data.get("channels"))
    youtube: list[YoutubeChannel] = []
    for y in data.get("youtube") or []:
        if isinstance(y, dict) and y.get("id"):
            cid = str(y["id"]).strip()
            if all(c.channel_id != cid for c in youtube):
                youtube.append(YoutubeChannel(cid, str(y.get("title") or cid), str(y.get("source") or cid)))
    return ChannelsFile(rubrics, loose, youtube)


def read_channels_file(path: Path) -> ChannelsFile:
    """channels.yaml -> ChannelsFile; raises OSError/yaml.YAMLError if the file is missing or broken."""
    return parse_channels_data(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_config() -> Config:
    load_dotenv(env_path())

    api_id = os.environ["TG_API_ID"]
    api_hash = os.environ["TG_API_HASH"]
    session_name = os.environ.get("TG_SESSION_NAME", "tg_newspaper")

    cf = read_channels_file(channels_path())

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        session_name=session_base(session_name),
        channels=cf.channels,
        db_path=db_path(),
        media_dir=media_dir(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
        gemini_base_url=os.environ.get("GEMINI_BASE_URL", "").strip().rstrip("/"),
        rubric_by_channel=cf.rubric_by_channel,
        rubric_order=cf.rubric_order,
        youtube_channels=cf.youtube,
    )
