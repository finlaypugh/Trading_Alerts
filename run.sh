#!/usr/bin/env bash
#
# run.sh — set up (once) and launch the signal bot for the configured ticker.
#
# Usage:
#   1. Copy .env.example to .env and fill in DISCORD_WEBHOOK_URL (and any
#      overrides you want, e.g. SIGNAL_INTERVAL).
#   2. ./run.sh
#
# Re-running this script is safe: it reuses the existing venv and only
# reinstalls deps if requirements.txt changed.

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

if [ -z "${DISCORD_WEBHOOK_URL:-}" ]; then
    echo "DISCORD_WEBHOOK_URL is not set. Add it to .env before running."
    exit 1
fi

# No default. A silent fallback would run a strategy tuned for one instrument
# against whatever the fallback happens to be.
if [ -z "${SIGNAL_TICKER:-}" ]; then
    echo "SIGNAL_TICKER is not set. Add it to .env before running (e.g. GC=F)."
    exit 1
fi

echo "Starting signal bot for ${SIGNAL_TICKER}..."
exec python signal_bot.py