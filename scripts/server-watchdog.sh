#!/bin/bash
# LifeOS Server Watchdog
# Run via cron every 5 minutes to detect and fix server issues:
#   - Duplicate uvicorn processes (causes Telegram 409 errors)
#   - Unresponsive server (stale after long syncs)
#
# Cron entry:
#   */5 * * * * /Applications/LifeOS.app/Contents/MacOS/LifeOS server-watchdog

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HEALTH_URL="http://localhost:8000/health"
LOG_FILE="$PROJECT_DIR/logs/server-watchdog.log"
FAILURE_COUNT_FILE="$PROJECT_DIR/logs/server-watchdog-failures.count"
ENV_FILE="$PROJECT_DIR/.env"
ALERT_THRESHOLD=2
ALERT_INTERVAL=6  # every 6th check = every 30 min at 5-min cron

mkdir -p "$PROJECT_DIR/logs"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

get_failure_count() {
    if [ -f "$FAILURE_COUNT_FILE" ]; then
        cat "$FAILURE_COUNT_FILE"
    else
        echo "0"
    fi
}

set_failure_count() {
    echo "$1" > "$FAILURE_COUNT_FILE"
}

send_telegram() {
    local message="$1"
    if [ -f "$ENV_FILE" ]; then
        local bot_token chat_id
        bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
        chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2)
        if [ -n "$bot_token" ] && [ -n "$chat_id" ]; then
            /usr/bin/curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
                -d "chat_id=${chat_id}" \
                -d "text=${message}" \
                -d "parse_mode=Markdown" > /dev/null 2>&1 || true
        fi
    fi
}

restart_server() {
    log "Restarting server via server.sh..."
    if "$SCRIPT_DIR/server.sh" restart >> "$LOG_FILE" 2>&1; then
        log "Server restarted successfully"
        return 0
    else
        log "ERROR: Server restart failed"
        return 1
    fi
}

# --- Check 1: Duplicate processes ---
# Count distinct PIDs listening on port 8000 (avoids matching SSH/shell args).
# Normal state: 1 master PID listening. >1 means conflicting server instances.
LISTENER_COUNT=$(lsof -ti :8000 2>/dev/null | sort -u | wc -l | tr -d ' ')

if [ "$LISTENER_COUNT" -gt 1 ]; then
    log "DUPLICATE DETECTED: $LISTENER_COUNT processes listening on :8000 — restarting"
    restart_server
    # Count this as a failure for alerting purposes
    FAILURES=$(get_failure_count)
    FAILURES=$((FAILURES + 1))
    set_failure_count "$FAILURES"

    if [ "$FAILURES" -eq "$ALERT_THRESHOLD" ] || { [ "$FAILURES" -gt "$ALERT_THRESHOLD" ] && [ $(( (FAILURES - ALERT_THRESHOLD) % ALERT_INTERVAL )) -eq 0 ]; }; then
        send_telegram "$(printf '⚠️ *LifeOS Server Watchdog*\n\nDuplicate uvicorn processes detected and killed.\nConsecutive issues: %d\nServer has been restarted.' "$FAILURES")"
    fi
    exit 0
fi

# --- Check 2: Health check ---
if curl -s -f --max-time 10 "$HEALTH_URL" > /dev/null 2>&1; then
    # Healthy — reset failure counter and notify recovery if we were alerting
    PREV_FAILURES=$(get_failure_count)
    if [ "$PREV_FAILURES" -ge "$ALERT_THRESHOLD" ]; then
        log "Server recovered after $PREV_FAILURES consecutive failures"
        send_telegram "$(printf '✅ *LifeOS Server Recovered*\n\nServer is healthy again after %d consecutive failures.' "$PREV_FAILURES")"
    fi
    set_failure_count 0
    exit 0
fi

# --- Server unhealthy ---
FAILURES=$(get_failure_count)
FAILURES=$((FAILURES + 1))
set_failure_count "$FAILURES"

log "Health check failed (consecutive: $FAILURES)"

# Try to restart
if restart_server; then
    # Restart succeeded but still counts as a failure event
    if [ "$FAILURES" -eq "$ALERT_THRESHOLD" ] || { [ "$FAILURES" -gt "$ALERT_THRESHOLD" ] && [ $(( (FAILURES - ALERT_THRESHOLD) % ALERT_INTERVAL )) -eq 0 ]; }; then
        send_telegram "$(printf '⚠️ *LifeOS Server Watchdog*\n\nServer was unresponsive and has been restarted.\nConsecutive failures: %d' "$FAILURES")"
    fi
else
    # Restart failed
    if [ "$FAILURES" -eq "$ALERT_THRESHOLD" ] || { [ "$FAILURES" -gt "$ALERT_THRESHOLD" ] && [ $(( (FAILURES - ALERT_THRESHOLD) % ALERT_INTERVAL )) -eq 0 ]; }; then
        send_telegram "$(printf '🚨 *LifeOS Server Down*\n\nServer is unresponsive and restart FAILED.\nConsecutive failures: %d\nManual intervention required.' "$FAILURES")"
    fi
fi
