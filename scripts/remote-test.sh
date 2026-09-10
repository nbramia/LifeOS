#!/bin/bash
# LifeOS Remote Test Runner
# =========================
#
# Usage: ./scripts/remote-test.sh [test.sh-args]   (default: auto; use
# `candidate` for the isolated snapshot/evidence verifier on the runner host)
#
# Runs the test suite on the server host for checkouts that have no local venv
# (the MacBook). Rsyncs the CURRENT WORKING TREE — uncommitted and untracked
# changes included — to an isolated dir on the server, then runs
# ./scripts/test.sh there. No commit or push is required, so this satisfies
# AGENTS.md § Testing ("get a green run before committing; rsync the tree to an
# isolated temp dir") without the contradiction the old push-then-worktree
# dance created.
#
# The default mode is `auto`, which picks scope (unit/browser/slow/skip) from
# the git diff exactly as ./scripts/test.sh auto does locally. `.git` itself is
# never rsynced — a linked worktree's `.git` is a file pointing at the source
# host's administrative directory, which does not exist remotely, and even an
# ordinary checkout's `.git` carries credentials/hooks/config this transfer
# must not export. Instead, `_remote_git_bundle.py` resolves HEAD and (if
# present) origin/main locally, bundles just that history, and the remote side
# materializes a fresh, self-contained repository from it (throwaway identity,
# no source credentials) before your working-tree edits are rsynced on top —
# so the remote diff matches the Mac's, including uncommitted/untracked state.
# Pass any test.sh mode to override, e.g. `remote-test.sh unit`.
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
# Configure via env: LIFEOS_REMOTE_HOST (ssh target of the machine that has the
# venv — required, no default, since it is specific to your setup),
# LIFEOS_REMOTE_TEST_DIR (remote parent dir, default /tmp/lifeos-remote-test).

set -u

REMOTE_HOST="${LIFEOS_REMOTE_HOST:-}"
if [ -z "$REMOTE_HOST" ]; then
    echo "remote-test: LIFEOS_REMOTE_HOST is not set." >&2
    echo "  Set it to the ssh target of the machine that hosts the LifeOS venv, e.g." >&2
    echo "    export LIFEOS_REMOTE_HOST=my-server" >&2
    echo "  (add it to your shell profile so it persists)." >&2
    exit 2
fi
REMOTE_BASE="${LIFEOS_REMOTE_TEST_DIR:-/tmp/lifeos-remote-test}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Development-lifecycle instrumentation: shared JSONL record format from
# development_metrics.py, never a second bespoke format. Best-effort only —
# every call is bounded and swallowed on failure (see _bounded/_metrics_record)
# so a missing venv, an unwritable receipt, an unreachable host, or an
# invalid value can never change this script's real exit code, which is
# decided solely by the actual rsync/ssh outcomes.
METRICS_PY="$HOME/.venvs/lifeos/bin/python"
[ -x "$METRICS_PY" ] || METRICS_PY="python3"
METRICS_PATH="${LIFEOS_DEV_METRICS_PATH:-$HOME/.cache/lifeos/remote-test-metrics/metrics.jsonl}"
METRICS_RUN_ID=""
METRICS_ACTIVE_PHASE=""
METRICS_ACTIVE_KIND=""
METRICS_PHASE_STARTED=""

# Run "$@" with a three-second deadline. Deliberately not the external
# `timeout` binary — GNU coreutils' `timeout` is not guaranteed present on
# macOS, and this wrapper is documented to run from the Mac. The Python
# supervisor owns the child process group, so a timeout closes every inherited
# pipe writer before returning to a command substitution.
_bounded() {
    "$METRICS_PY" "$SCRIPT_DIR/_bounded_exec.py" "$@"
}

# A real monotonic reading from this process's own Python interpreter — used
# for every elapsed-time measurement below instead of bash's wall-clock-based
# $SECONDS, and portable to any host with the same interpreter (Linux or
# macOS both provide CLOCK_MONOTONIC; there is nothing GNU-specific here).
_metrics_now() {
    _bounded "$METRICS_PY" "$SCRIPT_DIR/development_metrics.py" now 2>/dev/null
}

# $1=phase $2=phase-kind $3=result $4=started-monotonic (from _metrics_now; empty means "no real duration to report") $5=exit-status (empty for none)
_metrics_record() {
    [ -z "${METRICS_CANDIDATE:-}" ] && return 0
    local out args=(record --path "$METRICS_PATH" --candidate-id "$METRICS_CANDIDATE"
        --phase "$1" --phase-kind "$2" --result "$3")
    if [ -n "${4:-}" ]; then
        args+=(--started-monotonic "$4")
    fi
    [ -n "$METRICS_RUN_ID" ] && args+=(--run-id "$METRICS_RUN_ID")
    [ -n "${5:-}" ] && args+=(--exit-status "$5")
    [ -n "${TEST_ARGS_0:-}" ] && args+=(--suite "$TEST_ARGS_0")
    out="$(_bounded "$METRICS_PY" "$SCRIPT_DIR/development_metrics.py" "${args[@]}" 2>/dev/null)" || return 0
    [ -z "$METRICS_RUN_ID" ] && METRICS_RUN_ID="$out"
    return 0
}

# Best-effort: after a completed (non-interrupted) remote dispatch, fetch the
# remote host's OWN real queue/execution records for this exact shared
# run_id — sourced from its own verify_candidate.py invocation (via
# LIFEOS_DEV_METRICS_RUN_ID below), never guessed by this wrapper — and
# import them verbatim, preserving their own phase/phase_kind/result exactly.
# This is what actually distinguishes real remote capacity-queue waiting and
# real remote test execution from this script's own local connect/transfer/
# envelope timings above, which measure something different (this wrapper's
# own SSH/rsync overhead, not the remote host's internal scheduling). If the
# remote host is unreachable for this step, has no matching records (an
# older verify_candidate.py, a non-"candidate" test.sh mode, or metrics
# construction failed there), that is recorded explicitly as unknown rather
# than silently omitted or guessed as zero/success.
_correlate_remote_receipts() {
    [ -z "$METRICS_RUN_ID" ] && return 0
    local pattern="\"run_id\":\"${METRICS_RUN_ID}\""
    local remote_cmd
    remote_cmd="f=\$HOME/.cache/lifeos/verification-evidence/metrics.jsonl; [ -f \"\$f\" ] && grep -F $(printf '%q' "$pattern") \"\$f\" || true"
    local lines imported=0
    lines="$(_bounded ssh "$REMOTE_HOST" "$remote_cmd" 2>/dev/null)"
    if [ -n "$lines" ]; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            if printf '%s\n' "$line" | _bounded "$METRICS_PY" "$SCRIPT_DIR/development_metrics.py" import --path "$METRICS_PATH" >/dev/null 2>&1; then
                imported=$((imported + 1))
            fi
        done <<< "$lines"
    fi
    if [ "$imported" -eq 0 ]; then
        _metrics_record remote-verifier-correlation execution unknown "" ""
    fi
}

# Always clean up the temp exclude file and git bundle; a dedicated INT/TERM
# trap guarantees the DONE marker is emitted even when the script is
# interrupted, so the documented `until grep -q "[remote-test] DONE"` wait
# loop can never hang.
EXCLUDE_FILE=""
BUNDLE_FILE=""
LS_FILES_TMP=""
cleanup() {
    [ -n "$EXCLUDE_FILE" ] && rm -f "$EXCLUDE_FILE"
    [ -n "$BUNDLE_FILE" ] && rm -f "$BUNDLE_FILE"
    [ -n "$LS_FILES_TMP" ] && rm -f "$LS_FILES_TMP"
}
trap cleanup EXIT
_interrupted() {
    # Whichever transfer/connect/execution phase was in flight is reported
    # as cancelled — a distinct, honest outcome from a plain failure — before
    # the required DONE marker prints. $METRICS_CANDIDATE and
    # $METRICS_PHASE_STARTED are read at signal-delivery time, not
    # trap-registration time, so this is correct even though both are
    # assigned after this trap is installed. No remote-receipt correlation
    # is attempted here — a fresh SSH round-trip during an interrupt could
    # itself hang and jeopardize the DONE-marker guarantee this trap exists
    # to keep.
    if [ -n "$METRICS_ACTIVE_PHASE" ]; then
        _metrics_record "$METRICS_ACTIVE_PHASE" "$METRICS_ACTIVE_KIND" cancelled "$METRICS_PHASE_STARTED" ""
    fi
    echo "[remote-test] DONE rc=130 (interrupted)"
    exit 130
}
trap _interrupted INT TERM

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
# An opaque candidate identity for metrics: a checksum of the branch+path
# hash pair, never the raw branch text itself (unlike REMOTE_DIR above,
# which is a real remote directory name and is allowed, even expected, to be
# human-readable). A record exported from this script must never let a
# reader recover a branch name or any other project-identifying word from
# candidate_id.
METRICS_CANDIDATE="remote-$(printf '%s' "${SAFE_BRANCH}:${DIR_HASH}" | cksum | cut -d' ' -f1)"

# test.sh mode/args (default: diff-aware auto).
TEST_ARGS=("$@")
[ ${#TEST_ARGS[@]} -eq 0 ] && TEST_ARGS=(auto)
TEST_ARGS_0="${TEST_ARGS[0]}"

# Build the rsync exclude list from git: every path git ignores. This is the
# authoritative privacy boundary (see header) and also mirrors exactly what
# test.sh's own diff logic ignores. .git itself is excluded separately below
# — never rsynced — since a linked worktree's .git is a file pointing at the
# source host's administrative directory (main checkout's .git/worktrees/...),
# which does not exist on the execution host; and even for an ordinary
# checkout, .git carries credentials/hooks/config this transfer must not
# export. A self-contained git identity is materialized on the execution
# host instead (see _remote_git_bundle.py below).
EXCLUDE_FILE="$(mktemp "${TMPDIR:-/tmp}/lifeos-remote-test-exclude.XXXXXX")"
# -z (NUL-separated) plus _remote_exclude_list.py's own escaping: an
# unescaped ignored filename containing an rsync glob character ("[", "*",
# "?") would silently fail to exclude by that exact name — "[1]" in a bare
# pattern is a character class matching "1", not the literal substring
# "[1]" — so an ignored secret named e.g. "token[1].json" would transfer
# anyway. _remote_exclude_list.py also anchors each pattern to the
# transfer root (a leading "/") so e.g. /data/ excludes only the top-level
# dir, not any nested directory that happens to share the name. Its output
# is NUL-separated too, consumed below via the matching rsync --from0 flag
# — a pattern containing a literal newline can't be represented as one
# *line* in --exclude-from's default line-oriented format at all, but a
# NUL-delimited entry keeps it intact as one pattern.
#
# Deliberately two sequential commands through temp files, not one piped
# command whose exit status is inspected via $PIPESTATUS: this script has
# no other use for $PIPESTATUS, and a `git ls-files` failure here must
# never be silently treated as success — checking each stage's own exit
# status directly, one command at a time, is simpler to get right than a
# multi-element array read.
LS_FILES_TMP="$(mktemp "${TMPDIR:-/tmp}/lifeos-remote-test-lsfiles.XXXXXX")"
if ! git ls-files -z --others --ignored --exclude-standard --directory > "$LS_FILES_TMP" 2>/dev/null; then
    echo "[remote-test] could not compute gitignore excludes — refusing to sync (would risk leaking secrets)"
    echo "[remote-test] DONE rc=1"
    exit 1
fi
if ! "$METRICS_PY" "$SCRIPT_DIR/_remote_exclude_list.py" < "$LS_FILES_TMP" > "$EXCLUDE_FILE"; then
    echo "[remote-test] could not compute gitignore excludes — refusing to sync (would risk leaking secrets)"
    echo "[remote-test] DONE rc=1"
    exit 1
fi

# Materialize a self-contained git identity to send, rather than the source
# .git itself. _remote_git_bundle.py resolves HEAD and (if present)
# origin/main via plain git commands — never by reading the .git file/dir
# directly — so this works identically for an ordinary checkout or a linked
# worktree, and never touches config/hooks/credentials. Purely local, so it
# runs before paying for any SSH round-trip.
BUNDLE_FILE="$(mktemp "${TMPDIR:-/tmp}/lifeos-remote-test-bundle.XXXXXX.bundle")"
BUNDLE_INFO="$("$METRICS_PY" "$SCRIPT_DIR/_remote_git_bundle.py" --repo "$PROJECT_DIR" --bundle-out "$BUNDLE_FILE" 2>&1)"
if [ $? -ne 0 ]; then
    echo "[remote-test] could not prepare a self-contained git bundle: $BUNDLE_INFO"
    echo "[remote-test] DONE rc=1"
    exit 1
fi
# Field separator is ASCII Unit Separator (0x1F), matching
# _remote_git_bundle.py's output — NOT a tab: bash's own `IFS=$'\t' read`
# collapses a run of consecutive tabs exactly like it collapses spaces,
# which silently misparses an empty middle field (a detached HEAD's empty
# branch) as if it were absent. 0x1F does not have this collapsing
# behavior, and git forbids it in ref names, so it can never appear inside
# a real field value either.
IFS=$'\x1f' read -r HEAD_SHA REAL_BRANCH ORIGIN_MAIN_SHA <<< "$BUNDLE_INFO"
REMOTE_BUNDLE_PATH="${REMOTE_DIR}.git-bundle"

echo "[remote-test] branch=$BRANCH -> $REMOTE_HOST:$REMOTE_DIR"
echo "[remote-test] syncing working tree (uncommitted + untracked; gitignored paths excluded)..."

# Create the remote dir (and its parent) first — rsync only creates the final
# path component, so a missing $REMOTE_BASE makes the first-ever run, and every
# run after a reboot wipes /tmp, fail. mkdir -p is idempotent.
#
# This is local connection/envelope overhead (an SSH round-trip this wrapper
# pays before any real work starts) — phase_kind "transfer", never "waiting".
# "waiting" in this shared schema means a real capacity-queue wait (see
# verify_candidate.py's own "verification-queue" phase); an SSH handshake is
# not that, and must never be recorded as if it were.
METRICS_ACTIVE_PHASE="remote-connect"; METRICS_ACTIVE_KIND="transfer"; METRICS_PHASE_STARTED="$(_metrics_now)"
if ! ssh "$REMOTE_HOST" "mkdir -p $(printf '%q' "$REMOTE_DIR")"; then
    _metrics_record remote-connect transfer failure "$METRICS_PHASE_STARTED" 1
    METRICS_ACTIVE_PHASE=""
    echo "[remote-test] cannot reach $REMOTE_HOST (try: tailscale status)"
    echo "[remote-test] DONE rc=1"
    exit 1
fi
_metrics_record remote-connect transfer success "$METRICS_PHASE_STARTED" ""
METRICS_ACTIVE_PHASE=""

# Send the bundle and materialize a real, self-contained git repository at
# $REMOTE_DIR from it — a throwaway local identity, HEAD's history, and (if
# resolved) origin/main's, nothing else. This must complete BEFORE the
# content rsync below: it populates a clean working tree matching HEAD via
# `checkout -f` first, so the content rsync's own overlay (uncommitted edits
# + untracked-unignored files) lands on top and reproduces the exact
# uncommitted state `git status`/`git diff` see locally — reversing this
# order would let a plain `checkout -f` clobber the real dirty content this
# transfer exists to preserve. Any stale files left in $REMOTE_DIR by a
# previous run are reconciled by the content rsync's own --delete, same as
# before.
echo "[remote-test] materializing self-contained git identity..."
METRICS_ACTIVE_PHASE="remote-git-init"; METRICS_ACTIVE_KIND="transfer"; METRICS_PHASE_STARTED="$(_metrics_now)"
GIT_INIT_OK=1
if ! rsync -e ssh "$BUNDLE_FILE" "$REMOTE_HOST:$REMOTE_BUNDLE_PATH"; then
    GIT_INIT_OK=0
fi
if [ "$GIT_INIT_OK" -eq 1 ]; then
    # Do not fetch directly into refs/heads/$REAL_BRANCH: a fresh `git init`
    # has its configured default branch checked out even before its first
    # commit, and Git refuses to update that checked-out unborn branch. Fetch
    # HEAD into a private temporary ref first, then create/reset the requested
    # branch from it. This works whether the source branch matches the remote
    # default branch or not.
    REMOTE_HEAD_REF="refs/lifeos/remote-transfer-head"
    FETCH_ARGS=" $(printf '%q' "$HEAD_SHA:$REMOTE_HEAD_REF")"
    [ -n "$ORIGIN_MAIN_SHA" ] && FETCH_ARGS+=" $(printf '%q' "$ORIGIN_MAIN_SHA:refs/remotes/origin/main")"
    REMOTE_GIT_CMD="set -eu"
    REMOTE_GIT_CMD+=" && mkdir -p $(printf '%q' "$REMOTE_DIR")"
    REMOTE_GIT_CMD+=" && cd $(printf '%q' "$REMOTE_DIR")"
    REMOTE_GIT_CMD+=" && rm -rf .git"
    REMOTE_GIT_CMD+=" && git init -q ."
    REMOTE_GIT_CMD+=" && git config user.name $(printf '%q' 'LifeOS Remote Test')"
    REMOTE_GIT_CMD+=" && git config user.email $(printf '%q' 'remote-test@lifeos.invalid')"
    REMOTE_GIT_CMD+=" && git fetch -q $(printf '%q' "$REMOTE_BUNDLE_PATH")$FETCH_ARGS"
    if [ -n "$REAL_BRANCH" ]; then
        REMOTE_GIT_CMD+=" && git checkout -q -f -B $(printf '%q' "$REAL_BRANCH") $(printf '%q' "$REMOTE_HEAD_REF")"
    else
        REMOTE_GIT_CMD+=" && git checkout -q -f $(printf '%q' "$REMOTE_HEAD_REF")"
    fi
    REMOTE_GIT_CMD+=" && git update-ref -d $(printf '%q' "$REMOTE_HEAD_REF")"
    REMOTE_GIT_CMD+=" && rm -f $(printf '%q' "$REMOTE_BUNDLE_PATH")"
    if ! ssh "$REMOTE_HOST" "$REMOTE_GIT_CMD"; then
        GIT_INIT_OK=0
    fi
fi
METRICS_ACTIVE_PHASE=""
if [ "$GIT_INIT_OK" -ne 1 ]; then
    _metrics_record remote-git-init transfer failure "$METRICS_PHASE_STARTED" 1
    echo "[remote-test] could not materialize a self-contained git identity on $REMOTE_HOST"
    echo "[remote-test] DONE rc=1"
    exit 1
fi
_metrics_record remote-git-init transfer success "$METRICS_PHASE_STARTED" ""

# --delete keeps the remote copy an exact mirror; --exclude-from applies the
# gitignore-derived privacy list, --from0 reading it the same NUL-delimited
# way _remote_exclude_list.py wrote it (see above). The inline excludes are
# belt-and-suspenders for heavy build artifacts that aren't necessarily
# gitignored (node_modules, .gstack, stray *.pyc). --exclude .git: the
# remote's own git identity, materialized above, is never overwritten or
# deleted by this content-only sync.
METRICS_ACTIVE_PHASE="remote-transfer"; METRICS_ACTIVE_KIND="transfer"; METRICS_PHASE_STARTED="$(_metrics_now)"
rsync -a --delete --from0 --exclude-from="$EXCLUDE_FILE" \
    --exclude='.git' \
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
METRICS_ACTIVE_PHASE=""

if [ "$RSYNC_RC" -ne 0 ]; then
    _metrics_record remote-transfer transfer failure "$METRICS_PHASE_STARTED" "$RSYNC_RC"
    echo "[remote-test] rsync failed (is $REMOTE_HOST reachable? try: tailscale status)"
    echo "[remote-test] DONE rc=$RSYNC_RC"
    exit "$RSYNC_RC"
fi
_metrics_record remote-transfer transfer success "$METRICS_PHASE_STARTED" ""

echo "[remote-test] running ./scripts/test.sh ${TEST_ARGS[*]} on $REMOTE_HOST..."
echo "----------------------------------------------------------------------"

# Build the remote command with the dir and every arg shell-quoted, so a branch
# name or test arg can never break out of the ssh command string. Passing
# `candidate` uses test.sh's exact same verifier entry point after transfer;
# no local receipt is copied or trusted across hosts — the ONLY thing handed
# across is the opaque run_id itself (never a payload, env dump, or secret),
# inline in the quoted command so it doesn't depend on the remote sshd's
# AcceptEnv/SendEnv configuration, purely so the remote verify_candidate.py
# invocation's own real queue/execution records share this run's identity
# and can be correlated back afterward (see _correlate_remote_receipts).
REMOTE_CMD="cd $(printf '%q' "$REMOTE_DIR")"
if [ -n "$METRICS_RUN_ID" ]; then
    REMOTE_CMD+=" && LIFEOS_DEV_METRICS_RUN_ID=$(printf '%q' "$METRICS_RUN_ID") ./scripts/test.sh"
else
    REMOTE_CMD+=" && ./scripts/test.sh"
fi
for arg in "${TEST_ARGS[@]}"; do
    REMOTE_CMD+=" $(printf '%q' "$arg")"
done
# This is this wrapper's own local envelope measurement — the wall time of
# the whole SSH dispatch, from its own vantage point, not a claim about what
# fraction of that was the remote host's internal capacity queue versus its
# actual test execution. _correlate_remote_receipts below adds that real
# breakdown, sourced from the remote side itself, when available.
METRICS_ACTIVE_PHASE="remote-execution"; METRICS_ACTIVE_KIND="execution"; METRICS_PHASE_STARTED="$(_metrics_now)"
ssh "$REMOTE_HOST" "$REMOTE_CMD"
TEST_RC=$?
METRICS_ACTIVE_PHASE=""

echo "----------------------------------------------------------------------"
if [ "$TEST_RC" -eq 0 ]; then
    _metrics_record remote-execution execution success "$METRICS_PHASE_STARTED" ""
    _correlate_remote_receipts
    echo "[remote-test] DONE rc=0 (tests passed)"
else
    _metrics_record remote-execution execution failure "$METRICS_PHASE_STARTED" "$TEST_RC"
    _correlate_remote_receipts
    echo "[remote-test] DONE rc=$TEST_RC (tests failed — see output above)"
fi
exit "$TEST_RC"
