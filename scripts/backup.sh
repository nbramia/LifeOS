#!/bin/bash
# LifeOS Backup Script
# ====================
# Creates daily backups of all SQLite databases and critical config files.
# Uses SQLite's .backup command for safe, consistent database copies.
# Keeps 7 daily backups with automatic rotation.
#
# Usage: ./scripts/backup.sh
#
# Designed to run via cron at 4:00 AM (after the 3:00 AM sync completes).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJECT_DIR/data/backups"
DATE=$(date +%Y-%m-%d)
TODAY_DIR="$BACKUP_DIR/$DATE"
KEEP_DAYS=7
SQLITE="sqlite3"
LOG_FILE="$PROJECT_DIR/logs/backup.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Ensure directories exist
mkdir -p "$TODAY_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log "Starting LifeOS backup to $TODAY_DIR"

# Track success/failure
FAILED=0
BACKED_UP=0

# Backup a SQLite database using the .backup command
backup_db() {
    local src="$1"
    local name="$2"

    if [ ! -f "$src" ]; then
        log "  SKIP $name (file not found: $src)"
        return
    fi

    # Skip empty databases
    local size=$(stat -f%z "$src" 2>/dev/null || echo "0")
    if [ "$size" -eq 0 ]; then
        return
    fi

    local dst="$TODAY_DIR/$name"
    if $SQLITE "$src" ".backup '$dst'" 2>>"$LOG_FILE"; then
        local dst_size=$(stat -f%z "$dst" 2>/dev/null || echo "?")
        log "  OK   $name ($(numfmt_bytes $dst_size))"
        BACKED_UP=$((BACKED_UP + 1))
    else
        log "  FAIL $name"
        FAILED=$((FAILED + 1))
    fi
}

# Backup a regular file
backup_file() {
    local src="$1"
    local name="$2"

    if [ ! -f "$src" ]; then
        log "  SKIP $name (not found)"
        return
    fi

    if cp "$src" "$TODAY_DIR/$name" 2>>"$LOG_FILE"; then
        log "  OK   $name"
        BACKED_UP=$((BACKED_UP + 1))
    else
        log "  FAIL $name"
        FAILED=$((FAILED + 1))
    fi
}

# Human-readable byte sizes
numfmt_bytes() {
    local bytes=$1
    if [ "$bytes" -ge 1073741824 ]; then
        echo "$(echo "scale=1; $bytes/1073741824" | bc)GB"
    elif [ "$bytes" -ge 1048576 ]; then
        echo "$(echo "scale=1; $bytes/1048576" | bc)MB"
    elif [ "$bytes" -ge 1024 ]; then
        echo "$(echo "scale=0; $bytes/1024" | bc)KB"
    else
        echo "${bytes}B"
    fi
}

# --- SQLite Databases ---
log "Backing up databases..."
backup_db "$PROJECT_DIR/data/crm.db" "crm.db"
backup_db "$PROJECT_DIR/data/interactions.db" "interactions.db"
backup_db "$PROJECT_DIR/data/imessage.db" "imessage.db"
backup_db "$PROJECT_DIR/data/conversations.db" "conversations.db"
backup_db "$PROJECT_DIR/data/sync_health.db" "sync_health.db"
backup_db "$PROJECT_DIR/data/usage.db" "usage.db"
backup_db "$PROJECT_DIR/data/bm25_index.db" "bm25_index.db"
backup_db "$PROJECT_DIR/data/gsheet_sync.db" "gsheet_sync.db"
backup_db "$HOME/.lifeos/cost_tracking.db" "cost_tracking.db"

# --- Config Files ---
log "Backing up config files..."
backup_file "$PROJECT_DIR/config/people_dictionary.json" "people_dictionary.json"
backup_file "$HOME/.lifeos/reminders.json" "reminders.json"
backup_file "$HOME/.lifeos/memories.json" "memories.json"

# --- Rotate old backups ---
log "Rotating backups (keeping $KEEP_DAYS days)..."
REMOVED=0
for dir in "$BACKUP_DIR"/????-??-??; do
    if [ -d "$dir" ] && [ "$dir" != "$TODAY_DIR" ]; then
        dir_date=$(basename "$dir")
        # Calculate age in days
        dir_epoch=$(date -j -f "%Y-%m-%d" "$dir_date" "+%s" 2>/dev/null || continue)
        now_epoch=$(date "+%s")
        age_days=$(( (now_epoch - dir_epoch) / 86400 ))
        if [ "$age_days" -ge "$KEEP_DAYS" ]; then
            rm -rf "$dir"
            REMOVED=$((REMOVED + 1))
            log "  Removed $dir_date ($age_days days old)"
        fi
    fi
done

# --- Clean up old log files ---
LOG_DIR="$PROJECT_DIR/logs"
LOG_KEEP_DAYS=30
LOGS_REMOVED=0
log "Cleaning old log files (keeping $LOG_KEEP_DAYS days)..."
for pattern in "sync_*.log" "fda_sync_*.log" "slack_sync_monitor*.log" "slack_catchup*.log"; do
    for f in "$LOG_DIR"/$pattern; do
        [ -f "$f" ] || continue
        # Use file modification time
        file_epoch=$(stat -f%m "$f" 2>/dev/null || continue)
        now_epoch=$(date "+%s")
        age_days=$(( (now_epoch - file_epoch) / 86400 ))
        if [ "$age_days" -ge "$LOG_KEEP_DAYS" ]; then
            rm "$f"
            LOGS_REMOVED=$((LOGS_REMOVED + 1))
        fi
    done
done
if [ "$LOGS_REMOVED" -gt 0 ]; then
    log "  Removed $LOGS_REMOVED old log files"
fi

# --- Summary ---
TOTAL_SIZE=$(du -sh "$TODAY_DIR" 2>/dev/null | awk '{print $1}')
log "Backup complete: $BACKED_UP files backed up, $FAILED failures, $REMOVED old backups removed, $LOGS_REMOVED old logs cleaned ($TOTAL_SIZE total)"

if [ "$FAILED" -gt 0 ]; then
    log "WARNING: $FAILED backup(s) failed!"
    exit 1
fi
