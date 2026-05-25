#!/usr/bin/env bash
set -euo pipefail

# remove-worktree.sh — Remove a Claude Code worktree.
#
# Two modes:
#   Hook mode:   stdin JSON with worktree_path (called by WorktreeRemove hook)
#   Manual mode: ./scripts/remove-worktree.sh <name>
#
# Removes the git worktree and optionally deletes the branch (behind
# LIFEOS_WORKTREE_DELETE_BRANCH=1). All logging goes to stderr.

log()  { echo "$*" >&2; }
warn() { echo "warning: $*" >&2; }
die()  { dbg "FATAL: $*"; echo "error: $*" >&2; exit 1; }

# Persistent file log — survives hook stderr capture.
_LOG_DIR="$HOME/.claude/logs"
mkdir -p "$_LOG_DIR" 2>/dev/null || true
_LOG_FILE="$_LOG_DIR/worktree-remove.log"
# Rotate at ~64 KB.
if [ -f "$_LOG_FILE" ] && [ "$(wc -c < "$_LOG_FILE")" -gt 65536 ]; then
  tail -c 32768 "$_LOG_FILE" > "$_LOG_FILE.tmp" && mv "$_LOG_FILE.tmp" "$_LOG_FILE"
fi
dbg() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$_LOG_FILE" 2>/dev/null || true; }

dbg "=== WorktreeRemove hook fired (pid=$$) ==="
dbg "args: ${*:-(none)}"
dbg "stdin: $([ -t 0 ] && echo 'tty' || echo 'pipe')"
dbg "caller_ppid=${PPID:-unknown}"

repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"

# ---------------------------------------------------------------------------
# 1. Determine worktree path — from argument or stdin JSON
# ---------------------------------------------------------------------------
if [ $# -ge 1 ]; then
  # Manual mode: argument is the worktree name. Flatten slashes to match
  # the directory naming convention (feat/foo → feat-foo).
  dbg "mode: manual (arg=$1)"
  name="$(echo "$1" | tr '/' '-')"
  worktree_path="$worktree_dir/$name"
else
  dbg "mode: hook (stdin JSON)"
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
dbg "resolved worktree_path=$worktree_path"
[ -d "$worktree_path" ] || { dbg "not found (already removed?): $worktree_path"; log "worktree not found: $worktree_path"; exit 0; }

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
  dbg ".worktree-info: branch=$branch"
  log "read .worktree-info: branch=$branch"
else
  dbg ".worktree-info missing at $info_file"
  warn ".worktree-info not found at $info_file — branch cleanup may be incomplete"
fi

# ---------------------------------------------------------------------------
# 3. Remove git worktree
# ---------------------------------------------------------------------------
if [ -d "$worktree_path" ]; then
  dbg "git worktree remove starting: $worktree_path"
  log "removing worktree: $worktree_path"
  git worktree remove --force "$worktree_path" 2>/dev/null || {
    dbg "git worktree remove failed, falling back to rm -rf"
    warn "git worktree remove failed — falling back to manual cleanup"
    rm -rf "$worktree_path"
    git worktree prune
  }
  dbg "git worktree remove done"
else
  dbg "worktree directory already gone: $worktree_path"
fi

# ---------------------------------------------------------------------------
# 4. Optional branch deletion (behind env flag + safety checks)
# ---------------------------------------------------------------------------
if [ "${LIFEOS_WORKTREE_DELETE_BRANCH:-}" = "1" ] && [ -n "$branch" ]; then
  dbg "branch deletion requested: $branch"
  # Only delete if merged into origin/main.
  if git branch --merged origin/main | grep -q "^  $branch$"; then
    dbg "branch $branch is merged into origin/main"
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

dbg "worktree removal complete"
dbg "EXIT_SENTINEL hook_pid=$$ ppid=${PPID:-unknown} status=success"
log "worktree removal complete"

# Launch exit watchdog to monitor the parent Claude process.
# In observed hangs, SessionEnd never fires, but WorktreeRemove does. The
# watchdog captures diagnostics as the process fails to shut down.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
caller_pid="${PPID:-}"
if [ -n "$caller_pid" ] && [ -x "$_SCRIPT_DIR/exit-watchdog.sh" ]; then
  worktree_name="$(basename "$worktree_path" 2>/dev/null)" || worktree_name=""
  nohup "$_SCRIPT_DIR/exit-watchdog.sh" "$caller_pid" "WorktreeRemove" "$worktree_name" \
    </dev/null >/dev/null 2>&1 &
  dbg "exit_watchdog: launched for pid=$caller_pid (watchdog_pid=$!)"
fi
