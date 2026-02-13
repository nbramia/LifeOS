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

# --- NVMe pre-flight check ---

MAX_RETRIES=3
RETRY_DELAY=5

for i in $(seq 1 $MAX_RETRIES); do
    # Try to wake the NVMe by listing the Homebrew directory
    ls /opt/homebrew/bin > /dev/null 2>&1

    # Test that the venv Python can actually start and import dotenv
    if "$PYTHON" -c "from dotenv import load_dotenv" 2>/dev/null; then
        # Everything works - hand off to Python
        exec "$PYTHON" "$SYNC_SCRIPT" "$@"
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
