#!/bin/bash
# LifeOS Remote Test Runner
# =========================
#
# Usage: ./scripts/remote-test.sh [test.sh-args]   (default: auto)
#
# Runs the test suite on nathan-linux for checkouts that have no local venv
# (the MacBook). Rsyncs the CURRENT WORKING TREE — uncommitted and untracked
# changes included — to an isolated dir on the server, then runs
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
# Privacy: rsync does NOT honor .gitignore. We build the exclude list from git
# so the sync carries only what git tracks plus untracked-unignored files —
# secrets (.env, config/token-*.json, config/credentials-*.json) and personal
# data (data/, ~15 GB) are gitignored and therefore never leave the machine.
#
# Streaming: remote stdout/stderr stream back live. A final marker line
#   [remote-test] DONE rc=<code>
# is always printed — even on Ctrl-C / kill — so a backgrounded run can be
# waited on with:
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

# Always clean up the temp exclude file; a dedicated INT/TERM trap guarantees
# the DONE marker is emitted even when the script is interrupted, so the
# documented `until grep -q "[remote-test] DONE"` wait loop can never hang.
EXCLUDE_FILE=""
cleanup() { [ -n "$EXCLUDE_FILE" ] && rm -f "$EXCLUDE_FILE"; }
trap cleanup EXIT
trap 'echo "[remote-test] DONE rc=130 (interrupted)"; exit 130' INT TERM

# Isolated remote dir keyed by BOTH the branch and a hash of this checkout's
# path, so two agents on the same branch but in different worktrees don't
# clobber each other's run, while re-runs from the same checkout sync
# incrementally (fast). Sanitize the branch to a safe charset — a refname may
# legally contain shell metacharacters (`;`, `$`, quotes) and it is
# interpolated into the remote path and command below.
BRANCH="$(git branch --show-current 2>/dev/null || echo detached)"
[ -z "$BRANCH" ] && BRANCH="detached"
SAFE_BRANCH="$(printf '%s' "$BRANCH" | tr -c 'A-Za-z0-9._-' '_')"
DIR_HASH="$(printf '%s' "$PROJECT_DIR" | cksum | cut -d' ' -f1)"
REMOTE_DIR="$REMOTE_BASE/${SAFE_BRANCH}-${DIR_HASH}"

# test.sh mode/args (default: diff-aware auto).
TEST_ARGS=("$@")
[ ${#TEST_ARGS[@]} -eq 0 ] && TEST_ARGS=(auto)

# Build the rsync exclude list from git: every path git ignores. This is the
# authoritative privacy boundary (see header) and also mirrors exactly what
# test.sh's own diff logic ignores. .git is NOT ignored, so it is still synced
# (the remote `test.sh auto` needs it for the merge-base diff).
EXCLUDE_FILE="$(mktemp "${TMPDIR:-/tmp}/lifeos-remote-test-exclude.XXXXXX")"
if ! git ls-files --others --ignored --exclude-standard --directory > "$EXCLUDE_FILE" 2>/dev/null; then
    echo "[remote-test] could not compute gitignore excludes — refusing to sync (would risk leaking secrets)"
    echo "[remote-test] DONE rc=1"
    exit 1
fi
# Anchor each pattern to the transfer root so e.g. /data/ excludes only the
# top-level dir, not any nested directory that happens to share the name.
sed -i.bak 's#^#/#' "$EXCLUDE_FILE" && rm -f "$EXCLUDE_FILE.bak"

echo "[remote-test] branch=$BRANCH -> $REMOTE_HOST:$REMOTE_DIR"
echo "[remote-test] syncing working tree (uncommitted + untracked; gitignored paths excluded)..."

# Create the remote dir (and its parent) first — rsync only creates the final
# path component, so a missing $REMOTE_BASE makes the first-ever run, and every
# run after a reboot wipes /tmp, fail. mkdir -p is idempotent.
if ! ssh "$REMOTE_HOST" "mkdir -p $(printf '%q' "$REMOTE_DIR")"; then
    echo "[remote-test] cannot reach $REMOTE_HOST (try: tailscale status)"
    echo "[remote-test] DONE rc=1"
    exit 1
fi

# --delete keeps the remote copy an exact mirror; --exclude-from applies the
# gitignore-derived privacy list. The inline excludes are belt-and-suspenders
# for heavy build artifacts that aren't necessarily gitignored (node_modules,
# .gstack, stray *.pyc). Never --exclude .git.
rsync -a --delete --exclude-from="$EXCLUDE_FILE" \
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

# Build the remote command with the dir and every arg shell-quoted, so a branch
# name or test arg can never break out of the ssh command string.
REMOTE_CMD="cd $(printf '%q' "$REMOTE_DIR") && ./scripts/test.sh"
for arg in "${TEST_ARGS[@]}"; do
    REMOTE_CMD+=" $(printf '%q' "$arg")"
done
ssh "$REMOTE_HOST" "$REMOTE_CMD"
TEST_RC=$?

echo "----------------------------------------------------------------------"
if [ "$TEST_RC" -eq 0 ]; then
    echo "[remote-test] DONE rc=0 (tests passed)"
else
    echo "[remote-test] DONE rc=$TEST_RC (tests failed — see output above)"
fi
exit "$TEST_RC"
