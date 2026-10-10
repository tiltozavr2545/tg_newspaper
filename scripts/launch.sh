#!/bin/bash
# Запуск консоли "одной кнопкой": поднимает scripts/console.py в фоне без окна
# Терминала и открывает её в браузере. Это единственное место с логикой запуска
# — приложение "TG Newspaper.app" (scripts/make_app.sh) лишь вызывает этот скрипт.
#
# Переменные окружения (нужны для проверки, в обычной работе не задаются):
#   TG_NEWSPAPER_PORT     — порт консоли (по умолчанию 8420)
#   TG_NEWSPAPER_RUN_DIR  — где лежат console.log и console.pid (по умолчанию
#                           каталог БД из TG_NEWSPAPER_DB, иначе <репозиторий>/data)
#   TG_NEWSPAPER_NO_OPEN  — не открывать браузер, только напечатать URL
#   TG_NEWSPAPER_DB       — путь к БД (пробрасывается в консоль как есть)

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${TG_NEWSPAPER_PORT:-8420}"
URL="http://localhost:${PORT}/"

if [ -n "${TG_NEWSPAPER_RUN_DIR:-}" ]; then
    RUN_DIR="$TG_NEWSPAPER_RUN_DIR"
elif [ -n "${TG_NEWSPAPER_DB:-}" ]; then
    RUN_DIR="$(dirname "$TG_NEWSPAPER_DB")"
else
    RUN_DIR="$ROOT/data"
fi
export TG_NEWSPAPER_RUN_DIR="$RUN_DIR"
LOG="$RUN_DIR/console.log"
PID_FILE="$RUN_DIR/console.pid"

# Диалог через argv, а не подстановкой в строку AppleScript: в логе бывают
# кавычки и обратные слэши, они бы сломали скрипт.
dialog() {
    osascript -e 'on run argv' \
        -e 'display dialog (item 1 of argv) with title "TG Newspaper" buttons {"OK"} default button "OK" with icon caution' \
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
    curl -s -o /dev/null "$URL"
}

# Повторный клик по иконке: консоль уже работает — просто открываем вкладку.
if is_up; then
    open_url
    exit 0
fi

# Finder-приложения не получают PATH из .zshrc, поэтому uv ищем и по обычным местам.
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            break
        fi
    done
fi
if [ -z "$UV" ]; then
    dialog "Не найден uv — менеджер Python-окружения, без него консоль не запустить.

Установите его в Терминале командой:
curl -LsSf https://astral.sh/uv/install.sh | sh

и запустите TG Newspaper снова."
    exit 1
fi

mkdir -p "$RUN_DIR"
cd "$ROOT" || exit 1

# Первый запуск (нет .venv) занимает минуту-две — предупреждаем уведомлением.
if [ ! -d "$ROOT/.venv" ]; then
    notify "Готовлю окружение… первый запуск займёт минуту-две"
fi

SETUP_LOG="$RUN_DIR/setup.log"
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

# Ждём готовности порта до ~30 с.
for _ in $(seq 1 60); do
    if is_up; then
        open_url
        exit 0
    fi
    sleep 0.5
done

dialog "Консоль не поднялась за 30 секунд. Последние строки лога ($LOG):

$(tail -n 15 "$LOG")"
rm -f "$PID_FILE"
exit 1
