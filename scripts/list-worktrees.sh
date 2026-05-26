#!/usr/bin/env bash
set -euo pipefail

# list-worktrees.sh — List active Claude Code worktrees with status.
#
# Prints a table of worktrees with name, branch, and allocated ports.

repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"

if [ ! -d "$worktree_dir" ]; then
  echo "no worktrees found"
  exit 0
fi

# Collect worktree directories that have .worktree-info.
found=false
fmt="%-30s %-30s %-10s %-12s\n"

for info_file in "$worktree_dir"/*/.worktree-info; do
  [ -f "$info_file" ] || continue

  # Print header on first match.
  if ! $found; then
    found=true
    # shellcheck disable=SC2059
    printf "$fmt" "NAME" "BRANCH" "API_PORT" "CHROMA_PORT"
    # shellcheck disable=SC2059
    printf "$fmt" "----" "------" "--------" "-----------"
  fi

  name="$(grep '^NAME=' "$info_file" | cut -d= -f2)" || name="?"
  branch="$(grep '^BRANCH=' "$info_file" | cut -d= -f2)" || branch="?"
  api_port="$(grep '^API_PORT=' "$info_file" | cut -d= -f2)" || api_port="?"
  chroma_port="$(grep '^CHROMA_PORT=' "$info_file" | cut -d= -f2)" || chroma_port="?"

  # shellcheck disable=SC2059
  printf "$fmt" "$name" "$branch" "$api_port" "$chroma_port"
done

if ! $found; then
  echo "no worktrees found"
fi
