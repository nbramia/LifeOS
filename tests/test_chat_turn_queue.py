"""Per-conversation turn queue (`if_busy: "queue"` on POST /api/ask/stream).

A queued turn does not cancel the turn already running for its
conversation; it waits server-side (whether or not any client is attached),
runs after every turn ahead of it, and sees their answers in its history.
`POST /api/conversations/{id}/cancel` drops the queue along with the running
turn, and `GET /api/conversations/{id}` reports how many turns are waiting.
Omitting `if_busy` keeps supersede.
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


@pytest.fixture
def scripted_loop(monkeypatch):
    """A fake agent loop that answers each question with `answer to <q>`,
    holding at a per-question gate when one is registered, and records the
    history each turn was given."""
    import api.services.agent_loop as agent_loop_mod

    gates: dict[str, asyncio.Event] = {}
    histories: dict[str, list[tuple[str, str]]] = {}
    started: list[str] = []

    async def fake_loop(**kwargs):
        question = kwargs["question"]
        started.append(question)
        histories[question] = [(m.role, m.content) for m in kwargs["conversation_history"] or []]
        yield {"type": "text", "content": f"answer to {question}"}
        gate = gates.get(question)
        if gate is not None:
            await gate.wait()
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="m", tool_calls_log=[], full_text=f"answer to {question}",
        )}

    async def fake_classify(*a, **k):
        return None

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
    return SimpleNamespace(gates=gates, histories=histories, started=started)


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


async def _drain(gen):
    events = []
    async for raw in gen:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


async def _start_held_first_turn(scripted_loop, question="first question"):
    """Start a turn on a new conversation that holds after its first chunk,
    then disconnect from it. Returns (conversation_id, gate, turn)."""
    gate = asyncio.Event()
    scripted_loop.gates[question] = gate
    response = await chat.ask_stream(chat.AskStreamRequest(question=question))
    gen = response.body_iterator
    events = await _drive_until(gen, 3)
    conversation_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    await gen.aclose()
    turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
    assert turn is not None
    return conversation_id, gate, turn


async def test_queued_turn_runs_after_the_first_with_its_answer_in_history(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)

    second = await chat.ask_stream(chat.AskStreamRequest(
        question="second question", conversation_id=conversation_id,
        if_busy="queue", client_turn_id="client-turn-2",
    ))
    second_gen = second.body_iterator
    first_events = await _drive_until(second_gen, 1)
    assert first_events == [{
        "type": "queued", "conversation_id": conversation_id,
        "position": 1, "client_turn_id": "client-turn-2",
    }]

    # The running turn was not cancelled, and the queued one has not started.
    await asyncio.sleep(0.05)
    assert not first_turn.task.done()
    assert scripted_loop.started == ["first question"]
    assert [m.content for m in store.get_messages(conversation_id)] == ["first question"]

    gate.set()
    rest = await _drain(second_gen)
    await first_turn.task

    assert rest[0] == {"type": "conversation_id", "conversation_id": conversation_id}
    assert {"type": "content", "content": "answer to second question"} in rest
    assert rest[-1] == {"type": "done"}
    assert scripted_loop.histories["second question"] == [
        ("user", "first question"),
        ("assistant", "answer to first question"),
    ]
    assert [(m.role, m.content) for m in store.get_messages(conversation_id)] == [
        ("user", "first question"),
        ("assistant", "answer to first question"),
        ("user", "second question"),
        ("assistant", "answer to second question"),
    ]


async def test_queued_turns_run_fifo_with_no_client_attached(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)

    queued_turns = []
    for position, question in enumerate(["second question", "third question"], start=1):
        response = await chat.ask_stream(chat.AskStreamRequest(
            question=question, conversation_id=conversation_id, if_busy="queue",
        ))
        gen = response.body_iterator
        events = await _drive_until(gen, 1)
        assert events[0]["type"] == "queued" and events[0]["position"] == position
        await gen.aclose()  # every client gone
        queued_turns.append(next(
            t for t in chat_turns.get_turn_registry()._by_turn_id.values()
            if t.queued and t not in queued_turns
        ))

    gate.set()
    await first_turn.task
    for turn in queued_turns:
        await turn.task

    assert scripted_loop.started == ["first question", "second question", "third question"]
    assert scripted_loop.histories["third question"] == [
        ("user", "first question"), ("assistant", "answer to first question"),
        ("user", "second question"), ("assistant", "answer to second question"),
    ]
    assert len(store.get_messages(conversation_id)) == 6
    assert chat_turns.get_turn_registry().is_busy(conversation_id) is False


async def test_queue_on_an_idle_conversation_runs_immediately(store, scripted_loop):
    conv = store.create_conversation()
    response = await chat.ask_stream(chat.AskStreamRequest(
        question="only question", conversation_id=conv.id, if_busy="queue",
    ))
    events = await _drain(response.body_iterator)
    assert events[0] == {"type": "conversation_id", "conversation_id": conv.id}
    assert all(e["type"] != "queued" for e in events)
    assert events[-1] == {"type": "done"}


async def test_default_still_supersedes_a_running_turn(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)

    response = await chat.ask_stream(chat.AskStreamRequest(
        question="second question", conversation_id=conversation_id,
    ))
    events = await _drain(response.body_iterator)
    assert all(e["type"] != "queued" for e in events)
    with pytest.raises(asyncio.CancelledError):
        await first_turn.task
    messages = store.get_messages(conversation_id)
    assert messages[1].routing == {"truncated": True, "truncation_reason": "cancelled"}


async def test_explicit_supersede_value_behaves_like_the_default(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    response = await chat.ask_stream(chat.AskStreamRequest(
        question="second question", conversation_id=conversation_id, if_busy="supersede",
    ))
    await _drain(response.body_iterator)
    with pytest.raises(asyncio.CancelledError):
        await first_turn.task


async def test_supersede_also_drops_queued_turns(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    queued = await chat.ask_stream(chat.AskStreamRequest(
        question="queued question", conversation_id=conversation_id, if_busy="queue",
    ))
    await _drive_until(queued.body_iterator, 1)

    response = await chat.ask_stream(chat.AskStreamRequest(
        question="replacement", conversation_id=conversation_id,
    ))
    await _drain(response.body_iterator)
    assert await _drain(queued.body_iterator) == []
    assert "queued question" not in scripted_loop.started
    contents = [m.content for m in store.get_messages(conversation_id)]
    assert "queued question" not in contents
    assert contents[-1] == "answer to replacement"


def test_unknown_if_busy_value_is_rejected():
    with pytest.raises(Exception):
        chat.AskStreamRequest(question="hi", if_busy="wait")


async def test_get_conversation_reports_queued_turns(api_client, store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    resp = await api_client("GET", f"/api/conversations/{conversation_id}")
    assert resp.json()["queued_turns"] == 0
    assert resp.json()["active_turn"] is not None

    queued = []
    for question in ("second question", "third question"):
        response = await chat.ask_stream(chat.AskStreamRequest(
            question=question, conversation_id=conversation_id, if_busy="queue",
        ))
        queued.append(response.body_iterator)
        await _drive_until(response.body_iterator, 1)

    resp = await api_client("GET", f"/api/conversations/{conversation_id}")
    body = resp.json()
    assert body["queued_turns"] == 2
    assert body["active_turn"]["turn_id"] == first_turn.turn_id
    # A queued turn's user message is persisted when it starts, not before.
    assert [m["content"] for m in body["messages"]] == ["first question"]

    gate.set()
    for gen in queued:
        await _drain(gen)
    resp = await api_client("GET", f"/api/conversations/{conversation_id}")
    assert resp.json()["queued_turns"] == 0
    assert resp.json()["active_turn"] is None


async def test_cancel_endpoint_cancels_running_turn_and_clears_queue(api_client, store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    queued_gens = []
    for question in ("second question", "third question"):
        response = await chat.ask_stream(chat.AskStreamRequest(
            question=question, conversation_id=conversation_id, if_busy="queue",
        ))
        queued_gens.append(response.body_iterator)
        await _drive_until(response.body_iterator, 1)

    resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
    assert resp.json() == {"ok": True, "cancelled": True}

    with pytest.raises(asyncio.CancelledError):
        await first_turn.task
    for gen in queued_gens:
        assert await _drain(gen) == []
    await asyncio.sleep(0.05)

    assert scripted_loop.started == ["first question"]
    messages = store.get_messages(conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert "cut off" in messages[1].content

    body = (await api_client("GET", f"/api/conversations/{conversation_id}")).json()
    assert body["queued_turns"] == 0
    assert body["active_turn"] is None
    assert chat_turns.get_turn_registry()._by_turn_id == {}


async def test_shutdown_drains_queued_turns(store, scripted_loop):
    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    response = await chat.ask_stream(chat.AskStreamRequest(
        question="second question", conversation_id=conversation_id, if_busy="queue",
    ))
    await _drive_until(response.body_iterator, 1)

    await chat_turns.get_turn_registry().shutdown()

    assert scripted_loop.started == ["first question"]
    assert chat_turns.get_turn_registry()._by_turn_id == {}
    assert chat_turns.get_turn_registry().queued_count(conversation_id) == 0


async def test_queued_turn_deadline_clock_starts_when_it_starts(store, scripted_loop, monkeypatch):
    """A detached queued turn is not timed out while it waits: its
    detached-lifetime clock starts when it starts running."""
    from config.settings import settings

    conversation_id, gate, first_turn = await _start_held_first_turn(scripted_loop)
    # Applies to turns detached from here on; the first turn keeps the default.
    monkeypatch.setattr(settings, "detached_turn_timeout_seconds", 0.3)
    scripted_loop.gates["second question"] = asyncio.Event()  # never released
    response = await chat.ask_stream(chat.AskStreamRequest(
        question="second question", conversation_id=conversation_id, if_busy="queue",
    ))
    gen = response.body_iterator
    await _drive_until(gen, 1)
    await gen.aclose()
    second_turn = next(t for t in chat_turns.get_turn_registry()._by_turn_id.values() if t.queued)

    await asyncio.sleep(0.5)  # detached longer than the timeout, still waiting
    assert not second_turn.task.done()
    assert scripted_loop.started == ["first question"]

    gate.set()
    await first_turn.task
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(second_turn.task, timeout=3)
    assert scripted_loop.started == ["first question", "second question"]
    assert second_turn.cancel_reason == "deadline"
