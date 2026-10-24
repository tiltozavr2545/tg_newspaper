#!/bin/bash
# "One-button" console launch: starts scripts/console.py in the background without a
# Terminal window and opens it in the browser. This is the single place with the launch logic
# for macOS — the "TG Newspaper.app" application (scripts/make_app.sh for development,
# scripts/build_release_mac.sh for release) just calls this script.
#
# Two modes:
#   development (TG_NEWSPAPER_HOME not set) — everything lives in the repo: .venv,
#     data/, .env; dependencies via `uv sync`/`uv run`, uv must be installed.
#   release (TG_NEWSPAPER_HOME set by the app from the dmg) — data and the environment live in
#     the user's directory, the script itself lives in the app bundle, which
#     gets replaced on update. uv installs itself if needed while
#     preparation is underway — a waiting page is shown.
#
# Environment variables (needed for testing; not set during normal use):
#   TG_NEWSPAPER_PORT     — console port (default 8420)
#   TG_NEWSPAPER_RUN_DIR  — where console.log and console.pid live (default:
#                           the DB directory from TG_NEWSPAPER_DB, otherwise <data>/data)
#   TG_NEWSPAPER_NO_OPEN  — don't open the browser, just print the URL
#   TG_NEWSPAPER_DB       — path to the DB (passed through to the console as-is)
#   TG_NEWSPAPER_HOME     — user data directory (also enables release mode)

set -u

# Windows by default writes stdout to a file as cp1251 — Cyrillic/emoji in logs
# crash the process; harmless on macOS, but we keep it consistent with the Windows launcher.
export PYTHONUTF8=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${TG_NEWSPAPER_PORT:-8420}"
URL="http://localhost:${PORT}/"
# Availability checks go against 127.0.0.1: on Windows/some systems localhost first
# resolves to IPv6 (::1), while the console listens only on IPv4. We open $URL in the browser.
PROBE_URL="http://127.0.0.1:${PORT}/"

RELEASE=""
HOME_DIR=""
if [ -n "${TG_NEWSPAPER_HOME:-}" ]; then
    RELEASE=1
    HOME_DIR="$TG_NEWSPAPER_HOME"
    export TG_NEWSPAPER_HOME
    # Environment outside the bundle: its path can change (App Translocation), and the venv
    # must survive an app update.
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

# Dialog via argv, not by substituting into the AppleScript string: the log can contain
# quotes and backslashes that would break the script.
dialog() {
    # In tests/CI (NO_OPEN, CI) there's no one to close the dialog - it would hang forever,
    # so stderr only. For humans the dialog closes itself after 10 minutes.
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

# Repeat click on the icon: the console is already running — just open a tab.
if is_up; then
    open_url
    exit 0
fi

mkdir -p "$RUN_DIR"

# Finder-launched apps don't get PATH from .zshrc, so we also look for uv in the usual places.
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
    # Preparation is "actually needed" if there's no environment or no Chromium: in that case
    # we open the waiting page right away, instead of making the user stare at a blank screen.
    need_prepare=""
    BUILD_ID=""
    [ -f "$ROOT/BUILD_ID" ] && BUILD_ID="$(cat "$ROOT/BUILD_ID")"
    STAMP="$UV_PROJECT_ENVIRONMENT/.tg-newspaper-build"
    if [ ! -x "$PY" ]; then
        need_prepare=1
    elif [ -z "$BUILD_ID" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$BUILD_ID" ]; then
        # A different build (app update) or no BUILD_ID (dev build) — sync.
        need_prepare=1
    elif ! "$PY" -c 'import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)' >/dev/null 2>&1; then
        need_prepare=1
    fi

    if [ -z "$need_prepare" ]; then
        # Already prepared by this same build: uv isn't needed at all (works offline,
        # even if uv was removed), start the console right away.
        echo "preparation not needed" >"$SETUP_LOG"
    else
        : >"$SETUP_LOG"
        if [ -z "${TG_NEWSPAPER_NO_OPEN:-}" ]; then
            # Replace __PORT__ in the template with the port; the page will navigate to the console itself.
            sed "s/__PORT__/${PORT}/g" "$ROOT/scripts/preparing.html" >"$RUN_DIR/preparing.html"
            open "$RUN_DIR/preparing.html" && PREPARING_OPENED=1
        fi

        # Only to test the uv-install code path: pretend uv wasn't found.
        [ -n "${TG_NEWSPAPER_FORCE_UV_INSTALL:-}" ] && UV=""
        if [ -z "$UV" ]; then
            echo "== installing uv ==" >>"$SETUP_LOG"
            # Install uv into our own directory, without touching shell profiles (NO_MODIFY_PATH).
            if ! curl -LsSf https://astral.sh/uv/install.sh \
                | env UV_INSTALL_DIR="$HOME_DIR/uv" UV_NO_MODIFY_PATH=1 sh >>"$SETUP_LOG" 2>&1; then
                dialog "Не удалось установить uv (менеджер Python). Проверьте интернет и запустите TG Newspaper снова.

$(tail -n 15 "$SETUP_LOG")"
                exit 1
            fi
            UV="$HOME_DIR/uv/uv"
        fi

        cd "$ROOT" || exit 1
        # --no-editable: the bundle's path can change (App Translocation), an editable
        # .pth would point nowhere. --reinstall-package: after an app update, the venv
        # must end up with the new code even if the package version is unchanged.
        echo "== uv sync ($UV) ==" >>"$SETUP_LOG"
        if ! "$UV" sync --frozen --no-editable --reinstall-package tg-newspaper --quiet >>"$SETUP_LOG" 2>&1; then
            dialog "Не удалось установить зависимости (uv sync):

$(tail -n 15 "$SETUP_LOG")"
            exit 1
        fi
        # Idempotent: returns immediately if Chromium is already installed.
        echo "== playwright install chromium ==" >>"$SETUP_LOG"
        if ! "$PY" -m playwright install chromium >>"$SETUP_LOG" 2>&1; then
            dialog "Не удалось установить Chromium для вёрстки (playwright install):

$(tail -n 15 "$SETUP_LOG")"
            exit 1
        fi
        [ -n "$BUILD_ID" ] && echo "$BUILD_ID" >"$STAMP"
    fi
    cd "$ROOT" || exit 1

    # Directly via the venv's interpreter, not `uv run`: that would re-sync
    # the environment (and in editable mode by default).
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

    # The first launch (no .venv yet) takes a minute or two — warn with a notification.
    if [ ! -d "$ROOT/.venv" ]; then
        notify "Готовлю окружение… первый запуск займёт минуту-две"
    fi

    if ! "$UV" sync --quiet >"$SETUP_LOG" 2>&1; then
        dialog "Не удалось установить зависимости (uv sync):

$(tail -n 15 "$SETUP_LOG")"
        exit 1
    fi
    # Idempotent: returns immediately if Chromium is already installed.
    if ! "$UV" run playwright install chromium >"$SETUP_LOG" 2>&1; then
        dialog "Не удалось установить Chromium для вёрстки (playwright install):

$(tail -n 15 "$SETUP_LOG")"
        exit 1
    fi

    nohup "$UV" run python scripts/console.py --no-browser --port "$PORT" >>"$LOG" 2>&1 &
    echo $! >"$PID_FILE"
fi

# Wait up to ~90s for the port to come up (cold start on Windows-like systems and Defender scans).
for _ in $(seq 1 180); do
    if is_up; then
        # The waiting page redirects to the console on its own — no need for a second tab.
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
