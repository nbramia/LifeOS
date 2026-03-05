#!/bin/bash
# Apple Data Agent — runs on Mac Mini to export Apple ecosystem data
# and sync it to the Linux server.
#
# This replaces the Mac Mini's role as the LifeOS server. Instead, it:
# 1. Exports contacts, iMessage, phone data to data/apple-exports/
# 2. Rsyncs exports to the Linux server at data/apple-imports/
#
# Schedule (Mac Mini crontab):
#   50 2 * * * /path/to/LifeOS/scripts/apple_data_agent.sh
#
# Prerequisites:
#   - Terminal.app has Full Disk Access
#   - SSH key to Linux server (no password prompt)
#   - rsync installed on both machines

set -euo pipefail

LIFEOS_DIR="${LIFEOS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="${LIFEOS_DIR}/../.venvs/lifeos/bin/python"
LINUX_SERVER="${LIFEOS_LINUX_HOST:?Set LIFEOS_LINUX_HOST to your Linux server IP}"
LINUX_USER="${LIFEOS_LINUX_USER:-$USER}"
LINUX_LIFEOS="${LIFEOS_LINUX_DIR:-/home/${LINUX_USER}/Code/LifeOS}"
LOG_DIR="${LIFEOS_DIR}/logs"
LOG_FILE="${LOG_DIR}/apple_agent_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "${LOG_DIR}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "${LOG_FILE}"
}

# Only run on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script only runs on macOS (Apple Data Agent)."
    exit 0
fi

# Check Python venv exists
if [[ ! -f "${PYTHON}" ]]; then
    # Try standard Mac Mini path
    PYTHON="${HOME}/.venvs/lifeos/bin/python"
fi

if [[ ! -f "${PYTHON}" ]]; then
    log "ERROR: Python venv not found. Expected at ~/.venvs/lifeos/"
    exit 1
fi

log "=== Apple Data Agent starting ==="
log "LifeOS dir: ${LIFEOS_DIR}"
log "Linux server: ${LINUX_USER}@${LINUX_SERVER}"

# -------------------------------------------------------------------
# Step 1: Export Apple data to data/apple-exports/
# The export script reads Apple DBs directly (requires FDA)
# -------------------------------------------------------------------
log "Step 1: Exporting Apple data..."

cd "${LIFEOS_DIR}"
if "${PYTHON}" scripts/apple_data_export.py --execute >> "${LOG_FILE}" 2>&1; then
    log "Export: OK"
else
    log "Export: FAILED"
    # Continue anyway — rsync whatever we have
fi

# -------------------------------------------------------------------
# Step 2: Rsync exports to Linux server
# -------------------------------------------------------------------
log "Step 2: Syncing to Linux server..."

EXPORT_DIR="${LIFEOS_DIR}/data/apple-exports/"
IMPORT_DIR="${LINUX_USER}@${LINUX_SERVER}:${LINUX_LIFEOS}/data/apple-imports/"

# Create remote directory if needed
ssh -o ConnectTimeout=10 "${LINUX_USER}@${LINUX_SERVER}" \
    "mkdir -p ${LINUX_LIFEOS}/data/apple-imports" 2>> "${LOG_FILE}" || {
    log "ERROR: Cannot connect to Linux server at ${LINUX_SERVER}"
    log "Exports saved locally at ${EXPORT_DIR} — will retry next run"
    exit 1
}

# Rsync with compression
if rsync -avz --timeout=60 \
    "${EXPORT_DIR}" "${IMPORT_DIR}" >> "${LOG_FILE}" 2>&1; then
    log "Rsync: OK"
else
    log "Rsync: FAILED (exports saved locally)"
    exit 1
fi

# -------------------------------------------------------------------
# Step 3: Trigger import on Linux server (optional, non-blocking)
# -------------------------------------------------------------------
log "Step 3: Triggering import on Linux server..."

ssh -o ConnectTimeout=10 "${LINUX_USER}@${LINUX_SERVER}" \
    "cd ${LINUX_LIFEOS} && ~/.venvs/lifeos/bin/python scripts/apple_data_import.py --execute" \
    >> "${LOG_FILE}" 2>&1 &

# Don't wait for import — it runs on the server
log "Import triggered (runs async on server)"

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
log "=== Apple Data Agent complete ==="

# Cleanup old logs (keep 30 days)
find "${LOG_DIR}" -name "apple_agent_*.log" -mtime +30 -delete 2>/dev/null || true
