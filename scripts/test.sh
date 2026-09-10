#!/bin/bash
# LifeOS Test Runner
# ==================
#
# Usage: ./scripts/test.sh [unit|integration|browser|smoke|all|auto|health]
#
# Test levels:
#   unit        - Fast tests, no external dependencies (~2min, parallelized)
#   integration - Integration lane (server prerequisites are checked per lane)
#   browser     - All browser lanes (server-free and server-dependent)
#   smoke       - Unit + critical browser test (used by deploy.sh)
#   all         - Run all tests in sequence
#   auto        - Pick scope from the git diff (see decide_plan below)
#   health      - Quick server health check
#
# Note: Only server-dependent lanes start the server automatically (takes 30-60s
# for ML model loading). Server-free browser tests remain independent of it.
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
source "$SCRIPT_DIR/test-lanes.sh"

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
# keeps a test class (or a module's top-level functions) on one worker, which
# avoids ordering surprises from shared singletons within a group.
# Browser/integration runs stay serial (single shared server + Playwright),
# so they don't use this.
PYTEST_PARALLEL=()
# Cold/synthetic runners may intentionally have only pytest installed. The
# lane remains complete serially there; a configured parallel dispatch never
# turns into an unrecognized-option collection failure.
if python -c "import xdist" >/dev/null 2>&1; then
    PYTEST_PARALLEL=(-n "${LIFEOS_TEST_PARALLEL_WORKERS:-auto}" --dist loadscope)
fi

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

ensure_lane_prerequisites() {
    local lane="$1"
    if test_lane_requires_playwright "$lane" && ! python -c "import playwright" 2>/dev/null; then
        log_error "Lane '$lane' requires Playwright. Run: pip install playwright && playwright install"
        return 1
    fi
    if test_lane_requires_server "$lane" && ! check_server; then
        log_warn "Server not running. Starting it for lane '$lane'..."
        start_server_background || return 1
        sleep 3
    fi
}

# Run one explicit, disjoint lane.  Browser and server lanes stay in their own
# process; fast and isolated-slow lanes retain the existing xdist behaviour.
run_lane() {
    local lane="$1"
    shift
    local marker
    marker=$(test_lane_marker "$lane") || return
    if [ -n "${LIFEOS_TEST_DISPATCH_ONLY:-}" ]; then
        echo "auto-dispatch: $lane"
        return 0
    fi
    ensure_lane_prerequisites "$lane" || return 1
    log_step "Running test lane: $lane"
    local parallel=()
    case "$lane" in fast-unit|slow) parallel=("${PYTEST_PARALLEL[@]}") ;; esac
    local browser_args=()
    test_lane_requires_playwright "$lane" && browser_args=(--browser chromium)
    python -m pytest "$@" -v --ignore=tests/archive -m "$marker" --tb=short -q \
        "${parallel[@]}" "${browser_args[@]}"
}

run_unit_tests() { run_candidate_verification fast-unit; }
run_integration_tests() { run_candidate_verification server,integration; }
run_browser_tests() {
    run_candidate_verification browser-free
    run_candidate_verification browser-server
}

# Run the single critical browser test that verifies the full user flow.
# Serial (single shared server + Playwright). Used by the smoke command.
run_critical_browser_test() {
    run_candidate_verification "browser-server" "" "tests/test_e2e_flow.py::TestRealUserFlow::test_user_sends_query_gets_response"
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

run_slow_tests() { run_candidate_verification slow; }

# Verify a complete local candidate through the same snapshot/evidence/
# capacity/supervisor path used by pre-push. Covers every lane, including
# server-owned ones: verify_candidate.py's pytest_lane_executor spins up an
# owned TestInstance itself whenever a lane's prerequisites include
# "server" (and adds --browser chromium whenever they include "playwright"),
# so a server-owned lane never touches the shared production server or its
# localhost:8000 default.
run_candidate_verification() {
    local lanes="${1:-fast-unit,browser-free}"
    local paths="${2:-}"
    local nodeids="${3:-}"
    if [ -n "${LIFEOS_TEST_DISPATCH_ONLY:-}" ]; then
        echo "auto-dispatch: $lanes"
        return 0
    fi
    local evidence_root="${XDG_CACHE_HOME:-$HOME/.cache}/lifeos/verification-evidence"
    local path_args=()
    [ -n "$paths" ] && path_args=(--paths "$paths")
    [ -n "$nodeids" ] && path_args+=(--nodeids "$nodeids")
    python "$SCRIPT_DIR/verify_candidate.py" local \
        --source "$PROJECT_DIR" --evidence-root "$evidence_root" \
        --lanes "$lanes" \
        --workers "${LIFEOS_TEST_PARALLEL_WORKERS:-1}" "${path_args[@]}"
}

# Collection drives changed-test dispatch.  A mixed-marker file may correctly
# execute in more than one lane; a test ID itself remains in exactly one lane.
run_changed_test_files() {
    if [ -z "${LIFEOS_TEST_DISPATCH_ONLY:-}" ]; then
        local selected_paths
        selected_paths=$(IFS=,; echo "$*")
        run_candidate_verification "fast-unit,slow,browser-free,browser-server,server,integration" "$selected_paths"
        return $?
    fi
    local inventory_dir inventory selected=0 lane count
    inventory_dir=$(mktemp -d) || { log_error "Could not create lane inventory directory"; return 1; }
    inventory="$inventory_dir/lane-inventory.json"
    if ! test_lane_collect "$inventory" "$@"; then
        log_error "Could not collect changed test files into a lane inventory."
        rm -rf "$inventory_dir"
        return 1
    fi
    if ! test_lane_inventory_is_usable "$inventory"; then
        log_error "Changed test collection did not produce a usable lane inventory."
        rm -rf "$inventory_dir"
        return 1
    fi
    for lane in "${TEST_LANE_NAMES[@]}"; do
        count=$(test_lane_count "$inventory" "$lane")
        [ "$count" = "0" ] && continue
        selected=$((selected + count))
        if [ -n "${LIFEOS_TEST_DISPATCH_ONLY:-}" ]; then
            echo "auto-dispatch: $lane ($count tests)"
        else
            run_candidate_verification "$lane" "$(IFS=,; echo "$*")"
        fi
    done
    if [ "$selected" = "0" ]; then
        log_error "Changed test files selected zero tests in every lane; check markers or deleted paths."
        rm -rf "$inventory_dir"
        return 1
    fi
    rm -rf "$inventory_dir"
}

# Start server in background for tests using server.sh
#
# Deliberately holds no PID anywhere, file or variable. server.sh's own
# get_server_pid() identifies the server by port (`lsof -ti :$PORT`), not by
# a PID handed to it, and it owns the whole start/stop lifecycle
# (kill_server, health-check wait, etc.) -- so test.sh has nothing to track.
# The server itself is one shared resource on a single hardcoded port:
# server.sh's `start` kills and replaces whatever is listening there before
# launching, so two test.sh runs that both need a server contend for that
# one port regardless of any PID file -- that is server.sh's contract, not
# something a PID path, fixed or per-run, could change. test.sh deliberately
# leaves the started server running after the run: a leftover server from a
# previous run is cleaned up the next time any `start_server_background`
# call happens, not on this script's exit.
start_server_background() {
    log_info "Starting server for tests (takes 30-60s for ML model loading)..."

    # Use server.sh for robust startup (handles cleanup, lock files, proper timeouts)
    if ! "$SCRIPT_DIR/server.sh" start; then
        log_error "Server failed to start. Check logs: $PROJECT_DIR/logs/server.log"
        return 1
    fi
}

# Verify that marker precedence partitions every non-archived collected test.
# An optional caller-owned receipt lets `all` reuse this one collection for
# dispatch, avoiding a second startup just to learn which lanes are nonempty.
verify_lane_inventory() {
    local temp_dir="" inventory="${1:-}"
    if [ -z "$inventory" ]; then
        temp_dir=$(mktemp -d) || { log_error "Could not create lane inventory directory"; return 1; }
        inventory="$temp_dir/lane-inventory.json"
    fi
    if ! test_lane_collect "$inventory" tests/; then
        log_error "Could not collect the full test inventory."
        [ -n "$temp_dir" ] && rm -rf "$temp_dir"
        return 1
    fi
    if ! test_lane_inventory_is_usable "$inventory"; then
        log_error "Test lanes do not partition the collected non-archived inventory."
        [ -n "$temp_dir" ] && rm -rf "$temp_dir"
        return 1
    fi
    if [ -n "$temp_dir" ]; then
        rm -rf "$temp_dir"
    fi
    return 0
}

run_inventory_lane() {
    local inventory="$1" lane="$2"
    local count
    count=$(test_lane_count "$inventory" "$lane") || return 1
    case "$count" in
        ''|*[!0-9]*) log_error "Lane '$lane' inventory count is invalid."; return 1 ;;
    esac
    [ "$count" = "0" ] && return 0
    run_lane "$lane" tests/
}

# Run all non-archived tests through the complete lane partition.
run_all_tests() {
    local start_time=$(date +%s)

    log_step "Running full test suite..."
    echo ""

    run_candidate_verification "fast-unit,slow,browser-free,browser-server,server,integration" || return $?

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
                log_warn "Changed test files no longer exist — running the conservative fast-unit lane."
                run_unit_tests
            else
                run_changed_test_files "${existing[@]}"
            fi
            ;;
        *)
            local lanes="fast-unit"
            case "$plan" in
                *browser*) lanes="$lanes,browser-free,browser-server" ;;
            esac
            case "$plan" in
                *slow*) lanes="$lanes,slow" ;;
            esac
            run_candidate_verification "$lanes"
            ;;
    esac
}

# Main
# Plan-only auto runs are pure. Dispatch-only runs collect synthetic node IDs
# in tests but never execute pytest, so neither needs the venv.
if [ "${1:-}" = "auto" ] && { [ -n "${LIFEOS_TEST_PLAN_ONLY:-}" ] || [ -n "${LIFEOS_TEST_DISPATCH_ONLY:-}" ]; }; then
    run_auto
    exit 0
fi

# Test-only inventory mode validates lane partitioning with a synthetic pytest
# collector. It deliberately stops before venv activation or test execution.
if [ "${1:-}" = "all" ] && [ -n "${LIFEOS_TEST_LANE_INVENTORY_ONLY:-}" ]; then
    verify_lane_inventory
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
    candidate)
        run_candidate_verification
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
        echo "Usage: $0 [unit|integration|browser|smoke|all|auto|candidate|health]"
        echo ""
        echo "Test levels:"
        echo "  unit         Fast tests, no external dependencies (default)"
        echo "  integration  Tests requiring server to be running"
        echo "  browser      Playwright browser tests"
        echo "  smoke        Unit tests + critical browser test (for deployment)"
        echo "  all          Run all tests in sequence"
        echo "  auto         Pick scope from the git diff (unit/browser/slow/skip)"
        echo "  candidate    Isolated fast-unit + server-free-browser candidate verification"
        echo "  health       Quick server health check"
        exit 1
        ;;
esac
