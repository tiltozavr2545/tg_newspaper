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
# Переменная окружения, переопределяющая путь к БД. Нужна для ручной проверки
# консоли на копии базы (чтобы эксперименты с опросами не трогали рабочую
# data/tg_newspaper.db); в обычной работе не задаётся.
DB_PATH_ENV = "TG_NEWSPAPER_DB"

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
    # Gemini — нужен только для LLM-классификации (Этап 2 п.2), не для сбора/эвристик.
    # Пустой api_key — нормально для остального пайплайна; GeminiClassifier сам
    # откажет с понятной ошибкой, если его создать без ключа.
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str  # прокси к Gemini (Cloudflare Worker); пусто = напрямую


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
        db_path=Path(os.environ[DB_PATH_ENV]) if os.environ.get(DB_PATH_ENV) else DB_PATH,
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        gemini_base_url=os.environ.get("GEMINI_BASE_URL", "").strip().rstrip("/"),
    )
