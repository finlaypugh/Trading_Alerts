#!/usr/bin/env bash
#
# deploy/install.sh — install the signal-bot and dashboard systemd units and
# the dashboard's sudoers rule for THIS checkout and THIS user.
#
# The files in deploy/ are templates written for user `pi` at
# /home/pi/Trading_Alerts; this fills in the real user and path. Run it as
# the user the services should run as (it calls sudo itself), not with sudo.
#
#   ./deploy/install.sh
#   sudo systemctl enable --now signal-bot dashboard
#
# Re-running is safe: it overwrites the installed copies.

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as the user the services should run as, not as root: ./deploy/install.sh"
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="$(id -un)"

render() {
    sed -e "s#/home/pi/Trading_Alerts#$REPO#g" \
        -e "s#^User=pi\$#User=$RUN_AS#" \
        -e "s#^pi #$RUN_AS #" \
        "$1"
}

for unit in signal-bot dashboard; do
    render "$REPO/deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done

# Validate the sudoers rule before it goes live: a broken file in
# /etc/sudoers.d can lock sudo out entirely.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
render "$REPO/deploy/sudoers-signal-bot" > "$tmp"
sudo visudo -cf "$tmp"
sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/signal-bot

# Lets the dashboard read the bot's journal for its log panel.
sudo usermod -aG systemd-journal "$RUN_AS"

sudo systemctl daemon-reload
echo "Installed for $RUN_AS at $REPO."
echo "Start with: sudo systemctl enable --now signal-bot dashboard"
echo "(already running? sudo systemctl restart signal-bot dashboard)"
