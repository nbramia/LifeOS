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
# Advisory lock shared with scripts/run_all_syncs.py (#793) — see
# sync_in_progress_lock_acquire() below for why this replaced a
# pid-in-a-file marker.
SYNC_LOCK_FILE="$PROJECT_DIR/data/sync.lock"
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

# .env's mtime — a second, independent drift signal (#792). Editing .env is
# expected to require a manual restart, but that expectation only holds if
# something actually notices the edit; .env is deliberately untracked (it
# holds secrets), so it's invisible to newest_code_mtime()'s `git ls-files`
# above, and a config-only change (a rotated token, a new backend flag) could
# otherwise go unnoticed by every running service indefinitely, until an
# unrelated code change happens to restart the same unit. Kept as its own
# check rather than folded into newest_code_mtime() itself, so that function's
# "only tracked files count" contract — and the test asserting it — stays
# exactly as-is. Empty output (no error) when .env doesn't exist on this host.
env_file_mtime() {
    [ -f "$ENV_FILE" ] || return 0
    stat -c '%Y' "$ENV_FILE" 2>/dev/null
}

# Path to the per-unit record of which $ENV_MTIME value a unit was last
# actually restarted for. Per-unit because different units can be restarted
# (or deferred) at different times.
env_mtime_applied_file() {
    echo "$PROJECT_DIR/data/env-mtime-applied-$1"
}

# Is $1 (a unit, started at $2) stale with respect to .env — found on
# review: comparing $active_since (a wall-clock timestamp) against
# $ENV_MTIME directly breaks if ENV_MTIME is ever in the future relative to
# real time (clock skew, `rsync -t` preserving a future mtime, a manual
# `touch` with a bad date) — active_since, even moments after a restart
# that just happened, is still "before" a future ENV_MTIME, so the unit
# would get restarted again on every single tick forever. Comparing the
# exact ENV_MTIME VALUE this unit was last actually restarted for (rather
# than wall-clock times) breaks that loop: once restarted for a given .env
# edit, that same edit never triggers a second restart no matter what the
# clock does afterward. Falls back to the original active_since comparison
# only when no applied-marker exists yet for this unit (a fresh install, or
# one upgrading to this fix for the first time), so behavior for an
# already-configured install is unchanged unless/until that fallback path
# itself restarts once — at which point the marker takes over.
env_stale_for_unit() {
    local unit="$1" active_since="$2" applied
    [ -n "$ENV_MTIME" ] || return 1
    applied=$(cat "$(env_mtime_applied_file "$unit")" 2>/dev/null) || applied=""
    if [ -n "$applied" ]; then
        [ "$ENV_MTIME" != "$applied" ]
    else
        [ "$active_since" -lt "$ENV_MTIME" ]
    fi
}

# Record that $1 (a unit) has just been restarted while $ENV_MTIME was
# current, so env_stale_for_unit never re-triggers for this same edit.
mark_env_mtime_applied() {
    [ -n "$ENV_MTIME" ] || return 0
    mkdir -p "$PROJECT_DIR/data"
    echo "$ENV_MTIME" > "$(env_mtime_applied_file "$1")"
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

# Authoritative "is the worker mid-session" check (#631 gap 3, narrowed by
# #636). Queries the SQLite session store rather than guessing from process
# CPU or log recency.
#
# Busy means genuinely-active work only: claimed or running. `yielded` is
# deliberately excluded (#636) — the worker's own recovery path skips yielded
# sessions with "Sleeping sessions are healthy — main loop will wake them",
# so a restart does not harm them. `list_non_terminal()` (which includes
# yielded) answers "which sessions must I look at?", not "which would a
# restart harm?" — a wider set, and using it meant one abandoned yielded
# session pinned the worker on stale code on every tick forever. One such
# session, 82 days old, was doing exactly that when this was found.
#
# Any failure to ask (missing venv, locked DB, import error) is still treated
# as busy: killing live work is worse than one more 10-minute stale tick.
#
# LIFEOS_AGENT_SESSIONS_DB is test-only: unset in production, so SessionStore()
# always resolves its own repo-root-anchored default there (unchanged
# behavior). Tests source this script from a sandbox cwd and need worker_busy
# to read a seeded throwaway store instead of the real one — the same problem
# solved for the venv itself via $LIFEOS_VENV above.
worker_busy() {
    "$VENV_DIR/bin/python" -c "
import os
import sys
try:
    from api.services.agent_worker.session_store import (
        STATUS_CLAIMED,
        STATUS_RUNNING,
        SessionStore,
    )
    active = {STATUS_CLAIMED, STATUS_RUNNING}
    db_path = os.environ.get('LIFEOS_AGENT_SESSIONS_DB')
    store = SessionStore(db_path=db_path) if db_path else SessionStore()
    busy = [s for s in store.list_non_terminal() if s.status in active]
    sys.exit(0 if busy else 2)
except Exception as e:
    print(f'worker_busy check failed: {e}', file=sys.stderr)
    sys.exit(3)
" 2>>"$LOG_FILE"
    [ $? -eq 2 ] && return 1   # confirmed idle
    return 0                   # busy, or unknown/error -> treat as busy
}

# Tier 1 of the "is a sync actually running" check: a systemd-launched sync.
# Cheap and authoritative when it applies, so it's checked before ever
# touching the lock file below.
sync_in_progress_systemd() {
    case "$(systemctl show lifeos-sync.service -p ActiveState --value 2>/dev/null)" in
        activating|active|deactivating|reloading) return 0 ;;
    esac
    return 1
}

# Tier 2: a manually-launched sync, via the shared advisory lock
# scripts/run_all_syncs.py holds on $SYNC_LOCK_FILE for its whole run (#793).
#
# This replaced two earlier approaches, each with a real failure mode found
# on review:
#   - `pgrep -f "run_all_syncs\.py"` matched ANY process whose command line
#     merely mentioned the script's name/path as an argument (e.g. a
#     remote-shell invocation, or — observed live — a code-review tool
#     handed a diff that quotes the filename), deferring deploys for no
#     reason.
#   - A pid-in-a-file marker (checked once, `kill -0`'d) fixed that, but
#     checking it once and then restarting seconds later is TOCTOU: a sync
#     starting in that gap gets killed mid-run by the restart it should have
#     blocked. A marker's recorded pid can also be reused by an unrelated
#     process after the sync that wrote it exits, reading as "still alive"
#     forever. And a single global marker can't represent two overlapping
#     manual syncs — the second overwrites the first's marker and can clear
#     it out from under the still-running first sync on exit.
#
# A kernel-held advisory lock has none of those problems. run_all_syncs.py
# takes a SHARED lock (LOCK_SH) for its whole run — any number of syncs may
# hold it concurrently, each independently, with no "the one marker" to
# race. This function takes the matching EXCLUSIVE, non-blocking lock
# (LOCK_EX | LOCK_NB) via `flock`(1) on fd 9: it succeeds (sync NOT in
# progress) only when no process holds the shared lock, and the caller is
# expected to hold fd 9 open for its *entire* restart section, not just this
# check — closing the TOCTOU gap, because a sync trying to start meanwhile
# blocks (briefly, on its own LOCK_SH acquisition) until the restart section
# releases fd 9, rather than starting into a restart. There is no cleanup to
# reason about on a hard kill either: the kernel releases a flock the
# instant its holding process exits, for any reason.
#
# Returns 0 (sync in progress — caller must not proceed) if the lock could
# NOT be acquired; 1 (clear to proceed) if it was acquired, in which case fd
# 9 is left open and locked for the caller to release later.
sync_in_progress_lock_acquire() {
    mkdir -p "$(dirname "$SYNC_LOCK_FILE")"
    exec 9>"$SYNC_LOCK_FILE"
    flock -x -n 9 || return 0
    return 1
}

sync_in_progress_lock_release() {
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
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
if sync_in_progress_systemd; then
    log "skip: nightly sync in progress — deferring deploy to avoid a mid-sync restart"
    exit 0
fi
# Exclusive, non-blocking lock held from here through the end of this
# function (released just before main() returns, below) — not just checked
# once — so a sync starting anywhere in between is never restarted out from
# under itself (#793's TOCTOU finding). Any process exit on any path below
# also releases it, via the kernel, with or without the explicit release.
if sync_in_progress_lock_acquire; then
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
ENV_MTIME=$(env_file_mtime)
if [ -n "$CODE_MTIME" ] || [ -n "$ENV_MTIME" ]; then
    for unit in lifeos-api lifeos-agent-worker lifeos-mcp-http; do
        systemctl is-active --quiet "$unit" || continue
        active_since=$(service_active_since_epoch "$unit") || {
            log "drift-check: could not read $unit's start time — skipping"
            continue
        }
        # Stale if EITHER signal moved past this unit's start — tracked
        # source and .env are independent triggers, not folded together (#792).
        code_stale=false
        env_stale=false
        [ -n "$CODE_MTIME" ] && [ "$active_since" -lt "$CODE_MTIME" ] && code_stale=true
        env_stale_for_unit "$unit" "$active_since" && env_stale=true
        { [ "$code_stale" = true ] || [ "$env_stale" = true ]; } || continue   # current, nothing to do

        if [ "$code_stale" = true ] && [ "$env_stale" = true ]; then
            drift_msg="$unit active since $(date -d "@$active_since" '+%F %T'), code on disk changed $(date -d "@$CODE_MTIME" '+%F %T') and .env changed $(date -d "@$ENV_MTIME" '+%F %T')"
        elif [ "$env_stale" = true ]; then
            drift_msg="$unit active since $(date -d "@$active_since" '+%F %T'), .env changed $(date -d "@$ENV_MTIME" '+%F %T')"
        else
            drift_msg="$unit active since $(date -d "@$active_since" '+%F %T'), code on disk changed $(date -d "@$CODE_MTIME" '+%F %T')"
        fi

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
            mark_env_mtime_applied "$unit"
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
# Every exit path in this function terminates the process, and the kernel
# releases fd 9's flock the instant that happens regardless — this explicit
# release is redundant here, kept only so the lock's lifetime reads clearly
# as "acquired near the top, released at the bottom" rather than "acquired,
# then implicitly relies on process exit."
sync_in_progress_lock_release
exit 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
