#!/bin/bash
# LifeOS GPU VRAM Watchdog
# Runs every 5 minutes to detect impending VRAM exhaustion before the next
# embedding-heavy operation (vault reindex, fact extraction) tries to allocate
# on top of an already-saturated GPU and freezes the machine.
#
# The actual incident this guards against — issue #199 follow-up, 2026-05-28:
# llama-server + Ollama held ~48 GB of 64 GB BIOS-allocated VRAM at sync
# start; the embedding model load pushed past the ceiling and the kernel
# locked up with "amdgpu_cs_ioctl ERROR Not enough memory for command
# submission!" until the operator force-shut the machine.
#
# Behaviour:
#   - Reads VRAM usage from AMDGPU sysfs (works without rocm-smi)
#   - Alerts via Telegram when usage > THRESHOLD_PCT (default 80%)
#   - Rate-limits alerts to one per COOLDOWN_MIN (default 60 min)
#   - When rocm-smi is present, includes top per-PID VRAM consumers in the alert
#   - Posts a recovery message when usage drops back below the threshold
#
# SDMA-queue exhaustion detection (#521): the gfx1151 iGPU has only 8 SDMA
# queues. Concurrent GPU embedders (e.g. the API server and a manual reindex
# both loading/encoding on GPU at once) can exhaust them with VRAM still
# perfectly healthy — the kernel logs "No more SDMA queue to allocate", the
# same signature that preceded the 2026-07-10 host freeze. VRAM% alone can't
# see this, so each tick also scans the kernel log (via `journalctl -k`) for
# that signature since the previous tick and alerts on it, with its own
# cooldown so it can't spam independently of the VRAM alert.
#
# Linux: triggered by lifeos-gpu-watchdog.timer (installed by setup-systemd.sh).
# macOS: not applicable (Metal manages VRAM via OS-level limits).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/gpu-watchdog.log"
ALERT_STAMP_FILE="$PROJECT_DIR/logs/gpu-watchdog-alert.stamp"
SDMA_STAMP_FILE="$PROJECT_DIR/logs/gpu-watchdog-sdma.stamp"
SDMA_ALERT_STAMP_FILE="$PROJECT_DIR/logs/gpu-watchdog-sdma-alert.stamp"
# Overridable for testing; defaults to the project .env in production.
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
# Overridable for testing (a stub `journalctl` on $PATH, or a plain command
# name if journald isn't present).
JOURNALCTL_CMD="${JOURNALCTL_CMD:-journalctl}"

THRESHOLD_PCT="${LIFEOS_VRAM_ALERT_PCT:-80}"
COOLDOWN_MIN="${LIFEOS_VRAM_ALERT_COOLDOWN_MIN:-60}"
SDMA_COOLDOWN_MIN="${LIFEOS_SDMA_ALERT_COOLDOWN_MIN:-$COOLDOWN_MIN}"
SDMA_PATTERN="No more SDMA queue to allocate"

mkdir -p "$PROJECT_DIR/logs"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

send_telegram() {
    # Returns 0 on successful POST, non-zero otherwise. ``--data-urlencode``
    # protects against rocm-smi / ps output containing & or = which would
    # otherwise truncate the Telegram payload.
    local message="$1"
    if [ ! -f "$ENV_FILE" ]; then
        return 1
    fi
    local bot_token chat_id
    bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
    if [ -z "$bot_token" ] || [ -z "$chat_id" ]; then
        return 1
    fi
    # ``-f`` makes curl return non-zero on HTTP 4xx/5xx so an invalid bot
    # token / wrong chat_id reports as failure to the caller, not silent
    # success.
    /usr/bin/curl -s -f -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${message}" \
        --data-urlencode "parse_mode=Markdown" > /dev/null 2>&1
}

# Scan the kernel log for SDMA-queue exhaustion since the previous tick, and
# alert if any occurred (#521).
#
# VRAM% (the check below) can't see this failure mode: the gfx1151 iGPU has
# only 8 SDMA queues, and concurrent GPU embedders can exhaust those queues
# — "No more SDMA queue to allocate" — with VRAM usage still low. That
# signature preceded the 2026-07-10 host freeze.
#
# Approach: track only a timestamp (not a running count) in $SDMA_STAMP_FILE,
# and re-query the kernel log each tick with `journalctl -k --since <that
# timestamp>`. This was chosen over diffing a persisted occurrence count
# because a count survives reboots awkwardly (dmesg's ring buffer resets, so
# a stored count would either have to be reset on every boot or drift out of
# sync with what's actually still in the buffer) and because `--since` lets
# journald do the filtering instead of this script re-parsing the entire log
# every tick. The timestamp is written *before* the query so a slow or
# failed journalctl call can't cause the same window to be re-scanned (and
# double-counted) on the next tick.
check_sdma_exhaustion() {
    local since_arg
    if [ -f "$SDMA_STAMP_FILE" ]; then
        since_arg=$(date -d "@$(cat "$SDMA_STAMP_FILE")" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "10 minutes ago")
    else
        since_arg="10 minutes ago"
    fi

    date +%s > "$SDMA_STAMP_FILE"

    if ! command -v "$JOURNALCTL_CMD" > /dev/null 2>&1; then
        # No journald (e.g. non-systemd host) — nothing to scan.
        return 0
    fi

    local count
    count=$("$JOURNALCTL_CMD" -k --since "$since_arg" 2>/dev/null | grep -c "$SDMA_PATTERN" || true)
    [[ "$count" =~ ^[0-9]+$ ]] || count=0

    if [ "$count" -eq 0 ]; then
        return 0
    fi

    log "SDMA queue exhaustion detected: ${count} occurrence(s) of '${SDMA_PATTERN}' since ${since_arg}"

    # Separate cooldown/stamp from the VRAM alert so the two signals can't
    # suppress each other.
    local now last
    now=$(date +%s)
    if [ -f "$SDMA_ALERT_STAMP_FILE" ]; then
        last=$(cat "$SDMA_ALERT_STAMP_FILE")
        if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < SDMA_COOLDOWN_MIN * 60 )); then
            log "SDMA alert suppressed (cooldown)"
            return 0
        fi
    fi

    local msg
    msg=$(printf '🔥 *LifeOS GPU SDMA Queues Exhausted*\n\n%d occurrence(s) of "%s" in the kernel log — the same signature that preceded the 2026-07-10 host freeze. VRAM may look healthy; this is a different failure mode (concurrent GPU embedders exhausting the 8 SDMA queues, not memory pressure). Check for overlapping GPU embed jobs (API server, agent worker, sync, ad-hoc scripts).' \
        "$count" "$SDMA_PATTERN")
    if send_telegram "$msg"; then
        echo "$now" > "$SDMA_ALERT_STAMP_FILE"
    else
        log "Telegram POST failed for SDMA alert; will retry on next tick"
    fi
}

# Run the SDMA check every tick, independent of the VRAM logic below (which
# has several early exits) — this failure mode can occur regardless of VRAM
# level.
check_sdma_exhaustion

# Locate the first AMDGPU card sysfs node that exposes VRAM stats. Multi-GPU
# systems are not in scope — this picks the first card with sysfs stats.
find_card_dir() {
    local d
    for d in /sys/class/drm/card*/device; do
        if [ -f "$d/mem_info_vram_used" ] && [ -f "$d/mem_info_vram_total" ]; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

CARD_DIR=""
if ! CARD_DIR=$(find_card_dir); then
    # No AMDGPU sysfs — silently exit. Not a watchdog problem (could be
    # macOS, a fresh boot before the module loaded, etc.).
    exit 0
fi

USED=$(cat "$CARD_DIR/mem_info_vram_used" 2>/dev/null || echo "0")
TOTAL=$(cat "$CARD_DIR/mem_info_vram_total" 2>/dev/null || echo "0")

if [ "$TOTAL" -le 0 ] || [ "$USED" -lt 0 ]; then
    log "Sysfs returned unexpected values (used=$USED total=$TOTAL) — skipping check"
    exit 0
fi

PCT=$(( USED * 100 / TOTAL ))
USED_GB=$(awk -v u="$USED" 'BEGIN {printf "%.1f", u/1024/1024/1024}')
TOTAL_GB=$(awk -v t="$TOTAL" 'BEGIN {printf "%.1f", t/1024/1024/1024}')

# --- Healthy path: clean up alert state and emit recovery if needed ---
if [ "$PCT" -lt "$THRESHOLD_PCT" ]; then
    if [ -f "$ALERT_STAMP_FILE" ]; then
        log "VRAM recovered to ${PCT}% (${USED_GB}/${TOTAL_GB} GB) — clearing alert"
        send_telegram "$(printf '✅ *LifeOS VRAM Recovered*\n\nVRAM back to %d%% (%.1f / %.1f GB).' \
            "$PCT" "$USED_GB" "$TOTAL_GB")"
        rm -f "$ALERT_STAMP_FILE"
    fi
    exit 0
fi

# --- Saturated path: cooldown then alert ---
NOW=$(date +%s)
if [ -f "$ALERT_STAMP_FILE" ]; then
    LAST=$(cat "$ALERT_STAMP_FILE")
    # Guard against a truncated / non-numeric stamp file tripping ``set -e``
    # inside the arithmetic. Fall back to "no prior alert" if unparseable.
    if [[ "$LAST" =~ ^[0-9]+$ ]] && (( NOW - LAST < COOLDOWN_MIN * 60 )); then
        # Already alerted recently; just log.
        log "VRAM at ${PCT}% (${USED_GB}/${TOTAL_GB} GB) — alert suppressed (cooldown)"
        exit 0
    fi
fi

log "VRAM SATURATED: ${PCT}% (${USED_GB}/${TOTAL_GB} GB)"

# Try to attribute usage via rocm-smi. Failures are fine — we still alert,
# just without the per-process breakdown.
CONSUMERS=""
if command -v rocm-smi > /dev/null 2>&1; then
    CONSUMERS=$(rocm-smi --showpids 2>/dev/null \
        | awk '/^[0-9]+/ {gb=$4/1024/1024/1024; printf "  • PID %s (%s): %.1f GB\n", $1, $2, gb}' \
        | sort -t: -k2 -nr | head -5 || true)
fi

# Top RAM-resident Python-ish processes as a fallback hint.
if [ -z "$CONSUMERS" ]; then
    CONSUMERS=$(ps -eo pid,comm,rss --sort=-rss \
        | awk 'NR<=6 {printf "  • PID %s (%s): RSS %d MB\n", $1, $2, $3/1024}')
fi

MSG=$(printf '⚠️ *LifeOS VRAM Saturated*\n\nVRAM at %d%% (%.1f / %.1f GB) — embedding loads may OOM and lock up the GPU.\n\n*Top consumers:*\n%s' \
    "$PCT" "$USED_GB" "$TOTAL_GB" "$CONSUMERS")
# Stamp written AFTER the Telegram POST so a transient curl failure doesn't
# silently suppress the next 5-min retry.
if send_telegram "$MSG"; then
    echo "$NOW" > "$ALERT_STAMP_FILE"
else
    log "Telegram POST failed; will retry on next tick"
fi

exit 0
