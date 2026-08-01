#!/usr/bin/env bash
set -euo pipefail

GATEWAY_DIR=/opt/caddie-gateway
GATEWAY_DATA_DIR=/opt/caddie-gateway-data
EXISTING_CONFIG=/opt/caddie-data/config.json
CADDYFILE=/opt/Caddyfile
PUBLIC_HOST=43-128-7-135.sslip.io
RAW_BASE=https://raw.githubusercontent.com/Cecilia-Wendy/Caddie/alpha/hosted-ai-beta

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo." >&2
  exit 1
fi

for command_name in docker curl python3 openssl; do
  command -v "$command_name" >/dev/null || {
    echo "Missing required command: $command_name" >&2
    exit 1
  }
done

test -f "$EXISTING_CONFIG" || {
  echo "Existing Caddie AI configuration was not found." >&2
  exit 1
}
test -f "$CADDYFILE" || {
  echo "Existing Caddyfile was not found." >&2
  exit 1
}

mkdir -p "$GATEWAY_DIR" "$GATEWAY_DATA_DIR"
chmod 700 "$GATEWAY_DIR" "$GATEWAY_DATA_DIR"

for file_name in app.py requirements.txt Dockerfile; do
  curl --fail --location --silent --show-error \
    "$RAW_BASE/$file_name" -o "$GATEWAY_DIR/$file_name"
done

API_KEY="$(python3 - "$EXISTING_CONFIG" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
providers = config.get("providers") or []
active_id = config.get("active_provider_id")
candidates = sorted(
    providers,
    key=lambda p: (
        p.get("id") != active_id,
        "deepseek" not in " ".join(str(p.get(k) or "") for k in ("name", "base_url", "model")).lower(),
    ),
)
key = next((str(p.get("api_key") or "").strip() for p in candidates if p.get("api_key")), "")
if not key:
    raise SystemExit("No configured cloud AI key was found in the existing Caddie configuration.")
print(key, end="")
PY
)"

ENV_FILE="$GATEWAY_DIR/gateway.env"
if [ -f "$ENV_FILE" ]; then
  TESTER_TOKEN="$(sed -n 's/.*"token":"\([^"]*\)".*/\1/p' "$ENV_FILE" | head -1)"
fi
TESTER_TOKEN="${TESTER_TOKEN:-$(openssl rand -hex 24)}"
printf 'DEEPSEEK_API_KEY=%s\n' "$API_KEY" > "$ENV_FILE"
printf 'CADDIE_GATEWAY_TESTERS_JSON={"tester-01":{"token":"%s","daily_calls":50,"daily_tokens":300000}}\n' "$TESTER_TOKEN" >> "$ENV_FILE"
printf 'DEEPSEEK_MODEL=deepseek-chat\nCADDIE_GATEWAY_DATA_DIR=/data\n' >> "$ENV_FILE"
chmod 600 "$ENV_FILE"
unset API_KEY

docker build --tag caddie-hosted-gateway:0.2.0 "$GATEWAY_DIR"
docker rm --force caddie-gateway >/dev/null 2>&1 || true
docker run --detach \
  --name caddie-gateway \
  --restart unless-stopped \
  --network caddie-net \
  --env-file "$ENV_FILE" \
  --volume "$GATEWAY_DATA_DIR:/data" \
  caddie-hosted-gateway:0.2.0 >/dev/null

sleep 1
docker exec caddie-gateway python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5) as response:
    value = json.load(response)
if not value.get("ok"):
    raise SystemExit("Gateway health check failed")
PY

BACKUP_PATH="${CADDYFILE}.backup-$(date +%Y%m%d-%H%M%S)"
cp "$CADDYFILE" "$BACKUP_PATH"
python3 - "$CADDYFILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
if "handle_path /gateway/*" not in text:
    needle = "\treverse_proxy caddie:8766"
    replacement = """\thandle_path /gateway/* {
\t\treverse_proxy caddie-gateway:8080
\t}

\thandle {
\t\treverse_proxy caddie:8766
\t}"""
    if needle not in text:
        raise SystemExit("Expected Caddy reverse_proxy line was not found; no changes were made.")
    text = text.replace(needle, replacement, 1)
    path.write_text(text, encoding="utf-8")
PY

if ! docker exec caddy caddy validate --config /etc/caddy/Caddyfile; then
  cp "$BACKUP_PATH" "$CADDYFILE"
  echo "Caddy validation failed; original configuration was restored." >&2
  exit 1
fi
docker exec caddy caddy reload --config /etc/caddy/Caddyfile

echo
echo "CADDIE_GATEWAY_DEPLOYED"
echo "Base URL: https://${PUBLIC_HOST}/gateway/v1"
echo "Model: deepseek-chat"
echo "Tester 01 token: ${TESTER_TOKEN}"
echo "Keep this token private. The DeepSeek API key was not printed."
