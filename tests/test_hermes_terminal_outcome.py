"""Hermes worker terminal validation using synthetic chunked SSE streams."""
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


@pytest.fixture(autouse=True)
def _isolated_task_store(tmp_path, monkeypatch):
    """Use a real synthetic TaskManager for each envelope-building test."""
    from api.services.task_manager import TaskManager
    import api.services.task_manager as task_manager_mod

    store = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )
    monkeypatch.setattr(task_manager_mod, "_task_manager", store)


def _frame(event: dict) -> bytes:
    return b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"


class _Response:
    status_code = 200

    def __init__(self, chunks: list[bytes], *, fail_after: bool = False):
        self._chunks = chunks
        self._fail_after = fail_after

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield from self._chunks
        if self._fail_after:
            raise httpx.ReadError("synthetic connection failure")


class _Stream:
    def __init__(self, response: _Response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *args):
        return False


class _Client:
    def __init__(self, chunks: list[bytes], *, fail_after: bool = False):
        self.response = _Response(chunks, fail_after=fail_after)

    def stream(self, method, url, content=None, headers=None):
        return _Stream(self.response)

    def close(self):
        return None


def _build(tmp_path, monkeypatch, chunks: list[bytes], *, fail_after: bool = False):
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://synthetic-hermes", raising=False)
    monkeypatch.setattr(settings, "hermes_backend_token", "synthetic-token", raising=False)

    conv_store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage_store = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hp, "get_store", lambda: conv_store)
    monkeypatch.setattr(hp, "get_usage_store", lambda: usage_store)
    monkeypatch.setattr(hp, "schedule_retitle", lambda conv_id: None)

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = HermesExecutor(
        session_store=store,
        transcript_store=transcripts,
        http_client_factory=lambda: _Client(chunks, fail_after=fail_after),
    )
    session = store.create(task_id="synthetic-hermes-task", routing="hermes")
    return executor, store, transcripts, session


def _terminal_events(content: str = "synthetic reply") -> list[dict]:
    return [
        {"type": "conversation_id", "conversation_id": "synthetic-conversation"},
        {"type": "content", "content": content},
        {"type": "done"},
    ]


def test_split_sse_frames_require_and_accept_terminal_done(tmp_path, monkeypatch):
    """Split SSE reads still complete only after a valid done event."""
    body = b"".join(_frame(event) for event in _terminal_events())
    # Split inside JSON and across frame boundaries: iter_bytes yields reads,
    # not complete SSE frames.
    chunks = [body[:11], body[11:47], body[47:89], body[89:]]
    executor, _store, transcripts, session = _build(tmp_path, monkeypatch, chunks)

    outcome = executor.execute(session, {"description": "synthetic terminal check"})

    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "synthetic reply"
    assert outcome.exit_meta == {
        "done_seen": True,
        "error_seen": False,
        "stream_truncated": False,
    }
    assert transcripts.read(session.session_id)[-1]["kind"] == "hermes_completed"
    persisted = _store.get(session.task_id)
    assert persisted.status == STATUS_COMPLETED
    assert outcome.attempt_id == persisted.attempt_id
    assert outcome.turn_id == persisted.turn_id


def test_cancellation_wins_if_it_lands_before_late_stream_success_write(
    tmp_path, monkeypatch,
):
    """A cancellation inserted at the terminal write cannot be overwritten."""
    body = b"".join(_frame(event) for event in _terminal_events())
    executor, store, transcripts, session = _build(tmp_path, monkeypatch, [body])
    original_update = store.update_status

    def cancel_before_success(task_id, status, **kwargs):
        if status == STATUS_COMPLETED:
            assert store.mark_cancelled(
                task_id,
                attempt_id=kwargs["attempt_id"],
                turn_id=kwargs["turn_id"],
                reason="operator requested",
            )
        return original_update(task_id, status, **kwargs)

    monkeypatch.setattr(store, "update_status", cancel_before_success)
    outcome = executor.execute(session, {"description": "synthetic cancellation race"})

    assert outcome.status == STATUS_FAILED
    assert store.get(session.task_id).status == STATUS_FAILED
    assert not any(e["kind"] == "hermes_completed" for e in transcripts.read(session.session_id))


def test_content_without_done_fails_with_partial_text_and_truncation_evidence(
    tmp_path, monkeypatch,
):
    """Missing done fails and persists partial content with truncation metadata."""
    chunks = [
        _frame({"type": "conversation_id", "conversation_id": "synthetic-truncated"}),
        _frame({"type": "content", "content": "partial synthetic reply"}),
    ]
    executor, _store, transcripts, session = _build(tmp_path, monkeypatch, chunks)

    outcome = executor.execute(session, {"description": "synthetic interrupted check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == "partial synthetic reply"
    assert outcome.reason == "hermes stream ended before terminal done"
    assert outcome.exit_meta["stream_truncated"] is True
    event = transcripts.read(session.session_id)[-1]
    assert event["kind"] == "hermes_stream_interrupted"
    assert event["payload"] == {
        "conversation_id": "synthetic-truncated",
        "partial_chars": len("partial synthetic reply"),
        "done_seen": False,
        "error_seen": False,
        "stream_truncated": True,
    }
    messages = ConversationStore(db_path=str(tmp_path / "conversations.db")).get_messages(
        "synthetic-truncated"
    )
    assert messages[-1].content == (
        "partial synthetic reply\n\n_[cut off — the turn ended before it finished]_"
    )
    assert messages[-1].routing == {
        "truncated": True,
        "truncation_reason": "stream_error",
    }


def test_content_error_done_is_failed_even_with_nonempty_content(tmp_path, monkeypatch):
    """An error+done failure persists partial text with failure routing."""
    events = [
        {"type": "conversation_id", "conversation_id": "synthetic-error"},
        {"type": "content", "content": "partial before synthetic error"},
        {
            "type": "usage",
            "model": "synthetic-error-model",
            "input_tokens": 8,
            "output_tokens": 3,
            "cost_usd": 0.004,
        },
        {"type": "error", "message": "synthetic upstream failure"},
        {"type": "done"},
    ]
    executor, _store, transcripts, session = _build(
        tmp_path, monkeypatch, [_frame(e) for e in events]
    )

    outcome = executor.execute(session, {"description": "synthetic error check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == "partial before synthetic error"
    assert outcome.reason == "hermes stream reported an error"
    assert outcome.exit_meta == {
        "done_seen": True,
        "error_seen": True,
        "stream_truncated": False,
    }
    assert transcripts.read(session.session_id)[-1]["kind"] == "hermes_stream_failed"
    messages = ConversationStore(db_path=str(tmp_path / "conversations.db")).get_messages(
        "synthetic-error"
    )
    assert messages[-1].content == (
        "partial before synthetic error\n\n_[cut off — the turn ended before it finished]_"
    )
    assert messages[-1].routing == {
        "truncated": True,
        "truncation_reason": "stream_error",
    }
    assert UsageStore(db_path=str(tmp_path / "usage.db")).get_conversation_usage(
        "synthetic-error"
    ) == {
        "cost_usd": 0.004,
        "input_tokens": 8,
        "output_tokens": 3,
        "turn_count": 1,
        "is_lower_bound": False,
    }


def test_done_without_content_fails(tmp_path, monkeypatch):
    """A genuine done with no content is not a successful terminal outcome."""
    executor, _store, _transcripts, session = _build(
        tmp_path,
        monkeypatch,
        [
            _frame({"type": "conversation_id", "conversation_id": "synthetic-empty"}),
            _frame({"type": "done"}),
        ],
    )

    outcome = executor.execute(session, {"description": "synthetic empty check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == ""
    assert outcome.reason == "hermes turn produced no content"
    assert outcome.exit_meta == {
        "done_seen": True,
        "error_seen": False,
        "stream_truncated": False,
    }


def test_content_after_done_cannot_make_an_empty_turn_succeed(tmp_path, monkeypatch):
    """Late content is ignored while a complete usage frame is still captured."""
    events = [
        {"type": "conversation_id", "conversation_id": "synthetic-post-done"},
        {"type": "done"},
        {"type": "content", "content": "late synthetic content"},
        {
            "type": "usage",
            "model": "synthetic-post-done-model",
            "input_tokens": 5,
            "output_tokens": 2,
            "cost_usd": 0.003,
        },
    ]
    executor, _store, _transcripts, session = _build(
        tmp_path, monkeypatch, [_frame(e) for e in events]
    )

    outcome = executor.execute(session, {"description": "synthetic post-done check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == ""
    assert outcome.reason == "hermes turn produced no content"
    assert ConversationStore(db_path=str(tmp_path / "conversations.db")).get_conversation(
        "synthetic-post-done"
    ) is None
    assert UsageStore(db_path=str(tmp_path / "usage.db")).get_conversation_usage(
        "synthetic-post-done"
    ) == {
        "cost_usd": 0.003,
        "input_tokens": 5,
        "output_tokens": 2,
        "turn_count": 1,
        "is_lower_bound": False,
    }


def test_incomplete_eof_frame_is_discarded_without_terminal_separator(tmp_path, monkeypatch):
    """An SSE event without EOF's final blank line is discarded, per WHATWG."""
    chunks = [
        _frame({"type": "conversation_id", "conversation_id": "synthetic-eof"}),
        _frame({"type": "content", "content": "complete synthetic content"}),
        b'data: {"type": "done"}\n',
    ]
    executor, _store, _transcripts, session = _build(tmp_path, monkeypatch, chunks)

    outcome = executor.execute(session, {"description": "synthetic EOF check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == "complete synthetic content"
    assert outcome.reason == "hermes stream ended before terminal done"
    assert outcome.exit_meta == {
        "done_seen": False,
        "error_seen": False,
        "stream_truncated": True,
    }
    messages = ConversationStore(db_path=str(tmp_path / "conversations.db")).get_messages(
        "synthetic-eof"
    )
    assert messages[-1].content == (
        "complete synthetic content\n\n_[cut off — the turn ended before it finished]_"
    )


def test_network_failure_retains_partial_text_and_truncation_evidence(tmp_path, monkeypatch):
    """A post-usage network drop retains partial text and truncation evidence."""
    chunks = [
        _frame({"type": "conversation_id", "conversation_id": "synthetic-network"}),
        _frame({"type": "content", "content": "partial network reply"}),
        _frame({
            "type": "usage",
            "model": "synthetic-afterusage-model",
            "input_tokens": 4,
            "output_tokens": 3,
            "cost_usd": 0.001,
        }),
    ]
    executor, store, transcripts, session = _build(
        tmp_path, monkeypatch, chunks, fail_after=True,
    )

    outcome = executor.execute(session, {"description": "synthetic network check"})

    assert outcome.status == STATUS_FAILED
    assert outcome.final_text == "partial network reply"
    assert "hermes request failed" in outcome.reason
    assert outcome.exit_meta == {
        "done_seen": False,
        "error_seen": False,
        "stream_truncated": True,
    }
    assert store.get(session.task_id).hermes_model == "synthetic-afterusage-model"
    event = transcripts.read(session.session_id)[-1]
    assert event["kind"] == "hermes_request_failed"
    assert event["payload"]["partial_chars"] == len("partial network reply")
    assert event["payload"]["stream_truncated"] is True
    messages = ConversationStore(db_path=str(tmp_path / "conversations.db")).get_messages(
        "synthetic-network"
    )
    assert messages[-1].content == (
        "partial network reply\n\n_[cut off — the turn ended before it finished]_"
    )
    assert messages[-1].routing == {
        "truncated": True,
        "truncation_reason": "stream_error",
    }
