#!/usr/bin/env bash
# Expose LifeOS on the tailnet HTTPS front (port 443). Required for /chat voice
# (getUserMedia needs a secure context). whisper-relay stays on localhost:9788;
# LifeOS reverse-proxies /api/voice/* (ADR-016).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Port/URL come from the environment (systemd EnvironmentFile or caller export).
# Do not source .env here — paths may contain spaces.

PORT="${LIFEOS_PORT:-8000}"
BACKEND="http://127.0.0.1:${PORT}"

tailscale serve reset
tailscale serve --bg "${BACKEND}"

echo "LifeOS tailnet URLs:"
if [[ -n "${TAILNET_HTTPS_URL:-}" ]]; then
  echo "  HTTPS /chat (voice): ${TAILNET_HTTPS_URL}/chat"
else
  echo "  Set TAILNET_HTTPS_URL in .env for a stable bookmark hint."
fi
tailscale serve status
