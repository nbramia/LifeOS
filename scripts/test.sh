#!/bin/bash
# LifeOS Test Runner
# ==================
#
# Usage: ./scripts/test.sh [unit|integration|browser|smoke|all|auto|health]
#
# Test levels:
#   unit        - Fast tests, no external dependencies (~2min, parallelized)
#   integration - Tests requiring server to be running
#   browser     - Playwright browser tests (requires server)
#   smoke       - Unit + critical browser test (used by deploy.sh)
#   all         - Run all tests in sequence
#   auto        - Pick scope from the git diff (see decide_plan below)
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

# Force CPU-only embeddings for every test run (#521). This host's iGPU has
# only 8 SDMA queues; `pytest -n auto` below spawns one worker process per
# core (16 here), and each worker that touches EmbeddingService would
# otherwise independently try to load the GPU model — several processes
# grabbing GPU compute queues at once is the exact concurrency pattern that
# exhausted the queues and preceded the 2026-07-10 host freeze. Tests don't
# need GPU throughput. `tests/conftest.py`'s pytest_configure hook sets the
# same vars (defense in depth, and it's the only guard for anyone who runs
# pytest directly instead of through this script) — exporting here as well
# covers anything this script shells out to outside of pytest itself.
export HIP_VISIBLE_DEVICES=""
export ROCR_VISIBLE_DEVICES=""
export CUDA_VISIBLE_DEVICES=""

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

# Parallelize unit tests across all cores via pytest-xdist. --dist loadscope
# keeps every test in a module on the same worker, which avoids cross-module
# ordering surprises from shared singletons. Browser/integration runs stay
# serial (single shared server + Playwright), so they don't use this.
PYTEST_PARALLEL=(-n auto --dist loadscope)

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
#
# #682: this negative filter and the pre-push hook's `-m "unit and not slow"`
# are meant to select the same set, and after the #682 marker triage they
# collect the identical set (verified via --collect-only on both filters) —
# see scripts/pre-push and tests/conftest.py's pytest_collection_modifyitems
# for how a pre-existing name-substring auto-marker used to break that
# agreement and why it was removed rather than special-cased.
run_unit_tests() {
    log_step "Running unit tests..."
    python -m pytest tests/ -v \
        --ignore=tests/test_ui_browser.py \
        --ignore=tests/archive \
        -m "not browser and not requires_server and not integration and not slow" \
        --tb=short \
        -q \
        "${PYTEST_PARALLEL[@]}"
}

# Run integration tests (requires server)
run_integration_tests() {
    log_step "Running integration tests..."

    if ! check_server; then
        log_warn "Server not running. Starting server for integration tests..."
        start_server_background
        sleep 3
    fi

    # #682: was hardcoded to tests/test_e2e_flow.py only, so every other
    # `integration`-marked test (including the direct-DB data-integrity
    # suites) had no scope that ever ran them. Sweep the whole tree instead —
    # this is the only place `integration`-marked tests run on this box.
    python -m pytest tests/ -v \
        --ignore=tests/archive \
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

    # test_voice_mic_block_ui_browser.py serves web/ itself on an ephemeral port
    # and stubs every /api/ call, so it needs no server and carries no
    # `requires_server` marker — that's what lets pre-push run it. This scope
    # deliberately runs the full `browser` set, server-dependent tests included.
    python -m pytest tests/test_ui_browser.py tests/test_e2e_flow.py \
        tests/test_voice_mic_block_ui_browser.py -v \
        --ignore=tests/archive \
        -m "browser" \
        --tb=short \
        --browser chromium
}

# Run the single critical browser test that verifies the full user flow.
# Serial (single shared server + Playwright). Shared by smoke and auto.
run_critical_browser_test() {
    log_step "Running critical browser smoke test..."

    if ! check_server; then
        log_warn "Server not running. Starting server for browser test..."
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
}

# Run smoke tests (unit + critical browser test for deployment verification)
run_smoke_tests() {
    local start_time=$(date +%s)

    log_step "Running smoke tests (unit + critical browser test)..."
    echo ""

    # Unit tests first (fast feedback)
    run_unit_tests
    echo ""

    run_critical_browser_test

    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    log_info "Smoke tests passed in ${duration}s"
}

# Run slow tests (ChromaDB, embeddings, heavy processing). Parallelized.
run_slow_tests() {
    log_step "Running slow tests..."
    python -m pytest tests/ -v \
        --ignore=tests/test_ui_browser.py \
        --ignore=tests/archive \
        -m "slow and not browser and not requires_server and not integration" \
        --tb=short \
        -q \
        "${PYTEST_PARALLEL[@]}"
}

# Run a specific list of changed test files (parallelized). `slow` is NOT
# excluded here on purpose: if you changed a test file, run all of its cases.
run_changed_test_files() {
    log_step "Running changed test files: $*"
    python -m pytest "$@" -v \
        -m "not browser and not requires_server and not integration" \
        --tb=short \
        -q \
        "${PYTEST_PARALLEL[@]}"
}

# Start server in background for tests using server.sh
#
# #919: this deliberately holds no PID anywhere, file or variable.
# server.sh's own get_server_pid() identifies the server by port (`lsof -ti
# :$PORT`), not by a PID we'd have to hand it, and it already owns the whole
# start/stop lifecycle (kill_server, health-check wait, etc.) — so test.sh
# has nothing to track. A fixed path shared across every worktree/branch on
# this box would be the same cross-worktree collision class as #908/#913
# (two concurrent test.sh runs clobbering or killing each other's server),
# but this script used to hold a fixed PID-file path with no matching
# writer: nothing in the repo ever wrote it, and the helper that read/rm'd
# it was never called from anywhere, so it was dead since the initial
# commit. Removed rather than reintroduced with a per-run path, since a
# file nothing writes doesn't need a safer path — see #919. This also means
# test.sh still deliberately leaves the started server running after the
# run (unchanged prior behaviour) — server.sh's `start` already kills and
# replaces whatever was on the port before launching, so a leftover server
# from a previous run is cleaned up the next time any
# `start_server_background` call happens, not on this script's exit.
start_server_background() {
    log_info "Starting server for tests (takes 30-60s for ML model loading)..."

    # Use server.sh for robust startup (handles cleanup, lock files, proper timeouts)
    if ! "$SCRIPT_DIR/server.sh" start; then
        log_error "Server failed to start. Check logs: $PROJECT_DIR/logs/server.log"
        exit 1
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

# ---------------------------------------------------------------------------
# Auto mode: pick test scope from the git diff.
#
# Lets /implement (and anyone) run only the tests a change can affect.
# compute_changed_files() gathers the diff; decide_plan() is a pure mapping
# from a file list to a scope plan (kept pure so it's unit-testable via
# LIFEOS_TEST_PLAN_ONLY); run_auto() dispatches into the existing runners.
# ---------------------------------------------------------------------------

# Print the changed files (one per line) for the current branch: everything
# since the merge-base with origin/main, plus uncommitted and untracked work.
# Overridable via LIFEOS_TEST_CHANGED_FILES (newline-separated) for testing.
# Use ${VAR+x} (set, even if empty) — NOT ${VAR:-} (non-empty) — so an explicit
# empty override ("no changes") is honored instead of falling back to the git
# diff. Without this the 'empty' case picks up the working tree's real changes.
compute_changed_files() {
    if [ -n "${LIFEOS_TEST_CHANGED_FILES+x}" ]; then
        printf '%s\n' "$LIFEOS_TEST_CHANGED_FILES" | grep -v '^$' || true
        return
    fi
    local base
    base=$(git merge-base HEAD origin/main 2>/dev/null || true)
    {
        [ -n "$base" ] && { git diff --name-only "$base" HEAD 2>/dev/null || true; }
        git diff --name-only HEAD 2>/dev/null || true                  # unstaged tracked
        git diff --name-only --cached 2>/dev/null || true              # staged
        git ls-files --others --exclude-standard 2>/dev/null || true   # untracked
    } | sort -u | grep -v '^$' || true
}

# Pure mapping: changed-file list (newline-separated, in $1) -> scope plan.
# Plans: "skip" | "files <f1> <f2> ..." | "unit" [browser] [slow].
# Adding files can only broaden the plan, never narrow it, so unknown or
# stray files fall back to the safe full-unit run.
decide_plan() {
    local files="$1"

    # No detected changes -> safest default is the full unit suite.
    [ -z "$files" ] && { echo "unit"; return; }

    # docs-only: no file falls outside the docs patterns (matches pre-push).
    # Dependency manifests (requirements*.txt / constraints*.txt) are
    # code-affecting, so they're excluded from the docs class — a dep bump
    # must still run tests rather than skipping.
    if ! printf '%s\n' "$files" | grep -qE '(^|/)(requirements|constraints)[^/]*\.txt$' \
       && ! printf '%s\n' "$files" | grep -qvE '\.(md|txt|rst)$|^docs/'; then
        echo "skip"; return
    fi

    # tests-only: every changed file lives under tests/.
    if ! printf '%s\n' "$files" | grep -qvE '^tests/'; then
        # A conftest/fixture/helper change affects every test -> full suite.
        if printf '%s\n' "$files" | grep -qvE '^tests/test_[^/]*\.py$'; then
            echo "unit"
        else
            local list
            list=$(printf '%s\n' "$files" | grep -E '^tests/test_[^/]*\.py$' | tr '\n' ' ' | sed 's/ *$//')
            echo "files $list"
        fi
        return
    fi

    # Code change: always run unit; additively widen for the touched areas so
    # a change spanning categories is fully covered.
    local plan="unit"
    # Web assets live in web/ and are only *served* under the /static URL
    # prefix — matching on a `static/` path never fires (#518).
    if printf '%s\n' "$files" | grep -qE '\.html$|^web/.*\.js$|^api/routes/'; then
        plan="$plan browser"
    fi
    if printf '%s\n' "$files" | grep -qE '^scripts/run_all_syncs\.py$|^api/services/[^/]*sync[^/]*|indexer|embeddings|vectorstore|bm25_index'; then
        plan="$plan slow"
    fi
    echo "$plan"
}

# Compute the plan, then run it (or just print it under LIFEOS_TEST_PLAN_ONLY).
run_auto() {
    local files plan
    files=$(compute_changed_files)
    plan=$(decide_plan "$files")

    if [ -n "${LIFEOS_TEST_PLAN_ONLY:-}" ]; then
        echo "auto-plan: $plan"
        return 0
    fi

    log_info "Auto-selected test scope: $plan"
    case "$plan" in
        skip)
            log_info "Docs-only change — skipping tests."
            ;;
        files\ *)
            local existing=()
            local f
            for f in ${plan#files }; do
                [ -f "$f" ] && existing+=("$f")
            done
            if [ ${#existing[@]} -eq 0 ]; then
                log_warn "Changed test files no longer exist — running full unit suite."
                run_unit_tests
            else
                run_changed_test_files "${existing[@]}"
            fi
            ;;
        *)
            run_unit_tests
            case "$plan" in
                *browser*) echo ""; run_critical_browser_test ;;
            esac
            case "$plan" in
                *slow*) echo ""; run_slow_tests ;;
            esac
            ;;
    esac
}

# Main
# Plan-only auto runs are pure (no pytest), so they don't need the venv.
if [ "${1:-}" = "auto" ] && [ -n "${LIFEOS_TEST_PLAN_ONLY:-}" ]; then
    run_auto
    exit 0
fi

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
    auto)
        run_auto
        ;;
    health)
        run_health_check
        ;;
    *)
        echo "LifeOS Test Runner"
        echo ""
        echo "Usage: $0 [unit|integration|browser|smoke|all|auto|health]"
        echo ""
        echo "Test levels:"
        echo "  unit         Fast tests, no external dependencies (default)"
        echo "  integration  Tests requiring server to be running"
        echo "  browser      Playwright browser tests"
        echo "  smoke        Unit tests + critical browser test (for deployment)"
        echo "  all          Run all tests in sequence"
        echo "  auto         Pick scope from the git diff (unit/browser/slow/skip)"
        echo "  health       Quick server health check"
        exit 1
        ;;
esac
