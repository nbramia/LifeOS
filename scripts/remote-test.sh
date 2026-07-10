#!/bin/bash
# LifeOS Remote Test Runner
# =========================
#
# Usage: ./scripts/remote-test.sh [test.sh-args]   (default: auto)
#
# Runs the test suite on nathan-linux for checkouts that have no local venv
# (the MacBook). Rsyncs the CURRENT WORKING TREE — uncommitted and untracked
# changes included — to a branch-keyed isolated dir on the server, then runs
# ./scripts/test.sh there. No commit or push is required, so this satisfies
# AGENTS.md § Testing ("get a green run before committing; rsync the tree to an
# isolated temp dir") without the contradiction the old push-then-worktree
# dance created.
#
# The default mode is `auto`, which picks scope (unit/browser/slow/skip) from
# the git diff exactly as ./scripts/test.sh auto does locally — the rsynced
# copy includes .git plus your working-tree edits, so the remote diff matches
# the Mac's. Pass any test.sh mode to override, e.g. `remote-test.sh unit`.
#
# Streaming: remote stdout/stderr stream back live. A final marker line
#   [remote-test] DONE rc=<code>
# is always printed, so a backgrounded run can be waited on with:
#   ./scripts/remote-test.sh > "$OUT" 2>&1 &   # (or run_in_background)
#   until grep -q "\[remote-test\] DONE" "$OUT"; do sleep 5; done
#
# Overridable via env: LIFEOS_REMOTE_HOST (ssh target, default nathan-linux-ts),
# LIFEOS_REMOTE_TEST_DIR (remote parent dir, default /tmp/lifeos-remote-test).

set -u

REMOTE_HOST="${LIFEOS_REMOTE_HOST:-nathan-linux-ts}"
REMOTE_BASE="${LIFEOS_REMOTE_TEST_DIR:-/tmp/lifeos-remote-test}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Branch-keyed remote dir so parallel worktrees/branches don't clobber each
# other, and re-runs on the same branch sync incrementally (fast).
BRANCH="$(git branch --show-current 2>/dev/null || echo detached)"
[ -z "$BRANCH" ] && BRANCH="detached"
SAFE_BRANCH="$(printf '%s' "$BRANCH" | tr '/ ' '__')"
REMOTE_DIR="$REMOTE_BASE/$SAFE_BRANCH"

# test.sh mode/args (default: diff-aware auto).
TEST_ARGS=("$@")
[ ${#TEST_ARGS[@]} -eq 0 ] && TEST_ARGS=(auto)

echo "[remote-test] branch=$BRANCH -> $REMOTE_HOST:$REMOTE_DIR"
echo "[remote-test] syncing working tree (uncommitted + untracked included)..."

# Rsync the whole tree INCLUDING .git (so the remote `test.sh auto` sees the
# same merge-base diff). Exclude only heavy/irrelevant local artifacts; never
# --exclude .git. --delete keeps the remote copy an exact mirror of local.
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache/' \
    --exclude='node_modules/' \
    --exclude='.gstack/' \
    --exclude='logs/' \
    -e ssh \
    "$PROJECT_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"
RSYNC_RC=$?

if [ "$RSYNC_RC" -ne 0 ]; then
    echo "[remote-test] rsync failed (is $REMOTE_HOST reachable? try: tailscale status)"
    echo "[remote-test] DONE rc=$RSYNC_RC"
    exit "$RSYNC_RC"
fi

echo "[remote-test] running ./scripts/test.sh ${TEST_ARGS[*]} on $REMOTE_HOST..."
echo "----------------------------------------------------------------------"

ssh "$REMOTE_HOST" "cd '$REMOTE_DIR' && ./scripts/test.sh ${TEST_ARGS[*]}"
TEST_RC=$?

echo "----------------------------------------------------------------------"
if [ "$TEST_RC" -eq 0 ]; then
    echo "[remote-test] DONE rc=0 (tests passed)"
else
    echo "[remote-test] DONE rc=$TEST_RC (tests failed — see output above)"
fi
exit "$TEST_RC"
