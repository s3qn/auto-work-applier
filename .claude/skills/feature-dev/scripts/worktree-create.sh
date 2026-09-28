#!/usr/bin/env bash
# worktree-create.sh <feature name or slug>
# Create an isolated worktree for one feature, branched from main, in a SIBLING
# directory outside the repo (keeps `git status` and the bundler clean). Shares
# node_modules and .env from the main checkout so the app builds and can reach
# its backend without a reinstall.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
NAME="${1:?usage: worktree-create.sh <feature name>}"
slugify() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }
SLUG="$(slugify "$NAME")"
BRANCH="feat/${SLUG}"
WT_ROOT="$(cd "$REPO/.." && pwd)/auto-job-worktrees"
WT="$WT_ROOT/$SLUG"

mkdir -p "$WT_ROOT"

if git -C "$REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "worktree-create: branch $BRANCH already exists" >&2
  exit 1
fi
if [ -e "$WT" ]; then
  echo "worktree-create: path already exists: $WT" >&2
  exit 1
fi

# Send git's progress/"HEAD is now at" chatter to stderr so stdout stays clean
# (the caller captures stdout as the worktree path).
git -C "$REPO" worktree add -b "$BRANCH" "$WT" main >&2

# Share dependencies and secrets from the main checkout (both are gitignored, so
# the fresh worktree lacks them). Both are optional: before the app has a stack
# there is nothing to share, and that is not an error.
#
# node_modules is HARDLINK-COPIED (cp -al), not symlinked. A symlink pointing at
# the main checkout resolves to a path OUTSIDE this worktree, which makes a
# bundler push its server root up to the common parent and hand the device a
# broken entry path. A hardlink copy lives physically inside the worktree (same
# inodes, ~1s, near-zero extra disk on one filesystem) so the server root stays
# at the worktree and the entry resolves. `-a` preserves inner .bin symlinks.
if [ -d "$REPO/node_modules" ]; then
  cp -al "$REPO/node_modules" "$WT/node_modules"
fi
if [ -e "$REPO/.env" ]; then
  ln -s "$REPO/.env" "$WT/.env"
fi
# The backend keeps its own venv and its own .env (both gitignored), and a
# feature that touches backend/ needs them: without backend/.env the worktree
# falls back to an empty data dir instead of the real islands.
for rel in backend/.venv backend/.env; do
  if [ -e "$REPO/$rel" ] && [ ! -e "$WT/$rel" ]; then
    ln -s "$REPO/$rel" "$WT/$rel"
  fi
done

# Belt-and-suspenders: a .gitignore entry of `node_modules/` (dir only) does not
# match a symlink named node_modules. Add both names to this worktree's exclude
# so they can never be staged/committed.
EXCLUDE="$(git -C "$WT" rev-parse --git-path info/exclude)"
for pat in node_modules .env backend/.venv backend/.env; do
  grep -qxF "$pat" "$EXCLUDE" 2>/dev/null || printf '%s\n' "$pat" >> "$EXCLUDE"
done

echo "$WT"   # stdout = the worktree path, for the caller to capture
echo "worktree-create: $BRANCH at $WT" >&2
