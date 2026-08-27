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
# It sources the project's .env (if present) into its own process
# environment, then execs the real command — the same ".env reaches the
# process environment at start" behavior a systemd service already gets via
# EnvironmentFile=.
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
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

exec "$@"
