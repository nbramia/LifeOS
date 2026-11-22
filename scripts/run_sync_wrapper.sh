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
# 4. Runs Python under a runtime watchdog
# 5. Sends a Telegram alert if Python exits non-zero before its summary step
#    (killed, crashed), since the sync's own summary never goes out then

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFEOS_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$HOME/.venvs/lifeos/bin/python"
SYNC_SCRIPT="$LIFEOS_DIR/scripts/run_all_syncs.py"
LOG="$LIFEOS_DIR/logs/crm-sync-error.log"

# Maximum wall-clock runtime (6 hours). If the sync process hangs for any
# reason, this watchdog kills it so launchd can fire the next night's run.
MAX_RUNTIME=21600

ENV_FILE="$LIFEOS_DIR/.env"

# Send a Telegram alert from the primary bot and copy it to the bot-notices log
# (api/services/bot_notices.py). Best-effort: never fails the wrapper.
send_alert() {
    local text="$1" bot_token chat_id
    [ -f "$ENV_FILE" ] || return 0
    bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
    chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2)
    [ -n "$bot_token" ] && [ -n "$chat_id" ] || return 0
    "${LIFEOS_WATCHDOG_CURL:-/usr/bin/curl}" -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=Markdown" > /dev/null 2>&1 || true
    printf '%s' "$text" | python3 -I "$LIFEOS_DIR/api/services/bot_notices.py" "LifeOS/run_sync_wrapper" > /dev/null 2>&1 || true
}

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
        # run_all_syncs.py touches this file once it reaches its summary step.
        SUMMARY_MARKER="$(mktemp -u "${TMPDIR:-/tmp}/lifeos-sync-summary.XXXXXX")"
        export LIFEOS_SYNC_SUMMARY_MARKER="$SUMMARY_MARKER"

        "$PYTHON" "$SYNC_SCRIPT" "$@" &
        SYNC_PID=$!

        # Watchdog: SIGTERM after MAX_RUNTIME, SIGKILL 60s later as backstop.
        #
        # `sleep` returns early if the subshell catches a signal, and the next
        # line then kills a sync that has run for minutes rather than hours.
        # That is not hypothetical: on 2026-08-15 this killed the nightly run
        # 31 minutes in and logged it as a 21600s timeout, taking out
        # relationship_discovery and every phase after it. So re-check the wall
        # clock and go back to sleep unless the deadline has genuinely passed.
        WATCH_START=$(date +%s)
        (
            while :; do
                sleep "$MAX_RUNTIME"
                ELAPSED=$(( $(date +%s) - WATCH_START ))
                if [ "$ELAPSED" -ge "$MAX_RUNTIME" ]; then
                    break
                fi
                # Woken early — log it, since a signal reaching this subshell
                # means something is stray-signalling the sync's process group.
                echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Watchdog woke early at ${ELAPSED}s (limit ${MAX_RUNTIME}s) — not killing" >> "$LOG"
            done
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Killing stuck sync (PID $SYNC_PID) after ${ELAPSED}s" >> "$LOG"
            kill -TERM "$SYNC_PID" 2>/dev/null
            sleep 60
            kill -KILL "$SYNC_PID" 2>/dev/null
        ) &
        WATCHDOG_PID=$!

        # Wait for sync to finish, then clean up watchdog
        # systemd stops the unit (its own runtime limit, a manual stop) by
        # sending SIGTERM to every process in it. Catch it here so the wrapper
        # outlives the sync long enough to report it; the sync handles its own.
        trap 'true' TERM
        wait "$SYNC_PID"
        EXIT_CODE=$?
        # A trapped signal ends `wait` early; keep waiting for the real status.
        while kill -0 "$SYNC_PID" 2>/dev/null; do
            wait "$SYNC_PID"
            EXIT_CODE=$?
        done
        kill "$WATCHDOG_PID" 2>/dev/null
        wait "$WATCHDOG_PID" 2>/dev/null

        # A non-zero exit after the summary step is a sync with failed
        # sources, which the summary already reported. Before it, nothing
        # has told the operator the run stopped.
        if [ "$EXIT_CODE" -ne 0 ] && [ ! -e "$SUMMARY_MARKER" ]; then
            ELAPSED=$(( $(date +%s) - WATCH_START ))
            HOW="exited with code $EXIT_CODE"
            if [ "$EXIT_CODE" -gt 128 ]; then
                HOW="was killed by signal $(( EXIT_CODE - 128 ))"
                # systemd's limit counts from unit start, a little before this clock.
                [ "$ELAPSED" -ge $(( MAX_RUNTIME - 600 )) ] && HOW="$HOW at the 6-hour runtime limit"
            fi
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Sync $HOW after ${ELAPSED}s, before its summary" >> "$LOG"
            send_alert "$(printf '🚨 *LifeOS Sync did not finish*\n\nThe sync %s after %dm %ds, before sending its summary.\n\nCheck journalctl -u lifeos-sync.service and logs/crm-sync-error.log.' "$HOW" $(( ELAPSED / 60 )) $(( ELAPSED % 60 )))"
        fi
        rm -f "$SUMMARY_MARKER"
        exit "$EXIT_CODE"
    fi

    echo "$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] Python/NVMe not ready (attempt $i/$MAX_RETRIES)" >> "$LOG"
    sleep "$RETRY_DELAY"
done

# --- All retries failed - alert and exit ---

MSG="$(date '+%Y-%m-%d %H:%M:%S') [WRAPPER] CRITICAL: Nightly sync cannot start - NVMe/Homebrew unavailable after $MAX_RETRIES retries"
echo "$MSG" >> "$LOG"

# Send Telegram alert so this isn't a silent failure
send_alert "$(printf '🚨 *LifeOS Sync Failed*\n\nNVMe drive not accessible at 3 AM.\nPython venv cannot start — Homebrew is on the NVMe.\n\nCheck if the drive is mounted and awake.')"

exit 1
