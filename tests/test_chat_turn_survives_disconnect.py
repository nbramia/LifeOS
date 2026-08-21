"""#611: a chat turn's lifetime is owned by the server, not the SSE
connection that happened to be watching it when it started.

Today (before #611), `generate()` in `api/routes/chat.py` has no `finally`
block and the assistant row is written once at the very end — so closing
the SSE reader mid-turn (a browser tab closing, an app backgrounding) kills
generation within milliseconds via the `CancelledError`/`GeneratorExit`
Starlette delivers at the suspended `yield`, and nothing is ever persisted,
not even the partial text. `test_native_turn_completes_and_persists_after_client_disconnect`
below drives that exact sequence and must fail until the turn registry
(`api/services/chat_turns.py`) decouples the turn's task from its reader.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from api.routes import chat
from api.services import chat_turns
from api.services.conversation_store import ConversationStore
from api.services.usage_store import UsageStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    return s


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    s = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(chat, "get_usage_store", lambda: s)
    return s


async def _drive_until(gen, min_events):
    """Pull SSE `data:` events off a StreamingResponse body_iterator until at
    least `min_events` have been parsed, then stop pulling (the caller
    closes the generator from here to simulate a client disconnect)."""
    events = []
    async for raw in gen:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
        if len(events) >= min_events:
            break
    return events


async def test_native_turn_completes_and_persists_after_client_disconnect(
    store, usage_store, monkeypatch,
):
    import api.services.agent_loop as agent_loop_mod

    resume = asyncio.Event()

    async def fake_loop(**kwargs):
        yield {"type": "text", "content": "Hello "}
        yield {"type": "text", "content": "world"}
        # A long tool-using turn is still running when the browser goes
        # away — resumed explicitly below, after the reader has been
        # closed, so the test controls exactly when that happens.
        await resume.wait()
        yield {"type": "text", "content": ", the full answer"}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=12,
            total_output_tokens=34,
            total_cost_usd=0.0012,
            model="claude-haiku-4-5",
            tool_calls_log=[],
            full_text="Hello world, the full answer",
        )}

    async def fake_classify(*a, **k):
        return None

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

    request = chat.AskStreamRequest(question="tell me something")
    response = await chat.ask_stream(request)
    gen = response.body_iterator

    events = await _drive_until(gen, 3)  # conversation_id, routing, first content chunk
    conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

    # The client disconnects here: close the reader without ever pulling the
    # rest of the stream — exactly what a closed tab does to a StreamingResponse.
    await gen.aclose()

    # Let the still-running turn past its artificial pause point.
    resume.set()

    turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
    assert turn is not None and turn.task is not None
    await turn.task  # the turn must keep running to completion server-side

    messages = store.get_messages(conversation_id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "tell me something"),
        ("assistant", "Hello world, the full answer"),
    ]
    # A turn that ran to completion carries no truncation marker.
    assert "cut off" not in messages[-1].content
    assert not (messages[-1].routing or {}).get("truncated")

    stats = usage_store.get_usage_stats()
    assert stats["request_count"] == 1
    assert stats["total_input_tokens"] == 12
    assert stats["total_output_tokens"] == 34


async def test_voice_modality_disconnect_detaches_and_survives_like_text(store, monkeypatch):
    """#616: this test used to be named
    `test_voice_modality_disconnect_cancels_rather_than_detaches` and
    asserted the OPPOSITE of what it asserts now — that a voice-modality
    turn's disconnect cancelled the task immediately rather than detaching
    it, because whisper-relay had no way to say "stop" other than
    abandoning the stream. Now that whisper-relay calls `POST
    /api/chat/cancel` with its `client_turn_id` on a real cancel gesture
    (whisper-relay#37), a disconnect alone — a hangup or network drop with
    no explicit cancel — no longer means "stop": a voice turn detaches and
    keeps running to completion server-side, exactly like the text turn in
    `test_native_turn_completes_and_persists_after_client_disconnect`
    above. This inversion is deliberate, not a weakening — see #616."""
    import api.services.agent_loop as agent_loop_mod

    resume = asyncio.Event()

    async def fake_loop(**kwargs):
        yield {"type": "text", "content": "Hello "}
        await resume.wait()
        yield {"type": "text", "content": "world"}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="m", tool_calls_log=[], full_text="Hello world",
        )}

    async def fake_classify(*a, **k):
        return None

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

    request = chat.AskStreamRequest(question="hi", modality="voice")
    response = await chat.ask_stream(request)
    gen = response.body_iterator

    events = await _drive_until(gen, 3)  # conversation_id, routing, "Hello "
    conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

    turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
    assert turn is not None and turn.modality == "voice"

    # The client disconnects (a hangup or network drop, NOT an explicit
    # cancel) — the turn must detach and keep running, same as a
    # text-modality turn.
    await gen.aclose()
    resume.set()
    await turn.task  # must run to completion, never cancelled

    messages = store.get_messages(conversation_id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Hello world"),
    ]
    # A turn that ran to completion carries no truncation marker.
    assert "cut off" not in messages[-1].content
    assert not (messages[-1].routing or {}).get("truncated")
