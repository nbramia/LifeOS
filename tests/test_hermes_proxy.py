"""Tests for the Hermes text-backend proxy (api/routes/hermes_proxy.py, #587).

Mirrors tests/test_agent_proxy.py: both backends are mounted from the same
`make_backend_router()` factory in api/routes/_proxy.py, so the same behaviors
(status shape, bearer injection, 503/502) need to hold for Hermes too. Routes
the proxy's httpx client through an in-process stub backend via ASGITransport
(no sockets), so they're fast and deterministic.
"""

import base64
import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from api.routes import hermes_proxy as hp
from api.services.conversation_store import ConversationStore
from api.services.usage_store import UsageStore

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


# ---------------------------------------------------------------------------
# `lifeos_context` envelope (#590) — persona resolution, defaulting, voice
# gating, and rejection paths. Mirrors the registry-fixture pattern in
# tests/test_persona_api.py.
# ---------------------------------------------------------------------------

def _registry(tmp_path, entries):
    """Write a telegram_bots.json registry and point settings at it."""
    reg = tmp_path / "bots.json"
    reg.write_text(json.dumps(entries))
    return reg


async def test_envelope_defaults_to_primary_persona(proxy_client):
    # No persona_id sent → resolves to primary, text modality → no voice rules.
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    # Pin the exact key sets, not just presence. `turn` is a sibling of
    # `persona` added by #591 — see the `turn` sub-object tests below for its
    # own shape and its relationship to persona.
    assert set(ctx.keys()) == {"schema_version", "modality", "persona", "turn"}
    assert set(ctx["persona"].keys()) == {"id", "label", "preamble", "voice_rules", "orchestrates"}
    assert ctx["schema_version"] == 1
    assert ctx["modality"] == "text"
    assert ctx["persona"]["id"] == "primary"
    assert ctx["persona"]["label"] == "Primary"
    assert ctx["persona"]["orchestrates"] is False
    assert ctx["persona"]["voice_rules"] == []
    # Byte-identical to what the native /api/ask/stream path resolves for the
    # same id — both call settings.resolve_persona(), so this can't drift.
    assert ctx["persona"]["preamble"] == hp.settings.resolve_persona("primary")


async def test_envelope_voice_with_no_persona_id_gives_empty_voice_rules(proxy_client, tmp_path, monkeypatch):
    # Native ask_stream() gates voice_rules on `modality == "voice" and
    # request.persona_id` (api/routes/chat.py) — persona_id omitted means no
    # voice rules even if primary's own persona file defines some. The
    # envelope must mirror that exactly rather than inventing a rule just
    # because persona.id defaults to "primary".
    primary_file = tmp_path / "primary.md"
    primary_file.write_text("---\nvoice:\n  - terse\n---\n\nPRIMARY BODY")
    monkeypatch.setattr("config.settings._PRIMARY_PERSONA_FILE", primary_file)

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "modality": "voice"},
    )
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["modality"] == "voice"
    assert ctx["persona"]["id"] == "primary"
    assert ctx["persona"]["voice_rules"] == []


async def test_envelope_empty_preamble_when_primary_has_no_persona_file(proxy_client, monkeypatch):
    # Contract: preamble "may be an empty string (primary with no persona file
    # configured)". Point primary at a file that doesn't exist to exercise it.
    monkeypatch.setattr("config.settings._PRIMARY_PERSONA_FILE", Path("/nonexistent/primary.md"))
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["preamble"] == ""


async def test_envelope_resolves_specialized_persona(proxy_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("FITNESS PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "label": "Fitness Coach", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": "fitness"},
    )
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["id"] == "fitness"
    assert ctx["persona"]["label"] == "Fitness Coach"
    assert ctx["persona"]["preamble"] == "FITNESS PERSONA BODY"
    assert ctx["persona"]["preamble"] == hp.settings.resolve_persona("fitness")
    assert ctx["persona"]["orchestrates"] is False


async def test_envelope_voice_modality_populates_voice_rules(proxy_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("---\nid: fitness\nvoice:\n  - terse\n  - no emoji\n---\n\nBODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "hi", "persona_id": "fitness", "modality": "voice"},
    )
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["modality"] == "voice"
    assert ctx["persona"]["voice_rules"] == ["terse", "no emoji"]


async def test_envelope_text_modality_gives_empty_voice_rules(proxy_client, tmp_path, monkeypatch):
    # Same persona (with real voice rules configured) but a text turn — the
    # rules must NOT be applied, matching the native modality == "voice" gate.
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("---\nid: fitness\nvoice:\n  - terse\n---\n\nBODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": "fitness"},
    )
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["modality"] == "text"
    assert ctx["persona"]["voice_rules"] == []


async def test_unknown_persona_400_and_not_forwarded(proxy_client):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": "ghost"},
    )
    assert resp.status_code == 400
    assert "ghost" in resp.json()["detail"]
    assert _received == {}  # rejected before the upstream backend was ever called


async def test_orchestrating_persona_400_and_not_forwarded(proxy_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "doctor.md"
    persona_file.write_text("DOCTOR PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "doctor", "token_env": "TG_DOC", "persona_file": str(persona_file), "orchestrates": True},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_DOC", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": "doctor"},
    )
    assert resp.status_code == 400
    assert "doctor" in resp.json()["detail"]
    assert _received == {}  # a routing failure, so this must never reach hermes


async def test_orchestrates_field_is_derived_not_hardcoded(proxy_client, monkeypatch):
    # Guards against a regression to a literal `"orchestrates": False` in the
    # envelope. Both the pre-stream guard and the envelope call
    # settings.persona_orchestrates(persona_id); a spy returns False on the
    # first call (so the guard lets the turn through, as it would for any
    # ordinary persona) and True on every call after. A hardcoded literal
    # would report False here regardless of what the guard saw — only a real
    # second call to persona_orchestrates() can surface True.
    calls = {"n": 0}

    def _spy(self, persona_id):
        calls["n"] += 1
        return calls["n"] > 1

    # settings is a pydantic model instance — arbitrary attributes can't be
    # set on it directly (only declared fields), so the method is patched on
    # the class instead.
    monkeypatch.setattr(type(hp.settings), "persona_orchestrates", _spy)

    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["orchestrates"] is True
    assert calls["n"] >= 2


async def test_malformed_json_400_and_not_forwarded(proxy_client):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert _received == {}


async def test_oversized_attachment_400_and_not_forwarded(proxy_client):
    # The same per-file size caps the native chat request model enforces
    # apply here (Constraints in #590) — 6MB of raw data exceeds the 5MB
    # image/png cap (see tests/test_attachments.py for the same pattern).
    large_data = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
    payload = {
        "question": "hi",
        "attachments": [{"filename": "big.png", "media_type": "image/png", "data": large_data}],
    }
    resp = await proxy_client.post("/api/hermes/ask/stream", json=payload)
    assert resp.status_code == 400
    assert _received == {}


async def test_non_envelope_fields_forwarded_unchanged(proxy_client):
    payload = {
        "question": "how are you",
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "modality": "voice",
        "include_sources": False,
    }
    resp = await proxy_client.post("/api/hermes/ask/stream", json=payload)
    assert resp.status_code == 200
    forwarded = json.loads(_received["body"])
    # Exact object, not just "these keys are present": the forwarded body is
    # the browser's payload plus exactly one added top-level key.
    expected = dict(payload)
    expected["lifeos_context"] = forwarded.get("lifeos_context")
    assert forwarded == expected
    assert set(forwarded.keys()) == set(payload.keys()) | {"lifeos_context"}


# ---------------------------------------------------------------------------
# `lifeos_context.turn` (#591) — a sibling of `persona`, sharing its shape
# and literal keys with GET /api/chat/turn-context so one parser handles
# either source.
# ---------------------------------------------------------------------------

async def test_turn_shape_and_literal_keys(proxy_client):
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    turn = ctx["turn"]
    # Literal keys pinned by the cross-repo schema comment on #590, exactly —
    # not a subset check.
    assert set(turn.keys()) == {
        "current_datetime", "current_datetime_iso", "timezone",
        "time_resolution_instruction", "personal_context",
        "existing_tags", "tags_instruction",
    }
    assert isinstance(turn["current_datetime"], str) and turn["current_datetime"]
    assert isinstance(turn["current_datetime_iso"], str) and turn["current_datetime_iso"]
    assert isinstance(turn["timezone"], str) and turn["timezone"]
    assert isinstance(turn["time_resolution_instruction"], str) and turn["time_resolution_instruction"]
    assert isinstance(turn["personal_context"], str)  # may be empty
    assert isinstance(turn["existing_tags"], list)
    assert isinstance(turn["tags_instruction"], str) and turn["tags_instruction"]
    # `turn` and `persona` are siblings under `lifeos_context`, never merged
    # into one object — each key set is disjoint from the other's.
    assert set(turn.keys()).isdisjoint(set(ctx["persona"].keys()))


async def test_turn_matches_endpoint_for_same_persona(proxy_client, monkeypatch):
    """The envelope's `turn` and GET /api/chat/turn-context's body must come
    from the same call to build_turn_context() — pinned by comparing them
    directly rather than by re-deriving each independently, so the two
    surfaces cannot silently diverge.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from api.services import agent_system_prompt as asp

    fixed_now = datetime(2026, 8, 19, 9, 14, 22, tzinfo=ZoneInfo("America/New_York"))

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(asp, "datetime", _Frozen)

    from fastapi import FastAPI as _FastAPI
    from api.routes import chat as chat_routes

    app = _FastAPI()
    app.include_router(chat_routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as chat_client:
        endpoint_resp = await chat_client.get("/api/chat/turn-context", params={"persona_id": "primary"})
    assert endpoint_resp.status_code == 200
    endpoint_turn = endpoint_resp.json()

    hermes_resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert hermes_resp.status_code == 200
    envelope_turn = json.loads(_received["body"])["lifeos_context"]["turn"]

    assert envelope_turn == endpoint_turn


# ---------------------------------------------------------------------------
# Turn persistence (#592) — the proxy is a read-only tee on its own relay:
# the browser gets byte-identical output, and a parallel copy is reassembled
# from the SSE frames and written to the conversation store. `_HermesTurnPersister`
# is exercised both directly (frame reassembly, partial-stream, dedup) and
# through the full HTTP path (byte-exactness, persona/backend on the created
# row, store-failure resilience).
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_store(tmp_path, monkeypatch):
    """A real ConversationStore on a throwaway db, wired in place of the
    singleton `get_store()` hermes_proxy imports, so these tests assert real
    rows without touching the shared conversations.db."""
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(hp, "get_store", lambda: store)
    return store


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    """A real UsageStore on a throwaway db, wired in place of the singleton
    `get_usage_store()` hermes_proxy imports (#595) — same pattern as
    `hermes_store` above."""
    store = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hp, "get_usage_store", lambda: store)
    return store


stub_hermes_persist = FastAPI()

# Two "content" frames deliberately split across the yielded chunk boundary
# (`...conte` | `nt": "world"}...`) — a chunk is a network read, not an SSE
# frame, so the persister must reassemble it rather than assume one frame per
# chunk. Concatenated, these are also the exact bytes the byte-identity
# assertion below expects — a relay that reflows or re-chunks would fail it.
_PERSIST_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "hermes-conv-1"}\n\n',
    b'data: {"type": "content", "content": "Hello "}\n\ndata: {"type": "content", "content": "wo',
    b'rld"}\n\n',
    b'data: {"type": "done"}\n\n',
]


@stub_hermes_persist.post("/api/ask/stream")
async def _stub_persist_ask(request: Request):
    async def gen():
        for chunk in _PERSIST_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def persist_proxy_client(monkeypatch, hermes_store):
    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_persist), base_url="http://hermes"
        )

    monkeypatch.setattr(hp, "_client", _stub_client)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_persists_turn_and_relay_stays_byte_identical(persist_proxy_client, hermes_store):
    resp = await persist_proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi there", "persona_id": "primary"},
    )
    assert resp.status_code == 200
    # Exact bytes despite the split frame — persistence tees a copy, it
    # never reflows what reaches the browser.
    assert resp.content == b"".join(_PERSIST_SSE_CHUNKS)

    conv = hermes_store.get_conversation("hermes-conv-1")
    assert conv is not None
    assert conv.persona_id == "primary"
    assert conv.backend == "hermes"

    messages = hermes_store.get_messages("hermes-conv-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi there"),
        ("assistant", "Hello world"),
    ]


async def test_persists_with_the_selected_persona(persist_proxy_client, hermes_store, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("FITNESS BODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "label": "Fitness Coach", "token_env": "TG_FIT_592", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT_592", "tok")

    resp = await persist_proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "workout plan?", "persona_id": "fitness"},
    )
    assert resp.status_code == 200
    conv = hermes_store.get_conversation("hermes-conv-1")
    assert conv.persona_id == "fitness"
    assert conv.backend == "hermes"


async def test_voice_turn_persists_like_a_typed_turn(persist_proxy_client, hermes_store):
    """#593: a spoken Hermes turn (persona_id + modality=voice, the shape a
    gateway routing voice through this proxy is expected to send) persists
    exactly like a typed one -- modality only affects the upstream envelope
    (test_envelope_voice_modality_populates_voice_rules above), never the
    read-only persistence tee. This is what lets a completed Hermes voice
    turn show up in the sidebar and render after reload, same as text."""
    resp = await persist_proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "hi there", "persona_id": "primary", "modality": "voice"},
    )
    assert resp.status_code == 200
    assert resp.content == b"".join(_PERSIST_SSE_CHUNKS)

    conv = hermes_store.get_conversation("hermes-conv-1")
    assert conv is not None
    assert conv.persona_id == "primary"
    assert conv.backend == "hermes"

    messages = hermes_store.get_messages("hermes-conv-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi there"),
        ("assistant", "Hello world"),
    ]


async def test_store_failure_is_logged_and_never_breaks_the_relay(persist_proxy_client, monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(hp, "get_store", _boom)
    with caplog.at_level(logging.WARNING, logger="api.routes.hermes_proxy"):
        resp = await persist_proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})

    assert resp.status_code == 200
    assert resp.content == b"".join(_PERSIST_SSE_CHUNKS)
    assert "hermes turn persistence" in caplog.text


stub_hermes_truncated = FastAPI()

# No "done" event and no closing content frame — simulates the upstream
# connection dying mid-turn. Whatever arrived before that must still be
# persisted (a partial turn is not discarded).
_TRUNCATED_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "trunc-1"}\n\n',
    b'data: {"type": "content", "content": "partial reply"}\n\n',
]


@stub_hermes_truncated.post("/api/ask/stream")
async def _stub_truncated_ask(request: Request):
    async def gen():
        for chunk in _TRUNCATED_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


async def test_truncated_stream_still_persists_partial_content(monkeypatch, hermes_store):
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_truncated), base_url="http://hermes"
        ),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "will this finish?"})

    assert resp.status_code == 200
    assert resp.content == b"".join(_TRUNCATED_SSE_CHUNKS)

    messages = hermes_store.get_messages("trunc-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "will this finish?"),
        ("assistant", "partial reply"),
    ]


stub_hermes_crlf = FastAPI()

# CRLF frame separators (`\r\n\r\n`) rather than bare LF — SSE permits both
# (WHATWG spec), and an intermediary is free to rewrite line endings even
# though the Hermes adapter itself emits LF today (#592 review: without
# handling this, `_FRAME_SEP` never matched and nothing was persisted —
# silent total data loss, not an error).
_CRLF_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "crlf-1"}\r\n\r\n',
    b'data: {"type": "content", "content": "crlf reply"}\r\n\r\n',
]


@stub_hermes_crlf.post("/api/ask/stream")
async def _stub_crlf_ask(request: Request):
    async def gen():
        for chunk in _CRLF_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


async def test_crlf_framed_stream_still_persists(monkeypatch, hermes_store):
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_crlf), base_url="http://hermes"
        ),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "crlf ok?"})

    assert resp.status_code == 200
    # The relay is byte-for-byte untouched regardless of framing style —
    # persistence tees a copy, it never reflows what reaches the browser.
    assert resp.content == b"".join(_CRLF_SSE_CHUNKS)

    conv = hermes_store.get_conversation("crlf-1")
    assert conv is not None
    messages = hermes_store.get_messages("crlf-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "crlf ok?"),
        ("assistant", "crlf reply"),
    ]


async def test_client_disconnect_still_persists_the_last_chunk(monkeypatch, hermes_store):
    """MAJOR (#592 review): `_proxy.py`'s relay loop used to call
    `observer.observe(chunk)` *after* `yield chunk`. Closing the response
    generator while it's suspended at that yield — exactly what an early
    client disconnect does — raises `GeneratorExit` right there, which
    skips any code written after the yield in that same loop iteration. The
    chunk the relay had already handed off was silently never observed, so
    a disconnect mid-turn lost the very content the partial-turn guarantee
    (see `test_truncated_stream_still_persists_partial_content` above)
    exists to keep.

    Drives the endpoint's returned `StreamingResponse.body_iterator` by
    hand — pulling exactly one chunk, then closing it without ever asking
    for the second — to reproduce that precise "closed while suspended at
    the yield" moment deterministically, rather than relying on real
    socket/ASGI disconnect timing (which httpx's in-process ASGITransport
    doesn't reliably reproduce chunk-for-chunk anyway). A fake upstream
    client (mirroring the `_Failing` pattern in test_agent_proxy.py) gives
    exact control over chunk boundaries that a real ASGI transport can
    silently coalesce away.
    """
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    disco_chunks = [
        b'data: {"type": "conversation_id", "conversation_id": "disco-1"}\n\n'
        b'data: {"type": "content", "content": "first chunk"}\n\n',
        b'data: {"type": "content", "content": "never requested"}\n\n',
    ]

    class _FakeUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            for c in disco_chunks:
                yield c

        async def aclose(self):
            pass

    class _FakeClient:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            return _FakeUpstream()

        async def aclose(self):
            pass

    monkeypatch.setattr(hp, "_client", lambda: _FakeClient())

    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint

    body = json.dumps({"question": "will this survive a disconnect?"}).encode()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/hermes/ask/stream",
        "raw_path": b"/api/hermes/ask/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "http_version": "1.1",
    }
    request = Request(scope, receive=receive)

    response = await endpoint(request)
    gen = response.body_iterator

    first = await gen.__anext__()
    assert first == disco_chunks[0]

    # Simulate the client vanishing right here: close the generator without
    # ever pulling the second chunk. This is exactly the "suspended at the
    # yield" moment the finding describes.
    await gen.aclose()

    conv = hermes_store.get_conversation("disco-1")
    assert conv is not None
    messages = hermes_store.get_messages("disco-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "will this survive a disconnect?"),
        ("assistant", "first chunk"),
    ]


# ---------------------------------------------------------------------------
# Usage capture (#595) — the same read-only tee that persists a Hermes turn
# (#592, above) also captures its `usage` event, if any, and writes a usage
# row on `finalize()`. The relay's byte-identity guarantee applies equally
# here: capturing usage is observation, never a rewrite of what the browser
# receives.
# ---------------------------------------------------------------------------

stub_hermes_usage = FastAPI()

# cost_usd (0.00087) is deliberately *not* what Anthropic sonnet-fallback
# pricing (api/services/cost_tracker.py's MODEL_PRICING, the price any
# non-haiku/opus model falls through to) would compute for these same token
# counts: (120/1e6)*3.0 + (340/1e6)*15.0 = 0.00546. Recording the wrong,
# upstream-reported number rather than that recomputed one is exactly the
# behavior under test — proof the store isn't quietly recalculating it.
_USAGE_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "usage-conv-1"}\n\n',
    b'data: {"type": "content", "content": "here is your answer"}\n\n',
    b'data: {"type": "usage", "model": "deepseek-v3-fireworks", '
    b'"input_tokens": 120, "output_tokens": 340, "cost_usd": 0.00087}\n\n',
    b'data: {"type": "done"}\n\n',
]


@stub_hermes_usage.post("/api/ask/stream")
async def _stub_usage_ask(request: Request):
    async def gen():
        for chunk in _USAGE_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def usage_proxy_client(monkeypatch, hermes_store, usage_store):
    def _stub_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_usage), base_url="http://hermes"
        )

    monkeypatch.setattr(hp, "_client", _stub_client)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_usage_event_writes_a_row_with_verbatim_cost(usage_proxy_client, usage_store):
    resp = await usage_proxy_client.post("/api/hermes/ask/stream", json={"question": "what's 2+2?"})
    assert resp.status_code == 200
    # Byte-identical relay even though this same pass captures usage.
    assert resp.content == b"".join(_USAGE_SSE_CHUNKS)

    with sqlite3.connect(usage_store.db_path) as conn:
        row = conn.execute(
            "SELECT model, input_tokens, output_tokens, cost_usd, conversation_id FROM usage"
        ).fetchone()
    assert row is not None
    model, input_tokens, output_tokens, cost_usd, conversation_id = row
    assert model == "deepseek-v3-fireworks"
    assert input_tokens == 120
    assert output_tokens == 340
    # Verbatim, not the ~0.00546 sonnet-fallback recompute — see the chunk
    # comment above.
    assert cost_usd == pytest.approx(0.00087)
    assert conversation_id == "usage-conv-1"


stub_hermes_usage_no_cost = FastAPI()

_USAGE_NO_COST_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "usage-conv-2"}\n\n',
    b'data: {"type": "content", "content": "answer"}\n\n',
    b'data: {"type": "usage", "model": "some-model", "input_tokens": 10, "output_tokens": 5}\n\n',
    b'data: {"type": "done"}\n\n',
]


@stub_hermes_usage_no_cost.post("/api/ask/stream")
async def _stub_usage_no_cost_ask(request: Request):
    async def gen():
        for chunk in _USAGE_NO_COST_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


async def test_usage_event_without_cost_records_zero(monkeypatch, hermes_store, usage_store):
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_usage_no_cost), base_url="http://hermes"
        ),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "hi"})

    assert resp.status_code == 200
    assert resp.content == b"".join(_USAGE_NO_COST_SSE_CHUNKS)

    stats = usage_store.get_usage_stats()
    assert stats["request_count"] == 1
    assert stats["total_input_tokens"] == 10
    assert stats["total_output_tokens"] == 5
    assert stats["total_cost"] == 0.0


stub_hermes_malformed_usage = FastAPI()

# Missing "model" -- partial, must be ignored rather than raised, and must
# not stop the (otherwise complete) turn from streaming and persisting.
_MALFORMED_USAGE_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "usage-conv-3"}\n\n',
    b'data: {"type": "content", "content": "answer"}\n\n',
    b'data: {"type": "usage", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}\n\n',
    b'data: {"type": "done"}\n\n',
]


@stub_hermes_malformed_usage.post("/api/ask/stream")
async def _stub_malformed_usage_ask(request: Request):
    async def gen():
        for chunk in _MALFORMED_USAGE_SSE_CHUNKS:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


async def test_malformed_usage_event_is_ignored_and_does_not_interrupt_the_relay(
    monkeypatch, hermes_store, usage_store,
):
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_hermes_malformed_usage), base_url="http://hermes"
        ),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post("/api/hermes/ask/stream", json={"question": "hi"})

    assert resp.status_code == 200
    assert resp.content == b"".join(_MALFORMED_USAGE_SSE_CHUNKS)

    # No usage row -- the malformed event was dropped, not recorded.
    assert usage_store.get_usage_stats()["request_count"] == 0
    # The turn itself still completed and persisted normally.
    messages = hermes_store.get_messages("usage-conv-3")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "answer"),
    ]


async def test_no_usage_event_writes_no_row_and_turn_completes_normally(
    persist_proxy_client, hermes_store, usage_store,
):
    """_PERSIST_SSE_CHUNKS (above) carries no `usage` event at all."""
    resp = await persist_proxy_client.post("/api/hermes/ask/stream", json={"question": "hi there"})
    assert resp.status_code == 200
    assert resp.content == b"".join(_PERSIST_SSE_CHUNKS)
    assert usage_store.get_usage_stats()["request_count"] == 0
    # Conversation persistence (#592) is unaffected by usage capture.
    assert hermes_store.get_conversation("hermes-conv-1") is not None


async def test_usage_store_failure_is_logged_and_never_breaks_the_relay(
    usage_proxy_client, monkeypatch, caplog,
):
    def _boom():
        raise RuntimeError("usage db exploded")

    monkeypatch.setattr(hp, "get_usage_store", _boom)
    with caplog.at_level(logging.WARNING, logger="api.routes.hermes_proxy"):
        resp = await usage_proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})

    assert resp.status_code == 200
    assert resp.content == b"".join(_USAGE_SSE_CHUNKS)
    assert "hermes turn persistence" in caplog.text


class TestHermesTurnPersisterDirect:
    """Direct, non-HTTP tests of `_HermesTurnPersister` — the SSE frame
    reassembly and store-write logic in isolation from the transport."""

    def test_reassembles_frame_split_across_observe_calls(self, hermes_store):
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(b'data: {"type": "conversation_id", "conv')
        persister.observe(b'ersation_id": "split-1"}\n\n')
        persister.observe(b'data: {"type": "content", "content": "he')
        persister.observe(b'llo"}\n\n')
        persister.finalize()

        messages = hermes_store.get_messages("split-1")
        assert [(m.role, m.content) for m in messages] == [
            ("user", "q"),
            ("assistant", "hello"),
        ]

    def test_observe_never_calls_the_store(self, monkeypatch):
        """BLOCKER (#592 review): `observe()` used to call
        `store.create_conversation()`/`add_message()` synchronously as soon
        as a `conversation_id` event was parsed, so a locked db
        (`ConversationStore._connect()`'s 10s busy timeout) could stall
        delivery of this stream's own next chunk. `observe()` must do only
        in-memory parsing; every store call belongs in `finalize()`, once,
        after the relay has already handed off every byte of the turn.
        """
        calls = []

        class _TrackingStore:
            def create_conversation(self, **kw):
                calls.append(("create_conversation", kw))

            def add_message(self, *a, **kw):
                calls.append(("add_message", a))

        monkeypatch.setattr(hp, "get_store", lambda: _TrackingStore())

        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(b'data: {"type": "conversation_id", "conversation_id": "track-1"}\n\n')
        persister.observe(b'data: {"type": "content", "content": "hello"}\n\n')

        # Still mid-stream: nothing written to the store yet.
        assert calls == []

        persister.finalize()

        assert calls == [
            ("create_conversation", {"conv_id": "track-1", "persona_id": "primary", "backend": "hermes"}),
            ("add_message", ("track-1", "user", "q")),
            ("add_message", ("track-1", "assistant", "hello")),
        ]

    def test_finalize_without_a_conversation_id_persists_nothing(self, hermes_store):
        # Content arrived but the backend never sent a conversation_id event
        # (e.g. it errored before assigning one) — nothing to attach it to.
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(b'data: {"type": "content", "content": "orphaned"}\n\n')
        persister.finalize()
        assert hermes_store.list_conversations() == []

    def test_continues_an_existing_conversation_without_duplicating(self, hermes_store):
        hermes_store.create_conversation(conv_id="existing-1", persona_id="primary", backend="hermes")

        persister = hp._HermesTurnPersister(question="second turn q", persona_id="primary")
        persister.observe(b'data: {"type": "conversation_id", "conversation_id": "existing-1"}\n\n')
        persister.observe(b'data: {"type": "content", "content": "second turn a"}\n\n')
        persister.finalize()

        assert len(hermes_store.list_conversations()) == 1
        messages = hermes_store.get_messages("existing-1")
        assert [(m.role, m.content) for m in messages] == [
            ("user", "second turn q"),
            ("assistant", "second turn a"),
        ]

    def test_overlong_conversation_id_is_dropped_not_truncated(self, hermes_store, caplog):
        """MAJOR (#592 review): an overlong id used to be silently
        truncated (`conv_id[:_MAX_CONVERSATION_ID_LEN]`) before the store
        write. The browser keeps the verbatim upstream id and would later
        request it in full via `GET /api/conversations/{id}` — a row
        created under the truncated id could never be found on reload.
        Now it's logged and dropped instead of creating that mismatched
        row.
        """
        overlong = "x" * (hp._MAX_CONVERSATION_ID_LEN + 50)
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        with caplog.at_level(logging.WARNING, logger="api.routes.hermes_proxy"):
            persister.observe(
                b'data: ' + json.dumps(
                    {"type": "conversation_id", "conversation_id": overlong}
                ).encode() + b'\n\n'
            )
            persister.observe(b'data: {"type": "content", "content": "hi"}\n\n')
            persister.finalize()

        assert "over the" in caplog.text
        assert hermes_store.list_conversations() == []
        # No row under either the truncated id or the verbatim one.
        assert hermes_store.get_conversation(overlong[:hp._MAX_CONVERSATION_ID_LEN]) is None
        assert hermes_store.get_conversation(overlong) is None

    def test_observe_never_calls_the_usage_store(self, monkeypatch, hermes_store):
        """Same structural guarantee as `test_observe_never_calls_the_store`
        above (#592 review), extended to usage capture (#595): `observe()`
        must only reassemble frames and buffer the parsed usage fields in
        memory. Every usage-store call belongs in `finalize()`."""
        calls = []

        class _TrackingUsageStore:
            def record_usage(self, **kw):
                calls.append(("record_usage", kw))

        monkeypatch.setattr(hp, "get_usage_store", lambda: _TrackingUsageStore())

        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(b'data: {"type": "conversation_id", "conversation_id": "usage-track-1"}\n\n')
        persister.observe(
            b'data: {"type": "usage", "model": "m", "input_tokens": 1, '
            b'"output_tokens": 2, "cost_usd": 0.01}\n\n'
        )

        # Still mid-stream: nothing written to the usage store yet.
        assert calls == []

        persister.finalize()

        assert calls == [
            ("record_usage", {
                "model": "m", "input_tokens": 1, "output_tokens": 2,
                "cost_usd": 0.01, "conversation_id": "usage-track-1",
            }),
        ]

    def test_usage_event_missing_model_is_ignored(self, hermes_store, usage_store):
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(
            b'data: {"type": "usage", "input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01}\n\n'
        )
        persister.finalize()
        assert usage_store.get_usage_stats()["request_count"] == 0

    def test_usage_event_with_non_int_tokens_is_ignored(self, hermes_store, usage_store):
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(
            b'data: {"type": "usage", "model": "m", "input_tokens": "lots", "output_tokens": 2}\n\n'
        )
        persister.finalize()
        assert usage_store.get_usage_stats()["request_count"] == 0

    def test_a_malformed_usage_event_does_not_shadow_a_later_valid_one(self, hermes_store, usage_store):
        """Unlike `conversation_id` (recorded "once" regardless of
        validity), a malformed `usage` event must not permanently block
        capture — only a successfully validated one sets
        `_usage_captured`."""
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(b'data: {"type": "usage", "input_tokens": 1, "output_tokens": 2}\n\n')
        persister.observe(
            b'data: {"type": "usage", "model": "m", "input_tokens": 10, '
            b'"output_tokens": 20, "cost_usd": 0.5}\n\n'
        )
        persister.finalize()

        stats = usage_store.get_usage_stats()
        assert stats["request_count"] == 1
        assert stats["total_input_tokens"] == 10
        assert stats["total_output_tokens"] == 20
        assert stats["total_cost"] == pytest.approx(0.5)

    def test_usage_recorded_even_without_a_conversation_id(self, hermes_store, usage_store):
        """Conversation persistence and usage persistence are independent
        (#595) -- a usage event with no preceding `conversation_id` still
        gets recorded, with a null conversation id, rather than being
        dropped because there's nothing to attach a conversation row to."""
        persister = hp._HermesTurnPersister(question="q", persona_id="primary")
        persister.observe(
            b'data: {"type": "usage", "model": "m", "input_tokens": 1, '
            b'"output_tokens": 2, "cost_usd": 0.001}\n\n'
        )
        persister.finalize()

        assert hermes_store.list_conversations() == []
        stats = usage_store.get_usage_stats()
        assert stats["request_count"] == 1
        assert stats["total_cost"] == pytest.approx(0.001)


class TestMakePersister:
    """`_make_persister()` — the minimal reparse of the raw request body
    that builds this turn's `_HermesTurnPersister`."""

    def test_defaults_persona_to_primary_when_omitted(self):
        p = hp._make_persister(json.dumps({"question": "hi"}).encode())
        assert p is not None
        assert p._persona_id == "primary"
        assert p._question == "hi"

    def test_uses_the_given_persona_id(self):
        p = hp._make_persister(json.dumps({"question": "hi", "persona_id": "doctor"}).encode())
        assert p._persona_id == "doctor"

    def test_returns_none_for_malformed_json(self):
        assert hp._make_persister(b"{not valid json") is None

    def test_returns_none_when_question_is_missing_or_wrong_type(self):
        assert hp._make_persister(json.dumps({}).encode()) is None
        assert hp._make_persister(json.dumps({"question": 5}).encode()) is None
