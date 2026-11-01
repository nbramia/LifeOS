#!/usr/bin/env bash
#
# Merge a pull request through the hosted candidate gate.
#
# Usage: ./scripts/merge-pr.sh <pr-number> [<head-sha>]
#
# `main` requires the `candidate-verification` check from a dedicated GitHub
# App, which is only ever published on a candidate merge commit, so
# `gh pr merge` cannot satisfy it. This wraps scripts/candidate_publisher.py,
# which builds that commit, has it verified, and publishes exactly that commit.
#
# The publisher comes from a freshly fetched origin/main, never the checkout, so
# a stale clone or a feature branch cannot change how a merge is published. It
# needs only git, gh, and the Python standard library, so no venv is required.
# The PR head is pinned to <head-sha>, or to whatever it is when this starts: any
# other head makes the publish refuse rather than merge something else.

set -euo pipefail

REPOSITORY="nbramia/LifeOS"
WORKFLOW_FILE="candidate-verification.yml"
CHECK_NAME="candidate-verification"
TRUSTED_APP_ID="4891159"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || ! [[ "$1" =~ ^[0-9]+$ ]] \
    || { [ "$#" -eq 2 ] && ! [[ "$2" =~ ^[0-9a-f]{40}$ ]]; }; then
    echo "Usage: $0 <pr-number> [<head-sha>]" >&2
    exit 2
fi
PR_NUMBER="$1"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

HEAD_SHA="${2:-$(gh pr view "$PR_NUMBER" --repo "$REPOSITORY" --json headRefOid --jq .headRefOid)}"
if [ -z "$HEAD_SHA" ]; then
    echo "Could not read the head commit of PR #$PR_NUMBER." >&2
    exit 1
fi

git fetch --quiet origin main

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
git show origin/main:scripts/candidate_publisher.py > "$WORK_DIR/candidate_publisher.py"

python3 -I "$WORK_DIR/candidate_publisher.py" \
    --repository "$REPOSITORY" \
    --pr "$PR_NUMBER" \
    --repo-path "$PROJECT_ROOT" \
    --workflow-file "$WORKFLOW_FILE" \
    --check-name "$CHECK_NAME" \
    --trusted-app-id "$TRUSTED_APP_ID" \
    --expected-head-sha "$HEAD_SHA"
