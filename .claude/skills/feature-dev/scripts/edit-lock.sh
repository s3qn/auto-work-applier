#!/usr/bin/env bash
# edit-lock.sh acquire|release|check <worktree> [label]
#
# One fixer per worktree. A fixer acquires the lock before its first edit and
# releases it when done, so two sessions (or a leftover session and a new
# agent) never edit the same worktree at once. A lock older than 45 minutes
# counts as stale and is taken over. Bobs split on one chief feature don't use
# it: their plan already assigns file ownership.
#
# acquire: exit 0 and write the lock, or exit 2 and print who holds it.
# check:   exit 0 if free, exit 2 if held. Also warns about files in src/ or
#          backend/ modified in the last 3 minutes (someone may be editing
#          without a lock, such as a leftover interactive session).
set -euo pipefail

CMD="${1:?usage: edit-lock.sh acquire|release|check <worktree> [label]}"
WORKTREE="${2:?usage: edit-lock.sh acquire|release|check <worktree> [label]}"
LABEL="${3:-unnamed}"
LOCK="$WORKTREE/.edit-lock"
STALE_MIN=45

[ -d "$WORKTREE" ] || { echo "edit-lock: worktree not found: $WORKTREE" >&2; exit 1; }

held() {
  [ -f "$LOCK" ] || return 1
  if [ -n "$(find "$LOCK" -mmin +"$STALE_MIN" 2>/dev/null)" ]; then
    echo "edit-lock: taking over stale lock: $(cat "$LOCK")" >&2
    rm -f "$LOCK"
    return 1
  fi
  return 0
}

recent_edits() {
  find "$WORKTREE/src" "$WORKTREE/backend" -type f -mmin -3 \
    -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null | head -5 || true
}

case "$CMD" in
  acquire)
    if held; then
      echo "edit-lock: HELD by $(cat "$LOCK")" >&2
      exit 2
    fi
    echo "$LABEL pid=$$ $(date -u +%FT%TZ)" > "$LOCK"
    echo "edit-lock: acquired by $LABEL"
    ;;
  release)
    rm -f "$LOCK"
    echo "edit-lock: released"
    ;;
  check)
    RECENT="$(recent_edits)"
    if [ -n "$RECENT" ]; then
      echo "edit-lock: files changed in the last 3 minutes:" >&2
      echo "$RECENT" >&2
    fi
    if held; then
      echo "edit-lock: HELD by $(cat "$LOCK")" >&2
      exit 2
    fi
    echo "edit-lock: free"
    ;;
  *)
    echo "edit-lock: unknown command $CMD" >&2
    exit 1
    ;;
esac
