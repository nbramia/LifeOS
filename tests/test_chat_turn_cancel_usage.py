"""#615: a native turn that ends via `asyncio.CancelledError` (explicit
cancel, supersede, or the detached-lifetime deadline) must still record its
usage row -- the tokens were already spent and billed even though the loop
never reached its terminal `result` event.

`run_agent_loop` now yields a `turn_state` event carrying a live, mutable
reference to its `AgentResult` before doing any work; `chat.py` stashes it
and reads accrued usage from it in the `CancelledError` handler if the
normal end-of-turn write never ran. These tests exercise that path directly
against real `ChatTurn`/`TurnRegistry` and `UsageStore` instances, following
the same fake-agent-loop pattern as tests/test_chat_turn_cancel.py.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from api.routes import chat, conversations
from api.services import chat_turns
from api.services.conversation_store import ConversationStore
from api.services.usage_store import get_usage_store
from config.settings import settings

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    monkeypatch.setattr(conversations, "get_store", lambda: s)
    return s


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(conversations.router)
    app.include_router(chat.router)

    async def _call(method, path, **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
            return await c.request(method, path, **kw)

    return _call


async def _drive_until(gen, min_events):
    events = []
    async for raw in gen:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
        if len(events) >= min_events:
            break
    return events


def _fake_loop_with_live_usage(hold, first_chunk="Hello "):
    """A fake agent loop that emits `turn_state` up front (as the real
    `run_agent_loop` does), streams one round of text, accrues usage on
    that same object -- mirroring `_track_usage` mutating `result` in
    place -- then blocks on `hold` before it would ever reach a terminal
    `result` event."""
    async def fake_loop(**kwargs):
        live = SimpleNamespace(
            total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
            model="fake-model", tool_calls_log=[], full_text="",
        )
        yield {"type": "turn_state", "result": live}
        yield {"type": "text", "content": first_chunk}
        # A round "completed": usage accrues on the live object, exactly
        # like _track_usage() does inside run_agent_loop.
        live.total_input_tokens = 42
        live.total_output_tokens = 7
        live.total_cost_usd = 0.0123
        await hold.wait()
        yield {"type": "text", "content": "unreachable"}
        live.full_text = first_chunk + "unreachable"
        yield {"type": "result", "result": live}
    return fake_loop


def _fake_loop_turn_state_before_any_round(hold):
    """Emits `turn_state` (zero usage) then blocks before any round ever
    completes -- no usage was ever accrued."""
    async def fake_loop(**kwargs):
        live = SimpleNamespace(
            total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
            model="fake-model", tool_calls_log=[], full_text="",
        )
        yield {"type": "turn_state", "result": live}
        await hold.wait()
        yield {"type": "text", "content": "unreachable"}
        yield {"type": "result", "result": live}
    return fake_loop


class TestCancelledTurnRecordsUsage:
    async def test_cancelled_after_one_round_records_nonzero_usage(
        self, api_client, store, monkeypatch,
    ):
        import api.services.agent_loop as agent_loop_mod

        hold = asyncio.Event()

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop_with_live_usage(hold))

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "Hello "
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")
        await gen.aclose()  # detach -- turn keeps running, unwatched

        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.json() == {"ok": True, "cancelled": True}

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        if turn is not None and turn.task is not None:
            with pytest.raises(asyncio.CancelledError):
                await turn.task

        usage = get_usage_store().get_conversation_usage(conversation_id)
        assert usage["turn_count"] == 1
        assert usage["input_tokens"] == 42
        assert usage["output_tokens"] == 7
        assert usage["cost_usd"] == pytest.approx(0.0123)

        # The partial-text persistence #611 already guarantees is unaffected.
        messages = store.get_messages(conversation_id)
        assert "Hello " in messages[1].content
        assert "cut off" in messages[1].content

    async def test_cancelled_before_any_round_writes_no_usage_row(
        self, api_client, store, monkeypatch,
    ):
        import api.services.agent_loop as agent_loop_mod

        hold = asyncio.Event()

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop_turn_state_before_any_round(hold))

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 2)  # conversation_id, routing -- no content ever arrives
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")
        await gen.aclose()

        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.json() == {"ok": True, "cancelled": True}

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        if turn is not None and turn.task is not None:
            with pytest.raises(asyncio.CancelledError):
                await turn.task

        usage = get_usage_store().get_conversation_usage(conversation_id)
        assert usage["turn_count"] == 0

    async def test_deadline_cancellation_records_usage_too(
        self, store, monkeypatch,
    ):
        """Not just the explicit /cancel endpoint -- the detached-lifetime
        deadline watcher (chat_turns.ChatTurn.reader()'s finally -> arm_deadline)
        also runs the turn's task through the same `except CancelledError`,
        so it must record usage on the same terms."""
        import api.services.agent_loop as agent_loop_mod

        monkeypatch.setattr(settings, "detached_turn_timeout_seconds", 0.05)

        hold = asyncio.Event()  # never set -- the deadline, not `hold`, ends this turn

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_loop_with_live_usage(hold))

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "Hello "
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")
        await gen.aclose()  # detach -- arms the deadline watcher (text modality)

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        assert turn is not None
        with pytest.raises(asyncio.CancelledError):
            await turn.task
        assert turn.cancel_reason == "deadline"

        usage = get_usage_store().get_conversation_usage(conversation_id)
        assert usage["turn_count"] == 1
        assert usage["input_tokens"] == 42
        assert usage["output_tokens"] == 7


class TestCompletedTurnStillRecordsExactlyOneRow:
    async def test_no_double_write_on_normal_completion(self, store, monkeypatch):
        """Regression guard for #611's own acceptance criterion: a turn that
        completes normally (never cancelled) must still write exactly one
        usage row, via the pre-existing end-of-turn path -- the new
        `turn_state` reference must not cause a second write."""
        import api.services.agent_loop as agent_loop_mod

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        async def fake_loop(**kwargs):
            live = SimpleNamespace(
                total_input_tokens=10, total_output_tokens=5, total_cost_usd=0.001,
                model="fake-model", tool_calls_log=[], full_text="",
            )
            yield {"type": "turn_state", "result": live}
            yield {"type": "text", "content": "the full answer"}
            live.total_input_tokens = 10
            live.total_output_tokens = 5
            live.full_text = "the full answer"
            yield {"type": "result", "result": live}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        events = await _drive_until(response.body_iterator, 100)  # drain to "done"
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        if turn is not None and turn.task is not None:
            await turn.task  # let the turn's own finally (pop/close) finish

        usage = get_usage_store().get_conversation_usage(conversation_id)
        assert usage["turn_count"] == 1
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 5


class TestFakeLoopWithoutTurnStateEvent:
    async def test_a_loop_that_never_emits_turn_state_still_cancels_cleanly(
        self, api_client, store, monkeypatch,
    ):
        """A consumer (or, as here, a test double) that predates #615 and
        never emits `turn_state` must be unaffected: `live_result` simply
        stays None, no usage row is invented, and cancellation still
        persists the partial text exactly as #611 designed it."""
        import api.services.agent_loop as agent_loop_mod

        hold = asyncio.Event()

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "Hello "}
            await hold.wait()
            yield {"type": "text", "content": "unreachable"}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="Hello unreachable",
            )}

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "Hello "
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")
        await gen.aclose()

        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.json() == {"ok": True, "cancelled": True}

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        if turn is not None and turn.task is not None:
            with pytest.raises(asyncio.CancelledError):
                await turn.task

        messages = store.get_messages(conversation_id)
        assert "Hello " in messages[1].content
        assert "cut off" in messages[1].content

        usage = get_usage_store().get_conversation_usage(conversation_id)
        assert usage["turn_count"] == 0
