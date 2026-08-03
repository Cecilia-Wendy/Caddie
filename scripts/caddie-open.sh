#!/bin/zsh

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_URL="http://127.0.0.1:8766"

mkdir -p "$HOME/.caddie/bin" "$HOME/.caddie/logs" "$HOME/Library/LaunchAgents"
ln -sfn "$APP_DIR" "$HOME/.caddie/app"
cp "$APP_DIR/scripts/caddie-server.sh" "$HOME/.caddie/bin/caddie-server"
chmod +x "$HOME/.caddie/bin/caddie-server"

if [ -f "$APP_DIR/scripts/com.caddie.server.plist" ]; then
  sed "s|__HOME__|$HOME|g" "$APP_DIR/scripts/com.caddie.server.plist" \
    > "$HOME/Library/LaunchAgents/com.caddie.server.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.caddie.server.plist" 2>/dev/null || true
  launchctl kickstart -k "gui/$(id -u)/com.caddie.server" 2>/dev/null || true
fi

for i in {1..20}; do
  if curl -fsS "$APP_URL" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 0.3
done

cd "$APP_DIR"
PYTHONDONTWRITEBYTECODE=1 nohup "$APP_DIR/.venv/bin/python" -m uvicorn server:app \
  --host 127.0.0.1 \
  --port 8766 \
  --loop asyncio \
  --http h11 \
  >> "$HOME/.caddie/logs/server.out.log" \
  2>> "$HOME/.caddie/logs/server.err.log" &

for i in {1..20}; do
  if curl -fsS "$APP_URL" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 0.3
done

exit 1
