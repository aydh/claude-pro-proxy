#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
SKIP_INSTALL="${SKIP_INSTALL:-0}"

# Load .env so HOST/PORT and other vars are available to this script.
if [ -f .env ]; then
    # shellcheck disable=SC2046
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [ "$SKIP_INSTALL" != "1" ]; then
    pip install --upgrade pip >/dev/null
    pip install -r requirements.txt
fi

if [ ! -f .env ] && [ -z "${API_KEY:-}" ]; then
    echo "No .env file and no API_KEY env var set."
    echo "Run: python3 generate_api_key.py"
    echo "Or:  cp .env.example .env && \$EDITOR .env"
    exit 1
fi

LOG_FILE="${LOG_FILE:-proxy.log}"

nohup uvicorn claude_proxy:app --host "$HOST" --port "$PORT" --reload "$@" \
    >> "$LOG_FILE" 2>&1 &
PID=$!
echo "Proxy started (PID $PID) on $HOST:$PORT — logs: $LOG_FILE"
echo "$PID" > proxy.pid
