#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Missing .venv. Create it and install requirements first."
  exit 1
fi

cd "$APP_DIR"
VERSION="$("$PYTHON" -c 'from app_version import APP_VERSION; print(APP_VERSION)')"

export PYINSTALLER_CONFIG_DIR="$APP_DIR/build/pyinstaller-config"

"$PYTHON" scripts/create_app_icon.py
"$PYTHON" -m PyInstaller --noconfirm --clean caddie-alpha.spec

RELEASE_DIR="$APP_DIR/dist/Caddie Alpha"
RELEASE_ZIP="$APP_DIR/dist/Caddie-$VERSION-macos-arm64.zip"
STAGE_DIR="$(mktemp -d /tmp/caddie-release.XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$STAGE_DIR/Caddie Alpha"
/usr/bin/ditto --noextattr --noqtn "$APP_DIR/dist/Caddie.app" "$STAGE_DIR/Caddie.app"
xattr -cr "$STAGE_DIR/Caddie.app"
codesign --force --deep --sign - "$STAGE_DIR/Caddie.app"
codesign --verify --deep --strict "$STAGE_DIR/Caddie.app"
/usr/bin/ditto --noextattr --noqtn "$STAGE_DIR/Caddie.app" "$STAGE_DIR/Caddie Alpha/Caddie.app"
cp "$APP_DIR/scripts/open-caddie-alpha.command" "$STAGE_DIR/Caddie Alpha/Open Caddie.command"
cp "$APP_DIR/docs/首次打开-Caddie.txt" "$STAGE_DIR/Caddie Alpha/首次打开-Caddie.txt"
chmod +x "$STAGE_DIR/Caddie Alpha/Open Caddie.command"
rm -f "$RELEASE_ZIP"
/usr/bin/ditto -c -k --keepParent "$STAGE_DIR/Caddie Alpha" "$STAGE_DIR/$(basename "$RELEASE_ZIP")"
cp "$STAGE_DIR/$(basename "$RELEASE_ZIP")" "$RELEASE_ZIP"

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
/usr/bin/ditto --noextattr --noqtn "$STAGE_DIR/Caddie.app" "$RELEASE_DIR/Caddie.app"
cp "$STAGE_DIR/Caddie Alpha/Open Caddie.command" "$RELEASE_DIR/Open Caddie.command"
cp "$STAGE_DIR/Caddie Alpha/首次打开-Caddie.txt" "$RELEASE_DIR/首次打开-Caddie.txt"

echo ""
echo "Built: $APP_DIR/dist/Caddie.app"
echo "Share: $RELEASE_ZIP"
echo "Data will remain in: $HOME/.caddie"
