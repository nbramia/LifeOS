"""Tests for `api/services/jev_orchestrator_shadow.py` — the production
SHADOW instrumentation for the chat orchestrator's Jev typed-judgment
questions.

Every Jev call is stubbed via `monkeypatch.setattr(JevClient, "aask", ...)`
— no test contacts the real API (see tests/test_jev_client.py for the
client's own transport-level tests). Covers:

  - `LIFEOS_JEV_ORCHESTRATOR` gating (off/shadow/invalid) and the
    off-mode guarantee that `JevClient` is never constructed.
  - The bundle-choice and cache-thrash helpers, unit tested directly.
  - `jev_preturn`/`jev_inloop` span metadata on success, timeout, and
    error, via `perf_trace.start_trace`/`finish_trace` directly.
  - At the `agent_loop.run_agent_loop` level: the `tool_{name}` span's
    `result_preview` is attached unconditionally (not gated by the
    setting), and the in-loop shadow fires only for rounds >= 2.
  - At the `/api/ask/stream` route level: shadow mode leaves
    `run_agent_loop`'s arguments byte-identical to off mode, and a
    timed-out shadow call doesn't touch the turn's reply.
  - Fail-open coverage of client construction and state building (not just
    the Jev call itself), `cancel_and_forget`'s no-await/no-raise/no-
    unretrieved-exception guarantees, cleanup on an exception mid-loop and
    on the caller closing `run_agent_loop`'s generator early, dict-shaped
    conversation-history entries, and the bounded bundle-cache LRU.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.routes import chat
from api.services import agent_loop
from api.services import jev_orchestrator_shadow as jos
from api.services import perf_trace as perf_trace_mod
from api.services.conversation_store import ConversationStore
from api.services.jev_client import JevClient, JevError
from api.services.perf_trace import get_perf_trace_store
from config.settings import settings

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _clear_bundle_cache():
    jos._LAST_BUNDLE_BY_CONVERSATION.clear()
    yield
    jos._LAST_BUNDLE_BY_CONVERSATION.clear()


def _full_preturn_answers(**family_overrides):
    """A complete PRETURN_QUESTIONS answer set. `family_overrides` maps a
    bare family name (e.g. `fitness=0.9`) to override that family's noul
    (default 0.1 -- below the 0.5 selection threshold)."""
    answers = {
        "needs_tools": {"noul": 0.8},
        "difficulty": {"score": 1.0},
        "is_followup": {"noul": 0.1},
    }
    for fam in jos.FAMILY_TO_TOOLS:
        answers[f"family_{fam}"] = {"noul": family_overrides.get(fam, 0.1)}
    return answers


def _full_inloop_answers(is_repeating=0.2, answered=0.7):
    return {"is_repeating": {"noul": is_repeating}, "answered": {"noul": answered}}


async def _drain(gen):
    """Collect every parsed SSE `data:` event from a chat turn's body
    iterator, running it to completion."""
    events = []
    async for raw in gen:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


async def _fake_classify(*a, **k):
    return None


def _fake_loop(reply="hi"):
    """A minimal agent-loop stand-in: turn_state, one text chunk, result.
    No tool calls -- chat.py's own kwargs are what these tests check."""
    async def loop(**kwargs):
        live = SimpleNamespace(
            total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
            model="fake-model", tool_calls_log=[], full_text="",
        )
        yield {"type": "turn_state", "result": live}
        yield {"type": "text", "content": reply}
        live.full_text = reply
        yield {"type": "result", "result": live}
    return loop


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------

class TestModeGating:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        assert jos.jev_orchestrator_mode() == "off"

    def test_invalid_value_falls_back_to_off_with_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "jev_orchestrator", "bogus", raising=False)
        with caplog.at_level("WARNING"):
            assert jos.jev_orchestrator_mode() == "off"
        assert "LIFEOS_JEV_ORCHESTRATOR" in caplog.text

    def test_shadow_enabled_requires_both_setting_and_key(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        assert jos.shadow_enabled() is False  # no key

        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        assert jos.shadow_enabled() is True

        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        assert jos.shadow_enabled() is False  # key present but off


# ---------------------------------------------------------------------------
# Bundle choice / cache-thrash
# ---------------------------------------------------------------------------

class TestChooseBundle:
    def test_no_family_needed_returns_none(self):
        answers = {fam: 0.1 for fam in jos.FAMILY_TO_TOOLS}
        assert jos.choose_bundle(answers) is None

    def test_fitness_only_selects_bundle_4(self):
        answers = {fam: 0.1 for fam in jos.FAMILY_TO_TOOLS}
        answers["fitness"] = 0.9
        assert jos.choose_bundle(answers) == 4

    def test_finance_only_selects_bundle_2(self):
        answers = {fam: 0.1 for fam in jos.FAMILY_TO_TOOLS}
        answers["finance"] = 0.9
        assert jos.choose_bundle(answers) == 2

    def test_home_only_selects_no_bundle(self):
        # No bundle's tools cover the "home" family (pause/resume/status
        # internet) -- none of the k=5 bundles include those tools.
        answers = {fam: 0.1 for fam in jos.FAMILY_TO_TOOLS}
        answers["home"] = 0.9
        assert jos.choose_bundle(answers) is None

    def test_tie_breaks_to_lowest_bundle_id(self):
        # person_info (people_crm) is in every bundle -- a tie broken to
        # the lowest id.
        answers = {fam: 0.1 for fam in jos.FAMILY_TO_TOOLS}
        answers["people_crm"] = 0.9
        assert jos.choose_bundle(answers) == 0


class TestBundleChanged:
    def test_first_turn_in_conversation_is_not_changed(self):
        assert jos.bundle_changed("conv-1", 2) is False

    def test_same_bundle_next_turn_is_not_changed(self):
        jos.bundle_changed("conv-2", 3)
        assert jos.bundle_changed("conv-2", 3) is False

    def test_different_bundle_next_turn_is_changed(self):
        jos.bundle_changed("conv-3", 1)
        assert jos.bundle_changed("conv-3", 4) is True

    def test_first_turn_choosing_no_bundle_is_not_changed(self):
        assert jos.bundle_changed("conv-4", None) is False

    def test_none_to_a_bundle_next_turn_is_changed(self):
        jos.bundle_changed("conv-5", None)
        assert jos.bundle_changed("conv-5", 0) is True


class TestBuildPreturnState:
    def test_last_two_turns_role_content_and_truncation(self):
        history = [
            SimpleNamespace(role="user", content="a" * 700),
            SimpleNamespace(role="assistant", content="dropped -- only the last two are kept"),
            SimpleNamespace(role="user", content="b" * 700),
            SimpleNamespace(role="assistant", content="c" * 700),
        ]
        state = jos.build_preturn_state("primary", history, "d" * 1500)
        assert state["persona"] == "primary"
        assert [t["role"] for t in state["prev_turns"]] == ["user", "assistant"]
        assert state["prev_turns"][0]["content"] == "b" * 600
        assert state["prev_turns"][1]["content"] == "c" * 600
        assert state["message"] == "d" * 1200

    def test_empty_history(self):
        state = jos.build_preturn_state("primary", None, "hi")
        assert state["prev_turns"] == []
        assert state["message"] == "hi"


# ---------------------------------------------------------------------------
# jev_preturn span: success / timeout / error
# ---------------------------------------------------------------------------

class TestFinishPreturnSpan:
    async def test_none_task_is_a_no_op(self):
        perf_trace_mod.start_trace("conv", "question")
        await jos.finish_preturn_span(None, None, conversation_id="conv", agent_result=None)
        trace = perf_trace_mod.finish_trace()
        assert trace.spans == []

    async def test_success_records_expected_keys(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_preturn_answers(fitness=0.9)))

        perf_trace_mod.start_trace("conv-a", "question")
        task, start = jos.start_preturn_task("primary", [], "hello")
        assert task is not None
        agent_result = SimpleNamespace(tool_calls_log=[{"tool": "search_web"}])
        await jos.finish_preturn_span(task, start, conversation_id="conv-a", agent_result=agent_result)
        trace = perf_trace_mod.finish_trace()

        spans = [s for s in trace.spans if s.name == "jev_preturn"]
        assert len(spans) == 1
        meta = spans[0].metadata
        assert set(meta) == {
            "needs_tools", *[f"family_{f}" for f in jos.FAMILY_TO_TOOLS],
            "difficulty", "is_followup", "latency_ms", "before_round1",
            "families_needed", "bundle_id", "bundle_changed", "tool_count", "round_count",
        }
        assert meta["families_needed"] == ["fitness"]
        assert meta["tool_count"] == 1
        assert meta["round_count"] == 0  # no llm_api_round_* spans in this unit test
        assert meta["bundle_id"] == 4  # fitness -> bundle 4
        assert meta["bundle_changed"] is False  # first turn recorded for conv-a

    async def test_timeout_records_error_only_and_turn_is_unaffected(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(2)
            return _full_preturn_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        perf_trace_mod.start_trace("conv-b", "question")
        task, start = jos.start_preturn_task("primary", [], "hello")
        await jos.finish_preturn_span(task, start, conversation_id="conv-b", agent_result=None)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_preturn")
        assert span.metadata == {"error": "TimeoutError"}

    async def test_jev_error_records_error_only(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)

        async def _boom(self, state, questions, *, model=None):
            raise JevError("boom")
        monkeypatch.setattr(JevClient, "aask", _boom)

        perf_trace_mod.start_trace("conv-c", "question")
        task, start = jos.start_preturn_task("primary", [], "hello")
        await jos.finish_preturn_span(task, start, conversation_id="conv-c", agent_result=None)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_preturn")
        assert span.metadata == {"error": "JevError"}


class TestFinishInloopSpan:
    async def test_none_task_is_a_no_op(self):
        perf_trace_mod.start_trace("conv", "question")
        await jos.finish_inloop_span(None, None, round_index=2)
        trace = perf_trace_mod.finish_trace()
        assert trace.spans == []

    async def test_success_records_probabilities_and_round_index(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_inloop_answers(0.1, 0.6)))

        perf_trace_mod.start_trace("conv-d", "question")
        task, start = jos.start_inloop_task("hello", {1: [], 2: []}, 2)
        await jos.finish_inloop_span(task, start, round_index=2)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_inloop")
        assert span.metadata == {"is_repeating": 0.1, "answered": 0.6, "round_index": 2}

    async def test_timeout_records_error_only(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(2)
            return _full_inloop_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        perf_trace_mod.start_trace("conv-e", "question")
        task, start = jos.start_inloop_task("hello", {1: []}, 1)
        await jos.finish_inloop_span(task, start, round_index=2)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_inloop")
        assert span.metadata == {"error": "TimeoutError"}


# ---------------------------------------------------------------------------
# agent_loop.run_agent_loop level
# ---------------------------------------------------------------------------

class _OneToolThenTextClient:
    """One tool-call round, then a final text round."""

    def __init__(self):
        self._round = 0

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        if self._round == 0:
            yield {"type": "tool_calls", "calls": [{
                "id": "c0", "function": {"name": "search_vault", "arguments": "{}"},
            }]}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
        else:
            yield {"type": "text", "content": "done"}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}
        self._round += 1


class _ThreeToolRoundClient:
    """Three rounds of tool calls, then a final text round -- for the
    in-loop shadow test (fires for rounds 2 and 3 only)."""

    def __init__(self):
        self._round = 0

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        if self._round < 3:
            yield {"type": "tool_calls", "calls": [{
                "id": f"c{self._round}", "function": {"name": "search_vault", "arguments": "{}"},
            }]}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
        else:
            yield {"type": "text", "content": "final"}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}
        self._round += 1


class TestResultPreviewOnToolSpan:
    async def test_attached_unconditionally_and_truncated_at_300(self, monkeypatch):
        # Off mode -- result_preview must still be attached; it isn't
        # gated by LIFEOS_JEV_ORCHESTRATOR.
        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        long_result = "x" * 500

        with patch.object(agent_loop, "_select_client", return_value=_OneToolThenTextClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value=long_result)):
            perf_trace_mod.start_trace("conv", "q")
            [e async for e in agent_loop.run_agent_loop("find my notes")]
            trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "tool_search_vault")
        assert span.metadata["result_preview"] == long_result[:300]
        assert len(span.metadata["result_preview"]) == 300

    async def test_short_result_is_not_padded(self, monkeypatch):
        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        with patch.object(agent_loop, "_select_client", return_value=_OneToolThenTextClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="short")):
            perf_trace_mod.start_trace("conv", "q")
            [e async for e in agent_loop.run_agent_loop("find my notes")]
            trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "tool_search_vault")
        assert span.metadata["result_preview"] == "short"


class TestOffModeNeverConstructsJevClient:
    async def test_off_mode_across_a_multi_round_turn(self, monkeypatch):
        # Even with a key configured, `off` must never construct a client.
        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        def _boom_init(self, *a, **kw):
            raise AssertionError("JevClient must not be constructed when the setting is off")
        monkeypatch.setattr(JevClient, "__init__", _boom_init)

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="ok")):
            perf_trace_mod.start_trace("conv", "q")
            events = [e async for e in agent_loop.run_agent_loop(
                "do something", max_tool_rounds=5, user_message="do something",
            )]
            trace = perf_trace_mod.finish_trace()

        assert any(e["type"] == "result" for e in events)
        assert not any(s.name == "jev_inloop" for s in trace.spans)


class TestInloopShadowFiring:
    async def test_fires_for_rounds_2_and_3_only(self, monkeypatch):
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_inloop_answers()))

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="ok result")):
            perf_trace_mod.start_trace("conv-inloop", "q")
            [e async for e in agent_loop.run_agent_loop(
                "do something", max_tool_rounds=5, user_message="do something",
            )]
            trace = perf_trace_mod.finish_trace()

        inloop_spans = [s for s in trace.spans if s.name == "jev_inloop"]
        assert sorted(s.metadata["round_index"] for s in inloop_spans) == [2, 3]
        for s in inloop_spans:
            assert set(s.metadata) == {"is_repeating", "answered", "round_index"}

    async def test_one_call_per_round_cap(self, monkeypatch):
        """Exactly one jev_inloop span per eligible round -- never more."""
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_inloop_answers()))

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="ok")):
            perf_trace_mod.start_trace("conv-cap", "q")
            [e async for e in agent_loop.run_agent_loop(
                "do something", max_tool_rounds=5, user_message="do something",
            )]
            trace = perf_trace_mod.finish_trace()

        by_round = {}
        for s in trace.spans:
            if s.name == "jev_inloop":
                by_round[s.metadata["round_index"]] = by_round.get(s.metadata["round_index"], 0) + 1
        assert set(by_round.values()) == {1}


# ---------------------------------------------------------------------------
# /api/ask/stream route level
# ---------------------------------------------------------------------------

class TestChatRouteShadowWiring:
    async def test_shadow_mode_leaves_run_agent_loop_args_unchanged(self, store, monkeypatch):
        import api.services.agent_loop as agent_loop_mod

        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        captured = []

        def make_fake_loop():
            async def loop(**kwargs):
                captured.append(kwargs)
                live = SimpleNamespace(
                    total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                    model="fake-model", tool_calls_log=[], full_text="",
                )
                yield {"type": "turn_state", "result": live}
                yield {"type": "text", "content": "hi"}
                live.full_text = "hi"
                yield {"type": "result", "result": live}
            return loop

        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", make_fake_loop())
        await _drain((await chat.ask_stream(chat.AskStreamRequest(question="hello there"))).body_iterator)

        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_preturn_answers()))
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", make_fake_loop())
        await _drain((await chat.ask_stream(chat.AskStreamRequest(question="hello there"))).body_iterator)

        assert len(captured) == 2
        off_kwargs, shadow_kwargs = captured
        assert off_kwargs["persona_id"] == shadow_kwargs["persona_id"]
        assert off_kwargs["max_tool_rounds"] == shadow_kwargs["max_tool_rounds"] == 5
        # Nothing about the call differs between modes.
        assert off_kwargs == shadow_kwargs

    async def test_shadow_mode_records_jev_preturn_span(self, store, monkeypatch):
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_preturn_answers(fitness=0.9)))

        import api.services.agent_loop as agent_loop_mod
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop("hi"))

        req = chat.AskStreamRequest(question="find my last workout")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        trace_evt = next(e for e in events if e.get("type") == "perf_trace")
        stored = get_perf_trace_store().get_trace(trace_evt["trace_id"])
        jev_spans = [s for s in stored["spans"] if s["name"] == "jev_preturn"]
        assert len(jev_spans) == 1
        meta = jev_spans[0]["metadata"]
        assert meta["bundle_id"] == 4
        assert meta["tool_count"] == 0
        assert "error" not in meta

    async def test_off_mode_no_jev_preturn_span_and_no_client_constructed(self, store, monkeypatch):
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "off", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        def _boom_init(self, *a, **kw):
            raise AssertionError("JevClient must not be constructed in off mode")
        monkeypatch.setattr(JevClient, "__init__", _boom_init)

        import api.services.agent_loop as agent_loop_mod
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop("hi"))

        req = chat.AskStreamRequest(question="find my last workout")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        trace_evt = next(e for e in events if e.get("type") == "perf_trace")
        stored = get_perf_trace_store().get_trace(trace_evt["trace_id"])
        assert not any(s["name"] == "jev_preturn" for s in stored["spans"])

    async def test_shadow_timeout_leaves_turn_reply_unchanged(self, store, monkeypatch):
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(2)
            return _full_preturn_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        import api.services.agent_loop as agent_loop_mod
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop("expected reply"))

        req = chat.AskStreamRequest(question="hello")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        content = "".join(e["content"] for e in events if e.get("type") == "content")
        assert content == "expected reply"

        trace_evt = next(e for e in events if e.get("type") == "perf_trace")
        stored = get_perf_trace_store().get_trace(trace_evt["trace_id"])
        jev_span = next(s for s in stored["spans"] if s["name"] == "jev_preturn")
        assert jev_span["metadata"] == {"error": "TimeoutError"}


# ---------------------------------------------------------------------------
# cancel_and_forget: never awaits, never raises, never leaves an exception
# unretrieved.
# ---------------------------------------------------------------------------

class TestCancelAndForget:
    def test_none_task_is_a_no_op(self):
        jos.cancel_and_forget(None)  # must not raise

    async def test_cancels_a_pending_task_without_awaiting_it(self):
        async def _slow():
            await asyncio.sleep(10)
            return "done"
        task = asyncio.create_task(_slow())
        jos.cancel_and_forget(task)
        assert not task.done()  # cancel() only requests cancellation
        await asyncio.sleep(0)  # let the cancellation actually land
        assert task.cancelled()

    async def test_consumes_a_finished_tasks_exception_without_raising(self):
        async def _boom():
            raise RuntimeError("boom")
        task = asyncio.create_task(_boom())
        await asyncio.sleep(0)  # let it actually run and fail
        assert task.done()
        jos.cancel_and_forget(task)  # must not raise, even though task already failed
        await asyncio.sleep(0)  # let the done-callback run
        assert isinstance(task.exception(), RuntimeError)  # retrieved, so no gc warning

    async def test_no_unretrieved_exception_warning_on_gc(self, caplog):
        import gc
        import logging

        async def _boom():
            raise RuntimeError("boom")
        task = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        jos.cancel_and_forget(task)
        await asyncio.sleep(0)
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            del task
            gc.collect()
        assert "was never retrieved" not in caplog.text


# ---------------------------------------------------------------------------
# Item: client-construction failure is inside the fail-open boundary too.
# ---------------------------------------------------------------------------

class TestFailOpenOnClientConstruction:
    async def test_client_init_raising_still_records_error_span_preturn(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)

        def _boom_init(self, *a, **kw):
            raise AssertionError("boom from JevClient.__init__")
        monkeypatch.setattr(JevClient, "__init__", _boom_init)

        perf_trace_mod.start_trace("conv-ctor", "q")
        task, start = jos.start_preturn_task("primary", [], "hello")
        assert task is not None  # setup succeeds synchronously; the failure is inside the task
        await jos.finish_preturn_span(task, start, conversation_id="conv-ctor", agent_result=None)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_preturn")
        assert span.metadata == {"error": "AssertionError"}

    async def test_client_init_raising_still_records_error_span_inloop(self, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)

        def _boom_init(self, *a, **kw):
            raise AssertionError("boom from JevClient.__init__")
        monkeypatch.setattr(JevClient, "__init__", _boom_init)

        perf_trace_mod.start_trace("conv-ctor2", "q")
        task, start = jos.start_inloop_task("hello", {1: []}, 1)
        assert task is not None
        await jos.finish_inloop_span(task, start, round_index=2)
        trace = perf_trace_mod.finish_trace()

        span = next(s for s in trace.spans if s.name == "jev_inloop")
        assert span.metadata == {"error": "AssertionError"}

    async def test_turn_completes_normally_when_client_init_raises(self, store, monkeypatch):
        """Route-level: the failure never surfaces to the user."""
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        def _boom_init(self, *a, **kw):
            raise AssertionError("boom from JevClient.__init__")
        monkeypatch.setattr(JevClient, "__init__", _boom_init)

        import api.services.agent_loop as agent_loop_mod
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop("expected reply"))

        req = chat.AskStreamRequest(question="hello")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        content = "".join(e["content"] for e in events if e.get("type") == "content")
        assert content == "expected reply"
        assert not any(e.get("type") == "error" for e in events)

        trace_evt = next(e for e in events if e.get("type") == "perf_trace")
        stored = get_perf_trace_store().get_trace(trace_evt["trace_id"])
        jev_span = next(s for s in stored["spans"] if s["name"] == "jev_preturn")
        assert jev_span["metadata"] == {"error": "AssertionError"}


# ---------------------------------------------------------------------------
# build_preturn_state: conversation-history entries may be dicts OR objects.
# ---------------------------------------------------------------------------

class TestBuildPreturnStateDictHistory:
    def test_dict_shaped_history_entries_are_supported(self):
        history = [
            {"role": "user", "content": "a" * 700},
            {"role": "assistant", "content": "b" * 700},
        ]
        state = jos.build_preturn_state("primary", history, "hi")
        assert [t["role"] for t in state["prev_turns"]] == ["user", "assistant"]
        assert state["prev_turns"][0]["content"] == "a" * 600
        assert state["prev_turns"][1]["content"] == "b" * 600

    def test_mixed_object_and_dict_history_entries(self):
        history = [
            SimpleNamespace(role="user", content="object-shaped"),
            {"role": "assistant", "content": "dict-shaped"},
        ]
        state = jos.build_preturn_state("primary", history, "hi")
        assert state["prev_turns"] == [
            {"role": "user", "content": "object-shaped"},
            {"role": "assistant", "content": "dict-shaped"},
        ]


# ---------------------------------------------------------------------------
# _LAST_BUNDLE_BY_CONVERSATION: bounded LRU.
# ---------------------------------------------------------------------------

class TestBundleCacheBounded:
    def test_evicts_least_recently_touched_beyond_max_tracked(self, monkeypatch):
        monkeypatch.setattr(jos, "_MAX_TRACKED_CONVERSATIONS", 3)
        for i in range(3):
            jos.bundle_changed(f"conv-{i}", i)
        assert list(jos._LAST_BUNDLE_BY_CONVERSATION.keys()) == ["conv-0", "conv-1", "conv-2"]

        jos.bundle_changed("conv-3", 3)  # pushes out conv-0, the oldest
        assert "conv-0" not in jos._LAST_BUNDLE_BY_CONVERSATION
        assert list(jos._LAST_BUNDLE_BY_CONVERSATION.keys()) == ["conv-1", "conv-2", "conv-3"]

    def test_touching_an_existing_entry_keeps_it_from_eviction(self, monkeypatch):
        monkeypatch.setattr(jos, "_MAX_TRACKED_CONVERSATIONS", 3)
        for i in range(3):
            jos.bundle_changed(f"conv-{i}", i)
        jos.bundle_changed("conv-0", 99)  # re-touch conv-0 -- now most recent
        jos.bundle_changed("conv-3", 3)  # should evict conv-1 (now oldest), not conv-0
        assert "conv-0" in jos._LAST_BUNDLE_BY_CONVERSATION
        assert "conv-1" not in jos._LAST_BUNDLE_BY_CONVERSATION


# ---------------------------------------------------------------------------
# Abnormal exit: an exception, a cancellation, or the caller closing
# run_agent_loop's generator early must never leak a pending shadow task.
# ---------------------------------------------------------------------------

class TestAbnormalExitCleanup:
    async def test_agent_loop_exception_propagates_and_cleans_up_pending_inloop_task(self, monkeypatch):
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(10)  # never resolves before cleanup runs
            return _full_inloop_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        calls = []
        real_cancel_and_forget = agent_loop.cancel_and_forget

        def _spy(task):
            calls.append(task)
            real_cancel_and_forget(task)
        monkeypatch.setattr(agent_loop, "cancel_and_forget", _spy)

        # Tool execution succeeds for the first two rounds, then raises on
        # the third -- by then the second round's in-loop task has already
        # been started.
        mock_exec = AsyncMock(side_effect=["ok first", "ok second", RuntimeError("boom third")])

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", mock_exec):
            perf_trace_mod.start_trace("conv-abnormal", "q")
            with pytest.raises(RuntimeError, match="boom third"):
                [e async for e in agent_loop.run_agent_loop(
                    "do something", max_tool_rounds=5, user_message="do something",
                )]
            trace = perf_trace_mod.finish_trace()

        assert len(calls) == 1  # only the second round's task existed when the exception hit
        await asyncio.sleep(0)  # let the cancellation land
        assert calls[0].cancelled()
        assert not any(s.name == "jev_inloop" for s in trace.spans)  # abandoned, never recorded

    async def test_early_close_cleans_up_pending_inloop_task(self, monkeypatch):
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(10)
            return _full_inloop_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        calls = []
        real_cancel_and_forget = agent_loop.cancel_and_forget

        def _spy(task):
            calls.append(task)
            real_cancel_and_forget(task)
        monkeypatch.setattr(agent_loop, "cancel_and_forget", _spy)

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="ok")):
            perf_trace_mod.start_trace("conv-close", "q")
            gen = agent_loop.run_agent_loop("do something", max_tool_rounds=5, user_message="do something")
            # Event order: turn_state, then one status event per round's
            # tool call. The second round's in-loop task is started between
            # the status events for the second and third rounds (after its
            # own tool results are gathered, before the third round yields
            # its status event) -- consuming exactly four events then
            # closing catches that task started but never awaited.
            for _ in range(4):
                await gen.__anext__()
            await gen.aclose()
            trace = perf_trace_mod.finish_trace()

        assert len(calls) == 1
        await asyncio.sleep(0)
        assert calls[0].cancelled()
        assert not any(s.name == "jev_inloop" for s in trace.spans)

    async def test_chat_route_mid_stream_exception_cleans_up_preturn_task(self, store, monkeypatch):
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)

        async def _slow_aask(self, state, questions, *, model=None):
            await asyncio.sleep(10)
            return _full_preturn_answers()
        monkeypatch.setattr(JevClient, "aask", _slow_aask)

        calls = []
        real_cancel_and_forget = jos.cancel_and_forget

        def _spy(task):
            calls.append(task)
            real_cancel_and_forget(task)
        monkeypatch.setattr(jos, "cancel_and_forget", _spy)

        import api.services.agent_loop as agent_loop_mod

        class _Boom(Exception):
            pass

        async def fake_loop(**kwargs):
            live = SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="fake-model", tool_calls_log=[], full_text="",
            )
            yield {"type": "turn_state", "result": live}
            yield {"type": "text", "content": "partial"}
            raise _Boom("mid-stream failure")
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)

        req = chat.AskStreamRequest(question="hello")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        # chat.py's pre-existing outer handler turns the exception into an
        # 'error' event rather than crashing the SSE stream; it does not
        # emit a perf_trace event on this path, so no jev_preturn span was
        # recorded (finish_preturn_span is never reached on this path).
        assert any(e.get("type") == "error" for e in events)
        assert not any(e.get("type") == "perf_trace" for e in events)

        assert len(calls) == 1  # the pre-turn task was handed to cleanup
        await asyncio.sleep(0)
        assert calls[0].cancelled()


# ---------------------------------------------------------------------------
# Mutation-witness tests: each of these must fail under a specific,
# targeted code mutation, proving the assertion actually exercises the
# behavior it names rather than passing vacuously.
# ---------------------------------------------------------------------------

class TestMutationChecks:
    """Pins the specific assertion that fails under each mutation below --
    see the commit message for the live mutation-and-revert results."""

    async def test_round_num_gate_mutation_would_be_caught(self, monkeypatch):
        """Guards `if round_num >= 2:` in agent_loop.py. If that condition
        became `if False:`, no in-loop task would ever start, and this
        assertion (round_index 2 and 3 present) would fail."""
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_inloop_answers()))

        with patch.object(agent_loop, "_select_client", return_value=_ThreeToolRoundClient()), \
             patch("api.services.agent_loop.execute_tool_parallel", AsyncMock(return_value="ok")):
            perf_trace_mod.start_trace("conv-mutation-a", "q")
            [e async for e in agent_loop.run_agent_loop(
                "do something", max_tool_rounds=5, user_message="do something",
            )]
            trace = perf_trace_mod.finish_trace()

        inloop_spans = [s for s in trace.spans if s.name == "jev_inloop"]
        assert sorted(s.metadata["round_index"] for s in inloop_spans) == [2, 3]

    async def test_span_name_mutation_would_be_caught(self, store, monkeypatch):
        """Guards the literal `"jev_preturn"` span name in
        jev_orchestrator_shadow.py. If that string were renamed, this
        lookup (by that exact name) would find zero spans."""
        monkeypatch.setattr(chat, "classify_action_intent", _fake_classify)
        monkeypatch.setattr(settings, "jev_orchestrator", "shadow", raising=False)
        monkeypatch.setattr(settings, "typesafe_api_key", "test-key", raising=False)
        monkeypatch.setattr(JevClient, "aask", AsyncMock(return_value=_full_preturn_answers()))

        import api.services.agent_loop as agent_loop_mod
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop("hi"))

        req = chat.AskStreamRequest(question="find my last workout")
        events = await _drain((await chat.ask_stream(req)).body_iterator)

        trace_evt = next(e for e in events if e.get("type") == "perf_trace")
        stored = get_perf_trace_store().get_trace(trace_evt["trace_id"])
        jev_spans = [s for s in stored["spans"] if s["name"] == "jev_preturn"]
        assert len(jev_spans) == 1
