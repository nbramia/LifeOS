#!/usr/bin/env bash
set -euo pipefail

# cleanup-worktrees.sh — Prune stale worktree references.
#
# Runs git worktree prune to remove references to deleted worktree directories.

echo "pruning stale git worktree references..."
git worktree prune

echo "done. run './scripts/list-worktrees.sh' to see remaining worktrees."
