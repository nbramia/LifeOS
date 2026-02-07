#!/bin/bash
# Monitor Slack sync progress every 20 minutes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/slack_sync.log"
MONITOR_LOG="$PROJECT_DIR/logs/slack_sync_monitor.log"

echo "=== Slack Sync Monitor Started at $(date) ===" | tee -a "$MONITOR_LOG"
echo "Checking every 20 minutes. Ctrl+C to stop." | tee -a "$MONITOR_LOG"
echo "" | tee -a "$MONITOR_LOG"

check_status() {
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📊 Status Check at $(date '+%Y-%m-%d %H:%M:%S')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Check if sync process is running
    SYNC_PID=$(pgrep -f "slack_sync|run_slack_sync" | head -1)
    if [ -n "$SYNC_PID" ]; then
        echo "⚙️  Sync process: RUNNING (PID: $SYNC_PID)"
    else
        echo "⏹️  Sync process: NOT RUNNING"
    fi

    # Get index status from API
    STATUS=$(curl -s http://localhost:8000/api/slack/status 2>/dev/null)
    if [ -n "$STATUS" ]; then
        MSGS=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('indexed_messages', 0))" 2>/dev/null)
        echo "📨 Messages indexed: $MSGS"
    else
        echo "❌ Could not reach API"
    fi

    # Show last activity from sync log
    if [ -f "$LOG_FILE" ]; then
        CHANNELS=$(grep -c "messages from" "$LOG_FILE" 2>/dev/null || echo "0")
        ERRORS=$(grep -c "Error\|error" "$LOG_FILE" 2>/dev/null || echo "0")
        echo "📁 Channels processed: ~$CHANNELS"
        echo "⚠️  Errors: $ERRORS"
        echo ""
        echo "📝 Last 3 log entries:"
        tail -3 "$LOG_FILE" | sed 's/^/   /'
    fi

    # Check if sync completed
    if grep -q "Sync complete" "$LOG_FILE" 2>/dev/null; then
        echo ""
        echo "✅ SYNC COMPLETED!"
        grep "Sync complete" "$LOG_FILE" | tail -1
        return 1
    fi

    echo ""
    return 0
}

# Initial check
check_status | tee -a "$MONITOR_LOG"

# Loop every 20 minutes
while true; do
    sleep 1200  # 20 minutes
    check_status | tee -a "$MONITOR_LOG"

    # Exit if sync completed
    if [ $? -eq 1 ]; then
        echo "Monitor exiting - sync completed." | tee -a "$MONITOR_LOG"
        exit 0
    fi
done
