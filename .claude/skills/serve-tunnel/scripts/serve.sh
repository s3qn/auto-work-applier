#!/usr/bin/env bash
# serve.sh <worktree-path> <feature-name>
# Launch the app's dev server exposed over an ngrok v3 tunnel, capture its
# public URL, and register it in the shared state file so the dashboard renders
# a QR. Serve ONE tunnel at a time (ngrok free tier = one usable domain).
#
# Prints the scan URL, the dashboard link, and an ASCII QR fallback.
# Exits 0 on a ready tunnel, non-zero on cap-exceeded / port-exhausted / failure.
#
# HOW THE DEV SERVER IS STARTED
#   This script is stack-agnostic. It sources $REPO/.claude/serve-config.sh,
#   which defines DEV_START_CMD, DEV_PROXY_ENV, DEV_READY_RE and DEV_SCAN_SCHEME
#   for whatever stack the app uses. Edit that file, not this one.
#
# WHY A STANDALONE NGROK v3 AGENT, NOT `expo start --tunnel`?
#   That path uses the bundled @expo/ngrok, whose only published release ships a
#   ngrok v2 AGENT (@expo/ngrok-bin@2.3.42). ngrok retired the v2 agent, so it
#   now fails every connection with ERR_NGROK_108 regardless of the token. So we
#   run our own ngrok v3 agent and point the bundler at it via DEV_PROXY_ENV,
#   which makes it advertise the tunnel host so the phone loads over the tunnel.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="${1:?usage: serve.sh <worktree-path> <feature-name>}"
NAME="${2:?usage: serve.sh <worktree-path> <feature-name>}"
DASH_URL="${DEV_DASHBOARD_URL:-https://dev.sean.build}"
POLL_SECONDS="${SERVE_TUNNEL_TIMEOUT:-120}"

slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"
BRANCH="feat/${SLUG}"

if [ ! -d "$WORKTREE" ]; then
  echo "serve.sh: worktree not found: $WORKTREE" >&2
  exit 1
fi

# --- 0. load the stack config -------------------------------------------------
REPO="$(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir)"
REPO="$(cd "$(dirname "$REPO")" && pwd)"
CONFIG="$REPO/.claude/serve-config.sh"
if [ ! -f "$CONFIG" ]; then
  echo "serve.sh: no $CONFIG. Copy .claude/serve-config.example.sh and set the" >&2
  echo "  dev-server command for this app's stack." >&2
  exit 5
fi
# shellcheck source=/dev/null
. "$CONFIG"
: "${DEV_START_CMD:?serve-config.sh must set DEV_START_CMD}"
DEV_PROXY_ENV="${DEV_PROXY_ENV:-}"
DEV_READY_RE="${DEV_READY_RE:-.}"
DEV_FAIL_RE="${DEV_FAIL_RE:-Skipping dev server|Input is required|EADDRINUSE}"
DEV_SCAN_SCHEME="${DEV_SCAN_SCHEME:-https}"

# --- 1. locate a ngrok v3 agent ----------------------------------------------
NGROK_BIN="${NGROK_BIN:-}"
if [ -z "$NGROK_BIN" ]; then
  for c in "$HOME/.local/bin/ngrok" "$(command -v ngrok 2>/dev/null || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then NGROK_BIN="$c"; break; fi
  done
fi
if [ -z "$NGROK_BIN" ] || ! "$NGROK_BIN" version 2>/dev/null | grep -q "version 3"; then
  echo "serve.sh: no ngrok v3 agent found (set NGROK_BIN or install to ~/.local/bin):" >&2
  echo "  curl -sSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz | tar xz -C ~/.local/bin" >&2
  echo "  ~/.local/bin/ngrok config add-authtoken <your-personal-token>" >&2
  exit 4
fi

# --- 2. one tunnel at a time -------------------------------------------------
# The free tier has one usable DOMAIN, and the agent's inspector binds port 4040.
# This state file is shared with the other projects on this machine on purpose,
# so the cap is enforced globally rather than per repo.
ACTIVE="$(node "$SCRIPT_DIR/count-active.js")"
if [ "$ACTIVE" -ge 1 ]; then
  echo "serve.sh: a tunnel is already active (ngrok free = one domain). Accept/Delete it first." >&2
  node "$SCRIPT_DIR/list.js" >&2
  exit 2
fi

# --- 3. pick a free port ------------------------------------------------------
PORT="$(node "$SCRIPT_DIR/pick-port.js")"
LOG="$(mktemp "/tmp/serve-tunnel-${SLUG}.XXXXXX.log")"
NGROK_LOG="$(mktemp "/tmp/serve-tunnel-${SLUG}-ngrok.XXXXXX.log")"
echo "serve.sh: $NAME -> port $PORT, dev log $LOG, ngrok log $NGROK_LOG"

node "$SCRIPT_DIR/write-state.js" --slug "$SLUG" --name "$NAME" --branch "$BRANCH" \
  --worktree "$WORKTREE" --port "$PORT" --status starting --log "$LOG" >/dev/null

# --- 4. launch ngrok + the dev server in ONE process group --------------------
# setsid => a new group led by $PID. The subshell starts the ngrok v3 agent,
# waits for it to publish the public URL, exports it under DEV_PROXY_ENV, then
# execs the dev server. Because both share the group, stop-tunnel.js reaps BOTH
# by killing -$PID.
setsid bash -c "
  cd '$WORKTREE' || exit 1
  '$NGROK_BIN' http $PORT --log=stdout --log-format=logfmt > '$NGROK_LOG' 2>&1 &
  for _ in \$(seq 1 30); do
    url=\$(curl -s -m 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
      | grep -oE 'https://[a-z0-9-]+\.ngrok[a-z0-9.-]*' | head -n1 || true)
    [ -n \"\$url\" ] && break
    sleep 1
  done
  [ -n '$DEV_PROXY_ENV' ] && export $DEV_PROXY_ENV=\"\$url\"
  export PORT=$PORT
  exec $DEV_START_CMD
" >"$LOG" 2>&1 &
PID=$!
node "$SCRIPT_DIR/write-state.js" --slug "$SLUG" --pid "$PID" >/dev/null

# --- 5. poll for the public URL + dev-server readiness -----------------------
NGROK_FAIL_RE="ERR_NGROK|failed to auth|authentication failed"
SCAN_URL=""
for ((i = 0; i < POLL_SECONDS; i++)); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "serve.sh: launcher exited early; see $LOG / $NGROK_LOG" >&2
    break
  fi
  if grep -qiE "$NGROK_FAIL_RE" "$NGROK_LOG" 2>/dev/null; then
    echo "serve.sh: ngrok agent failed (v2/wrong token? run 'ngrok config add-authtoken'):" >&2
    tail -n 8 "$NGROK_LOG" >&2
    break
  fi
  if grep -qiE "$DEV_FAIL_RE" "$LOG" 2>/dev/null; then
    echo "serve.sh: dev server did not start (port taken?):" >&2
    tail -n 8 "$LOG" >&2
    break
  fi
  PUB_URL="$(curl -s -m 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.ngrok[a-z0-9.-]*' | head -n1 || true)"
  if [ -n "$PUB_URL" ] && grep -qiE "$DEV_READY_RE" "$LOG" 2>/dev/null; then
    if [ "$DEV_SCAN_SCHEME" = "https" ]; then
      SCAN_URL="$PUB_URL"
    else
      SCAN_URL="${DEV_SCAN_SCHEME}://${PUB_URL#https://}"
    fi
    break
  fi
  sleep 1
done

# --- 6. record result + report -----------------------------------------------
if [ -n "$SCAN_URL" ]; then
  node "$SCRIPT_DIR/write-state.js" --slug "$SLUG" --status ready --exp "$SCAN_URL" >/dev/null
  echo
  echo "  feature : $NAME"
  echo "  url     : $SCAN_URL"
  echo "  dash    : $DASH_URL"
  echo
  npx -y qrcode-terminal "$SCAN_URL" 2>/dev/null || echo "(install qrcode-terminal for an inline QR; scan on $DASH_URL)"
  exit 0
else
  node "$SCRIPT_DIR/write-state.js" --slug "$SLUG" --status failed >/dev/null
  echo "serve.sh: no tunnel within ${POLL_SECONDS}s. Log tails:" >&2
  echo "--- dev server ($LOG) ---" >&2; tail -n 15 "$LOG" >&2
  echo "--- ngrok ($NGROK_LOG) ---" >&2; tail -n 8 "$NGROK_LOG" >&2
  exit 3
fi
