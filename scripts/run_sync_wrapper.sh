#!/bin/bash
# Wrapper for run_all_syncs.py that ensures NVMe/Homebrew is accessible.
#
# /opt/homebrew is a symlink to the NVMe external drive. At 3 AM (when launchd
# runs the nightly sync), the drive may be asleep or unmounted, which breaks
# the entire Python venv since it symlinks through Homebrew.
#
# This wrapper:
# 1. Wakes the NVMe by touching the mount point
# 2. Verifies the Python venv can start
# 3. Sends a Telegram alert if it can't, so failures aren't silent
# 4. Execs into Python if everything is OK

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFEOS_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$HOME/.venvs/lifeos/bin/python"
SYNC_SCRIPT="$LIFEOS_DIR/scripts/run_all_syncs.py"
LOG="$LIFEOS_DIR/logs/crm-sync-error.log"

# Maximum wall-clock runtime (6 hours). If the sync process hangs for any
# reason, this watchdog kills it so launchd can fire the next night's run.
MAX_RUNTIME=21600

# --- Free RAM before the heavy sync (opt-in) -----------------------------------
# The embedding/reindex phase co-resident with a memory-hungry desktop app (e.g.
# Chrome, tens of GB across tabs) has pushed this host into an OOM cascade that
# killed the desktop. Operators can list process-name patterns to gracefully
# quit first via LIFEOS_SYNC_QUIT_PROCS (space-separated, default empty = no-op;
# injected from .env by the systemd unit's EnvironmentFile). Reopen them later —
# SIGTERM lets the app save its session so tabs are restored.
if [ -n "${LIFEOS_SYNC_QUIT_PROCS:-}" ]; then
    LOG="$LIFEOS_DIR/logs/crm-sync-error.log"
    for proc in $LIFEOS_SYNC_QUIT_PROCS; do
        if pkill -TERM -f "$proc" 2>/dev/null; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Quit '$proc' to free RAM for sync" >> "$LOG"
        fi
    done
fi

# --- NVMe pre-flight check ---

MAX_RETRIES=3
RETRY_DELAY=5

for i in $(seq 1 $MAX_RETRIES); do
    # On macOS, wake the NVMe by listing the Homebrew directory
    if [[ "$(uname)" == "Darwin" ]]; then
        ls /opt/homebrew/bin > /dev/null 2>&1
    fi

    # Test that the venv Python can actually start and import dotenv
    if "$PYTHON" -c "from dotenv import load_dotenv" 2>/dev/null; then
        # Everything works - hand off to Python
        # LIFEOS_HEADLESS prevents Google OAuth from blocking on browser flow
        export LIFEOS_HEADLESS=true

        # Run Python in the background so we can start a watchdog alongside it.
        # When Python exits normally, we kill the watchdog to prevent it from
        # firing kill on a recycled PID hours later.
        "$PYTHON" "$SYNC_SCRIPT" "$@" &
        SYNC_PID=$!

        # Watchdog: SIGTERM after MAX_RUNTIME, SIGKILL 60s later as backstop.
        (
            sleep "$MAX_RUNTIME"
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Killing stuck sync (PID $SYNC_PID) after ${MAX_RUNTIME}s" >> "$LOG"
            kill -TERM "$SYNC_PID" 2>/dev/null
            sleep 60
            kill -KILL "$SYNC_PID" 2>/dev/null
        ) &
        WATCHDOG_PID=$!

        # Wait for sync to finish, then clean up watchdog
        wait "$SYNC_PID"
        EXIT_CODE=$?
        kill "$WATCHDOG_PID" 2>/dev/null
        wait "$WATCHDOG_PID" 2>/dev/null
        exit "$EXIT_CODE"
    fi

    echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Python/NVMe not ready (attempt $i/$MAX_RETRIES)" >> "$LOG"
    sleep "$RETRY_DELAY"
done

# --- All retries failed - alert and exit ---

MSG="$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] CRITICAL: Nightly sync cannot start - NVMe/Homebrew unavailable after $MAX_RETRIES retries"
echo "$MSG" >> "$LOG"

# Send Telegram alert so this isn't a silent failure
ENV_FILE="$LIFEOS_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
    CHAT_ID=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2)

    if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
        /usr/bin/curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=$(printf '🚨 *LifeOS Sync Failed*\n\nNVMe drive not accessible at 3 AM.\nPython venv cannot start — Homebrew is on the NVMe.\n\nCheck if the drive is mounted and awake.')" \
            -d "parse_mode=Markdown" > /dev/null 2>&1
    fi
fi

exit 1
