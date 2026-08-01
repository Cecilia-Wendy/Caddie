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

APP_NAME="Caddie Hosted Alpha"
RELEASE_DIR="$APP_DIR/dist/$APP_NAME"
RELEASE_ZIP="$APP_DIR/dist/Caddie-Hosted-Alpha-$VERSION-macos-arm64.zip"
STAGE_DIR="$(mktemp -d /tmp/caddie-release.XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$STAGE_DIR/$APP_NAME"
/usr/bin/ditto --noextattr --noqtn "$APP_DIR/dist/$APP_NAME.app" "$STAGE_DIR/$APP_NAME.app"
xattr -cr "$STAGE_DIR/$APP_NAME.app"
codesign --force --deep --sign - "$STAGE_DIR/$APP_NAME.app"
codesign --verify --deep --strict "$STAGE_DIR/$APP_NAME.app"

# Replace PyInstaller's original bundle with the verified, clean copy. Desktop
# and File Provider folders can attach FinderInfo after PyInstaller signs it.
rm -rf "$APP_DIR/dist/$APP_NAME.app"
/usr/bin/ditto --noextattr --noqtn "$STAGE_DIR/$APP_NAME.app" "$APP_DIR/dist/$APP_NAME.app"
xattr -d com.apple.FinderInfo "$APP_DIR/dist/$APP_NAME.app" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "$APP_DIR/dist/$APP_NAME.app" 2>/dev/null || true
codesign --verify --deep --strict "$APP_DIR/dist/$APP_NAME.app"

/usr/bin/ditto --noextattr --noqtn "$STAGE_DIR/$APP_NAME.app" "$STAGE_DIR/$APP_NAME/$APP_NAME.app"
cp "$APP_DIR/scripts/open-caddie-hosted-alpha.command" "$STAGE_DIR/$APP_NAME/Open Caddie Hosted Alpha.command"
cp "$APP_DIR/docs/首次打开-Caddie.txt" "$STAGE_DIR/$APP_NAME/首次打开-Caddie.txt"
chmod +x "$STAGE_DIR/$APP_NAME/Open Caddie Hosted Alpha.command"
rm -f "$RELEASE_ZIP"
/usr/bin/ditto -c -k --keepParent "$STAGE_DIR/$APP_NAME" "$STAGE_DIR/$(basename "$RELEASE_ZIP")"
cp "$STAGE_DIR/$(basename "$RELEASE_ZIP")" "$RELEASE_ZIP"

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
/usr/bin/ditto --noextattr --noqtn "$STAGE_DIR/$APP_NAME.app" "$RELEASE_DIR/$APP_NAME.app"
cp "$STAGE_DIR/$APP_NAME/Open Caddie Hosted Alpha.command" "$RELEASE_DIR/Open Caddie Hosted Alpha.command"
cp "$STAGE_DIR/$APP_NAME/首次打开-Caddie.txt" "$RELEASE_DIR/首次打开-Caddie.txt"

echo ""
echo "Built: $APP_DIR/dist/$APP_NAME.app"
echo "Share: $RELEASE_ZIP"
echo "Data will remain in: $HOME/.caddie-hosted-alpha"
