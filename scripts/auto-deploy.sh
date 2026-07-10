#!/bin/bash
# LifeOS Auto-Deploy
# ==================
# Polls origin/main and, when it advances, fast-forward pulls the new code and
# restarts the services whose code the deploy touched. Pull-based by design: it
# needs no inbound network, so it works on this WiFi-only host and simply catches
# up on the next tick after any outage.
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
if [ -n "$(git status --porcelain)" ]; then
    log "skip: working tree dirty — not auto-deploying over local edits"
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
[ "$LOCAL" = "$REMOTE" ] && exit 0   # up to date — the common case, stay silent

log "main advanced: ${LOCAL:0:7} -> ${REMOTE:0:7}"
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")   # capture before the pull moves HEAD

# --- Fast-forward pull ----------------------------------------------------------
if ! git pull --ff-only --quiet origin main 2>>"$LOG_FILE"; then
    log "ERROR: --ff-only pull failed (local main diverged from origin)"
    notify_failure "🚨 *LifeOS Auto-Deploy*
Fast-forward pull failed — local \`main\` has diverged from origin. Manual fix needed."
    exit 1
fi

# --- Install dependencies if they changed ---------------------------------------
if echo "$CHANGED" | grep -q '^requirements\.txt$'; then
    log "requirements.txt changed — installing into $VENV_DIR"
    if ! "$VENV_DIR/bin/pip" install -q -r requirements.txt >>"$LOG_FILE" 2>&1; then
        log "ERROR: pip install failed — NOT restarting services"
        notify_failure "🚨 *LifeOS Auto-Deploy*
\`pip install\` failed after pulling new requirements. Services were NOT restarted."
        exit 1
    fi
fi

# --- Decide whether a restart is needed -----------------------------------------
# Docs / tests / static frontend / CI config ship without a service restart.
CODE_CHANGED=$(echo "$CHANGED" | grep -vE '^(docs/|tests/|web/|\.github/|plans/)|\.md$' || true)
if [ -z "$CODE_CHANGED" ]; then
    log "deployed ${REMOTE:0:7} (docs/tests only — no restart)"
    notify_success "✅ *LifeOS Auto-Deploy*
Pulled \`${REMOTE:0:7}\` (docs/tests only — no restart)."
    exit 0
fi

# Restart every *currently active* code service. Restarting only running units
# keeps opt-in services (agent-worker, mcp-http) off if the operator left them
# off, and restarting the API on any code change is correct because it imports
# the worker/mcp modules too. llm + chromadb hold no repo Python that changes.
RESTARTED=()
FAILED=()
for unit in lifeos-api lifeos-agent-worker lifeos-mcp-http; do
    systemctl is-active --quiet "$unit" || continue
    if sudo -n systemctl restart "$unit" 2>>"$LOG_FILE"; then
        RESTARTED+=("$unit")
    else
        FAILED+=("$unit")
        log "ERROR: restart failed for $unit"
    fi
done

if [ "${#FAILED[@]}" -gt 0 ]; then
    log "deployed ${REMOTE:0:7} but restart FAILED: ${FAILED[*]}"
    notify_failure "🚨 *LifeOS Auto-Deploy*
Pulled \`${REMOTE:0:7}\` but restart failed: ${FAILED[*]}. Manual intervention needed."
    exit 1
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
    log "deployed ${REMOTE:0:7}, restarted ${RESTARTED[*]}, but /health did not recover"
    notify_failure "🚨 *LifeOS Auto-Deploy*
Deployed \`${REMOTE:0:7}\` and restarted ${RESTARTED[*]}, but \`/health\` never came back. Check the host."
    exit 1
fi

log "deployed ${REMOTE:0:7}, restarted ${RESTARTED[*]}, health OK"
notify_success "✅ *LifeOS Auto-Deploy*
Deployed \`${REMOTE:0:7}\`, restarted ${RESTARTED[*]}. Health OK."
exit 0
