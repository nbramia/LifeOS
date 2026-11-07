"""The voice proxy's `turn/stream` drain is server-owned
(`api/routes/voice.py`, `_detachable_turn_response`): once the gateway has
sent `started`, a browser disconnect detaches and the Hermes persistence tee
still sees `done`. Before `started` a disconnect stops the drain. The proxy
also forwards `HEAD`, which the browser's mid-stream-drop audio poll uses.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.requests import Request as StarletteRequest

from api.routes import voice as voice_module
from api.services import chat_turns
from api.services.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


@pytest.fixture
def voice_store(tmp_path, monkeypatch):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(voice_module, "get_store", lambda: store)
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")
    return store


def _turn_request(fields: dict) -> StarletteRequest:
    """A `POST turn/stream` Request carrying a multipart body."""
    built = httpx.Request(
        "POST", "http://proxy/api/voice/turn/stream",
        data=fields, files={"audio": ("turn.webm", b"\x00fakeaudio", "audio/webm")},
    )
    body = built.read()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/api/voice/turn/stream", "raw_path": b"/api/voice/turn/stream",
        "query_string": b"", "root_path": "",
        "headers": [(b"content-type", built.headers["content-type"].encode())],
        "client": ("testclient", 1), "server": ("testserver", 80), "http_version": "1.1",
    }
    return StarletteRequest(scope, receive=receive)


def _fake_gateway(gen_factory, closed: list):
    class _Upstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            async for chunk in gen_factory():
                yield chunk

        async def aclose(self):
            closed.append(True)

    class _Client:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            return _Upstream()

        async def aclose(self):
            pass

    return _Client


_DONE = (
    b'data: {"type": "done", "data": {"transcript": "what is next", '
    b'"response_text": "the next thing", "conversation_id": "voice-detach-1"}}\n\n'
)


async def test_hermes_voice_turn_persists_after_the_browser_disconnects(monkeypatch, voice_store):
    hold = asyncio.Event()
    closed: list = []

    async def gen():
        yield b'data: {"type": "started", "turn_id": "t1"}\n\n'
        yield b'data: {"type": "transcript", "text": "what is next"}\n\n'
        await hold.wait()
        yield _DONE

    monkeypatch.setattr(voice_module, "_client", _fake_gateway(gen, closed))
    response = await voice_module.voice_proxy(
        "turn/stream", _turn_request({"backend": "hermes", "persona_id": "primary"}),
    )
    body = response.body_iterator
    await body.__anext__()  # started
    await body.__anext__()  # transcript
    await body.aclose()     # the browser goes away mid-turn

    turns = list(chat_turns.get_turn_registry()._by_turn_id.values())
    assert len(turns) == 1 and not turns[0].task.done()
    hold.set()
    await turns[0].task

    messages = voice_store.get_messages("voice-detach-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "what is next"),
        ("assistant", "the next thing"),
    ]
    assert closed == [True]


async def test_disconnect_before_started_stops_the_drain(monkeypatch, voice_store):
    hold = asyncio.Event()
    closed: list = []

    async def gen():
        yield b": keepalive\n\n"
        await hold.wait()
        yield b'data: {"type": "started", "turn_id": "t1"}\n\n'
        yield _DONE

    monkeypatch.setattr(voice_module, "_client", _fake_gateway(gen, closed))
    response = await voice_module.voice_proxy(
        "turn/stream", _turn_request({"backend": "hermes"}),
    )
    body = response.body_iterator
    await body.__anext__()
    turn = next(iter(chat_turns.get_turn_registry()._by_turn_id.values()))
    await body.aclose()

    with pytest.raises(asyncio.CancelledError):
        await turn.task
    assert closed == [True]
    assert voice_store.get_conversation("voice-detach-1") is None


async def test_head_is_forwarded_to_the_gateway(monkeypatch):
    gateway = FastAPI()
    seen = []

    @gateway.api_route("/api/voice/audio/{turn_id}", methods=["GET", "HEAD"])
    async def _audio(turn_id: str, request: Request):
        seen.append(request.method)
        return Response(content=b"" if request.method == "HEAD" else b"RIFF",
                        media_type="audio/wav", headers={"X-Turn": turn_id})

    monkeypatch.setattr(
        voice_module, "_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"),
    )
    monkeypatch.setattr(voice_module.settings, "voice_gateway_url", "http://gateway")
    app = FastAPI()
    app.include_router(voice_module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as c:
        resp = await c.head("/api/voice/audio/t-head-1")
    assert resp.status_code == 200
    assert resp.headers["x-turn"] == "t-head-1"
    assert seen == ["HEAD"]
