#!/usr/bin/env bash
# session-exit-diag.sh — Capture diagnostic data when a Claude Code session ends.
#
# Called as a SessionEnd hook. Always exits 0 to avoid blocking session teardown.
# I/O: All output goes to log file; stdout/stderr are silent.
# Logs to ~/.claude/logs/session-exit-diag.log.

# Swallow all errors — diagnostics must never block session exit.
set +e

_LOG_DIR="$HOME/.claude/logs"
mkdir -p "$_LOG_DIR" 2>/dev/null || true
_LOG_FILE="$_LOG_DIR/session-exit-diag.log"

# Rotate at ~128 KB.
if [ -f "$_LOG_FILE" ] && [ "$(wc -c < "$_LOG_FILE" 2>/dev/null)" -gt 131072 ]; then
  tail -c 65536 "$_LOG_FILE" > "$_LOG_FILE.tmp" && mv "$_LOG_FILE.tmp" "$_LOG_FILE"
fi

dbg() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$_LOG_FILE" 2>/dev/null || true; }

claude_pid="${PPID:-unknown}"

dbg "=== SessionEnd diag (pid=$$ claude_pid=$claude_pid) ==="

# FD summary for the Claude process.
if [ "$claude_pid" != "unknown" ] && [ -n "$claude_pid" ]; then
  fd_out="$(lsof -p "$claude_pid" 2>/dev/null)" || fd_out=""
  if [ -n "$fd_out" ]; then
    revoked="$(echo "$fd_out" | grep -c 'revoked' || true)"
    kqueue="$(echo "$fd_out" | grep -c 'KQUEUE' || true)"
    unix_sock="$(echo "$fd_out" | grep -c 'unix' || true)"
    skill_watcher="$(echo "$fd_out" | grep -c 'skill' || true)"
    total_fds="$(echo "$fd_out" | tail -n +2 | wc -l | tr -d ' ')"
    dbg "fd_summary: total=$total_fds revoked=$revoked kqueue=$kqueue unix_socket=$unix_sock skill_watcher=$skill_watcher"
  else
    dbg "fd_summary: lsof returned no data for pid=$claude_pid"
  fi

  # Child processes.
  children="$(pgrep -P "$claude_pid" 2>/dev/null | tr '\n' ' ')" || children=""
  dbg "children: ${children:-none}"
else
  dbg "fd_summary: claude_pid unavailable"
fi

# OTEL env var state.
dbg "otel_env: BSP_EXPORT_TIMEOUT=${OTEL_BSP_EXPORT_TIMEOUT:-unset} OTLP_TIMEOUT=${OTEL_EXPORTER_OTLP_TIMEOUT:-unset}"

# Worktree info (if running inside a worktree session).
if [ -f ".worktree-info" ]; then
  dbg "worktree_info: $(cat .worktree-info 2>/dev/null | tr '\n' ' ')"
else
  dbg "worktree_info: not in a worktree session"
fi

dbg "=== SessionEnd diag complete ==="

# Launch exit watchdog in background to monitor if the Claude process actually terminates.
# This covers the case where shutdown stalls *after* SessionEnd fires.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$claude_pid" != "unknown" ] && [ -n "$claude_pid" ] && [ -x "$_SCRIPT_DIR/exit-watchdog.sh" ]; then
  worktree_name=""
  [ -f ".worktree-info" ] && worktree_name="$(grep '^NAME=' .worktree-info 2>/dev/null | cut -d= -f2)"
  nohup "$_SCRIPT_DIR/exit-watchdog.sh" "$claude_pid" "SessionEnd" "$worktree_name" \
    </dev/null >/dev/null 2>&1 &
  dbg "exit_watchdog: launched for pid=$claude_pid (watchdog_pid=$!)"
fi

exit 0
