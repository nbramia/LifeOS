#!/bin/bash
# LifeOS Test Runner
# ==================
#
# Usage: ./scripts/test.sh [unit|integration|browser|smoke|all|health]
#
# Test levels:
#   unit        - Fast tests, no external dependencies (~30s)
#   integration - Tests requiring server to be running
#   browser     - Playwright browser tests (requires server)
#   smoke       - Unit + critical browser test (used by deploy.sh)
#   all         - Run all tests in sequence
#   health      - Quick server health check
#
# Note: Integration, browser, and smoke tests require the server to be running.
# If not running, this script will start it automatically (takes 30-60s for ML model loading).
#
# Related Scripts:
#   ./scripts/deploy.sh   - Full deployment (test, restart, commit, push)
#   ./scripts/server.sh   - Server management (start/stop/restart/status)
#   ./scripts/service.sh  - launchd service management (auto-start on boot)
#
# See README.md for full documentation.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Activate virtual environment (located outside Documents for faster startup)
activate_venv() {
    if [ -f "$HOME/.venvs/lifeos/bin/activate" ]; then
        source "$HOME/.venvs/lifeos/bin/activate"
    else
        log_error "Virtual environment not found at ~/.venvs/lifeos"
        log_error "Run: python -m venv ~/.venvs/lifeos && ~/.venvs/lifeos/bin/pip install -r requirements.txt"
        exit 1
    fi
}

# Check if server is running
check_server() {
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null | grep -q "200"; then
        return 0
    else
        return 1
    fi
}

# Run unit tests (fast, no external deps)
run_unit_tests() {
    log_step "Running unit tests..."
    python -m pytest tests/ -v \
        --ignore=tests/test_ui_browser.py \
        --ignore=tests/archive \
        -m "not browser and not requires_server and not integration and not slow" \
        --tb=short \
        -q
}

# Run integration tests (requires server)
run_integration_tests() {
    log_step "Running integration tests..."

    if ! check_server; then
        log_warn "Server not running. Starting server for integration tests..."
        start_server_background
        sleep 3
    fi

    python -m pytest tests/test_e2e_flow.py -v \
        -m "integration" \
        --tb=short
}

# Run browser tests (requires server + playwright)
run_browser_tests() {
    log_step "Running browser tests..."

    if ! check_server; then
        log_warn "Server not running. Starting server for browser tests..."
        start_server_background
        sleep 3
    fi

    # Check if playwright is installed
    if ! python -c "import playwright" 2>/dev/null; then
        log_error "Playwright not installed. Run: pip install playwright && playwright install"
        exit 1
    fi

    python -m pytest tests/test_ui_browser.py tests/test_e2e_flow.py -v \
        --ignore=tests/archive \
        -m "browser" \
        --tb=short \
        --browser chromium
}

# Run smoke tests (unit + critical browser test for deployment verification)
run_smoke_tests() {
    local start_time=$(date +%s)

    log_step "Running smoke tests (unit + critical browser test)..."
    echo ""

    # Unit tests first (fast feedback)
    run_unit_tests
    echo ""

    # Critical browser test - verifies the full user flow works
    log_step "Running critical browser smoke test..."

    if ! check_server; then
        log_warn "Server not running. Starting server for smoke test..."
        start_server_background
        sleep 3
    fi

    # Check if playwright is installed
    if ! python -c "import playwright" 2>/dev/null; then
        log_error "Playwright not installed. Run: pip install playwright && playwright install"
        exit 1
    fi

    # Run only the critical e2e test that verifies the full user flow
    python -m pytest tests/test_e2e_flow.py::TestRealUserFlow::test_user_sends_query_gets_response -v \
        --tb=short \
        --browser chromium

    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    log_info "Smoke tests passed in ${duration}s"
}

# Start server in background for tests using server.sh
start_server_background() {
    log_info "Starting server for tests (takes 30-60s for ML model loading)..."

    # Use server.sh for robust startup (handles cleanup, lock files, proper timeouts)
    if ! "$SCRIPT_DIR/server.sh" start; then
        log_error "Server failed to start. Check logs: $PROJECT_DIR/logs/server.log"
        exit 1
    fi
}

# Stop background test server
stop_test_server() {
    if [ -f /tmp/lifeos_test_server.pid ]; then
        PID=$(cat /tmp/lifeos_test_server.pid)
        if kill -0 $PID 2>/dev/null; then
            kill $PID
            log_info "Stopped test server (PID: $PID)"
        fi
        rm /tmp/lifeos_test_server.pid
    fi
}

# Run all tests
run_all_tests() {
    local start_time=$(date +%s)

    log_step "Running full test suite..."
    echo ""

    # Unit tests first (fast feedback)
    run_unit_tests
    echo ""

    # Integration tests
    run_integration_tests
    echo ""

    # Browser tests
    run_browser_tests
    echo ""

    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    log_info "All tests passed in ${duration}s"
}

# Health check test (quick sanity check)
run_health_check() {
    log_step "Running health check..."

    if ! check_server; then
        log_error "Server not running"
        return 1
    fi

    HEALTH=$(curl -s http://localhost:8000/health)
    echo "$HEALTH" | python -m json.tool

    # Check if healthy or degraded
    STATUS=$(echo "$HEALTH" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
    if [ "$STATUS" = "healthy" ]; then
        log_info "Health check passed"
        return 0
    else
        log_warn "Health check returned: $STATUS"
        return 1
    fi
}

# Main
activate_venv

case "${1:-unit}" in
    unit)
        run_unit_tests
        ;;
    integration)
        run_integration_tests
        ;;
    browser)
        run_browser_tests
        ;;
    smoke)
        run_smoke_tests
        ;;
    all)
        run_all_tests
        ;;
    health)
        run_health_check
        ;;
    *)
        echo "LifeOS Test Runner"
        echo ""
        echo "Usage: $0 [unit|integration|browser|smoke|all|health]"
        echo ""
        echo "Test levels:"
        echo "  unit         Fast tests, no external dependencies (default)"
        echo "  integration  Tests requiring server to be running"
        echo "  browser      Playwright browser tests"
        echo "  smoke        Unit tests + critical browser test (for deployment)"
        echo "  all          Run all tests in sequence"
        echo "  health       Quick server health check"
        exit 1
        ;;
esac
