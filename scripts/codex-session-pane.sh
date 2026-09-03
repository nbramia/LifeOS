#!/usr/bin/env bash
# Codex SessionStart hook → bind session_id → wezterm pane_id.
#
# Superseded by scripts/lifeos-agent-hook.sh (#849), which registers a
# session from ANY machine (not just the one hosting the API) and covers
# every hook event, not just SessionStart. Kept here unchanged for
# existing operator configs that still point at it; new installs should
# use scripts/install-agent-hooks.sh instead.
#
# Sibling of claude-session-pane.sh. Install by adding this file as a
# SessionStart hook in ~/.codex/hooks.json:
#
#   {
#     "hooks": {
#       "SessionStart": [{
#         "matcher": "startup|resume",
#         "hooks": [{
#           "type": "command",
#           "command": "/path/to/LifeOS/scripts/codex-session-pane.sh"
#         }]
#       }]
#     }
#   }
#
# What it does: every time a `codex` invocation starts inside a wezterm
# pane, this hook reads the SessionStart payload from stdin
# (`{session_id, cwd, source, ...}`), picks up the pane id from
# $WEZTERM_PANE, and POSTs both to /api/agents/cx-pane-bind so the
# /agents Focus / Go To button can jump straight to the right pane.
#
# Exits 0 silently in all non-fatal cases:
#   - Not running under wezterm ($WEZTERM_PANE unset)
#   - LifeOS API unreachable (server stopped, hook running before boot)
#   - `jq` not installed (won't parse stdin)
#   - curl missing
#
# Never blocks codex startup; total wall time on the happy path is one
# localhost HTTP round-trip (~10ms).

set -u

# Only act inside wezterm — other terminals don't have a pane id to bind.
if [[ -z "${WEZTERM_PANE:-}" ]]; then
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
    exit 0
fi

PAYLOAD="$(cat 2>/dev/null || true)"
if [[ -z "$PAYLOAD" ]]; then
    exit 0
fi

SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null)"
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null)"

if [[ -z "$SESSION_ID" ]]; then
    exit 0
fi

LIFEOS_URL="${LIFEOS_API_URL:-http://localhost:8000}"

curl -fsS --max-time 2 \
    -X POST "${LIFEOS_URL}/api/agents/cx-pane-bind" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc \
        --arg sid "$SESSION_ID" \
        --argjson pid "$WEZTERM_PANE" \
        --arg cwd "$CWD" \
        '{session_id: $sid, pane_id: $pid, cwd: $cwd}')" \
    >/dev/null 2>&1 || true

exit 0
