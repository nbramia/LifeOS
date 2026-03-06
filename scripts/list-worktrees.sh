#!/usr/bin/env bash
set -euo pipefail

# list-worktrees.sh — List active Claude Code worktrees with status.
#
# Prints a table of worktrees with name, branch, server port, and creation time.

# Derive repo name via --git-common-dir (correct even inside a worktree).
repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"

if [ ! -d "$worktree_dir" ]; then
  echo "no worktrees found"
  exit 0
fi

# Collect worktree directories that have .worktree-info.
found=false
fmt="%-30s %-30s %-12s %-25s\n"

for info_file in "$worktree_dir"/*/.worktree-info; do
  [ -f "$info_file" ] || continue

  # Print header on first match.
  if ! $found; then
    found=true
    # shellcheck disable=SC2059
    printf "$fmt" "NAME" "BRANCH" "SERVER_PORT" "CREATED_AT"
    # shellcheck disable=SC2059
    printf "$fmt" "----" "------" "-----------" "----------"
  fi

  name="$(grep '^NAME=' "$info_file" | cut -d= -f2)" || name="?"
  branch="$(grep '^BRANCH=' "$info_file" | cut -d= -f2)" || branch="?"
  server_port="$(grep '^SERVER_PORT=' "$info_file" | cut -d= -f2)" || server_port="?"
  created_at="$(grep '^CREATED_AT=' "$info_file" | cut -d= -f2)" || created_at="?"

  # shellcheck disable=SC2059
  printf "$fmt" "$name" "$branch" "$server_port" "$created_at"
done

if ! $found; then
  echo "no worktrees found"
fi
