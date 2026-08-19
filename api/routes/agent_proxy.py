"""Agent text-backend proxy (#361).

The `/chat` "Agent" backend talks to the OpenClaw voice-adapter, which speaks the
same `/api/ask/stream` SSE contract as LifeOS but is reached at
``LIFEOS_AGENT_BACKEND_URL`` and may require a bearer token. LifeOS proxies it at
``POST /api/agent/ask/stream``, **adding the token server-side** so it never
reaches the browser. The browser stays same-origin and treats the response
exactly like a normal ask/stream (minus handoff — the agent backend has none).

The status/ask-stream/bearer-injection logic itself is shared with the Hermes
backend via `make_backend_router()` in `_proxy.py` (#587); this module just
supplies this backend's settings fields and its own `_client()` test seam.
"""

import httpx

# Imported (not just re-exported through _proxy.py) so tests can monkeypatch
# `agent_proxy.settings.agent_backend_url` / `agent_backend_token` directly —
# `settings` is a shared singleton, so the factory in `_proxy.py` sees the same
# mutated object.
from config.settings import settings  # noqa: F401

from api.routes._proxy import TIMEOUT, make_backend_router


def _client() -> httpx.AsyncClient:
    """httpx client for the agent backend (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


router = make_backend_router(
    prefix="/api/agent",
    tag="agent",
    backend_label="agent",
    url_attr="agent_backend_url",
    token_attr="agent_backend_token",
    client_factory=lambda: _client(),
)
