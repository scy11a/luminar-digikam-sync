#!/bin/bash
# Builds LuminarSync.app with the Xcode command-line tools (no external dependencies).
# Result: gui/build/LuminarSync.app
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
BIN="$(swift build -c release --show-bin-path)/LuminarSync"

APP="build/LuminarSync.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/LuminarSync"
cp Info.plist "$APP/Contents/Info.plist"
cp ../luminar_sync.py "$APP/Contents/Resources/luminar_sync.py"

# ad-hoc signature; because of the new signature macOS may ask for folder access again after each build
codesign --force --sign - "$APP"
echo "Done: $(pwd)/$APP"
