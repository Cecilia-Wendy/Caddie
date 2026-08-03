#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEMO_DATA_DIR="${CADDIE_DEMO_DATA_DIR:-$APP_DIR/.demo-data}"
DEMO_PORT="${CADDIE_DEMO_PORT:-8877}"

cd "$APP_DIR"

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "缺少 .venv，请先在 Caddie 目录安装依赖。"
  exit 1
fi

"$APP_DIR/.venv/bin/python" "$SCRIPT_DIR/create_demo_workspace.py" \
  --data-dir "$DEMO_DATA_DIR"

echo "启动 Caddie 虚拟测试版：http://127.0.0.1:$DEMO_PORT"
echo "数据目录：$DEMO_DATA_DIR"
echo "提示：所有求职者、公司、项目、数字和面试记录均为虚构。"

export CADDIE_DATA_DIR="$DEMO_DATA_DIR"
export CADDIE_PORT="$DEMO_PORT"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py"
