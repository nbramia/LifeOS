#!/usr/bin/env bash
# Generate ~/.config/systemd/user/lifeos-tailscale.service (oneshot after API is up).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT/.env}"
REPO_DIR="${DEPLOY_REPO_DIR:-$ROOT}"
ENV_PATH="${DEPLOY_ENV_FILE:-$ROOT/.env}"
LIFEOS_PORT="${LIFEOS_PORT:-8000}"

DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lifeos-tailscale.service"
mkdir -p "$(dirname "$DEST")"

cat >"$DEST" <<EOF
[Unit]
Description=Tailscale Serve proxy for LifeOS (/chat HTTPS)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_PATH}
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 60); do curl -sf http://127.0.0.1:${LIFEOS_PORT}/health >/dev/null && exit 0; sleep 2; done; echo "LifeOS API not healthy on :${LIFEOS_PORT}"; exit 1'
ExecStart=${REPO_DIR}/scripts/setup-tailscale.sh

[Install]
WantedBy=default.target
EOF

echo "Wrote ${DEST}"
echo "Enable with: systemctl --user enable --now lifeos-tailscale.service"
echo "Disable whisper-relay on :443 if still enabled:"
echo "  systemctl --user disable --now whisper-relay-tailscale.service"
