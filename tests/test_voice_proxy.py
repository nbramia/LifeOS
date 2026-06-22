"""Tests for the voice-gateway reverse proxy (api/routes/voice.py, #361).

The proxy forwards /api/voice/* to the whisper-relay voice gateway. These tests
route the proxy's httpx client through an in-process stub gateway via
ASGITransport (no real server/sockets), so they're fast and deterministic.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import Request as StarletteRequest

from api.routes import voice as voice_module

pytestmark = pytest.mark.unit


# --- in-process stub gateway (stands in for whisper-relay) ---
stub_gateway = FastAPI()


@stub_gateway.post("/api/voice/turn/stream")
async def _stub_turn_stream(request: Request):
    form = await request.form()
    backend = form.get("backend")
    persona = form.get("persona_id")

    async def gen():
        yield b'data: {"type": "started", "turn_id": "t1"}\n\n'
        yield b'data: {"type": "transcript", "text": "hello"}\n\n'
        yield (
            'data: {"type": "done", "data": {"conversation_id": "c1", '
            f'"backend": "{backend}", "persona_id": "{persona}"}}}}\n\n'
        ).encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


@stub_gateway.get("/api/voice/audio/{turn_id}/{clip_id}")
async def _stub_audio(turn_id: str, clip_id: str):
    return Response(
        content=b"RIFF....WAVEfmt ",
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=3600", "X-Clip": clip_id},
    )


@pytest.fixture
def proxy_client(monkeypatch):
    """An httpx client hitting the proxy app, whose upstream is the stub gateway."""
    # Route the proxy's httpx calls into the stub gateway in-process.
    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_gateway),
            base_url="http://gateway",
        )

    monkeypatch.setattr(voice_module, "_client", _stub_client)
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")

    app = FastAPI()
    app.include_router(voice_module.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_turn_stream_forwards_multipart_and_sse(proxy_client):
    files = {"audio": ("turn.webm", b"\x00\x01\x02fakeaudio", "audio/webm")}
    data = {"backend": "lifeos", "persona_id": "fitness", "conversation_id": "c1"}
    resp = await proxy_client.post("/api/voice/turn/stream", files=files, data=data)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    # SSE events streamed through unchanged...
    assert '"type": "started"' in body
    assert '"type": "transcript"' in body
    assert '"type": "done"' in body
    # ...and the multipart fields actually reached the gateway.
    assert '"backend": "lifeos"' in body
    assert '"persona_id": "fitness"' in body


async def test_audio_clip_forwards_bytes_and_headers(proxy_client):
    resp = await proxy_client.get("/api/voice/audio/t1/status-0")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFF....WAVEfmt "
    # non-hop-by-hop upstream headers pass through
    assert resp.headers.get("cache-control") == "private, max-age=3600"
    assert resp.headers.get("x-clip") == "status-0"


async def test_gateway_unreachable_returns_502(monkeypatch):
    class _FailingClient:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            raise httpx.ConnectError("connection refused")

        async def aclose(self):
            pass

    monkeypatch.setattr(voice_module, "_client", _FailingClient)
    app = FastAPI()
    app.include_router(voice_module.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        resp = await client.post("/api/voice/turn/stream", content=b"x")
    assert resp.status_code == 502
    assert resp.json()["error"] == "voice gateway unreachable"


async def test_rejects_parent_traversal_path():
    """The `..` guard rejects path-escape attempts before any upstream call."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/voice/../secret",
        "headers": [],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = StarletteRequest(scope, receive)
    resp = await voice_module.voice_proxy("../secret", req)
    assert resp.status_code == 400
