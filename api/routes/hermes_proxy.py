"""Hermes text-backend proxy (#587).

`/chat`'s third text backend: Hermes, an agent harness running as a gateway
(same box or reached over the tailnet), which speaks the same `/api/ask/stream`
SSE contract as LifeOS and the Agent backend. LifeOS proxies it at
``POST /api/hermes/ask/stream``, **adding the token server-side** so it never
reaches the browser. Empty ``LIFEOS_HERMES_BACKEND_URL`` disables it entirely —
`GET /api/hermes/status` then reports unavailable and `/chat` behaves exactly as
it does today.

This module only supplies the backend's settings fields and its own `_client()`
test seam; the status/ask-stream/bearer-injection logic is shared with the Agent
backend via `make_backend_router()` in `_proxy.py`.
"""

import httpx

# Imported (not just re-exported through _proxy.py) so tests can monkeypatch
# `hermes_proxy.settings.hermes_backend_url` / `hermes_backend_token` directly —
# `settings` is a shared singleton, so the factory in `_proxy.py` sees the same
# mutated object.
from config.settings import settings  # noqa: F401

from api.routes._proxy import TIMEOUT, make_backend_router


def _client() -> httpx.AsyncClient:
    """httpx client for the Hermes backend (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


router = make_backend_router(
    prefix="/api/hermes",
    tag="hermes",
    backend_label="hermes",
    url_attr="hermes_backend_url",
    token_attr="hermes_backend_token",
    client_factory=lambda: _client(),
)
