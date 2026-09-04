"""Tests for HermesExecutor (#851, AC4): a board-assigned #hermes task opens
a Hermes conversation via the configured backend, the turn's final text
lands on the outcome (worker.py hands it to the Agent Output note the same
way every other executor's final_text does), the session records
routing='hermes' + the conversation id, and the card's open_url can point
at `/chat?conversation=<id>`. Stubs the backend entirely — no network call.
"""
from __future__ import annotations

import json

import httpx
import pytest

from api.routes import hermes_proxy as hp
from api.services.agent_worker.hermes_executor import HermesExecutor
from api.services.agent_worker.session_store import STATUS_COMPLETED, STATUS_FAILED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.conversation_store import ConversationStore
from api.services.usage_store import UsageStore


pytestmark = pytest.mark.unit


def _sse(events: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


class _FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", "http://backend/api/ask/stream"),
                response=httpx.Response(self.status_code),
            )

    def iter_bytes(self):
        yield self._body


class _FakeStreamCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *args):
        return False


class _FakeClient:
    def __init__(self, body: bytes, status_code: int = 200, captured: dict | None = None):
        self._body = body
        self._status_code = status_code
        self.captured = captured if captured is not None else {}

    def stream(self, method, url, content=None, headers=None):
        self.captured["method"] = method
        self.captured["url"] = url
        self.captured["content"] = content
        self.captured["headers"] = headers
        return _FakeStreamCM(_FakeResponse(self._body, self._status_code))

    def close(self):
        pass


def _build(tmp_path, monkeypatch, *, body: bytes, status_code: int = 200,
           backend_url: str = "http://hermes-backend.example"):
    from config.settings import settings
    monkeypatch.setattr(settings, "hermes_backend_url", backend_url, raising=False)
    monkeypatch.setattr(settings, "hermes_backend_token", "test-token", raising=False)

    # _HermesTurnPersister.finalize() (called via HermesExecutor, which
    # imports it from api.routes.hermes_proxy) resolves the conversation/
    # usage stores through THIS module's own `get_store`/`get_usage_store`
    # names — patch them here (not on conversation_store/usage_store
    # directly) so a real turn's persistence never touches the operator's
    # actual data/ directory. Mirrors test_hermes_proxy.py's own pattern.
    conv_store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage_store = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hp, "get_store", lambda: conv_store)
    monkeypatch.setattr(hp, "get_usage_store", lambda: usage_store)
    monkeypatch.setattr(hp, "schedule_retitle", lambda conv_id: None)

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    captured: dict = {}
    factory = lambda: _FakeClient(body, status_code, captured)  # noqa: E731
    executor = HermesExecutor(
        session_store=store, transcript_store=transcripts, http_client_factory=factory,
    )
    session = store.create(task_id="t1", routing="hermes")
    return executor, store, session, captured


def test_happy_path_completes_and_records_conversation_id(tmp_path, monkeypatch):
    body = _sse([
        {"type": "conversation_id", "conversation_id": "conv-abc123"},
        {"type": "content", "content": "Here is your schedule."},
        {"type": "usage", "model": "gpt-5.5", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.002},
        {"type": "done"},
    ])
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=body)
    outcome = executor.execute(session, {"description": "What's on my schedule today?"})
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "Here is your schedule."
    refreshed = store.get("t1")
    assert refreshed.conversation_id == "conv-abc123"
    # Envelope + bearer token present.
    assert captured["url"].endswith("/api/ask/stream")
    assert captured["headers"]["Authorization"] == "Bearer test-token"


def test_prompt_combines_title_and_notes(tmp_path, monkeypatch):
    body = _sse([
        {"type": "conversation_id", "conversation_id": "conv-1"},
        {"type": "content", "content": "ack"},
        {"type": "done"},
    ])
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=body)
    executor.execute(session, {"description": "Book dinner", "notes": "somewhere quiet, 7pm"})
    sent_body = json.loads(captured["content"])
    assert "Book dinner" in sent_body["question"]
    assert "somewhere quiet, 7pm" in sent_body["question"]


def test_empty_prompt_fails_without_request(tmp_path, monkeypatch):
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=b"")
    outcome = executor.execute(session, {"description": "   "})
    assert outcome.status == STATUS_FAILED
    assert captured == {}  # no HTTP call made


def test_backend_not_configured_fails_without_request(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "hermes_backend_url", "", raising=False)
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    calls = []
    executor = HermesExecutor(
        session_store=store, transcript_store=transcripts,
        http_client_factory=lambda: calls.append(1) or _FakeClient(b""),
    )
    session = store.create(task_id="t1", routing="hermes")
    outcome = executor.execute(session, {"description": "hello"})
    assert outcome.status == STATUS_FAILED
    assert "not configured" in outcome.reason
    assert calls == []


def test_truncated_stream_without_done_still_fails_gracefully(tmp_path, monkeypatch):
    """No conversation_id/content at all (e.g. the connection dropped
    immediately) -> FAILED with a clear reason, not a crash."""
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=b"")
    outcome = executor.execute(session, {"description": "hello"})
    assert outcome.status == STATUS_FAILED


def test_http_error_fails_gracefully(tmp_path, monkeypatch):
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=b"", status_code=500)
    outcome = executor.execute(session, {"description": "hello"})
    assert outcome.status == STATUS_FAILED
    assert "hermes request failed" in outcome.reason


# ---------------------------------------------------------------------------
# #892 — HermesExecutor is the only writer of Session.hermes_model. It must
# record the model Hermes reported for THIS session's OWN turn, on both the
# normal-completion exit path and the failure exit path (a dropped
# connection after usage was reported still means the turn ran on that
# model).
# ---------------------------------------------------------------------------


def test_completed_turn_records_the_reported_model_on_its_own_session(tmp_path, monkeypatch):
    body = _sse([
        {"type": "conversation_id", "conversation_id": "conv-model-1"},
        {"type": "content", "content": "here you go"},
        {"type": "usage", "model": "gpt-5.5", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.002},
        {"type": "done"},
    ])
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=body)
    outcome = executor.execute(session, {"description": "hi"})
    assert outcome.status == STATUS_COMPLETED
    assert store.get("t1").hermes_model == "gpt-5.5"


def test_no_usage_event_leaves_hermes_model_null(tmp_path, monkeypatch):
    """A turn whose upstream never sent a well-formed `usage` event (e.g.
    the malformed-event case `_HermesTurnPersister._handle_usage` already
    drops) must not write a bogus/empty value onto the session."""
    body = _sse([
        {"type": "conversation_id", "conversation_id": "conv-model-2"},
        {"type": "content", "content": "ack"},
        {"type": "done"},
    ])
    executor, store, session, captured = _build(tmp_path, monkeypatch, body=body)
    outcome = executor.execute(session, {"description": "hi"})
    assert outcome.status == STATUS_COMPLETED
    assert store.get("t1").hermes_model is None


def test_failed_turn_after_usage_event_still_records_the_reported_model(tmp_path, monkeypatch):
    """(#892) The upstream connection can drop AFTER Hermes's own `usage`
    event already arrived (simulated here as an `iter_bytes()` that yields
    the usage frame, then raises mid-stream, exactly like a genuine
    connection error). The turn still ran on that model, so it must still
    be recorded even though the executor's outcome is FAILED — dropping it
    would be less honest, not more (design.md, #892)."""
    usage_frame = _sse([
        {"type": "conversation_id", "conversation_id": "conv-model-3"},
        {"type": "usage", "model": "drop-after-usage-model", "input_tokens": 3, "output_tokens": 2, "cost_usd": 0.001},
    ])

    class _DropAfterUsageResponse(_FakeResponse):
        def iter_bytes(self):
            yield self._body
            raise httpx.ReadError("connection dropped mid-stream")

    class _DropAfterUsageClient(_FakeClient):
        def stream(self, method, url, content=None, headers=None):
            self.captured["method"] = method
            self.captured["url"] = url
            self.captured["content"] = content
            self.captured["headers"] = headers
            return _FakeStreamCM(_DropAfterUsageResponse(self._body))

    from config.settings import settings
    monkeypatch.setattr(settings, "hermes_backend_url", "http://hermes-backend.example", raising=False)
    monkeypatch.setattr(settings, "hermes_backend_token", "test-token", raising=False)

    conv_store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage_store = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hp, "get_store", lambda: conv_store)
    monkeypatch.setattr(hp, "get_usage_store", lambda: usage_store)
    monkeypatch.setattr(hp, "schedule_retitle", lambda conv_id: None)

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    captured: dict = {}
    factory = lambda: _DropAfterUsageClient(usage_frame, 200, captured)  # noqa: E731
    executor = HermesExecutor(
        session_store=store, transcript_store=transcripts, http_client_factory=factory,
    )
    session = store.create(task_id="t1", routing="hermes")

    outcome = executor.execute(session, {"description": "hi"})
    assert outcome.status == STATUS_FAILED
    assert store.get("t1").hermes_model == "drop-after-usage-model"
