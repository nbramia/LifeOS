#!/usr/bin/env bash
# Cross-machine CLI session lifecycle hook (#849) — supersedes
# claude-session-pane.sh and codex-session-pane.sh (kept in place; their
# operator configs may still point at them, but new installs should use
# this script instead — see scripts/install-agent-hooks.sh).
#
# Registers a Claude Code or Codex session with LifeOS from ANY machine —
# not just the one hosting the API — so it shows up on /agents with its
# host, branch, and current status. One script serves every hook event;
# the engine and event names are passed as args:
#
#   lifeos-agent-hook.sh claude_code session_start
#   lifeos-agent-hook.sh claude_code user_prompt_submit
#   lifeos-agent-hook.sh claude_code stop
#   lifeos-agent-hook.sh claude_code session_end
#   lifeos-agent-hook.sh codex session_start
#   lifeos-agent-hook.sh codex user_prompt_submit
#   lifeos-agent-hook.sh codex stop
#
# What it does: reads the hook's JSON payload from stdin
# (`{session_id, cwd, transcript_path?, source, prompt?}` — the exact
# fields present vary by event and by engine), adds the hostname, the
# cwd's git branch, and an optional task id from $LIFEOS_TASK_ID, and
# POSTs the result to POST /api/agents/cli-sessions/events with a bearer
# token. Unlike claude-session-pane.sh's /cc-pane-bind (localhost-only),
# this endpoint is reachable over Tailscale, so $LIFEOS_API_URL normally
# points at the box hosting the API, not localhost.
#
# Config: sources a small env file for LIFEOS_API_URL and
# LIFEOS_AGENT_HOOK_TOKEN, defaulting to ~/.config/lifeos/agent-hook.env
# (override with $LIFEOS_AGENT_HOOK_ENV). Values already set in the
# environment take precedence over the file. This script never writes the
# token itself — see scripts/install-agent-hooks.sh for the one-time setup
# instructions it prints.
#
# Exits 0 silently in every non-fatal case: jq/curl missing, empty stdin,
# no session_id in the payload, no token configured (env or file), API
# unreachable. Never blocks the CLI; total wall time on the happy path is
# one HTTP round trip capped at 2s. Never writes to stdout — a hook's
# stdout may be interpreted by the CLI.
#
# WezTerm pane binding is optional, not required: pane_id/wezterm_pid are
# included only when $WEZTERM_PANE is set. Unlike the scripts this
# replaces, running outside WezTerm is a normal case, not a silent exit —
# the session still registers, just without a pane to jump to.
#
# Portable to bash 3.2 (macOS's shipped bash) as well as Linux bash: no
# mapfile, no `${var,,}`, no associative arrays, no GNU-only flags.

set -u

ENGINE="${1:-}"
EVENT="${2:-}"

if [[ -z "$ENGINE" || -z "$EVENT" ]]; then
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
    exit 0
fi

# Config file: LIFEOS_API_URL / LIFEOS_AGENT_HOOK_TOKEN. Capture whatever
# the environment already set first so sourcing the file never overrides
# an explicit env value — the file only fills in what's missing.
_PRE_API_URL="${LIFEOS_API_URL:-}"
_PRE_TOKEN="${LIFEOS_AGENT_HOOK_TOKEN:-}"
ENV_FILE="${LIFEOS_AGENT_HOOK_ENV:-$HOME/.config/lifeos/agent-hook.env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi
if [[ -n "$_PRE_API_URL" ]]; then
    LIFEOS_API_URL="$_PRE_API_URL"
fi
if [[ -n "$_PRE_TOKEN" ]]; then
    LIFEOS_AGENT_HOOK_TOKEN="$_PRE_TOKEN"
fi

TOKEN="${LIFEOS_AGENT_HOOK_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
    exit 0
fi

LIFEOS_URL="${LIFEOS_API_URL:-http://localhost:8000}"

# Buffer stdin once — every extraction below reads from it.
PAYLOAD="$(cat 2>/dev/null || true)"
if [[ -z "$PAYLOAD" ]]; then
    exit 0
fi

SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null)"
if [[ -z "$SESSION_ID" ]]; then
    exit 0
fi
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$PAYLOAD" | jq -r '.transcript_path // empty' 2>/dev/null)"
PROMPT="$(printf '%s' "$PAYLOAD" | jq -r '.prompt // empty' 2>/dev/null)"
MODEL="$(printf '%s' "$PAYLOAD" | jq -r '.model // empty' 2>/dev/null)"

# Hostname without domain suffix — never rely on `hostname -s`, which
# isn't portable across Linux and macOS `hostname` implementations.
HOST_NAME="$(hostname 2>/dev/null || true)"
HOST_NAME="${HOST_NAME%%.*}"

BRANCH=""
if [[ -n "$CWD" ]]; then
    BRANCH="$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi

TASK_ID="${LIFEOS_TASK_ID:-}"

JQ_ARGS=(
    --arg engine "$ENGINE"
    --arg event "$EVENT"
    --arg session_id "$SESSION_ID"
    --arg host "$HOST_NAME"
    --arg cwd "$CWD"
    --arg transcript_path "$TRANSCRIPT_PATH"
    --arg branch "$BRANCH"
    --arg model "$MODEL"
    --arg prompt_preview "$PROMPT"
    --arg task_id "$TASK_ID"
)
JQ_FILTER='{engine:$engine, event:$event, session_id:$session_id, host:$host, cwd:$cwd, transcript_path:$transcript_path, branch:$branch, model:$model, prompt_preview:$prompt_preview, task_id:$task_id}'

# A non-numeric $WEZTERM_PANE would make `jq --argjson pane_id` fail,
# emptying $BODY and silently dropping the whole event below — not just
# the pane fields. Guard it the same way $pid/$mtime are guarded so a
# malformed value just omits the pane fields instead.
case "${WEZTERM_PANE:-}" in
    ''|*[!0-9]*) ;;
    *)
    # Best-effort: the pid of the live wezterm-gui process this pane
    # belongs to, found the same way the API's own `_current_wezterm_pid`
    # does (newest live `gui-sock-*` socket under the wezterm runtime
    # dir) — kept in sync deliberately so a local event's pane mapping
    # matches what /cc-pane-bind would have written.
    RTDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    SOCKDIR="$RTDIR/wezterm"
    NEWEST_PID=""
    NEWEST_MTIME=0
    if [[ -d "$SOCKDIR" ]]; then
        for f in "$SOCKDIR"/gui-sock-*; do
            [[ -e "$f" ]] || continue
            pid="${f##*gui-sock-}"
            case "$pid" in
                ''|*[!0-9]*) continue ;;
            esac
            kill -0 "$pid" 2>/dev/null || continue
            mtime="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)"
            case "$mtime" in
                ''|*[!0-9]*) mtime=0 ;;
            esac
            if [[ "$mtime" -gt "$NEWEST_MTIME" ]]; then
                NEWEST_MTIME="$mtime"
                NEWEST_PID="$pid"
            fi
        done
    fi
    JQ_ARGS+=(--argjson pane_id "$WEZTERM_PANE")
    if [[ -n "$NEWEST_PID" ]]; then
        JQ_ARGS+=(--argjson wezterm_pid "$NEWEST_PID")
        JQ_FILTER="$JQ_FILTER"' + {pane_id:$pane_id, wezterm_pid:$wezterm_pid}'
    else
        JQ_FILTER="$JQ_FILTER"' + {pane_id:$pane_id}'
    fi
    ;;
esac

BODY="$(jq -nc "${JQ_ARGS[@]}" "$JQ_FILTER" 2>/dev/null)"
if [[ -z "$BODY" ]]; then
    exit 0
fi

curl -fsS --max-time 2 \
    -X POST "${LIFEOS_URL}/api/agents/cli-sessions/events" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -d "$BODY" \
    >/dev/null 2>&1 || true

exit 0
