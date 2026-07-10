#!/usr/bin/env bash
# Claude Code SessionStart hook → bind session_id → wezterm pane_id.
#
# Install by adding this file as a SessionStart hook in ~/.claude/settings.json:
#
#   "hooks": {
#     "SessionStart": [{
#       "matcher": "*",
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/LifeOS/scripts/claude-session-pane.sh"
#       }]
#     }]
#   }
#
# What it does: every time a `claude` invocation starts inside a wezterm
# pane, this hook reads the SessionStart payload from stdin
# (`{session_id, cwd, transcript_path, source}`), picks up the pane id
# from $WEZTERM_PANE, and POSTs both to /api/agents/cc-pane-bind so the
# /agents Focus / Go To button can jump straight to the right pane.
#
# Exits 0 silently in all non-fatal cases:
#   - Not running under wezterm ($WEZTERM_PANE unset)
#   - LifeOS API unreachable (server stopped, hook running before boot)
#   - `jq` not installed (won't parse stdin)
#   - curl missing
#
# Never blocks claude startup; total wall time on the happy path is one
# localhost HTTP round-trip (~10ms).

set -u

# Only act inside wezterm — other terminals don't have a pane id to bind.
if [[ -z "${WEZTERM_PANE:-}" ]]; then
    exit 0
fi

# Need jq to parse the SessionStart JSON payload reliably.
if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
    exit 0
fi

# Buffer stdin once — both extractions read from it.
PAYLOAD="$(cat 2>/dev/null || true)"
if [[ -z "$PAYLOAD" ]]; then
    exit 0
fi

SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null)"
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null)"

if [[ -z "$SESSION_ID" ]]; then
    exit 0
fi

# Candidate API URLs, tried in order until one accepts the bind. localhost
# first so the common case — running ON the API host (e.g. nathan-linux) — is a
# single fast round-trip; fall back to $LIFEOS_API_URL when localhost is down.
# On the MacBook the API is remote, so localhost:8000 has no listener and the
# Tailscale $LIFEOS_API_URL (if provided to the hook) is what actually binds.
URLS=("http://localhost:8000")
if [[ -n "${LIFEOS_API_URL:-}" && "${LIFEOS_API_URL}" != "http://localhost:8000" ]]; then
    URLS+=("$LIFEOS_API_URL")
fi

BODY="$(jq -nc \
    --arg sid "$SESSION_ID" \
    --argjson pid "$WEZTERM_PANE" \
    --arg cwd "$CWD" \
    '{session_id: $sid, pane_id: $pid, cwd: $cwd}')"

# Fire-and-forget. Short timeout so a stalled/absent server never holds up the
# user's prompt. Stop at the first URL that accepts the POST.
for url in "${URLS[@]}"; do
    if curl -fsS --max-time 2 \
        -X POST "${url}/api/agents/cc-pane-bind" \
        -H "Content-Type: application/json" \
        -d "$BODY" \
        >/dev/null 2>&1; then
        break
    fi
done

exit 0
