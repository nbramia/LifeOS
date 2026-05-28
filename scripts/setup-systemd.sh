#!/bin/bash
# Install LifeOS systemd unit files and enable services.
#
# Usage: sudo ./scripts/setup-systemd.sh
#
# Template variables are substituted at install time:
#   __USER__          → current $SUDO_USER (the user who ran sudo)
#   __LIFEOS_DIR__    → project directory (auto-detected)
#   __VENV__          → venv path (default: ~/.venvs/lifeos)
#   __LLAMA_CPP_DIR__ → llama.cpp directory (default: ~/llama.cpp)
#   __LLM_SOURCE_ARGS__       → either `-hf <repo>` (default) or
#                               `-m <gguf> --mmproj <mmproj>` when
#                               LIFEOS_LLM_MODEL_PATH is set (local override
#                               for when the HuggingFace cache is stale and
#                               the upstream model has been updated — see
#                               docs/guides/agent-worker-setup.md).
#   __LLM_RESTART_POLICY__    → "on-failure" or "no" (from LIFEOS_LOCAL_LLM_AUTOSTART)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SYSTEMD_SRC="$PROJECT_DIR/config/systemd"
SYSTEMD_DST="/etc/systemd/system"

if [[ "$(uname)" != "Linux" ]]; then
    echo "This script is for Linux only."
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)."
    exit 1
fi

# Resolve the actual user (not root)
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(eval echo "~$REAL_USER")
VENV_DIR="${LIFEOS_VENV:-$REAL_HOME/.venvs/lifeos}"
LLAMA_DIR="${LIFEOS_LLAMA_DIR:-$REAL_HOME/llama.cpp}"

# Read settings from .env (falls back to defaults if not set)
_read_env() {
    local key="$1" default="$2"
    if [ -f "$PROJECT_DIR/.env" ]; then
        local val
        val=$(grep -E "^${key}=" "$PROJECT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//;s/ *#.*//" | tr -d '[:space:]')
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}

LLM_MODEL=$(_read_env "LIFEOS_LLM_MODEL" "${LIFEOS_LLM_MODEL:-unsloth/gemma-4-26B-A4B-it-GGUF}")
LLM_MODEL_PATH=$(_read_env "LIFEOS_LLM_MODEL_PATH" "")
LLM_MMPROJ_PATH=$(_read_env "LIFEOS_LLM_MMPROJ_PATH" "")
LLM_AUTOSTART=$(_read_env "LIFEOS_LOCAL_LLM_AUTOSTART" "false")
MCP_BEARER_TOKEN=$(_read_env "LIFEOS_MCP_BEARER_TOKEN" "")
AGENT_WORKER_AUTOSTART=$(_read_env "LIFEOS_AGENT_WORKER_AUTOSTART" "false")

# Normalize boolean
case "$(echo "$LLM_AUTOSTART" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) LLM_AUTOSTART="true" ;;
    *)          LLM_AUTOSTART="false" ;;
esac

case "$(echo "$AGENT_WORKER_AUTOSTART" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) AGENT_WORKER_AUTOSTART="true" ;;
    *)          AGENT_WORKER_AUTOSTART="false" ;;
esac

if [ "$LLM_AUTOSTART" = "true" ]; then
    LLM_RESTART_POLICY="on-failure"
else
    LLM_RESTART_POLICY="no"
fi

# Decide how llama-server gets pointed at the model. Default: `-hf <repo>`,
# letting llama.cpp manage download + cache. Override: `-m <gguf>` (with
# optional `--mmproj`) when LIFEOS_LLM_MODEL_PATH is set. The override is
# needed when an unsloth/HF model has been updated upstream but the local
# cache still has the old sha256 — `-hf` will then 404 on redownload and
# fall through to router mode with no model loaded.
if [ -n "$LLM_MODEL_PATH" ]; then
    if [ ! -f "$LLM_MODEL_PATH" ]; then
        echo "  WARNING: LIFEOS_LLM_MODEL_PATH=$LLM_MODEL_PATH does not exist"
    fi
    LLM_SOURCE_ARGS="-m $LLM_MODEL_PATH"
    if [ -n "$LLM_MMPROJ_PATH" ]; then
        if [ ! -f "$LLM_MMPROJ_PATH" ]; then
            echo "  WARNING: LIFEOS_LLM_MMPROJ_PATH=$LLM_MMPROJ_PATH does not exist"
        fi
        LLM_SOURCE_ARGS="$LLM_SOURCE_ARGS --mmproj $LLM_MMPROJ_PATH"
    fi
    LLM_SOURCE_DISPLAY="$LLM_MODEL_PATH (local file)"
else
    LLM_SOURCE_ARGS="-hf $LLM_MODEL"
    LLM_SOURCE_DISPLAY="$LLM_MODEL (HuggingFace)"
fi

echo "=== LifeOS systemd Setup ==="
echo ""
echo "  User:       $REAL_USER"
echo "  Project:    $PROJECT_DIR"
echo "  Venv:       $VENV_DIR"
echo "  llama.cpp:  $LLAMA_DIR"
echo "  LLM Model:  $LLM_SOURCE_DISPLAY"
echo "  LLM Auto:   $LLM_AUTOSTART (restart policy: $LLM_RESTART_POLICY)"
if [ -n "$MCP_BEARER_TOKEN" ]; then
    echo "  MCP HTTP:   enabled (token configured)"
else
    echo "  MCP HTTP:   disabled (set LIFEOS_MCP_BEARER_TOKEN to enable)"
fi
echo "  Agent Worker: $AGENT_WORKER_AUTOSTART"
echo ""

# Install unit files with variable substitution
echo "Installing unit files to $SYSTEMD_DST..."
for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
    [ -f "$unit" ] || continue
    name=$(basename "$unit")
    sed \
        -e "s|__USER__|$REAL_USER|g" \
        -e "s|__LIFEOS_DIR__|$PROJECT_DIR|g" \
        -e "s|__VENV__|$VENV_DIR|g" \
        -e "s|__LLAMA_CPP_DIR__|$LLAMA_DIR|g" \
        -e "s|__LLM_MODEL__|$LLM_MODEL|g" \
        -e "s|__LLM_SOURCE_ARGS__|$LLM_SOURCE_ARGS|g" \
        -e "s|__LLM_RESTART_POLICY__|$LLM_RESTART_POLICY|g" \
        "$unit" > "$SYSTEMD_DST/$name"
    echo "  Installed $name"
done

# Reload systemd
echo ""
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable and start services (order matters)
echo ""
echo "Enabling and starting services..."

if [ "$LLM_AUTOSTART" = "true" ]; then
    systemctl enable --now lifeos-llm.service
    echo "  lifeos-llm: $(systemctl is-active lifeos-llm.service) (autostart enabled)"
else
    systemctl disable lifeos-llm.service 2>/dev/null || true
    systemctl stop lifeos-llm.service 2>/dev/null || true
    echo "  lifeos-llm: disabled (set LIFEOS_LOCAL_LLM_AUTOSTART=true to enable)"
fi

systemctl enable --now lifeos-chromadb.service
echo "  lifeos-chromadb: $(systemctl is-active lifeos-chromadb.service)"

systemctl enable --now lifeos-api.service
echo "  lifeos-api: $(systemctl is-active lifeos-api.service)"

systemctl enable --now lifeos-watchdog.timer
echo "  lifeos-watchdog.timer: $(systemctl is-active lifeos-watchdog.timer)"

systemctl enable --now lifeos-server-watchdog.timer
echo "  lifeos-server-watchdog.timer: $(systemctl is-active lifeos-server-watchdog.timer)"

systemctl enable --now lifeos-gpu-watchdog.timer
echo "  lifeos-gpu-watchdog.timer: $(systemctl is-active lifeos-gpu-watchdog.timer)"

systemctl enable --now lifeos-sync.timer
echo "  lifeos-sync.timer: $(systemctl is-active lifeos-sync.timer)"

# MCP HTTP transport is only enabled when a bearer token is configured.
# The systemd unit reads the live .env at runtime; we check the token here
# purely to decide whether to enable/start the unit at install time. Without
# a token, exposing the MCP server over HTTP would let any caller hit the
# agent worker's tool surface — see docs/guides/agent-worker-setup.md.
if [ -n "$MCP_BEARER_TOKEN" ]; then
    systemctl enable --now lifeos-mcp-http.service
    echo "  lifeos-mcp-http: $(systemctl is-active lifeos-mcp-http.service)"
else
    systemctl disable lifeos-mcp-http.service 2>/dev/null || true
    systemctl stop lifeos-mcp-http.service 2>/dev/null || true
    echo "  lifeos-mcp-http: disabled (set LIFEOS_MCP_BEARER_TOKEN to enable)"
fi

# Agent worker is opt-in via LIFEOS_AGENT_WORKER_AUTOSTART. Off by default
# so fresh clones don't start polling the task list with no preflight call
# wired up. Issue B installs the no-op dispatcher; later issues add real
# execution.
if [ "$AGENT_WORKER_AUTOSTART" = "true" ]; then
    systemctl enable --now lifeos-agent-worker.service
    echo "  lifeos-agent-worker: $(systemctl is-active lifeos-agent-worker.service)"
else
    systemctl disable lifeos-agent-worker.service 2>/dev/null || true
    systemctl stop lifeos-agent-worker.service 2>/dev/null || true
    echo "  lifeos-agent-worker: disabled (set LIFEOS_AGENT_WORKER_AUTOSTART=true to enable)"
fi

# Install logrotate config with substitution
LOGROTATE_SRC="$PROJECT_DIR/config/logrotate-lifeos.conf"
if [ -f "$LOGROTATE_SRC" ]; then
    echo ""
    echo "Installing logrotate config..."
    sed "s|__LIFEOS_DIR__|$PROJECT_DIR|g" "$LOGROTATE_SRC" > /etc/logrotate.d/lifeos
    echo "  Installed /etc/logrotate.d/lifeos"
fi

# Install sudoers rule so server.sh and sync scripts can manage services without a password
echo ""
echo "Installing sudoers rule for passwordless systemctl..."
SUDOERS_FILE="/etc/sudoers.d/lifeos"
TMP_SUDOERS=$(mktemp)
# Build the NOPASSWD command list programmatically. Each unit gets
# start/stop/reset-failed (in both name and name.service forms); units
# that should also restart get restart entries too. `reset-failed` is
# required to recover units that have tripped systemd's StartLimit (e.g.
# the agent worker getting cascade-restarted past the rate limit during
# a dev session); without it, plain `restart` can't break out of the
# failed state. lifeos-llm intentionally lacks `restart` because of GPU
# memory cleanup concerns — operator uses stop + start instead.
_sudo_cmds=()
for unit in lifeos-api lifeos-mcp-http lifeos-agent-worker; do
    for verb in start stop restart reset-failed; do
        _sudo_cmds+=("/usr/bin/systemctl $verb $unit" "/usr/bin/systemctl $verb $unit.service")
    done
done
for verb in start stop reset-failed; do
    _sudo_cmds+=("/usr/bin/systemctl $verb lifeos-llm" "/usr/bin/systemctl $verb lifeos-llm.service")
done
# Join with ", " for the sudoers Cmnd_Alias line
IFS=','
_sudo_csv="${_sudo_cmds[*]}"
unset IFS
_sudo_csv="${_sudo_csv//,/, }"
echo "$REAL_USER ALL=(root) NOPASSWD: $_sudo_csv" > "$TMP_SUDOERS"
if visudo -c -f "$TMP_SUDOERS" > /dev/null 2>&1; then
    mv "$TMP_SUDOERS" "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    echo "  Installed $SUDOERS_FILE"
else
    rm -f "$TMP_SUDOERS"
    echo "  ERROR: Invalid sudoers syntax — skipping installation"
fi

# Ensure swap is available as OOM safety net (idempotent)
SWAP_SIZE_GB=8
echo ""
if swapon --show --noheadings | grep -q .; then
    SWAP_INFO=$(swapon --show --noheadings | head -1)
    echo "Swap already active: $SWAP_INFO"
elif [ -f /swapfile ]; then
    swapon /swapfile 2>/dev/null && echo "Activated existing /swapfile" || echo "  /swapfile exists but could not be activated"
elif [ -f /swap.img ]; then
    swapon /swap.img 2>/dev/null && echo "Activated existing /swap.img" || echo "  /swap.img exists but could not be activated"
else
    echo "Creating ${SWAP_SIZE_GB}GB swap file as OOM safety net..."
    dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB * 1024)) status=progress
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -q "/swapfile" /etc/fstab; then
        echo "/swapfile none swap sw 0 0" >> /etc/fstab
    fi
    echo "  Swap enabled: ${SWAP_SIZE_GB}GB at /swapfile"
fi

# Show status
echo ""
echo "=== Service Status ==="
systemctl status lifeos-chromadb.service lifeos-api.service --no-pager -l 2>/dev/null || true

echo ""
echo "=== Timer Status ==="
systemctl list-timers lifeos-* --no-pager 2>/dev/null || true

echo ""
echo "Setup complete. Check health with: curl http://localhost:8000/health/full | jq"
