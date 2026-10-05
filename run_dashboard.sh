#!/usr/bin/env bash
#
# run_dashboard.sh — set up (once) and launch the LAN dashboard.
#
# Usage:
#   1. Set DASHBOARD_TOKEN in .env (without it the dashboard is read-only).
#   2. ./run_dashboard.sh, then open http://<this-machine>:8080
#
# Runs alongside run.sh, not instead of it: the dashboard only reads the
# bot's files, so stopping it never stops alerts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
ENV_FILE=".env"

# --- venv setup ---
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

REQ_HASH_FILE="$VENV_DIR/.requirements.hash"
CURRENT_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE")" != "$CURRENT_HASH" ]; then
    echo "Installing/updating dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

# --- load .env if present ---
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "No .env file found. Copy .env.example to .env and fill it in first."
    exit 1
fi

if [ -z "${SIGNAL_TICKER:-}" ]; then
    echo "SIGNAL_TICKER is not set. Add it to .env so the dashboard finds the bot's files."
    exit 1
fi

if [ -z "${DASHBOARD_TOKEN:-}" ]; then
    echo "DASHBOARD_TOKEN is not set: starting read-only, all actions will be refused."
fi

exec python -m dashboard
