#!/bin/bash
# Builds the release dmg for macOS: scripts/build_release_mac.sh <version> <output_dir>
# Result — <output_dir>/TG-Newspaper-<version>-mac.dmg with "TG Newspaper.app"
# and a symlink to /Applications (for drag-and-drop). The app contains only
# the project sources; Python, dependencies and Chromium are installed on first run by
# scripts/launch.sh into the user's directory (see AGENTS.md). Not code-signed.
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

# Copy of the project — only files from the allow-listed paths that git knows about
# (tracked + new ones not in .gitignore): this way .env, *.session, data/,
# channels.yaml and other local files never end up in the bundle.
(
    cd "$ROOT"
    git ls-files -z --cached --others --exclude-standard -- pyproject.toml uv.lock README.md .python-version src \
        scripts/console.py scripts/launch.sh scripts/preparing.html \
        | tar --null -T - -cf -
) | tar -xf - -C "$RES/app"
chmod +x "$RES/app/scripts/launch.sh"

# Build identifier: the launcher uses it to decide whether the environment needs
# preparing (uv sync) — it changes after an app update, otherwise it starts without the network.
REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then REV="$REV-dirty"; fi
printf '%s' "$VERSION+$REV" >"$RES/app/BUILD_ID"

cp "$ROOT/packaging/icons/AppIcon.icns" "$RES/AppIcon.icns"

# Path to launch.sh — relative to itself: the app can be placed anywhere.
cat >"$APP/Contents/MacOS/$EXE" <<'SCRIPT'
#!/bin/sh
# User data stays outside the bundle: the app gets replaced on update.
# We don't overwrite a value set from outside — this is how the CI smoke test works with a temporary HOME.
: "${TG_NEWSPAPER_HOME:=$HOME/Library/Application Support/TG Newspaper}"
export TG_NEWSPAPER_HOME
export TG_NEWSPAPER_RELEASE=1
exec "$(dirname "$0")/../Resources/app/scripts/launch.sh"
SCRIPT
chmod +x "$APP/Contents/MacOS/$EXE"

# LSUIElement: no extra icon sits in the Dock — the script runs and exits,
# the console lives on as a separate background process.
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
