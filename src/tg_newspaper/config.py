"""Загрузка конфигурации: переменные окружения и список каналов."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNELS_PATH = REPO_ROOT / "config" / "channels.yaml"
DB_PATH = REPO_ROOT / "data" / "tg_newspaper.db"

# Период сбора — всегда фиксированные последние сутки от момента запуска.
# Наверстывание пропущенных дней сознательно не делается: не напечаталось — значит не напечаталось,
# следующий прогон всё равно заберёт только последние сутки.
LOOKBACK_HOURS = 24


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    channels: list[str]
    db_path: Path


def load_config() -> Config:
    load_dotenv()

    api_id = os.environ["TG_API_ID"]
    api_hash = os.environ["TG_API_HASH"]
    session_name = os.environ.get("TG_SESSION_NAME", "tg_newspaper")

    channels_data = yaml.safe_load(CHANNELS_PATH.read_text(encoding="utf-8"))
    channels = channels_data["channels"]

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        session_name=session_name,
        channels=channels,
        db_path=DB_PATH,
    )
