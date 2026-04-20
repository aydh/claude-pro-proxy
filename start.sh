#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install --upgrade pip >/dev/null
pip install -r requirements.txt

exec uvicorn claude_proxy:app --host "$HOST" --port "$PORT" "$@"
