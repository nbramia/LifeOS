#!/usr/bin/env bash
#
# LifeOS Git Hooks Setup
#
# Configures this clone's git plumbing so the tracked hooks run and pushes
# don't hang:
#
#   1. `core.hooksPath = scripts` -- git runs the tracked scripts/pre-push,
#      scripts/pre-commit, scripts/post-commit directly. No copying into
#      .git/hooks (a copy there is never executed).
#   2. A separate HTTPS push URL on `origin`, derived from its SSH fetch URL.
#      The pre-push gate takes ~9 minutes; GitHub closes an idle SSH session
#      after ~6, so an SSH push can fail with "Connection closed by remote
#      host" even after every test passed (#886). Fetches stay SSH; only the
#      push URL changes. Idempotent -- a non-GitHub or already-HTTPS origin
#      is left alone.
#   3. A check that the `gh` credential helper is wired up for HTTPS pushes,
#      since that's what makes step 2 work without a password prompt.
#
# Usage: ./scripts/setup-hooks.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Configuring git hooks..."
git config core.hooksPath scripts
echo "  core.hooksPath = scripts"

FETCH_URL="$(git remote get-url origin 2>/dev/null || true)"
if [ -z "$FETCH_URL" ]; then
    echo "No 'origin' remote found -- skipping push transport setup."
    exit 0
fi

# Derive https://github.com/<owner>/<repo>.git from an SSH GitHub fetch URL
# (git@github.com:<owner>/<repo>.git). Leave everything else alone: an
# https:// origin already avoids the idle-SSH problem, and a non-GitHub
# remote isn't something we can safely rewrite.
case "$FETCH_URL" in
    git@github.com:*)
        REPO_PATH="${FETCH_URL#git@github.com:}"
        HTTPS_URL="https://github.com/${REPO_PATH}"
        CURRENT_PUSH_URL="$(git remote get-url --push origin 2>/dev/null || true)"
        if [ "$CURRENT_PUSH_URL" = "$HTTPS_URL" ]; then
            echo "Push URL already set to $HTTPS_URL"
        else
            git remote set-url --push origin "$HTTPS_URL"
            echo "Set push URL to $HTTPS_URL (fetch stays $FETCH_URL)"
        fi
        ;;
    https://github.com/*)
        echo "origin already uses HTTPS ($FETCH_URL) -- leaving push URL alone."
        ;;
    *)
        echo "origin is not a github.com SSH remote ($FETCH_URL) -- leaving push URL alone."
        ;;
esac

# The HTTPS push above relies on gh's own credential helper. Point at the
# fix rather than failing setup if it isn't configured yet.
if ! git config --get-all 'credential.https://github.com.helper' 2>/dev/null | grep -q "gh auth git-credential"; then
    echo ""
    echo "NOTE: the 'gh' credential helper for github.com is not configured."
    echo "  HTTPS pushes will prompt for a password/token until you run:"
    echo "    gh auth setup-git"
fi

echo "Done."
