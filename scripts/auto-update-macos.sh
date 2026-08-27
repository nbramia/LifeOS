#!/bin/bash
# LifeOS macOS Auto-Update
# ========================
# The macOS analog of scripts/auto-deploy.sh (Linux/systemd): polls
# origin/main and, when it advances (or .env changes — #792's signal
# applies here too), fast-forward pulls and restarts the launchd-managed
# API service.
#
# Opt-in: does nothing unless LIFEOS_AUTODEPLOY_ENABLED=true in .env — same
# flag, same off-by-default posture as the Linux timer. Nothing in this
# script installs a cron entry or launchd timer for itself; an operator who
# wants it to run periodically adds that themselves (see the header of
# scripts/run_sync_wrapper.sh's crontab entry in docs/guides/installation.md
# for the pattern). A host that hasn't opted in, or never invokes this
# script, is completely unaffected.
#
# The restart sequence below is the one a real field deployment converged on
# after this took the API down twice in one night:
#   1. Unload (stop) the service, then POLL for the previous process to
#      actually be gone before loading (starting) the next one. launchd's
#      `unload` returns once it has *asked* the process to exit, not once it
#      has. Starting immediately after — the fixed `stop_macos; sleep 2;
#      start_macos` in scripts/service.sh's `restart` dispatch — let the new
#      process's bind() collide with the old one still tearing down. This
#      script does its own restart cycle rather than calling that dispatch,
#      because fixing the collision meant replacing the fixed sleep with an
#      actual poll.
#   2. Load (start) the service. A bare I/O-type error from `launchctl`
#      here has been observed to be transient — retry once before treating
#      it as a hard failure.
#   3. Poll /health with a timeout SCALED to the on-disk vault size (file
#      count), not a fixed short window — a larger vault takes longer to
#      embed/reindex on boot, and a fixed short timeout declared the server
#      dead while it was still loading.
#   4. If /health still isn't up after that, perform exactly ONE more full
#      restart cycle. If that also fails, alert a human (Telegram) and stop
#      — never restart indefinitely, and never silently stay down either.
#
# NEVER uses `launchctl kickstart` — it has wedged the API service before
# (see docs/guides/operations.md). Always a clean unload-then-load cycle.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || exit 1

LOG_FILE="$PROJECT_DIR/logs/auto-update-macos.log"
ENV_FILE="$PROJECT_DIR/.env"
PLIST_NAME="com.lifeos.api"
PLIST_PATH="${LIFEOS_PLIST_PATH:-$HOME/Library/LaunchAgents/$PLIST_NAME.plist}"
# Where this script records the last time IT restarted the service — launchd
# has no equivalent of systemd's ActiveEnterTimestamp, so (unlike
# auto-deploy.sh, which reads the unit's own start time) this script tracks
# its own restarts instead.
LAST_RESTART_MARKER="${LIFEOS_MACOS_RESTART_MARKER:-$PROJECT_DIR/data/macos-autoupdate-last-restart}"
# Non-interactive git over SSH: fail fast instead of prompting for a passphrase.
export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'

mkdir -p "$PROJECT_DIR/logs"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"; }

# Read a KEY=value from .env, stripping quotes/whitespace/inline comments —
# same convention as scripts/auto-deploy.sh's own _read_env.
_read_env() {
    local key="$1" default="$2" val
    val=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
        | sed "s/^['\"]//;s/['\"]$//;s/ *#.*//" | tr -d '[:space:]')
    echo "${val:-$default}"
}

send_telegram() {
    local message="$1"
    [ -f "$ENV_FILE" ] || return 0
    local bot_token chat_id
    bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
    chat_id=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2)
    [ -n "$bot_token" ] && [ -n "$chat_id" ] || return 0
    curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d "chat_id=${chat_id}" \
        -d "text=${message}" \
        -d "parse_mode=Markdown" > /dev/null 2>&1 || true
}

NOTIFY=$(_read_env "LIFEOS_AUTODEPLOY_NOTIFY" "failure")
notify_failure() { [ "$NOTIFY" != "never" ] && send_telegram "$1"; }
notify_success() { [ "$NOTIFY" = "always" ] && send_telegram "$1"; }

# Portable mtime: GNU `stat -c` (this repo's dev/CI hosts, Linux) first,
# BSD `stat -f` (the actual macOS target) as fallback — so the same
# function works when this script is sourced under test on Linux and when
# it actually runs on a Mac.
_file_mtime() {
    stat -c '%Y' "$1" 2>/dev/null || stat -f '%m' "$1" 2>/dev/null
}

# --- Drift check helpers (mirrors auto-deploy.sh's #631/#792) ---------------
newest_code_mtime() {
    git ls-files -- api config mcp_server.py 2>/dev/null \
        | while read -r f; do _file_mtime "$f"; done \
        | sort -rn | head -1
}

env_file_mtime() {
    [ -f "$ENV_FILE" ] || return 0
    _file_mtime "$ENV_FILE"
}

last_restart_epoch() {
    [ -f "$LAST_RESTART_MARKER" ] || return 1
    _file_mtime "$LAST_RESTART_MARKER"
}

mark_restarted() {
    mkdir -p "$(dirname "$LAST_RESTART_MARKER")"
    : > "$LAST_RESTART_MARKER"
}

# --- Sync-in-progress defer (reuses #793's marker) --------------------------
# No systemd unit to check on macOS — just the pid marker
# scripts/run_all_syncs.py writes/removes around a real run.
sync_marker_in_progress() {
    local marker="$PROJECT_DIR/data/sync_in_progress.pid"
    [ -f "$marker" ] || return 1
    local pid
    pid=$(cat "$marker" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# --- launchd primitives ------------------------------------------------------

# Current PID of the running service (columnar `launchctl list` output —
# same convention scripts/service.sh's status_macos() already parses), or
# empty if not running / not loaded.
api_pid() {
    launchctl list 2>/dev/null | awk -v label="$PLIST_NAME" '$3 == label { print $1 }'
}

# Poll until the previous instance's PID is actually gone (empty or "-"),
# up to $1 seconds. Returns 0 once torn down, 1 on timeout (caller proceeds
# anyway and logs a warning — better to try starting than to hang forever).
wait_for_teardown() {
    local timeout="${1:-30}" waited=0 pid
    while true; do
        pid=$(api_pid)
        { [ -z "$pid" ] || [ "$pid" = "-" ]; } && return 0
        [ "$waited" -ge "$timeout" ] && return 1
        sleep 1
        waited=$((waited + 1))
    done
}

# Start the service, retrying once on a transient-looking failure. Never
# `launchctl kickstart` — always `load`.
start_with_retry() {
    local out
    if out=$(launchctl load "$PLIST_PATH" 2>&1); then
        return 0
    fi
    log "start: first attempt failed ($out) — retrying once (transient errors have been observed here)"
    sleep 2
    if out=$(launchctl load "$PLIST_PATH" 2>&1); then
        return 0
    fi
    log "start: retry also failed ($out)"
    return 1
}

# Health-check timeout, scaled to the on-disk vault size rather than a fixed
# short window: baseline 60s, +1s per 200 files under the vault, capped at
# 600s. LIFEOS_VAULT_PATH comes from .env, the same source setup-launchd.sh
# substituted into the plist at install time.
health_check_timeout() {
    local vault_path file_count=0 scaled
    vault_path=$(_read_env "LIFEOS_VAULT_PATH" "")
    if [ -n "$vault_path" ] && [ -d "$vault_path" ]; then
        file_count=$(find "$vault_path" -type f 2>/dev/null | wc -l | tr -d ' ')
    fi
    scaled=$((60 + file_count / 200))
    [ "$scaled" -gt 600 ] && scaled=600
    echo "$scaled"
}

# Poll /health until it responds OK or $1 seconds pass.
wait_for_health() {
    local timeout="$1" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        curl -s -f --max-time 5 http://localhost:8000/health > /dev/null 2>&1 && return 0
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

# One full unload -> wait-for-teardown -> load(-with-retry) -> health-poll
# cycle. Returns 0 if the server answers /health afterward, 1 otherwise.
restart_cycle() {
    launchctl unload "$PLIST_PATH" 2>>"$LOG_FILE" || true
    if ! wait_for_teardown 30; then
        log "WARNING: previous instance did not tear down within 30s — starting anyway"
    fi
    start_with_retry || return 1
    wait_for_health "$(health_check_timeout)"
}

# Everything below is the operational run — wrapped in a function, guarded so
# tests can `source` this script to call the decision helpers above without
# tripping the opt-in gate, guards, or a real git fetch/pull/launchctl call.
main() {

# --- Opt-in gate ---------------------------------------------------------------
case "$(_read_env "LIFEOS_AUTODEPLOY_ENABLED" "false" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) ;;
    *) exit 0 ;;
esac

# --- Defer while the nightly sync is running ------------------------------------
if sync_marker_in_progress; then
    log "skip: nightly sync in progress — deferring update to avoid a mid-sync restart"
    exit 0
fi

# --- Guards: only auto-update a clean main --------------------------------------
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$BRANCH" != "main" ]; then
    log "skip: on branch '$BRANCH', not main"
    exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "skip: tracked files modified — not auto-updating over local edits"
    exit 0
fi

# --- Fetch and compare ----------------------------------------------------------
if ! git fetch --quiet origin main 2>>"$LOG_FILE"; then
    log "ERROR: git fetch failed"
    notify_failure "🚨 *LifeOS Auto-Update (macOS)*
\`git fetch\` failed on the host — check network / SSH auth."
    exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
    log "main advanced: ${LOCAL:0:7} -> ${REMOTE:0:7}"
    if ! git pull --ff-only --quiet origin main 2>>"$LOG_FILE"; then
        log "ERROR: --ff-only pull failed (local main diverged from origin)"
        notify_failure "🚨 *LifeOS Auto-Update (macOS)*
Fast-forward pull failed — local \`main\` has diverged from origin. Manual fix needed."
        exit 1
    fi
fi

# --- Drift check: compare code/.env mtimes against our own last restart ---------
CODE_MTIME=$(newest_code_mtime)
ENV_MTIME=$(env_file_mtime)
RESTART_EPOCH=$(last_restart_epoch) || {
    log "no restart baseline yet — establishing one, will compare from the next run"
    mark_restarted
    exit 0
}

STALE=false
[ -n "$CODE_MTIME" ] && [ "$RESTART_EPOCH" -lt "$CODE_MTIME" ] && STALE=true
[ -n "$ENV_MTIME" ] && [ "$RESTART_EPOCH" -lt "$ENV_MTIME" ] && STALE=true
if [ "$STALE" != true ]; then
    exit 0   # nothing stale
fi

log "drift detected (code/.env changed since last restart) — restarting $PLIST_NAME"
if restart_cycle; then
    mark_restarted
    log "restart OK, health confirmed"
    notify_success "✅ *LifeOS Auto-Update (macOS)*
Restarted — was running stale code/config. Health OK."
    exit 0
fi

log "first restart cycle did not come up healthy — retrying once"
if restart_cycle; then
    mark_restarted
    log "retry restart OK, health confirmed"
    notify_success "✅ *LifeOS Auto-Update (macOS)*
Restarted (after one retry) — was running stale code/config. Health OK."
    exit 0
fi

log "restart FAILED after retry — alerting"
notify_failure "🚨 *LifeOS Auto-Update (macOS)*
Restarted $PLIST_NAME twice but \`/health\` never came back. Check the host."
exit 1

}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
