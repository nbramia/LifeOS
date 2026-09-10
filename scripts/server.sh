#!/bin/bash
# LifeOS Server Management Script
# Designed for reliable server management from Claude or command line
#
# Usage: ./scripts/server.sh [start|stop|restart|status|wait|preflight|classify-change|verify-deployed|verify-runtime-evidence|restart-worker-detached|test-instance]
#
# Commands:
#   start                    - Kill any existing processes, start server, wait for health check
#   stop                     - Stop the server
#   restart                  - Stop and start the server
#   status                   - Check if server is running and healthy
#   wait                     - Wait for server to be healthy (use after manual start)
#   preflight                - Check prerequisites before first start
#   classify-change          - Print whether a diff needs a worker or api-only restart (#401)
#   verify-deployed          - Exit 0 only if the checkout is a real work tree on the expected SHA (#419)
#   verify-runtime-evidence - Emit structured startup-bound deployment evidence
#   restart-worker-detached  - Detached restart of lifeos-agent-worker for the doctor (#401)
#   test-instance ACTION     - Private, isolated candidate instance for tests:
#                              run|verify|stop — see scripts/test_instance.py --help. `run` is
#                              the one supported way to exercise a candidate: it starts an
#                              instance, runs a command against it, and always stops it — no
#                              detached "start now, stop later" mode. Never touches the shared
#                              lifeos-api service; this is the ONLY server.sh path that never
#                              calls kill_server/pkill/systemctl.
#
# Expected startup time: 30-60 seconds (loading sentence-transformers model)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Configuration
HOST="0.0.0.0"
PORT="8000"
STARTUP_TIMEOUT=180  # seconds to wait for server to start (model loading can take 2-3 minutes)
HEALTH_URL="http://127.0.0.1:$PORT/health"
CHROMADB_URL="http://localhost:8001/api/v2/heartbeat"
LOG_FILE="$PROJECT_DIR/logs/server.log"
# The agent worker is its own systemd unit (separate from lifeos-api). The
# doctor self-repair persona drives a headless session *inside* this unit, so
# bouncing it kills the doctor's own session — see restart-worker-detached.
WORKER_UNIT="lifeos-agent-worker"
VENV_PYTHON="${LIFEOS_VENV_PYTHON:-$HOME/.venvs/lifeos/bin/python}"
# Files whose change requires the agent worker (not just lifeos-api) to
# restart for the change to take effect — see classify-change.
WORKER_CODE_PATH="api/services/agent_worker/"

# Ensure logs directory exists
mkdir -p "$PROJECT_DIR/logs"

# Colors (only if terminal supports it)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if server is healthy
is_healthy() {
    curl -s -f "$HEALTH_URL" > /dev/null 2>&1
}

# Check if ChromaDB server is healthy
chromadb_healthy() {
    curl -s --max-time 2 "$CHROMADB_URL" > /dev/null 2>&1
}

# check_host_guard — refuse to start on a non-designated machine (#506).
#
# Mirrors check_server_host_guard() in api/main.py so a misconfigured second
# server fails fast and legibly here, before Python (and the 30-60s model
# load) even starts. LIFEOS_SERVER_HOSTNAME unset in .env (the default) never
# blocks a start — safe for a fresh clone.
check_host_guard() {
    local expected=""
    if [ -f "$PROJECT_DIR/.env" ]; then
        expected=$(grep -E "^LIFEOS_SERVER_HOSTNAME=" "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d ' "'"'" || true)
    fi
    if [ -z "$expected" ]; then
        return 0
    fi
    local actual
    actual="$(hostname)"
    if [ "$actual" != "$expected" ]; then
        log_error "Refusing to start: LIFEOS_SERVER_HOSTNAME=$expected but this machine's hostname is $actual."
        log_error "The LifeOS API must run only on $expected. Point this machine at it via LIFEOS_API_URL instead of running a second server."
        return 1
    fi
    return 0
}

# Get server PID
get_server_pid() {
    lsof -ti :$PORT 2>/dev/null || true
}

# Kill all server processes
kill_server() {
    log_info "Stopping any existing server processes..."

    # Method 1: Kill by port — catches whoever is currently bound.
    local pids=$(get_server_pid)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
        log_info "Killed processes on port $PORT: $pids"
    fi

    # Method 2: Kill by process name — catches stragglers that haven't bound
    # yet (mid-startup) or have lost the port. The cmdline we actually launch
    # is `python -c "import uvicorn\nuvicorn.run('api.main:app', ...)"`, NOT
    # `uvicorn api.main:app`, so the legacy pattern silently missed every
    # ghost process. Match against the chunk that's stable across both the
    # script-launched and systemd-launched paths.
    pkill -9 -f "uvicorn.run.*api.main:app" 2>/dev/null || true

    # Method 3: Clean up any HuggingFace lock files that might cause hangs
    rm -f ~/.cache/huggingface/hub/.locks/models--sentence-transformers--all-MiniLM-L6-v2/*.lock 2>/dev/null || true

    sleep 2

    # Verify killed
    if [ -n "$(get_server_pid)" ]; then
        log_error "Failed to kill all server processes"
        return 1
    fi

    log_info "All server processes stopped"
}

# Wait for server to become healthy
wait_for_healthy() {
    local timeout=${1:-$STARTUP_TIMEOUT}
    local start_time=$(date +%s)

    log_info "Waiting for server to become healthy (timeout: ${timeout}s)..."
    log_info "Note: Initial startup loads ML models and takes 30-60 seconds"

    local dots=""
    while true; do
        if is_healthy; then
            echo ""  # New line after dots
            local elapsed=$(($(date +%s) - start_time))
            log_info "Server is healthy (started in ${elapsed}s)"
            return 0
        fi

        local elapsed=$(($(date +%s) - start_time))
        if [ $elapsed -ge $timeout ]; then
            echo ""
            log_error "Server failed to become healthy within ${timeout}s"
            return 1
        fi

        # Print progress every 5 seconds
        if [ $((elapsed % 5)) -eq 0 ]; then
            printf "\r${YELLOW}[WAIT]${NC} Elapsed: ${elapsed}s / ${timeout}s"
        fi

        sleep 1
    done
}

# Rotate server.log if it exceeds 10 MB (keeps 5 rotations)
rotate_log() {
    local max_size=$((10 * 1024 * 1024))  # 10 MB
    local max_rotations=5

    if [ -f "$LOG_FILE" ]; then
        local size
        if [[ "$(uname)" == "Darwin" ]]; then
            size=$(stat -f%z "$LOG_FILE" 2>/dev/null || echo "0")
        else
            size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo "0")
        fi
        if [ "$size" -ge "$max_size" ]; then
            log_info "Rotating server.log ($(($size / 1048576))MB)..."
            # Shift existing rotations
            for i in $(seq $((max_rotations - 1)) -1 1); do
                [ -f "$LOG_FILE.$i" ] && mv "$LOG_FILE.$i" "$LOG_FILE.$((i + 1))"
            done
            mv "$LOG_FILE" "$LOG_FILE.1"
            touch "$LOG_FILE"
            # Remove oldest if over limit
            [ -f "$LOG_FILE.$((max_rotations + 1))" ] && rm "$LOG_FILE.$((max_rotations + 1))"
        fi
    fi
}

# Run the server in the foreground (for systemd)
run_foreground() {
    log_info "Starting LifeOS server in foreground mode..."

    # Guard: this machine must be the designated host, if one is configured.
    check_host_guard || return 1

    # Check ChromaDB is running (required dependency)
    if ! chromadb_healthy; then
        log_error "ChromaDB server not running. Start it first."
        return 1
    fi

    log_info "ChromaDB: Running"
    log_info "Launching uvicorn on $HOST:$PORT (foreground)..."

    # Exec replaces this process — systemd manages the lifecycle.
    # timeout_graceful_shutdown caps how long uvicorn waits for in-flight
    # connections to drain on SIGTERM before force-closing them. Without it,
    # long-lived connections (the /agents SSE streams, the Telegram
    # getUpdates long-poll) keep the old process alive until systemd's
    # 90s TimeoutStopSec fires a SIGKILL — a ~90s window where the dying
    # process still holds :8000 and every request is connection-refused.
    exec "$HOME/.venvs/lifeos/bin/python" -c "
import uvicorn
uvicorn.run('api.main:app', host='$HOST', port=$PORT, log_level='info', timeout_graceful_shutdown=10)
"
}

# Start the server
start_server() {
    log_info "Starting LifeOS server..."

    # Guard: this machine must be the designated host, if one is configured.
    check_host_guard || return 1

    # Rotate logs before starting
    rotate_log

    # Check ChromaDB is running (required dependency)
    if ! chromadb_healthy; then
        log_warn "ChromaDB server not running. Starting it..."
        "$SCRIPT_DIR/chromadb.sh" start
        if ! chromadb_healthy; then
            log_error "Failed to start ChromaDB. Cannot start LifeOS."
            return 1
        fi
    else
        log_info "ChromaDB: Running"
    fi

    # First, ensure no existing processes
    kill_server

    # Start the server using Python's uvicorn.run() - more reliable than shell command
    log_info "Launching uvicorn on $HOST:$PORT..."
    nohup "$HOME/.venvs/lifeos/bin/python" -c "
import uvicorn
uvicorn.run('api.main:app', host='$HOST', port=$PORT, log_level='info', timeout_graceful_shutdown=10)
" >> "$LOG_FILE" 2>&1 &

    local pid=$!
    log_info "Server process started with PID: $pid"

    # Wait for it to become healthy
    if wait_for_healthy; then
        show_status
        return 0
    else
        log_error "Server failed to start. Check logs: $LOG_FILE"
        tail -20 "$LOG_FILE" 2>/dev/null || true
        return 1
    fi
}

# Stop the server
stop_server() {
    kill_server
}

# Show server status
show_status() {
    echo ""
    log_info "=== Server Status ==="

    local pid=$(get_server_pid)
    if [ -n "$pid" ]; then
        log_info "Process: Running (PID: $pid)"

        # Check binding
        local binding=$(lsof -i :$PORT 2>/dev/null | grep LISTEN | awk '{print $9}' | head -1)
        if [ -n "$binding" ]; then
            log_info "Binding: $binding"
        fi
    else
        log_warn "Process: Not running"
    fi

    # Health check
    if is_healthy; then
        log_info "Health: Healthy"
        curl -s "$HEALTH_URL" | python3 -m json.tool 2>/dev/null || curl -s "$HEALTH_URL"
    else
        log_warn "Health: Not responding"
    fi

    echo ""
    echo "URLs:"
    echo "  Local:     http://127.0.0.1:$PORT"
    echo "  Network:   http://$HOST:$PORT"

    # Tailscale URL if available
    if command -v tailscale &> /dev/null; then
        local ts_ip
        ts_ip=$(tailscale ip -4 2>/dev/null || true)
        if [ -n "$ts_ip" ]; then
            echo "  Tailscale: http://$ts_ip:$PORT"
        fi
        if [ -n "${TAILNET_HTTPS_URL:-}" ]; then
            echo "  Voice/chat: ${TAILNET_HTTPS_URL}/chat  (HTTPS — required for mic)"
        fi
    fi
    echo ""
}

# Check if systemd is managing the service (avoid ghost processes)
is_systemd_managed() {
    systemctl is-active lifeos-api.service &>/dev/null ||
    systemctl is-enabled lifeos-api.service &>/dev/null
}

# Run systemctl command with sudo (passwordless via sudoers rule from setup-systemd.sh)
sctl() {
    sudo systemctl "$@"
}

# classify-change <git-range> — print which restart a diff needs.
#
# The doctor ships changes through PRs; at end-of-goal it must restart so the
# change takes effect. API-only changes are cheap (`lifeos-api` restart leaves
# the worker — and the doctor's own session — alive). Changes under
# api/services/agent_worker/ require bouncing the worker itself, which kills the
# doctor mid-run, so they go through restart-worker-detached instead.
#
# Prints "worker" if any changed file is under WORKER_CODE_PATH, else "api".
# Default range is the last commit (HEAD~1..HEAD); pass an explicit range
# (e.g. "main..HEAD") to classify a whole branch.
#
# This is a path-prefix HEURISTIC, not a guarantee: a change that alters worker
# behavior from outside api/services/agent_worker/ (e.g. config/settings.py,
# which the worker imports) classifies as "api" and would leave the worker on
# stale code. Acceptable because the doctor ships localized PRs; widen
# WORKER_CODE_PATH or add paths if that assumption stops holding.
classify_change() {
    local range="${1:-HEAD~1..HEAD}"
    local changed
    changed=$(git -C "$PROJECT_DIR" diff --name-only "$range" 2>/dev/null)
    if echo "$changed" | grep -q "^${WORKER_CODE_PATH}"; then
        echo "worker"
    else
        echo "api"
    fi
}

# verify-deployed [<expected-sha>] — confirm the canonical checkout actually
# advanced to the merged code and is a real work tree (#419).
#
# The doctor's deploy step pulls main then restarts; a silent pull failure — most
# notably a bare/misconfigured checkout (core.bare=true makes `git pull`/`checkout`
# error out as a no-op) — otherwise lets it report "Shipped" while the OLD code is
# still running. This is the guard: prints "deployed: <head>" and exits 0 only when
# PROJECT_DIR is a real work tree AND its HEAD matches <expected-sha> (or origin/main
# when no arg is given). On any mismatch it prints "not-deployed: <reason>" to stderr
# and exits 1, so the doctor reports a deploy failure instead of a false success.
verify_deployed() {
    local expected="${1:-}"
    # Reject an ambiguously-short explicit sha: a 1-2 char prefix could match an
    # unrelated HEAD and yield a false "deployed". Require full or git-abbrev
    # (>=7 chars); omit the arg to compare against origin/main instead.
    if [ -n "$expected" ] && [ "${#expected}" -lt 7 ]; then
        echo "verify-deployed: expected sha '$expected' is too short to be unambiguous (need >=7 chars, or omit to use origin/main)" >&2
        return 2
    fi
    # A bare repo (core.bare=true) or non-work-tree can't pull/checkout, so a
    # "successful" deploy there never changed the code on disk.
    if [ "$(git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
        echo "not-deployed: $PROJECT_DIR is not a work tree (core.bare?) — pull/checkout cannot apply" >&2
        return 1
    fi
    local head
    if ! head=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null); then
        echo "not-deployed: cannot read HEAD of $PROJECT_DIR" >&2
        return 1
    fi
    if [ -z "$expected" ]; then
        if ! expected=$(git -C "$PROJECT_DIR" rev-parse origin/main 2>/dev/null); then
            echo "not-deployed: cannot resolve origin/main (fetch first)" >&2
            return 1
        fi
    fi
    # Accept an abbreviated <expected-sha> (prefix match), the way git does.
    case "$head" in
        "$expected"*)
            echo "deployed: HEAD=$head"
            return 0 ;;
        *)
            echo "not-deployed: HEAD=$head != expected=$expected — deploy did NOT take effect" >&2
            return 1 ;;
    esac
}

# verify-runtime-evidence --restart-evidence <path>
#   [--services lifeos-api[,lifeos-agent-worker]] [--worker-evidence <path>]
##
# Build a machine-attributed deployment result from startup identity records,
# the scoped /health response, and a restart-wrapper result.  This is a
# stronger, additive producer for doctor consumers; verify-deployed above is
# intentionally unchanged for legacy callers.  The Python producer rejects
# checkout-only or user-supplied success claims and exits non-zero unless every
# requested service proves the expected immutable revision is running.
verify_runtime_evidence() {
    local identity_dir="${LIFEOS_RUNTIME_IDENTITY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/runtime-identities}"
    local expected_revision
    if ! expected_revision=$(git -C "$PROJECT_DIR" rev-parse --verify HEAD 2>/dev/null); then
        log_error "verify-runtime-evidence: cannot resolve checkout HEAD"
        return 1
    fi
    # source-root and expected-revision are wrapper-owned bindings.  Reject
    # attempts to append duplicate flags: argparse would otherwise let the
    # last user-supplied value silently override the trusted values below.
    local arg
    for arg in "$@"; do
        case "$arg" in
            --source-root|--source-root=*|--expected-revision|--expected-revision=*)
                log_error "verify-runtime-evidence: source-root and expected-revision are wrapper-bound"
                return 2
                ;;
        esac
    done
    exec "$VENV_PYTHON" -m api.services.runtime_identity verify \
        --source-root "$PROJECT_DIR" \
        --expected-revision "$expected_revision" \
        --identity-dir "$identity_dir" \
        --api-url "$HEALTH_URL" \
        "$@"
}

# Record the restart outcome produced by this wrapper.  The evidence verifier
# treats this file as an input only when its provenance and result are exact;
# an operator/model cannot turn a claimed success boolean into deployment proof.
latest_runtime_identity() {
    local identity_dir="$1"
    local candidate=""
    local latest=""
    for candidate in "$identity_dir"/lifeos-api-*.json; do
        [ -f "$candidate" ] || continue
        if [ -z "$latest" ] || [ "$candidate" -nt "$latest" ]; then
            latest="$candidate"
        fi
    done
    printf '%s' "$latest"
}

record_runtime_restart() {
    local service="${1:-lifeos-api}"
    local result="${2:-failure}"
    local previous_identity="${3:-}"
    local health_status="${4:-}"
    local identity_dir="${LIFEOS_RUNTIME_IDENTITY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/runtime-identities}"
    local output="$identity_dir/restart-$service.json"
    local identity=""
    if [ "$service" = "lifeos-api" ]; then
        identity="$(latest_runtime_identity "$identity_dir")"
    else
        for candidate in "$identity_dir"/"$service"-*.json; do
            [ -f "$candidate" ] || continue
            identity="$candidate"
            break
        done
    fi
    local args=(record-restart --service "$service" --result "$result" --output "$output")
    if [ -n "$identity" ]; then
        args+=(--identity "$identity")
    fi
    if [ -n "$previous_identity" ]; then
        args+=(--previous-identity "$previous_identity")
    fi
    if [ -n "$health_status" ]; then
        args+=(--health-status "$health_status")
    fi
    "$VENV_PYTHON" -m api.services.runtime_identity "${args[@]}" \
        || log_warn "could not record runtime restart outcome"
}

# restart-worker-detached [--session ID] [--notify TEXT] [--bot NAME]
#
# Restart lifeos-agent-worker such that any pending final notice is delivered
# BEFORE the worker gets SIGTERM (#401). The doctor runs a headless session
# *inside* the worker, so an inline `systemctl restart` would kill the restart
# command along with the session. Steps:
#   1. Send the final notice first (if --notify given) so the operator gets the
#      "Shipped" message even if the streamed [NOTIFY] raced the SIGTERM.
#   2. Write the self-restart marker (names --session) so resume_pending()
#      finalizes that session quietly instead of firing the rollback notice.
#   3. Start an independent watcher, then restart the worker in a DETACHED
#      process (systemd-run on Linux, launchd on macOS, nohup+setsid fallback)
#      that outlives the dying session and actually performs the bounce.
restart_worker_detached() {
    local session_id="" notify_text="" bot=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --session) session_id="$2"; shift 2 ;;
            --notify)  notify_text="$2"; shift 2 ;;
            --bot)     bot="$2"; shift 2 ;;
            *) log_warn "restart-worker-detached: ignoring unknown arg '$1'"; shift ;;
        esac
    done

    # 1. Flush the final notice BEFORE anything can kill the session.
    if [ -n "$notify_text" ]; then
        log_info "Sending final notice before worker restart..."
        "$VENV_PYTHON" - "$notify_text" "$bot" <<'PYEOF' || log_warn "final notice send failed (continuing to restart)"
import sys
text = sys.argv[1]
bot = sys.argv[2] or None
from api.services.telegram import send_message
send_message(text, bot=bot)
PYEOF
    fi

    # 2. Mark the deliberate self-restart so resume_pending() stays quiet.
    if [ -n "$session_id" ]; then
        log_info "Marking session $session_id as a deliberate self-restart..."
        "$VENV_PYTHON" -m api.services.agent_worker.worker \
            --mark-self-restart --session "$session_id" \
            || log_warn "could not write self-restart marker (continuing to restart)"
    fi

# 3. Start the watcher before requesting the restart.  The caller is normally
#    the worker itself, so anything started after the restart request can be
#    killed before it records the new process identity.
    # The watcher waits for a new startup identity and active unit before it
    # writes restart-lifeos-agent-worker.json.  The command therefore produces
    # evidence of an observed worker, rather than evidence of a restart request.
    local identity_dir="${LIFEOS_RUNTIME_IDENTITY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/runtime-identities}"
    local previous_identity=""
    for candidate in "$identity_dir"/lifeos-agent-worker-*.json; do
        [ -f "$candidate" ] || continue
        if [ -z "$previous_identity" ] || [ "$candidate" -nt "$previous_identity" ]; then
            previous_identity="$candidate"
        fi
    done
    local worker_output="$identity_dir/restart-lifeos-agent-worker.json"
    log_info "Triggering detached restart of $WORKER_UNIT..."
    local watcher_args=("$VENV_PYTHON" -m api.services.runtime_identity wait-and-record
        --service lifeos-agent-worker --unit "$WORKER_UNIT"
        --identity-dir "$identity_dir" --output "$worker_output")
    if [ -n "$previous_identity" ]; then
        watcher_args+=(--previous-identity "$previous_identity")
    fi
    if [ "$(uname)" = "Darwin" ] && command -v launchctl &>/dev/null; then
        # launchctl submit gives both the watcher and the restart their own
        # launchd jobs, independent of the worker job being unloaded.  Keep
        # the clean unload-then-load sequence; kickstart is known to wedge
        # this service on macOS (see docs/guides/operations.md).
        local launchd_label="com.lifeos.agent-worker"
        local watcher_label="com.lifeos.agent-worker-restart-watch-$$"
        local restart_label="com.lifeos.agent-worker-restart-$$"
        watcher_args+=(--manager launchd --launchd-label "$launchd_label")
        if ! launchctl submit -l "$watcher_label" -- "${watcher_args[@]}" \
            >/dev/null 2>>"$PROJECT_DIR/logs/worker-restart.log"; then
            log_error "launchd watcher submission failed"
            record_runtime_restart lifeos-agent-worker failure "$previous_identity" failed
            return 1
        fi
        local worker_plist="${LIFEOS_AGENT_WORKER_PLIST:-$HOME/Library/LaunchAgents/com.lifeos.agent-worker.plist}"
        local restart_script="launchctl unload $(printf '%q' "$worker_plist") >/dev/null 2>>$(printf '%q' "$PROJECT_DIR/logs/worker-restart.log") || true; launchctl load $(printf '%q' "$worker_plist") >>$(printf '%q' "$PROJECT_DIR/logs/worker-restart.log") 2>&1"
        if ! launchctl submit -l "$restart_label" -- /bin/bash -c "$restart_script" \
            >/dev/null 2>>"$PROJECT_DIR/logs/worker-restart.log"; then
            log_error "launchd worker restart submission failed"
            record_runtime_restart lifeos-agent-worker failure "$previous_identity" failed
            return 1
        fi
    elif command -v systemd-run &>/dev/null; then
        # Transient unit in its own cgroup — fully decoupled from the caller, so
        # the SIGTERM the worker is about to receive can't kill the restarter.
        local watcher_unit="lifeos-worker-restart-watch-$$"
        if ! sudo systemd-run --unit="$watcher_unit" --collect --no-block \
            "${watcher_args[@]}"; then
            log_error "systemd-run watcher submission failed"
            record_runtime_restart lifeos-agent-worker failure "$previous_identity" failed
            return 1
        fi
        if ! sudo systemd-run --unit="lifeos-worker-restart-$$" --collect --no-block \
            systemctl restart "$WORKER_UNIT"; then
            log_error "systemd-run restart failed"
            record_runtime_restart lifeos-agent-worker failure "$previous_identity" failed
            return 1
        fi
    else
        # Fallback for non-systemd/non-launchd hosts (no service manager).
        # Start the watcher first; setsid+nohup only protects against a shell
        # SIGHUP, not a cgroup-level kill, so this remains best effort.
        setsid nohup "${watcher_args[@]}" \
            >> "$PROJECT_DIR/logs/worker-restart.log" 2>&1 &
        disown 2>/dev/null || true
        setsid nohup sudo systemctl restart "$WORKER_UNIT" \
            >> "$PROJECT_DIR/logs/worker-restart.log" 2>&1 &
        disown 2>/dev/null || true
    fi
    log_info "Detached worker restart triggered; evidence will be written to $worker_output after the new worker starts."
}

# Main
case "${1:-status}" in
    start)
        if is_systemd_managed; then
            log_info "Delegating to systemd..."
            kill_server  # Clear any ghost processes holding the port
            sctl start lifeos-api
            wait_for_healthy
        else
            start_server
        fi
        ;;
    stop)
        if is_systemd_managed; then
            log_info "Delegating to systemd..."
            # No unit cascade anymore (see lifeos-agent-worker.service) — stop
            # the worker explicitly so "stop" still means everything down.
            systemctl is-active --quiet lifeos-agent-worker && sctl stop lifeos-agent-worker
            sctl stop lifeos-api
        else
            stop_server
        fi
        ;;
    restart)
        log_info "Restarting server..."
        if is_systemd_managed; then
            log_info "Delegating to systemd..."
            previous_identity="$(latest_runtime_identity "${LIFEOS_RUNTIME_IDENTITY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/runtime-identities}")"
            if sctl restart lifeos-api && wait_for_healthy; then
                record_runtime_restart lifeos-api success "$previous_identity" healthy
            else
                record_runtime_restart lifeos-api failure "$previous_identity" failed
                exit 1
            fi
        else
            previous_identity="$(latest_runtime_identity "${LIFEOS_RUNTIME_IDENTITY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/runtime-identities}")"
            if start_server; then
                record_runtime_restart lifeos-api success "$previous_identity" healthy
            else
                record_runtime_restart lifeos-api failure "$previous_identity" failed
                exit 1
            fi
        fi
        ;;
    foreground)
        run_foreground
        ;;
    status)
        show_status
        ;;
    wait)
        wait_for_healthy ${2:-$STARTUP_TIMEOUT}
        ;;
    preflight)
        exec "$SCRIPT_DIR/preflight.sh"
        ;;
    classify-change)
        classify_change "${2:-}"
        ;;
    verify-deployed)
        verify_deployed "${2:-}"
        ;;
    verify-runtime-evidence)
        shift
        verify_runtime_evidence "$@"
        ;;
    restart-worker-detached)
        shift
        restart_worker_detached "$@"
        ;;
    test-instance)
        # Isolated candidate instance for tests. Delegates entirely to
        # scripts/test_instance.py: an owned source snapshot, its own port,
        # a sanitized environment, and manifest-verified process identity.
        # Deliberately does NOT go through kill_server/start_server/systemd —
        # this path must never touch the shared lifeos-api service or any
        # process it didn't itself spawn.
        shift
        exec "$VENV_PYTHON" "$SCRIPT_DIR/test_instance.py" "$@"
        ;;
    *)
        echo "LifeOS Server Management"
        echo ""
        echo "Usage: $0 {start|stop|restart|status|wait [timeout]|foreground|preflight|classify-change [range]|verify-deployed [sha]|verify-runtime-evidence [opts]|restart-worker-detached [opts]|test-instance <run|verify|stop> [opts]}"
        echo ""
        echo "Commands:"
        echo "  start                    - Start server (kills existing, waits for healthy)"
        echo "  stop                     - Stop the server"
        echo "  restart                  - Restart the server"
        echo "  foreground               - Run in foreground (for systemd)"
        echo "  status                   - Show server status"
        echo "  wait                     - Wait for server to become healthy"
        echo "  preflight                - Check prerequisites before first start"
        echo "  classify-change [range]  - Print 'worker' or 'api': which restart a diff needs"
        echo "                             (default range HEAD~1..HEAD; e.g. 'main..HEAD')"
        echo "  verify-deployed [sha]    - Exit 0 only if the checkout is a real work tree on"
        echo "                             <sha> (default origin/main); else exit 1 (#419)"
        echo "  verify-runtime-evidence - Emit startup-bound structured deployment evidence"
        echo "  restart-worker-detached  - Detached restart of $WORKER_UNIT for the doctor"
        echo "                             [--session ID] [--notify TEXT] [--bot NAME]"
        echo "  test-instance ACTION     - Isolated candidate instance for tests:"
        echo "                             run|verify|stop (see test_instance.py --help); run"
        echo "                             is the supported way to exercise a candidate"
        echo ""
        echo "Expected startup time: 30-60 seconds (ML model loading)"
        exit 1
        ;;
esac
