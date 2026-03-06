#!/usr/bin/env bash
set -euo pipefail

# cleanup-worktrees.sh — Prune stale worktree references.
#
# 1. Runs git worktree prune to remove references to deleted worktree directories.
# 2. Lists remaining worktrees.

# Derive repo name via --git-common-dir (correct even inside a worktree).
repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"

# ---------------------------------------------------------------------------
# 1. Prune stale git worktree references
# ---------------------------------------------------------------------------
echo "pruning stale git worktree references..."
git worktree prune

# ---------------------------------------------------------------------------
# 2. Remove orphaned worktree directories (exist on disk but not in git)
# ---------------------------------------------------------------------------
orphan_count=0
if [ -d "$worktree_dir" ]; then
  for wt_dir in "$worktree_dir"/*/; do
    [ -d "$wt_dir" ] || continue
    # Check if git still knows about this worktree.
    wt_abs="$(realpath "$wt_dir" 2>/dev/null)" || continue
    if ! git worktree list --porcelain | grep -q "^worktree $wt_abs$"; then
      echo "removing orphaned directory: $wt_abs"
      rm -rf "$wt_abs"
      orphan_count=$((orphan_count + 1))
    fi
  done
fi

if [ "$orphan_count" -eq 0 ]; then
  echo "no orphaned worktree directories found"
else
  echo "removed $orphan_count orphaned worktree director(ies)"
fi

echo "done. run './scripts/list-worktrees.sh' to see remaining worktrees."
