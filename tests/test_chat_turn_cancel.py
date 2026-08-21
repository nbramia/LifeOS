"""#611: the explicit cancellation path (`POST /api/conversations/{id}/cancel`),
`active_turn` surfacing on `GET /api/conversations/{id}`, and supersede (a new
turn on a conversation that already has one in flight cancels the old one
first).

Uses a lightweight app (just the chat + conversations routers, no lifespan)
against a real ConversationStore/UsageStore on throwaway dbs — the same
pattern tests/test_hermes_proxy.py and tests/test_persona_api.py already use
for this router, so these don't pay for the full app's startup.
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

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_turn_registry():
    chat_turns.reset_turn_registry()
    yield
    chat_turns.reset_turn_registry()


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    monkeypatch.setattr(conversations, "get_store", lambda: s)
    return s


@pytest.fixture
def api_client():
    """A callable `(method, path) -> Response` — each call opens and closes
    its own short-lived httpx.AsyncClient (ASGITransport is stateless/cheap),
    so a test can make several requests without hitting httpx's "can't
    reopen a closed client" guard that a single shared `async with` would."""
    app = FastAPI()
    app.include_router(conversations.router)

    async def _call(method, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
            return await c.request(method, path)

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


def _fake_agent_loop_holding_at(hold: asyncio.Event, first_chunk="Hello "):
    async def fake_loop(**kwargs):
        yield {"type": "text", "content": first_chunk}
        await hold.wait()
        yield {"type": "text", "content": "unreachable"}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="m", tool_calls_log=[], full_text=first_chunk + "unreachable",
        )}
    return fake_loop


async def _start_turn_and_disconnect(monkeypatch, question="tell me something", hold=None):
    """Start a real ask_stream() turn, pull events until the first content
    chunk, then disconnect (close the reader) -- leaving a detached,
    still-running turn behind, exactly like test_chat_turn_survives_disconnect.py."""
    import api.services.agent_loop as agent_loop_mod

    hold = hold or asyncio.Event()
    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_agent_loop_holding_at(hold))

    async def fake_classify(*a, **k):
        return None
    monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

    request = chat.AskStreamRequest(question=question)
    response = await chat.ask_stream(request)
    gen = response.body_iterator
    events = await _drive_until(gen, 3)  # conversation_id, routing, content
    conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")
    await gen.aclose()
    return conversation_id, hold


class TestCancelEndpoint:
    async def test_404_for_unknown_conversation(self, api_client, store):
        resp = await api_client("POST", "/api/conversations/does-not-exist/cancel")
        assert resp.status_code == 404

    async def test_200_cancelled_false_when_nothing_in_flight(self, api_client, store):
        conv = store.create_conversation()
        resp = await api_client("POST", f"/api/conversations/{conv.id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": False}

    async def test_cancels_an_unwatched_detached_turn_and_persists_partial(
        self, api_client, store, monkeypatch,
    ):
        conversation_id, hold = await _start_turn_and_disconnect(monkeypatch)

        # Nobody is reading the SSE stream any more (disconnected above) --
        # the turn is running unwatched. Cancel it via the real endpoint.
        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": True}

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        # The registry entry is popped once the task's own finally runs;
        # await it directly here (not via the endpoint) to deterministically
        # wait for that teardown before asserting on persisted rows.
        if turn is not None and turn.task is not None:
            with pytest.raises(asyncio.CancelledError):
                await turn.task

        messages = store.get_messages(conversation_id)
        assert [(m.role, m.content) for m in messages[:1]] == [("user", "tell me something")]
        assert len(messages) == 2
        assert messages[1].role == "assistant"
        assert "Hello " in messages[1].content
        assert "cut off" in messages[1].content
        assert messages[1].routing == {"truncated": True, "truncation_reason": "cancelled"}

        # A second cancel of the same (now-finished) conversation reports
        # nothing left in flight.
        resp2 = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp2.json() == {"ok": True, "cancelled": False}


class TestActiveTurnField:
    async def test_present_while_running_absent_once_finished(
        self, api_client, store, monkeypatch,
    ):
        conversation_id, hold = await _start_turn_and_disconnect(monkeypatch)

        resp = await api_client("GET", f"/api/conversations/{conversation_id}")
        assert resp.status_code == 200
        active = resp.json()["active_turn"]
        assert active is not None
        assert active["conversation_id"] == conversation_id
        assert active["turn_id"]
        assert active["started_at"]

        # Let the turn finish, then the field must be gone.
        hold.set()
        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        await turn.task

        resp2 = await api_client("GET", f"/api/conversations/{conversation_id}")
        assert resp2.json()["active_turn"] is None

    async def test_absent_for_a_conversation_with_no_turn(self, api_client, store):
        conv = store.create_conversation()
        resp = await api_client("GET", f"/api/conversations/{conv.id}")
        assert resp.json()["active_turn"] is None


class TestSupersede:
    async def test_new_turn_on_conversation_cancels_the_old_one_first(self, store, monkeypatch):
        """A second POST /api/ask/stream naming a conversation that already
        has a turn in flight cancels the old one before starting the new
        one -- asking again is itself a stop gesture. Both rows land, in
        order, the first marked as cut off."""
        import api.services.agent_loop as agent_loop_mod

        hold_first = asyncio.Event()

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_agent_loop_holding_at(hold_first, "first partial "))

        first_request = chat.AskStreamRequest(question="first question")
        first_response = await chat.ask_stream(first_request)
        first_gen = first_response.body_iterator
        first_events = await _drive_until(first_gen, 3)
        conversation_id = next(e["conversation_id"] for e in first_events if e.get("type") == "conversation_id")
        await first_gen.aclose()  # detach, but the first turn keeps running

        first_turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        assert first_turn is not None

        # A second, fully-synchronous fake loop for the superseding turn.
        async def fake_loop_2(**kwargs):
            yield {"type": "text", "content": "second full answer"}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=2, total_output_tokens=2, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="second full answer",
            )}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop_2)

        second_request = chat.AskStreamRequest(question="second question", conversation_id=conversation_id)
        second_response = await chat.ask_stream(second_request)
        second_gen = second_response.body_iterator
        async for _ in second_gen:
            pass  # drain fully -- this fake loop finishes immediately

        # The first turn was cancelled by the supersede, not left running.
        with pytest.raises(asyncio.CancelledError):
            await first_turn.task

        messages = store.get_messages(conversation_id)
        contents = [(m.role, m.content, m.routing) for m in messages]
        assert contents[0] == ("user", "first question", None)
        assert contents[1][0] == "assistant"
        assert "first partial " in contents[1][1]
        assert "cut off" in contents[1][1]
        assert contents[1][2] == {"truncated": True, "truncation_reason": "cancelled"}
        assert contents[2] == ("user", "second question", None)
        assert contents[3] == ("assistant", "second full answer", {
            "sources": [], "reasoning": "agentic (claude-haiku-4-5)", "tool_rounds": 0,
        })
