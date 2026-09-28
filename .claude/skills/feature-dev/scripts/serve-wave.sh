#!/usr/bin/env bash
# serve-wave.sh <worktree> <name> [checklist-json-file]
#
# Put an integrated wave worktree on the phone. The client's tunnel target is
# fixed at 127.0.0.1:8020, so this binds the wave's own backend there,
# replacing whatever backend is currently running. Companion: restore-main.sh
# puts main's backend back on 8020 afterwards.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SERVE_DIR="$REPO/.claude/skills/serve-tunnel/scripts"
TMP_DIR="${TMPDIR:-/tmp}"

WORKTREE="${1:?usage: serve-wave.sh <worktree> <name> [checklist-json-file]}"
NAME="${2:?usage: serve-wave.sh <worktree> <name> [checklist-json-file]}"
CHECKLIST_FILE="${3:-}"

slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"

[ -d "$WORKTREE" ] || { echo "serve-wave: worktree not found: $WORKTREE" >&2; exit 1; }
if [ -n "$CHECKLIST_FILE" ]; then
  [ -f "$CHECKLIST_FILE" ] || { echo "serve-wave: checklist file not found: $CHECKLIST_FILE" >&2; exit 1; }
fi

# --- 0. slot check, before anything is killed ---------------------------------
# One tunnel slot exists. If another slug holds it, stop here and leave its
# backend and tmux session alone; the caller decides whether to stop it
# (stop-tunnel.js <slug>). A dead entry for another slug is only a warning.
LIST="$(node "$SERVE_DIR/list.js")"
BLOCKER="$(printf '%s\n' "$LIST" | awk -v slug="$SLUG" \
  '$1 != slug && ($2 == "starting" || $2 == "ready") && $0 ~ /\(up\)/')"
if [ -n "$BLOCKER" ]; then
  echo "serve-wave: tunnel slot is taken, not touching port 8020 or tmux:" >&2
  printf '%s\n' "$BLOCKER" | awk '{ printf "  slug %s  status %s  port %s  up %s\n", $1, $2, $4, $9 }' >&2
  echo "serve-wave: stop it first with: node $SERVE_DIR/stop-tunnel.js <slug>" >&2
  exit 2
fi
STALE="$(printf '%s\n' "$LIST" | awk -v slug="$SLUG" \
  '$1 != slug && ($2 == "starting" || $2 == "ready") && $0 ~ /\(dead\)/ { print $1 }')"
if [ -n "$STALE" ]; then
  echo "serve-wave: stale tunnel entries (process gone), clean up with stop-tunnel.js: $(echo "$STALE" | tr '\n' ' ')" >&2
fi

# --- 1. free port 8020 and any stale sessions --------------------------------
fuser -k 8020/tcp >/dev/null 2>&1 || true
sleep 1
tmux kill-session -t shadow-backend >/dev/null 2>&1 || true
tmux kill-session -t "$SLUG" >/dev/null 2>&1 || true

# --- 2. dependencies -----------------------------------------------------
if [ ! -e "$WORKTREE/backend/.venv" ]; then
  ln -s "$REPO/backend/.venv" "$WORKTREE/backend/.venv"
fi
if [ ! -d "$WORKTREE/node_modules" ] || [ "$WORKTREE/package.json" -nt "$WORKTREE/node_modules" ]; then
  (cd "$WORKTREE" && npm install)
fi

# --- 3. tmux session with two windows ----------------------------------------
# Launcher scripts avoid nested-quoting mistakes in the tmux command strings.
BACKEND_LAUNCHER="$(mktemp "$TMP_DIR/serve-wave-${SLUG}-backend.XXXXXX.sh")"
cat > "$BACKEND_LAUNCHER" <<EOF
#!/usr/bin/env bash
cd "$WORKTREE/backend"
./run.sh
echo "[backend exited]"
exec bash
EOF
chmod +x "$BACKEND_LAUNCHER"

METRO_LAUNCHER="$(mktemp "$TMP_DIR/serve-wave-${SLUG}-metro.XXXXXX.sh")"
cat > "$METRO_LAUNCHER" <<EOF
#!/usr/bin/env bash
bash "$SERVE_DIR/serve.sh" "$WORKTREE" "$NAME"
LOG=\$(ls -t /tmp/serve-tunnel-${SLUG}.*.log 2>/dev/null | head -n1)
if [ -n "\$LOG" ]; then
  exec tail -f "\$LOG"
else
  exec bash
fi
EOF
chmod +x "$METRO_LAUNCHER"

tmux new-session -d -s "$SLUG" -n backend "$BACKEND_LAUNCHER"
tmux new-window -t "$SLUG" -n metro "$METRO_LAUNCHER"

# --- 4. wait for ready --------------------------------------------------
READY=0
for i in $(seq 1 180); do
  if node "$SERVE_DIR/list.js" | grep -qE "^${SLUG}[[:space:]].*ready" \
    && curl -s -m 2 http://127.0.0.1:8020/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "serve-wave: not ready within 180s (tmux session: $SLUG)" >&2
  exit 3
fi

# --- 5. checklist --------------------------------------------------------
if [ -n "$CHECKLIST_FILE" ]; then
  node "$SERVE_DIR/write-checklist.js" --slug "$SLUG" < "$CHECKLIST_FILE"
fi

# --- 6. report -------------------------------------------------------------
node "$SERVE_DIR/list.js" | grep -E "^${SLUG}[[:space:]]" || true
echo "backend cwd: $WORKTREE/backend"
echo "tmux attach -t $SLUG"
