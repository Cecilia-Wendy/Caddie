#!/bin/zsh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$SCRIPT_DIR/Caddie Hosted Alpha.app"

if [ ! -d "$APP_PATH" ]; then
  echo "Caddie Hosted Alpha.app must stay beside this launcher."
  echo "请把启动器和 Caddie Hosted Alpha.app 放在同一文件夹。"
  read -r "?Press Enter to close..."
  exit 1
fi

/usr/bin/xattr -dr com.apple.quarantine "$APP_PATH"
/usr/bin/open "$APP_PATH"

echo "Caddie Hosted Alpha is opening."
