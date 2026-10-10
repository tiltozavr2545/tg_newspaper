#!/bin/bash
# Сборка релизного dmg для macOS: scripts/build_release_mac.sh <версия> <каталог_вывода>
# Результат — <каталог_вывода>/TG-Newspaper-<версия>-mac.dmg с "TG Newspaper.app"
# и ссылкой на /Applications (чтобы перетащить). Внутри приложения — только
# исходники проекта; Python, зависимости и Chromium при первом запуске ставит
# scripts/launch.sh в каталог пользователя (см. AGENTS.md). Без цифровой подписи.
set -eu

if [ $# -ne 2 ]; then
    echo "Использование: $0 <версия> <каталог_вывода>" >&2
    exit 2
fi
VERSION="$1"
OUT_DIR="$2"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXE="TG Newspaper"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

APP="$WORK/stage/$EXE.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/app" "$OUT_DIR"

# Копия проекта — только файлы из белого списка путей, которые знает git
# (отслеживаемые + новые не из .gitignore): так .env, *.session, data/,
# channels.yaml и прочее локальное в бандл не попадут никогда.
(
    cd "$ROOT"
    git ls-files -z --cached --others --exclude-standard -- pyproject.toml uv.lock README.md .python-version src \
        scripts/console.py scripts/launch.sh scripts/preparing.html \
        | tar --null -T - -cf -
) | tar -xf - -C "$RES/app"
chmod +x "$RES/app/scripts/launch.sh"

# Идентификатор сборки: лаунчер по нему решает, нужна ли подготовка окружения
# (uv sync) — после обновления приложения он меняется, иначе запуск без сети.
REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then REV="$REV-dirty"; fi
printf '%s' "$VERSION+$REV" >"$RES/app/BUILD_ID"

cp "$ROOT/packaging/icons/AppIcon.icns" "$RES/AppIcon.icns"

# Путь к launch.sh — относительно самого себя: приложение можно положить куда угодно.
cat >"$APP/Contents/MacOS/$EXE" <<'SCRIPT'
#!/bin/sh
# Данные пользователя вне бандла: приложение заменяется при обновлении.
# Заданное снаружи значение не затираем — так смоук-тест CI работает во временном HOME.
: "${TG_NEWSPAPER_HOME:=$HOME/Library/Application Support/TG Newspaper}"
export TG_NEWSPAPER_HOME
export TG_NEWSPAPER_RELEASE=1
exec "$(dirname "$0")/../Resources/app/scripts/launch.sh"
SCRIPT
chmod +x "$APP/Contents/MacOS/$EXE"

# LSUIElement: в Dock не висит лишняя иконка — скрипт отрабатывает и завершается,
# консоль живёт отдельным фоновым процессом.
cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>$EXE</string>
    <key>CFBundleIdentifier</key><string>local.tg-newspaper</string>
    <key>CFBundleName</key><string>TG Newspaper</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

ln -s /Applications "$WORK/stage/Applications"

DMG="$OUT_DIR/TG-Newspaper-$VERSION-mac.dmg"
rm -f "$DMG"
hdiutil create -volname "TG Newspaper" -srcfolder "$WORK/stage" -ov -format UDZO "$DMG" >/dev/null
echo "Готово: $DMG"
