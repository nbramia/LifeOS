#!/bin/bash
# Install LifeOS systemd unit files and enable services.
#
# Usage: sudo ./scripts/setup-systemd.sh
#
# This copies unit files from config/systemd/ into /etc/systemd/system/,
# reloads systemd, and enables+starts all LifeOS services and timers.

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

echo "=== LifeOS systemd Setup ==="
echo ""

# Copy unit files
echo "Copying unit files to $SYSTEMD_DST..."
for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
    [ -f "$unit" ] || continue
    name=$(basename "$unit")
    cp "$unit" "$SYSTEMD_DST/$name"
    echo "  Installed $name"
done

# Reload systemd
echo ""
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable and start services (order matters)
echo ""
echo "Enabling and starting services..."

systemctl enable --now lifeos-chromadb.service
echo "  lifeos-chromadb: $(systemctl is-active lifeos-chromadb.service)"

systemctl enable --now lifeos-api.service
echo "  lifeos-api: $(systemctl is-active lifeos-api.service)"

systemctl enable --now lifeos-watchdog.timer
echo "  lifeos-watchdog.timer: $(systemctl is-active lifeos-watchdog.timer)"

systemctl enable --now lifeos-sync.timer
echo "  lifeos-sync.timer: $(systemctl is-active lifeos-sync.timer)"

# Install logrotate config
LOGROTATE_SRC="$PROJECT_DIR/config/logrotate-lifeos.conf"
if [ -f "$LOGROTATE_SRC" ]; then
    echo ""
    echo "Installing logrotate config..."
    cp "$LOGROTATE_SRC" /etc/logrotate.d/lifeos
    echo "  Installed /etc/logrotate.d/lifeos"
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
