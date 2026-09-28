#!/usr/bin/env bash
# overlap-check.sh <feature-slug-or-worktree>...
#
# For each feature worktree, list the files it changes relative to main
# (committed on its branch, modified in the working tree, or untracked), then
# print every pair of features that touch the same file. A changed file over
# 1500 lines gets a warning: only one fixer may edit such a file at a time.
# Run it once the worktrees exist and again before wave-integrate.sh.
#
# Exit 0 when nothing overlaps, 1 when at least one pair shares a file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
WT_ROOT="$(cd "$REPO/.." && pwd)/auto-job-worktrees"
BIG_LINES=1500

slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }

[ $# -ge 1 ] || { echo "usage: overlap-check.sh <feature-slug-or-worktree>..." >&2; exit 1; }

changed_files() {
  # Tracked files that differ from the point the feature branched off main
  # (covers commits on the branch and uncommitted edits, without the commits
  # main gained since), plus untracked files.
  local base
  base="$(git -C "$1" merge-base main HEAD 2>/dev/null || echo main)"
  { git -C "$1" diff --name-only "$base" -- 2>/dev/null
    git -C "$1" ls-files --others --exclude-standard 2>/dev/null
  } | sort -u
}

NAMES=()
LISTS=()
for ARG in "$@"; do
  if [ -d "$ARG/.git" ] || [ -f "$ARG/.git" ]; then
    WT="$(cd "$ARG" && pwd)"
    NAME="$(basename "$WT")"
  else
    NAME="$(slugify "$ARG")"
    WT="$WT_ROOT/$NAME"
  fi
  if ! git -C "$WT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "overlap-check: $WT is not a git worktree, skipping" >&2
    continue
  fi
  LIST="$(changed_files "$WT")"
  NAMES+=("$NAME")
  LISTS+=("$LIST")
  COUNT=0
  [ -n "$LIST" ] && COUNT="$(printf '%s\n' "$LIST" | wc -l)"
  echo "$NAME: $COUNT changed file(s)"
  [ -n "$LIST" ] && printf '%s\n' "$LIST" | sed 's/^/  /'
  while IFS= read -r F; do
    [ -n "$F" ] && [ -f "$WT/$F" ] || continue
    LINES="$(wc -l < "$WT/$F")"
    if [ "$LINES" -gt "$BIG_LINES" ]; then
      echo "  warning: $F is $LINES lines (over $BIG_LINES), queue fixers on it one at a time"
    fi
  done <<< "$LIST"
done

[ ${#NAMES[@]} -ge 2 ] || { echo "overlap-check: fewer than two worktrees, nothing to compare"; exit 0; }

OVERLAP=0
for ((i = 0; i < ${#NAMES[@]}; i++)); do
  for ((j = i + 1; j < ${#NAMES[@]}; j++)); do
    SHARED="$(comm -12 <(printf '%s\n' "${LISTS[$i]}" | grep . || true) \
                      <(printf '%s\n' "${LISTS[$j]}" | grep . || true))"
    if [ -n "$SHARED" ]; then
      OVERLAP=1
      echo "overlap: ${NAMES[$i]} and ${NAMES[$j]} both touch:"
      printf '%s\n' "$SHARED" | sed 's/^/  /'
    fi
  done
done

if [ "$OVERLAP" -eq 0 ]; then
  echo "no overlapping files"
  exit 0
fi
exit 1
