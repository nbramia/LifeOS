"""
Tests for the journal ring ingestion endpoint (#660).

POST /api/journal/ingest lets a capture device (e.g. the Pebble Index ring)
feed transcribed fragments into the `journal` persona built in #659. The
underlying chat pipeline (`api.services.telegram.chat_via_api`) is mocked
throughout — these tests cover the endpoint's own contract (auth, payload
adapter, idempotency, clean failure) and that it calls into the pipeline the
same way the journal Telegram bot does, not the persona's LLM behavior
itself (which #659's own tests already note isn't unit-testable without a
model). Never touches the real vault or a real chat pipeline.

The mock returns `journal_capture` because since #674 the pipeline reports
back that the fragment reached disk, and this endpoint refuses to call a
delivery `logged` (or burn its dedupe key) without that confirmation — a
mocked pipeline asserting only that it was *called* is precisely what let
#674 ship. The tests that exercise a real capture, file content and all, are
in tests/test_journal_capture.py.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.journal_ingest as journal_ingest
import api.services.journal_ingest_store as journal_ingest_store
from api.services.journal_ingest_store import JournalIngestStore

pytestmark = pytest.mark.unit


def _captured_result(conversation_id="conv-1"):
    """What the chat pipeline returns for a journal turn since #674: a reply
    AND proof the fragment is on disk."""
    return {
        "answer": "Logged.",
        "conversation_id": conversation_id,
        "claude_intent": False,
        "task": None,
        "journal_capture": {"path": "Personal/Log/2026-08-23.md", "created": True},
    }


def _payload(**overrides):
    payload = {
        "text": "call a friend this week",
        "device_id": "ring-test-1",
        "timestamp": "2026-08-23T14:37:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = JournalIngestStore(db_path=str(tmp_path / "journal_ingest.db"))
    monkeypatch.setattr(journal_ingest_store, "_store_instance", store)
    journal_ingest._conversations.clear()

    monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "secret-token")
    # `settings` is a pydantic model — instance attributes must be declared
    # fields, so a method override goes on the class, not the instance.
    monkeypatch.setattr(
        type(journal_ingest.settings), "resolve_persona",
        lambda self, persona_id, surface=None: "JOURNAL PERSONA PREAMBLE" if persona_id == "journal" else None,
    )

    calls = []

    async def fake_chat_via_api(question, conversation_id=None, persona=None):
        calls.append({"question": question, "conversation_id": conversation_id, "persona": persona})
        return _captured_result()

    monkeypatch.setattr("api.services.telegram.chat_via_api", fake_chat_via_api)

    app = FastAPI()
    app.include_router(journal_ingest.router)
    client = TestClient(app)
    return client, store, calls, monkeypatch


def _auth():
    return {"Authorization": "Bearer secret-token"}


class TestAuth:
    def test_disabled_without_token(self, env):
        client, _, calls, monkeypatch = env
        monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "")
        resp = client.post("/api/journal/ingest", json=_payload())
        assert resp.status_code == 503
        assert calls == []

    def test_missing_bearer_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json=_payload())
        assert resp.status_code == 401
        assert calls == []

    def test_wrong_token_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post(
            "/api/journal/ingest", json=_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        assert calls == []

    def test_unauthenticated_post_writes_nothing(self, env):
        client, store, calls, _ = env
        resp = client.post("/api/journal/ingest", json=_payload())
        assert resp.status_code == 401
        assert calls == []
        assert not store.was_processed("hash:anything")

    def test_auth_runs_before_body_validation(self, env):
        # Malformed body + no auth must hit the auth gate, never 422 leaking
        # field-level validation to an unauthenticated caller.
        client, _, calls, monkeypatch = env
        monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "")
        resp = client.post("/api/journal/ingest", json={"text": 123})
        assert resp.status_code == 503

        monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "secret-token")
        resp = client.post("/api/journal/ingest", json={"text": 123})
        assert resp.status_code == 401
        assert calls == []


class TestCapture:
    def test_valid_post_captures_exactly_as_typed_fragment(self, env):
        """Same call the journal Telegram bot makes for a typed message:
        chat_via_api(text, conversation_id=..., persona=<journal preamble>)."""
        client, store, calls, _ = env
        resp = client.post("/api/journal/ingest", json=_payload(text="idea about the deploy gate"), headers=_auth())
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "logged"

        assert len(calls) == 1
        assert calls[0]["question"] == "idea about the deploy gate"
        assert calls[0]["persona"] == "JOURNAL PERSONA PREAMBLE"

    def test_action_field_ignored(self, env):
        """The device's on-device LLM may pick an 'action' — never honored;
        the fragment sent to the pipeline carries only the text."""
        client, _, calls, _ = env
        resp = client.post(
            "/api/journal/ingest",
            json=_payload(text="add reminder to call mum", action="create_reminder"),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert calls[0]["question"] == "add reminder to call mum"

    def test_conversation_continuity_per_device(self, env):
        client, _, calls, _ = env
        client.post("/api/journal/ingest", json=_payload(device_id="ring-A", timestamp="2026-08-23T09:00:00Z", text="first"), headers=_auth())
        client.post("/api/journal/ingest", json=_payload(device_id="ring-A", timestamp="2026-08-23T09:05:00Z", text="second"), headers=_auth())
        assert calls[0]["conversation_id"] is None
        assert calls[1]["conversation_id"] == "conv-1"  # from fake_chat_via_api's fixed return


class TestIdempotency:
    def test_duplicate_payload_logged_once(self, env):
        client, _, calls, _ = env
        payload = _payload(text="same fragment twice")
        r1 = client.post("/api/journal/ingest", json=payload, headers=_auth())
        r2 = client.post("/api/journal/ingest", json=payload, headers=_auth())
        assert r1.status_code == 200 and r1.json()["status"] == "logged"
        assert r2.status_code == 200 and r2.json()["status"] == "duplicate"
        assert len(calls) == 1

    def test_duplicate_by_derived_key_ignores_incidental_field_order(self, env):
        # Same device/timestamp/text -> same derived key, even without an id.
        client, _, calls, _ = env
        client.post("/api/journal/ingest", json=_payload(text="derived key dedupe"), headers=_auth())
        r2 = client.post("/api/journal/ingest", json=_payload(text="derived key dedupe"), headers=_auth())
        assert r2.json()["status"] == "duplicate"
        assert len(calls) == 1

    def test_explicit_id_preferred_over_derived_key(self, env):
        # Device-supplied id wins even if timestamp differs slightly (e.g. a
        # retry that re-stamps "now" on resend) — same id must still collapse.
        client, _, calls, _ = env
        client.post(
            "/api/journal/ingest",
            json=_payload(id="ring-msg-42", timestamp="2026-08-23T14:37:00Z"),
            headers=_auth(),
        )
        r2 = client.post(
            "/api/journal/ingest",
            json=_payload(id="ring-msg-42", timestamp="2026-08-23T14:37:05Z"),
            headers=_auth(),
        )
        assert r2.json()["status"] == "duplicate"
        assert len(calls) == 1

    def test_different_fragments_both_processed(self, env):
        client, _, calls, _ = env
        client.post("/api/journal/ingest", json=_payload(text="fragment one"), headers=_auth())
        client.post("/api/journal/ingest", json=_payload(text="fragment two", timestamp="2026-08-23T14:38:00Z"), headers=_auth())
        assert len(calls) == 2

    def test_only_derived_hash_persisted_never_raw_text(self, env):
        client, store, _, _ = env
        client.post("/api/journal/ingest", json=_payload(text="a very personal thought"), headers=_auth())
        conn = __import__("sqlite3").connect(store.db_path)
        rows = conn.execute("SELECT dedupe_key FROM processed_ingests").fetchall()
        conn.close()
        assert len(rows) == 1
        assert "a very personal thought" not in rows[0][0]

    def test_pipeline_failure_not_marked_processed_can_retry(self, env):
        client, store, calls, monkeypatch = env

        async def failing_chat_via_api(question, conversation_id=None, persona=None):
            raise RuntimeError("boom")

        monkeypatch.setattr("api.services.telegram.chat_via_api", failing_chat_via_api)
        payload = _payload(text="will fail then succeed")
        resp = client.post("/api/journal/ingest", json=payload, headers=_auth())
        assert resp.status_code == 502

        async def fake_chat_via_api(question, conversation_id=None, persona=None):
            calls.append(question)
            return _captured_result(conversation_id="conv-2")

        monkeypatch.setattr("api.services.telegram.chat_via_api", fake_chat_via_api)
        resp = client.post("/api/journal/ingest", json=payload, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["status"] == "logged"
        assert calls == ["will fail then succeed"]


class TestMalformedPayload:
    def test_invalid_json_body(self, env):
        client, _, calls, _ = env
        resp = client.post(
            "/api/journal/ingest", content=b"not json",
            headers={**_auth(), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert calls == []

    def test_missing_text_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json={"device_id": "d1", "timestamp": "2026-08-23T00:00:00Z"}, headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_empty_text_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json=_payload(text="   "), headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_missing_device_id_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json={"text": "x", "timestamp": "2026-08-23T00:00:00Z"}, headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_missing_timestamp_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json={"text": "x", "device_id": "d1"}, headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_malformed_timestamp_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json=_payload(timestamp="not-a-timestamp"), headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_non_object_body_rejected(self, env):
        client, _, calls, _ = env
        resp = client.post("/api/journal/ingest", json=["not", "an", "object"], headers=_auth())
        assert resp.status_code == 422
        assert calls == []

    def test_malformed_payload_writes_nothing(self, env):
        client, store, calls, _ = env
        client.post("/api/journal/ingest", json={"device_id": "d1"}, headers=_auth())
        assert calls == []
        import sqlite3
        conn = sqlite3.connect(store.db_path)
        count = conn.execute("SELECT COUNT(*) FROM processed_ingests").fetchone()[0]
        conn.close()
        assert count == 0


class TestPersonaUnconfigured:
    def test_journal_persona_not_configured_returns_503(self, env):
        client, _, calls, monkeypatch = env
        monkeypatch.setattr(type(journal_ingest.settings), "resolve_persona", lambda self, persona_id, surface=None: None)
        resp = client.post("/api/journal/ingest", json=_payload(), headers=_auth())
        assert resp.status_code == 503
        assert calls == []
