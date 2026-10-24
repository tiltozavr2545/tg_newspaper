"""Logic for the initial setup wizard (pages are in setup_html.py,
routes are in scripts/console.py).

The wizard lets a person configure the project in the browser, with no manual
file editing:
  1. Telegram API (api_id/api_hash)  -> .env
  2. Telegram account login          -> Telethon session file
  3. Gemini (key/model/proxy)        -> .env
  4. channel list (with sections)    -> config/channels.yaml
  (+ optional: YouTube channels for the "New videos" block -> same file)
There's no bot token and never will be: collection runs over MTProto under a
user account (see AGENTS.md). The session and api_hash are secrets: never
logged, and only ever shown masked in HTML and error messages.

The module deliberately doesn't depend on the HTTP server: everything that
can be tested without the network (writing .env, channel normalization,
status, login logic with a swappable client factory) is checked by a
separate script.
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

from .config import ChannelsFile, channels_path, env_path, read_channels_file, session_base
from .youtube import YoutubeChannel, YoutubeError, resolve_channel

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_SESSION_NAME = "tg_newspaper"

# Telegram network operations must not hang the handler: without a network,
# Telethon by default keeps retrying to reconnect for a long time.
TG_TIMEOUT_SECONDS = 15

# Wizard step names (order matters — it's the order steps are taken in).
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
    """A setup error with text ready to show to a human (not a traceback)."""


@dataclass(frozen=True)
class StepResult:
    """The outcome of a wizard action: ok + text to show on the page."""
    ok: bool
    message: str


# ---------------------------------------------------------------- secrets

def mask_secret(value: str) -> str:
    """Mask a secret for display: only the last 4 characters, and only if
    the secret is long enough (for a short one, 4 characters is already a
    noticeable chunk, so we show nothing in that case). Empty in, empty out."""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return "••••" + value[-4:]


def scrub(text: str, *secrets: str) -> str:
    """Strips known secrets out of error text before it's shown/logged: some
    third-party libraries include a URL with the key in their messages."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, mask_secret(secret))
    return text


# ---------------------------------------------------------------- .env

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _format_env_value(value: str) -> str:
    """Format a value for an .env line. Plain values are left as-is (matching
    .env.example's style); values with spaces/#/quotes get double-quoted."""
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+\-=,]+", value):
        return value
    if not value:
        return ""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Changes/adds only the keys from `updates` in .env, keeping every other
    line, comment, and unknown key as-is.

    The write is atomic: a temp file in the same directory (otherwise
    os.replace isn't atomic) with 0600 permissions from the moment it's
    created, then os.replace — a reader never sees a half-written .env, and
    the secret's permissions are never wider than 0600, not even for a
    moment. The process's own environment is then refreshed, so the console
    doesn't need restarting.
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
            # os.fchmod doesn't exist on Windows (Python 3.12); POSIX 0600
            # permissions don't apply there anyway — access to the profile is
            # governed by the user's folder ACL (%LOCALAPPDATA%), so this is
            # skipped with no real loss.
            if hasattr(os, "fchmod"):
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

    # Reload the environment. override=True: the values we just wrote must
    # win over whatever was already in os.environ. dotenv also substitutes an
    # empty value (as an empty string) — which is exactly what's needed for
    # "clear this field".
    load_dotenv(path, override=True)
    for key, value in updates.items():
        os.environ[key] = value


def env_value(key: str, default: str = "") -> str:
    """The current value from the process environment (.env is already loaded into it)."""
    return os.environ.get(key, default).strip()


def load_env_into_process() -> None:
    """Load .env into the environment without overwriting variables already
    set in the shell (same as load_config does)."""
    load_dotenv(env_path())


# ---------------------------------------------------------------- channels

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.I)


def normalize_channel(raw: str) -> str | None:
    """Normalizes an entry to a channel username with no @, or returns None if
    it isn't a username (t.me/+xxx invite links and joinchat links aren't
    openable by username — our Telethon collection works by username)."""
    item = raw.strip().strip("<>").strip()
    if not item:
        return None
    m = _LINK.match(item)
    if m:
        path = re.split(r"[?#]", m.group(1))[0].strip("/")
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "s":  # t.me/s/<username> — the channel's web preview
            parts = parts[1:]
        if not parts:
            return None
        item = parts[0]
    item = item.lstrip("@")
    return item if _USERNAME.fullmatch(item) else None


def parse_channels_text(text: str) -> tuple[ChannelsFile, list[str]]:
    """From the textarea's text — (structure minus youtube, unrecognized
    entries). A line starting with `#` is a section heading (taken whole,
    spaces included: "# World and politics"); channels after it belong to
    that section; before the first heading, channels have no section. Parsed
    line by line: splitting the whole text on whitespace would break
    headings. Within a regular line, commas/spaces separate channels.
    Duplicates (case-insensitive) are removed, keeping the first mention; an
    empty section is allowed."""
    cf = ChannelsFile()
    invalid: list[str] = []
    seen: set[str] = set()
    current: list[str] = cf.loose
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            name = stripped.lstrip("#").strip()
            if not name:
                invalid.append(stripped)
                continue
            existing = next((chs for n, chs in cf.rubrics if n == name), None)
            if existing is None:
                existing = []
                cf.rubrics.append((name, existing))
            current = existing
            continue
        for chunk in re.split(r"[\s,;]+", stripped):
            if not chunk:
                continue
            ch = normalize_channel(chunk)
            if ch is None:
                invalid.append(chunk)
            elif ch.lower() not in seen:
                seen.add(ch.lower())
                current.append(ch)
    return cf, invalid


def channels_to_text(cf: ChannelsFile) -> str:
    """The inverse of parse_channels_text: what's shown in the textarea."""
    blocks: list[str] = []
    if cf.loose:
        blocks.append("\n".join(cf.loose))
    for name, chs in cf.rubrics:
        blocks.append("\n".join([f"# {name}", *chs]))
    return "\n\n".join(blocks)


_CHANNELS_HEADER = (
    "# Список каналов-источников. Правится в консоли (мастер настройки) или руками.\n"
    "# Канал — username без @ (как в ссылке t.me/<username>). rubrics — секции газеты,\n"
    "# channels — каналы без рубрики, youtube — каналы для блока \"Новые видео\".\n"
)


def save_channels(path: Path, cf: ChannelsFile) -> None:
    """Writes channels.yaml (atomically). With no sections and no youtube
    entries, this comes out as the old `channels: [...]` format."""
    data: dict[str, object] = {}
    if cf.rubrics:
        data["rubrics"] = [{"name": n, "channels": list(chs)} for n, chs in cf.rubrics]
    data["channels"] = list(cf.loose)
    if cf.youtube:
        data["youtube"] = [{"id": y.channel_id, "title": y.title, "source": y.source} for y in cf.youtube]
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
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


def load_channels(path: Path) -> ChannelsFile:
    """The whole of channels.yaml; an empty structure if the file is missing or broken."""
    try:
        return read_channels_file(path)
    except (OSError, yaml.YAMLError):
        return ChannelsFile()


# ---------------------------------------------------------------- YouTube

def youtube_to_text(channels: list[YoutubeChannel]) -> str:
    """For the textarea: what the user entered (source), one per line."""
    return "\n".join(c.source for c in channels)


def resolve_youtube_text(
    text: str, known: list[YoutubeChannel], resolver=resolve_channel
) -> tuple[list[YoutubeChannel], list[tuple[str, StepResult]]]:
    """From the textarea's text — (channels, per-line results). Lines with a
    source that's already saved aren't re-resolved (the id/title come from
    the file) — the network is only needed for new ones. Duplicates by id
    are collapsed. If there are any errors, the caller saves nothing."""
    by_source = {c.source: c for c in known}
    channels: list[YoutubeChannel] = []
    results: list[tuple[str, StepResult]] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        ch = by_source.get(raw)
        if ch is None:
            try:
                ch = resolver(raw)
            except YoutubeError as exc:
                results.append((raw, StepResult(False, str(exc))))
                continue
        if any(c.channel_id == ch.channel_id for c in channels):
            results.append((raw, StepResult(True, f"повтор: {ch.title}")))
            continue
        channels.append(ch)
        results.append((raw, StepResult(True, f"{ch.title} ({ch.channel_id})")))
    return channels, results


# ---------------------------------------------------------------- Telegram

def session_file(session_name: str) -> Path:
    """The session file on disk: Telethon appends `.session` to the name."""
    return Path(session_base(session_name) + ".session")


def secure_session_files(session_name: str) -> None:
    """0600 permissions on the session file (and its SQLite journal): it's the key to the account."""
    base = session_file(session_name)
    for p in (base, base.with_name(base.name + "-journal")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            logger.warning("failed to set 0600 permissions on the session file")


def _default_client_factory(session: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    return TelegramClient(
        session, api_id, api_hash,
        connection_retries=1, retry_delay=1, timeout=TG_TIMEOUT_SECONDS,
    )


# The Telethon client factory. A module-level variable so a test script can
# swap in a fake and exercise login logic without a network.
client_factory = _default_client_factory


def _credentials() -> tuple[int, str, str] | None:
    api_id, api_hash = env_value("TG_API_ID"), env_value("TG_API_HASH")
    name = env_value("TG_SESSION_NAME", DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME
    if not api_id.isdigit() or not api_hash:
        return None
    return int(api_id), api_hash, name


def _run_with_client(op):
    """Opens a client on the session file, runs `await op(client)`, and
    closes it. Raises SetupError if there's no api_id/api_hash.

    Why the client isn't kept alive between HTTP requests. Login steps
    (phone -> code -> password) arrive as separate requests on separate
    ThreadingHTTPServer threads. A "one client in its own thread with its own
    loop" approach would need queues/synchronization, and would leave an open
    connection and SQLite session behind if the person closed the tab
    mid-login. "Reopen on the same session file" is simpler and more robust:
    the auth key is already saved to the file right after send_code_request,
    and Telegram's login state is tied to that key, not to the TCP
    connection, so sign_in(code/password) on a fresh connection with the same
    key works fine. Only phone_code_hash lives in process memory (see
    _Login). The cost is one extra connection per step — irrelevant for a
    manual, one-off login.
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
    """A human-readable message instead of a traceback. Known Telegram errors
    get their (Russian-language) text; anything else gets the class name plus
    a truncated, secret-scrubbed message."""
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
    """In-process-memory state for the multi-step login (one console = one
    person, so a single global state, behind a lock since requests arrive on
    different threads)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()  # reentrant: submit_* call session_account
        self.reset()

    def reset(self) -> None:
        self.stage: str | None = None  # None | "code" | "password"
        self.phone = ""
        self.code_hash = ""
        self.auth_cache: tuple[str, str] | None = None  # (session path, "Name (@user)")


LOGIN = _Login()


def _who(me) -> str:
    name = " ".join(p for p in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if p)
    username = getattr(me, "username", None)
    label = name or "без имени"
    return f"{label} (@{username})" if username else label


def session_account() -> tuple[bool | None, str]:
    """Whether the session is authorized: (True, "Name (@user)") / (False, "")
    / (None, "") when it couldn't be checked (no network). No session file,
    or no api_id/api_hash, means an immediate False with no network call.
    A success is cached in memory: the check makes a network connection, and
    it's needed on every console page (the redirect-to-setup logic)."""
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
        except Exception as exc:  # noqa: BLE001 — "couldn't check" isn't a setup error
            logger.warning("failed to check the Telegram session: %s", type(exc).__name__)
            return None, ""
        if ok:
            LOGIN.auth_cache = (path, who)
        return ok, who


def start_login(phone: str) -> StepResult:
    """Step 2a: send a code to the phone."""
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
    """Step 2b: enter the code. Result: logged in, 2FA password needed, or an error."""
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
                LOGIN.stage = None  # the code is no longer valid — must start over from the phone
            return StepResult(False, describe_telegram_error(exc))
        return _finish_login()


def submit_password(password: str) -> StepResult:
    """Step 2c: the two-factor auth password."""
    with LOGIN.lock:
        if LOGIN.stage != "password":
            return StepResult(False, "Пароль сейчас не требуется.")

        async def op(client):
            return await client.sign_in(password=password)

        try:
            _run_with_client(op)
        except Exception as exc:  # noqa: BLE001 — the message never contains the password (see describe_*)
            return StepResult(False, describe_telegram_error(exc))
        return _finish_login()


def _finish_login() -> StepResult:
    """Call this while holding LOGIN.lock, after a successful sign_in."""
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
    """Access check: get_entity for each channel via the authorized session.
    Returns [(channel, result)]; a blanket problem (no session/network) gets
    the same result repeated for all of them."""
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
                break  # anything further is just more flood errors
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
    """One cheap request (model metadata, no generation) using the same
    client as the classifier. Errors come back as readable text with hints."""
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


# ---------------------------------------------------------------- status

@dataclass(frozen=True)
class StepStatus:
    key: str
    title: str
    done: bool
    detail: str  # what's missing / what's configured (no secrets)


def compute_status(check_session: bool = True) -> list[StepStatus]:
    """What's configured and what isn't — with no exceptions when .env,
    channels.yaml, or the session is missing. check_session=False skips the
    network check of the session (the file-existence check still runs)."""
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
            if ok is None:  # no network — don't block the console just because we can't check
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], True, "файл сессии есть, проверить авторизацию не удалось (нет сети?)"))
            elif ok:
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], True, f"вход выполнен: {who}"))
            else:
                steps.append(StepStatus(STEP_LOGIN, STEP_TITLES[STEP_LOGIN], False, "сессия не авторизована — завершите вход"))

    # The proxy is mandatory: the agent runs from Russia, and Gemini
    # geo-blocks direct access. This requirement is enforced only here
    # (wizard/status); load_config() still works without a proxy, so the
    # pipeline and offline checks don't need to change.
    key = env_value("GEMINI_API_KEY")
    proxy = env_value("GEMINI_BASE_URL").rstrip("/")
    gem_missing = [n for n, v in (("ключ Gemini", key), ("адрес прокси GEMINI_BASE_URL", proxy)) if not v]
    if gem_missing:
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], False, "не задан " + " и ".join(gem_missing)))
    elif not proxy.startswith("https://"):
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], False, "GEMINI_BASE_URL должен начинаться с https://"))
    else:
        steps.append(StepStatus(STEP_GEMINI, STEP_TITLES[STEP_GEMINI], True, f"ключ {mask_secret(key)}, модель {env_value('GEMINI_MODEL', DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL}, прокси {proxy}"))

    channels = load_channels(channels_path()).channels
    if channels:
        steps.append(StepStatus(STEP_CHANNELS, STEP_TITLES[STEP_CHANNELS], True, f"каналов: {len(channels)}"))
    else:
        steps.append(StepStatus(STEP_CHANNELS, STEP_TITLES[STEP_CHANNELS], False, "список каналов пуст или channels.yaml отсутствует"))
    return steps


def next_missing_step(steps: list[StepStatus]) -> StepStatus | None:
    return next((s for s in steps if not s.done), None)
