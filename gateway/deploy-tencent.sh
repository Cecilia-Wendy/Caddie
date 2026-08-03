#!/usr/bin/env bash
set -euo pipefail

GATEWAY_DIR=/opt/caddie-gateway
GATEWAY_DATA_DIR=/opt/caddie-gateway-data
SECRET_FILE=/opt/caddie-gateway-secrets.env
CADDYFILE=/opt/Caddyfile
PUBLIC_HOST=43-128-7-135.sslip.io
RAW_BASE="${CADDIE_GATEWAY_SOURCE_BASE:-https://raw.githubusercontent.com/Cecilia-Wendy/Caddie/deploy/online-caddie/gateway}"
IMAGE_TAG=caddie-hosted-gateway:0.3.1

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

test -f "$SECRET_FILE" || {
  echo "Create $SECRET_FILE with DEEPSEEK_API_KEY before deploying." >&2
  exit 1
}
test -f "$CADDYFILE" || {
  echo "Existing Caddyfile was not found." >&2
  exit 1
}

mkdir -p "$GATEWAY_DIR" "$GATEWAY_DATA_DIR"
chmod 700 "$GATEWAY_DIR" "$GATEWAY_DATA_DIR"

for file_name in app.py admin_dashboard.html requirements.txt Dockerfile; do
  curl --fail --location --silent --show-error \
    "$RAW_BASE/$file_name" -o "$GATEWAY_DIR/$file_name"
done

chmod 600 "$SECRET_FILE"
set -a
# shellcheck disable=SC1090
. "$SECRET_FILE"
set +a
test -n "${DEEPSEEK_API_KEY:-}" || {
  echo "DEEPSEEK_API_KEY is missing from $SECRET_FILE." >&2
  exit 1
}

ENV_FILE="$GATEWAY_DIR/gateway.env"
TOKEN_FILE="$GATEWAY_DIR/tester-credentials.json"
ADMIN_FILE="$GATEWAY_DIR/admin-credential.txt"
if [ ! -f "$ADMIN_FILE" ]; then
  openssl rand -hex 32 > "$ADMIN_FILE"
  chmod 600 "$ADMIN_FILE"
fi
ADMIN_TOKEN="$(cat "$ADMIN_FILE")"
if [ ! -f "$TOKEN_FILE" ]; then
  TESTER_01_TOKEN="$(openssl rand -hex 24)"
  TESTER_02_TOKEN="$(openssl rand -hex 24)"
  printf '{"tester-01":"%s","tester-02":"%s"}\n' \
    "$TESTER_01_TOKEN" "$TESTER_02_TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
TESTER_01_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tester-01"])' "$TOKEN_FILE")"
TESTER_02_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tester-02"])' "$TOKEN_FILE")"
printf 'DEEPSEEK_API_KEY=%s\n' "$DEEPSEEK_API_KEY" > "$ENV_FILE"
printf 'CADDIE_GATEWAY_ADMIN_TOKEN=%s\n' "$ADMIN_TOKEN" >> "$ENV_FILE"
if [ -n "${CADDIE_WEBSITE_CALLBACK_URL:-}" ]; then
  printf 'CADDIE_WEBSITE_CALLBACK_URL=%s\n' "$CADDIE_WEBSITE_CALLBACK_URL" >> "$ENV_FILE"
fi
if [ -n "${CADDIE_GATEWAY_CALLBACK_TOKEN:-}" ]; then
  printf 'CADDIE_GATEWAY_CALLBACK_TOKEN=%s\n' "$CADDIE_GATEWAY_CALLBACK_TOKEN" >> "$ENV_FILE"
fi
if [ -n "${SUPABASE_URL:-}" ]; then
  printf 'SUPABASE_URL=%s\n' "$SUPABASE_URL" >> "$ENV_FILE"
fi
if [ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
  printf 'SUPABASE_SERVICE_ROLE_KEY=%s\n' "$SUPABASE_SERVICE_ROLE_KEY" >> "$ENV_FILE"
fi
printf 'CADDIE_GATEWAY_TESTERS_JSON={"tester-01":{"token":"%s","daily_calls":50,"daily_tokens":300000},"tester-02":{"token":"%s","daily_calls":50,"daily_tokens":300000}}\n' \
  "$TESTER_01_TOKEN" "$TESTER_02_TOKEN" >> "$ENV_FILE"
printf 'DEEPSEEK_MODEL=deepseek-v4-flash\nCADDIE_GATEWAY_DATA_DIR=/data\n' >> "$ENV_FILE"
chmod 600 "$ENV_FILE"
unset DEEPSEEK_API_KEY TESTER_01_TOKEN TESTER_02_TOKEN ADMIN_TOKEN

docker build --tag "$IMAGE_TAG" "$GATEWAY_DIR"
docker rm --force caddie-gateway >/dev/null 2>&1 || true
docker run --detach \
  --name caddie-gateway \
  --restart unless-stopped \
  --network caddie-net \
  --env-file "$ENV_FILE" \
  --volume "$GATEWAY_DATA_DIR:/data" \
  "$IMAGE_TAG" >/dev/null

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
python3 - "$CADDYFILE" "$PUBLIC_HOST" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
public_host = sys.argv[2]
site = f"""{public_host} {{
\tencode zstd gzip
\thandle_path /gateway/* {{
\t\treverse_proxy caddie-gateway:8080
\t}}
\thandle {{
\t\trespond 404
\t}}
}}
"""
path.write_text(site, encoding="utf-8")
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
echo "Model: deepseek-v4-flash"
echo "Admin dashboard: https://${PUBLIC_HOST}/gateway/admin"
echo "Tester credentials saved at: ${TOKEN_FILE}"
echo "Admin credential saved at: ${ADMIN_FILE}"
echo "Credentials and the DeepSeek API key were not printed."
