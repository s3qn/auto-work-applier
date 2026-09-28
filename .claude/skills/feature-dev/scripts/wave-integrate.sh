#!/usr/bin/env bash
# wave-integrate.sh <wave-name> <feature-slug>...
# wave-integrate.sh --resume <wave-name>
#
# Merge several already-built feature worktrees into one integration worktree
# for a wave, without committing anything in the source worktrees:
#   1. Snapshot each feature's uncommitted changes as a patch (git add -A,
#      diff --cached against main, then git reset -q). The feature worktree
#      keeps its working-tree edits on disk, just unstaged; nothing there is
#      committed.
#   2. Create the wave worktree with worktree-create.sh.
#   3. Apply the patches in order with `git apply --3way --index`. On the
#      first patch that leaves conflicts, stop: print the conflicted files,
#      the patches not yet applied, and the exact resume command.
# Rerun with --resume after resolving conflicts by hand in the wave worktree
# to apply the remaining patches.
#
# Exit 0 when every patch applied cleanly, 3 when conflicts need resolving.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
WT_ROOT="$(cd "$REPO/.." && pwd)/auto-job-worktrees"
TMP_DIR="${TMPDIR:-/tmp}"

slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }

RESUME=0
if [ "${1:-}" = "--resume" ]; then
  RESUME=1
  shift
fi

WAVE_NAME="${1:?usage: wave-integrate.sh [--resume] <wave-name> [<feature-slug>...]}"
WAVE_SLUG="$(slugify "$WAVE_NAME")"
STATE_FILE="$TMP_DIR/wave-integrate-${WAVE_SLUG}.state"

resume_cmd() { echo "bash $SCRIPT_DIR/wave-integrate.sh --resume \"$WAVE_NAME\""; }

if [ "$RESUME" -eq 1 ]; then
  [ -f "$STATE_FILE" ] || { echo "wave-integrate: no state file at $STATE_FILE, nothing to resume" >&2; exit 1; }
  WAVE_WT="$(sed -n '1p' "$STATE_FILE")"
  mapfile -t PATCHES < <(tail -n +2 "$STATE_FILE")
  [ -d "$WAVE_WT" ] || { echo "wave-integrate: wave worktree missing: $WAVE_WT" >&2; exit 1; }
  if git -C "$WAVE_WT" diff --name-only --diff-filter=U | grep -q .; then
    echo "wave-integrate: unresolved conflicts remain in $WAVE_WT, resolve and git add them first:" >&2
    git -C "$WAVE_WT" diff --name-only --diff-filter=U | sed 's/^/  /' >&2
    exit 3
  fi
else
  shift
  [ $# -ge 1 ] || { echo "wave-integrate: need at least one feature slug" >&2; exit 1; }

  PATCHES=()
  for FEATURE in "$@"; do
    SLUG="$(slugify "$FEATURE")"
    FWT="$WT_ROOT/$SLUG"
    [ -d "$FWT" ] || { echo "wave-integrate: no worktree at $FWT" >&2; exit 1; }
    PATCH="$TMP_DIR/wave-integrate-${WAVE_SLUG}-${SLUG}.patch"
    git -C "$FWT" add -A
    if git -C "$FWT" diff --cached --quiet; then
      echo "wave-integrate: $SLUG has no uncommitted changes, skipping" >&2
      git -C "$FWT" reset -q
      continue
    fi
    git -C "$FWT" diff --cached --binary main > "$PATCH"
    git -C "$FWT" reset -q
    if grep -q "^diff --git a/FEATURES.md" "$PATCH"; then
      echo "wave-integrate: warning: $SLUG touches FEATURES.md" >&2
    fi
    PATCHES+=("$PATCH")
  done

  [ ${#PATCHES[@]} -ge 1 ] || { echo "wave-integrate: no patches to apply (nothing changed in any feature)" >&2; exit 1; }

  WAVE_WT="$(bash "$SCRIPT_DIR/worktree-create.sh" "$WAVE_NAME")"
fi

{
  echo "$WAVE_WT"
  printf '%s\n' "${PATCHES[@]}"
} > "$STATE_FILE"

REMAINING=("${PATCHES[@]}")
while [ ${#REMAINING[@]} -gt 0 ]; do
  PATCH="${REMAINING[0]}"
  FUTURE=("${REMAINING[@]:1}")
  echo "wave-integrate: applying $(basename "$PATCH")" >&2
  if git -C "$WAVE_WT" apply --3way --index "$PATCH"; then
    REMAINING=("${FUTURE[@]}")
    {
      echo "$WAVE_WT"
      printf '%s\n' "${REMAINING[@]}"
    } > "$STATE_FILE"
  else
    echo "wave-integrate: conflicts applying $(basename "$PATCH")" >&2
    echo "  conflicted files:" >&2
    git -C "$WAVE_WT" diff --name-only --diff-filter=U | sed 's/^/    /' >&2
    if [ ${#FUTURE[@]} -gt 0 ]; then
      echo "  not yet applied:" >&2
      printf '    %s\n' "${FUTURE[@]}" >&2
    else
      echo "  not yet applied: none, this was the last patch" >&2
    fi
    {
      echo "$WAVE_WT"
      if [ ${#FUTURE[@]} -gt 0 ]; then printf '%s\n' "${FUTURE[@]}"; fi
    } > "$STATE_FILE"
    echo "wave-integrate: resolve the conflicts in $WAVE_WT (edit, then git -C \"$WAVE_WT\" add <file>), then run:" >&2
    echo "  $(resume_cmd)" >&2
    exit 3
  fi
done

rm -f "$STATE_FILE"
echo "$WAVE_WT"
exit 0
