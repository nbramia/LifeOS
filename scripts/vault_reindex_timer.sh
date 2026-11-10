#!/usr/bin/env bash
# Incremental vault reindex for the timer: indexes notes that changed since the last run.
# Notes edited from Windows (Obsidian, the Claude app, OneDrive sync) do not trigger the file
# watcher inside WSL, so a periodic pass picks them up. Summaries are skipped to keep the pass light.
set -u
LIFEOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LIFEOS_VENV_PYTHON:-$HOME/.venvs/lifeos/bin/python}"
LOG="$LIFEOS_DIR/logs/vault-reindex.log"
LOCK="${XDG_RUNTIME_DIR:-/tmp}/lifeos-vault-reindex.lock"

cd "$LIFEOS_DIR" || exit 1
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') another vault reindex is running; skipping" >> "$LOG"
  exit 0
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') start" >> "$LOG"
"$PY" scripts/sync_vault_reindex.py --execute --skip-summaries >> "$LOG" 2>&1
rc=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') done (exit $rc)" >> "$LOG"
exit $rc
