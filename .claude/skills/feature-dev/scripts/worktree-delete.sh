#!/usr/bin/env bash
# worktree-delete.sh <feature name or slug>
# Throw a feature's worktree away: stop its tunnel, force-remove the worktree,
# delete the branch, and drop its state entry. Uncommitted work is discarded.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
NAME="${1:?usage: worktree-delete.sh <feature>}"
slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"
BRANCH="feat/${SLUG}"
WT_ROOT="$(cd "$REPO/.." && pwd)/auto-job-worktrees"
WT="$WT_ROOT/$SLUG"
SERVE_DIR="$REPO/.claude/skills/serve-tunnel/scripts"

node "$SERVE_DIR/stop-tunnel.js" "$SLUG" || true

if [ -d "$WT" ]; then
  git -C "$REPO" worktree remove --force "$WT"
fi
if git -C "$REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git -C "$REPO" branch -D "$BRANCH"
fi
git -C "$REPO" worktree prune
echo "worktree-delete: removed $BRANCH and its worktree" >&2
