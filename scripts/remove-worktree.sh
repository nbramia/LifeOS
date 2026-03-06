#!/usr/bin/env bash
set -euo pipefail

# remove-worktree.sh — Remove a Claude Code worktree and its resources.
#
# Two modes:
#   Hook mode:   stdin JSON with worktree_path (called by WorktreeRemove hook)
#   Manual mode:  ./scripts/remove-worktree.sh <name>
#
# Removes the git worktree and optionally deletes the branch.
# All logging goes to stderr.

log()   { echo "$*" >&2; }
warn()  { echo "warning: $*" >&2; }
debug() { [ "${LIFEOS_WORKTREE_DEBUG:-}" = "1" ] && echo "debug: $*" >&2 || true; }
die()   { echo "error: $*" >&2; exit 1; }

# Derive repo name via --git-common-dir (correct even inside a worktree).
repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"

# ---------------------------------------------------------------------------
# 1. Determine worktree path — from argument or stdin JSON
# ---------------------------------------------------------------------------
if [ $# -ge 1 ]; then
  # Manual mode: argument is the worktree name. Flatten slashes to match
  # the directory naming convention (feat/foo → feat-foo).
  debug "mode: manual (argument)"
  name="$(echo "$1" | tr '/' '-')"
  worktree_path="$worktree_dir/$name"
else
  debug "mode: hook (stdin JSON)"
  # Hook mode: read single-line JSON from stdin. `read -r -t 5` returns
  # immediately on newline and times out after 5s if no data arrives (avoids
  # blocking when the caller keeps stdin open).
  command -v python3 >/dev/null 2>&1 || die "python3 is required but not found in PATH"
  if ! IFS= read -r -t 5 input; then
    [ -n "${input:-}" ] || die "timed out or received no data on stdin"
  fi
  worktree_path="$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('worktree_path',''))" 2>/dev/null)" \
    || die "failed to parse stdin json"
fi

[ -n "$worktree_path" ] || die "worktree path is empty"
[ -d "$worktree_path" ] || { log "worktree not found: $worktree_path"; exit 0; }
debug "resolved path: $worktree_path"

# Canonicalize to defeat symlink traversal before the safety check.
worktree_path="$(realpath "$worktree_path")"
worktree_dir="$(realpath "$worktree_dir")"

# Safety: ensure the path is under the worktree base dir.
case "$worktree_path" in
  "$worktree_dir/"*) ;;
  *) die "worktree_path is not under $worktree_dir: $worktree_path" ;;
esac

# ---------------------------------------------------------------------------
# 2. Read .worktree-info if present
# ---------------------------------------------------------------------------
branch=""
info_file="$worktree_path/.worktree-info"
if [ -f "$info_file" ]; then
  branch="$(grep '^BRANCH=' "$info_file" | cut -d= -f2)" || true
  debug ".worktree-info contents: branch=$branch"
  log "read .worktree-info: branch=$branch"
else
  warn ".worktree-info not found at $info_file — branch cleanup may be incomplete"
fi

# ---------------------------------------------------------------------------
# 3. Remove git worktree
# ---------------------------------------------------------------------------
if [ -d "$worktree_path" ]; then
  log "removing worktree: $worktree_path"
  git worktree remove --force "$worktree_path" 2>/dev/null || {
    warn "git worktree remove failed — falling back to manual cleanup"
    rm -rf "$worktree_path"
    git worktree prune
  }
fi

# ---------------------------------------------------------------------------
# 4. Optional branch deletion (behind env flag + safety checks)
# ---------------------------------------------------------------------------
if [ "${LIFEOS_WORKTREE_DELETE_BRANCH:-}" = "1" ] && [ -n "$branch" ]; then
  debug "branch deletion requested for: $branch"
  # Only delete if merged into origin/main.
  if git branch --merged origin/main | grep -q "^  $branch$"; then
    debug "branch $branch is merged into origin/main"
    # Only delete if not checked out in another worktree.
    if ! git worktree list --porcelain | grep -q "^branch refs/heads/$branch$"; then
      log "deleting merged branch: $branch"
      git branch -d "$branch" 2>/dev/null || warn "branch deletion failed (non-fatal)"
    else
      log "skipping branch deletion: $branch is checked out in another worktree"
    fi
  else
    log "skipping branch deletion: $branch is not merged into origin/main"
  fi
fi

log "worktree removal complete"
