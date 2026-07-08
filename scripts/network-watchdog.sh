#!/bin/bash
# LifeOS Network Watchdog
# Runs every 2 minutes to detect and self-heal a dead network link.
#
# The incident this guards against (2026-06-30 → 2026-07-08): the WiFi radio
# was deauthenticated from the AP (4WAY_HANDSHAKE_TIMEOUT), NetworkManager's
# activation then "failed", and the interface sat `disconnected` for 8 days.
# Autoconnect never resumed on its own — it only recovered when the operator
# manually re-activated the connection. Because this box is WiFi-only (no
# wired fallback), that took every remote surface offline: Tailscale, the
# Anthropic API, Google sync, AND every alert channel. Nothing local watches
# connectivity, so nothing noticed.
#
# This watchdog pings the gateway + a public target; if BOTH are unreachable
# for consecutive ticks it escalates repair: re-activate the connection →
# bounce the radio → reload the WiFi driver module → restart NetworkManager.
# It backs off between levels so a momentary blip self-heals cheaply.
#
# Alerting is best-effort by design: while the link is down NO channel can
# reach out (that gap is what an external heartbeat/dead-man's-switch would
# cover). Instead this posts a *recovery* notice once connectivity returns,
# reporting how long the link was down and which step fixed it — so sustained
# outages are visible after the fact even though they self-heal.
#
# Runs as root (system oneshot) because repair needs nmcli/ip/modprobe/
# systemctl. Interface, WiFi profile, gateway, and driver module are all
# derived at runtime — nothing machine-specific is hardcoded, so this is a
# no-op on hosts without a managed WiFi device.
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
# sub-tick blips that self-heal at level 1 don't generate noise.
NOTIFY_MIN_SECONDS="${LIFEOS_NET_WATCHDOG_NOTIFY_MIN_SECONDS:-180}"
# Set to 1 to skip all repair actions (detect + log only). Useful for
# running the watchdog unprivileged to verify detection.
CHECK_ONLY="${LIFEOS_NET_WATCHDOG_CHECK_ONLY:-0}"

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
    [ -f "$ENV_FILE" ] || return 1
    local bot_token chat_id
    bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
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
            send_telegram "$(printf '✅ *LifeOS Network Recovered*\n\nWiFi (%s) was offline ~%d min and the watchdog restored it. If this repeats, the MediaTek link is the culprit.' "$WIFI_DEV" "$MINS")" \
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

# Graduated repair, one level per down tick, gentlest first. Each command is
# guarded so a failure (expected while the link is down) doesn't abort the
# script under `set -e`, and we re-probe after each so we stop escalating the
# moment connectivity returns.
attempt() {
    local desc="$1"; shift
    log "Repair L${DOWN}: $desc"
    "$@" >> "$LOG_FILE" 2>&1 || true
    sleep 3
    if is_online; then
        log "Connectivity restored after: $desc"
        return 0
    fi
    return 1
}

case "$DOWN" in
    1)
        attempt "nmcli device connect $WIFI_DEV" nmcli device connect "$WIFI_DEV" || true
        ;;
    2)
        # Bounce the radio, then re-activate.
        log "Repair L2: cycling WiFi radio"
        nmcli radio wifi off >> "$LOG_FILE" 2>&1 || true
        sleep 3
        nmcli radio wifi on >> "$LOG_FILE" 2>&1 || true
        sleep 3
        attempt "nmcli device connect $WIFI_DEV (post radio cycle)" nmcli device connect "$WIFI_DEV" || true
        ;;
    3)
        # Reload the WiFi driver module — recovers a wedged firmware/driver,
        # the most likely root of a handshake-timeout that never clears.
        MODULE=$(basename "$(readlink -f "/sys/class/net/$WIFI_DEV/device/driver/module" 2>/dev/null)" 2>/dev/null || true)
        if [ -n "$MODULE" ] && [ "$MODULE" != "." ]; then
            log "Repair L3: reloading driver module '$MODULE'"
            modprobe -r "$MODULE" >> "$LOG_FILE" 2>&1 || true
            sleep 2
            modprobe "$MODULE" >> "$LOG_FILE" 2>&1 || true
            sleep 4
            attempt "nmcli device connect $WIFI_DEV (post module reload)" nmcli device connect "$WIFI_DEV" || true
        else
            log "Repair L3: could not resolve driver module for $WIFI_DEV — skipping to NM restart"
            attempt "systemctl restart NetworkManager" systemctl restart NetworkManager || true
        fi
        ;;
    *)
        # Sustained outage. Restart NetworkManager, but only every 3rd tick so
        # we don't thrash the whole stack every 2 minutes.
        if [ $(( DOWN % 3 )) -eq 1 ]; then
            attempt "systemctl restart NetworkManager (sustained outage)" systemctl restart NetworkManager || true
        else
            attempt "nmcli device connect $WIFI_DEV (retry)" nmcli device connect "$WIFI_DEV" || true
        fi
        ;;
esac

exit 0
