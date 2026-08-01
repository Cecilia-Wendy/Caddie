#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"
TEST_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/caddie-tests.XXXXXX")"

cleanup() {
  rm -rf "$TEST_DATA_DIR"
}
trap cleanup EXIT

if [ ! -x "$PYTHON" ]; then
  echo "Missing .venv. Create it and install requirements first."
  exit 1
fi

export CADDIE_DATA_DIR="$TEST_DATA_DIR/data"
export PYTHONPATH="$APP_DIR"
export PYTHONPYCACHEPREFIX="$TEST_DATA_DIR/pycache"

cd "$APP_DIR"
for test_file in tests/test_*.py; do
  echo "RUN $test_file"
  "$PYTHON" "$test_file"
done

echo "ALL_TESTS_OK data_dir=$CADDIE_DATA_DIR"
