"""Unit tests for api/services/chat_turns.py (#611) — the turn registry
primitives directly, below the HTTP layer already covered by
tests/test_chat_turn_survives_disconnect.py and tests/test_chat_turn_cancel.py.

Covers the detached-lifetime deadline and the shutdown drain: both act on a
turn's own `task`, so these build a `ChatTurn` and give it a real asyncio
task directly rather than going through `ask_stream()`.
"""
import asyncio

import pytest

from api.services import chat_turns
from config.settings import settings

pytestmark = pytest.mark.unit


def _spawn_turn(registry, conversation_id="conv-1", modality="text", body=None):
    """A ChatTurn with a real task running `body` (an awaitable), wired into
    the registry the same way ask_stream() wires a real one."""
    turn = registry.create(conversation_id=conversation_id, modality=modality)

    async def _run():
        try:
            await (body or asyncio.Event().wait())
        finally:
            registry.pop(turn)
            await turn.close()

    turn.task = asyncio.create_task(_run())
    return turn


class TestArmDeadline:
    async def test_detached_turn_is_cancelled_after_the_configured_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "detached_turn_timeout_seconds", 0.05)
        registry = chat_turns.get_turn_registry()
        hang_forever = asyncio.Event()
        turn = _spawn_turn(registry, body=hang_forever.wait())

        registry.arm_deadline(turn)

        with pytest.raises(asyncio.CancelledError):
            await turn.task
        assert turn.cancel_reason == "deadline"
        assert registry.get_by_conversation("conv-1") is None

    async def test_a_turn_that_finishes_before_the_deadline_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(settings, "detached_turn_timeout_seconds", 60.0)
        registry = chat_turns.get_turn_registry()
        done = asyncio.Event()
        turn = _spawn_turn(registry, body=done.wait())
        registry.arm_deadline(turn)

        done.set()
        await turn.task  # completes normally, well inside the 60s deadline
        assert turn.cancel_reason is None
        assert registry.get_by_conversation("conv-1") is None
        # The deadline watcher itself must not still be pending forever —
        # pop() cancels it once the turn finalizes.
        assert turn._deadline_task.cancelled() or turn._deadline_task.done()


class TestShutdown:
    async def test_cancels_every_in_flight_turn_and_awaits_it(self):
        registry = chat_turns.get_turn_registry()
        never = asyncio.Event()
        turn_a = _spawn_turn(registry, conversation_id="conv-a", body=never.wait())
        turn_b = _spawn_turn(registry, conversation_id="conv-b", body=never.wait())
        # Let both tasks actually start (reach their `await`) before
        # cancelling them -- a task cancelled before its first scheduled
        # step ever runs never executes its `finally` at all, which would
        # make this test pass for the wrong reason (or, with a real
        # ask_stream() turn, never happens: there's always at least one
        # `await turn.emit(...)` before anything could try to cancel it).
        await asyncio.sleep(0)

        await registry.shutdown()

        assert turn_a.task.cancelled()
        assert turn_b.task.cancelled()
        assert turn_a.cancel_reason == "shutdown"
        assert turn_b.cancel_reason == "shutdown"
        assert registry.get_by_conversation("conv-a") is None
        assert registry.get_by_conversation("conv-b") is None

    async def test_shutdown_with_no_turns_in_flight_is_a_no_op(self):
        registry = chat_turns.get_turn_registry()
        await registry.shutdown()  # must not raise


class TestBindAndPop:
    def test_bind_points_a_new_conversation_id_at_the_turn(self):
        registry = chat_turns.get_turn_registry()
        turn = registry.create(conversation_id=None)
        assert registry.get_by_conversation("brand-new") is None

        registry.bind(turn, "brand-new")

        assert turn.conversation_id == "brand-new"
        assert registry.get_by_conversation("brand-new") is turn

    def test_pop_does_not_clobber_a_superseding_turns_mapping(self):
        registry = chat_turns.get_turn_registry()
        old_turn = registry.create(conversation_id="conv-1")
        new_turn = registry.create(conversation_id="conv-1")  # supersede

        # The old turn finishing after being superseded must not remove the
        # NEW turn's mapping for the same conversation id.
        registry.pop(old_turn)

        assert registry.get_by_conversation("conv-1") is new_turn


class TestChatTurnEmit:
    async def test_emit_drops_frames_once_detached_instead_of_blocking(self):
        turn = chat_turns.ChatTurn("t1", "conv-1", "text")
        turn.reader_attached = False
        # A detached turn's queue (maxsize=1) already has room, but emit()
        # must not even try to use it -- this must return immediately
        # without ever touching the queue that nobody will drain again.
        await asyncio.wait_for(turn.emit("frame-1"), timeout=1.0)
        assert turn._queue.qsize() == 0

    async def test_emit_backpressures_while_attached(self):
        turn = chat_turns.ChatTurn("t1", "conv-1", "text")
        await turn.emit("frame-1")  # fills the maxsize=1 queue
        second_emit = asyncio.ensure_future(turn.emit("frame-2"))
        await asyncio.sleep(0.01)
        assert not second_emit.done()  # blocked -- nobody has consumed frame-1 yet

        got = await turn._queue.get()
        assert got == "frame-1"
        await second_emit  # now unblocks
        second_emit.result()
