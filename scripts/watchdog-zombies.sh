#!/usr/bin/env bash
# watchdog-zombies.sh — Scan for detached (zombie) Claude Code processes and log diagnostics.
#
# Diagnostic only — never kills processes.
# Uses marker files for 6-hour deduplication to avoid re-documenting the same PID.

set +e

_LOG_DIR="$HOME/.claude/logs"
_MARKER_DIR="$_LOG_DIR/.watchdog-markers"
mkdir -p "$_LOG_DIR" "$_MARKER_DIR" 2>/dev/null || true
_LOG_FILE="$_LOG_DIR/zombie-watchdog.log"

# Rotate at ~256 KB.
if [ -f "$_LOG_FILE" ] && [ "$(wc -c < "$_LOG_FILE" 2>/dev/null)" -gt 262144 ]; then
  tail -c 131072 "$_LOG_FILE" > "$_LOG_FILE.tmp" && mv "$_LOG_FILE.tmp" "$_LOG_FILE"
fi

dbg() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$_LOG_FILE" 2>/dev/null || true; }

# Clean up stale markers older than 6 hours.
find "$_MARKER_DIR" -name "pid-*" -mmin +360 -delete 2>/dev/null || true

# Find detached Claude processes: TTY == ?? means no controlling terminal.
# Match only processes whose executable name is "claude" (not zsh/bash children
# that happen to have "claude" in their CWD or output paths).
zombies=()
while IFS= read -r pid; do
  [ -n "$pid" ] && zombies+=("$pid")
done < <(ps -eo pid=,tty=,comm= 2>/dev/null | awk '$2=="??" && $3=="claude" {print $1}')

if [ ${#zombies[@]} -eq 0 ]; then
  echo "no zombie claude processes detected"
  exit 0
fi

dbg "=== Watchdog scan: found ${#zombies[@]} detached Claude process(es) ==="

documented_count=0
for pid in "${zombies[@]}"; do
  # Validate PID is numeric.
  [[ "$pid" =~ ^[0-9]+$ ]] || continue

  # Dedup: skip if marker exists and is less than 6 hours old.
  marker="$_MARKER_DIR/pid-$pid"
  if [ -f "$marker" ]; then
    dbg "skipping pid=$pid (already documented within 6h)"
    continue
  fi

  dbg "--- Documenting pid=$pid ---"

  # FD summary.
  fd_out="$(lsof -p "$pid" 2>/dev/null)" || fd_out=""
  if [ -n "$fd_out" ]; then
    revoked="$(echo "$fd_out" | grep -c 'revoked' || true)"
    kqueue="$(echo "$fd_out" | grep -c 'KQUEUE' || true)"
    unix_sock="$(echo "$fd_out" | grep -c 'unix' || true)"
    skill_watcher="$(echo "$fd_out" | grep -c 'skill' || true)"
    total_fds="$(echo "$fd_out" | tail -n +2 | wc -l | tr -d ' ')"
    dbg "fd_summary: total=$total_fds revoked=$revoked kqueue=$kqueue unix_socket=$unix_sock skill_watcher=$skill_watcher"
  else
    dbg "fd_summary: lsof returned no data for pid=$pid"
  fi

  # Worktree association — check if the process CWD is inside a worktree.
  cwd="$(lsof -a -d cwd -p "$pid" -Fn 2>/dev/null | grep '^n' | sed 's/^n//')" || cwd=""
  worktree_name=""
  worktree_dir_exists="false"
  if [ -n "$cwd" ]; then
    # Try to extract worktree name from CWD path.
    case "$cwd" in
      */.claude/worktrees/*/*)
        worktree_name="$(echo "$cwd" | sed 's|.*/.claude/worktrees/[^/]*/||' | cut -d/ -f1)"
        worktree_dir="$(echo "$cwd" | grep -o '.*/.claude/worktrees/[^/]*/[^/]*')"
        [ -d "$worktree_dir" ] && worktree_dir_exists="true"
        ;;
    esac
  fi
  dbg "worktree: name=${worktree_name:-unknown} dir_exists=$worktree_dir_exists"

  # Process stats.
  ps_info="$(ps -p "$pid" -o pid=,lstart=,cputime=,rss= 2>/dev/null)" || ps_info=""
  if [ -n "$ps_info" ]; then
    start_time="$(echo "$ps_info" | awk '{print $2, $3, $4, $5, $6}')"
    cpu_time="$(echo "$ps_info" | awk '{print $(NF-1)}')"
    rss="$(echo "$ps_info" | awk '{print $NF}')"
    dbg "process_stats: start_time=$start_time cpu_time=$cpu_time rss_kb=$rss"
  else
    dbg "process_stats: ps returned no data for pid=$pid"
  fi

  # Full lsof dump (for detailed analysis).
  # WARNING: lsof output may contain sensitive file paths — review before sharing externally.
  if [ -n "$fd_out" ]; then
    dbg "lsof_dump_start pid=$pid"
    while IFS= read -r fd_line; do
      dbg "  $fd_line"
    done <<< "$fd_out"
    dbg "lsof_dump_end pid=$pid"
  fi

  # Create dedup marker.
  touch "$marker" 2>/dev/null || true
  documented_count=$((documented_count + 1))

  dbg "--- End pid=$pid ---"
done

dbg "=== Watchdog scan complete ==="
echo "documented $documented_count new, ${#zombies[@]} total detached Claude process(es) — see $_LOG_FILE"
exit 0
