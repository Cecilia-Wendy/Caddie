#!/bin/zsh

set -eu

PROJECT_DIR="${CADDIE_PROJECT_DIR:-$HOME/.caddie/app}"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/caddie_mcp.py"
