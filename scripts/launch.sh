#!/bin/bash
# Запуск консоли "одной кнопкой": поднимает scripts/console.py в фоне без окна
# Терминала и открывает её в браузере. Это единственное место с логикой запуска
# для macOS — приложение "TG Newspaper.app" (scripts/make_app.sh для разработки,
# scripts/build_release_mac.sh для релиза) лишь вызывает этот скрипт.
#
# Два режима:
#   разработка (TG_NEWSPAPER_HOME не задан) — всё лежит в репозитории: .venv,
#     data/, .env; зависимости через `uv sync`/`uv run`, uv должен быть установлен.
#   релиз (TG_NEWSPAPER_HOME задан приложением из dmg) — данные и окружение в
#     каталоге пользователя, сам скрипт лежит в бандле приложения, который
#     заменяется при обновлении. uv при необходимости ставится сам, пока идёт
#     подготовка — открыта страница ожидания.
#
# Переменные окружения (нужны для проверки, в обычной работе не задаются):
#   TG_NEWSPAPER_PORT     — порт консоли (по умолчанию 8420)
#   TG_NEWSPAPER_RUN_DIR  — где лежат console.log и console.pid (по умолчанию
#                           каталог БД из TG_NEWSPAPER_DB, иначе <данные>/data)
#   TG_NEWSPAPER_NO_OPEN  — не открывать браузер, только напечатать URL
#   TG_NEWSPAPER_DB       — путь к БД (пробрасывается в консоль как есть)
#   TG_NEWSPAPER_HOME     — каталог данных пользователя (включает релизный режим)

set -u

# Windows по умолчанию пишет stdout в файл в cp1251 — кириллица/эмодзи в логах
# роняют процесс; на macOS безвредно, но держим одинаково с лаунчером Windows.
export PYTHONUTF8=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${TG_NEWSPAPER_PORT:-8420}"
URL="http://localhost:${PORT}/"
# Проверки доступности — на 127.0.0.1: на Windows/некоторых системах localhost сначала
# резолвится в IPv6 (::1), а консоль слушает только IPv4. В браузер открываем $URL.
PROBE_URL="http://127.0.0.1:${PORT}/"

RELEASE=""
HOME_DIR=""
if [ -n "${TG_NEWSPAPER_HOME:-}" ]; then
    RELEASE=1
    HOME_DIR="$TG_NEWSPAPER_HOME"
    export TG_NEWSPAPER_HOME
    # Окружение вне бандла: его путь может меняться (App Translocation), а venv
    # должен переживать обновление приложения.
    export UV_PROJECT_ENVIRONMENT="$HOME_DIR/venv"
fi

if [ -n "${TG_NEWSPAPER_RUN_DIR:-}" ]; then
    RUN_DIR="$TG_NEWSPAPER_RUN_DIR"
elif [ -n "${TG_NEWSPAPER_DB:-}" ]; then
    RUN_DIR="$(dirname "$TG_NEWSPAPER_DB")"
elif [ -n "$RELEASE" ]; then
    RUN_DIR="$HOME_DIR/data"
else
    RUN_DIR="$ROOT/data"
fi
export TG_NEWSPAPER_RUN_DIR="$RUN_DIR"
LOG="$RUN_DIR/console.log"
PID_FILE="$RUN_DIR/console.pid"
SETUP_LOG="$RUN_DIR/setup.log"

# Диалог через argv, а не подстановкой в строку AppleScript: в логе бывают
# кавычки и обратные слэши, они бы сломали скрипт.
dialog() {
    # В тестах/CI (NO_OPEN, CI) диалог некому закрыть - он бы завис навсегда,
    # поэтому только stderr. Для людей диалог сам закрывается через 10 минут.
    if [ -n "${TG_NEWSPAPER_NO_OPEN:-}" ] || [ -n "${CI:-}" ]; then
        echo "$1" >&2
        return 0
    fi
    osascript -e 'on run argv' \
        -e 'display dialog (item 1 of argv) with title "TG Newspaper" buttons {"OK"} default button "OK" with icon caution giving up after 600' \
        -e 'end run' -- "$1" >/dev/null 2>&1 || echo "$1" >&2
}

notify() {
    osascript -e "display notification \"$1\" with title \"TG Newspaper\"" >/dev/null 2>&1 || true
}

open_url() {
    if [ -n "${TG_NEWSPAPER_NO_OPEN:-}" ]; then
        echo "open $URL"
    else
        open "$URL"
    fi
}

is_up() {
    curl -s -o /dev/null "$PROBE_URL"
}

# Повторный клик по иконке: консоль уже работает — просто открываем вкладку.
if is_up; then
    open_url
    exit 0
fi

mkdir -p "$RUN_DIR"

# Finder-приложения не получают PATH из .zshrc, поэтому uv ищем и по обычным местам.
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv ${HOME_DIR:+"$HOME_DIR/uv/uv"}; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            break
        fi
    done
fi

PREPARING_OPENED=""
if [ -n "$RELEASE" ]; then
    PY="$UV_PROJECT_ENVIRONMENT/bin/python"
    # Подготовка "реально нужна", если нет окружения или Chromium: тогда
    # открываем страницу ожидания сразу, не заставляя смотреть на пустой экран.
    need_prepare=""
    BUILD_ID=""
    [ -f "$ROOT/BUILD_ID" ] && BUILD_ID="$(cat "$ROOT/BUILD_ID")"
    STAMP="$UV_PROJECT_ENVIRONMENT/.tg-newspaper-build"
    if [ ! -x "$PY" ]; then
        need_prepare=1
    elif [ -z "$BUILD_ID" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$BUILD_ID" ]; then
        # Другая сборка (обновление приложения) или BUILD_ID нет (dev-сборка) — синхронизируем.
        need_prepare=1
    elif ! "$PY" -c 'import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)' >/dev/null 2>&1; then
        need_prepare=1
    fi

    if [ -z "$need_prepare" ]; then
        # Уже подготовлено этой же сборкой: uv не нужен вообще (работает офлайн,
        # даже если uv удалён), сразу стартуем консоль.
        echo "подготовка не нужна" >"$SETUP_LOG"
    else
        : >"$SETUP_LOG"
        if [ -z "${TG_NEWSPAPER_NO_OPEN:-}" ]; then
            # __PORT__ в шаблоне заменяем на порт; страница сама перейдёт в консоль.
            sed "s/__PORT__/${PORT}/g" "$ROOT/scripts/preparing.html" >"$RUN_DIR/preparing.html"
            open "$RUN_DIR/preparing.html" && PREPARING_OPENED=1
        fi

        # Только для проверки ветки установки uv: делаем вид, что uv не найден.
        [ -n "${TG_NEWSPAPER_FORCE_UV_INSTALL:-}" ] && UV=""
        if [ -z "$UV" ]; then
            echo "== установка uv ==" >>"$SETUP_LOG"
            # Ставим uv в свой каталог, без правки профилей оболочки (NO_MODIFY_PATH).
            if ! curl -LsSf https://astral.sh/uv/install.sh \
                | env UV_INSTALL_DIR="$HOME_DIR/uv" UV_NO_MODIFY_PATH=1 sh >>"$SETUP_LOG" 2>&1; then
                dialog "Не удалось установить uv (менеджер Python). Проверьте интернет и запустите TG Newspaper снова.

$(tail -n 15 "$SETUP_LOG")"
                exit 1
            fi
            UV="$HOME_DIR/uv/uv"
        fi

        cd "$ROOT" || exit 1
        # --no-editable: путь к бандлу может меняться (App Translocation), editable
        # .pth указывал бы в никуда. --reinstall-package: после обновления приложения
        # в venv должен оказаться новый код при той же версии пакета.
        echo "== uv sync ($UV) ==" >>"$SETUP_LOG"
        if ! "$UV" sync --frozen --no-editable --reinstall-package tg-newspaper --quiet >>"$SETUP_LOG" 2>&1; then
            dialog "Не удалось установить зависимости (uv sync):

$(tail -n 15 "$SETUP_LOG")"
            exit 1
        fi
        # Идемпотентно: при уже установленном Chromium возвращается сразу.
        echo "== playwright install chromium ==" >>"$SETUP_LOG"
        if ! "$PY" -m playwright install chromium >>"$SETUP_LOG" 2>&1; then
            dialog "Не удалось установить Chromium для вёрстки (playwright install):

$(tail -n 15 "$SETUP_LOG")"
            exit 1
        fi
        [ -n "$BUILD_ID" ] && echo "$BUILD_ID" >"$STAMP"
    fi
    cd "$ROOT" || exit 1

    # Напрямую интерпретатором venv, не `uv run`: он бы пере-синхронизировал
    # окружение (и в editable-режиме по умолчанию).
    nohup "$PY" scripts/console.py --no-browser --port "$PORT" >>"$LOG" 2>&1 &
    echo $! >"$PID_FILE"
else
    if [ -z "$UV" ]; then
        dialog "Не найден uv — менеджер Python-окружения, без него консоль не запустить.

Установите его в Терминале командой:
curl -LsSf https://astral.sh/uv/install.sh | sh

и запустите TG Newspaper снова."
        exit 1
    fi

    cd "$ROOT" || exit 1

    # Первый запуск (нет .venv) занимает минуту-две — предупреждаем уведомлением.
    if [ ! -d "$ROOT/.venv" ]; then
        notify "Готовлю окружение… первый запуск займёт минуту-две"
    fi

    if ! "$UV" sync --quiet >"$SETUP_LOG" 2>&1; then
        dialog "Не удалось установить зависимости (uv sync):

$(tail -n 15 "$SETUP_LOG")"
        exit 1
    fi
    # Идемпотентно: при уже установленном Chromium возвращается сразу.
    if ! "$UV" run playwright install chromium >"$SETUP_LOG" 2>&1; then
        dialog "Не удалось установить Chromium для вёрстки (playwright install):

$(tail -n 15 "$SETUP_LOG")"
        exit 1
    fi

    nohup "$UV" run python scripts/console.py --no-browser --port "$PORT" >>"$LOG" 2>&1 &
    echo $! >"$PID_FILE"
fi

# Ждём готовности порта до ~90 с (холодный старт на Windows-подобных системах и Defender-сканах).
for _ in $(seq 1 180); do
    if is_up; then
        # Страница ожидания сама переходит на консоль — вторая вкладка не нужна.
        if [ -z "$PREPARING_OPENED" ]; then
            open_url
        fi
        exit 0
    fi
    sleep 0.5
done

dialog "Консоль не поднялась за 90 секунд. Последние строки лога ($LOG):

$(tail -n 15 "$LOG")"
rm -f "$PID_FILE"
exit 1
