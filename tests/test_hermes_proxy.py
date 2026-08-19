"""Tests for the Hermes text-backend proxy (api/routes/hermes_proxy.py, #587).

Mirrors tests/test_agent_proxy.py: both backends are mounted from the same
`make_backend_router()` factory in api/routes/_proxy.py, so the same behaviors
(status shape, bearer injection, 503/502) need to hold for Hermes too. Routes
the proxy's httpx client through an in-process stub backend via ASGITransport
(no sockets), so they're fast and deterministic.
"""

import base64
import json
from pathlib import Path

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
