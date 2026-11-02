#!/usr/bin/env bash
# Installs and starts the daily and overnight pilot timers for the current user.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ~/.config/systemd/user
cp autojob-scan.service autojob-scan.timer autojob-pilot.service autojob-pilot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now autojob-scan.timer
systemctl --user enable --now autojob-pilot.timer
systemctl --user list-timers autojob-scan.timer autojob-pilot.timer --no-pager
loginctl show-user "$USER" -p Linger
echo "Linger must be yes for the timer to fire without a login session."
