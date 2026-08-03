#!/bin/zsh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$SCRIPT_DIR/Caddie.app"

if [ ! -d "$APP_PATH" ]; then
  echo "Caddie.app must stay in the same folder as this launcher."
  echo "请把启动器和 Caddie.app 放在同一文件夹后重试。"
  read -r "?Press Enter to close..."
  exit 1
fi

/usr/bin/xattr -dr com.apple.quarantine "$APP_PATH"
/usr/bin/open "$APP_PATH"

echo "Caddie is opening. You can close this window."
echo "Caddie 正在打开，可以关闭这个窗口。"
