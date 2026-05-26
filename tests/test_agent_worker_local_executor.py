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
    _system_prompt,
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
