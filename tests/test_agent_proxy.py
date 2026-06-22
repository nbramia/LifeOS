"""Tests for the agent text-backend proxy (api/routes/agent_proxy.py, #361).

Routes the proxy's httpx client through an in-process stub agent backend via
ASGITransport (no sockets), so they're fast and deterministic.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from api.routes import agent_proxy as ap

pytestmark = pytest.mark.unit

stub_agent = FastAPI()
_received = {}


@stub_agent.post("/api/ask/stream")
async def _stub_ask(request: Request):
    _received["authorization"] = request.headers.get("authorization")
    body = await request.body()
    _received["body"] = body

    async def gen():
        yield b'data: {"type": "content", "content": "agent says hi"}\n\n'
        yield b'data: {"type": "done"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def proxy_client(monkeypatch):
    _received.clear()

    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_agent), base_url="http://agent"
        )

    monkeypatch.setattr(ap, "_client", _stub_client)
    monkeypatch.setattr(ap.settings, "agent_backend_url", "http://agent")
    monkeypatch.setattr(ap.settings, "agent_backend_token", "secret-token")

    app = FastAPI()
    app.include_router(ap.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_status_reflects_configuration(monkeypatch):
    app = FastAPI()
    app.include_router(ap.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        monkeypatch.setattr(ap.settings, "agent_backend_url", "http://agent")
        assert (await c.get("/api/agent/status")).json()["available"] is True
        monkeypatch.setattr(ap.settings, "agent_backend_url", "")
        assert (await c.get("/api/agent/status")).json()["available"] is False


async def test_ask_stream_injects_bearer_and_streams(proxy_client):
    # The browser sends NO auth; the proxy adds it server-side.
    resp = await proxy_client.post("/api/agent/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "agent says hi" in resp.text
    assert _received["authorization"] == "Bearer secret-token"
    assert b'"question"' in _received["body"]


async def test_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(ap.settings, "agent_backend_url", "")
    app = FastAPI()
    app.include_router(ap.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/agent/ask/stream", json={"question": "hi"})
    assert resp.status_code == 503


async def test_502_when_backend_unreachable(monkeypatch):
    class _Failing:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            raise httpx.ConnectError("refused")

        async def aclose(self):
            pass

    monkeypatch.setattr(ap, "_client", _Failing)
    monkeypatch.setattr(ap.settings, "agent_backend_url", "http://agent")
    app = FastAPI()
    app.include_router(ap.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/agent/ask/stream", json={"question": "hi"})
    assert resp.status_code == 502
