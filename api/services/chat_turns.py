"""Turn lifecycle registry (#611).

A chat turn's lifetime is owned by the server, not the SSE connection that
happened to be watching it when it started. `ChatTurn` decouples the two:
the turn's own task (`_run_turn` in `api/routes/chat.py`, or the Hermes pump
in `api/routes/hermes_proxy.py`) emits frames into a bounded queue and runs
to completion regardless of whether anything is reading it; `reader()` is
the generator handed to `StreamingResponse`, and closing it — what happens
when a browser disconnects — detaches without touching the task.

`TurnRegistry` is the shared, process-global lookup (`turn_id -> ChatTurn`
and `conversation_id -> turn_id`) that makes a turn cancellable by
conversation id from `POST /api/conversations/{id}/cancel`, bounds a
detached turn's lifetime, and lets `api/main.py`'s shutdown drain every
turn still running before the process exits.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Appended to whatever text a turn managed to produce before it was
# interrupted (cancelled, hit its detached-lifetime deadline, was cancelled
# by a shutdown drain, or died on a genuine stream error) — visible, no
# client change needed, so a truncated reply is never mistaken for a
# finished one (the #611 problem statement: "a partial answer... looks like
# a complete answer that trails away").
TRUNCATION_MARKER = "\n\n_[cut off — the turn ended before it finished]_"


def truncation_routing(reason: str) -> dict:
    """The `routing` fragment recorded alongside a `TRUNCATION_MARKER`-suffixed
    message. `reason` is one of "cancelled" | "deadline" | "shutdown" |
    "stream_error"."""
    return {"truncated": True, "truncation_reason": reason}


# Sentinel pushed into a turn's queue to make `reader()`'s `while True` loop
# exit normally once the turn has finished (successfully, cancelled, or
# errored) — distinct from any real frame, including an empty one.
_SENTINEL = object()


class ChatTurn:
    """One turn's server-side lifetime.

    `turn_id`/`conversation_id` identify it; `conversation_id` may be `None`
    at construction (a brand-new conversation doesn't have an id yet) and is
    filled in later via `TurnRegistry.bind()` once the id is known — either
    immediately (an existing conversation) or mid-run (the native path
    creates the id itself; the Hermes pump learns it from the first
    `conversation_id` SSE frame it observes).
    """

    def __init__(
        self,
        turn_id: str,
        conversation_id: Optional[str],
        modality: str = "text",
        client_turn_id: Optional[str] = None,
    ):
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        # Opaque, client-generated key (#611 review) — known to the client
        # BEFORE it ever sends the request, unlike `conversation_id` (which
        # doesn't exist yet for a brand-new conversation) or `turn_id`
        # (server-generated, never sent to the client). This is what makes a
        # turn cancellable before its first SSE frame arrives — the gap that
        # matters most for voice barge-in, where the interruption is
        # frequently within the first second of a reply.
        self.client_turn_id = client_turn_id
        # Recorded for parity with the request (voice turns get spoken-style
        # system-prompt rules elsewhere, e.g. `api/routes/chat.py`'s
        # `build_system_prompt()` call) and for observability. #616 lifted
        # the modality-keyed detachment gate `reader()` used to check here —
        # a voice turn's disconnect now detaches exactly like a text turn's,
        # so this field no longer changes cancellation behavior on its own.
        self.modality = modality
        self.started_at = time.time()
        self.detached_at: Optional[float] = None
        self.reader_attached = True
        self.finalized = False
        # Set by whoever initiates a cancellation (the /cancel endpoint, a
        # supersede, the deadline watcher, or registry.shutdown()) BEFORE
        # calling task.cancel(), so the task's `except asyncio.CancelledError`
        # handler can record why. Falls back to "cancelled" if unset.
        self.cancel_reason: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self._deadline_task: Optional[asyncio.Task] = None
        # maxsize=1: while a reader is attached, `emit()` blocks until that
        # frame has been consumed — the same backpressure a bare `yield`
        # gives a StreamingResponse today. This is what keeps a connected
        # turn's frame sequence byte-identical to before #611; it is not
        # needed once detached (emit() short-circuits instead of queuing,
        # see below).
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=1)

    async def emit(self, frame: Any) -> None:
        """Send one frame (an SSE `data: ...` string on the native path, a
        raw upstream byte chunk on the Hermes pump) to the reader.

        While attached this awaits exactly the backpressure a bare `yield`
        gave the old generator. Once detached, frames are dropped rather
        than queued: nobody will ever call `reader()` again for this turn,
        so queuing would either block the producer forever (bounded queue,
        nothing draining it) or grow without bound (unbounded one) for a
        turn that could otherwise run for the full detached-lifetime
        deadline.
        """
        if not self.reader_attached:
            return
        await self._queue.put(frame)

    async def reader(self):
        """The generator handed to `StreamingResponse`. Its `GeneratorExit` —
        raised here, at the suspended `await self._queue.get()`, when
        Starlette closes this generator on client disconnect — detaches the
        turn. It must NEVER cancel `self.task`: the task keeps running to
        completion server-side regardless, which is the entire point of
        #611. `finally` runs on every exit path (normal drain via the
        sentinel, or GeneratorExit), so `not self.finalized` is what tells
        the two apart: by the time the sentinel is queued the task has
        already finalized, so a normal drain never re-arms anything here.
        """
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            self.reader_attached = False
            if not self.finalized:
                self.detached_at = time.time()
                # #616 lifted the modality-keyed gate that used to live here:
                # a voice-modality turn's disconnect immediately cancelled
                # the task instead of arming the deadline below, because
                # whisper-relay's adapter (src/voice_gateway/adapters/lifeos.py
                # in the whisper-relay repo) used to only abandon this stream
                # on barge-in/hangup, with no explicit way to say "stop". Now
                # that it calls `POST /api/chat/cancel` with its
                # `client_turn_id` instead, every modality detaches and
                # survives its client identically — see ADR-019 (as amended
                # by ADR-020) for the full history.
                get_turn_registry().arm_deadline(self)

    def request_cancel(self, reason: str) -> bool:
        """Cancel this turn's task, recording why. Returns False (no-op) if
        there's no task or it's already finished."""
        if self.task is None or self.task.done():
            return False
        self.cancel_reason = reason
        self.task.cancel()
        return True

    async def close(self) -> None:
        """Wake up `reader()` if it's still attached and waiting, and mark
        this turn done taking frames. Called from the turn task's own
        `finally` — always, on every exit path — never from `reader()`.

        If nothing is attached, there is nothing to wake: `reader()` has
        already returned (that's what "detached" means), so nobody will
        ever call `self._queue.get()` again. Blocking on `put()` in that
        case — e.g. a detached turn whose last `emit()` left one real frame
        sitting in the (maxsize=1) queue, un-drained — would wait forever
        for room that can never open up.
        """
        self.finalized = True
        if not self.reader_attached:
            return
        try:
            self._queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            # A reader is still attached but hasn't drained the last real
            # frame yet — wait for it to, so it's guaranteed to see the
            # sentinel and exit its loop normally rather than hanging until
            # GC. `reader()` runs as a separate task (the ASGI server
            # driving the StreamingResponse), so it can still disconnect
            # while this wait is in flight — bounded rather than unbounded,
            # so that narrow race can't leak this task forever.
            try:
                await asyncio.wait_for(self._queue.put(_SENTINEL), timeout=5.0)
            except asyncio.TimeoutError:
                pass


class TurnRegistry:
    """Process-global (but instance-per-test-via-`reset_turn_registry`)
    lookup of in-flight turns, keyed by their own id, the conversation they
    belong to, and (#611 review) an opaque client-supplied key."""

    def __init__(self):
        self._by_turn_id: dict[str, ChatTurn] = {}
        self._by_conversation_id: dict[str, str] = {}
        self._by_client_turn_id: dict[str, str] = {}

    def create(
        self,
        conversation_id: Optional[str],
        modality: str = "text",
        client_turn_id: Optional[str] = None,
    ) -> ChatTurn:
        turn = ChatTurn(str(uuid.uuid4()), conversation_id, modality, client_turn_id)
        self._by_turn_id[turn.turn_id] = turn
        if conversation_id:
            self._by_conversation_id[conversation_id] = turn.turn_id
        if client_turn_id:
            # Known at creation time (the client generated it before
            # sending), unlike conversation_id for a brand-new conversation
            # — no separate bind() step needed. A collision with an
            # in-flight turn's client_turn_id is handled by the caller via
            # supersede (cancel_by_client_turn_id) before create() runs, the
            # same pattern conversation_id supersede already uses; this
            # dict simply always reflects the CURRENT holder of a given key,
            # so a stale/replayed key can only ever reach whichever turn
            # most recently claimed it, never one further back.
            self._by_client_turn_id[client_turn_id] = turn.turn_id
        return turn

    def bind(self, turn: ChatTurn, conversation_id: str) -> None:
        """(Re)point the conversation_id -> turn_id mapping once a turn's
        conversation id becomes known after creation — the native path binds
        it as soon as a brand-new conversation is created; the Hermes pump
        binds it when the first `conversation_id` SSE frame is observed. In
        both cases there's a sub-second window before the bind where the
        turn exists but isn't yet reachable by conversation id (so it can't
        be cancelled or superseded) — acceptable per the #611 design.
        `client_turn_id`, if the client sent one, closes exactly this gap:
        it's known and registered from the moment `create()` runs, before
        this bind ever happens."""
        turn.conversation_id = conversation_id
        self._by_conversation_id[conversation_id] = turn.turn_id

    def get_by_conversation(self, conversation_id: str) -> Optional[ChatTurn]:
        turn_id = self._by_conversation_id.get(conversation_id)
        return self._by_turn_id.get(turn_id) if turn_id else None

    def get_by_client_turn_id(self, client_turn_id: str) -> Optional[ChatTurn]:
        turn_id = self._by_client_turn_id.get(client_turn_id)
        return self._by_turn_id.get(turn_id) if turn_id else None

    def pop(self, turn: ChatTurn) -> None:
        """Remove a finished turn. Called from the turn task's own
        `finally`. Only clears the conversation_id/client_turn_id mappings
        if they still point at THIS turn — a supersede may already have
        replaced either with a newer turn's id, and this must not clobber
        that (the same guard, applied to both keys)."""
        self._by_turn_id.pop(turn.turn_id, None)
        if turn.conversation_id and self._by_conversation_id.get(turn.conversation_id) == turn.turn_id:
            self._by_conversation_id.pop(turn.conversation_id, None)
        if turn.client_turn_id and self._by_client_turn_id.get(turn.client_turn_id) == turn.turn_id:
            self._by_client_turn_id.pop(turn.client_turn_id, None)
        deadline_task = turn._deadline_task
        if deadline_task is not None and not deadline_task.done():
            deadline_task.cancel()

    def cancel_conversation(self, conversation_id: str, reason: str = "cancelled") -> bool:
        """Cancel whatever turn is in flight for this conversation, if any.
        Used both by the explicit `POST /{id}/cancel` endpoint and by
        supersede (a new turn on a conversation that already has one running
        cancels it first — asking again IS a stop gesture)."""
        turn = self.get_by_conversation(conversation_id)
        if turn is None:
            return False
        return turn.request_cancel(reason)

    def cancel_by_client_turn_id(self, client_turn_id: str, reason: str = "cancelled") -> bool:
        """Cancel whatever turn is in flight under this client-supplied key
        (#611 review). This is the key a client has BEFORE it ever gets a
        `conversation_id` back — closing the "first-turn barge-in" gap
        `cancel_conversation` alone can't: a request that hasn't reached its
        first SSE frame yet has no conversation id to cancel by."""
        turn = self.get_by_client_turn_id(client_turn_id)
        if turn is None:
            return False
        return turn.request_cancel(reason)

    def arm_deadline(self, turn: ChatTurn) -> None:
        """Start the clock on a just-detached turn's bounded lifetime. Reads
        the timeout fresh from settings (not captured at import time) so
        tests can monkeypatch it per-case, matching the pattern
        `api/routes/_proxy.py` already uses for its own settings fields."""
        from config.settings import settings

        timeout = settings.detached_turn_timeout_seconds

        async def _watch():
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                return
            if not turn.finalized:
                turn.request_cancel("deadline")

        turn._deadline_task = asyncio.create_task(_watch())

    async def shutdown(self) -> None:
        """Cancel every in-flight turn and await its finalization. Called
        from `api/main.py`'s lifespan shutdown so a mid-turn auto-redeploy
        (#437) stores an honest partial instead of silently losing the
        turn."""
        turns = list(self._by_turn_id.values())
        tasks = []
        for turn in turns:
            if turn.task is not None and not turn.task.done():
                turn.cancel_reason = "shutdown"
                turn.task.cancel()
                tasks.append(turn.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_registry: Optional[TurnRegistry] = None


def get_turn_registry() -> TurnRegistry:
    global _registry
    if _registry is None:
        _registry = TurnRegistry()
    return _registry


def reset_turn_registry() -> None:
    """Test-only: replace the global registry with a fresh, empty one — the
    same pattern `conversation_store.reset_conversation_store()` uses —
    so turns from one test can never leak into the next."""
    global _registry
    _registry = TurnRegistry()
