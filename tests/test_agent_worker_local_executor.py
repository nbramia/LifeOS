"""Local executor agent-loop tests.

The executor is driven against a fake LLM that returns scripted responses
(text + tool_calls), so each test exercises one turn-shape: simple
completion, multi-turn with tools, budget breach, sleep yield, error.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from api.services.agent_worker.local_executor import (
    _SYSTEM_PROMPT_STATIC,
    LocalExecutor,
    _default_llm_client,
    _remote_only_llm_client,
    _system_prompt,
)
from api.services.agent_worker.pricing import cost_for, is_known_model
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_YIELDED,
    SessionStore,
)
from api.services.agent_worker.tools import ToolRegistry
from api.services.agent_worker.transcript_store import TranscriptStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _FakeResponse:
    text: str = ""
    usage: _FakeUsage = None
    tool_calls: list = None
    model: str = "local"
    finish_reason: str = ""


class _ScriptedLLM:
    """A fake LLM that returns successive scripted responses on each call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        self.calls.append({
            "messages": messages, "system": system, "tools": tools,
            "max_tokens": max_tokens,
        })
        if not self._responses:
            raise AssertionError("LLM was called more times than scripted")
        return self._responses.pop(0)


class _FakeMCPServer:
    tools: list[dict] = []
    def _call_api(self, name, args): return {}
    def _format_response(self, name, data): return ""


@pytest.fixture
def fake_session(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(
        task_id="t1",
        routing="local",
        budget={"wall_seconds": 3600, "max_tokens": 5_000, "max_dollars": 5.0},
        expected_output="text",
    )
    session = store.get("t1")
    return store, session


def _make_executor(session_store, transcript_dir, llm):
    return LocalExecutor(
        session_store=session_store,
        transcript_store=TranscriptStore(transcripts_dir=transcript_dir),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="local",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_executor_reseeds_with_original_task_when_resuming_after_blocked_clarification(
    tmp_path: Path, fake_session,
):
    """Repro of the live bug: preflight blocks for ambiguity → executor
    never runs → worker resumes by appending only the user's answer →
    executor saw a 1-message history that wasn't the task ("Web Searches")
    and acted on it as if it were the task. After the fix, executor
    detects the missing system role, clears, re-seeds with system +
    original task, and re-appends the answer in the right order."""
    store, session = fake_session
    sid = session.session_id

    # Worker has pre-injected the operator's Telegram reply, but seeding
    # never happened. Conversation has exactly one orphan user message.
    store.append_message(sid, "user", "(user answered via Telegram) Web Searches")

    llm = _ScriptedLLM([
        _FakeResponse(text="Done with task using web search.", usage=_FakeUsage(40, 20)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(
        session,
        {"id": "t1", "description": "Summarize professional background of Julia Barnes"},
    )

    assert outcome.status == STATUS_COMPLETED
    # First (and only) LLM call must see: system, then user-task, then user-answer.
    first_call = llm.calls[0]
    system_text = first_call["system"]
    msgs = first_call["messages"]
    assert system_text and "<role>" in system_text, "system prompt was missing"
    # Build a flat sequence of (role, text-substring) to assert order.
    roles_and_text = [(m["role"], m["content"] if isinstance(m["content"], str) else "") for m in msgs]
    # Drop tool_result-shaped messages — only care about textual ones here.
    text_only = [(r, t) for r, t in roles_and_text if t]
    assert text_only[0][0] == "user"
    assert "Julia Barnes" in text_only[0][1] and "Task:" in text_only[0][1]
    # The user's answer comes AFTER the task message, not before.
    answer_idx = next(i for i, (r, t) in enumerate(text_only) if "Web Searches" in t)
    task_idx = next(i for i, (r, t) in enumerate(text_only) if "Julia Barnes" in t)
    assert task_idx < answer_idx, (
        "user answer must come after the seeded task message — out of "
        f"order would re-introduce the live bug. Got: {text_only}"
    )


@pytest.mark.unit
def test_executor_completes_when_model_returns_text_only(tmp_path: Path, fake_session):
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="The date is May 26, 2026.", usage=_FakeUsage(50, 20)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "what's the date"})
    assert outcome.status == STATUS_COMPLETED
    assert "May 26" in outcome.final_text
    # One LLM call only — no tools required.
    assert len(llm.calls) == 1
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 50
    assert refreshed.total_output_tokens == 20


@pytest.mark.unit
def test_executor_truncates_oversize_tool_results_in_context(tmp_path: Path, fake_session):
    """Live bug repro: an unbounded `grep -r` returned 32k chars of noise
    and was appended verbatim to conversation history, which then pushed
    the next LLM call past Gemma's 32k context window — llama-server
    dropped the connection mid-request. The fix caps any single tool
    result the model sees at MAX_TOOL_RESULT_CHARS; the full output
    still lives in the transcript for operator audit."""
    from api.services.agent_worker.local_executor import MAX_TOOL_RESULT_CHARS
    from api.services.agent_worker.tools import ToolResult

    store, session = fake_session

    # First response: agent calls a tool that returns a huge string.
    # Second response: agent finalizes.
    big = "Julia mention. " * 5000  # ~75 KB
    llm = _ScriptedLLM([
        _FakeResponse(
            text="",
            usage=_FakeUsage(40, 10),
            tool_calls=[{"id": "c1", "name": "Bash",
                         "input": {"command": "grep -r 'Julia' ."}}],
        ),
        _FakeResponse(text="Done.", usage=_FakeUsage(30, 15)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    # Patch the tool registry to return our oversized payload.
    executor.tools.dispatch = lambda name, args: ToolResult(output=big, is_error=False)  # type: ignore[assignment]

    outcome = executor.execute(session, {"id": "t1", "description": "search"})
    assert outcome.status == STATUS_COMPLETED

    # The second LLM call sees the tool_result, which must be capped.
    second_msgs = llm.calls[1]["messages"]
    tool_result_msg = next(
        m for m in second_msgs
        if isinstance(m["content"], list) and m["content"]
        and isinstance(m["content"][0], dict)
        and m["content"][0].get("type") == "tool_result"
    )
    tool_content = tool_result_msg["content"][0]["content"]
    assert len(tool_content) <= MAX_TOOL_RESULT_CHARS + 500, (
        f"tool result still {len(tool_content)} chars — truncation missed"
    )
    assert "truncated" in tool_content.lower()
    # Original size is referenced so the agent knows what was dropped.
    assert str(len(big)) in tool_content


@pytest.mark.unit
def test_executor_retries_transient_llm_disconnect(tmp_path: Path, fake_session):
    """llama-server can drop the connection under memory pressure
    ("Server disconnected without sending a response"). One retry after
    a short backoff usually recovers — we should not fail the whole
    session on the first transient error."""
    store, session = fake_session

    attempts = {"n": 0}
    class _FlakyLLM:
        calls: list = []
        def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
            self.calls.append({})
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx_remote_error("Server disconnected without sending a response.")
            return _FakeResponse(text="Recovered.", usage=_FakeUsage(40, 10))

    executor = _make_executor(store, tmp_path / "transcripts", _FlakyLLM())
    outcome = executor.execute(session, {"id": "t1", "description": "ask"})
    assert outcome.status == STATUS_COMPLETED, outcome
    assert outcome.final_text == "Recovered."
    assert attempts["n"] == 2, "expected exactly one retry"


@pytest.mark.unit
def test_executor_does_not_retry_non_transient_llm_error(tmp_path: Path, fake_session):
    """A schema / validation error from the LLM should fail fast, not
    burn the retry budget — retry is for connection-shaped drops only."""
    store, session = fake_session

    attempts = {"n": 0}
    class _StructuralErrorLLM:
        def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
            attempts["n"] += 1
            raise ValueError("invalid tool schema — model returned bad json")

    executor = _make_executor(store, tmp_path / "transcripts", _StructuralErrorLLM())
    outcome = executor.execute(session, {"id": "t1", "description": "ask"})
    assert outcome.status == STATUS_FAILED
    assert attempts["n"] == 1, "should NOT have retried a structural error"


def httpx_remote_error(msg: str) -> Exception:
    """Construct an httpx-flavored remote-protocol error. We don't import
    httpx at module top-level because the test should work in environments
    where the LLM client wraps a different HTTP library."""
    try:
        import httpx
        return httpx.RemoteProtocolError(msg)
    except ImportError:
        # Fallback that still has the right substring match.
        return ConnectionError(msg)


@pytest.mark.unit
def test_executor_runs_tool_then_completes(tmp_path: Path, fake_session):
    store, session = fake_session
    # First response: agent calls Bash. Second response: agent finalizes.
    llm = _ScriptedLLM([
        _FakeResponse(
            text="",
            usage=_FakeUsage(40, 10),
            tool_calls=[{
                "id": "call_1",
                "name": "Bash",
                "input": {"command": "echo hi"},
            }],
        ),
        _FakeResponse(text="Output was 'hi'.", usage=_FakeUsage(30, 15)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "echo hi"})
    assert outcome.status == STATUS_COMPLETED
    # Second call should include the tool result as a user turn.
    second_msgs = llm.calls[1]["messages"]
    assert any(
        isinstance(m["content"], list)
        and m["content"]
        and isinstance(m["content"][0], dict)
        and m["content"][0].get("type") == "tool_result"
        for m in second_msgs
    )


@pytest.mark.unit
def test_executor_yields_on_sleep_tool(tmp_path: Path, fake_session):
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(
            text="",
            usage=_FakeUsage(20, 10),
            tool_calls=[{"id": "c1", "name": "sleep", "input": {"seconds": 5, "reason": "wait"}}],
        ),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "wait for it"})
    assert outcome.status == STATUS_YIELDED
    assert outcome.wake_at is not None
    # A sleeps row should exist for this session.
    assert session.session_id in store.due_sleeps(now_ts=outcome.wake_at + 1)


@pytest.mark.unit
def test_executor_kills_loop_on_token_budget(tmp_path: Path):
    """One call exceeds the budget; the next loop iteration's check kills us."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(
        task_id="t1",
        routing="local",
        budget={"wall_seconds": 3600, "max_tokens": 50, "max_dollars": 5.0},
        expected_output="text",
    )
    session = store.get("t1")
    # First call spends 60 tokens — over the 50-token cap. The top-of-loop
    # check kills before the second call is attempted.
    llm = _ScriptedLLM([
        _FakeResponse(
            text="",
            usage=_FakeUsage(40, 20),
            tool_calls=[{"id": "c1", "name": "Bash", "input": {"command": "echo a"}}],
        ),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "loop"})
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_tokens" in outcome.reason
    assert len(llm.calls) == 1


@pytest.mark.unit
def test_executor_marks_failed_on_llm_exception(tmp_path: Path, fake_session):
    store, session = fake_session

    class _Boom:
        def create(self, **kw):
            raise RuntimeError("llama-server unreachable")

    executor = _make_executor(store, tmp_path / "transcripts", _Boom())
    outcome = executor.execute(session, {"id": "t1", "description": "x"})
    assert outcome.status == STATUS_FAILED
    assert "llama-server unreachable" in outcome.reason
    assert store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_executor_records_local_spend_is_zero_dollars(tmp_path: Path, fake_session):
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(1234, 567)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 1234
    assert refreshed.total_output_tokens == 567
    # Local model is $0/token — total_dollars stays 0 even with many tokens.
    assert refreshed.total_dollars == pytest.approx(0.0)
    # "local" is a genuinely free known model, not an unrecognized one.
    assert refreshed.unpriced is False


@pytest.mark.unit
def test_executor_records_known_model_priced_and_not_unpriced(tmp_path: Path, fake_session):
    """A recognized model prices real dollars and leaves `unpriced` False —
    the record path must not regress known-model behavior (#669)."""
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(1000, 500)),
    ])
    executor = LocalExecutor(
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="claude-haiku-4-5",
    )
    executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_dollars == pytest.approx(cost_for("claude-haiku-4-5", 1000, 500))
    assert refreshed.unpriced is False


@pytest.mark.unit
def test_executor_records_unknown_model_as_unpriced_not_fallback_rate(tmp_path: Path, fake_session):
    """This is a **record** path (#669): an unrecognized model must not be
    silently billed at cost_for's conservative fallback (priciest) rate —
    it must record $0.00 and flag the session `unpriced` instead."""
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(1000, 500)),
    ])
    executor = LocalExecutor(
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="typoed-model",
    )
    assert is_known_model("typoed-model") is False
    executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 1000
    assert refreshed.total_output_tokens == 500
    assert refreshed.total_dollars == pytest.approx(0.0)
    assert refreshed.unpriced is True


# ---------------------------------------------------------------------------
# #699 — remote fallback executor construction, spend, and served_by
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_default_llm_client_flag_off_stays_local_without_probing(monkeypatch):
    """Pins behavior-neutrality: flag off is byte-identical to pre-#699,
    including making zero network calls to check anything — the remote
    branch is never even entered."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "remote-model", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "key", raising=False)

    probed = []
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: probed.append(1) or True)

    client, model_name, is_remote = _default_llm_client("local")

    assert is_remote is False
    assert model_name == "local"
    assert isinstance(client, LocalLLMClient)
    assert client.base_url == LocalLLMClient().base_url
    assert probed == [], "flag off must not probe the local server at all"


@pytest.mark.unit
def test_default_llm_client_flag_on_but_remote_not_configured_stays_local(monkeypatch):
    """Second half of behavior-neutrality: flag on with an incompletely
    configured remote provider (missing api key here) is also a no-op —
    same as flag off."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_remote_executor", True, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "remote-model", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)  # unconfigured

    probed = []
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: probed.append(1) or True)

    client, model_name, is_remote = _default_llm_client("local")

    assert is_remote is False
    assert model_name == "local"
    assert probed == []


@pytest.mark.unit
def test_default_llm_client_flag_on_remote_configured_local_alive_stays_local(monkeypatch):
    """Fallback, not replacement: an explicit local route on a host with a
    live llama-server keeps using it even with the flag on and a fully
    configured remote provider."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_remote_executor", True, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "remote-model", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "key", raising=False)

    probed = []
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: probed.append(1) or True)

    client, model_name, is_remote = _default_llm_client("local")

    assert is_remote is False
    assert model_name == "local"
    assert client.base_url == LocalLLMClient().base_url
    assert probed == [1], "must check reachability exactly once, not zero times"


@pytest.mark.unit
def test_default_llm_client_flag_on_remote_configured_local_down_selects_remote(monkeypatch):
    """The end-to-end AC: flag on + remote configured + no reachable local
    llama-server ⇒ construct a LocalLLMClient pointed at the remote
    provider's base_url/model/api_key from settings."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_remote_executor", True, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/deepseek-v4-flash-0731", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "fw_test_key", raising=False)
    monkeypatch.setattr(settings, "remote_llm_timeout", 42, raising=False)

    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: False)

    client, model_name, is_remote = _default_llm_client("local")

    assert is_remote is True
    assert model_name == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert isinstance(client, LocalLLMClient)
    # #706: LocalLLMClient strips one trailing /v1 segment so the wire
    # path is always {base}/v1/chat/completions, never .../v1/v1/....
    assert client.base_url == "https://remote.example"
    assert client.model == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert client.timeout == 42
    assert client._auth_headers() == {"Authorization": "Bearer fw_test_key"}


@pytest.mark.unit
def test_remote_only_llm_client_never_probes_local_or_checks_the_flag(monkeypatch):
    """(#809) `_remote_only_llm_client` — the `#cloud` tag's executor — is a
    first-class route, not a contingency: unlike `_default_llm_client`, it
    never checks local llama-server reachability and is not gated on
    `settings.agent_remote_executor` at all. The operator tagging a task
    `#cloud` is itself the opt-in."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    # Flag deliberately left False/default — must not matter for this path.
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/deepseek-v3", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "fw_test_key", raising=False)
    monkeypatch.setattr(settings, "remote_llm_timeout", 99, raising=False)

    probed = []
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: probed.append(1) or True)

    client, model_name, is_remote = _remote_only_llm_client()

    assert is_remote is True
    assert model_name == "accounts/fireworks/models/deepseek-v3"
    assert isinstance(client, LocalLLMClient)
    assert client.base_url == "https://remote.example"
    assert client.model == "accounts/fireworks/models/deepseek-v3"
    assert client.timeout == 99
    assert client._auth_headers() == {"Authorization": "Bearer fw_test_key"}
    assert probed == [], "the remote-forced route must never probe local reachability"


@pytest.mark.unit
def test_executor_construction_flag_off_builds_bare_local_client(tmp_path: Path, monkeypatch):
    """Same guarantee as the module-level test above, but through the
    LocalExecutor constructor itself — pins the actual call site worker.py
    uses (no llm_client injected)."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    store = SessionStore(db_path=tmp_path / "sessions.db")
    executor = LocalExecutor(
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
    )
    assert executor.is_remote is False
    assert executor.model_name == "local"
    assert isinstance(executor.llm, LocalLLMClient)
    assert executor.llm.base_url == LocalLLMClient().base_url


@pytest.mark.unit
def test_executor_records_remote_spend_priced_with_configured_rates(tmp_path: Path, fake_session, monkeypatch):
    """Mirrors agent_loop.py's force_remote _track_usage branch (#654):
    when remote rates are configured, a remote-served session prices real
    dollars from them, not from pricing.PRICING (the remote model id isn't
    in that table at all)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "remote_llm_input_price_per_mtok", 0.5, raising=False)
    monkeypatch.setattr(settings, "remote_llm_output_price_per_mtok", 1.5, raising=False)

    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(1_000_000, 1_000_000)),
    ])
    executor = LocalExecutor(
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="accounts/fireworks/models/deepseek-v4-flash-0731",
        is_remote=True,
    )
    outcome = executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_dollars == pytest.approx(0.5 + 1.5)
    assert refreshed.unpriced is False
    assert outcome.served_by == "accounts/fireworks/models/deepseek-v4-flash-0731"


@pytest.mark.unit
def test_executor_records_remote_spend_unpriced_without_configured_rates(tmp_path: Path, fake_session, monkeypatch):
    """No configured rate ⇒ real unpriced spend, never fallback-priced —
    same #669 convention as the unknown-model case, applied to the remote
    branch."""
    from config.settings import settings
    monkeypatch.setattr(settings, "remote_llm_input_price_per_mtok", None, raising=False)
    monkeypatch.setattr(settings, "remote_llm_output_price_per_mtok", None, raising=False)

    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(1000, 500)),
    ])
    executor = LocalExecutor(
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        tool_registry=ToolRegistry(lifeos_mcp_server=_FakeMCPServer()),
        llm_client=llm,
        model_name="accounts/fireworks/models/deepseek-v4-flash-0731",
        is_remote=True,
    )
    executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 1000
    assert refreshed.total_output_tokens == 500
    assert refreshed.total_dollars == pytest.approx(0.0)
    assert refreshed.unpriced is True


@pytest.mark.unit
def test_executor_outcome_served_by_empty_for_ordinary_local_session(tmp_path: Path, fake_session):
    """served_by must stay empty (no message-text change downstream) for
    every session not on the remote fallback — including the default
    is_remote=False path exercised by every existing test above."""
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(10, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "x"})
    assert outcome.served_by == ""


@pytest.mark.unit
def test_executor_normalizes_openai_tool_calls(tmp_path: Path, fake_session):
    """LocalLLMClient emits OpenAI-format tool_calls; the executor must convert.

    Without normalization, the dispatcher receives `name=""` and routes to
    "unknown tool", breaking the agent loop on every real tool call.
    """
    store, session = fake_session
    openai_shape_call = {
        "id": "call_abc",
        "type": "function",
        "function": {
            "name": "Bash",
            "arguments": '{"command": "echo normalized"}',
        },
    }
    llm = _ScriptedLLM([
        _FakeResponse(text="", usage=_FakeUsage(20, 10), tool_calls=[openai_shape_call]),
        _FakeResponse(text="ok", usage=_FakeUsage(5, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    outcome = executor.execute(session, {"id": "t1", "description": "x"})
    assert outcome.status == STATUS_COMPLETED
    # The dispatcher should have received the right tool name + args.
    second_msgs = llm.calls[1]["messages"]
    tool_result = next(
        b for m in second_msgs if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert "normalized" in tool_result["content"]


@pytest.mark.unit
def test_executor_tracks_active_seconds_not_wall(tmp_path: Path, fake_session):
    """A completed turn should bump total_active_seconds — used for wall budget."""
    store, session = fake_session
    llm = _ScriptedLLM([
        _FakeResponse(text="done.", usage=_FakeUsage(10, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    executor.execute(session, {"id": "t1", "description": "x"})
    refreshed = store.get("t1")
    assert refreshed.total_active_seconds > 0


@pytest.mark.unit
def test_executor_truncates_persisted_tool_use_to_match_dispatch(tmp_path: Path):
    """If MAX_TOOL_CALLS_PER_TURN truncates dispatch, the persisted assistant
    turn must drop the surplus tool_use blocks so the next turn has 1:1
    tool_use ↔ tool_result counts."""
    from api.services.agent_worker.local_executor import MAX_TOOL_CALLS_PER_TURN
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(task_id="t1", routing="local",
                 budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
                 expected_output="text")
    session = store.get("t1")
    too_many = [
        {"id": f"c{i}", "name": "Bash", "input": {"command": "true"}}
        for i in range(MAX_TOOL_CALLS_PER_TURN + 5)
    ]
    llm = _ScriptedLLM([
        _FakeResponse(text="", usage=_FakeUsage(5, 5), tool_calls=too_many),
        _FakeResponse(text="ok", usage=_FakeUsage(5, 5)),
    ])
    executor = _make_executor(store, tmp_path / "transcripts", llm)
    executor.execute(session, {"id": "t1", "description": "x"})
    # Second LLM call's messages must have matching tool_use / tool_result counts.
    second_msgs = llm.calls[1]["messages"]
    assistant_msg = next(m for m in second_msgs if m["role"] == "assistant")
    user_msg = next(m for m in second_msgs if m["role"] == "user"
                    and isinstance(m["content"], list)
                    and m["content"] and isinstance(m["content"][0], dict)
                    and m["content"][0].get("type") == "tool_result")
    use_count = sum(1 for b in assistant_msg["content"] if isinstance(b, dict) and b.get("type") == "tool_use")
    result_count = len(user_msg["content"])
    assert use_count == result_count == MAX_TOOL_CALLS_PER_TURN


@pytest.mark.unit
def test_pricing_table_local_is_free():
    assert cost_for("local", 1_000_000, 1_000_000) == pytest.approx(0.0)


@pytest.mark.unit
def test_pricing_table_opus_uses_correct_rates():
    # 1k input + 1k output of Opus = $0.005 + $0.025 = $0.03
    cost = cost_for("claude-opus-4-7", 1000, 1000)
    assert cost == pytest.approx(0.005 + 0.025)


@pytest.mark.unit
def test_pricing_table_opus_5_uses_verified_rate():
    """Claude Opus 5's rate is $5/$25 per Mtok — verified against
    https://platform.claude.com/docs/en/about-claude/pricing (2026-08-23),
    same tier price as Opus 4.5/4.6/4.7/4.8 (#655)."""
    cost = cost_for("claude-opus-5", 1000, 1000)
    assert cost == pytest.approx(0.005 + 0.025)


@pytest.mark.unit
def test_pricing_unknown_model_falls_through_to_priciest_rate():
    """Conservative: unknown model = highest plausible price (so budgets stay
    enforced rather than silently suppressed by a typo). Fable 5 / Mythos 5
    are the priciest tier as of #655 (Opus was, before they were added)."""
    unknown = cost_for("typoed-model", 1000, 1000)
    priciest = cost_for("claude-fable-5", 1000, 1000)
    assert unknown == pytest.approx(priciest)


@pytest.mark.unit
def test_pricing_fallback_does_not_hardcode_a_specific_model_id():
    """The unknown-model fallback must track whichever tier is priciest,
    not a specific superseded id (#655) — so it can't itself go stale the
    next time a new top-tier model ships.

    #669 narrowed the set it maxes over to models Anthropic still serves
    (see RETIRED_MODELS); the original guard — computed, never a hardcoded
    id — is unchanged, and a newly-added top tier is still picked up
    automatically, which is what this test exists to protect.
    """
    from api.services.agent_worker.pricing import (
        PRICING, RETIRED_MODELS, fallback_rates,
    )

    priciest_served_rate = max(
        rates["output"]
        for name, rates in PRICING.items()
        if name != "local" and name not in RETIRED_MODELS
    )
    assert fallback_rates()["output"] == pytest.approx(priciest_served_rate)

    # Still dynamic: a hypothetical new top tier would take over the ceiling
    # without any code change.
    hypothetical = {"input": 99.0e-6, "output": 999.0e-6}
    PRICING["zz-hypothetical-top-tier"] = hypothetical
    try:
        assert fallback_rates() == hypothetical
    finally:
        del PRICING["zz-hypothetical-top-tier"]


@pytest.mark.unit
def test_pricing_historical_dated_snapshot_ids_still_resolve():
    """Real usage rows record the exact dated snapshot id the API echoed
    back (e.g. Claude Code sessions), not the bare tier alias — these must
    keep pricing correctly (#656)."""
    assert cost_for("claude-sonnet-4-5-20250929", 1000, 1000) == pytest.approx(
        cost_for("claude-sonnet-4-5", 1000, 1000)
    )
    assert cost_for("claude-sonnet-4-20250514", 1000, 1000) == pytest.approx(
        cost_for("claude-sonnet-4", 1000, 1000)
    )


@pytest.mark.unit
def test_is_known_model_true_for_dated_snapshot_of_a_priced_tier():
    assert is_known_model("claude-sonnet-4-5-20250929") is True


@pytest.mark.unit
def test_is_known_model_false_for_unrecognized_id():
    """An unrecognized model records as unpriced (#661) rather than being
    silently priced at the (expensive) Opus fallback rate."""
    assert is_known_model("typoed-model") is False


@pytest.mark.unit
def test_pricing_cache_creation_charged_at_125pct_of_input():
    """cache_creation tokens cost 1.25× the model's input rate."""
    # Sonnet input rate is $3/M, so 1k cache_creation = $0.003 × 1.25 = $0.00375.
    cost = cost_for("claude-sonnet-4-6", 0, 0, cache_creation_tokens=1000)
    assert cost == pytest.approx(0.003 * 1.25)


@pytest.mark.unit
def test_pricing_cache_read_charged_at_10pct_of_input():
    """cache_read tokens cost 0.10× the model's input rate (12.5× cheaper
    than cache_creation, 10× cheaper than uncached input)."""
    cost = cost_for("claude-sonnet-4-6", 0, 0, cache_read_tokens=1000)
    assert cost == pytest.approx(0.003 * 0.10)


@pytest.mark.unit
def test_pricing_all_four_buckets_compose():
    """Mixed payload: input + output + cache_creation + cache_read all sum."""
    # Sonnet: input $3/M, output $15/M.
    # 1k input = $0.003, 1k output = $0.015,
    # 1k cache_creation = $0.00375, 1k cache_read = $0.0003.
    cost = cost_for(
        "claude-sonnet-4-6",
        tokens_in=1000,
        tokens_out=1000,
        cache_creation_tokens=1000,
        cache_read_tokens=1000,
    )
    assert cost == pytest.approx(0.003 + 0.015 + 0.00375 + 0.0003)


@pytest.mark.unit
def test_pricing_cache_buckets_default_to_zero():
    """Two-arg call sites (the local executor) keep working unchanged."""
    cost = cost_for("claude-haiku-4-5", 1000, 500)
    expected_two_arg = cost_for(
        "claude-haiku-4-5", 1000, 500, cache_creation_tokens=0, cache_read_tokens=0,
    )
    assert cost == pytest.approx(expected_two_arg)


@pytest.mark.unit
@pytest.mark.parametrize("model,input_rate,output_rate", [
    ("claude-opus-4-1", 15.0e-6, 75.0e-6),
    ("claude-opus-4", 15.0e-6, 75.0e-6),
    ("claude-haiku-3-5", 0.8e-6, 4.0e-6),
])
def test_retired_model_pricing_added_by_669(model, input_rate, output_rate):
    """Before #669, these retired-but-still-served ids had no PRICING entry
    and silently fell through to fallback_rates() (the $10/$50 tier) —
    which *understated* the Opus pair's real $15/$75 rate."""
    assert is_known_model(model) is True
    # 1M input + 1M output tokens, so the dollar total is the per-Mtok pair.
    assert cost_for(model, 1_000_000, 1_000_000) == pytest.approx(
        (input_rate + output_rate) * 1_000_000
    )


@pytest.mark.unit
def test_retired_models_do_not_raise_the_unknown_model_ceiling():
    """Retired ids are priced for historical rows but must never win
    fallback_rates(). Opus 4/4.1 are $15/$75 — pricier than any tier
    Anthropic still serves — so including them would silently inflate every
    unknown-model *estimate* by 50% (found while implementing #669)."""
    from api.services.agent_worker.pricing import (
        PRICING, RETIRED_MODELS, fallback_rates,
    )

    assert "claude-opus-4-1" in RETIRED_MODELS
    # The ceiling is the priciest still-served tier, not the priciest row.
    assert fallback_rates() == PRICING["claude-fable-5"]
    priciest_row = max(
        (r for n, r in PRICING.items() if n != "local"),
        key=lambda r: r["output"],
    )
    assert priciest_row["output"] > fallback_rates()["output"]
    # ...and every retired id still prices from its own real rate.
    for name in RETIRED_MODELS:
        assert PRICING[name] != fallback_rates() or name == "claude-fable-5"


# ---------------------------------------------------------------------------
# System-prompt structure (issue #119 — Anthropic 4.6/4.7 best practices)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_system_prompt_uses_xml_section_tags():
    """Anthropic recommends XML tags for system-prompt sections (literal
    instruction-following in 4.7). Verify each major section is wrapped."""
    for tag in ("<role>", "<environment>", "<mcp_routing>", "<output_format>",
                "<ambiguity>", "<inter_agent>", "<sleep>", "<this_task>"):
        assert tag in _system_prompt(
            session_id="sess_x",
            expected_output="text",
            budget={"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
        ), f"missing XML section tag: {tag}"


@pytest.mark.unit
def test_system_prompt_requires_final_text_turn_after_tool_use():
    """Issue #117 / #119: explicit requirement that the agent produces a
    text summary turn after any tool use. Catches the 'idled without
    agent.message' regression at the prompt level."""
    prompt = _system_prompt(
        session_id="sess_x",
        expected_output="text",
        budget={"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
    )
    # Look for the canonical phrasing that triggers the behavior.
    assert "Every task must end with a final assistant turn" in prompt
    assert "Tool calls alone are not a complete response" in prompt


@pytest.mark.unit
def test_system_prompt_uses_positive_framing_for_ambiguity():
    """Anthropic best practice: 'Tell Claude what to do instead of what
    not to do'. The old prompt used 'do not invent answers' (negative)."""
    prompt = _system_prompt(
        session_id="sess_x",
        expected_output="text",
        budget={"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
    )
    assert "do not invent" not in prompt.lower()
    # Positive phrasing present (collapse whitespace so multi-line phrasing
    # in the prompt doesn't trip the substring check):
    import re
    collapsed = re.sub(r"\s+", " ", prompt)
    assert "make a reasonable assumption" in collapsed
    assert "note the assumption" in collapsed


@pytest.mark.unit
def test_system_prompt_injects_session_id_into_dynamic_block_only():
    """The agent needs its own session_id to pass as `caller_session_id`
    when calling inter-agent tools (the dispatcher requires it; MCP HTTP
    can't infer it server-side). The id lands in the dynamic <this_task>
    trailer, not the cached static block — so cache invalidation stays
    confined to per-session changes already happening there (today, budget)."""
    prompt = _system_prompt(
        session_id="sess_abc123",
        expected_output="text",
        budget={"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
    )
    # session id appears in the dynamic <this_task> section
    assert "lifeos_session_id=sess_abc123" in prompt
    # but the static portion stays cache-clean: no session_id leakage
    static, _, dynamic = prompt.partition("<this_task>")
    assert "sess_abc123" not in static
    assert "lifeos_session_id" not in static


@pytest.mark.unit
def test_system_prompt_static_portion_is_cache_friendly():
    """The static portion must not vary across sessions, only the trailing
    `<this_task>` section. Two calls with different session args should
    share the static prefix verbatim."""
    a = _system_prompt(
        session_id="sess_a", expected_output="text",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 0.5},
    )
    b = _system_prompt(
        session_id="sess_b", expected_output="file",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
    )
    # Both share the static prefix exactly.
    assert a.startswith(_SYSTEM_PROMPT_STATIC)
    assert b.startswith(_SYSTEM_PROMPT_STATIC)
    # And differ only in the trailing per-task block.
    assert a[:len(_SYSTEM_PROMPT_STATIC)] == b[:len(_SYSTEM_PROMPT_STATIC)]


@pytest.mark.unit
def test_system_prompt_carries_soft_budget_in_this_task_section():
    """Dynamic per-task content goes in <this_task>. Budget is framed as
    a soft target (worker enforces externally; model can't count cost)."""
    prompt = _system_prompt(
        session_id="sess_x", expected_output="text",
        budget={"wall_seconds": 90, "max_tokens": 5000, "max_dollars": 0.25},
    )
    # <this_task> wraps the dynamic part
    assert "<this_task>" in prompt
    assert "expected_output=text" in prompt
    assert "soft budget" in prompt
    assert "~90s" in prompt
    assert "~5000 tokens" in prompt
    assert "~$0.25" in prompt


@pytest.mark.unit
def test_system_prompt_includes_today_for_day_relative_reasoning():
    """The local model has a fixed training cutoff. Without today's date
    it hallucinates plausible-looking but wrong dates when the task
    involves calendar / due-date reasoning. End-to-end test caught
    Gemma using 2025-05-14 on a 2026-05-26 calendar lookup."""
    import re
    prompt = _system_prompt(
        session_id="sess_x", expected_output="text",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 0.5},
    )
    # today=YYYY-MM-DD (Weekday) inside <this_task>
    m = re.search(r"today=(\d{4}-\d{2}-\d{2}) \([A-Z][a-z]+day\)", prompt)
    assert m is not None, f"expected today=YYYY-MM-DD (Weekday); got: {prompt[-300:]}"
