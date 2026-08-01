#!/bin/zsh

set -eu

PROJECT_DIR="$HOME/.caddie/app"
LOG_DIR="$HOME/.caddie/logs"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

export PYTHONDONTWRITEBYTECODE=1
exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn server:app \
  --host 127.0.0.1 \
  --port 8766 \
  --log-level warning
