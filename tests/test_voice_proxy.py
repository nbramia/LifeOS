"""Tests for the voice-gateway reverse proxy (api/routes/voice.py, #361), and
its persistence tee (#711, `_VoiceTurnPersister`).

The proxy forwards /api/voice/* to the whisper-relay voice gateway. These tests
route the proxy's httpx client through an in-process stub gateway via
ASGITransport (no real server/sockets), so they're fast and deterministic.
"""

import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import Request as StarletteRequest

from api.routes import voice as voice_module
from api.services.conversation_store import ConversationStore

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


async def test_turn_stream_forwards_hermes_backend_value_unchanged(proxy_client):
    """#593: backend selection is a field on the turn, not a route -- the
    catch-all here has no branch on its value, so a hermes-selected turn
    reaches the gateway through the exact same handler as a lifeos one
    (proven by the same route succeeding for a different backend string,
    with the value itself passed through untouched)."""
    files = {"audio": ("turn.webm", b"\x00\x01\x02fakeaudio", "audio/webm")}
    data = {"backend": "hermes", "persona_id": "fitness", "conversation_id": "c1"}
    resp = await proxy_client.post("/api/voice/turn/stream", files=files, data=data)

    assert resp.status_code == 200
    body = resp.text
    assert '"backend": "hermes"' in body
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
    assert "voice gateway unreachable" in resp.json()["detail"]


def test_filter_headers_drops_hop_by_hop():
    # The proxy must not forward the *client's* connection-control headers; httpx
    # sets its own for the upstream hop (a round-trip can't assert their absence).
    headers = httpx.Headers({
        "Host": "proxy", "Connection": "keep-alive", "Content-Length": "10",
        "Content-Type": "multipart/form-data", "X-Keep": "yes",
    })
    out = {k.lower(): v for k, v in voice_module._filter_headers(headers).items()}
    assert "host" not in out
    assert "connection" not in out
    assert "content-length" not in out
    assert out.get("content-type") == "multipart/form-data"
    assert out.get("x-keep") == "yes"


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
    with pytest.raises(HTTPException) as exc:
        await voice_module.voice_proxy("../secret", req)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Persistence tee (#711) — `_VoiceTurnPersister`
# ---------------------------------------------------------------------------

@pytest.fixture
def voice_store(tmp_path, monkeypatch):
    """A real ConversationStore on a throwaway db, wired in place of the
    singleton `get_store()` voice.py imports — same pattern
    `tests/test_hermes_proxy.py`'s `hermes_store` fixture uses, so these
    tests assert real rows without touching the shared conversations.db."""
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(voice_module, "get_store", lambda: store)
    return store


# A second stub gateway, scripted entirely from the incoming request's form
# fields (`scenario`/`conversation_id`/`transcript_out`/`response_out`) rather
# than shared module state — safe under `-n 2 --dist loadscope` parallelism,
# where the plain `stub_gateway` above stays untouched for the other tests.
persist_stub_gateway = FastAPI()


@persist_stub_gateway.post("/api/voice/turn/stream")
async def _stub_persist_turn_stream(request: Request):
    form = await request.form()
    scenario = form.get("scenario") or "done"
    conv_id = form.get("conversation_id") or "voice-conv-1"
    transcript = form.get("transcript_out") or "hello there"
    response_text = form.get("response_out") or "hi, how can I help?"

    async def gen():
        yield b'data: {"type": "started", "turn_id": "t1"}\n\n'
        yield (
            "data: " + json.dumps({"type": "transcript", "text": transcript}) + "\n\n"
        ).encode()
        if scenario == "error":
            yield b'data: {"type": "error", "message": "boom", "status_code": 502}\n\n'
            return
        if scenario == "cancelled":
            yield b'data: {"type": "cancelled", "turn_id": "t1"}\n\n'
            return
        yield (
            "data: " + json.dumps({"type": "response", "text": response_text}) + "\n\n"
        ).encode()
        done_payload = {
            "type": "done",
            "data": {
                "transcript": transcript,
                "response_text": response_text,
                "conversation_id": conv_id,
                "status_audio_urls": [],
                "audio_url": "/api/voice/audio/t1",
                "handoff": None,
                "timings_ms": {},
            },
        }
        if scenario == "split_done":
            # Deliberately split the `done` frame's JSON across two chunks —
            # a chunk is a network read, not an SSE frame, so the persister
            # must reassemble it rather than assume one frame per chunk.
            whole = ("data: " + json.dumps(done_payload) + "\n\n").encode()
            mid = len(whole) // 2
            yield whole[:mid]
            yield whole[mid:]
        else:
            yield ("data: " + json.dumps(done_payload) + "\n\n").encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def persist_proxy_client(monkeypatch, voice_store):
    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=persist_stub_gateway),
            base_url="http://gateway",
        )

    monkeypatch.setattr(voice_module, "_client", _stub_client)
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")

    app = FastAPI()
    app.include_router(voice_module.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


def _voice_turn_files():
    return {"audio": ("turn.webm", b"\x00\x01\x02fakeaudio", "audio/webm")}


async def test_hermes_backend_turn_persists_transcript_and_response(persist_proxy_client, voice_store):
    """A Hermes-backend voice turn's `done` event is the trigger: it lands in
    the same conversation store the Hermes text route uses, tagged
    backend="hermes" with the request's persona_id, transcript as the user
    message and response_text as the assistant reply."""
    data = {
        "backend": "hermes", "persona_id": "fitness", "conversation_id": "voice-conv-1",
        "transcript_out": "what's my next workout", "response_out": "leg day, per your plan",
    }
    resp = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp.status_code == 200

    conv = voice_store.get_conversation("voice-conv-1")
    assert conv is not None
    assert conv.backend == "hermes"
    assert conv.persona_id == "fitness"

    messages = voice_store.get_messages("voice-conv-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "what's my next workout"),
        ("assistant", "leg day, per your plan"),
    ]


async def test_hermes_backend_multi_turn_groups_into_one_conversation(persist_proxy_client, voice_store):
    """Two turns of the same voice session (same `done.conversation_id`)
    group into ONE conversation, matching the Hermes text route's grouping
    semantics — `create_conversation` is idempotent, so the second turn
    appends rather than duplicating the row."""
    data = {
        "backend": "hermes", "persona_id": "primary", "conversation_id": "voice-conv-multi",
        "transcript_out": "hi", "response_out": "hello!",
    }
    resp1 = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp1.status_code == 200

    data2 = {
        **data, "transcript_out": "what's the weather", "response_out": "sunny today",
    }
    resp2 = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data2
    )
    assert resp2.status_code == 200

    conversations = voice_store.list_conversations()
    assert len([c for c in conversations if c.id == "voice-conv-multi"]) == 1

    messages = voice_store.get_messages("voice-conv-multi")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "hello!"),
        ("user", "what's the weather"),
        ("assistant", "sunny today"),
    ]


async def test_lifeos_backend_turn_does_not_double_write(persist_proxy_client, voice_store):
    """A lifeos-backend turn is already persisted by the native orchestrator
    (the gateway's own call to `/api/ask/stream`, outside this proxy) — the
    tee must not also write it, even though the same authoritative `done`
    event with a `conversation_id` flows through this proxy."""
    data = {
        "backend": "lifeos", "persona_id": "primary", "conversation_id": "voice-conv-lifeos",
        "transcript_out": "hi", "response_out": "hello!",
    }
    resp = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp.status_code == 200
    assert voice_store.get_conversation("voice-conv-lifeos") is None


async def test_agent_backend_turn_does_not_persist(persist_proxy_client, voice_store):
    """Agent-backend history isn't LifeOS-owned by design (client-surfaces.md)
    — the tee must not start persisting it just because a `done` event with a
    conversation_id happened to flow through this proxy."""
    data = {
        "backend": "agent", "conversation_id": "voice-conv-agent",
        "transcript_out": "hi", "response_out": "hello!",
    }
    resp = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp.status_code == 200
    assert voice_store.get_conversation("voice-conv-agent") is None


@pytest.mark.parametrize("scenario", ["error", "cancelled"])
async def test_failed_or_cancelled_turn_persists_nothing(persist_proxy_client, voice_store, scenario):
    """A turn that never reaches a `done` event — including a future bare-
    transcribe/wake-check call (#710) that errors or is cancelled before a
    real answer — creates no conversation at all, even for the hermes
    backend that would otherwise persist."""
    data = {
        "backend": "hermes", "persona_id": "primary", "conversation_id": "voice-conv-failed",
        "scenario": scenario,
    }
    resp = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp.status_code == 200
    assert voice_store.get_conversation("voice-conv-failed") is None


async def test_split_done_frame_reassembled_before_persisting(persist_proxy_client, voice_store):
    """The `done` frame's JSON can split across chunk boundaries — the
    persister must reassemble it rather than assume one frame per chunk."""
    data = {
        "backend": "hermes", "persona_id": "primary", "conversation_id": "voice-conv-split",
        "scenario": "split_done", "transcript_out": "hi", "response_out": "hello!",
    }
    resp = await persist_proxy_client.post(
        "/api/voice/turn/stream", files=_voice_turn_files(), data=data
    )
    assert resp.status_code == 200
    conv = voice_store.get_conversation("voice-conv-split")
    assert conv is not None
    messages = voice_store.get_messages("voice-conv-split")
    assert [(m.role, m.content) for m in messages] == [("user", "hi"), ("assistant", "hello!")]


async def test_malformed_form_does_not_persist_and_relay_still_succeeds(monkeypatch, voice_store):
    """A `turn/stream` POST that isn't multipart at all (a malformed or
    unexpected upload) must not raise — the relay still succeeds byte for
    byte, and `_build_persister` simply falls back to its "don't persist"
    default rather than guessing."""
    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=persist_stub_gateway),
            base_url="http://gateway",
        )

    monkeypatch.setattr(voice_module, "_client", _stub_client)
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")

    app = FastAPI()
    app.include_router(voice_module.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        resp = await client.post(
            "/api/voice/turn/stream",
            content=b"not multipart at all",
            headers={"content-type": "text/plain"},
        )
    assert resp.status_code == 200
    assert '"type": "done"' in resp.text
    # The stub's defaults apply (no backend/persona parsed), which resolve to
    # "lifeos" — i.e. no persistence, not a guess.
    assert voice_store.get_conversation("voice-conv-1") is None


async def test_streamed_bytes_unchanged_with_and_without_the_tee(persist_proxy_client):
    """The tee must never alter what the browser sees: the SAME script,
    proxied for a persisted (hermes) and a non-persisted (lifeos) backend
    alike, reaches the client as byte-identical SSE."""
    base_data = {
        "conversation_id": "voice-conv-bytes", "transcript_out": "hi", "response_out": "hello!",
    }
    responses = {}
    for backend in ("hermes", "lifeos"):
        resp = await persist_proxy_client.post(
            "/api/voice/turn/stream",
            files=_voice_turn_files(),
            data={**base_data, "backend": backend},
        )
        assert resp.status_code == 200
        responses[backend] = resp.content

    assert responses["hermes"] == responses["lifeos"]

    def _frame(event: dict) -> bytes:
        return ("data: " + json.dumps(event) + "\n\n").encode()

    expected = (
        b'data: {"type": "started", "turn_id": "t1"}\n\n'
        + _frame({"type": "transcript", "text": "hi"})
        + _frame({"type": "response", "text": "hello!"})
        + _frame({
            "type": "done",
            "data": {
                "transcript": "hi", "response_text": "hello!",
                "conversation_id": "voice-conv-bytes", "status_audio_urls": [],
                "audio_url": "/api/voice/audio/t1", "handoff": None, "timings_ms": {},
            },
        })
    )
    assert responses["hermes"] == expected


async def test_other_voice_paths_never_build_a_persister(monkeypatch, voice_store):
    """Only `POST turn/stream` is tee'd — a GET (audio clips) or a POST to a
    different path (cancel) must never touch `_build_persister`/the store,
    proving the persistence seam is scoped to the one endpoint it's
    documented for."""
    def _boom(*a, **k):
        raise AssertionError("_build_persister should not run for this path")

    monkeypatch.setattr(voice_module, "_build_persister", _boom)

    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_gateway),
            base_url="http://gateway",
        )

    monkeypatch.setattr(voice_module, "_client", _stub_client)
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")

    app = FastAPI()
    app.include_router(voice_module.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        resp = await client.get("/api/voice/audio/t1/status-0")
    assert resp.status_code == 200
