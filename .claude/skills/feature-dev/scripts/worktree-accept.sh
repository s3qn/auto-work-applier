#!/usr/bin/env bash
# worktree-accept.sh <feature name or slug> "<commit subject>"
# Commit the feature in its worktree and fast-forward it into LOCAL main, then
# tear the worktree down. Nothing is pushed. This is the one authorized commit
# point in the feature-dev loop.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
NAME="${1:?usage: worktree-accept.sh <feature> \"<commit subject>\"}"
SUBJECT="${2:?usage: worktree-accept.sh <feature> \"<commit subject>\"}"
slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"
BRANCH="feat/${SLUG}"
WT_ROOT="$(cd "$REPO/.." && pwd)/auto-job-worktrees"
WT="$WT_ROOT/$SLUG"
SERVE_DIR="$REPO/.claude/skills/serve-tunnel/scripts"

[ -d "$WT" ] || { echo "worktree-accept: no worktree at $WT" >&2; exit 1; }

# 1. commit in the worktree (skip if nothing staged/changed).
# worktree-create adds node_modules/.env to this worktree's exclude, so a plain
# `git add -A` silently skips those.
git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "worktree-accept: no changes to commit in $BRANCH" >&2
else
  git -C "$WT" commit -m "$SUBJECT"
fi

# 2. fast-forward main. If main advanced (an earlier feature merged), rebase the
#    feature branch onto main first, then ff.
if ! git -C "$REPO" merge --ff-only "$BRANCH" 2>/dev/null; then
  echo "worktree-accept: main advanced; rebasing $BRANCH onto main" >&2
  git -C "$WT" rebase main
  git -C "$REPO" merge --ff-only "$BRANCH"
fi
echo "worktree-accept: merged $BRANCH into local main (not pushed)" >&2

# 3. stop the tunnel + tear the worktree down
node "$SERVE_DIR/stop-tunnel.js" "$SLUG" || true
git -C "$REPO" worktree remove "$WT" || git -C "$REPO" worktree remove --force "$WT"
git -C "$REPO" branch -d "$BRANCH"
git -C "$REPO" worktree prune
echo "worktree-accept: cleaned up $BRANCH" >&2
