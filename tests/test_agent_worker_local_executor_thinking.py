"""Tests for the per-session local-executor thinking override (#851, AC3):
`effort: high`/`max` requests thinking on a LocalLLMClient turn; `low`/
`medium`/absent does not. Verifies the override never reaches a fake
(non-LocalLLMClient) test double, so every pre-#851 local_executor test
stays byte-identical.
"""
from __future__ import annotations

import pytest

from api.services.agent_worker.local_executor import LocalExecutor
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.llm_client import LocalLLMClient


pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self):
        self.text = "done"
        self.tool_calls = []
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 1, "output_tokens": 1}


def _build_executor(tmp_path, monkeypatch, *, effort: str | None, remote: bool = False):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    client = LocalLLMClient(base_url="http://localhost:9999", model="local")
    calls: list[dict] = []

    def _fake_create(self, messages, *, system=None, max_tokens, tools=None, **kwargs):
        calls.append(kwargs)
        return _FakeResponse()

    monkeypatch.setattr(LocalLLMClient, "create", _fake_create)
    executor = LocalExecutor(
        session_store=store, transcript_store=transcripts, llm_client=client, is_remote=remote,
    )
    session = store.create(task_id="t1", routing="local", effort=effort)
    return executor, store, session, calls


@pytest.mark.parametrize("effort", ["high", "max"])
def test_high_or_max_effort_requests_thinking(tmp_path, monkeypatch, effort):
    executor, store, session, calls = _build_executor(tmp_path, monkeypatch, effort=effort)
    executor._call_llm(session.session_id, effort=effort)
    assert calls[-1]["enable_thinking"] is True


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_low_or_medium_effort_disables_thinking(tmp_path, monkeypatch, effort):
    executor, store, session, calls = _build_executor(tmp_path, monkeypatch, effort=effort)
    executor._call_llm(session.session_id, effort=effort)
    assert calls[-1]["enable_thinking"] is False


def test_absent_effort_falls_back_to_setting(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "local_agent_enable_thinking", True, raising=False)
    executor, store, session, calls = _build_executor(tmp_path, monkeypatch, effort=None)
    executor._call_llm(session.session_id, effort=None)
    assert calls[-1]["enable_thinking"] is None  # True setting -> None (server default)

    monkeypatch.setattr(settings, "local_agent_enable_thinking", False, raising=False)
    executor._call_llm(session.session_id, effort=None)
    assert calls[-1]["enable_thinking"] is False


def test_remote_forced_route_never_receives_enable_thinking(tmp_path, monkeypatch):
    """The #809 remote-forced route is also a LocalLLMClient instance but
    must never get the local-only thinking toggle."""
    executor, store, session, calls = _build_executor(
        tmp_path, monkeypatch, effort="high", remote=True,
    )
    executor._call_llm(session.session_id, effort="high")
    assert "enable_thinking" not in calls[-1]


def test_non_local_llm_client_fake_never_receives_enable_thinking(tmp_path):
    """A test-injected fake whose create() doesn't accept enable_thinking
    must not be called with it — this is what keeps every pre-#851
    local_executor test (which uses exactly this kind of fake) passing
    unchanged."""

    class _Fake:
        def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
            return _FakeResponse()

    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = LocalExecutor(session_store=store, transcript_store=transcripts, llm_client=_Fake())
    session = store.create(task_id="t1", routing="local", effort="high")
    # Would raise TypeError if enable_thinking were passed — _Fake.create()
    # has no such parameter.
    result = executor._call_llm(session.session_id, effort="high")
    assert result.text == "done"
