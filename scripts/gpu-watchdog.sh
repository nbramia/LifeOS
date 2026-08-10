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
#   - ACTIVE RECLAIM: when usage stays >= RECLAIM_PCT (default 92%) for
#     RECLAIM_STRIKES consecutive ticks, stops the local LLM service to free
#     VRAM + GPU queues, then restarts it once usage drains below RESTART_PCT.
#     Degrades to alert-only where passwordless ``sudo -n systemctl`` is denied.
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
# State dir holds the log + stamp/strike/marker files. Overridable so tests
# can run against a scratch dir instead of the real logs/. The SDMA stamps
# (#521) route through it too — the default resolves to the same
# $PROJECT_DIR/logs path they used before, so production paths are unchanged.
STATE_DIR="${LIFEOS_VRAM_STATE_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$STATE_DIR/gpu-watchdog.log"
ALERT_STAMP_FILE="$STATE_DIR/gpu-watchdog-alert.stamp"
STRIKE_FILE="$STATE_DIR/gpu-watchdog-strikes.count"
RECLAIM_MARKER="$STATE_DIR/gpu-watchdog-reclaimed.marker"
SDMA_STAMP_FILE="$STATE_DIR/gpu-watchdog-sdma.stamp"
SDMA_ALERT_STAMP_FILE="$STATE_DIR/gpu-watchdog-sdma-alert.stamp"
# Overridable for testing; defaults to the project .env in production.
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
# Overridable for testing (a stub `journalctl` on $PATH, or a plain command
# name if journald isn't present).
JOURNALCTL_CMD="${JOURNALCTL_CMD:-journalctl}"

THRESHOLD_PCT="${LIFEOS_VRAM_ALERT_PCT:-80}"
COOLDOWN_MIN="${LIFEOS_VRAM_ALERT_COOLDOWN_MIN:-60}"
SDMA_COOLDOWN_MIN="${LIFEOS_SDMA_ALERT_COOLDOWN_MIN:-$COOLDOWN_MIN}"
SDMA_PATTERN="No more SDMA queue to allocate"

# Active reclaim (added 2026-07-09 after issue #199 recurred): alerting alone
# didn't stop a 97%-VRAM kernel lockup when a network wedge left GPU work hung
# and nothing reclaimed the memory. When VRAM stays at/above RECLAIM_PCT for
# RECLAIM_STRIKES consecutive ticks, stop the local LLM service to force-release
# its VRAM + GPU queues, then restart it automatically once VRAM falls back
# below RESTART_PCT. Set LIFEOS_VRAM_RECLAIM_PCT=101 to disable (alert-only).
RECLAIM_PCT="${LIFEOS_VRAM_RECLAIM_PCT:-92}"
RECLAIM_STRIKES="${LIFEOS_VRAM_RECLAIM_STRIKES:-2}"
RESTART_PCT="${LIFEOS_VRAM_RESTART_PCT:-60}"
LLM_SERVICE="${LIFEOS_LLM_SERVICE:-lifeos-llm.service}"

mkdir -p "$STATE_DIR"

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
    # success. Binary is overridable so tests can record POSTs without network.
    "${LIFEOS_WATCHDOG_CURL:-/usr/bin/curl}" -s -f -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
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
    # Test hook: point directly at a scratch dir with fake mem_info_vram_* files.
    if [ -n "${LIFEOS_VRAM_CARD_DIR:-}" ]; then
        echo "$LIFEOS_VRAM_CARD_DIR"
        return 0
    fi
    for d in /sys/class/drm/card*/device; do
        if [ -f "$d/mem_info_vram_used" ] && [ -f "$d/mem_info_vram_total" ]; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

# Consecutive-tick counter for the active-reclaim strike logic.
read_strikes() {
    local n
    n=$(cat "$STRIKE_FILE" 2>/dev/null || echo 0)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    echo "$n"
}

# Stop the local LLM to force-release VRAM + GPU queues. Uses the same
# passwordless sudo allowlist as run_all_syncs.py's embedding path. On a fresh
# clone without that allowlist, ``sudo -n`` is denied and we no-op with a log
# (degrading to the historical alert-only behaviour). Expects PCT to be set.
reclaim_vram() {
    if ! systemctl is-active --quiet "$LLM_SERVICE" 2>/dev/null; then
        return 1  # not running — nothing this service can free
    fi
    if sudo -n systemctl stop "$LLM_SERVICE" 2>/dev/null; then
        : > "$RECLAIM_MARKER"
        log "RECLAIM: stopped $LLM_SERVICE at ${PCT}% VRAM to prevent a GPU lockup"
        return 0
    fi
    log "RECLAIM: wanted to stop $LLM_SERVICE but 'sudo -n' was denied"
    return 1
}

# Restart the LLM we previously stopped, once VRAM has drained. Expects PCT set.
restore_llm() {
    if sudo -n systemctl start "$LLM_SERVICE" 2>/dev/null; then
        rm -f "$RECLAIM_MARKER"
        log "RECLAIM: restarted $LLM_SERVICE — VRAM recovered to ${PCT}%"
        return 0
    fi
    log "RECLAIM: wanted to restart $LLM_SERVICE but 'sudo -n' was denied"
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

# --- Active reclaim restore: bring the LLM back once VRAM has drained ---
# If a previous tick stopped the LLM and VRAM is now well below the ceiling,
# restart it so chat/agent surfaces recover without operator intervention.
if [ -f "$RECLAIM_MARKER" ] && [ "$PCT" -lt "$RESTART_PCT" ]; then
    if restore_llm; then
        # ``|| true``: a failed Telegram POST must not abort the run under set -e
        # now that the LLM has already been restarted and the marker cleared.
        send_telegram "$(printf '♻️ *LifeOS LLM Restored*\n\nVRAM drained to %d%% (%.1f / %.1f GB); restarted %s.' \
            "$PCT" "$USED_GB" "$TOTAL_GB" "$LLM_SERVICE")" || true
    fi
fi

# --- Healthy path: clean up alert state and emit recovery if needed ---
if [ "$PCT" -lt "$THRESHOLD_PCT" ]; then
    rm -f "$STRIKE_FILE"  # below the alert line — reset the consecutive-tick run
    if [ -f "$ALERT_STAMP_FILE" ]; then
        log "VRAM recovered to ${PCT}% (${USED_GB}/${TOTAL_GB} GB) — clearing alert"
        send_telegram "$(printf '✅ *LifeOS VRAM Recovered*\n\nVRAM back to %d%% (%.1f / %.1f GB).' \
            "$PCT" "$USED_GB" "$TOTAL_GB")"
        rm -f "$ALERT_STAMP_FILE"
    fi
    exit 0
fi

# --- Saturated path: reclaim, then cooldown-limited alert ---
NOW=$(date +%s)

# Active reclaim: count consecutive ticks at/above the hard ceiling. Once the
# run reaches RECLAIM_STRIKES, stop the LLM to release VRAM (unless a prior tick
# already did). This runs before the alert cooldown so a reclaim can never be
# suppressed by a recent warning.
if [ "$PCT" -ge "$RECLAIM_PCT" ]; then
    STRIKES=$(( $(read_strikes) + 1 ))
    echo "$STRIKES" > "$STRIKE_FILE"
    if [ "$STRIKES" -ge "$RECLAIM_STRIKES" ] && [ ! -f "$RECLAIM_MARKER" ]; then
        if reclaim_vram; then
            # ``|| true``: a failed Telegram POST must not abort before we stamp
            # the cooldown + exit (the LLM is already stopped, marker written).
            send_telegram "$(printf '🛑 *LifeOS VRAM Reclaim*\n\nVRAM stuck at %d%% (%.1f / %.1f GB) for %d checks — stopped %s to prevent a GPU lockup. Will restart it once VRAM drains.' \
                "$PCT" "$USED_GB" "$TOTAL_GB" "$STRIKES" "$LLM_SERVICE")" || true
            # Own the alert cooldown so we don't also fire the ⚠️ warning below.
            echo "$NOW" > "$ALERT_STAMP_FILE"
            exit 0
        fi
    fi
else
    # Above the alert line but below the hard ceiling — reset the strike run.
    rm -f "$STRIKE_FILE"
fi

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
