#!/usr/bin/env bash
# Nightly LifeOS -> Notion People sync (field updates, LifeOS_ID, new pages).
# Skips cleanly when LIFEOS_NOTION_TOKEN is not configured. Always executes
# (never a dry run) with at most 100 field updates and 25 new pages per night.
set -u
LIFEOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LIFEOS_VENV_PYTHON:-$HOME/.venvs/lifeos/bin/python}"
LOG_DIR="${LIFEOS_LOG_DIR:-$LIFEOS_DIR/logs}"
LOG="$LOG_DIR/notion-people-sync.log"

mkdir -p "$LOG_DIR"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

cd "$LIFEOS_DIR" || exit 1

# Settings read the environment first, then .env, so this sees the same value
# the sync script does.
# Only the exact stdout values 1 (present) and 0 (absent) are accepted; any
# other result (python missing, settings import error) is an error, not a skip.
errf=$(mktemp)
has_token=$("$PY" -c 'from config.settings import settings; print(1 if (settings.notion_token or "").strip() else 0)' 2>"$errf")
check_rc=$?
if [ $check_rc -ne 0 ] || { [ "$has_token" != "1" ] && [ "$has_token" != "0" ]; }; then
  log "ERROR: could not check LIFEOS_NOTION_TOKEN (python exit $check_rc): $(tail -n 3 "$errf" | cut -c1-200 | tr '\n' ' ')"
  rm -f "$errf"
  exit 1
fi
rm -f "$errf"
if [ "$has_token" = "0" ]; then
  log "LIFEOS_NOTION_TOKEN not set; skipping"
  exit 0
fi

log "start"
# --no-file-log: stdout is appended to the same log below.
"$PY" scripts/sync_notion_people.py --execute --max-writes 100 --max-creates 25 --no-file-log >> "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then log "ERROR: sync exited $rc"; exit $rc; fi
log "done"
