#!/bin/bash
# LifeOS Service Management Script (launchd / systemd)
# =====================================================
#
# Usage: ./scripts/service.sh [install|uninstall|start|stop|restart|status|logs]
#
# On macOS: manages LifeOS as a launchd service (auto-start on boot)
# On Linux: manages LifeOS as systemd services
# For day-to-day server management (without service managers), use server.sh instead.
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
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/lifeos-api.log"
ERROR_LOG="$LOG_DIR/lifeos-api-error.log"

# OS detection
OS="$(uname)"

# macOS-specific config
PLIST_NAME="com.lifeos.api"
PLIST_SRC="$PROJECT_DIR/config/launchd/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

# Linux-specific config
SYSTEMD_SERVICES="lifeos-chromadb lifeos-api"
SYSTEMD_TIMERS="lifeos-watchdog lifeos-sync"

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

# ===== macOS (launchd) =====

setup_log_rotation_macos() {
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

install_macos() {
    log_info "Installing LifeOS service (launchd)..."

    ensure_logs_dir
    mkdir -p "$HOME/Library/LaunchAgents"

    if [ -f "$PLIST_SRC" ]; then
        cp "$PLIST_SRC" "$PLIST_DST"
        log_info "Installed plist to $PLIST_DST"
    else
        log_error "Plist file not found: $PLIST_SRC"
        exit 1
    fi

    launchctl load "$PLIST_DST"
    log_info "Service loaded and started"

    read -p "Setup log rotation (requires sudo)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        setup_log_rotation_macos
    fi

    log_info "Installation complete!"
    status_macos
}

uninstall_macos() {
    log_info "Uninstalling LifeOS service..."

    if [ -f "$PLIST_DST" ]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm "$PLIST_DST"
        log_info "Service uninstalled"
    else
        log_warn "Service not installed"
    fi
}

start_macos() {
    log_info "Starting LifeOS service..."
    ensure_logs_dir

    if [ -f "$PLIST_DST" ]; then
        launchctl start "$PLIST_NAME"
        log_info "Service started"
        sleep 2
        status_macos
    else
        log_error "Service not installed. Run './scripts/service.sh install' first."
        exit 1
    fi
}

stop_macos() {
    log_info "Stopping LifeOS service..."

    if [ -f "$PLIST_DST" ]; then
        launchctl stop "$PLIST_NAME"
        log_info "Service stopped"
    else
        log_warn "Service not installed"
    fi
}

status_macos() {
    log_info "Checking LifeOS service status..."
    echo ""

    if [ ! -f "$PLIST_DST" ]; then
        log_warn "Service not installed"
        return
    fi

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

    check_health
}

# ===== Linux (systemd) =====

install_linux() {
    log_info "Installing LifeOS services (systemd)..."
    log_info "Run: sudo ./scripts/setup-systemd.sh"
    log_info "(setup-systemd.sh copies unit files, enables services, and starts them)"
}

uninstall_linux() {
    log_info "Uninstalling LifeOS services..."

    for timer in $SYSTEMD_TIMERS; do
        systemctl --user disable --now "${timer}.timer" 2>/dev/null || \
            sudo systemctl disable --now "${timer}.timer" 2>/dev/null || true
    done

    for svc in $SYSTEMD_SERVICES; do
        systemctl --user disable --now "${svc}.service" 2>/dev/null || \
            sudo systemctl disable --now "${svc}.service" 2>/dev/null || true
    done

    log_info "Services disabled. Unit files remain in /etc/systemd/system/."
}

start_linux() {
    log_info "Starting LifeOS services..."

    for svc in $SYSTEMD_SERVICES; do
        sudo systemctl start "${svc}.service" 2>/dev/null || systemctl --user start "${svc}.service"
    done

    for timer in $SYSTEMD_TIMERS; do
        sudo systemctl start "${timer}.timer" 2>/dev/null || systemctl --user start "${timer}.timer"
    done

    log_info "Services started"
    sleep 2
    status_linux
}

stop_linux() {
    log_info "Stopping LifeOS services..."

    for svc in $SYSTEMD_SERVICES; do
        sudo systemctl stop "${svc}.service" 2>/dev/null || systemctl --user stop "${svc}.service" 2>/dev/null || true
    done

    log_info "Services stopped"
}

status_linux() {
    log_info "Checking LifeOS service status..."
    echo ""

    for svc in $SYSTEMD_SERVICES; do
        local state
        state=$(systemctl is-active "${svc}.service" 2>/dev/null || echo "unknown")
        if [ "$state" = "active" ]; then
            log_info "${svc}: RUNNING"
        else
            log_warn "${svc}: $state"
        fi
    done

    echo ""
    for timer in $SYSTEMD_TIMERS; do
        local state
        state=$(systemctl is-active "${timer}.timer" 2>/dev/null || echo "unknown")
        if [ "$state" = "active" ]; then
            log_info "${timer}.timer: ACTIVE"
        else
            log_warn "${timer}.timer: $state"
        fi
    done

    check_health
}

# ===== Shared =====

check_health() {
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

    if [[ "$OS" == "Linux" ]]; then
        # Try journalctl first, fall back to log files
        if journalctl -u lifeos-api -f 2>/dev/null; then
            return
        fi
    fi

    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE" "$ERROR_LOG"
    else
        log_warn "No log files found. Service may not have run yet."
    fi
}

# ===== Dispatch =====

dispatch() {
    local action="$1"

    if [[ "$OS" == "Darwin" ]]; then
        case "$action" in
            install)   install_macos ;;
            uninstall) uninstall_macos ;;
            start)     start_macos ;;
            stop)      stop_macos ;;
            restart)   stop_macos; sleep 2; start_macos ;;
            status)    status_macos ;;
            logs)      logs ;;
        esac
    elif [[ "$OS" == "Linux" ]]; then
        case "$action" in
            install)   install_linux ;;
            uninstall) uninstall_linux ;;
            start)     start_linux ;;
            stop)      stop_linux ;;
            restart)   stop_linux; sleep 2; start_linux ;;
            status)    status_linux ;;
            logs)      logs ;;
        esac
    else
        log_error "Unsupported OS: $OS"
        exit 1
    fi
}

# Main
case "${1:-}" in
    install|uninstall|start|stop|restart|status|logs)
        dispatch "$1"
        ;;
    *)
        echo "LifeOS Service Manager"
        echo ""
        if [[ "$OS" == "Darwin" ]]; then
            echo "Platform: macOS (launchd)"
        else
            echo "Platform: Linux (systemd)"
        fi
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
