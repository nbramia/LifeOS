#!/bin/bash
# LifeOS launchd environment wrapper.
#
# A launchd `EnvironmentVariables` dict is static at load time — unlike
# systemd's `EnvironmentFile=`, a launchd-managed service never re-reads a
# live .env, and it does not source the operator's shell startup file either.
# Neither `~/.zshrc` nor `launchctl setenv` reliably reaches a launchd
# service's process environment. Without this wrapper, required config
# (e.g. ANTHROPIC_API_KEY, tokens the agent worker strips before spawning a
# CLI subprocess — see claude_code_executor.py's `_clean_env`) never reaches
# a launchd-managed service at all (#776).
#
# Every generated plist's ProgramArguments routes through this script first.
# It loads the project's .env (if present) into its own process environment,
# then execs the real command — the same ".env reaches the process
# environment at start" behavior a systemd service already gets via
# EnvironmentFile=.
#
# Deliberately NOT `source .env`: systemd's EnvironmentFile= is a plain
# KEY=VALUE parser — it never evaluates the value as shell — so `source`ing
# would be a meaningfully different (and riskier) analog: a value containing
# `$(...)`, backticks, or other shell metacharacters would be executed as
# code every time launchd starts this service, not treated as a literal
# string. The loop below reads .env line by line and exports each KEY=VALUE
# literally, stripping one layer of matching quotes — no shell evaluation of
# the value, matching EnvironmentFile='s actual semantics.
#
# Usage: launchd-env-wrapper.sh <project_dir> <command> [args...]
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: launchd-env-wrapper.sh <project_dir> <command> [args...]" >&2
    exit 1
fi

PROJECT_DIR="$1"
shift

if [ -f "$PROJECT_DIR/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
        esac
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            # `${val:1:-1}` (negative length) needs bash 4.2+; macOS ships
            # bash 3.2 as /bin/bash, so use %/# trimming instead, which
            # works on every bash version.
            if [[ "$val" == \"*\" && "$val" == *\" && ${#val} -ge 2 ]]; then
                val="${val#\"}"
                val="${val%\"}"
            elif [[ "$val" == \'*\' && "$val" == *\' && ${#val} -ge 2 ]]; then
                val="${val#\'}"
                val="${val%\'}"
            fi
            # Only export a key not already present in the inherited
            # environment — found on review: exporting unconditionally let
            # a stale/incorrect .env value silently override one launchd
            # already set via the plist's own EnvironmentVariables dict
            # (e.g. LIFEOS_VAULT_PATH, validated at install time by
            # setup-launchd.sh's check_paths_exist()). This matches
            # systemd's EnvironmentFile=, which likewise never overrides a
            # variable already set at the [Service] level (Environment=).
            # `${!key+x}` (indirect expansion) tests whether $key is set at
            # all, regardless of whether its value is empty.
            if [ -z "${!key+x}" ]; then
                export "$key=$val"
            fi
        fi
    done < "$PROJECT_DIR/.env"
fi

exec "$@"
