"""Загрузка конфигурации: переменные окружения и список каналов."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
# Базовый каталог данных пользователя: .env, channels.yaml, БД, фото, сессия.
# Установленная из релиза программа заменяется при обновлении (а на macOS может
# лежать на read-only томе из-за App Translocation), поэтому лаунчеры релиза
# задают TG_NEWSPAPER_HOME на каталог пользователя. Без переменной — репозиторий,
# то есть поведение разработки не меняется.
HOME_ENV = "TG_NEWSPAPER_HOME"


def home_dir() -> Path:
    """Каталог данных пользователя: TG_NEWSPAPER_HOME или корень репозитория.
    Читается при каждом вызове (не константой), чтобы тесты и лаунчеры могли
    менять окружение после импорта."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else REPO_ROOT


# Пути по умолчанию в режиме разработки (HOME не задан). Для реальной работы
# используйте env_path()/channels_path()/db_path()/media_dir() — они учитывают
# TG_NEWSPAPER_HOME и точечные переопределения.
ENV_PATH = REPO_ROOT / ".env"
CHANNELS_PATH = REPO_ROOT / "config" / "channels.yaml"
DB_PATH = REPO_ROOT / "data" / "tg_newspaper.db"
# Переменная окружения, переопределяющая путь к БД. Нужна для ручной проверки
# консоли на копии базы (чтобы эксперименты с опросами не трогали рабочую
# data/tg_newspaper.db); в обычной работе не задаётся.
DB_PATH_ENV = "TG_NEWSPAPER_DB"
# Аналогичные переопределения для мастера настройки (консоль, scripts/console.py):
# где лежат .env, список каналов и файл сессии Telethon. Нужны, чтобы проверять
# мастер во временном каталоге и не трогать реальные секреты; в обычной работе
# не задаются.
#   TG_NEWSPAPER_ENV          — путь к файлу .env (по умолчанию <репозиторий>/.env)
#   TG_NEWSPAPER_CHANNELS     — путь к channels.yaml (по умолчанию config/channels.yaml)
#   TG_NEWSPAPER_SESSION_DIR  — каталог файла сессии (по умолчанию — TG_NEWSPAPER_HOME,
#                               а без него — текущий каталог, как у Telethon, когда
#                               задано только имя сессии)
ENV_FILE_ENV = "TG_NEWSPAPER_ENV"
CHANNELS_PATH_ENV = "TG_NEWSPAPER_CHANNELS"
SESSION_DIR_ENV = "TG_NEWSPAPER_SESSION_DIR"
# Фото постов скачиваются в media_dir() при сборе (collector.py) — путь на файл
# живёт дальше в pipeline_outcomes.photo_path (storage.py), сами файлы вне БД.

# Период сбора — всегда фиксированные последние сутки от момента запуска.
# Наверстывание пропущенных дней сознательно не делается: не напечаталось — значит не напечаталось,
# следующий прогон всё равно заберёт только последние сутки.
LOOKBACK_HOURS = 24


def env_path() -> Path:
    """Путь к .env с учётом переопределения TG_NEWSPAPER_ENV."""
    override = os.environ.get(ENV_FILE_ENV)
    return Path(override) if override else home_dir() / ".env"


def channels_path() -> Path:
    """Путь к channels.yaml с учётом переопределения TG_NEWSPAPER_CHANNELS."""
    override = os.environ.get(CHANNELS_PATH_ENV)
    return Path(override) if override else home_dir() / "config" / "channels.yaml"


def db_path() -> Path:
    """Путь к БД с учётом переопределения TG_NEWSPAPER_DB."""
    override = os.environ.get(DB_PATH_ENV)
    return Path(override) if override else home_dir() / "data" / "tg_newspaper.db"


def media_dir() -> Path:
    """Каталог скачанных фото постов (рядом с данными пользователя)."""
    return home_dir() / "data" / "media"


def session_base(session_name: str) -> str:
    """Аргумент `session` для TelegramClient: имя сессии, а при заданном
    TG_NEWSPAPER_SESSION_DIR — полный путь без суффикса `.session` (его
    добавляет сам Telethon). Без переопределения возвращает имя как есть —
    поведение прежнее (файл создаётся в текущем каталоге)."""
    override = os.environ.get(SESSION_DIR_ENV)
    if override:
        return str(Path(override) / session_name)
    # Релиз: сессия лежит в каталоге пользователя, а не в cwd (cwd там — каталог
    # приложения, который заменяется при обновлении).
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
    # Gemini — нужен только для LLM-классификации (Этап 2 п.2), не для сбора/эвристик.
    # Пустой api_key — нормально для остального пайплайна; GeminiClassifier сам
    # откажет с понятной ошибкой, если его создать без ключа.
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str  # прокси к Gemini (Cloudflare Worker); пусто = напрямую


def load_config() -> Config:
    load_dotenv(env_path())

    api_id = os.environ["TG_API_ID"]
    api_hash = os.environ["TG_API_HASH"]
    session_name = os.environ.get("TG_SESSION_NAME", "tg_newspaper")

    channels_data = yaml.safe_load(channels_path().read_text(encoding="utf-8"))
    channels = channels_data["channels"]

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        session_name=session_base(session_name),
        channels=channels,
        db_path=db_path(),
        media_dir=media_dir(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
        gemini_base_url=os.environ.get("GEMINI_BASE_URL", "").strip().rstrip("/"),
    )
