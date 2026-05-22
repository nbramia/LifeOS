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
#   - LifeOS.app has Full Disk Access (routes export through it)
#   - SSH key to Linux server (no password prompt)
#   - rsync installed on both machines

set -euo pipefail

# Cron on macOS runs with a minimal PATH that excludes Homebrew. Prepend the
# standard brew locations so Python subprocesses (e.g. wacli) resolve.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

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

# Retry a command up to N times with explicit delays. The Mac Mini reaches
# the Linux server over whatever network you've configured (LAN, mesh VPN,
# tunnel) — without retry, a brief hiccup at cron time meant the exports
# sat on the Mac Mini until the next day's cron.
#
# Usage:  retry_with_backoff <label> <delays_csv> <command...>
# Returns the exit status of the last attempt.
retry_with_backoff() {
    local label=$1
    local delays_csv=$2
    shift 2

    local -a delays
    IFS=',' read -r -a delays <<< "${delays_csv}"
    local max_attempts=$(( ${#delays[@]} + 1 ))

    local attempt=1
    while (( attempt <= max_attempts )); do
        if "$@"; then
            if (( attempt > 1 )); then
                log "${label}: succeeded on attempt ${attempt}/${max_attempts}"
            fi
            return 0
        fi
        local rc=$?
        if (( attempt < max_attempts )); then
            local delay=${delays[$((attempt - 1))]}
            log "${label}: attempt ${attempt}/${max_attempts} failed (exit ${rc}); retrying in ${delay}s..."
            sleep "${delay}"
        else
            log "${label}: all ${max_attempts} attempts failed (final exit ${rc})"
            return ${rc}
        fi
        attempt=$((attempt + 1))
    done
}

# Retry delays in seconds: brief network hiccups usually resolve in <2 min.
# Total wall time on full failure ≈ 30 + 90 + 180 = 5 min before giving up.
RETRY_DELAYS="30,90,180"

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
# Step 0: Ensure Messages and Photos are running for iCloud sync
# These apps must be open for iCloud to deliver new data to the local
# SQLite databases. Open them early so they can sync while we work.
# -------------------------------------------------------------------
log "Step 0: Opening Messages and Photos for iCloud sync..."
osascript -e 'tell application "Messages" to activate' >> "${LOG_FILE}" 2>&1 || true
osascript -e 'tell application "Photos" to activate' >> "${LOG_FILE}" 2>&1 || true
# Give iCloud a head start before we read the databases
sleep 600

# -------------------------------------------------------------------
# Step 1: Export Apple data to data/apple-exports/
# Route through LifeOS.app's "run" subcommand so Python inherits
# Full Disk Access (TCC checks the responsible process — LifeOS.app).
# "run" keeps LifeOS.app as the parent; "exec" replaces it, losing FDA.
# -------------------------------------------------------------------
log "Step 1: Exporting Apple data..."
LIFEOS_APP="/Applications/LifeOS.app/Contents/MacOS/LifeOS"

cd "${LIFEOS_DIR}"
if "${LIFEOS_APP}" run "${PYTHON}" scripts/apple_data_export.py --execute >> "${LOG_FILE}" 2>&1; then
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

# Create remote directory if needed — retry on transient SSH failures
# (e.g., the link to the server just came back, or remote sshd is briefly busy).
ensure_remote_dir() {
    ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 \
        "${LINUX_USER}@${LINUX_SERVER}" \
        "mkdir -p ${LINUX_LIFEOS}/data/apple-imports" 2>> "${LOG_FILE}"
}
if ! retry_with_backoff "SSH mkdir" "${RETRY_DELAYS}" ensure_remote_dir; then
    log "ERROR: Cannot connect to Linux server at ${LINUX_SERVER} after retries"
    log "Exports saved locally at ${EXPORT_DIR} — will retry next run"
    exit 1
fi

# Rsync with compression — retry on transient failures (network, timeout).
run_rsync() {
    rsync -avz --timeout=60 \
        -e "ssh -o ConnectTimeout=10 -o ServerAliveInterval=15" \
        "${EXPORT_DIR}" "${IMPORT_DIR}" >> "${LOG_FILE}" 2>&1
}
if retry_with_backoff "Rsync" "${RETRY_DELAYS}" run_rsync; then
    log "Rsync: OK"
else
    log "Rsync: FAILED after retries (exports saved locally)"
    exit 1
fi

# -------------------------------------------------------------------
# Step 3: Trigger import on Linux server (optional, non-blocking)
# -------------------------------------------------------------------
log "Step 3: Triggering import on Linux server..."

# Fire-and-forget — server-side script handles its own logging. We don't
# retry here because by this point rsync already succeeded, so the data
# is on the server; the next nightly cron will pick it up if the trigger
# misses.
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 \
    "${LINUX_USER}@${LINUX_SERVER}" \
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
