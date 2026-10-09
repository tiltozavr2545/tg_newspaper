"""Логика мастера первоначальной настройки (страницы — в setup_html.py,
маршруты — в scripts/console.py).

Мастер даёт человеку настроить проект в браузере, не правя файлы руками:
  1. Telegram API (api_id/api_hash)  -> .env
  2. вход в Telegram-аккаунт         -> файл сессии Telethon
  3. Gemini (ключ/модель/прокси)     -> .env
  4. список каналов                  -> config/channels.yaml
Бот-токена нет и не будет: сбор идёт по MTProto под пользовательским
аккаунтом (см. AGENTS.md). Сессия и api_hash — секреты: не логируются, в HTML
и сообщения об ошибках попадают только в маскированном виде.

Модуль намеренно не зависит от HTTP-сервера: всё, что можно проверить без
сети (запись .env, нормализация каналов, статус, логика входа с подменяемой
фабрикой клиента), проверяется отдельным скриптом.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .config import channels_path, env_path, session_base

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_SESSION_NAME = "tg_newspaper"

# Сетевые операции Telegram не должны подвешивать обработчик: без сети
# Telethon по умолчанию долго пробует переподключаться.
TG_TIMEOUT_SECONDS = 15

# Имена шагов мастера (порядок важен — это порядок прохождения).
STEP_TELEGRAM = "telegram"
STEP_LOGIN = "login"
STEP_GEMINI = "gemini"
STEP_CHANNELS = "channels"
STEP_ORDER = (STEP_TELEGRAM, STEP_LOGIN, STEP_GEMINI, STEP_CHANNELS)
STEP_TITLES = {
    STEP_TELEGRAM: "Telegram API",
    STEP_LOGIN: "Вход в Telegram",
    STEP_GEMINI: "Gemini",
    STEP_CHANNELS: "Каналы",
}


class SetupError(RuntimeError):
    """Ошибка настройки с готовым для человека текстом (не трейсбек)."""


@dataclass(frozen=True)
class StepResult:
    """Итог действия мастера: ok + текст для показа на странице."""
    ok: bool
    message: str


# ---------------------------------------------------------------- секреты

def mask_secret(value: str) -> str:
    """Маска для показа секрета: только последние 4 символа, и то лишь если
    секрет достаточно длинный (у короткого 4 символа — это уже заметная доля,
    поэтому там не показываем ничего). Пустой — пустая строка."""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return "••••" + value[-4:]


def scrub(text: str, *secrets: str) -> str:
    """Убирает известные секреты из текста ошибки перед показом/логом: сторонние
    библиотеки иногда включают в сообщение URL с ключом."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, mask_secret(secret))
    return text


# ---------------------------------------------------------------- .env

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _format_env_value(value: str) -> str:
    """Значение для строки .env. Простые значения — как есть (формат
    .env.example); со пробелами/#/кавычками — в двойных кавычках."""
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+\-=,]+", value):
        return value
    if not value:
        return ""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Меняет/добавляет в .env только ключи из `updates`, сохраняя остальные
    строки, комментарии и неизвестные ключи как есть.

    Запись атомарная: временный файл в том же каталоге (иначе os.replace не
    атомарен) с правами 0600 с самого создания, затем os.replace — читатель
    никогда не увидит наполовину записанный .env, а права секрета не бывают
    шире 0600 даже на мгновение. Затем окружение процесса обновляется, чтобы
    консоль не приходилось перезапускать.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={_format_env_value(remaining.pop(key))}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        if not lines:
            out.append("# Создано мастером настройки консоли. .env в репозиторий не коммитить.")
        for key, value in remaining.items():
            out.append(f"{key}={_format_env_value(value)}")
    text = "\n".join(out) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    os.chmod(path, 0o600)

    # Перечитать окружение. override=True: значения, которые мы только что
    # записали, должны победить прежние в os.environ. Пустое значение dotenv
    # тоже подставит (как пустую строку) — это то, что нужно для "очистить".
    load_dotenv(path, override=True)
    for key, value in updates.items():
        os.environ[key] = value


def env_value(key: str, default: str = "") -> str:
    """Текущее значение из окружения процесса (в него уже подгружен .env)."""
    return os.environ.get(key, default).strip()


def load_env_into_process() -> None:
    """Подтянуть .env в окружение, не затирая переменные, заданные в оболочке
    (как делает load_config)."""
    load_dotenv(env_path())


# ---------------------------------------------------------------- каналы

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.I)


def normalize_channel(raw: str) -> str | None:
    """Приводит запись к username канала без @ или возвращает None, если это
    не username (приглашения t.me/+xxx и joinchat по username не открыть —
    Telethon-сбор у нас идёт по username)."""
    item = raw.strip().strip("<>").strip()
    if not item:
        return None
    m = _LINK.match(item)
    if m:
        path = re.split(r"[?#]", m.group(1))[0].strip("/")
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "s":  # t.me/s/<username> — веб-превью канала
            parts = parts[1:]
        if not parts:
            return None
        item = parts[0]
    item = item.lstrip("@")
    return item if _USERNAME.fullmatch(item) else None


def normalize_channels(text: str) -> tuple[list[str], list[str]]:
    """Из текста textarea — (каналы, нераспознанное). По одному на строку, но
    запятые/пробелы внутри строки тоже разделяют. Дубли (без учёта регистра)
    убираются, порядок первого появления сохраняется, пустые строки пропускаются."""
    channels: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\s,;]+", text):
        if not chunk:
            continue
        name = normalize_channel(chunk)
        if name is None:
            invalid.append(chunk)
        elif name.lower() not in seen:
            seen.add(name.lower())
            channels.append(name)
    return channels, invalid


_CHANNELS_HEADER = (
    "# Список каналов-источников. Правится в консоли (мастер настройки) или руками.\n"
    "# Формат — username канала без @ (как в ссылке t.me/<username>).\n"
)


def save_channels(path: Path, channels: list[str]) -> None:
    """Пишет channels.yaml в формате channels.example.yaml (атомарно)."""
    body = yaml.safe_dump({"channels": channels}, allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".channels.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(_CHANNELS_HEADER + body)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def load_channels(path: Path) -> list[str]:
    """Каналы из channels.yaml; пустой список, если файла нет или он битый."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        channels = data["channels"]
        return [str(c) for c in channels] if isinstance(channels, list) else []
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return []


# ---------------------------------------------------------------- Telegram

def session_file(session_name: str) -> Path:
    """Файл сессии на диске: Telethon дописывает `.session` к имени."""
    return Path(session_base(session_name) + ".session")


def secure_session_files(session_name: str) -> None:
    """Права 0600 на файл сессии (и журнал SQLite): это ключ к аккаунту."""
    base = session_file(session_name)
    for p in (base, base.with_name(base.name + "-journal")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            logger.warning("не удалось выставить права 0600 на файл сессии")


def _default_client_factory(session: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    return TelegramClient(
        session, api_id, api_hash,
        connection_retries=1, retry_delay=1, timeout=TG_TIMEOUT_SECONDS,
    )


# Фабрика клиента Telethon. Модуль-уровневая переменная — чтобы проверочный
# скрипт мог подставить фейк и прогнать логику входа без сети.
client_factory = _default_client_factory


def _credentials() -> tuple[int, str, str] | None:
    api_id, api_hash = env_value("TG_API_ID"), env_value("TG_API_HASH")
    name = env_value("TG_SESSION_NAME", DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME
    if not api_id.isdigit() or not api_hash:
        return None
    return int(api_id), api_hash, name


def _run_with_client(op):
    """Открывает клиента на файле сессии, выполняет `await op(client)`,
    закрывает. Бросает SetupError, если нет api_id/api_hash.

    Почему клиент не держится живым между HTTP-запросами. Шаги входа (телефон
    -> код -> пароль) приходят разными запросами в разных потоках
    ThreadingHTTPServer. Вариант "один клиент в отдельном потоке со своим
    циклом" требует очередей/синхронизации и оставляет открытое соединение и
    SQLite-сессию, если человек закрыл вкладку посреди входа. Вариант
    "переоткрывать на том же файле сессии" проще и надёжнее: auth key
    сохраняется в файл уже после send_code_request, а состояние входа на
    стороне Telegram привязано к этому ключу, а не к TCP-соединению, поэтому
    sign_in(code/password) на новом соединении с тем же ключом работает. В
    памяти процесса остаётся только phone_code_hash (см. _Login). Платим одним
    лишним подключением на шаг — для ручного одноразового входа несущественно.
    """
    creds = _credentials()
    if creds is None:
        raise SetupError("Сначала задайте api_id и api_hash (шаг 1).")
    api_id, api_hash, name = creds

    async def main():
        client = client_factory(session_base(name), api_id, api_hash)
        await asyncio.wait_for(client.connect(), TG_TIMEOUT_SECONDS * 2)
        try:
            return await op(client)
        finally:
            await client.disconnect()

    try:
        return asyncio.run(main())
    finally:
        secure_session_files(name)


def describe_telegram_error(exc: BaseException) -> str:
    """Понятный текст вместо трейсбека. Известные ошибки Telegram — по-русски,
    прочие — имя класса и обрезанное сообщение без секретов."""
    from telethon import errors as e

    if isinstance(exc, SetupError):
        return str(exc)
    if isinstance(exc, e.FloodWaitError):
        secs = getattr(exc, "seconds", 0) or 0
        mins = max(1, round(secs / 60))
        return f"Telegram просит подождать (флуд-лимит): примерно {mins} мин. ({secs} с). Попробуйте позже."
    table = (
        (e.PhoneNumberInvalidError, "Неверный номер телефона. Нужен международный формат, например +79991234567."),
        (e.PhoneNumberBannedError, "Этот номер заблокирован в Telegram."),
        (e.PhoneNumberFloodError, "С этого номера слишком много попыток — подождите и повторите позже."),
        (e.PhoneCodeInvalidError, "Неверный код. Проверьте код из Telegram и введите снова."),
        (e.PhoneCodeEmptyError, "Код не введён."),
        (e.PhoneCodeExpiredError, "Код истёк. Запросите новый код."),
        (e.PasswordHashInvalidError, "Неверный пароль двухфакторной защиты."),
        (e.ApiIdInvalidError, "Telegram не принял api_id/api_hash — проверьте их на my.telegram.org (шаг 1)."),
        (e.SendCodeUnavailableError, "Telegram временно не может отправить код (исчерпаны способы доставки). Подождите и повторите."),
    )
    for cls, text in table:
        if isinstance(exc, cls):
            return text
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return "Не удалось связаться с Telegram (нет сети или соединение заблокировано). Проверьте подключение и повторите."
    creds = _credentials()
    secrets = (creds[1],) if creds else ()
    return f"{type(exc).__name__}: {scrub(str(exc), *secrets)[:200]}"


class _Login:
    """Состояние многошагового входа в памяти процесса (одна консоль — один
    человек, поэтому одно глобальное состояние, под замком: запросы идут в
    разных потоках)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()  # реентерабельный: submit_* зовут session_account
        self.reset()

    def reset(self) -> None:
        self.stage: str | None = None  # None | "code" | "password"
        self.phone = ""
        self.code_hash = ""
        self.auth_cache: tuple[str, str] | None = None  # (путь сессии, "Имя (@user)")


LOGIN = _Login()


def _who(me) -> str:
    name = " ".join(p for p in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if p)
    username = getattr(me, "username", None)
    label = name or "без имени"
    return f"{label} (@{username})" if username else label


def session_account() -> tuple[bool | None, str]:
    """Авторизована ли сессия: (True, "Имя (@user)") / (False, "") / (None, "")
    когда проверить не удалось (нет сети). Файла сессии нет или нет
    api_id/api_hash — сразу False, без обращения к сети. Успех кэшируется
    в памяти: проверка делает сетевое подключение, а нужна на каждой странице
    консоли (редирект до настройки)."""
    creds = _credentials()
    if creds is None:
        return False, ""
    path = str(session_file(creds[2]))
    if not Path(path).exists():
        return False, ""
    with LOGIN.lock:
        if LOGIN.auth_cache and LOGIN.auth_cache[0] == path:
            return True, LOGIN.auth_cache[1]

        async def op(client):
            if not await client.is_user_authorized():
                return False, ""
            return True, _who(await client.get_me())

        try:
            ok, who = _run_with_client(op)
        except Exception as exc:  # noqa: BLE001 — "не удалось проверить" — не ошибка настройки
            logger.warning("не удалось проверить сессию Telegram: %s", type(exc).__name__)
            return None, ""
        if ok:
            LOGIN.auth_cache = (path, who)
        return ok, who


def start_login(phone: str) -> StepResult:
    """Шаг 2a: отправить код на телефон."""
    phone = phone.strip().replace(" ", "")
    if not re.fullmatch(r"\+?\d{7,15}", phone):
        return StepResult(False, "Введите номер в международном формате, например +79991234567.")

    async def op(client):
        return await client.send_code_request(phone)

    with LOGIN.lock:
        try:
            sent = _run_with_client(op)
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, describe_telegram_error(exc))
        LOGIN.stage = "code"
        LOGIN.phone = phone
        LOGIN.code_hash = sent.phone_code_hash
        LOGIN.auth_cache = None
    return StepResult(True, "Код отправлен в Telegram (в приложение на другом устройстве, не по SMS). Введите его ниже.")


def submit_code(code: str) -> StepResult:
    """Шаг 2b: ввести код. Результат: вход выполнен, нужен пароль 2FA или ошибка."""
    from telethon import errors as e

    code = re.sub(r"\D", "", code)
    with LOGIN.lock:
        if LOGIN.stage != "code":
            return StepResult(False, "Сначала запросите код: введите номер телефона.")
        phone, code_hash = LOGIN.phone, LOGIN.code_hash

        async def op(client):
            return await client.sign_in(phone, code, phone_code_hash=code_hash)

        try:
            _run_with_client(op)
        except e.SessionPasswordNeededError:
            LOGIN.stage = "password"
            return StepResult(True, "На аккаунте включена двухфакторная защита — введите пароль.")
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (e.PhoneCodeExpiredError, e.PhoneNumberInvalidError)):
                LOGIN.stage = None  # код уже не годится — надо начинать с телефона
            return StepResult(False, describe_telegram_error(exc))
        return _finish_login()


def submit_password(password: str) -> StepResult:
    """Шаг 2c: пароль двухфакторной защиты."""
    with LOGIN.lock:
        if LOGIN.stage != "password":
            return StepResult(False, "Пароль сейчас не требуется.")

        async def op(client):
            return await client.sign_in(password=password)

        try:
            _run_with_client(op)
        except Exception as exc:  # noqa: BLE001 — сообщение не содержит пароль (см. describe_*)
            return StepResult(False, describe_telegram_error(exc))
        return _finish_login()


def _finish_login() -> StepResult:
    """Вызывать под LOGIN.lock после успешного sign_in."""
    LOGIN.stage = None
    LOGIN.phone = LOGIN.code_hash = ""
    LOGIN.auth_cache = None
    ok, who = session_account()
    if ok:
        return StepResult(True, f"Вход выполнен: {who}.")
    return StepResult(False, "Telegram принял вход, но проверить сессию не удалось — обновите страницу.")


def cancel_login() -> None:
    with LOGIN.lock:
        LOGIN.stage = None
        LOGIN.phone = LOGIN.code_hash = ""


def check_channels(channels: list[str]) -> list[tuple[str, StepResult]]:
    """Проверка доступа: get_entity по каждому каналу через авторизованную
    сессию. Возвращает [(канал, результат)]; общая проблема (нет сессии/сети)
    — один результат на все."""
    from telethon import errors as e

    ok, _ = session_account()
    if ok is False:
        msg = StepResult(False, "Сначала войдите в Telegram (шаг 2) — без сессии каналы не проверить.")
        return [(c, msg) for c in channels]

    async def op(client):
        results = []
        for ch in channels:
            try:
                ent = await client.get_entity(ch)
            except (e.UsernameNotOccupiedError, e.UsernameInvalidError):
                results.append((ch, StepResult(False, "такого канала нет (проверьте username)")))
            except e.ChannelPrivateError:
                results.append((ch, StepResult(False, "канал приватный или аккаунт из него исключён")))
            except e.FloodWaitError as exc:
                results.append((ch, StepResult(False, describe_telegram_error(exc))))
                break  # дальше — только больше флуд-ошибок
            except ValueError:
                results.append((ch, StepResult(False, "не найден (Telegram не знает такой username)")))
            except Exception as exc:  # noqa: BLE001
                results.append((ch, StepResult(False, describe_telegram_error(exc))))
            else:
                title = getattr(ent, "title", None)
                if title is None:  # пользователь/бот, а не канал
                    results.append((ch, StepResult(False, "это не канал, а пользователь или бот")))
                else:
                    results.append((ch, StepResult(True, f"доступен: {title}")))
        return results

    try:
        results = _run_with_client(op)
    except Exception as exc:  # noqa: BLE001
        msg = StepResult(False, describe_telegram_error(exc))
        return [(c, msg) for c in channels]
    done = {c for c, _ in results}
    skipped = StepResult(False, "не проверен (остановлено из-за флуд-лимита)")
    return results + [(c, skipped) for c in channels if c not in done]


# ---------------------------------------------------------------- Gemini

def check_gemini(api_key: str, model: str, base_url: str, factory=None) -> StepResult:
    """Один дешёвый запрос (метаданные модели, без генерации) тем же клиентом,
    что у классификатора. Ошибки — понятным текстом с подсказками."""
    if not api_key:
        return StepResult(False, "Ключ Gemini не задан.")
    if not base_url:
        return StepResult(False, "Адрес прокси GEMINI_BASE_URL не задан — Gemini напрямую из РФ недоступен.")
    if factory is None:
        from .classifier import make_gemini_client as factory
    try:
        client = factory(api_key, base_url, 20000)
        client.models.get(model=model)
    except Exception as exc:  # noqa: BLE001
        text = scrub(str(exc), api_key)
        low = text.lower()
        if "location is not supported" in low:
            return StepResult(False, "Gemini недоступен из вашего региона (\"User location is not supported\"). "
                              "Нужен прокси через Cloudflare Worker — заполните поле GEMINI_BASE_URL (инструкция в README).")
        if "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low:
            return StepResult(False, "Ключ не принят Gemini — проверьте его в Google AI Studio.")
        if "not_found" in low or "404" in low:
            return StepResult(False, f"Модель «{model}» не найдена (или прокси не пропускает этот путь). Проверьте название модели и GEMINI_BASE_URL.")
        if any(w in low for w in ("connect", "timeout", "timed out", "name or service", "nodename")):
            return StepResult(False, "Не удалось связаться с Gemini (сеть/прокси недоступны). Проверьте подключение и GEMINI_BASE_URL.")
        return StepResult(False, f"{type(exc).__name__}: {text[:300]}")
    return StepResult(True, f"Ключ принят, модель «{model}» доступна.")


# ---------------------------------------------------------------- статус

@dataclass(frozen=True)
class StepStatus:
    key: str
    title: str
    done: bool
    detail: str  # чего не хватает / что настроено (без секретов)


def compute_status(check_session: bool = True) -> list[StepStatus]:
    """Что настроено, а что нет — без исключений при отсутствии .env,
    channels.yaml и сессии. check_session=False пропускает сетевую проверку
    сессии (остаётся проверка наличия файла)."""
    load_env_into_process()
    steps: list[StepStatus] = []

    api_id, api_hash = env_value("TG_API_ID"), env_value("TG_API_HASH")
    missing = [n for n, v in (("api_id", api_id), ("api_hash", api_hash)) if not v]
    if missing:
        steps.append(StepStatus(STEP_TELEGRAM, STEP_TITLES[STEP_TELEGRAM], False, "не задан " + " и ".join(missing)))
    elif not api_id.isdigit():
        steps.append(StepStatus(STEP_TELEGRAM, STEP_TITLES[STEP_TELEGRAM], False, "api_id должен быть целым числом"))
    else:
        steps.append(StepStatus(STEP_TELEGRAM, STEP_TITLES[STEP_TELEGRAM], True, f"api_id {api_id}, api_hash {mask_secret(api_hash)}"))
    telegram_ok = steps[-1].done

    if not telegram_ok:
        steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], False, "сначала шаг 1"))
    else:
        name = env_value("TG_SESSION_NAME", DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME
        if not session_file(name).exists():
            steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], False, "вход не выполнен (нет файла сессии)"))
        elif not check_session:
            steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], True, "файл сессии есть (авторизация не проверялась)"))
        else:
            ok, who = session_account()
            if ok is None:  # сети нет — не блокируем консоль из-за невозможности проверки
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], True, "файл сессии есть, проверить авторизацию не удалось (нет сети?)"))
            elif ok:
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], True, f"вход выполнен: {who}"))
            else:
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], False, "сессия не авторизована — завершите вход"))

    # Прокси обязателен: агент работает из РФ, Gemini напрямую гео-блокирует.
    # Обязательность — только здесь (мастер/статус); load_config() без прокси
    # по-прежнему работает, чтобы пайплайн и офлайн-проверки не менялись.
    key = env_value("GEMINI_API_KEY")
    proxy = env_value("GEMINI_BASE_URL").rstrip("/")
    gem_missing = [n for n, v in (("ключ Gemini", key), ("адрес прокси GEMINI_BASE_URL", proxy)) if not v]
    if gem_missing:
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], False, "не задан " + " и ".join(gem_missing)))
    elif not proxy.startswith("https://"):
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], False, "GEMINI_BASE_URL должен начинаться с https://"))
    else:
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], True, f"ключ {mask_secret(key)}, модель {env_value('GEMINI_MODEL', DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL}, прокси {proxy}"))

    channels = load_channels(channels_path())
    if channels:
        steps.append(StepStatus(STEP_CHANNELS, STEP_TITLES[STEP_CHANNELS], True, f"каналов: {len(channels)}"))
    else:
        steps.append(StepStatus(STEP_CHANNELS, STEP_TITLES[STEP_CHANNELS], False, "список каналов пуст или channels.yaml отсутствует"))
    return steps


def next_missing_step(steps: list[StepStatus]) -> StepStatus | None:
    return next((s for s in steps if not s.done), None)
