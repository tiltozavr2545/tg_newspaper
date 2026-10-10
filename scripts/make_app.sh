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

# Иконка — готовый ассет из репозитория (рисуется один раз scripts/make_icons.py).
ICON_KEY=""
if [ -f "$ROOT/packaging/icons/AppIcon.icns" ]; then
    cp "$ROOT/packaging/icons/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
    ICON_KEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
else
    echo "packaging/icons/AppIcon.icns не найден — приложение создано без иконки." >&2
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
