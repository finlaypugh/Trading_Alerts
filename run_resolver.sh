#!/usr/bin/env bash
#
# run_resolver.sh — resolve the bot's alert outcomes once, then exit.
#
# Usage:
#   ./run_resolver.sh             # ingest + resolve
#   ./run_resolver.sh --dry-run   # report what would change, write nothing
#
# On the Pi, deploy/resolve-alerts.timer runs this every 15 minutes. Elsewhere
# (macOS, a Linux box without systemd) schedule it with cron, e.g.
#   */15 * * * * /path/to/Trading_Alerts/run_resolver.sh >> resolver.log 2>&1
#
# Uses the venv run.sh created; it never installs anything itself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "No .venv found. Run ./run.sh once first to set it up."
    exit 1
fi
# shellcheck disable=SC1091
source ".venv/bin/activate"

if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
else
    echo "No .env file found. Copy .env.example to .env and fill it in first."
    exit 1
fi

exec python resolve_alerts.py "$@"
