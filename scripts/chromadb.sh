#!/bin/bash
# ChromaDB Server Management Script
#
# Usage: ./scripts/chromadb.sh [start|stop|restart|status]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Configuration
HOST="localhost"
PORT="8001"
DATA_DIR="$PROJECT_DIR/data/chromadb"
LOG_FILE="$PROJECT_DIR/logs/chromadb.log"
PID_FILE="$PROJECT_DIR/logs/chromadb.pid"
HEALTH_URL="http://$HOST:$PORT/api/v2/heartbeat"
STARTUP_TIMEOUT=30
CHROMA="$HOME/.venvs/lifeos/bin/chroma"

# Ensure directories exist
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$DATA_DIR"

# Colors
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    GREEN='' RED='' YELLOW='' NC=''
fi

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    # Fallback: find by port (only LISTEN state)
    lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null | head -1
}

is_healthy() {
    curl -s --max-time 2 "$HEALTH_URL" > /dev/null 2>&1
}

wait_for_healthy() {
    local elapsed=0
    while [ $elapsed -lt $STARTUP_TIMEOUT ]; do
        if is_healthy; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
        echo -ne "\r[WAIT] Elapsed: ${elapsed}s / ${STARTUP_TIMEOUT}s"
    done
    echo ""
    return 1
}

start_server() {
    log_info "Starting ChromaDB server..."

    local pid=$(get_pid)
    if [ -n "$pid" ]; then
        log_warn "ChromaDB already running (PID: $pid)"
        return 0
    fi

    # Start ChromaDB server
    nohup "$CHROMA" run \
        --host "$HOST" \
        --port "$PORT" \
        --path "$DATA_DIR" \
        >> "$LOG_FILE" 2>&1 &

    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"
    log_info "ChromaDB started with PID: $new_pid"

    log_info "Waiting for ChromaDB to become healthy..."
    if wait_for_healthy; then
        log_info "ChromaDB is healthy"
        return 0
    else
        log_error "ChromaDB failed to start. Check logs: $LOG_FILE"
        tail -20 "$LOG_FILE" 2>/dev/null || true
        return 1
    fi
}

stop_server() {
    log_info "Stopping ChromaDB server..."

    local pid=$(get_pid)
    if [ -z "$pid" ]; then
        log_warn "ChromaDB not running"
        rm -f "$PID_FILE"
        return 0
    fi

    kill "$pid" 2>/dev/null || true
    sleep 2

    if ps -p "$pid" > /dev/null 2>&1; then
        log_warn "Force killing ChromaDB..."
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    log_info "ChromaDB stopped"
}

show_status() {
    echo ""
    log_info "=== ChromaDB Status ==="

    local pid=$(get_pid)
    if [ -n "$pid" ]; then
        log_info "Process: Running (PID: $pid)"
    else
        log_warn "Process: Not running"
    fi

    if is_healthy; then
        log_info "Health: Healthy"
        echo "  URL: http://$HOST:$PORT"
        echo "  Data: $DATA_DIR"
    else
        log_warn "Health: Not responding"
    fi
    echo ""
}

case "${1:-status}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        start_server
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
