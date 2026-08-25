"""Tests for the Hermes text-backend proxy (api/routes/hermes_proxy.py, #587).

Mirrors tests/test_agent_proxy.py: both backends are mounted from the same
`make_backend_router()` factory in api/routes/_proxy.py, so the same behaviors
(status shape, bearer injection, 503/502) need to hold for Hermes too. Routes
the proxy's httpx client through an in-process stub backend via ASGITransport
(no sockets), so they're fast and deterministic.
"""

import asyncio
import base64
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient

from api.routes import hermes_proxy as hp
from api.services.conversation_store import ConversationStore
from api.services.journal_capture import log_path_for
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


async def test_orchestrating_persona_no_longer_rejected(proxy_client, tmp_path, monkeypatch):
    # #642: this used to be a 400 (test_orchestrating_persona_400_and_not_
    # forwarded) — Hermes had no way to drive a background Claude Code
    # session, so an orchestrating persona reaching this route was treated as
    # a routing bug. #640 gave Hermes that capability, so the persona now
    # reaches Hermes like any other: forwarded, 200, with `orchestrates: true`
    # in the envelope (see test_orchestrates_field_is_derived_not_hardcoded
    # and test_orchestrates_true_uses_hermes_surface_preamble for the rest of
    # what changed alongside this).
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
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["id"] == "doctor"
    assert ctx["persona"]["orchestrates"] is True
    assert ctx["persona"]["preamble"] == "DOCTOR PERSONA BODY"


async def test_orchestrates_true_uses_hermes_surface_preamble(proxy_client, tmp_path, monkeypatch):
    # #642: the envelope resolves persona_id with surface="hermes" so an
    # orchestrating persona with a Hermes-specific variant (e.g.
    # config/personas/doctor.hermes.md, #641) gets that body instead of the
    # plain one — which claims shell/filesystem access Hermes doesn't have.
    # A sibling `<stem>.hermes<suffix>` file next to persona_file (the naming
    # rule _surface_variant_body uses) proves the surface parameter is
    # actually threaded through, not just accepted and ignored.
    persona_file = tmp_path / "doctor.md"
    persona_file.write_text("PLAIN DOCTOR BODY (claims shell access)")
    (tmp_path / "doctor.hermes.md").write_text("HERMES DOCTOR BODY (drives lifeos_agent_spawn)")
    reg = _registry(tmp_path, [
        {"name": "doctor", "token_env": "TG_DOC", "persona_file": str(persona_file), "orchestrates": True},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_DOC", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": "doctor"},
    )
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["preamble"] == "HERMES DOCTOR BODY (drives lifeos_agent_spawn)"


async def test_orchestrates_field_is_derived_not_hardcoded(proxy_client, monkeypatch):
    # Guards against a regression to a literal `"orchestrates": False` (or,
    # since #642 removed the guard that used to make every real call False in
    # practice, a literal `True`) in the envelope. A spy that always returns
    # True proves the field reflects a real call to
    # settings.persona_orchestrates(), not a hardcoded literal.
    calls = {"n": 0}

    def _spy(self, persona_id):
        calls["n"] += 1
        return True

    # settings is a pydantic model instance — arbitrary attributes can't be
    # set on it directly (only declared fields), so the method is patched on
    # the class instead.
    monkeypatch.setattr(type(hp.settings), "persona_orchestrates", _spy)

    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]
    assert ctx["persona"]["orchestrates"] is True
    assert calls["n"] >= 1


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
    # not a subset check. `caller_session_id` (#640) is the one key added
    # here rather than by build_turn_context() itself — see hermes_proxy.py.
    assert set(turn.keys()) == {
        "current_datetime", "current_datetime_iso", "timezone",
        "time_resolution_instruction", "personal_context",
        "existing_tags", "tags_instruction",
        "session_cost_usd", "session_turn_count",
        "session_input_tokens", "session_output_tokens",
        "session_cost_is_lower_bound",
        "caller_session_id",
    }
    assert isinstance(turn["current_datetime"], str) and turn["current_datetime"]
    assert isinstance(turn["current_datetime_iso"], str) and turn["current_datetime_iso"]
    assert isinstance(turn["timezone"], str) and turn["timezone"]
    assert isinstance(turn["time_resolution_instruction"], str) and turn["time_resolution_instruction"]
    assert isinstance(turn["personal_context"], str)  # may be empty
    assert isinstance(turn["existing_tags"], list)
    assert isinstance(turn["tags_instruction"], str) and turn["tags_instruction"]
    assert isinstance(turn["caller_session_id"], str) and turn["caller_session_id"]
    # `turn` and `persona` are siblings under `lifeos_context`, never merged
    # into one object — each key set is disjoint from the other's. This is
    # the separation #640's caller_session_id must respect too: it lands in
    # `turn` (per-turn, never prompt-cached), not `persona` (stable across a
    # conversation, prompt-cacheable — a per-turn value there would bust
    # that cache every request).
    assert set(turn.keys()).isdisjoint(set(ctx["persona"].keys()))
    assert "caller_session_id" not in ctx["persona"]


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

    # `caller_session_id` (#640) is the one deliberate difference: it's an
    # agent-worker session identity Hermes needs and the plain turn-context
    # endpoint has no reason to hand out (it creates nothing). Every other
    # field must still come from the identical build_turn_context() call.
    assert "caller_session_id" not in endpoint_turn
    envelope_turn_without_session = dict(envelope_turn)
    caller_session_id = envelope_turn_without_session.pop("caller_session_id")
    assert isinstance(caller_session_id, str) and caller_session_id
    assert envelope_turn_without_session == endpoint_turn


# ---------------------------------------------------------------------------
# `caller_session_id` lifecycle (#640) — a real agent-worker session backs
# the id handed to Hermes, so `lifeos_agent_*` calls (which all require a
# resolvable `caller_session_id`, see inter_agent.py) work from a Hermes
# turn instead of failing with `no_caller`.
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_session_store(tmp_path, monkeypatch):
    """A real SessionStore on a throwaway db, wired in place of the class
    `hermes_proxy._resolve_caller_session_id` locally imports (#640) — same
    isolation pattern as `hermes_store`/`usage_store` above, applied to the
    class itself (rather than a module-level singleton) because
    `_resolve_caller_session_id` constructs a fresh `SessionStore()` per
    call, mirroring how every other API route touches this store."""
    from api.services.agent_worker.session_store import SessionStore

    store = SessionStore(str(tmp_path / "agent_sessions.db"))
    monkeypatch.setattr(
        "api.services.agent_worker.session_store.SessionStore",
        lambda *a, **kw: store,
    )
    return store


async def test_caller_session_id_resolves_to_a_real_session(proxy_client, agent_session_store):
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    session = agent_session_store.get_by_session_id(caller_session_id)
    assert session is not None
    assert session.routing == "hermes"
    assert session.origin == "hermes"


async def test_caller_session_id_is_stable_across_turns_of_the_same_conversation(
    proxy_client, agent_session_store,
):
    """Once a conversation has an id, every turn resolves to the SAME
    session — the per-conversation lifecycle this module chose (see
    hermes_session.py's module docstring) so a worker spawned on one turn
    is still reachable, under the same lineage root, on a later one."""
    payload = {"question": "hi", "conversation_id": "conv-stable-640"}
    resp1 = await proxy_client.post("/api/hermes/ask/stream", json=payload)
    caller_1 = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]
    resp2 = await proxy_client.post("/api/hermes/ask/stream", json=payload)
    caller_2 = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    assert resp1.status_code == 200 and resp2.status_code == 200
    assert caller_1 == caller_2
    # Exactly one row was created, not one per turn.
    assert len(agent_session_store.list_sessions(routing="hermes")) == 1


async def test_caller_session_id_differs_across_conversations(proxy_client, agent_session_store):
    resp1 = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-a-640"},
    )
    caller_a = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]
    resp2 = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-b-640"},
    )
    caller_b = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    assert resp1.status_code == 200 and resp2.status_code == 200
    assert caller_a != caller_b


async def test_spawn_succeeds_from_a_hermes_turns_caller_session_id(proxy_client, agent_session_store, tmp_path):
    """Acceptance criterion: a Hermes turn's caller_session_id can spawn —
    the exact call that returned `no_caller` before #640, since Hermes had
    no session at all."""
    from api.services.agent_worker import inter_agent
    from api.services.agent_worker.transcript_store import TranscriptStore

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-spawn-640"},
    )
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    ctx = inter_agent.InterAgentContext(
        session_store=agent_session_store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        caller_session_id=caller_session_id,
        caps=inter_agent.Caps(),
    )
    result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "look something up", "model": "claude_code",
    })

    assert result["ok"] is True, result


# ---------------------------------------------------------------------------
# Caller-session bot ownership (#684 adversarial review) — a Hermes turn's
# caller_session_id, and every lifeos_agent_spawn descendant of it, must
# carry the SAME bot ownership a native-spawned session gets, so the
# worker's status/blocked notices for that lineage route to the right
# Telegram bot and that bot's threaded-reply resume (scoped to its own
# `bot`) can find them. Before this, a Hermes-rooted session's `bot` was
# always None regardless of persona_id, so every descendant silently fell
# back to the PRIMARY bot's channel.
# ---------------------------------------------------------------------------

async def test_caller_session_bot_matches_persona_id(proxy_client, agent_session_store, tmp_path, monkeypatch):
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
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    session = agent_session_store.get_by_session_id(caller_session_id)
    assert session is not None
    assert session.bot == "doctor"


async def test_caller_session_bot_is_none_for_primary(proxy_client, agent_session_store):
    """No persona_id → defaults to primary — `bot` stays `None`, matching
    the convention every primary-rooted session already uses (never the
    literal string "primary")."""
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    session = agent_session_store.get_by_session_id(caller_session_id)
    assert session is not None
    assert session.bot is None


async def test_spawned_child_inherits_bot_from_hermes_caller(proxy_client, agent_session_store, tmp_path, monkeypatch):
    """The Codex-flagged regression: a doctor-persona Hermes turn's
    caller_session_id, used to `lifeos_agent_spawn` a worker, must produce a
    child session tagged `bot="doctor"` — not `None` (which would route the
    worker's own status/blocked notices to the primary bot's channel and
    make the doctor listener's threaded-reply resume, scoped to `bot=
    "doctor"`, unable to find them)."""
    from api.services.agent_worker import inter_agent
    from api.services.agent_worker.transcript_store import TranscriptStore

    persona_file = tmp_path / "doctor.md"
    persona_file.write_text("DOCTOR PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "doctor", "token_env": "TG_DOC", "persona_file": str(persona_file), "orchestrates": True},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_DOC", "tok")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "hi", "persona_id": "doctor", "conversation_id": "conv-bot-684"},
    )
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]
    assert agent_session_store.get_by_session_id(caller_session_id).bot == "doctor"

    ctx = inter_agent.InterAgentContext(
        session_store=agent_session_store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        caller_session_id=caller_session_id,
        caps=inter_agent.Caps(),
    )
    result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "investigate the sync timer", "model": "claude_code",
    })
    assert result["ok"] is True, result

    child = agent_session_store.get_by_session_id(result["child_session_id"])
    assert child is not None
    assert child.bot == "doctor"


async def test_spawn_model_claude_blocked_from_a_hermes_root(proxy_client, agent_session_store, tmp_path):
    """The spend guard (#640, extending #578/ADR-018): a Hermes-rooted
    session is not API-billed, so it may not open the model="claude" side
    door any more than a claude_code/codex root can."""
    from api.services.agent_worker import inter_agent
    from api.services.agent_worker.transcript_store import TranscriptStore

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-blocked-640"},
    )
    assert resp.status_code == 200
    caller_session_id = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]

    ctx = inter_agent.InterAgentContext(
        session_store=agent_session_store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        caller_session_id=caller_session_id,
        caps=inter_agent.Caps(),
    )
    result = inter_agent.dispatch(ctx, "lifeos_agent_spawn", {
        "prompt": "do some background work", "model": "claude",
    })

    assert result["ok"] is False
    assert result["error"] == "api_billing_blocked"


async def test_worker_spawned_on_one_turn_resolves_via_check_on_a_later_turn(
    proxy_client, agent_session_store, tmp_path,
):
    """Acceptance criterion: a session Hermes spawns doesn't vanish once its
    spawning turn ends — a later turn's lifeos_agent_check still finds it."""
    from api.services.agent_worker import inter_agent
    from api.services.agent_worker.transcript_store import TranscriptStore

    transcript = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    conv_payload = {"question": "spawn a worker", "conversation_id": "conv-later-640"}

    # Turn 1: resolve identity and spawn a child.
    resp1 = await proxy_client.post("/api/hermes/ask/stream", json=conv_payload)
    assert resp1.status_code == 200
    caller_turn_1 = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]
    ctx_turn_1 = inter_agent.InterAgentContext(
        session_store=agent_session_store, transcript_store=transcript,
        caller_session_id=caller_turn_1, caps=inter_agent.Caps(),
    )
    spawn_result = inter_agent.dispatch(ctx_turn_1, "lifeos_agent_spawn", {
        "prompt": "background task", "model": "claude_code",
    })
    assert spawn_result["ok"] is True, spawn_result
    child_session_id = spawn_result["child_session_id"]

    # Turn 2: same conversation, later request — same caller identity.
    resp2 = await proxy_client.post("/api/hermes/ask/stream", json=conv_payload)
    assert resp2.status_code == 200
    caller_turn_2 = json.loads(_received["body"])["lifeos_context"]["turn"]["caller_session_id"]
    assert caller_turn_2 == caller_turn_1

    ctx_turn_2 = inter_agent.InterAgentContext(
        session_store=agent_session_store, transcript_store=transcript,
        caller_session_id=caller_turn_2, caps=inter_agent.Caps(),
    )
    check_result = inter_agent.dispatch(ctx_turn_2, "lifeos_agent_check", {
        "session_id": child_session_id,
    })
    assert check_result["ok"] is True
    assert check_result["session_id"] == child_session_id


# ---------------------------------------------------------------------------
# Session-to-date cost (#610, extended with `session_cost_is_lower_bound`
# by #613) — `lifeos_context.turn` carries the verbatim sum of this
# conversation's already-recorded usage, never `persona` (which must stay
# cacheable/turn-invariant), scoped by the request's own `conversation_id`
# and excluding the in-flight turn.
# ---------------------------------------------------------------------------

async def test_session_cost_lands_in_turn_never_in_persona(proxy_client):
    """The cache-busting regression this issue exists to prevent: a
    cumulative, every-turn-changing figure in `persona` would invalidate a
    consumer's prompt cache on every single turn. `session_cost_is_lower_
    bound` (#613) is exactly as turn-variant as the other session fields --
    it's derived from the same per-conversation sum -- so it must land in
    `turn` alongside them, never in `persona`."""
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    ctx = json.loads(_received["body"])["lifeos_context"]

    assert "session_cost_usd" in ctx["turn"]
    assert "session_cost_is_lower_bound" in ctx["turn"]
    for key in ("session_cost_usd", "session_turn_count",
                "session_input_tokens", "session_output_tokens",
                "session_cost_is_lower_bound"):
        assert key not in ctx["persona"]


async def test_session_cost_sums_prior_turns_and_excludes_the_in_flight_one(proxy_client):
    """Seeded usage for this request's own `conversation_id` must be summed
    into the envelope -- and, since the stub backend below emits no `usage`
    event of its own, nothing from *this* turn is added on top, proving the
    figure reflects only turns already completed before this one started."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()  # per-test isolated singleton (conftest)
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-in-progress",
    )
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=200, output_tokens=80,
        cost_usd=0.0009, conversation_id="conv-in-progress",
    )

    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "what has this cost so far?", "conversation_id": "conv-in-progress"},
    )
    assert resp.status_code == 200
    turn = json.loads(_received["body"])["lifeos_context"]["turn"]

    assert turn["session_cost_usd"] == pytest.approx(0.002 + 0.0009)
    assert turn["session_input_tokens"] == 300
    assert turn["session_output_tokens"] == 130
    assert turn["session_turn_count"] == 2
    assert turn["session_cost_is_lower_bound"] is False


async def test_session_cost_fresh_conversation_is_zero_not_an_error(proxy_client):
    """No `conversation_id` on the request (the first turn of a brand-new
    conversation) reports the fields present and zero rather than omitting
    them or failing."""
    resp = await proxy_client.post("/api/hermes/ask/stream", json={"question": "hi"})
    assert resp.status_code == 200
    turn = json.loads(_received["body"])["lifeos_context"]["turn"]

    assert turn["session_cost_usd"] == 0.0
    assert turn["session_turn_count"] == 0
    assert turn["session_input_tokens"] == 0
    assert turn["session_output_tokens"] == 0
    assert turn["session_cost_is_lower_bound"] is False


async def test_session_cost_zero_cost_turn_still_reports_a_truthful_sum(proxy_client):
    """A conversation containing a turn recorded with cost_usd=0.0 and
    unpriced=False (genuinely free) must still report a truthful sum and
    turn count for the whole conversation, rather than erroring or
    silently dropping the zero-cost turn -- and must not be flagged as a
    lower bound, since nothing summed here is unpriced."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-mixed",
    )
    store.record_usage(
        model="some-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-mixed",
    )

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-mixed"},
    )
    assert resp.status_code == 200
    turn = json.loads(_received["body"])["lifeos_context"]["turn"]

    assert turn["session_cost_usd"] == pytest.approx(0.002)
    assert turn["session_input_tokens"] == 110
    assert turn["session_output_tokens"] == 60
    assert turn["session_turn_count"] == 2
    assert turn["session_cost_is_lower_bound"] is False


async def test_session_cost_unpriced_turn_marks_the_envelope_as_a_lower_bound(proxy_client):
    """#613: a conversation containing a turn recorded `unpriced=True` (its
    provider reported no cost) must surface `session_cost_is_lower_bound
    =True` in the envelope -- the real per-conversation distinction this
    field exists to carry to a consumer, replacing the unconditional-floor
    wording #610 originally shipped with."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-unpriced", unpriced=False,
    )
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-unpriced", unpriced=True,
    )

    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "conversation_id": "conv-unpriced"},
    )
    assert resp.status_code == 200
    turn = json.loads(_received["body"])["lifeos_context"]["turn"]

    assert turn["session_cost_usd"] == pytest.approx(0.002)
    assert turn["session_cost_is_lower_bound"] is True


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

    # #611: no `done` event ever arrived (the stub's stream just ends), so
    # this is now flagged as a genuine truncation -- the marker and
    # `routing.truncated` mean the browser can never mistake this cut-off
    # reply for a whole one, the same guarantee the native path gives a
    # cancelled/errored turn.
    messages = hermes_store.get_messages("trunc-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "will this finish?"),
        ("assistant", "partial reply\n\n_[cut off — the turn ended before it finished]_"),
    ]
    assert messages[-1].routing == {"truncated": True, "truncation_reason": "stream_error"}


stub_hermes_crlf = FastAPI()

# CRLF frame separators (`\r\n\r\n`) rather than bare LF — SSE permits both
# (WHATWG spec), and an intermediary is free to rewrite line endings even
# though the Hermes adapter itself emits LF today (#592 review: without
# handling this, `_FRAME_SEP` never matched and nothing was persisted —
# silent total data loss, not an error).
_CRLF_SSE_CHUNKS = [
    b'data: {"type": "conversation_id", "conversation_id": "crlf-1"}\r\n\r\n',
    b'data: {"type": "content", "content": "crlf reply"}\r\n\r\n',
    # A `done` event (#611: its absence is now the truncation signal — see
    # test_truncated_stream_still_persists_partial_content below) so this
    # CRLF-framing test isn't mistaken for a genuinely truncated turn; this
    # fixture predates #611 and was never about completion signaling.
    b'data: {"type": "done"}\r\n\r\n',
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
    """MAJOR (#592 review), STRENGTHENED by #611: `_proxy.py`'s relay loop
    used to call `observer.observe(chunk)` *after* `yield chunk`. Closing
    the response generator while it's suspended at that yield — exactly
    what an early client disconnect does — raised `GeneratorExit` right
    there, which skipped any code written after the yield in that same
    loop iteration, so the chunk already handed off was silently never
    observed. Fixed for #592 by observing before yielding.

    #611 goes further: the upstream drain is no longer the response
    generator's own loop at all. It's a registry-owned background pump
    that keeps draining upstream regardless of what the browser does, so a
    disconnect doesn't just fail to lose the LAST delivered chunk — it no
    longer stops the turn early at all. The invariant this test now pins is
    stronger: a disconnect never loses ANY observed chunk, including ones
    that hadn't reached the browser yet when it disconnected. That's why
    the persisted content below is the FULL relayed text ("first
    chunknever requested"), not just "first chunk".

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
        # A `done` event (#611: its absence is now the "this turn was cut
        # off" signal used by test_truncated_stream_still_persists_partial_content
        # below) — the detached pump drains this fully regardless of what
        # the reader below ever pulls, so this is what makes the persisted
        # reply come out unmarked, matching a turn that actually finished.
        b'data: {"type": "done"}\n\n',
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
    # yield" moment the finding describes. Unlike before #611, this detaches
    # the pump rather than stopping it -- await its turn's task to let it
    # actually finish draining upstream before asserting on what's persisted.
    await gen.aclose()

    from api.services.chat_turns import get_turn_registry
    turn = get_turn_registry().get_by_conversation("disco-1")
    assert turn is not None and turn.task is not None
    await turn.task

    conv = hermes_store.get_conversation("disco-1")
    assert conv is not None
    messages = hermes_store.get_messages("disco-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "will this survive a disconnect?"),
        ("assistant", "first chunknever requested"),
    ]


def _fake_upstream_and_client(gen_factory):
    """A minimal fake httpx client/response pair (mirrors the `_FakeClient`/
    `_FakeUpstream` pattern above and in test_agent_proxy.py) giving exact
    control over the SSE chunks and their timing, for tests that need to
    pause the upstream mid-turn to simulate a disconnect landing before it
    finishes."""
    class _FakeUpstream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            async for c in gen_factory():
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

    return _FakeClient


def _manual_request(body: dict):
    """A `Request` whose `receive()` delivers `body` once, then reports a
    disconnect on every subsequent call -- the same manual ASGI-scope
    construction `test_client_disconnect_still_persists_the_last_chunk`
    above uses, factored out for the two tests below."""
    raw_body = json.dumps(body).encode()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
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
    return Request(scope, receive=receive)


async def test_disconnect_then_completion_persists_full_reply_and_a_usage_row(
    monkeypatch, hermes_store, usage_store,
):
    """#611: a Hermes turn that survives a disconnect (as above) also gets
    its `usage` event captured once the pump finishes draining -- the
    money side of "the turn runs to completion" holds too, not just the
    text."""
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    hold = asyncio.Event()

    async def gen():
        yield b'data: {"type": "conversation_id", "conversation_id": "disco-usage-1"}\n\n'
        yield b'data: {"type": "content", "content": "partial "}\n\n'
        await hold.wait()
        yield b'data: {"type": "content", "content": "rest"}\n\n'
        yield b'data: {"type": "usage", "model": "m", "input_tokens": 5, "output_tokens": 7, "cost_usd": 0.002}\n\n'
        yield b'data: {"type": "done"}\n\n'

    monkeypatch.setattr(hp, "_client", lambda: _fake_upstream_and_client(gen)())

    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint
    request = _manual_request({"question": "will usage survive a disconnect?"})

    response = await endpoint(request)
    gen_iter = response.body_iterator
    await gen_iter.__anext__()  # conversation_id
    await gen_iter.__anext__()  # "partial "

    # Disconnect before "rest", the usage event, or done ever reach the browser.
    await gen_iter.aclose()
    hold.set()  # let the still-running pump past its pause

    turn = hp.get_turn_registry().get_by_conversation("disco-usage-1")
    assert turn is not None and turn.task is not None
    await turn.task

    messages = hermes_store.get_messages("disco-usage-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "will usage survive a disconnect?"),
        ("assistant", "partial rest"),
    ]
    stats = usage_store.get_usage_stats()
    assert stats["request_count"] == 1
    assert stats["total_input_tokens"] == 5
    assert stats["total_output_tokens"] == 7
    assert stats["total_cost"] == pytest.approx(0.002)


async def test_voice_modality_disconnect_detaches_the_pump_like_a_text_turn(
    monkeypatch, hermes_store,
):
    """#616: this test used to be named
    `test_voice_modality_disconnect_cancels_the_pump_rather_than_detaching`
    and asserted the OPPOSITE of what it asserts now -- that a
    voice-modality Hermes turn was cancelled by a disconnect rather than
    surviving it, because whisper-relay had no way to say "stop" other than
    abandoning the stream. Now that whisper-relay calls `POST
    /api/chat/cancel` with its `client_turn_id` on a real cancel gesture
    (whisper-relay#37), a disconnect alone no longer means "stop": a
    voice-modality turn relayed through Hermes detaches and keeps draining
    upstream to completion, exactly like the text turn in
    `test_client_disconnect_still_persists_the_last_chunk` above. This
    inversion is deliberate, not a weakening -- see #616."""
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    hold = asyncio.Event()

    async def gen():
        yield b'data: {"type": "conversation_id", "conversation_id": "voice-disco-1"}\n\n'
        yield b'data: {"type": "content", "content": "Hello "}\n\n'
        await hold.wait()
        yield b'data: {"type": "content", "content": "world"}\n\n'
        yield b'data: {"type": "done"}\n\n'

    monkeypatch.setattr(hp, "_client", lambda: _fake_upstream_and_client(gen)())

    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint
    request = _manual_request({"question": "hi", "modality": "voice"})

    response = await endpoint(request)
    gen_iter = response.body_iterator
    await gen_iter.__anext__()  # conversation_id
    await gen_iter.__anext__()  # "Hello "

    # The client disconnects (a hangup or network drop, NOT an explicit
    # cancel) -- the pump must detach and keep draining, same as a
    # text-modality turn.
    await gen_iter.aclose()
    hold.set()

    turn = hp.get_turn_registry().get_by_conversation("voice-disco-1")
    assert turn is not None and turn.modality == "voice"
    await turn.task  # must run to completion, never cancelled

    messages = hermes_store.get_messages("voice-disco-1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Hello world"),
    ]
    # A turn that ran to completion carries no truncation marker.
    assert not (messages[-1].routing or {}).get("truncated")


async def test_client_turn_id_cancel_halts_a_voice_barge_in_before_the_pumps_first_frame(
    monkeypatch, hermes_store,
):
    """#616 acceptance criterion, extended to the Hermes pump: a barge-in
    landing before the turn's first SSE frame -- before any conversation_id
    exists to cancel by -- must still halt generation via `client_turn_id`,
    the same first-turn barge-in gap #611 review closed for the native
    path (tests/test_chat_turn_cancel.py's `TestClientTurnIdCancel`)."""
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")

    hang_forever = asyncio.Event()

    async def gen():
        await hang_forever.wait()  # would hang forever if not cancelled
        yield b'data: {"type": "content", "content": "unreachable"}\n\n'

    monkeypatch.setattr(hp, "_client", lambda: _fake_upstream_and_client(gen)())

    endpoint = next(r for r in hp.router.routes if r.path.endswith("/ask/stream")).endpoint
    request = _manual_request({
        "question": "hi", "modality": "voice", "client_turn_id": "hermes-voice-barge-in-1",
    })

    await endpoint(request)
    # Deliberately never touch the response's body_iterator -- no SSE
    # frame, not even conversation_id, has been read. The gateway's only
    # handle is the client_turn_id it minted before sending.

    turn = hp.get_turn_registry().get_by_client_turn_id("hermes-voice-barge-in-1")
    assert turn is not None and turn.modality == "voice"
    assert turn.conversation_id is None  # not yet bound -- the whole point

    cancelled = hp.get_turn_registry().cancel_by_client_turn_id("hermes-voice-barge-in-1")
    assert cancelled is True

    with pytest.raises(asyncio.CancelledError):
        await turn.task


# ---------------------------------------------------------------------------
# Usage capture (#595) — the same read-only tee that persists a Hermes turn
# (#592, above) also captures its `usage` event, if any, and writes a usage
# row on `finalize()`. The relay's byte-identity guarantee applies equally
# here: capturing usage is observation, never a rewrite of what the browser
# receives.
# ---------------------------------------------------------------------------

stub_hermes_usage = FastAPI()

# cost_usd (0.00087) is deliberately *not* what the live pricing table
# (agent_worker/pricing.py's cost_for, #656) would compute for this
# unrecognized model -- its conservative Opus-rate fallback for these same
# token counts: (120/1e6)*15.0 + (340/1e6)*75.0 = 0.0273. Recording the
# wrong, upstream-reported number rather than that recomputed one is
# exactly the behavior under test — proof the store isn't quietly
# recalculating it.
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
            "SELECT model, input_tokens, output_tokens, cost_usd, conversation_id, unpriced FROM usage"
        ).fetchone()
    assert row is not None
    model, input_tokens, output_tokens, cost_usd, conversation_id, unpriced = row
    assert model == "deepseek-v3-fireworks"
    assert input_tokens == 120
    assert output_tokens == 340
    # Verbatim, not the ~0.00546 sonnet-fallback recompute — see the chunk
    # comment above.
    assert cost_usd == pytest.approx(0.00087)
    assert conversation_id == "usage-conv-1"
    # A real reported cost (#613) — not the "couldn't price this" case.
    assert unpriced == 0


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
    # #613: the row is flagged `unpriced` -- the zero above is "unknown",
    # not a real reported cost of zero.
    with sqlite3.connect(usage_store.db_path) as conn:
        (unpriced,) = conn.execute("SELECT unpriced FROM usage").fetchone()
    assert unpriced == 1


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
        # A `done` event (#611: its absence is now the "this turn was cut
        # off" signal — see test_truncated_stream_still_persists_partial_content
        # below) so this frame-reassembly test isn't mistaken for a
        # genuinely truncated turn; this fixture predates #611.
        persister.observe(b'data: {"type": "done"}\n\n')
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

        # A `done` event (#611) so this test isn't mistaken for a genuinely
        # truncated turn — see test_truncated_stream_still_persists_partial_content.
        persister.observe(b'data: {"type": "done"}\n\n')
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
        # A `done` event (#611) so this test isn't mistaken for a genuinely
        # truncated turn — see test_truncated_stream_still_persists_partial_content.
        persister.observe(b'data: {"type": "done"}\n\n')
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
                "unpriced": False,
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


# ---------------------------------------------------------------------------
# `POST /api/hermes/resolve-persona` (#644) — persona selection for Hermes's
# own Telegram front door, which never passes through the proxy above. Grammar
# tests exercise `_parse_persona_tag()` directly; endpoint tests exercise auth,
# the untagged/tagged/unrecognized-tag response contract, voice gating, and
# parity with `/api/hermes/ask/stream`'s envelope for the same persona.
# ---------------------------------------------------------------------------

class TestParsePersonaTag:
    """Grammar for the per-message `@name` prefix (Nathan's decision, no
    persisted state) — see the comment above `_PERSONA_TAG_RE` in
    hermes_proxy.py for the full rationale."""

    def test_recognizes_a_simple_tag(self):
        assert hp._parse_persona_tag("@doctor the sync timer looks wedged") == (
            "doctor", "the sync timer looks wedged",
        )

    def test_lowercases_the_tag(self):
        assert hp._parse_persona_tag("@DOCTOR hi") == ("doctor", "hi")

    def test_tag_with_no_trailing_text(self):
        assert hp._parse_persona_tag("@doctor") == ("doctor", "")

    def test_collapses_extra_whitespace_after_the_tag(self):
        assert hp._parse_persona_tag("@doctor   hi there") == ("doctor", "hi there")

    def test_plain_message_has_no_tag(self):
        assert hp._parse_persona_tag("just a normal message") == (None, "just a normal message")

    def test_leading_at_digit_is_not_a_tag(self):
        # "a message that merely begins with an @ is not necessarily a
        # persona tag" — no configured persona id starts with a digit.
        assert hp._parse_persona_tag("@3pm meeting reminder") == (None, "@3pm meeting reminder")

    def test_bare_at_with_space_is_not_a_tag(self):
        assert hp._parse_persona_tag("@ what's up") == (None, "@ what's up")

    def test_punctuation_glued_to_tag_is_not_a_tag(self):
        # Only the documented `@name <message>` form is recognized —
        # "@doctor," (no space before the comma) doesn't match it.
        assert hp._parse_persona_tag("@doctor, please check") == (None, "@doctor, please check")

    def test_unknown_tag_shaped_prefix_is_still_returned(self):
        # Deliberately NOT (None, text) — the caller must see this as an
        # attempted-but-unrecognized tag, not "no tag at all".
        assert hp._parse_persona_tag("@nonsense do a thing") == ("nonsense", "do a thing")


@pytest.fixture
def resolve_client(monkeypatch):
    """Same auth token as `proxy_client`, but only the resolve-persona route
    is needed — no upstream Hermes stub."""
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "secret-token")
    app = FastAPI()
    app.include_router(hp.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_resolve_persona_503_when_token_not_configured(monkeypatch):
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post(
            "/api/hermes/resolve-persona", json={"text": "hi"},
            headers={"Authorization": "Bearer whatever"},
        )
    assert resp.status_code == 503


async def test_resolve_persona_401_when_missing_bearer(resolve_client):
    resp = await resolve_client.post("/api/hermes/resolve-persona", json={"text": "hi"})
    assert resp.status_code == 401


async def test_resolve_persona_401_when_wrong_bearer(resolve_client):
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", json={"text": "hi"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def _auth(token="secret-token"):
    return {"Authorization": f"Bearer {token}"}


async def test_resolve_persona_untagged_is_unchanged(resolve_client):
    # "Given no persona selected, then behavior shall be unchanged from
    # today" (#644 AC) — text passes through byte-identical and there is no
    # envelope for Hermes to apply.
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", json={"text": "what's on my calendar today"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "persona_id": None,
        "text": "what's on my calendar today",
        "lifeos_context": None,
    }


async def test_resolve_persona_unrecognized_tag_is_a_400_not_silent(resolve_client):
    # A typo'd/unknown tag must be a deliberate error, never silently treated
    # as "no persona selected" (which would make the typo look like it worked).
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", json={"text": "@nonsense do a thing"},
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert "nonsense" in resp.json()["detail"]


async def test_resolve_persona_tagged_resolves_and_strips_prefix(resolve_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("FITNESS PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "label": "Fitness Coach", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", json={"text": "@fitness what's my workout today"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona_id"] == "fitness"
    assert body["text"] == "what's my workout today"
    ctx = body["lifeos_context"]
    # Same preamble /chat (via resolve_persona) would use for this persona —
    # the #644 AC in a nutshell.
    assert ctx["persona"]["id"] == "fitness"
    assert ctx["persona"]["label"] == "Fitness Coach"
    assert ctx["persona"]["preamble"] == "FITNESS PERSONA BODY"
    assert ctx["persona"]["preamble"] == hp.settings.resolve_persona("fitness", surface="hermes")


async def test_resolve_persona_matches_ask_stream_envelope_for_same_persona(
    resolve_client, proxy_client, tmp_path, monkeypatch,
):
    """The two entry points into persona resolution (`/resolve-persona` for
    Hermes-Telegram, `/ask/stream`'s envelope for the browser/voice-selected
    persona) must produce an IDENTICAL envelope for the same persona and
    conversation — proof they share `_resolve_lifeos_context()` rather than
    two implementations that happen to agree today."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from api.services import agent_system_prompt as asp

    fixed_now = datetime(2026, 8, 19, 9, 14, 22, tzinfo=ZoneInfo("America/New_York"))

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(asp, "datetime", _Frozen)

    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("---\nid: fitness\nvoice:\n  - terse\n---\n\nFITNESS BODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    conv_id = "11111111-1111-1111-1111-111111111111"

    resolve_resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness ready for today", "modality": "voice", "conversation_id": conv_id},
        headers=_auth(),
    )
    assert resolve_resp.status_code == 200
    resolved_ctx = resolve_resp.json()["lifeos_context"]

    ask_resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "ready for today", "persona_id": "fitness", "modality": "voice", "conversation_id": conv_id},
    )
    assert ask_resp.status_code == 200
    ask_ctx = json.loads(_received["body"])["lifeos_context"]

    assert resolved_ctx == ask_ctx


async def test_resolve_persona_voice_modality_applies_voice_rules(resolve_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("---\nid: fitness\nvoice:\n  - terse\n  - no emoji\n---\n\nBODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness plan", "modality": "voice"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["lifeos_context"]["persona"]["voice_rules"] == ["terse", "no emoji"]


async def test_resolve_persona_text_modality_gives_empty_voice_rules(resolve_client, tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("---\nid: fitness\nvoice:\n  - terse\n---\n\nBODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", json={"text": "@fitness plan"}, headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["lifeos_context"]["persona"]["voice_rules"] == []


async def test_resolve_persona_missing_text_field_is_400(resolve_client):
    resp = await resolve_client.post("/api/hermes/resolve-persona", json={}, headers=_auth())
    assert resp.status_code == 400


async def test_resolve_persona_malformed_json_400(resolve_client):
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona", content=b"{not valid json",
        headers={**_auth(), "content-type": "application/json"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Reply-thread persona inheritance (#644 follow-up) — the ordered rule:
# explicit @tag > inherited from reply-to > nothing. `resolve_client`,
# `_registry`, and `_auth` above are reused unchanged.
# ---------------------------------------------------------------------------

def _setup_fitness_persona(tmp_path, monkeypatch):
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("FITNESS PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "fitness", "label": "Fitness Coach", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")


def _setup_doctor_persona(tmp_path, monkeypatch):
    persona_file = tmp_path / "doctor.md"
    persona_file.write_text("DOCTOR PERSONA BODY")
    reg = _registry(tmp_path, [
        {"name": "doctor", "token_env": "TG_DOC", "persona_file": str(persona_file), "orchestrates": True},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_DOC", "tok")


async def test_reply_inherits_persona_from_tagged_message(resolve_client, tmp_path, monkeypatch):
    _setup_fitness_persona(tmp_path, monkeypatch)

    # First message tags fitness explicitly and is recorded under its own id.
    first = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness plan for today", "chat_id": "chat-1", "message_id": "msg-1"},
        headers=_auth(),
    )
    assert first.status_code == 200
    assert first.json()["persona_id"] == "fitness"

    # A reply to msg-1 with no tag inherits fitness -- text is untouched,
    # there was no prefix to strip.
    reply = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "what about tomorrow", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "msg-1",
        },
        headers=_auth(),
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["persona_id"] == "fitness"
    assert body["text"] == "what about tomorrow"
    assert body["lifeos_context"]["persona"]["id"] == "fitness"


async def test_explicit_tag_overrides_inherited_persona(resolve_client, tmp_path, monkeypatch):
    _setup_fitness_persona(tmp_path, monkeypatch)
    _setup_doctor_persona(tmp_path, monkeypatch)

    await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness plan for today", "chat_id": "chat-1", "message_id": "msg-1"},
        headers=_auth(),
    )

    # A reply that carries its OWN tag switches personas mid-thread rather
    # than inheriting fitness from msg-1.
    reply = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "@doctor unrelated question", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "msg-1",
        },
        headers=_auth(),
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["persona_id"] == "doctor"
    assert body["text"] == "unrelated question"


async def test_unknown_reply_to_id_falls_back_to_no_persona(resolve_client):
    # Never recorded (old thread, pruned mapping, predates this feature) --
    # not an error, just today's behavior.
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "continuing our chat", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "never-seen",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"persona_id": None, "text": "continuing our chat", "lifeos_context": None}


async def test_expired_reply_to_id_falls_back_to_no_persona(resolve_client, tmp_path, monkeypatch):
    _setup_fitness_persona(tmp_path, monkeypatch)
    import api.services.hermes_persona_thread_store as thread_store_mod

    fake_time = [1_000_000.0]
    monkeypatch.setattr(thread_store_mod.time, "time", lambda: fake_time[0])

    await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness plan", "chat_id": "chat-1", "message_id": "msg-1"},
        headers=_auth(),
    )
    fake_time[0] += thread_store_mod._TTL_SECONDS + 1

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "still there?", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "msg-1",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"persona_id": None, "text": "still there?", "lifeos_context": None}


async def test_reply_to_id_from_a_different_chat_does_not_collide(resolve_client, tmp_path, monkeypatch):
    _setup_fitness_persona(tmp_path, monkeypatch)

    await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@fitness plan", "chat_id": "chat-1", "message_id": "msg-1"},
        headers=_auth(),
    )
    # Same message_id, different chat -- must not inherit fitness from chat-1's msg-1.
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "hi there", "chat_id": "chat-2",
            "message_id": "msg-2", "reply_to_message_id": "msg-1",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["persona_id"] is None


async def test_reply_to_a_reply_inherits_transitively(resolve_client, tmp_path, monkeypatch):
    # A reply to a reply in a doctor thread is still doctor -- no special
    # case needed since every resolved message (tag or inherited) is
    # recorded under its own id.
    _setup_doctor_persona(tmp_path, monkeypatch)

    await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@doctor is the sync timer wedged", "chat_id": "chat-1", "message_id": "msg-1"},
        headers=_auth(),
    )
    await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "any update", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "msg-1",
        },
        headers=_auth(),
    )
    third = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "still nothing?", "chat_id": "chat-1",
            "message_id": "msg-3", "reply_to_message_id": "msg-2",
        },
        headers=_auth(),
    )
    assert third.status_code == 200
    assert third.json()["persona_id"] == "doctor"


async def test_reply_to_a_bot_authored_message_inherits(resolve_client, tmp_path, monkeypatch):
    # The thread anchor doesn't have to be a message this endpoint resolved
    # -- a bot reply registered via /register-persona-message works too.
    _setup_fitness_persona(tmp_path, monkeypatch)

    reg_resp = await resolve_client.post(
        "/api/hermes/register-persona-message",
        json={"chat_id": "chat-1", "message_id": "bot-msg-1", "persona_id": "fitness"},
        headers=_auth(),
    )
    assert reg_resp.status_code == 200
    assert reg_resp.json() == {"ok": True}

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "one more thing", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "bot-msg-1",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["persona_id"] == "fitness"


async def test_register_persona_message_rejects_unknown_persona(resolve_client):
    resp = await resolve_client.post(
        "/api/hermes/register-persona-message",
        json={"chat_id": "chat-1", "message_id": "bot-msg-1", "persona_id": "ghost"},
        headers=_auth(),
    )
    assert resp.status_code == 400


async def test_register_persona_message_requires_auth(resolve_client):
    resp = await resolve_client.post(
        "/api/hermes/register-persona-message",
        json={"chat_id": "chat-1", "message_id": "bot-msg-1", "persona_id": "primary"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Journal capture gate on the proxy relay path (#685). Before this, a
# journal-persona turn sent through `/api/hermes/ask/stream` — which `/chat`
# reaches by default whenever Hermes is available — was relayed to Hermes and
# never captured at all: #674's exact signature (a reply that reads like a
# successful capture and no file) resurrected on a surface nobody had
# checked. Mirrors tests/test_journal_capture.py's coverage of the native
# path: assertions land on the actual file on disk, not on a mock having
# been called — the gap that let #674 ship broken in the first place.
# ---------------------------------------------------------------------------

# Obviously synthetic fragment — nothing here resembles a real note.
_JOURNAL_FRAGMENT = "the deploy gate should fail closed #eng"


def _bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


@pytest.fixture
def journal_vault(tmp_path, monkeypatch) -> Path:
    """A throwaway vault for the proxy's journal-capture gate. Same
    redirect-and-assert pattern as tests/test_journal_capture.py's `vault`
    fixture, extended to this module's own `hp.settings` reference (the gate
    is reached through hermes_proxy.py, not just chat.py/journal_capture.py)
    since `config.settings` can be reloaded elsewhere in the suite, leaving a
    module that imported `settings` at import time holding a stale instance."""
    import api.services.journal_capture as journal_capture_mod
    import config.settings as settings_mod

    root = tmp_path / "vault"
    root.mkdir()
    seen = {}
    for obj in (settings_mod.settings, journal_capture_mod.settings, hp.settings):
        seen[id(obj)] = obj
    for obj in seen.values():
        monkeypatch.setattr(obj, "vault_path", root)
    assert journal_capture_mod.settings.vault_path == root
    return root


@pytest.fixture
def journal_persona_registered(tmp_path, monkeypatch) -> str:
    """Register a `journal` bot against the real persona file, the same way
    tests/test_journal_capture.py's `journal_persona` fixture does, so
    `persona_id="journal"` resolves on this proxy's envelope path too.
    Returns the resolved raw preamble — the shape `chat_via_api()` and the
    ring ingest actually send (`persona=...`, no `persona_id`), needed by
    the raw-preamble capture test below (#685 finding 1)."""
    reg = _registry(tmp_path, [
        {"name": "journal", "token_env": "TG_JOURNAL_TEST", "persona_file": "config/personas/journal.md"},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_JOURNAL_TEST", "tok")
    preamble = hp.settings.resolve_persona("journal")
    assert preamble, "journal persona did not resolve"
    return preamble


def _sse_events(resp) -> list[dict]:
    return [
        json.loads(chunk[len("data: "):])
        for chunk in resp.text.split("\n\n")
        if chunk.startswith("data: ")
    ]


async def test_journal_persona_writes_fragment_before_hermes_relay(
    proxy_client, journal_vault, journal_persona_registered,
):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
    )
    assert resp.status_code == 200

    log = journal_vault / log_path_for(date.today())
    assert log.exists(), "the fragment never reached disk"
    written = log.read_text()
    assert written.startswith(f"---\ntype: log\ndate: {date.today().isoformat()}\n---\n")
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]

    # The turn was still relayed to Hermes — a captured fragment doesn't
    # short-circuit the proxy, it just has to land first (the failure-path
    # test below proves that ordering structurally: on a capture failure,
    # Hermes is never contacted at all).
    assert json.loads(_received["body"])["question"] == _JOURNAL_FRAGMENT


async def test_journal_capture_sse_event_matches_native_shape(
    proxy_client, journal_vault, journal_persona_registered,
):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)

    # Same event shape ask_stream() emits natively (api/routes/chat.py) —
    # chat_via_api() and the ring ingest (api/routes/journal_ingest.py)
    # require this exact event as proof of capture on either path alike.
    capture_events = [e for e in events if e.get("type") == "journal_capture"]
    assert capture_events == [{
        "type": "journal_capture",
        "path": log_path_for(date.today()),
        "created": True,
    }]
    # It's the first frame the browser sees — ahead of anything Hermes
    # itself streamed for this turn.
    assert events[0]["type"] == "journal_capture"


async def test_second_fragment_of_the_day_appends_and_created_is_false(
    proxy_client, journal_vault, journal_persona_registered,
):
    first = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
    )
    assert [e for e in _sse_events(first) if e.get("type") == "journal_capture"][0]["created"] is True

    second = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "a second synthetic fragment", "persona_id": "journal"},
    )
    assert [e for e in _sse_events(second) if e.get("type") == "journal_capture"][0]["created"] is False

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert written.count("type: log") == 1
    assert [b.split(" · ")[1] for b in _bullets(written)] == [
        _JOURNAL_FRAGMENT, "a second synthetic fragment",
    ]


async def test_capture_failure_fails_the_request_and_never_reaches_hermes(
    proxy_client, journal_vault, journal_persona_registered,
):
    # Same "day dir exists as a file" trick tests/test_journal_capture.py's
    # equivalent native-path test (test_capture_failure_is_a_clean_error_and_
    # no_stream) uses to force a real write failure out of capture_fragment().
    (journal_vault / "Personal").mkdir()
    (journal_vault / "Personal" / "Log").write_text("not a directory")

    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
    )
    assert resp.status_code == 500
    # No false success, and the reply a user sees must not quote what they
    # said.
    assert _JOURNAL_FRAGMENT not in resp.text
    # Hermes was never contacted — no false success is even possible, and
    # whatever idempotency key a caller (e.g. the ring ingest) tracks for
    # this delivery stays unburned since the whole request failed before any
    # relay began.
    assert _received == {}


async def test_non_journal_persona_proxy_behavior_unchanged(proxy_client, journal_vault):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "what did I do last week?"},
    )
    assert resp.status_code == 200
    assert resp.content == b"".join(_UPSTREAM_SSE_CHUNKS)
    assert not any(e.get("type") == "journal_capture" for e in _sse_events(resp))
    assert not (journal_vault / "Personal").exists()
    assert json.loads(_received["body"])["question"] == "what did I do last week?"


# ---------------------------------------------------------------------------
# Adversarial-review follow-up (#685, finding 1): the journal-capture gate's
# effective-persona resolution must match ask_stream()'s native semantics —
# reverse-mapping a raw `persona` preamble (the exact shape chat_via_api()
# and the ring ingest send, api/routes/journal_ingest.py:172), and rejecting
# the same malformed persona_id/persona shapes with the same 400 — not
# approximate them as `persona_id or "primary"` alone.
# ---------------------------------------------------------------------------

async def test_raw_persona_preamble_journal_turn_still_captures(
    proxy_client, journal_vault, journal_persona_registered,
):
    """The shape chat_via_api()/the ring ingest actually send: a raw
    `persona` preamble, no `persona_id` at all. #684 is what's expected to
    point those callers at this proxy — if the prelude only ever looked at
    `persona_id`, this turn would silently stop being captured the moment
    that happens, #674's bug a third time."""
    journal_preamble = journal_persona_registered
    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": _JOURNAL_FRAGMENT, "persona": journal_preamble},
    )
    assert resp.status_code == 200

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]
    assert [e for e in _sse_events(resp) if e.get("type") == "journal_capture"] == [{
        "type": "journal_capture",
        "path": log_path_for(date.today()),
        "created": True,
    }]


async def test_empty_persona_id_gets_native_400_on_proxy(proxy_client, journal_vault):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream", json={"question": "hi", "persona_id": ""},
    )
    assert resp.status_code == 400
    assert _received == {}  # rejected before the upstream backend was ever called


async def test_persona_and_persona_id_conflict_gets_native_400_on_proxy(proxy_client, journal_vault):
    resp = await proxy_client.post(
        "/api/hermes/ask/stream",
        json={"question": "hi", "persona": "X", "persona_id": "primary"},
    )
    assert resp.status_code == 400
    assert _received == {}


# ---------------------------------------------------------------------------
# Adversarial-review follow-up (#685, finding 2): once capture has already
# succeeded, its proof must reach the caller regardless of what Hermes then
# does — chat_via_api() (api/services/telegram.py) and the ring ingest
# (api/routes/journal_ingest.py) both treat a non-200 response as "nothing
# happened" and never look at its body for a `journal_capture` event.
# ---------------------------------------------------------------------------

async def test_journal_capture_proof_survives_hermes_connect_failure(
    journal_vault, journal_persona_registered, monkeypatch,
):
    class _Refusing:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            raise httpx.ConnectError("refused")

        async def aclose(self):
            pass

    monkeypatch.setattr(hp, "_client", _Refusing)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post(
            "/api/hermes/ask/stream",
            json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
        )

    # NOT a 502 — a caller that error-branches on status code alone (both
    # real callers do) must still be able to see the capture proof.
    assert resp.status_code == 200
    events = _sse_events(resp)
    assert events[0] == {
        "type": "journal_capture",
        "path": log_path_for(date.today()),
        "created": True,
    }
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    # Same {"type": "error", "message": ...} shape api/routes/chat.py's
    # native path already emits, which chat_via_api() already knows to fold
    # into its answer rather than choke on.
    assert "unreachable" in error_events[0]["message"]

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]


async def test_journal_capture_proof_survives_hermes_non_200(
    journal_vault, journal_persona_registered, monkeypatch,
):
    # Reuses the module's existing `_stub_500` app (already used by
    # test_non_200_upstream_is_passed_through above) rather than a second
    # near-identical stub.
    monkeypatch.setattr(
        hp, "_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_stub_500), base_url="http://a"),
    )
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://a")
    monkeypatch.setattr(hp.settings, "hermes_backend_token", "")
    app = FastAPI()
    app.include_router(hp.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        resp = await c.post(
            "/api/hermes/ask/stream",
            json={"question": _JOURNAL_FRAGMENT, "persona_id": "journal"},
        )

    assert resp.status_code == 200
    events = _sse_events(resp)
    assert events[0]["type"] == "journal_capture"
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert "500" in error_events[0]["message"]

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]


@pytest.fixture
def journal_ingest_via_proxy(tmp_path, monkeypatch, journal_vault, journal_persona_registered):
    """`POST /api/journal/ingest` wired to a `chat_via_api`-shaped bridge that
    posts through the Hermes PROXY instead of native `/api/ask/stream` — the
    wiring #684 is expected to give `chat_via_api()` itself once it points
    the journal bot at Hermes. Exercises the REAL, unmocked ring-ingest
    dedupe logic (api/routes/journal_ingest.py) on top of the guaranteed-
    prelude-delivery fix (#685 finding 2): a capture that succeeds while
    Hermes then fails must still get the delivery's idempotency key burned,
    or a genuine retry re-invokes `capture_fragment()` a second time for the
    same delivery — the double-append the adversarial review flagged.

    Hermes is unreachable for every call in this fixture — the point being
    proven is that capture (and the ring ingest's success/dedupe
    bookkeeping) never depended on Hermes working in the first place.
    """
    import api.routes.journal_ingest as journal_ingest
    import api.services.journal_ingest_store as journal_ingest_store
    from api.services.journal_ingest_store import JournalIngestStore

    store = JournalIngestStore(db_path=str(tmp_path / "journal_ingest.db"))
    monkeypatch.setattr(journal_ingest_store, "_store_instance", store)
    journal_ingest._conversations.clear()
    monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "secret-token")

    class _Refusing:
        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            raise httpx.ConnectError("refused")

        async def aclose(self):
            pass

    monkeypatch.setattr(hp, "_client", _Refusing)
    monkeypatch.setattr(hp.settings, "hermes_backend_url", "http://hermes")

    proxy_app = FastAPI()
    proxy_app.include_router(hp.router)
    proxy_transport = httpx.ASGITransport(app=proxy_app)

    async def chat_via_proxy(question, conversation_id=None, persona=None):
        # Mirrors api.services.telegram.chat_via_api()'s SSE-parsing loop
        # verbatim, posting through the Hermes proxy instead of native
        # /api/ask/stream — see the fixture docstring.
        body = {"question": question}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if persona:
            body["persona"] = persona
        full_text = ""
        conv_id = conversation_id
        journal_capture = None
        async with httpx.AsyncClient(transport=proxy_transport, base_url="http://proxy") as c:
            async with c.stream("POST", "/api/hermes/ask/stream", json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    raise RuntimeError(f"Chat pipeline returned HTTP {resp.status_code}: {error_body[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[len("data: "):])
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "content":
                        full_text += event.get("content", "")
                    elif etype == "conversation_id":
                        conv_id = event.get("conversation_id", conv_id)
                    elif etype == "journal_capture":
                        journal_capture = {"path": event.get("path"), "created": bool(event.get("created"))}
                    elif etype == "error":
                        msg = event.get("message", "Unknown error")
                        full_text += f"\n\nError: {msg}" if full_text else f"Error: {msg}"
        return {"answer": full_text, "conversation_id": conv_id, "journal_capture": journal_capture}

    monkeypatch.setattr("api.services.telegram.chat_via_api", chat_via_proxy)

    ingest_app = FastAPI()
    ingest_app.include_router(journal_ingest.router)
    return TestClient(ingest_app), store


def _journal_ingest_payload(**overrides):
    payload = {
        "text": _JOURNAL_FRAGMENT,
        "device_id": "ring-test-1",
        "timestamp": "2026-08-23T14:37:00Z",
    }
    payload.update(overrides)
    return payload


def _journal_ingest_auth():
    return {"Authorization": "Bearer secret-token"}


def test_capture_proof_survives_hermes_failure_and_retry_does_not_double_append(
    journal_ingest_via_proxy, journal_vault,
):
    client, store = journal_ingest_via_proxy

    first = client.post(
        "/api/journal/ingest", json=_journal_ingest_payload(), headers=_journal_ingest_auth(),
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "logged"

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]

    # A genuine retry of the SAME delivery — Hermes is STILL unreachable,
    # but the dedupe key was already burned on the first attempt (its
    # capture proof reached the ring ingest despite Hermes failing), so this
    # must short-circuit as a duplicate rather than append a second bullet.
    second = client.post(
        "/api/journal/ingest", json=_journal_ingest_payload(), headers=_journal_ingest_auth(),
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"

    written = (journal_vault / log_path_for(date.today())).read_text()
    assert [b.split(" · ")[1] for b in _bullets(written)] == [_JOURNAL_FRAGMENT]


# ---------------------------------------------------------------------------
# Adversarial-review follow-up (#685, finding 3): `/resolve-persona` resolves
# a persona WITHOUT running a turn, so there is no ask/stream turn here for
# the journal-capture gate to hook — `journal` must be refused visibly
# instead of silently resolving into a phantom "Logged." reply with nothing
# on disk, whether it arrived via an explicit tag (rule 1) or thread
# inheritance (rule 2).
# ---------------------------------------------------------------------------

async def test_resolve_persona_rejects_tagged_journal(resolve_client, journal_persona_registered):
    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={"text": "@journal had a thought"},
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert "journal" in resp.json()["detail"].lower()


async def test_resolve_persona_rejects_inherited_journal(
    resolve_client, journal_persona_registered,
):
    first = await resolve_client.post(
        "/api/hermes/register-persona-message",
        json={"chat_id": "chat-1", "message_id": "bot-msg-1", "persona_id": "journal"},
        headers=_auth(),
    )
    assert first.status_code == 200

    resp = await resolve_client.post(
        "/api/hermes/resolve-persona",
        json={
            "text": "one more thing", "chat_id": "chat-1",
            "message_id": "msg-2", "reply_to_message_id": "bot-msg-1",
        },
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert "journal" in resp.json()["detail"].lower()
