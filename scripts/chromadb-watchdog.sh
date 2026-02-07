#!/bin/bash
# ChromaDB Watchdog Script
# Run via cron every 5 minutes to auto-restart ChromaDB if it crashes
#
# Add to crontab with: crontab -e
# */5 * * * * /path/to/LifeOS/scripts/chromadb-watchdog.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HEALTH_URL="http://localhost:8001/api/v2/heartbeat"
LOG_FILE="$PROJECT_DIR/logs/chromadb-watchdog.log"
CHROMADB_SCRIPT="$SCRIPT_DIR/chromadb.sh"

# Log function
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Check if chromadb is responding
if curl -s --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    # Healthy - do nothing
    exit 0
fi

# Not healthy - try to restart
log "ChromaDB not responding, attempting restart"

# Run the start script
"$CHROMADB_SCRIPT" restart >> "$LOG_FILE" 2>&1

# Check if it started successfully
sleep 5
if curl -s --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    log "ChromaDB restarted successfully"
else
    log "ERROR: ChromaDB restart failed"
fi
