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
#   __LLM_MODEL__     → HuggingFace model ID (default: ggml-org/gpt-oss-120b-GGUF)

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
LLM_MODEL="${LIFEOS_LLM_MODEL:-ggml-org/gpt-oss-120b-GGUF}"

echo "=== LifeOS systemd Setup ==="
echo ""
echo "  User:       $REAL_USER"
echo "  Project:    $PROJECT_DIR"
echo "  Venv:       $VENV_DIR"
echo "  llama.cpp:  $LLAMA_DIR"
echo "  LLM Model:  $LLM_MODEL"
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

systemctl enable --now lifeos-llm.service
echo "  lifeos-llm: $(systemctl is-active lifeos-llm.service)"

systemctl enable --now lifeos-chromadb.service
echo "  lifeos-chromadb: $(systemctl is-active lifeos-chromadb.service)"

systemctl enable --now lifeos-api.service
echo "  lifeos-api: $(systemctl is-active lifeos-api.service)"

systemctl enable --now lifeos-watchdog.timer
echo "  lifeos-watchdog.timer: $(systemctl is-active lifeos-watchdog.timer)"

systemctl enable --now lifeos-sync.timer
echo "  lifeos-sync.timer: $(systemctl is-active lifeos-sync.timer)"

# Install logrotate config with substitution
LOGROTATE_SRC="$PROJECT_DIR/config/logrotate-lifeos.conf"
if [ -f "$LOGROTATE_SRC" ]; then
    echo ""
    echo "Installing logrotate config..."
    sed "s|__LIFEOS_DIR__|$PROJECT_DIR|g" "$LOGROTATE_SRC" > /etc/logrotate.d/lifeos
    echo "  Installed /etc/logrotate.d/lifeos"
fi

# Install sudoers rule so server.sh can restart via systemctl without a password
# (required for nightly sync to restart the server after completion)
echo ""
echo "Installing sudoers rule for passwordless systemctl..."
SUDOERS_FILE="/etc/sudoers.d/lifeos"
TMP_SUDOERS=$(mktemp)
echo "$REAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start lifeos-api, /usr/bin/systemctl stop lifeos-api, /usr/bin/systemctl restart lifeos-api, /usr/bin/systemctl start lifeos-api.service, /usr/bin/systemctl stop lifeos-api.service, /usr/bin/systemctl restart lifeos-api.service" > "$TMP_SUDOERS"
if visudo -c -f "$TMP_SUDOERS" > /dev/null 2>&1; then
    mv "$TMP_SUDOERS" "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    echo "  Installed $SUDOERS_FILE"
else
    rm -f "$TMP_SUDOERS"
    echo "  ERROR: Invalid sudoers syntax — skipping installation"
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
