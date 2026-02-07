#!/bin/bash
# LifeOS Deployment Script
# ========================
#
# IMPORTANT: Always use this script after code changes to deploy new versions.
# The server does NOT auto-reload - you must restart it for changes to take effect.
#
# Usage: ./scripts/deploy.sh [--skip-tests] [--no-push] [message]
#
# This script:
# 1. Runs smoke tests (unit + critical browser test via test.sh)
# 2. Restarts the server (via server.sh) - takes 30-60 seconds for ML model loading
# 3. Verifies health check
# 4. Commits and pushes changes (if any)
#
# Options:
#   --skip-tests  Skip running tests (use with caution)
#   --no-push     Commit but don't push to remote
#   message       Custom commit message (optional)
#
# Related Scripts:
#   ./scripts/server.sh   - Server management (start/stop/restart/status)
#   ./scripts/test.sh     - Test runner (unit/integration/browser)
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
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[====]${NC} $1"; }
log_success() { echo -e "${CYAN}[DONE]${NC} $1"; }

# Parse arguments
SKIP_TESTS=false
NO_PUSH=false
COMMIT_MSG=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        --no-push)
            NO_PUSH=true
            shift
            ;;
        *)
            COMMIT_MSG="$1"
            shift
            ;;
    esac
done

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

# Run tests
run_tests() {
    log_step "Step 1/5: Running smoke tests (unit + critical browser test)"
    echo ""

    if [ "$SKIP_TESTS" = true ]; then
        log_warn "Skipping tests (--skip-tests flag)"
        return 0
    fi

    # Run smoke tests: unit tests + critical browser test for deployment verification
    if ! "$SCRIPT_DIR/test.sh" smoke; then
        log_error "Smoke tests failed! Fix tests before deploying."
        exit 1
    fi

    log_info "All smoke tests passed"
}

# Restart server using server.sh (handles cleanup, lock files, proper timeouts)
restart_server() {
    log_step "Step 2/5: Restarting server"
    echo ""

    # Use server.sh for robust restart (cleans up processes, lock files, waits for health)
    if ! "$SCRIPT_DIR/server.sh" restart; then
        log_error "Server failed to start. Check logs: $PROJECT_DIR/logs/server.log"
        exit 1
    fi
}

# Verify deployment
verify_deployment() {
    log_step "Step 3/5: Verifying deployment"
    echo ""

    # Check health endpoint
    HEALTH=$(curl -s http://localhost:8000/health)
    STATUS=$(echo "$HEALTH" | python -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "error")

    if [ "$STATUS" = "healthy" ]; then
        log_info "Health check: healthy"
    elif [ "$STATUS" = "degraded" ]; then
        log_warn "Health check: degraded (check configuration)"
        echo "$HEALTH" | python -m json.tool
    else
        log_error "Health check failed"
        exit 1
    fi

    # Quick API test
    log_info "Testing API endpoint..."
    RESPONSE=$(curl -s -X POST http://localhost:8000/api/search \
        -H "Content-Type: application/json" \
        -d '{"query": "test"}' \
        -w "\n%{http_code}" | tail -1)

    if [ "$RESPONSE" = "200" ]; then
        log_info "API responding correctly"
    else
        log_warn "API returned status: $RESPONSE"
    fi
}

# Commit changes
commit_changes() {
    log_step "Step 4/5: Committing changes"
    echo ""

    # Check if there are changes to commit
    if [ -z "$(git status --porcelain)" ]; then
        log_info "No changes to commit"
        return 0
    fi

    # Show what will be committed
    log_info "Changes to commit:"
    git status --short
    echo ""

    # Generate commit message if not provided
    if [ -z "$COMMIT_MSG" ]; then
        # Auto-generate based on changed files
        CHANGED_FILES=$(git status --porcelain | wc -l | tr -d ' ')
        COMMIT_MSG="Deploy: Update $CHANGED_FILES file(s)"
    fi

    # Stage and commit
    git add -A
    git commit -m "$COMMIT_MSG"

    log_info "Committed: $COMMIT_MSG"
}

# Push to remote
push_changes() {
    log_step "Step 5/5: Pushing to remote"
    echo ""

    if [ "$NO_PUSH" = true ]; then
        log_warn "Skipping push (--no-push flag)"
        return 0
    fi

    # Check if there's a remote
    if ! git remote | grep -q "origin"; then
        log_warn "No remote 'origin' configured, skipping push"
        return 0
    fi

    # Check if we have commits to push
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
    if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
        AHEAD=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo "0")
        if [ "$AHEAD" = "0" ]; then
            log_info "Nothing to push"
            return 0
        fi
    fi

    # Push
    git push origin "$BRANCH"
    log_info "Pushed to origin/$BRANCH"
}

# Summary
show_summary() {
    echo ""
    echo "========================================"
    log_success "Deployment complete!"
    echo "========================================"
    echo ""
    echo "Server: http://localhost:8000"
    echo "Health: http://localhost:8000/health"
    echo ""

    # Show Tailscale URL if available
    if command -v tailscale &> /dev/null; then
        TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [ -n "$TS_IP" ]; then
            echo "Tailscale: http://$TS_IP:8000"
        fi
    fi
}

# Main
main() {
    echo ""
    echo "========================================"
    echo "         LifeOS Deployment"
    echo "========================================"
    echo ""

    activate_venv

    run_tests
    echo ""

    restart_server
    echo ""

    verify_deployment
    echo ""

    commit_changes
    echo ""

    push_changes

    show_summary
}

main
