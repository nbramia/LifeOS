#!/usr/bin/env bash
set -euo pipefail

# create-worktree.sh — Claude Code WorktreeCreate hook.
#
# Reads JSON from stdin (fields: name, session_id, cwd, etc.).
# Prints the absolute worktree path to stdout (hook contract).
# All logging goes to stderr.
#
# Allocates per-worktree LifeOS service ports (API, ChromaDB) and records
# them in .worktree-info inside the worktree. Does NOT auto-start any
# services — start them manually with the allocated ports when needed.

log()  { echo "$*" >&2; }
warn() { echo "warning: $*" >&2; }
die()  { dbg "FATAL: $*"; echo "error: $*" >&2; exit 1; }

# Persistent file log — survives hook stderr capture.
_LOG_DIR="$HOME/.claude/logs"
mkdir -p "$_LOG_DIR" 2>/dev/null || true
_LOG_FILE="$_LOG_DIR/worktree-create.log"
# Rotate at ~64 KB.
if [ -f "$_LOG_FILE" ] && [ "$(wc -c < "$_LOG_FILE")" -gt 65536 ]; then
  tail -c 32768 "$_LOG_FILE" > "$_LOG_FILE.tmp" && mv "$_LOG_FILE.tmp" "$_LOG_FILE"
fi
dbg() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$_LOG_FILE" 2>/dev/null || true; }

dbg "=== WorktreeCreate hook fired (pid=$$) ==="

command -v python3 >/dev/null 2>&1 || die "python3 is required but not found in PATH"

# ---------------------------------------------------------------------------
# 1. Parse stdin JSON and extract name
# ---------------------------------------------------------------------------
# Use read with a timeout — returns immediately on newline (the hook protocol
# sends single-line JSON) and times out after 5s if no data arrives. Unlike
# `cat`, read doesn't wait for EOF so it won't stall when the caller keeps
# stdin open.
dbg "reading stdin…"
if ! IFS= read -r -t 5 input; then
  [ -n "${input:-}" ] || die "timed out or received no data on stdin"
fi
name="$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)" \
  || die "failed to parse stdin json"
dbg "parsed name=$name"

[ -n "$name" ] || die "worktree name is empty"

# ---------------------------------------------------------------------------
# 2. Validate name — reject path traversal and shell metacharacters
# ---------------------------------------------------------------------------
if [[ "$name" == *..* ]]; then
  die "name contains path traversal: $name"
fi
if [[ ! "$name" =~ ^[A-Za-z0-9./_-]+$ ]]; then
  die "name contains invalid characters (allowed: A-Za-z0-9./_-): $name"
fi
dbg "validated name=$name"

# ---------------------------------------------------------------------------
# 3. Resolve paths
# ---------------------------------------------------------------------------
# Flatten slashes for the directory name (e.g. feat/foo → feat-foo) so the
# worktree lives in a flat directory, not nested subdirectories.
dir_name="$(echo "$name" | tr '/' '-')"
# Use --git-common-dir so the repo name resolves to the main repo even when
# this script runs from inside an existing worktree.
repo_name="$(basename "$(cd "$(git rev-parse --git-common-dir)/.." && pwd)")"
worktree_dir="$HOME/.claude/worktrees/$repo_name"
worktree_path="$worktree_dir/$dir_name"

dbg "paths: worktree_dir=$worktree_dir worktree_path=$worktree_path"

# Ensure parent exists.
mkdir -p "$worktree_dir"

# ---------------------------------------------------------------------------
# 4. Idempotent re-entry — if worktree already exists and is fully configured,
#    return its path. If the worktree exists but .worktree-info is missing
#    (partial failure on a previous run), skip to port allocation and file writes.
# ---------------------------------------------------------------------------
worktree_exists=false
if [ -d "$worktree_path" ] && git worktree list --porcelain | grep -q "^worktree $worktree_path$"; then
  if [ -f "$worktree_path/.worktree-info" ]; then
    dbg "idempotent hit — already exists with .worktree-info"
    log "worktree already exists: $worktree_path"
    echo "$worktree_path"
    exit 0
  fi
  dbg "partial worktree — .worktree-info missing, re-running setup"
  log "worktree exists but .worktree-info is missing — re-running setup"
  worktree_exists=true
  # Determine the branch from the existing worktree for .worktree-info.
  branch="$(git -C "$worktree_path" rev-parse --abbrev-ref HEAD 2>/dev/null)" || branch="$name"
fi

# ---------------------------------------------------------------------------
# 5. Port allocation (mkdir-based lock)
#
# Runs before branch resolution / worktree creation so a port-allocation
# failure aborts cleanly with nothing created — no worktree or branch left
# to orphan.
# ---------------------------------------------------------------------------
lock_dir="$worktree_dir/.port-lock"

# Portable lock mtime: GNU stat uses -c, BSD/macOS stat uses -f. Try GNU
# first (this fleet is Linux), fall back to BSD, and validate the result is
# all-digits so a wrong-platform invocation (which can print a multi-line
# report to stdout and still exit 0) can't leak garbage into arithmetic.
lock_mtime() {
  local mtime
  mtime="$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null)"
  [[ "$mtime" =~ ^[0-9]+$ ]] && echo "$mtime" || echo 0
}

allocate_port() {
  local key="$1" range_start="$2" range_end="$3"

  # Collect ports already assigned to other worktrees.
  local used_ports=()
  for info_file in "$worktree_dir"/*/.worktree-info; do
    [ -f "$info_file" ] || continue
    local p
    p="$(grep "^${key}=" "$info_file" 2>/dev/null | cut -d= -f2)" || true
    [ -n "$p" ] && used_ports+=("$p")
  done
  dbg "$key: used ports: ${used_ports[*]+"${used_ports[*]}"}"

  for port in $(seq "$range_start" "$range_end"); do
    # Skip if already assigned.
    local skip=false
    for used in "${used_ports[@]+"${used_ports[@]}"}"; do
      if [ "$port" = "$used" ]; then
        skip=true
        break
      fi
    done
    $skip && continue

    # Skip if something is listening on this port.
    if lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
      continue
    fi

    echo "$port"
    return 0
  done

  die "no free port in range $range_start-$range_end"
}

# Acquire lock (mkdir is atomic on POSIX).
lock_max_attempts=60          # 60 * 0.5s = 30s wall-clock timeout
lock_attempts=0
lock_stale_seconds=300        # Break locks older than 5 minutes
while ! mkdir "$lock_dir" 2>/dev/null; do
  # Break stale locks (likely from a killed process).
  if [ -d "$lock_dir" ]; then
    lock_age=$(( $(date +%s) - $(lock_mtime "$lock_dir") ))
    if [ "$lock_age" -gt "$lock_stale_seconds" ]; then
      log "breaking stale lock (age: ${lock_age}s)"
      rmdir "$lock_dir" 2>/dev/null || true
      continue
    fi
  fi
  lock_attempts=$((lock_attempts + 1))
  if [ "$lock_attempts" -ge "$lock_max_attempts" ]; then
    die "timed out waiting for port allocation lock after ~30s"
  fi
  sleep 0.5
done
# Ensure lock is released on exit.
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

api_port="$(allocate_port API_PORT 8100 8199)"
chroma_port="$(allocate_port CHROMA_PORT 8200 8299)"
log "allocated ports: API_PORT=$api_port CHROMA_PORT=$chroma_port"

# Release lock early.
rmdir "$lock_dir" 2>/dev/null || true


if [ "$worktree_exists" = false ]; then
# ---------------------------------------------------------------------------
# 6. Branch resolution
#
# Candidate A: <name> (exact)
# Candidate B: if name matches <type>-<rest>, also try <type>/<rest>
# Order: local branch → remote branch → create new
# ---------------------------------------------------------------------------
candidate_a="$name"
candidate_b=""

if [[ "$name" =~ ^(feat|fix|docs|test|refactor|perf|chore)-(.+)$ ]]; then
  candidate_b="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
fi

dbg "branch candidates: $candidate_a${candidate_b:+ $candidate_b}"

# Fetch latest remote state (best-effort, non-fatal).
git fetch --quiet origin 2>/dev/null || warn "git fetch failed (non-fatal — using cached remote state)"

resolve_branch() {
  local candidates=("$candidate_a")
  [ -n "$candidate_b" ] && candidates+=("$candidate_b")

  for candidate in "${candidates[@]}"; do
    # Check local branch.
    if git show-ref --verify --quiet "refs/heads/$candidate" 2>/dev/null; then
      log "resolved to local branch: $candidate"
      echo "$candidate"
      return 0
    fi
  done

  for candidate in "${candidates[@]}"; do
    # Check remote branch.
    if git show-ref --verify --quiet "refs/remotes/origin/$candidate" 2>/dev/null; then
      log "resolved to remote branch: origin/$candidate"
      git branch --track "$candidate" "origin/$candidate" >/dev/null 2>/dev/null || true
      echo "$candidate"
      return 0
    fi
  done

  # No match — create new branch from origin/main.
  local new_branch="${candidate_b:-$candidate_a}"
  log "creating new branch from origin/main: $new_branch"
  git branch "$new_branch" origin/main >&2
  echo "$new_branch"
}

branch="$(resolve_branch)"

# ---------------------------------------------------------------------------
# 7. Create worktree
# ---------------------------------------------------------------------------
git worktree add "$worktree_path" "$branch" >&2
log "created worktree at $worktree_path on branch $branch"

fi # worktree_exists

# ---------------------------------------------------------------------------
# 8. Write .worktree-info metadata
# ---------------------------------------------------------------------------
cat > "$worktree_path/.worktree-info" <<INFO
NAME=$name
BRANCH=$branch
API_PORT=$api_port
CHROMA_PORT=$chroma_port
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
INFO
log "wrote .worktree-info"

# ---------------------------------------------------------------------------
# 9. Contract: print absolute worktree path to stdout
# ---------------------------------------------------------------------------
dbg "worktree ready: $worktree_path"
log "worktree ready: $worktree_path"
echo "$worktree_path"
