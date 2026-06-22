#!/bin/bash
# LifeOS Server Management Script
# Designed for reliable server management from Claude or command line
#
# Usage: ./scripts/server.sh [start|stop|restart|status|wait|preflight]
#
# Commands:
#   start     - Kill any existing processes, start server, wait for health check
#   stop      - Stop the server
#   restart   - Stop and start the server
#   status    - Check if server is running and healthy
#   wait      - Wait for server to be healthy (use after manual start)
#   preflight - Check prerequisites before first start
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
            sctl stop lifeos-api
        else
            stop_server
        fi
        ;;
    restart)
        log_info "Restarting server..."
        if is_systemd_managed; then
            log_info "Delegating to systemd..."
            sctl restart lifeos-api
            wait_for_healthy
        else
            start_server
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
    *)
        echo "LifeOS Server Management"
        echo ""
        echo "Usage: $0 {start|stop|restart|status|wait [timeout]|foreground|preflight}"
        echo ""
        echo "Commands:"
        echo "  start      - Start server (kills existing, waits for healthy)"
        echo "  stop       - Stop the server"
        echo "  restart    - Restart the server"
        echo "  foreground - Run in foreground (for systemd)"
        echo "  status     - Show server status"
        echo "  wait       - Wait for server to become healthy"
        echo "  preflight  - Check prerequisites before first start"
        echo ""
        echo "Expected startup time: 30-60 seconds (ML model loading)"
        exit 1
        ;;
esac
