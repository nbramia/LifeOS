#!/bin/bash
# LifeOS Service Management Script (launchd)
# ===========================================
#
# Usage: ./scripts/service.sh [install|uninstall|start|stop|restart|status|logs]
#
# This script manages LifeOS as a macOS launchd service for auto-start on boot.
# For day-to-day server management (without launchd), use server.sh instead.
#
# Commands:
#   install    - Install and start the service (auto-start on boot)
#   uninstall  - Stop and remove the service
#   start      - Start the service
#   stop       - Stop the service
#   restart    - Restart the service
#   status     - Check service status and health
#   logs       - Tail the service logs
#
# Note: Server startup takes 30-60 seconds for ML model loading.
#
# Related Scripts:
#   ./scripts/server.sh   - Day-to-day server management (recommended for Claude)
#   ./scripts/deploy.sh   - Full deployment (test, restart, commit, push)
#   ./scripts/test.sh     - Test runner (unit/integration/browser)
#
# See README.md for full documentation.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.lifeos.api"
PLIST_SRC="$PROJECT_DIR/config/launchd/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/lifeos-api.log"
ERROR_LOG="$LOG_DIR/lifeos-api-error.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

ensure_logs_dir() {
    if [ ! -d "$LOG_DIR" ]; then
        mkdir -p "$LOG_DIR"
        log_info "Created logs directory: $LOG_DIR"
    fi
}

setup_log_rotation() {
    # Create newsyslog config for log rotation (100MB max, 5 archives)
    NEWSYSLOG_CONF="/etc/newsyslog.d/lifeos.conf"

    if [ ! -f "$NEWSYSLOG_CONF" ]; then
        log_info "Setting up log rotation (requires sudo)..."
        CURRENT_USER=$(whoami)
        echo "# LifeOS log rotation
$LOG_FILE  $CURRENT_USER:staff  644  5  102400  *  J
$ERROR_LOG $CURRENT_USER:staff  644  5  102400  *  J" | sudo tee "$NEWSYSLOG_CONF" > /dev/null
        log_info "Log rotation configured: max 100MB, 5 archives"
    fi
}

install() {
    log_info "Installing LifeOS service..."

    # Ensure logs directory exists
    ensure_logs_dir

    # Create LaunchAgents directory if needed
    mkdir -p "$HOME/Library/LaunchAgents"

    # Copy plist file
    if [ -f "$PLIST_SRC" ]; then
        cp "$PLIST_SRC" "$PLIST_DST"
        log_info "Installed plist to $PLIST_DST"
    else
        log_error "Plist file not found: $PLIST_SRC"
        exit 1
    fi

    # Load the service
    launchctl load "$PLIST_DST"
    log_info "Service loaded and started"

    # Setup log rotation (optional, requires sudo)
    read -p "Setup log rotation (requires sudo)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        setup_log_rotation
    fi

    log_info "Installation complete!"
    status
}

uninstall() {
    log_info "Uninstalling LifeOS service..."

    if [ -f "$PLIST_DST" ]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm "$PLIST_DST"
        log_info "Service uninstalled"
    else
        log_warn "Service not installed"
    fi
}

start() {
    log_info "Starting LifeOS service..."
    ensure_logs_dir

    if [ -f "$PLIST_DST" ]; then
        launchctl start "$PLIST_NAME"
        log_info "Service started"
        sleep 2
        status
    else
        log_error "Service not installed. Run './scripts/service.sh install' first."
        exit 1
    fi
}

stop() {
    log_info "Stopping LifeOS service..."

    if [ -f "$PLIST_DST" ]; then
        launchctl stop "$PLIST_NAME"
        log_info "Service stopped"
    else
        log_warn "Service not installed"
    fi
}

restart() {
    log_info "Restarting LifeOS service..."
    stop
    sleep 2
    start
}

status() {
    log_info "Checking LifeOS service status..."
    echo ""

    # Check if plist is installed
    if [ ! -f "$PLIST_DST" ]; then
        log_warn "Service not installed"
        return
    fi

    # Check launchctl status
    if launchctl list | grep -q "$PLIST_NAME"; then
        PID=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
        if [ "$PID" != "-" ] && [ -n "$PID" ]; then
            log_info "Service is RUNNING (PID: $PID)"
        else
            log_warn "Service is LOADED but NOT RUNNING"
        fi
    else
        log_warn "Service is NOT LOADED"
    fi

    # Check health endpoint
    echo ""
    log_info "Checking health endpoint..."
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health | grep -q "200"; then
        HEALTH=$(curl -s http://localhost:8000/health)
        log_info "Health check: $HEALTH"
    else
        log_warn "Health check failed (service may be starting...)"
    fi
}

logs() {
    log_info "Showing LifeOS logs (Ctrl+C to exit)..."
    echo ""

    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE" "$ERROR_LOG"
    else
        log_warn "No log files found. Service may not have run yet."
    fi
}

# Main
case "${1:-}" in
    install)
        install
        ;;
    uninstall)
        uninstall
        ;;
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "LifeOS Service Manager"
        echo ""
        echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs}"
        echo ""
        echo "Commands:"
        echo "  install    Install and start the service (auto-start on boot)"
        echo "  uninstall  Stop and remove the service"
        echo "  start      Start the service"
        echo "  stop       Stop the service"
        echo "  restart    Restart the service"
        echo "  status     Check service status and health"
        echo "  logs       Tail the service logs"
        exit 1
        ;;
esac
