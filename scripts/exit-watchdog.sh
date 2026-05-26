#!/usr/bin/env bash
# exit-watchdog.sh — Monitor a Claude process after exit is initiated.
#
# Polls the target process at increasing intervals. If the process is still
# alive after the grace period, captures detailed diagnostics to help identify
# what is preventing shutdown.
#
# Usage: exit-watchdog.sh <claude_pid> <trigger> [worktree_name]
#   claude_pid   — PID of the Claude process to monitor
#   trigger      — What launched this watchdog: "SessionEnd" or "WorktreeRemove"
#   worktree_name — Optional worktree name for log context
#
# This script is designed to be run in the background (stdout/stderr detached).
# It is fire-and-forget: the caller should not wait for it.
#
# Logs to ~/.claude/logs/exit-watchdog.log.

set +e

# --- Args ---
TARGET_PID="${1:-}"
TRIGGER="${2:-unknown}"
WORKTREE_NAME="${3:-}"

if [ -z "$TARGET_PID" ] || ! [[ "$TARGET_PID" =~ ^[0-9]+$ ]]; then
  exit 1
fi

# Don't monitor ourselves or init.
[ "$TARGET_PID" -le 1 ] && exit 0

# --- Logging ---
_LOG_DIR="$HOME/.claude/logs"
mkdir -p "$_LOG_DIR" 2>/dev/null || true
_LOG_FILE="$_LOG_DIR/exit-watchdog.log"

# Rotate at ~256 KB.
if [ -f "$_LOG_FILE" ] && [ "$(wc -c < "$_LOG_FILE" 2>/dev/null)" -gt 262144 ]; then
  tail -c 131072 "$_LOG_FILE" > "$_LOG_FILE.tmp" && mv "$_LOG_FILE.tmp" "$_LOG_FILE"
fi

dbg() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$_LOG_FILE" 2>/dev/null || true; }

# --- Deduplication ---
# Only one watchdog per PID. Use a lockfile to prevent duplicates from
# SessionEnd and WorktreeRemove both launching one.
_LOCK_DIR="$_LOG_DIR/.watchdog-locks"
mkdir -p "$_LOCK_DIR" 2>/dev/null || true
_LOCK_FILE="$_LOCK_DIR/pid-$TARGET_PID"

if ! mkdir "$_LOCK_FILE" 2>/dev/null; then
  # Another watchdog is already monitoring this PID.
  dbg "watchdog: duplicate suppressed for pid=$TARGET_PID trigger=$TRIGGER"
  exit 0
fi

# Ensure lock is cleaned up on exit.
cleanup() { rm -rf "$_LOCK_FILE" 2>/dev/null || true; }
trap cleanup EXIT

# --- Configuration ---
# Grace period: how long to wait before declaring the process hung (seconds).
GRACE_PERIOD=30
# Hard timeout: stop monitoring after this many seconds regardless (seconds).
HARD_TIMEOUT=300
# Poll intervals: start fast, back off. Each value is sleep duration in seconds.
POLL_INTERVALS=(2 2 3 3 5 5 10 10 15 15 30 30 30 30 30 30 30 30)

# --- Helpers ---
is_alive() { kill -0 "$TARGET_PID" 2>/dev/null; }

capture_tcp() {
  # -a combines -p and -i filters with AND (without -a, lsof uses OR).
  lsof -a -p "$TARGET_PID" -i TCP 2>/dev/null | tail -n +2 || true
}

capture_children() {
  pgrep -P "$TARGET_PID" 2>/dev/null | tr '\n' ' ' || true
}

capture_fd_summary() {
  local fd_out
  fd_out="$(lsof -p "$TARGET_PID" 2>/dev/null)" || fd_out=""
  if [ -n "$fd_out" ]; then
    local total revoked tcp unix_sock
    total="$(echo "$fd_out" | tail -n +2 | wc -l | tr -d ' ')"
    revoked="$(echo "$fd_out" | grep -c 'revoked' || true)"
    tcp="$(echo "$fd_out" | grep -c 'TCP' || true)"
    unix_sock="$(echo "$fd_out" | grep -c 'unix' || true)"
    echo "total=$total revoked=$revoked tcp=$tcp unix=$unix_sock"
  else
    echo "lsof_failed"
  fi
}

capture_child_details() {
  local children
  children="$(pgrep -P "$TARGET_PID" 2>/dev/null)" || return
  for cpid in $children; do
    local info
    info="$(ps -p "$cpid" -o pid=,comm=,stat= 2>/dev/null)" || continue
    local cwd
    cwd="$(lsof -a -d cwd -p "$cpid" -Fn 2>/dev/null | grep '^n' | sed 's/^n//')" || cwd="unknown"
    dbg "  child: $info cwd=$cwd"
  done
}

# --- Main loop ---
dbg "=== Exit watchdog started: pid=$TARGET_PID trigger=$TRIGGER worktree=${WORKTREE_NAME:-none} ==="

start_time="$(date +%s)"
poll_index=0
exceeded_grace=false

while true; do
  elapsed=$(( $(date +%s) - start_time ))

  # Hard timeout — stop monitoring.
  if [ "$elapsed" -ge "$HARD_TIMEOUT" ]; then
    if is_alive; then
      dbg "HARD_TIMEOUT: pid=$TARGET_PID still alive after ${elapsed}s — stopping watchdog"
      dbg "  final_tcp: $(capture_tcp | tr '\n' '; ')"
      dbg "  final_children: $(capture_children)"
      dbg "  final_fds: $(capture_fd_summary)"
    fi
    dbg "=== Exit watchdog finished: pid=$TARGET_PID elapsed=${elapsed}s ==="
    break
  fi

  # Check if process exited.
  if ! is_alive; then
    dbg "CLEAN_EXIT: pid=$TARGET_PID exited after ${elapsed}s"
    dbg "=== Exit watchdog finished: pid=$TARGET_PID elapsed=${elapsed}s ==="
    break
  fi

  # Process still alive — log state at key transitions.
  if [ "$elapsed" -ge "$GRACE_PERIOD" ] && [ "$exceeded_grace" = false ]; then
    exceeded_grace=true
    dbg "HUNG_DETECTED: pid=$TARGET_PID still alive after ${elapsed}s (grace=${GRACE_PERIOD}s)"

    # Detailed diagnostic dump at the moment we declare it hung.
    dbg "--- Hung diagnostic dump ---"

    # TCP connections (key signal: is it stuck on API or LSP?)
    tcp_out="$(capture_tcp)"
    if [ -n "$tcp_out" ]; then
      dbg "tcp_connections:"
      while IFS= read -r line; do
        dbg "  $line"
      done <<< "$tcp_out"
    else
      dbg "tcp_connections: none"
    fi

    # Child processes with details.
    children="$(capture_children)"
    dbg "children: ${children:-none}"
    if [ -n "$children" ]; then
      capture_child_details
    fi

    # FD summary.
    dbg "fd_summary: $(capture_fd_summary)"

    # Process stats.
    ps_info="$(ps -p "$TARGET_PID" -o pid=,stat=,cputime=,rss=,etime= 2>/dev/null)" || ps_info=""
    dbg "process_stats: $ps_info"

    # macOS: sample the process for 1 second to capture stack state.
    if command -v sample >/dev/null 2>&1; then
      sample_file="$_LOG_DIR/exit-watchdog-sample-${TARGET_PID}-$(date +%s).txt"
      if sample "$TARGET_PID" 1 -f "$sample_file" 2>/dev/null; then
        dbg "stack_sample: saved to $sample_file"
      else
        dbg "stack_sample: sample command failed"
      fi
    fi

    dbg "--- End hung diagnostic dump ---"
  elif [ "$exceeded_grace" = true ]; then
    # Periodic check-in after the initial hung dump.
    dbg "STILL_HUNG: pid=$TARGET_PID elapsed=${elapsed}s tcp=$(capture_tcp | wc -l | tr -d ' ') children=$(capture_children)"
  fi

  # Sleep with backoff.
  if [ "$poll_index" -lt "${#POLL_INTERVALS[@]}" ]; then
    sleep "${POLL_INTERVALS[$poll_index]}"
  else
    sleep 30
  fi
  poll_index=$((poll_index + 1))
done

exit 0
