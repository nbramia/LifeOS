#!/bin/bash
# LifeOS Auto-Deploy
# ==================
# Polls origin/main and, when it advances, fast-forward pulls the new code.
# Pull-based by design: it needs no inbound network, so it works on this
# WiFi-only host and simply catches up on the next tick after any outage.
#
# Whether to restart a service is decided by a separate, unconditional drift
# check (every tick, pull or no pull) that compares what's actually running
# against what's on disk — see "Verify what's actually running" below. Earlier
# this inferred "services are current" from "nothing to pull", which is false
# for a merge performed in THIS checkout: local == remote the instant it's
# pushed, so that merge was never deployed (#631).
#
# Triggered by lifeos-autodeploy.timer (installed by setup-systemd.sh), default
# every 10 min. Opt-in: does nothing unless LIFEOS_AUTODEPLOY_ENABLED=true in .env.
#
# Safety guards (any tripped → skip this run, change nothing):
#   - must be on the main branch (never touches a feature branch you're working on)
#   - working tree must be clean (never clobbers local edits)
#   - pull is --ff-only (a diverged main is a real problem → alert, never reset)
#
# Notifications (LIFEOS_AUTODEPLOY_NOTIFY): failure (default) | always | never.
#
# Runs as the repo owner (not root): git pull uses the user's SSH key; service
# restarts go through the passwordless sudoers rule installed by setup-systemd.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || exit 1

LOG_FILE="$PROJECT_DIR/logs/auto-deploy.log"
ENV_FILE="$PROJECT_DIR/.env"
VENV_DIR="${LIFEOS_VENV:-$HOME/.venvs/lifeos}"
# Non-interactive git over SSH: fail fast instead of prompting for a passphrase.
export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'

mkdir -p "$PROJECT_DIR/logs"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"; }

# Read a KEY=value from .env, stripping quotes/whitespace/inline comments.
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
    /usr/bin/curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d "chat_id=${chat_id}" \
        -d "text=${message}" \
        -d "parse_mode=Markdown" > /dev/null 2>&1 || true
}

NOTIFY=$(_read_env "LIFEOS_AUTODEPLOY_NOTIFY" "failure")
notify_failure() { [ "$NOTIFY" != "never" ] && send_telegram "$1"; }
notify_success() { [ "$NOTIFY" = "always" ] && send_telegram "$1"; }

# --- Drift check helpers (#631) --------------------------------------------------
#
# Newest on-disk mtime among tracked files under the paths lifeos-api,
# lifeos-mcp-http, and lifeos-agent-worker all import (api/, config/,
# mcp_server.py — the same set the post-commit hook watches). `git ls-files`
# (not `find`) so __pycache__/*.pyc and other untracked/generated files never
# count: a .pyc's mtime tracks its last import, not its last real edit, which
# would make every service look permanently "current" the moment it runs.
#
# mtime, not `git log`'s commit date: scripts/post-commit normalizes every
# commit's author/committer date to 12:00 UTC (privacy: hides work schedule),
# so commit dates don't reliably order same-day commits, and a rebased or
# cherry-picked commit can carry an old author date that predates when it
# actually landed on main. A file's mtime is set by the checkout itself
# (pull/merge rewrite the file at real wall-clock time) and neither
# normalization touches it, so it's the sturdier signal for "did the code on
# disk actually change, and when."
newest_code_mtime() {
    git ls-files -- api config mcp_server.py 2>/dev/null \
        | xargs -r stat -c '%Y' 2>/dev/null \
        | sort -rn | head -1
}

# Real wall-clock time $1's current process started, on this host's own
# clock — the same clock `newest_code_mtime` read the mtimes from, so there's
# no cross-host skew to worry about. Empty output (return 1) means "unknown"
# (unit inactive, or systemd/date failed to parse); callers must skip rather
# than guess.
service_active_since_epoch() {
    local raw
    raw=$(systemctl show "$1" -p ActiveEnterTimestamp --value 2>/dev/null)
    [ -n "$raw" ] && [ "$raw" != "n/a" ] || return 1
    date -d "$raw" +%s 2>/dev/null
}

# Authoritative "is the worker mid-session" check (#631 gap 3). Queries the
# same SQLite session store the worker itself consults on restart —
# SessionStore.list_non_terminal(), the "sessions the worker may still need to
# act on" set (claimed/running/yielded) — rather than guessing from process
# CPU or log recency. Any failure to ask it (missing venv, locked DB, import
# error) is treated as busy: killing an in-flight #agent session is worse than
# leaving the worker one more 10-minute tick stale.
worker_busy() {
    "$VENV_DIR/bin/python" -c "
import sys
try:
    from api.services.agent_worker.session_store import SessionStore
    sys.exit(0 if SessionStore().list_non_terminal() else 2)
except Exception as e:
    print(f'worker_busy check failed: {e}', file=sys.stderr)
    sys.exit(3)
" 2>>"$LOG_FILE"
    [ $? -eq 2 ] && return 1   # confirmed idle
    return 0                   # busy, or unknown/error -> treat as busy
}

# Everything below is the operational run — wrapped in a function, guarded so
# it only fires when the file is executed directly, so tests can `source` this
# script to call the decision helpers above (newest_code_mtime,
# service_active_since_epoch, worker_busy) against a synthetic repo/stubbed
# systemctl without tripping the opt-in gate, guards, or a real git fetch/pull.
main() {

# --- Opt-in gate ---------------------------------------------------------------
case "$(_read_env "LIFEOS_AUTODEPLOY_ENABLED" "false" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) ;;
    *) exit 0 ;;
esac

# --- Defer while the nightly sync is running ------------------------------------
# Restarting lifeos-api mid-sync SIGTERMs the running reindex, and the restart's
# fresh allocations landing on top of the still-resident embedding process has
# OOM-frozen the host (killed the desktop). Never deploy during a sync — the next
# 10-min tick catches up once it finishes.
sync_in_progress() {
    case "$(systemctl show lifeos-sync.service -p ActiveState --value 2>/dev/null)" in
        activating|active|deactivating|reloading) return 0 ;;
    esac
    # Also catch a sync launched manually (not via the systemd unit).
    pgrep -f "[r]un_all_syncs\.py" >/dev/null 2>&1
}
if sync_in_progress; then
    log "skip: nightly sync in progress — deferring deploy to avoid a mid-sync restart"
    exit 0
fi

# --- Guards: only auto-deploy a clean main --------------------------------------
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$BRANCH" != "main" ]; then
    log "skip: on branch '$BRANCH', not main"
    exit 0
fi
# `--untracked-files=no` is deliberate (#634). The guard's purpose is to avoid
# deploying over uncommitted *edits to tracked code*; an untracked file is not
# that. Counting untracked paths made a single `.worktrees/` directory — the
# conventional location for worktree-based development here, which the
# post-commit hook already expects to exist — skip every tick silently and
# indefinitely: 62 consecutive skips before this was found, during which the
# drift check below never ran at all. `.worktrees/` is now gitignored too;
# this is the class fix, so the next stray scratch file doesn't repeat it.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "skip: tracked files modified — not auto-deploying over local edits"
    exit 0
fi

# --- Fetch and compare ----------------------------------------------------------
if ! git fetch --quiet origin main 2>>"$LOG_FILE"; then
    log "ERROR: git fetch failed"
    notify_failure "🚨 *LifeOS Auto-Deploy*
\`git fetch\` failed on the host — check network / SSH auth."
    exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
    log "main advanced: ${LOCAL:0:7} -> ${REMOTE:0:7}"
    CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")   # capture before the pull moves HEAD

    # --- Fast-forward pull -------------------------------------------------------
    if ! git pull --ff-only --quiet origin main 2>>"$LOG_FILE"; then
        log "ERROR: --ff-only pull failed (local main diverged from origin)"
        notify_failure "🚨 *LifeOS Auto-Deploy*
Fast-forward pull failed — local \`main\` has diverged from origin. Manual fix needed."
        exit 1
    fi

    # --- Install dependencies if they changed ------------------------------------
    if echo "$CHANGED" | grep -q '^requirements\.txt$'; then
        log "requirements.txt changed — installing into $VENV_DIR"
        if ! "$VENV_DIR/bin/pip" install -q -r requirements.txt >>"$LOG_FILE" 2>&1; then
            log "ERROR: pip install failed — NOT restarting services"
            notify_failure "🚨 *LifeOS Auto-Deploy*
\`pip install\` failed after pulling new requirements. Services were NOT restarted."
            exit 1
        fi
    fi
fi

# --- Verify what's actually running, don't infer from the pull (#631) -----------
# Runs every tick, whether or not the block above found anything to pull — a
# merge committed and pushed from this checkout already has LOCAL == REMOTE by
# the time this fires. Restarting only actually-running units keeps opt-in
# services (agent-worker, mcp-http) off if the operator left them off.
RESTARTED=()
DEFERRED=()
FAILED=()
CODE_MTIME=$(newest_code_mtime)
if [ -n "$CODE_MTIME" ]; then
    for unit in lifeos-api lifeos-agent-worker lifeos-mcp-http; do
        systemctl is-active --quiet "$unit" || continue
        active_since=$(service_active_since_epoch "$unit") || {
            log "drift-check: could not read $unit's start time — skipping"
            continue
        }
        [ "$active_since" -ge "$CODE_MTIME" ] && continue   # current, nothing to do

        drift_msg="$unit active since $(date -d "@$active_since" '+%F %T'), code on disk changed $(date -d "@$CODE_MTIME" '+%F %T')"

        # Worker policy: restart when idle, defer when busy (mirrors the
        # sync_in_progress guard above) — never silently stay stale, never
        # kill an in-flight #agent session either.
        if [ "$unit" = "lifeos-agent-worker" ] && worker_busy; then
            DEFERRED+=("$unit")
            log "defer: $drift_msg — session in flight, retrying next tick"
            continue
        fi

        log "drift: $drift_msg — restarting"
        if sudo -n systemctl restart "$unit" 2>>"$LOG_FILE"; then
            RESTARTED+=("$unit")
        else
            FAILED+=("$unit")
            log "ERROR: restart failed for $unit"
        fi
    done
fi

if [ "${#FAILED[@]}" -gt 0 ]; then
    log "restart FAILED for stale service(s): ${FAILED[*]}"
    notify_failure "🚨 *LifeOS Auto-Deploy*
Drift detected but restart failed: ${FAILED[*]}. Manual intervention needed."
    exit 1
fi

if [ "${#RESTARTED[@]}" -eq 0 ]; then
    exit 0   # nothing was stale (or the only stale unit deferred, already logged)
fi

# --- Post-deploy health check (API takes ~30-60s to reload its ML model) --------
HEALTHY=0
for _ in $(seq 1 18); do
    if curl -s -f --max-time 5 http://localhost:8000/health > /dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 5
done

if [ "$HEALTHY" -ne 1 ]; then
    log "restarted ${RESTARTED[*]} for drift, but /health did not recover"
    notify_failure "🚨 *LifeOS Auto-Deploy*
Restarted ${RESTARTED[*]} (was running stale code), but \`/health\` never came back. Check the host."
    exit 1
fi

log "restarted ${RESTARTED[*]} for drift, health OK"
notify_success "✅ *LifeOS Auto-Deploy*
Restarted ${RESTARTED[*]} — was running stale code. Health OK."
exit 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
