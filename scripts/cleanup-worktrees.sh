#!/usr/bin/env bash
set -euo pipefail

# cleanup-worktrees.sh — Idempotent worktree pruning + targeted stale cleanup.
#
# Safe to call pre-flight before `git worktree add`: it tolerates an
# already-pruned state and a stale worktree directory/branch left behind by a
# crashed run, so the subsequent add can't fail with "already exists" /
# "already checked out" (#400).
#
# Usage:
#   ./scripts/cleanup-worktrees.sh                       # prune dangling refs only
#   ./scripts/cleanup-worktrees.sh <worktree-path>       # + remove that stale worktree
#   ./scripts/cleanup-worktrees.sh <worktree-path> <br>  # + delete its branch too
#
# Every step no-ops cleanly when there's nothing to do, so re-running is safe.

worktree_path="${1:-}"
branch="${2:-}"

# 1. Prune references to worktree directories that no longer exist on disk.
#    Idempotent: a no-op when nothing is stale.
echo "pruning stale git worktree references..."
git worktree prune

# 2. Targeted cleanup of a specific stale worktree, if a path was given. A
#    crashed run can leave the directory *present* on disk — which prune does
#    NOT clear — so the next `git worktree add <same path>` fails. Remove it
#    here so the add is collision-free. All steps tolerate already-gone state.
if [ -n "$worktree_path" ]; then
  if git worktree list --porcelain | grep -qxF "worktree $worktree_path" \
     || [ -e "$worktree_path" ]; then
    echo "removing stale worktree: $worktree_path"
    # --force handles a dirty/locked worktree from a crash; the rm -rf fallback
    # covers a directory git no longer tracks.
    # Trailing `|| true` so a failed rm (e.g. permissions) can't abort the
    # script under `set -e` before the branch cleanup below runs.
    git worktree remove --force "$worktree_path" 2>/dev/null || rm -rf "$worktree_path" || true
    git worktree prune
  else
    echo "no stale worktree at: $worktree_path (already clean)"
  fi

  # Delete the leftover branch only if it exists and isn't checked out in some
  # other live worktree. Safe to call when the branch was never created.
  if [ -n "$branch" ] && git show-ref --verify --quiet "refs/heads/$branch"; then
    if git worktree list --porcelain | grep -qxF "branch refs/heads/$branch"; then
      echo "leaving branch checked out elsewhere: $branch"
    else
      echo "deleting stale branch: $branch"
      git branch -D "$branch" 2>/dev/null || true
    fi
  fi
fi

echo "done. run './scripts/list-worktrees.sh' to see remaining worktrees."
