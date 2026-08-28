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
# Where this script records which ENV_MTIME value was last actually applied
# via a restart — see env_stale()/mark_env_mtime_applied() below (#792
# follow-up: a future-dated mtime must restart at most once, not forever).
ENV_MTIME_APPLIED_FILE="${LIFEOS_MACOS_ENV_APPLIED_FILE:-$PROJECT_DIR/data/macos-env-mtime-applied}"
# Advisory lock shared with scripts/run_all_syncs.py (#793) and
# scripts/auto-deploy.sh — see sync_in_progress_lock_acquire() below.
# Host-wide (under $HOME, not $PROJECT_DIR/data) — same path auto-deploy.sh
# uses, and for the same reason: a sync launched from a different checkout
# of this repo must still be visible here. Deliberately not overridable via
# an env var (found on re-review) — see auto-deploy.sh's identical comment
# on SYNC_LOCK_FILE for why an override would silently split-brain against
# run_all_syncs.py's own .env-driven lock path.
SYNC_LOCK_FILE="$HOME/.lifeos/sync.lock"
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

# --- launchd primitives ------------------------------------------------------

# Current PID of the running service (columnar `launchctl list` output —
# same convention scripts/service.sh's status_macos() already parses), or
# empty if not running / not loaded.
api_pid() {
    launchctl list 2>/dev/null | awk -v label="$PLIST_NAME" '$3 == label { print $1 }'
}

# Real wall-clock time the currently-running API process actually started,
# queried from the OS itself — return 1 (unknown) if the service isn't
# running. launchd exposes no equivalent of systemd's ActiveEnterTimestamp
# (see auto-deploy.sh's service_active_since_epoch), but `ps -o lstart=`
# reports a process's real start time on both macOS (the actual target) and
# Linux (where this is developed/tested under bash), giving the same
# ground-truth comparison auto-deploy.sh uses for CODE_MTIME.
#
# This replaced an earlier design that tracked its OWN last-restart time in
# a marker file — found on review to have a first-run bug: on the very
# first opted-in run, a pull could update code on disk, and that marker
# would be written as "already restarted" (main() established it as a
# no-op baseline) without the still-running, still-old-code process ever
# actually being restarted, silently leaving stale code running
# indefinitely. Comparing against the process's real start time has no
# such bootstrap gap — it's accurate from the very first run, because it
# never depends on this script's own history.
#
# Return codes distinguish "nothing to check" from "something is broken" —
# found on review: collapsing every failure mode to a single "not running,
# do nothing" made a genuine parsing bug (5 distinct causes were possible,
# including BSD `date -j -f` choking on `ps`'s own leading whitespace) look
# identical to the ordinary, expected "service isn't loaded yet" case —
# untestable under Linux's coreutils `date`, and silently permanent on the
# actual target OS. 0 = success (epoch on stdout). 1 = not running (no pid,
# or the pid vanished between api_pid() and this ps call — a benign race,
# not an error). 2 = pid is alive but its start time could not be parsed —
# a real problem worth alerting on, not silence.
#
# `uname` gates which `date` form is even attempted, rather than trying GNU
# first and falling back to BSD on any failure: a BSD `date -j -f` given
# GNU-style input (or vice versa) can exit 0 with a nonsense epoch instead
# of failing, which `||`-based fallback can't detect but picking the right
# form up front avoids entirely.
api_active_since_epoch() {
    local pid ps_out epoch
    pid=$(api_pid) || return 1
    [ -n "$pid" ] && [ "$pid" != "-" ] || return 1
    ps_out=$(ps -p "$pid" -o lstart= 2>/dev/null)
    if [ -z "$ps_out" ]; then
        return 1  # pid vanished between api_pid() and this call — not running
    fi
    # Trim padding ps may add around a single requested column.
    ps_out="${ps_out#"${ps_out%%[![:space:]]*}"}"
    ps_out="${ps_out%"${ps_out##*[![:space:]]}"}"
    if [ "$(uname)" = "Darwin" ]; then
        epoch=$(date -j -f "%a %b %e %T %Y" "$ps_out" +%s 2>/dev/null)
    else
        epoch=$(date -d "$ps_out" +%s 2>/dev/null)
    fi
    if [ -z "$epoch" ]; then
        log "ERROR: could not parse process start time from ps output: '$ps_out'"
        return 2
    fi
    echo "$epoch"
}

# Is the running API service stale with respect to .env? Requires BOTH
# active_since predating the edit AND this exact edit not already recorded
# as applied — see auto-deploy.sh's env_stale_for_unit() for the full
# rationale (a future-dated mtime needs the marker to stop restarting
# forever; a marker-only check is wrong once a manual `launchctl unload;
# load` picks up an edit the marker never saw, and would restart the
# service again on the very next tick even though it's already current).
env_stale() {
    local active_since="$1" applied
    [ -n "$ENV_MTIME" ] || return 1
    applied=$(cat "$ENV_MTIME_APPLIED_FILE" 2>/dev/null) || applied=""
    [ "$active_since" -lt "$ENV_MTIME" ] && [ "$ENV_MTIME" != "$applied" ]
}

# Writes to a temp file, reads it back to confirm it landed intact, and only
# then atomically renames it into place — see auto-deploy.sh's
# mark_env_mtime_applied() for why a plain `echo > file` isn't durable
# enough here.
mark_env_mtime_applied() {
    [ -n "$ENV_MTIME" ] || return 0
    mkdir -p "$(dirname "$ENV_MTIME_APPLIED_FILE")"
    local tmp="${ENV_MTIME_APPLIED_FILE}.tmp.$$"
    if printf '%s' "$ENV_MTIME" > "$tmp" 2>/dev/null \
        && [ "$(cat "$tmp" 2>/dev/null)" = "$ENV_MTIME" ] \
        && mv -f "$tmp" "$ENV_MTIME_APPLIED_FILE" 2>/dev/null
    then
        :
    else
        log "ERROR: could not durably record env-mtime-applied — may restart again next tick"
        rm -f "$tmp" 2>/dev/null
    fi
}

# --- Sync-in-progress defer / mutual exclusion (#793's flock, shared with
# scripts/auto-deploy.sh) ----------------------------------------------------
# No systemd unit to check on macOS — just the advisory lock
# scripts/run_all_syncs.py holds (LOCK_SH) for its whole run. Taking the
# matching EXCLUSIVE, non-blocking lock here (LOCK_EX | LOCK_NB) and holding
# it for this script's ENTIRE operational body (not just this check) also
# gives this script something it lacked on review: mutual exclusion between
# two overlapping invocations of ITSELF (e.g. two overlapping cron/timer
# ticks) — the second one simply fails to acquire and defers this tick,
# rather than both interleaving `launchctl unload`/`load` calls against the
# same service.
# Deliberately NOT the `flock`(1) command auto-deploy.sh uses: it's a
# util-linux tool that ships on every Linux distro but is NOT part of a
# stock macOS install (no equivalent bundled with Darwin) — using it here,
# on the one script that actually targets macOS, would make every
# invocation fail with "command not found", which the "any unexpected
# failure means defer" hardening below would then treat as "always defer,
# never actually update." python3's stdlib `fcntl` module wraps the exact
# same underlying flock(2) syscall and is already a hard dependency of this
# whole project (run_all_syncs.py itself, whose lock this checks). The
# trick this relies on: fd 9, opened by `exec` in THIS shell, is inherited
# by the python3 child below; flock(2) locks belong to the open file
# description, not to any one process's fd table, so a lock the child
# acquires on its inherited copy of fd 9 is still held by fd 9 in this
# shell after the child exits — exactly how the real `flock`(1) command
# implements its own "operate on an already-open fd, no wrapped command"
# mode. Exit code 75 (matching auto-deploy.sh's `flock -E 75` convention)
# means "someone else holds it"; anything else is a genuine error, not a
# sync — see auto-deploy.sh's identical function for why that distinction
# matters (a broken lock mechanism must not silently look like "sync busy"
# in the log forever).
# Resolve a python3 interpreter: prefer the project venv's own — found on
# review: bare `python3` under cron/launchd's minimal PATH can resolve to
# macOS's Command Line Tools placeholder shim (prompts to install Xcode
# tools and exits 127 rather than running anything), which the "any
# unexpected exit code defers" hardening in the caller turns into "this
# lock check always fails, forever." Falls back to PATH's python3 for a
# host where the venv doesn't exist yet.
_python_bin() {
    local venv_python="$HOME/.venvs/lifeos/bin/python"
    if [ -x "$venv_python" ]; then
        echo "$venv_python"
    else
        echo "python3"
    fi
}

sync_in_progress_lock_acquire() {
    mkdir -p "$(dirname "$SYNC_LOCK_FILE")"
    exec 9>"$SYNC_LOCK_FILE" || {
        log "ERROR: could not open $SYNC_LOCK_FILE for locking — deferring as a precaution"
        return 0
    }
    local rc
    "$(_python_bin)" -c '
import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
except OSError:
    sys.exit(1)
'
    rc=$?
    case "$rc" in
        0) return 1 ;;
        75) return 0 ;;
        *)
            log "ERROR: lock acquisition failed unexpectedly (exit $rc) on $SYNC_LOCK_FILE — deferring as a precaution"
            return 0
            ;;
    esac
}

# Closing fd 9 releases whatever lock it held — no separate "unlock" call
# needed (and, per the comment above, no `flock`(1) command to make one
# with anyway).
sync_in_progress_lock_release() {
    exec 9>&- 2>/dev/null || true
}

# Poll until $1 (a pid captured BEFORE `launchctl unload` was called) is
# actually gone, up to $2 seconds. Returns 0 once torn down, 1 on timeout
# (caller proceeds anyway and logs a warning — better to try starting than
# to hang forever).
#
# Found on review: an earlier version polled api_pid() (`launchctl list`)
# AFTER calling unload, instead of a pid captured before it — `unload`
# deregisters the job from launchd's list as soon as it *asks* the process
# to exit, well before the process has actually torn down, so api_pid()
# read back empty immediately and the poll returned success instantly, with
# the still-shutting-down old process potentially still bound to the port —
# exactly the collision this whole restart sequence exists to prevent.
# `kill -0` on the specific pid captured up front has no such gap: it stays
# true until that exact process is actually gone, regardless of what
# launchd's own bookkeeping shows in the meantime.
wait_for_pid_gone() {
    local pid="$1" timeout="${2:-30}" waited=0
    [ -n "$pid" ] || return 0
    while kill -0 "$pid" 2>/dev/null; do
        [ "$waited" -ge "$timeout" ] && return 1
        sleep 1
        waited=$((waited + 1))
    done
    return 0
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
        # Guard against anything but a plain non-negative integer (found on
        # review: an unexpected `wc`/locale quirk, or a `find` failure mode
        # that doesn't cleanly reduce to "0", must not corrupt the
        # arithmetic below under set -u) — fall back to "no scaling."
        case "$file_count" in
            ''|*[!0-9]*) file_count=0 ;;
        esac
    fi
    scaled=$((60 + file_count / 200))
    [ "$scaled" -gt 600 ] && scaled=600
    echo "$scaled"
}

# Poll /health until it responds OK or $1 seconds pass. Uses $SECONDS (real
# elapsed wall-clock time since the shell started) rather than counting
# iterations — found on review: `curl --max-time 5` can itself take up to
# 5s per attempt on top of the 5s `sleep`, so counting "5 per iteration"
# could let actual elapsed time run to nearly double the given timeout
# before the loop notices.
wait_for_health() {
    local timeout="$1" start=$SECONDS
    while [ $((SECONDS - start)) -lt "$timeout" ]; do
        curl -s -f --max-time 5 http://localhost:8000/health > /dev/null 2>&1 && return 0
        sleep 5
    done
    return 1
}

# One full unload -> wait-for-teardown -> load(-with-retry) -> health-poll
# cycle. Returns 0 if the server answers /health afterward, 1 otherwise.
# Captures the outgoing process's pid BEFORE calling unload — see
# wait_for_pid_gone()'s comment for why the poll must target that specific
# pid rather than re-querying `launchctl list` after unload runs. Takes the
# health-check timeout as a parameter, computed once by main() right before
# the first restart attempt — after STALE is known true and outside the
# lock's critical section (found on review: health_check_timeout() scans
# the whole vault via `find`; computing it unconditionally on every tick
# kept a possibly-sleeping external disk spinning even on the common
# nothing-to-do path, and computing it while holding the exclusive lock
# made a slow vault also make a waiting sync wait longer than necessary for
# no reason, since the scan itself touches nothing the lock protects).
restart_cycle() {
    local health_timeout="$1"
    local old_pid
    old_pid=$(api_pid)
    launchctl unload "$PLIST_PATH" 2>>"$LOG_FILE" || true
    if ! wait_for_pid_gone "$old_pid" 30; then
        log "WARNING: previous instance (pid $old_pid) did not tear down within 30s — starting anyway"
    fi
    start_with_retry || return 1
    wait_for_health "$health_timeout"
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

# --- Defer while the nightly sync is running (or another instance of this
# script is already mid-restart) --------------------------------------------
# Exclusive, non-blocking lock held from here through the end of this
# function — not just checked once — so a sync (or an overlapping invocation
# of this same script) starting anywhere in between is never raced. A trap
# releases it on ANY exit from this point on (see auto-deploy.sh's identical
# trap for why that's more robust than an explicit release call on every
# exit path — this file had exactly the gap it warns about: the "API
# service not running" exit below had no release call at all before the
# trap was added).
if sync_in_progress_lock_acquire; then
    log "skip: nightly sync (or another update in progress) — deferring to avoid a mid-sync restart"
    exit 0
fi
trap sync_in_progress_lock_release EXIT

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

# --- Drift check: compare code/.env against the API process's REAL start
# time (ground truth from the OS, not a self-tracked marker — see
# api_active_since_epoch()'s comment for the first-run bug this fixes) ------
ACTIVE_SINCE=$(api_active_since_epoch)
RC=$?
if [ "$RC" -eq 1 ]; then
    log "API service not running — nothing to check for drift"
    exit 0
elif [ "$RC" -ne 0 ]; then
    notify_failure "🚨 *LifeOS Auto-Update (macOS)*
Could not determine $PLIST_NAME's start time — check the host (see auto-update-macos.log)."
    exit 1
fi
CODE_MTIME=$(newest_code_mtime)
ENV_MTIME=$(env_file_mtime)

STALE=false
[ -n "$CODE_MTIME" ] && [ "$ACTIVE_SINCE" -lt "$CODE_MTIME" ] && STALE=true
env_stale "$ACTIVE_SINCE" && STALE=true
if [ "$STALE" != true ]; then
    exit 0   # nothing stale
fi

log "drift detected (code/.env changed since $PLIST_NAME started) — restarting"
# Computed only now (STALE is true) and outside the lock's critical
# section — see restart_cycle()'s comment above for why.
HEALTH_TIMEOUT=$(health_check_timeout)
if restart_cycle "$HEALTH_TIMEOUT"; then
    mark_env_mtime_applied
    log "restart OK, health confirmed"
    notify_success "✅ *LifeOS Auto-Update (macOS)*
Restarted — was running stale code/config. Health OK."
    exit 0
fi

log "first restart cycle did not come up healthy — retrying once"
if restart_cycle "$HEALTH_TIMEOUT"; then
    mark_env_mtime_applied
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
