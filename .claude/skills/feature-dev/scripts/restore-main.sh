#!/usr/bin/env bash
# restore-main.sh <wave-name>
#
# Tear down a wave's serving session and put main's own backend back on 8020,
# supervised in the tmux session shadow-backend. Companion to serve-wave.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SERVE_DIR="$REPO/.claude/skills/serve-tunnel/scripts"

NAME="${1:?usage: restore-main.sh <wave-name>}"
slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"

node "$SERVE_DIR/stop-tunnel.js" "$SLUG" || true
tmux kill-session -t "$SLUG" >/dev/null 2>&1 || true

fuser -k 8020/tcp >/dev/null 2>&1 || true
sleep 1
tmux kill-session -t shadow-backend >/dev/null 2>&1 || true

tmux new-session -d -s shadow-backend -c "$REPO/backend" \
  "./run.sh; echo '[backend exited]'; exec bash"

for i in $(seq 1 30); do
  if curl -s -m 2 http://127.0.0.1:8020/health >/dev/null 2>&1; then
    echo "restore-main: backend healthy on :8020 (tmux: shadow-backend)"
    exit 0
  fi
  sleep 1
done

echo "restore-main: backend did not answer /health within 30s (tmux: shadow-backend)" >&2
exit 1
