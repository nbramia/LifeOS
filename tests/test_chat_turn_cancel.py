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


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    monkeypatch.setattr(conversations, "get_store", lambda: s)
    return s


@pytest.fixture
def api_client():
    """A callable `(method, path, **kw) -> Response` — each call opens and
    closes its own short-lived httpx.AsyncClient (ASGITransport is
    stateless/cheap), so a test can make several requests without hitting
    httpx's "can't reopen a closed client" guard that a single shared
    `async with` would. Mounts both routers so `/api/conversations/*` and
    `/api/chat/cancel` are both reachable."""
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


class TestClientTurnIdCancel:
    """#611 review (whisper-relay maintainer): `conversation_id` alone can't
    cancel a turn before its first SSE frame ever arrives -- a brand-new
    conversation's id doesn't exist until the `conversation_id` event, and
    that's exactly the window a first-turn voice barge-in falls into. A
    client-generated `client_turn_id`, known before the request is even
    sent, closes that gap. `POST /api/chat/cancel` accepts it."""

    async def test_cancels_before_any_sse_frame_ever_arrives(self, store, monkeypatch):
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            await hang_forever.wait()  # would hang forever if not cancelled
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="hi", client_turn_id="barge-in-key-1")
        await chat.ask_stream(request)
        # Deliberately never touch response.body_iterator -- no SSE frame,
        # not even `conversation_id`, has been read. The only thing the
        # caller has is the key it minted before sending the request.

        turn = chat_turns.get_turn_registry().get_by_client_turn_id("barge-in-key-1")
        assert turn is not None
        assert turn.conversation_id is None  # not yet bound -- this is the whole point

        cancelled = chat_turns.get_turn_registry().cancel_by_client_turn_id("barge-in-key-1")
        assert cancelled is True

        with pytest.raises(asyncio.CancelledError):
            await turn.task

    async def test_cancel_endpoint_accepts_client_turn_id(self, api_client, store, monkeypatch):
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            await hang_forever.wait()
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="hi", client_turn_id="barge-in-key-2")
        await chat.ask_stream(request)

        resp = await api_client("POST", "/api/chat/cancel", json={"client_turn_id": "barge-in-key-2"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": True}

        turn = chat_turns.get_turn_registry().get_by_client_turn_id("barge-in-key-2")
        with pytest.raises(asyncio.CancelledError):
            await turn.task

        # No such conversation exists to 404 on -- an unknown/expired key is
        # just "nothing to cancel", 200, never a 4xx.
        resp2 = await api_client("POST", "/api/chat/cancel", json={"client_turn_id": "never-existed"})
        assert resp2.status_code == 200
        assert resp2.json() == {"ok": True, "cancelled": False}

    async def test_empty_client_turn_id_is_rejected_not_a_silent_noop(self, api_client, store):
        # FastAPI's standard pydantic-validation-failure status (422) --
        # loudly rejected, not silently treated as "nothing to cancel".
        resp = await api_client("POST", "/api/chat/cancel", json={"client_turn_id": ""})
        assert resp.status_code == 422

    async def test_oversized_client_turn_id_rejected_on_ask_stream(self, store):
        with pytest.raises(Exception):  # pydantic ValidationError
            chat.AskStreamRequest(question="hi", client_turn_id="x" * 201)

    async def test_control_character_client_turn_id_rejected_on_ask_stream(self, store):
        with pytest.raises(Exception):
            chat.AskStreamRequest(question="hi", client_turn_id="abc\x00def")

    async def test_a_reused_client_turn_id_supersedes_the_old_turn(self, store, monkeypatch):
        """Reusing the same client_turn_id on a second request (e.g. a retry)
        cancels the first turn rather than leaving two turns registered
        under the one key -- the same supersede semantics conversation_id
        already gets."""
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop_1(**kwargs):
            await hang_forever.wait()
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop_1)

        first = chat.AskStreamRequest(question="first", client_turn_id="dup-key")
        await chat.ask_stream(first)
        first_turn = chat_turns.get_turn_registry().get_by_client_turn_id("dup-key")
        assert first_turn is not None

        async def fake_loop_2(**kwargs):
            yield {"type": "text", "content": "second answer"}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="second answer",
            )}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop_2)

        second = chat.AskStreamRequest(question="second", client_turn_id="dup-key")
        second_response = await chat.ask_stream(second)
        async for _ in second_response.body_iterator:
            pass

        with pytest.raises(asyncio.CancelledError):
            await first_turn.task

        # "dup-key" now resolves to the SECOND turn, not the first -- the
        # scoping guarantee: a reused key always reaches its current
        # claimant, never a stray earlier one.
        current = chat_turns.get_turn_registry().get_by_client_turn_id("dup-key")
        assert current is None  # the second turn already finished and was popped

    async def test_client_turn_id_and_conversation_id_are_independently_scoped(self, store, monkeypatch):
        """Two different, unrelated turns -- one reachable only by
        conversation_id, one only by client_turn_id -- and cancelling one
        must never touch the other."""
        import api.services.agent_loop as agent_loop_mod

        hold_a = asyncio.Event()
        hold_b = asyncio.Event()

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        # Turn A: has a conversation_id (existing conversation), no client_turn_id.
        conv = store.create_conversation()
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_agent_loop_holding_at(hold_a, "A "))
        request_a = chat.AskStreamRequest(question="a", conversation_id=conv.id)
        response_a = await chat.ask_stream(request_a)
        await _drive_until(response_a.body_iterator, 2)  # routing, "A "

        # Turn B: only a client_turn_id, no conversation_id yet.
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", _fake_agent_loop_holding_at(hold_b, "B "))
        request_b = chat.AskStreamRequest(question="b", client_turn_id="only-b")
        await chat.ask_stream(request_b)
        # Don't touch response_b.body_iterator -- B intentionally has no
        # conversation_id bound yet.

        turn_a = chat_turns.get_turn_registry().get_by_conversation(conv.id)
        turn_b = chat_turns.get_turn_registry().get_by_client_turn_id("only-b")
        assert turn_a is not None and turn_b is not None
        assert turn_a is not turn_b

        # Let B's task actually take its first step before cancelling it --
        # a task cancelled before it's ever run never executes its
        # `finally` at all (a real asyncio behavior, not specific to this
        # code), which would make the "pop() cleared the mapping" assertion
        # below pass for the wrong reason. A real turn always has at least
        # one `await turn.emit(...)` before anything could try to cancel it.
        await asyncio.sleep(0)

        # Cancelling B by its client_turn_id must not affect A.
        assert chat_turns.get_turn_registry().cancel_by_client_turn_id("only-b") is True
        with pytest.raises(asyncio.CancelledError):
            await turn_b.task
        assert not turn_a.task.done()

        # And a lookup of A by conversation_id must never resolve through B's key.
        assert chat_turns.get_turn_registry().get_by_conversation(conv.id) is turn_a
        assert chat_turns.get_turn_registry().get_by_client_turn_id("only-b") is None

        # Clean up A explicitly (its reader was only partially drained,
        # never closed, so its emit() would otherwise block forever with
        # nobody consuming the queue) -- the test's assertions are already
        # done; this just avoids leaking a permanently-blocked task.
        chat_turns.get_turn_registry().cancel_conversation(conv.id)
        with pytest.raises(asyncio.CancelledError):
            await turn_a.task


class TestCancelOrderingsAroundAnAttachedReader:
    """#611 review (whisper-relay maintainer): the gateway fires its cancel
    POST from its cancel-event handler, not from inside its SSE read loop
    (checking a flag between lines would add latency) -- so the POST can
    genuinely arrive slightly BEFORE the client stops reading. All three
    orderings below must be safe."""

    async def test_cancel_while_reader_still_attached_works_and_reader_gets_clean_eof(
        self, store, monkeypatch,
    ):
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "partial "}
            await hang_forever.wait()
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "partial "
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

        # Cancel now, WITHOUT ever closing/disconnecting the reader -- it's
        # still attached and the caller keeps reading from it below.
        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        assert turn is not None
        cancelled = chat_turns.get_turn_registry().cancel_conversation(conversation_id)
        assert cancelled is True

        # The still-attached reader must drain to a clean EOF (StopAsyncIteration
        # via the sentinel) rather than hang or raise -- no more real frames
        # were emitted after cancellation, so the loop should end immediately.
        remaining = [item async for item in gen]
        assert remaining == []

        with pytest.raises(asyncio.CancelledError):
            await turn.task

        messages = store.get_messages(conversation_id)
        assert len(messages) == 2
        assert "partial " in messages[1].content
        assert "cut off" in messages[1].content

    async def test_disconnect_after_already_cancelled_is_a_clean_noop(self, store, monkeypatch):
        """The reverse ordering: cancel fires, the turn's own finally pops
        the registry and (while the reader was still attached) queues the
        sentinel -- and only THEN does the client actually disconnect. That
        disconnect must not double-finalize or raise."""
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "partial "}
            await hang_forever.wait()
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        chat_turns.get_turn_registry().cancel_conversation(conversation_id)
        with pytest.raises(asyncio.CancelledError):
            await turn.task  # let the task's own finally (pop + close) fully run first

        # NOW the client disconnects -- after the turn already finalized.
        # Must not raise, and must not write a second time.
        await gen.aclose()

        messages = store.get_messages(conversation_id)
        assert len(messages) == 2  # unchanged: one user + one assistant row, not two

        # A cancel of this now-fully-finished conversation is also a clean
        # no-op, not an error.
        resp = chat_turns.get_turn_registry().cancel_conversation(conversation_id)
        assert resp is False

    async def test_cancel_of_a_normally_finished_turn_is_200_not_4xx(self, api_client, store, monkeypatch):
        """Distinct from the already-cancelled case above: a turn that ran
        to normal completion (never cancelled at all), then cancelled after
        the fact -- e.g. the gateway's POST lands just after `done`. Must
        read as success (cancelled: false), never an error status."""
        import api.services.agent_loop as agent_loop_mod

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "the whole answer"}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="the whole answer",
            )}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="tell me something")
        response = await chat.ask_stream(request)
        events = []
        async for raw in response.body_iterator:
            text = raw.decode() if isinstance(raw, bytes) else raw
            for line in text.split("\n"):
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": False}

        messages = store.get_messages(conversation_id)
        assert len(messages) == 2
        assert messages[1].content == "the whole answer"
        assert messages[1].routing != {"truncated": True, "truncation_reason": "cancelled"}


class TestVoiceModalityCancelAfterGateLift:
    """#616: with the modality-keyed detachment gate lifted, a voice turn
    now goes through the exact same explicit-cancel machinery as a text
    turn -- these are the acceptance-criteria cases the issue calls out by
    name: an explicit cancel stops a voice turn on the same bound as web
    chat's Stop button, the partial reply is marked truncated on the same
    terms as any other interrupted turn, and a barge-in that lands before
    the turn's first SSE frame (no conversation_id yet) still halts
    generation via `client_turn_id` -- exactly the whisper-relay first-turn
    barge-in gap #611 review closed for text and #616 now extends to voice."""

    async def test_explicit_cancel_stops_a_voice_turn_same_as_web_chats_stop(
        self, api_client, store, monkeypatch,
    ):
        import api.services.agent_loop as agent_loop_mod

        hold = asyncio.Event()
        monkeypatch.setattr(
            agent_loop_mod, "run_agent_loop", _fake_agent_loop_holding_at(hold),
        )

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(question="hi there", modality="voice")
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "Hello "
        conversation_id = next(
            e["conversation_id"] for e in events if e.get("type") == "conversation_id"
        )
        await gen.aclose()  # detach -- must NOT itself cancel (that's #616's point)

        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        assert turn is not None and turn.modality == "voice"

        # The explicit cancel -- the same endpoint, same bound, same
        # response shape web chat's Stop button gets for a text turn.
        resp = await api_client("POST", f"/api/conversations/{conversation_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": True}

        with pytest.raises(asyncio.CancelledError):
            await turn.task

        # Marked truncated on the same terms as any other interrupted turn
        # -- not a voice-specific truncation reason.
        messages = store.get_messages(conversation_id)
        assert len(messages) == 2
        assert "Hello " in messages[1].content
        assert "cut off" in messages[1].content
        assert messages[1].routing == {"truncated": True, "truncation_reason": "cancelled"}

    async def test_client_turn_id_cancel_halts_a_voice_barge_in_before_first_sse_frame(
        self, store, monkeypatch,
    ):
        """The corrected acceptance criterion #616 adds: a barge-in landing
        BEFORE the turn's first SSE frame -- so before any conversation_id
        exists to cancel by -- must still halt generation. This is exactly
        `client_turn_id`'s reason for existing (#611 review), now proven for
        a voice-modality turn specifically rather than just a text one."""
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            await hang_forever.wait()  # would hang forever if not cancelled
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(
            question="hi", modality="voice", client_turn_id="voice-barge-in-1",
        )
        await chat.ask_stream(request)
        # Deliberately never touch response.body_iterator -- no SSE frame,
        # not even conversation_id, has been read. The gateway's only
        # handle is the client_turn_id it minted before sending.

        turn = chat_turns.get_turn_registry().get_by_client_turn_id("voice-barge-in-1")
        assert turn is not None and turn.modality == "voice"
        assert turn.conversation_id is None  # not yet bound -- the whole point

        cancelled = chat_turns.get_turn_registry().cancel_by_client_turn_id("voice-barge-in-1")
        assert cancelled is True

        with pytest.raises(asyncio.CancelledError):
            await turn.task

    async def test_voice_cancel_via_client_turn_id_while_reader_attached_then_disconnect_is_clean(
        self, api_client, store, monkeypatch,
    ):
        """Mirrors the real whisper-relay#37 sequence: its `cancel_turn()`
        fires `POST /api/chat/cancel` with `client_turn_id` from its
        cancel-event handler (not its SSE read loop), and only afterward
        abandons the stream -- so the POST can genuinely land while our
        reader is still attached, with the disconnect following. #611
        review already proved this ordering safe for a text turn
        (`TestCancelOrderingsAroundAnAttachedReader` above); this is the
        same proof for a voice turn specifically, since with the gate
        removed this exact sequence is what a real barge-in now produces on
        a detachable voice turn -- the ordering must not depend on modality,
        and it doesn't: `reader()`'s `finally` no-ops once `finalized` is
        already True, regardless of which modality got it there."""
        import api.services.agent_loop as agent_loop_mod

        hang_forever = asyncio.Event()

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "partial "}
            await hang_forever.wait()
            yield {"type": "text", "content": "unreachable"}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(
            question="hi", modality="voice", client_turn_id="voice-ordering-1",
        )
        response = await chat.ask_stream(request)
        gen = response.body_iterator
        events = await _drive_until(gen, 3)  # conversation_id, routing, "partial "
        conversation_id = next(e["conversation_id"] for e in events if e.get("type") == "conversation_id")

        turn = chat_turns.get_turn_registry().get_by_client_turn_id("voice-ordering-1")
        assert turn is not None and turn.modality == "voice"

        # The gateway's cancel-event handler fires the POST now -- the
        # reader is STILL attached, the stream hasn't been abandoned yet.
        resp = await api_client("POST", "/api/chat/cancel", json={"client_turn_id": "voice-ordering-1"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": True}

        # The still-attached reader must drain to a clean EOF -- no more
        # real frames were emitted after cancellation.
        remaining = [item async for item in gen]
        assert remaining == []

        with pytest.raises(asyncio.CancelledError):
            await turn.task  # let the task's own finally (pop + close) fully run

        # NOW the gateway abandons the stream -- after the turn already
        # finalized. Must not raise or double-finalize.
        await gen.aclose()

        messages = store.get_messages(conversation_id)
        assert len(messages) == 2  # one user + one assistant row, not two
        assert "partial " in messages[1].content
        assert "cut off" in messages[1].content
        assert messages[1].routing == {"truncated": True, "truncation_reason": "cancelled"}

    async def test_cancel_of_an_already_finished_voice_turn_is_200_not_4xx(
        self, api_client, store, monkeypatch,
    ):
        """whisper-relay's `cancel_turn()` treats a `cancelled: false`
        response as success, never as an error -- e.g. if its cancel POST
        (keyed by `client_turn_id`) lands just after the turn already ran
        to completion and was popped from the registry. Must read 200, not
        404/4xx, exactly like the existing text-turn proof of this
        (`TestCancelOrderingsAroundAnAttachedReader.test_cancel_of_a_normally_finished_turn_is_200_not_4xx`
        above) -- re-confirmed here via `client_turn_id` specifically,
        since that's the key whisper-relay actually sends."""
        import api.services.agent_loop as agent_loop_mod

        async def fake_loop(**kwargs):
            yield {"type": "text", "content": "the whole answer"}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="the whole answer",
            )}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        request = chat.AskStreamRequest(
            question="hi", modality="voice", client_turn_id="voice-finished-1",
        )
        response = await chat.ask_stream(request)
        async for _ in response.body_iterator:
            pass  # drain fully -- the fake loop finishes immediately

        # The turn already finished and was popped -- client_turn_id no
        # longer resolves to anything, same as an unknown/expired key.
        resp = await api_client("POST", "/api/chat/cancel", json={"client_turn_id": "voice-finished-1"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": False}
