#!/usr/bin/env bash
# LifeOS Infra Watchdog
# Runs every 5 minutes (user unit lifeos-infra-watchdog.timer) and checks the
# host dependencies other devices rely on:
#   - Declared tailnet routes (LifeOS's own plus config/tailscale-routes.local):
#     re-applies any that are missing, alerts if one is still missing.
#   - LIFEOS_PEBBLE_HEALTH_URL (unset = skipped): alerts after 3 consecutive
#     failed health checks.
#   - LIFEOS_EXPECT_OBSIDIAN_SYNC=true: relaunches Obsidian in the user session
#     when it is not running, and alerts if it is still absent on the next run.
#
# Alerts go to Telegram with a per-kind cooldown. Handled failures exit 0 so
# the timer never crash-loops.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/tailscale-routes.sh
source "$SCRIPT_DIR/lib/tailscale-routes.sh"

STATE_DIR="${LIFEOS_INFRA_STATE_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$STATE_DIR/infra-watchdog.log"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
ROUTES_FILE="${LIFEOS_TAILSCALE_ROUTES_FILE:-$PROJECT_DIR/config/tailscale-routes.local}"
COOLDOWN_MIN="${LIFEOS_INFRA_ALERT_COOLDOWN_MIN:-360}"
PEBBLE_STRIKES="${LIFEOS_INFRA_PEBBLE_STRIKES:-3}"
OBSIDIAN_LAUNCH_CMD="${LIFEOS_OBSIDIAN_LAUNCH_CMD:-systemd-run --user --collect --unit=obsidian-session-\$(date +%s) snap run obsidian}"
NODE_SCRIPT="$SCRIPT_DIR/mcp-funnel-node.sh"
MODEL_PROBE_INTERVAL_MIN="${LIFEOS_MODEL_PROBE_INTERVAL_MIN:-60}"
MODEL_PROBE_TIMEOUT="${LIFEOS_MODEL_PROBE_TIMEOUT:-60}"
PROBE_PYTHON="${LIFEOS_PROBE_PYTHON:-$HOME/.venvs/lifeos/bin/python}"

mkdir -p "$STATE_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Copy the notice to the bot-notices log (api/services/bot_notices.py). Best-effort.
record_bot_notice() {
    printf '%s' "$1" | python3 -I "$PROJECT_DIR/api/services/bot_notices.py" "LifeOS/infra-watchdog" > /dev/null 2>&1 || true
}

send_telegram() {
    local message="$1" bot_token chat_id
    [ -f "$ENV_FILE" ] || return 1
    bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
    [ -n "$bot_token" ] && [ -n "$chat_id" ] || return 1
    local rc=0
    "${LIFEOS_WATCHDOG_CURL:-/usr/bin/curl}" -s -f --connect-timeout 5 --max-time 15 -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${message}" > /dev/null 2>&1 || rc=$?
    record_bot_notice "$message"
    return $rc
}

# alert <kind> <message>: send at most once per cooldown per kind. The stamp is
# written only when the send succeeds, so a failed send retries next run.
alert() {
    local kind="$1" message="$2" stamp last now
    stamp="$STATE_DIR/infra-watchdog-alert-${kind}.stamp"
    now=$(date +%s)
    if [ -f "$stamp" ]; then
        last=$(cat "$stamp")
        if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < COOLDOWN_MIN * 60 )); then
            log "alert '$kind' suppressed (cooldown)"
            return 0
        fi
    fi
    if send_telegram "$message"; then
        echo "$now" > "$stamp"
        log "alert '$kind' sent"
    else
        log "alert '$kind' not sent (Telegram failed); will retry next run"
    fi
}

read_count() {
    local n
    n=$(cat "$1" 2>/dev/null || echo 0)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    echo "$n"
}

check_routes() {
    if ! command -v tailscale > /dev/null 2>&1; then
        log "routes: tailscale not installed; skipped"
        return 0
    fi
    local err errfile="$STATE_DIR/infra-watchdog-routes.err"
    # Not command substitution: ROUTES must be set in this shell.
    if ! ts_load_routes "$ROUTES_FILE" 2> "$errfile"; then
        err=$(cat "$errfile")
        log "routes: invalid routes file: ${err//$'\n'/ }"
        alert routes-config "LifeOS host: tailnet routes file has invalid lines that are being skipped. ${err}"
    fi

    local route port path target missing=() reapplied=() still=()
    for route in "${ROUTES[@]}"; do
        IFS='|' read -r port path target _ <<< "$route"
        ts_route_present "$port" "$path" "$target" || missing+=("$route")
    done
    if [ ${#missing[@]} -eq 0 ]; then
        log "routes: ${#ROUTES[@]} declared, all present"
    fi

    for route in "${missing[@]}"; do
        IFS='|' read -r port path target _ <<< "$route"
        ts_apply_route "$route" > /dev/null 2>&1
        if ts_route_present "$port" "$path" "$target"; then
            reapplied+=("https=${port} path=${path}")
        else
            still+=("https=${port} path=${path}")
        fi
    done
    [ ${#reapplied[@]} -eq 0 ] || log "routes: re-applied missing route(s): ${reapplied[*]}"
    if [ ${#still[@]} -gt 0 ]; then
        log "routes: still missing after re-apply: ${still[*]}"
        alert routes "LifeOS host: tailnet route(s) missing and could not be re-applied: ${still[*]}"
    fi
    check_exposure
}

# Public exposure (Funnel) must match the declaration. A port found public that
# is declared private is switched off and alerted; a declared-public port found
# private is re-applied. Nothing is ever made public without a funnel=on line,
# and the loader accepts those only with LIFEOS_TAILSCALE_ALLOW_FUNNEL=true.
check_exposure() {
    local port route r_port funnel
    for port in $(ts_declared_ports); do
        if ts_wants_public "$port"; then
            ts_port_public "$port" && continue
            for route in "${ROUTES[@]}"; do
                IFS='|' read -r r_port _ _ funnel <<< "$route"
                [[ "$r_port" == "$port" && "$funnel" == "on" ]] && ts_apply_route "$route" > /dev/null 2>&1
            done
            if ts_port_public "$port"; then
                log "exposure: https=${port} was private but declared public; re-applied"
            else
                log "exposure: https=${port} declared public but could not be restored"
                alert "exposure-${port}" "LifeOS host: tailnet port ${port} is declared public (funnel=on) but is not, and could not be restored."
            fi
        elif ts_port_public "$port"; then
            if ts_make_private "$port"; then
                log "exposure: https=${port} was public, declared private; funnel turned off and routes re-applied"
                alert "exposure-${port}" "LifeOS host: tailnet port ${port} was publicly exposed (Funnel) but declared private. Funnel has been turned off and its routes re-applied."
            else
                log "exposure: https=${port} is public or incomplete, declared private; make-private FAILED"
                alert "exposure-${port}" "LifeOS host: tailnet port ${port} is declared private but is still public or missing routes after turning Funnel off."
            fi
        fi
    done
}

check_pebble() {
    local url="${LIFEOS_PEBBLE_HEALTH_URL:-}" count_file="$STATE_DIR/infra-watchdog-pebble.count" n
    if [ -z "$url" ]; then
        return 0
    fi
    if curl -sf --connect-timeout 5 -m 10 -o /dev/null "$url"; then
        echo 0 > "$count_file"
        log "pebble: healthy"
        return 0
    fi
    n=$(( $(read_count "$count_file") + 1 ))
    echo "$n" > "$count_file"
    log "pebble: health check failed ($n consecutive)"
    if (( n >= PEBBLE_STRIKES )); then
        alert pebble "LifeOS host: the Pebble receiver health check has failed ${n} consecutive times."
    fi
}

check_obsidian() {
    local launched="$STATE_DIR/infra-watchdog-obsidian-launched.stamp"
    [ "${LIFEOS_EXPECT_OBSIDIAN_SYNC:-false}" = "true" ] || return 0
    if pgrep -x obsidian > /dev/null 2>&1; then
        rm -f "$launched"
        log "obsidian: running"
        return 0
    fi
    if [ -f "$launched" ]; then
        log "obsidian: still not running after relaunch"
        alert obsidian "LifeOS host: Obsidian is not running and a relaunch did not bring it back, so vault sync is stalled."
        return 0
    fi
    date +%s > "$launched"
    if bash -c "$OBSIDIAN_LAUNCH_CMD" >> "$LOG_FILE" 2>&1; then
        log "obsidian: not running; relaunch requested"
    else
        log "obsidian: not running; relaunch command failed"
    fi
}

# The public MCP node (LIFEOS_MCP_FUNNEL_NODE=true): its container must be
# running and publish exactly the public socket. A logged-out node needs the
# operator.
check_mcp_funnel_node() {
    local rc
    [ "${LIFEOS_MCP_FUNNEL_NODE:-false}" = "true" ] || return 0
    "$NODE_SCRIPT" check > /dev/null 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        log "mcp-funnel: published"
        return 0
    fi
    if [ $rc -ne 3 ]; then
        "$NODE_SCRIPT" up > /dev/null 2>&1
        rc=$?
        if [ $rc -eq 0 ]; then
            log "mcp-funnel: node was down or not publishing exactly; restored"
            return 0
        fi
    fi
    if [ $rc -eq 3 ]; then
        log "mcp-funnel: node logged out"
        alert mcp-funnel-login "LifeOS host: the public MCP node is logged out of Tailscale, so connected apps (Claude, ChatGPT) cannot reach LifeOS. Run scripts/mcp-funnel-node.sh login (docs/guides/mcp-connected-apps.md)."
    else
        log "mcp-funnel: node down or not publishing exactly, and could not be restored"
        alert mcp-funnel "LifeOS host: the public MCP node is down or not publishing exactly its socket, and could not be restored, so connected apps cannot reach LifeOS."
    fi
}

# Remote-provider model ids (LifeOS settings plus LIFEOS_MODEL_PROBE_SOURCES):
# probed hourly, each problem alerted under its own cooldown key.
check_remote_models() {
    local stamp="$STATE_DIR/infra-watchdog-model-probe.stamp" out="$STATE_DIR/model-probe.out"
    local err="$STATE_DIR/model-probe.err" now last key message rc n=0
    now=$(date +%s)
    if [ -f "$stamp" ]; then
        last=$(cat "$stamp")
        if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < MODEL_PROBE_INTERVAL_MIN * 60 )); then
            return 0
        fi
    fi
    echo "$now" > "$stamp"
    LIFEOS_INFRA_STATE_DIR="$STATE_DIR" timeout "$MODEL_PROBE_TIMEOUT" \
        "$PROBE_PYTHON" "$SCRIPT_DIR/check_remote_models.py" > "$out" 2> "$err"
    rc=$?
    cat "$err" >> "$LOG_FILE"
    while IFS=$'\t' read -r key message; do
        [ -n "$key" ] || continue
        n=$((n + 1))
        alert "model-$(printf '%s' "$key" | tr -c 'A-Za-z0-9._-' '_')" "LifeOS host: $message"
    done < "$out"
    case $rc in
        0) log "model-probe: $n problem(s)" ;;
        2) log "model-probe: remote provider not configured; skipped" ;;
        *)
            log "model-probe: probe failed (exit $rc)"
            alert model-probe-failed "LifeOS host: the remote-model probe failed (exit $rc$( [ $rc -eq 124 ] && echo ', timed out')), so retired models may go unnoticed. See logs/infra-watchdog.log."
            ;;
    esac
}

# The model probe runs first: it is bounded by MODEL_PROBE_TIMEOUT, and the
# unit's own start timeout must leave it room.
check_remote_models
check_routes
check_pebble
check_obsidian
check_mcp_funnel_node
exit 0
