#!/bin/bash
# LifeOS Network Watchdog — gentle WiFi re-activation
# Runs every 2 minutes to detect a dead link and nudge it back online.
#
# The incident this guards against (2026-06-30 → 2026-07-08): the WiFi radio
# was deauthenticated from the AP (4WAY_HANDSHAKE_TIMEOUT), NetworkManager's
# activation then "failed", and the interface sat `disconnected` for 8 days.
# Autoconnect never resumed on its own — it only recovered when the operator
# manually re-activated the connection (`nmcli device connect`). Because this
# box is WiFi-only (no wired fallback), that took every remote surface offline:
# Tailscale, the Anthropic API, Google sync, AND every alert channel.
#
# GENTLE BY DESIGN (rewritten 2026-07-10). This watchdog does exactly ONE repair
# action: re-activate the connection (`nmcli device connect`) — the same manual
# step that recovered the outage above. It deliberately does NOT bounce the
# radio, reload the driver module, or restart NetworkManager. On the MediaTek
# MT7925 (`mt7925e`), those station-remove operations can deadlock the driver
# inside `mt7925_mac_sta_remove`, wedging NetworkManager in an unkillable
# D-state and forcing a HARD POWER-OFF (observed 2026-07-10, repeatedly). A
# gentle re-activate can't fix a truly wedged radio — but it also can't take the
# whole box down, which is the right trade for a WiFi-only host. A wedged driver
# is a reboot / kernel-update problem, not something a watchdog should fight.
#
# Opt-in: does nothing unless LIFEOS_NETWORK_WATCHDOG_ENABLED=true in .env. It
# runs as root (system oneshot) to call nmcli, so it stays off by default.
#
# Alerting is best-effort: while the link is down NO channel can reach out, so
# this posts a *recovery* notice once connectivity returns, reporting how long
# the link was down. Interface, WiFi profile, and gateway are derived at runtime
# — nothing machine-specific is hardcoded, so it is a no-op on hosts without a
# managed WiFi device.
#
# Linux: triggered by lifeos-network-watchdog.timer (installed by
# setup-systemd.sh). macOS: not applicable (launchd/networkd differ).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/network-watchdog.log"
DOWN_COUNT_FILE="$PROJECT_DIR/logs/network-watchdog.count"
DOWN_SINCE_FILE="$PROJECT_DIR/logs/network-watchdog.since"
# Overridable for testing; defaults to the project .env in production.
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"

# Space-separated public ping targets (stable anycast IPs; not personal).
# The default gateway is added automatically when a default route exists.
PUBLIC_TARGETS="${LIFEOS_NET_WATCHDOG_TARGETS:-1.1.1.1 8.8.8.8}"
# Only post a recovery notice for outages at/above this many seconds, so
# sub-tick blips that self-heal don't generate noise.
NOTIFY_MIN_SECONDS="${LIFEOS_NET_WATCHDOG_NOTIFY_MIN_SECONDS:-180}"
# Set to 1 to skip the repair action (detect + log only). Useful for running
# the watchdog unprivileged to verify detection.
CHECK_ONLY="${LIFEOS_NET_WATCHDOG_CHECK_ONLY:-0}"

# Read a KEY's value from .env, stripping whitespace. Pipefail/`set -e` safe:
# a missing key or absent file yields the empty string, never a failure.
_read_env() {
    local key="$1"
    [ -f "$ENV_FILE" ] || { echo ""; return 0; }
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
        | tr -d '[:space:]' || true
}

# --- Opt-in gate: a root service that pokes the WiFi link stays off unless
# --- explicitly enabled (open-source-safe default; also lets the operator
# --- kill it via .env without touching systemd). ---
case "$(_read_env LIFEOS_NETWORK_WATCHDOG_ENABLED | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) ;;
    *) exit 0 ;;
esac

mkdir -p "$PROJECT_DIR/logs"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

get_down_count() {
    if [ -f "$DOWN_COUNT_FILE" ]; then
        local v; v=$(cat "$DOWN_COUNT_FILE" 2>/dev/null || echo 0)
        [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
    else
        echo 0
    fi
}

send_telegram() {
    # Best-effort. Only useful once the link is back — see header note.
    local message="$1"
    local bot_token chat_id
    bot_token=$(_read_env TELEGRAM_BOT_TOKEN)
    chat_id=$(_read_env TELEGRAM_CHAT_ID)
    [ -n "$bot_token" ] && [ -n "$chat_id" ] || return 1
    /usr/bin/curl -s -f -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${message}" \
        --data-urlencode "parse_mode=Markdown" > /dev/null 2>&1
}

# --- Identify the managed WiFi device (portable; no hardcoded ifname) ---
WIFI_DEV=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
    | awk -F: '$2=="wifi"{print $1; exit}')

if [ -z "$WIFI_DEV" ]; then
    # No managed WiFi device — nothing for this watchdog to do (wired-only
    # host, macOS, container, etc.). Silent no-op.
    exit 0
fi

# --- Connectivity probe: healthy if ANY target answers a single ping ---
is_online() {
    local gw targets t
    gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
    targets="$gw $PUBLIC_TARGETS"
    for t in $targets; do
        [ -n "$t" ] || continue
        if ping -c1 -W2 "$t" > /dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

# ============================ HEALTHY PATH ============================
if is_online; then
    DOWN=$(get_down_count)
    if [ "$DOWN" -gt 0 ]; then
        # Transitioned back to online. Report downtime if it was notable.
        DOWN_FOR=0
        if [ -f "$DOWN_SINCE_FILE" ]; then
            SINCE=$(cat "$DOWN_SINCE_FILE" 2>/dev/null || echo "")
            if [[ "$SINCE" =~ ^[0-9]+$ ]]; then
                DOWN_FOR=$(( $(date +%s) - SINCE ))
            fi
        fi
        log "Link recovered after $DOWN down tick(s) (~${DOWN_FOR}s offline)"
        if [ "$DOWN_FOR" -ge "$NOTIFY_MIN_SECONDS" ]; then
            MINS=$(( DOWN_FOR / 60 ))
            send_telegram "$(printf '✅ *LifeOS Network Recovered*\n\nWiFi (%s) was offline ~%d min; the watchdog re-activated the link. If this repeats, the MediaTek radio/driver is the culprit — a kernel update, not the watchdog, is the fix.' "$WIFI_DEV" "$MINS")" \
                || log "Recovery Telegram POST failed (will not retry)"
        fi
    fi
    : > "$DOWN_COUNT_FILE"
    rm -f "$DOWN_SINCE_FILE"
    exit 0
fi

# ============================ DOWN PATH ==============================
DOWN=$(get_down_count)
DOWN=$(( DOWN + 1 ))
echo "$DOWN" > "$DOWN_COUNT_FILE"
[ -f "$DOWN_SINCE_FILE" ] || date +%s > "$DOWN_SINCE_FILE"

log "No connectivity via $WIFI_DEV (consecutive down ticks: $DOWN)"

if [ "$CHECK_ONLY" = "1" ]; then
    log "CHECK_ONLY=1 — skipping repair"
    exit 0
fi

# Gentle recovery ONLY — re-activate the connection so a brief deauth/roam that
# NetworkManager didn't auto-recover gets nudged back online. This is the exact
# manual step that recovered the 2026-06/07 outage, and it is idempotent: if the
# AP is genuinely down it fails harmlessly and we retry on the next tick.
#
# We do NOT escalate to radio-bounce / module-reload / NetworkManager-restart:
# on the MT7925 those station-remove paths can deadlock the driver and force a
# hard power-off (see header). A wedged radio is out of scope for a watchdog.
log "Repair: nmcli device connect $WIFI_DEV (gentle re-activate)"
nmcli device connect "$WIFI_DEV" >> "$LOG_FILE" 2>&1 || true
sleep 3
if is_online; then
    log "Connectivity restored after gentle re-activate"
fi

exit 0
