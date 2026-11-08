#!/usr/bin/env bash
# Nightly Google Messages import (gmcli archive -> LifeOS).
# Runs after the 3:30 AM sync. Dry run is never used here: this always executes.
set -u
LIFEOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LIFEOS_VENV_PYTHON:-$HOME/.venvs/lifeos/bin/python}"
LOG="$LIFEOS_DIR/logs/gmessages-import.log"
ARCHIVE_GLOB=/mnt/c/Users/*/.local/state/gmcli/gmcli.db
STALE_HOURS=26

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

cd "$LIFEOS_DIR" || exit 1
log "start"

# 1. Staleness check: the archive's last event time. The live sync should
#    write events all day; a gap longer than STALE_HOURS means it stopped.
archive=$(ls $ARCHIVE_GLOB 2>/dev/null | head -1)
if [ -z "$archive" ]; then
  log "WARN: no gmcli archive found at $ARCHIVE_GLOB; skipping import"
  exit 0
fi
# Read a copy: the live sync writes this file from Windows, and reading it
# in place from WSL can fail with a disk I/O error.
snap=$(mktemp -d)
cp "$archive" "$snap/gmcli.db" 2>/dev/null
cp "${archive}-wal" "$snap/gmcli.db-wal" 2>/dev/null
cp "${archive}-shm" "$snap/gmcli.db-shm" 2>/dev/null
last_ms=$("$PY" - "$snap/gmcli.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
row = c.execute("select last_event_ts from sync_state where id=1").fetchone()
print(row[0] if row and row[0] else 0)
PY
)
rm -rf "$snap"
last_ms=${last_ms:-0}
now_ms=$(( $(date +%s) * 1000 ))
age_h=$(( (now_ms - last_ms) / 3600000 ))
if [ "$last_ms" -eq 0 ] || [ "$age_h" -gt "$STALE_HOURS" ]; then
  log "WARN: no Google Messages event for ${age_h}h (limit ${STALE_HOURS}h). Live sync is probably stopped or unpaired; the import will still run on what is stored."
else
  log "archive last event ${age_h}h ago"
fi

# 2. Import (executes; backs up crm.db and interactions.db first).
"$PY" scripts/gmcli_import.py --execute --min-messages 2 >> "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then log "ERROR: import exited $rc"; exit $rc; fi
log "import done"

# 3. Strengths, if the API is up. Non-fatal: the import itself has already run.
if curl -s -m 5 -o /dev/null http://127.0.0.1:8000/health; then
  curl -s -m 280 -X POST http://127.0.0.1:8000/api/crm/strengths/update >> "$LOG" 2>&1 && log "strengths refreshed"
else
  log "WARN: API not running; strengths not refreshed"
fi

# 4. Keep the newest 7 import backups; they are full copies of the CRM.
ls -1dt data/backups/pre-gmcli-import-* 2>/dev/null | tail -n +8 | while read -r old; do
  rm -rf -- "$old" && log "pruned $old"
done
log "done"
