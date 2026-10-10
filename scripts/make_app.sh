#!/bin/bash
# Создаёт приложение "TG Newspaper.app" — иконку для запуска консоли одним
# кликом (Dock/Launchpad/Finder). Использование: scripts/make_app.sh [каталог],
# по умолчанию ~/Applications. Бандл минимальный, собран руками: внутри лишь
# sh-скрипт, который вызывает scripts/launch.sh этого репозитория (путь вписан
# при генерации — после переноса репозитория запустите make_app.sh заново).
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP="$DEST_DIR/TG Newspaper.app"
EXE="TG Newspaper"

mkdir -p "$DEST_DIR"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat >"$APP/Contents/MacOS/$EXE" <<SCRIPT
#!/bin/sh
exec "$ROOT/scripts/launch.sh"
SCRIPT
chmod +x "$APP/Contents/MacOS/$EXE"

# Иконка: PNG 1024x1024 рисуем в Chromium из HTML (Playwright уже в зависимостях),
# затем sips + iconutil (есть в macOS) собирают .icns. Не вышло — без иконки.
make_icon() {
    local uv work
    uv="$(command -v uv || true)"
    [ -z "$uv" ] && [ -x "$HOME/.local/bin/uv" ] && uv="$HOME/.local/bin/uv"
    [ -z "$uv" ] && return 1
    work="$(mktemp -d)"
    mkdir "$work/icon.iconset"
    (cd "$ROOT" && "$uv" run python - "$work/icon.png" <<'PY'
import sys
from playwright.sync_api import sync_playwright

HTML = """<html><body style="margin:0;background:transparent">
<div style="width:824px;height:824px;margin:100px;border-radius:185px;background:#f3ecd9;
 box-shadow:0 12px 30px rgba(0,0,0,.35);border:14px solid #1a1a1a;box-sizing:border-box;
 display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a">
 <div style="font-size:300px;font-weight:bold;line-height:1">TG</div>
 <div style="width:560px;height:16px;background:#1a1a1a;margin:24px 0 18px"></div>
 <div style="width:560px;height:6px;background:#1a1a1a;margin-bottom:16px"></div>
 <div style="font-size:92px;letter-spacing:6px;font-weight:bold">NEWSPAPER</div>
</div></body></html>"""

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1024, "height": 1024})
    page.set_content(HTML)
    page.screenshot(path=sys.argv[1], omit_background=True)
    b.close()
PY
    ) || return 1
    local s
    for s in 16 32 128 256 512; do
        sips -z $s $s "$work/icon.png" --out "$work/icon.iconset/icon_${s}x${s}.png" >/dev/null || return 1
        sips -z $((s * 2)) $((s * 2)) "$work/icon.png" --out "$work/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null || return 1
    done
    iconutil -c icns "$work/icon.iconset" -o "$APP/Contents/Resources/AppIcon.icns" || return 1
    rm -rf "$work"
}

ICON_KEY=""
if make_icon; then
    ICON_KEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
else
    echo "Иконку собрать не удалось — приложение создано без неё." >&2
fi

# LSUIElement: после запуска в Dock не висит лишняя иконка — скрипт отрабатывает
# и завершается, консоль живёт отдельным фоновым процессом.
cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>$EXE</string>
    <key>CFBundleIdentifier</key><string>local.tg-newspaper</string>
    <key>CFBundleName</key><string>TG Newspaper</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSUIElement</key><true/>
    $ICON_KEY
</dict>
</plist>
PLIST

touch "$APP"
echo "Готово: $APP"
