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
    LocalExecutor,
)
from api.services.agent_worker.pricing import cost_for
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
    # 1k input + 1k output of Opus = $0.015 + $0.075 = $0.09
    cost = cost_for("claude-opus-4-7", 1000, 1000)
    assert cost == pytest.approx(0.015 + 0.075)


@pytest.mark.unit
def test_pricing_unknown_model_falls_through_to_opus_rate():
    """Conservative: unknown model = highest plausible price (so budgets stay
    enforced rather than silently suppressed by a typo)."""
    unknown = cost_for("typoed-model", 1000, 1000)
    opus = cost_for("claude-opus-4-7", 1000, 1000)
    assert unknown == pytest.approx(opus)
