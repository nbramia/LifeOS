"""Tests for the Hermes text-backend proxy (api/routes/hermes_proxy.py, #587).

Mirrors tests/test_agent_proxy.py: both backends are mounted from the same
`make_backend_router()` factory in api/routes/_proxy.py, so the same behaviors
(status shape, bearer injection, 503/502) need to hold for Hermes too. Routes
the proxy's httpx client through an in-process stub backend via ASGITransport
(no sockets), so they're fast and deterministic.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from api.routes import hermes_proxy as hp

pytestmark = pytest.mark.unit

stub_hermes = FastAPI()
_received = {}


_UPSTREAM_SSE_CHUNKS = [
    b'data: {"type": "content", "content": "hermes says hi"}\n\n',
    b'data: {"type": "done"}\n\n',
]


@stub_hermes.post("/api/ask/stream")
async def _stub_ask(request: Request):
    _received["authorization"] = request.headers.get("authorization")
    body = await request.body()
    _received["body"] = body

    async def gen():
        for chunk in _UPSTREAM_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def proxy_client(monkeypatch):
    _received.clear()

    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes), base_url="http://hermes"
        )

    monkeypatch.setattr(hp, "_client", _stub_client)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "secret-token")

    app = FastAPI()
    app.include_router(hp.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_status_reflects_configuration(monkeypatch):
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
        assert (await c.get("/api/hermes/status")).json()["available"] is True
        monkeypatch.setattr(hp.settings, "hermes_backend_url", "")
        assert (await c.get("/api/hermes/status")).json()["available"] is False


async def test_ask_stream_injects_bearer_and_streams(proxy_client):
    # The browser sends NO auth; the proxy adds it server-side.
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Exact bytes, not a substring — a relay that re-chunks or wraps the
    # upstream stream (e.g. adds framing, buffers, or drops a chunk boundary)
    # must fail this, not just "still contains the text somewhere".
    assert resp.content == b"".join(_UPSTREAM_SSE_CHUNKS)
    assert _received["authorization"] == "Bearer secret-token"
    assert b'"question"' in _received["body"]


async def test_client_supplied_auth_is_stripped_not_forwarded(proxy_client):
    # A client-supplied Authorization must never reach upstream — the server's
    # configured bearer replaces it.
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi"},
        headers={"Authorization": "Bearer client-sneaky"},
    )
    assert resp.status_code == 200
    assert _received["authorization"] == "Bearer secret-token"


async def test_empty_token_forwards_no_auth(proxy_client, monkeypatch):
    # No token configured + no client auth → nothing on the wire upstream.
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi"},
        headers={"Authorization": "Bearer client-sneaky"},
    )
    assert resp.status_code == 200
    # the client's Authorization is stripped (not forwarded), and no server token
    assert _received["authorization"] is None


_stub_500 = FastAPI()


@_stub_500.post("/api/ask/stream")
async def _stub_err():
    return Response(status_code=500, content=b"upstream boom")


async def test_non_200_upstream_is_passed_through(monkeypatch):
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_stub_500), base_url="http://a"),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://a")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 500


async def test_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 503


async def test_502_when_backend_unreachable(monkeypatch):
    class _Failing:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            raise httpx.ConnectError("refused")

        async def aclose(self):
            pass

    monkeypatch.setattr(hp, "_client", _Failing)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 502
    assert "refused" in resp.json()["detail"]
