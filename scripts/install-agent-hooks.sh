#!/usr/bin/env bash
# Idempotently install scripts/lifeos-agent-hook.sh into a Claude Code
# settings.json and a Codex hooks.json (#849), so this machine's CLI
# sessions register themselves with LifeOS on /agents.
#
# Usage: scripts/install-agent-hooks.sh
#
# Targets (override for testing against temp copies — never point these
# at anything but a scratch file outside a real run):
#   LIFEOS_CLAUDE_SETTINGS   default: ~/.claude/settings.json
#   LIFEOS_CODEX_HOOKS       default: ~/.codex/hooks.json
#
# What it does: for each event Claude Code and Codex support
# (claude_code: SessionStart, UserPromptSubmit, Stop, SessionEnd; codex:
# SessionStart, UserPromptSubmit, Stop — Codex has no SessionEnd hook),
# appends one entry that runs lifeos-agent-hook.sh with that event's args,
# UNLESS an entry already exists for that event whose command contains
# "lifeos-agent-hook.sh" (identifies a prior run of this installer, so
# running it again is a no-op). Every other tool's entries — Orca, atuin,
# a legacy claude-session-pane.sh / codex-session-pane.sh entry — are left
# exactly as they were. Creates the target file (and its `hooks` object)
# if missing.
#
# The installed command is the absolute path of lifeos-agent-hook.sh in
# THIS checkout, resolved from this installer's own location, wrapped so
# a moved or deleted checkout never breaks the CLI:
#   bash -c 's="<path>"; [ -x "$s" ] && exec "$s" <engine> <event>; exit 0'
#
# Does NOT write LIFEOS_AGENT_HOOK_TOKEN anywhere — prints instructions
# for the operator to create the token/URL env file
# scripts/lifeos-agent-hook.sh reads.
#
# Portable to bash 3.2 (macOS's shipped bash) as well as Linux bash. No
# mapfile, no `${var,,}`, no associative arrays, no GNU-only flags.
# Requires jq (same dependency as the hook script itself).

set -eu

if ! command -v jq >/dev/null 2>&1; then
    echo "install-agent-hooks.sh: jq is required" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SCRIPT="$SCRIPT_DIR/lifeos-agent-hook.sh"

CLAUDE_SETTINGS="${LIFEOS_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CODEX_HOOKS="${LIFEOS_CODEX_HOOKS:-$HOME/.codex/hooks.json}"

# Appends one hook entry to the `.hooks` object of the JSON file at $1,
# under hook key $2 (e.g. "SessionStart" — the CLI's own event name),
# running lifeos-agent-hook.sh with args ($3 engine, $4 hook-script event
# name) under matcher $5 — unless an entry already exists under that hook
# key whose `.hooks[].command` contains "lifeos-agent-hook.sh".
_install_event() {
    file="$1"; hook_key="$2"; engine="$3"; hook_event="$4"; matcher="$5"

    mkdir -p "$(dirname "$file")"
    if [[ ! -f "$file" ]]; then
        echo '{}' > "$file"
    fi

    already="$(jq --arg ev "$hook_key" '
        ((.hooks // {})[$ev] // [])
        | map((.hooks // []) | map(.command // "") | any(contains("lifeos-agent-hook.sh")))
        | any
    ' "$file")"

    if [[ "$already" == "true" ]]; then
        echo "  $engine $hook_key: already installed"
        return 0
    fi

    cmd="bash -c 's=\"$HOOK_SCRIPT\"; [ -x \"\$s\" ] && exec \"\$s\" $engine $hook_event; exit 0'"

    tmp="$(mktemp "${file}.XXXXXX")"
    jq --arg ev "$hook_key" --arg matcher "$matcher" --arg cmd "$cmd" '
        .hooks = (.hooks // {})
        | .hooks[$ev] = ((.hooks[$ev] // []) + [{
              matcher: $matcher,
              hooks: [{type: "command", command: $cmd}]
          }])
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
    echo "  $engine $hook_key: installed"
}

echo "Claude Code ($CLAUDE_SETTINGS):"
_install_event "$CLAUDE_SETTINGS" "SessionStart" "claude_code" "session_start" "*"
_install_event "$CLAUDE_SETTINGS" "UserPromptSubmit" "claude_code" "user_prompt_submit" "*"
_install_event "$CLAUDE_SETTINGS" "Stop" "claude_code" "stop" "*"
_install_event "$CLAUDE_SETTINGS" "SessionEnd" "claude_code" "session_end" "*"

echo "Codex ($CODEX_HOOKS):"
_install_event "$CODEX_HOOKS" "SessionStart" "codex" "session_start" "startup|resume"
_install_event "$CODEX_HOOKS" "UserPromptSubmit" "codex" "user_prompt_submit" "*"
_install_event "$CODEX_HOOKS" "Stop" "codex" "stop" "*"

echo
echo "Session registration needs a bearer token before it will post anywhere."
echo "On the machine hosting the LifeOS API, set LIFEOS_AGENT_HOOK_TOKEN (e.g."
echo "openssl rand -hex 32) and restart the API. Then on THIS machine, create:"
echo
echo "  \${LIFEOS_AGENT_HOOK_ENV:-~/.config/lifeos/agent-hook.env}"
echo
echo "containing:"
echo
echo "  LIFEOS_API_URL=http://<api-host>:8000"
echo "  LIFEOS_AGENT_HOOK_TOKEN=<the same token>"
echo
echo "Until that file (or the equivalent environment variables) exists,"
echo "lifeos-agent-hook.sh exits silently without posting anything."
