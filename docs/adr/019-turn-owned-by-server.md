# ADR-019: A Turn's Lifetime Is Owned by the Server, Not the Connection

**Status:** Complete
**Last Updated:** 2026-08-21
**Decision:** Accepted
**Amended by:** [ADR-020](020-voice-cancel-gate-lifted.md)

## Context

Closing the chat page or app mid-response used to abandon the turn. The SSE
connection *was* the turn: `/api/ask/stream`'s `generate()` was a plain async
generator with no `finally` block, so the `GeneratorExit`/`CancelledError`
Starlette delivers at the suspended `yield` on client disconnect killed
generation within milliseconds. Nothing was persisted — not even the partial
text the user had already seen stream past. The same was true of the Hermes
proxy's relay loop before #592, and #592's fix (a read-only tee that
persists whatever arrived before the relay ended) only closed half the gap:
it stops a partial reply from being *lost*, but it can't make the reply
*complete*, because the relay itself still died with the connection.

This was the wrong contract for how the assistant is used. A tool-using turn
can run over a minute, and on mobile, interruption is routine rather than
exceptional — a phone locks, an app backgrounds, a network switches from
wifi to cellular. Each of those discarded work the user asked for, and on
the Anthropic (API-billed) backend, work already paid for.

Three constraints shaped the fix:

1. **Cancellation must remain possible.** Today, closing the page *is* the
   cancel gesture. Decoupling the turn from the connection without adding an
   explicit stop path would make a runaway tool loop unstoppable — a real
   problem on a paid backend.
2. **Voice is a special case.** whisper-relay's adapter deliberately
   abandons the LifeOS stream as its cancel gesture on barge-in and hangup
   (`LifeOSCancelled`). If a voice turn silently survived that abandonment,
   every interruption would run to completion and bill for a reply the
   operator explicitly cut off — on the surface used most from a phone.
3. **[ADR-018](018-api-spend-requires-consent.md)'s posture must hold.**
   ADR-018 established that LifeOS never spends API credits without the
   operator's consent. Keeping a turn alive after its client leaves is a new
   way to keep spending on an API-billed engine without a live human
   watching — the tradeoff isn't "no more spend without consent" (the
   consent already happened when the turn was started) but it does add a
   new *duration* the operator isn't directly present for, which needs a
   ceiling of its own (see Decision 4 below).

## Decision

**A chat turn's lifetime is owned by the server from the moment it starts.
The SSE stream is a live view of a turn in progress, not its lifeline.**

`api/services/chat_turns.py` introduces `ChatTurn` + `TurnRegistry`:

- The turn's own logic (`_run_turn()` in `api/routes/chat.py`, or a
  registry-owned pump in `api/routes/_proxy.py` for the Hermes backend) runs
  as an `asyncio.Task`, independent of any reader.
- `emit()` sends a frame into a small bounded queue; while a reader is
  attached this backpressures exactly as a bare `yield` did, so a connected
  client's byte sequence is unchanged.
- `reader()` is the generator handed to `StreamingResponse`. Its
  `GeneratorExit` — fired when the browser disconnects — detaches the queue
  (frames are dropped rather than queued from then on) but **never cancels
  the task**. The task keeps running to completion regardless.

Four supporting decisions close the constraints above:

1. **An explicit cancel path exists, keyed two ways.** `POST
   /api/conversations/{id}/cancel` stops whatever turn is in flight for that
   conversation, since every client already has that id from the first SSE
   event — no new header or event type was needed. A Stop button in the
   composer calls it while a turn is loading. A new turn on a conversation
   that already has one running cancels the old one first (supersede) —
   asking again is itself a stop gesture, and it prevents a stale reply
   landing after a newer question. Review (the whisper-relay maintainer)
   found a hole in that alone: a brand-new conversation's id doesn't exist
   until the `conversation_id` SSE event arrives, so a client racing to
   cancel *before* that — the common shape of a voice barge-in, which often
   lands within the first second — has nothing to cancel by. `POST
   /api/ask/stream` therefore also accepts an optional, client-generated
   `client_turn_id` (opaque, bounded, not assumed to be a UUID), indexed by
   the registry alongside `conversation_id` from the moment the turn is
   created; `POST /api/chat/cancel` cancels by that key alone. Reusing a
   `client_turn_id` supersedes, exactly like `conversation_id` does — a
   stale or duplicate key always resolves to whichever turn currently holds
   it, never a stray earlier one.
2. **Voice turns are gated out of detachment.** `ChatTurn.reader()` checks
   `modality`: a voice-modality turn's disconnect cancels the task
   immediately, reproducing today's behavior exactly, rather than arming the
   survive-and-finish path every other turn gets. This is deliberately a
   narrower fix than the ideal end state (correct barge-in **and**
   survive-hangup) — that requires whisper-relay to call the cancel endpoint
   explicitly instead of just walking away, which is a coordinated,
   cross-repo follow-up, not part of this change.
3. **Every interrupted turn is marked, never presented as whole.** Whatever
   text a turn produced before being cancelled, hitting its deadline, being
   caught in a shutdown drain, or dying to a genuine stream error is
   persisted with a visible `TRUNCATION_MARKER` and `routing.truncated`,
   rather than either being lost (the old behavior) or looking like a
   complete answer that just trails off (the risk #592 called out and this
   ADR fully closes).
4. **A detached turn still has a ceiling.** `LIFEOS_DETACHED_TURN_TIMEOUT_SECONDS`
   (default 300s, matching the proxy's own upstream read timeout) starts a
   clock at the moment of detachment — not at turn start, so a turn that's
   still being watched is never affected by it. If a detached turn hasn't
   finished by then, it's cancelled the same way an explicit `/cancel` would.
   `api/main.py`'s lifespan shutdown cancels every in-flight turn and awaits
   its own partial-persist handling, so a mid-turn auto-redeploy stores an
   honest partial instead of silently losing the turn outright.

**Scope**: the native path and the Hermes proxy share the same registry in
one change (splitting them would mean reviewing the same machinery twice).
The Agent backend is untouched — it has no persistence tee, so detaching it
would only spend money with nothing to show for it; the gate is literally
"does something observe this relay," which is false for Agent today.

**Not built**: stream resume/replay from a byte offset (the additive
`active_turn` field on `GET /api/conversations/{id}` plus polling is enough
for "is a turn still running" without replaying bytes); a durable turn table
surviving a process restart (the shutdown drain is a smaller, sufficient
answer); routing turns through the existing job queue
(`api/services/job_queue.py` is cross-process and thread-backed, with no
path back to a live SSE connection — solving a bigger problem than this
one); a global concurrency cap (the deadline, supersede, and the task's own
`finally` already bound how many turns can pile up on a single-operator
box); per-turn token ceilings (`max_tool_rounds` and `max_tokens` already
bound a native turn's cost).

## Rationale

- **The reader is not the turn.** Conflating "someone is watching" with "the
  work should happen" was the root bug. Separating them into a task (does
  the work) and a queue-backed reader (shows the work) is the smallest
  change that fixes it, and it composes: cancellation, deadlines, and
  shutdown all become "stop the task," not special cases of streaming logic.
- **Keying cancellation on conversation id (plus a client-generated key),
  not a new server-minted turn id, avoids surface-area growth.**
  `X-LifeOS-Turn-ID` already exists with a different meaning (the Gmail
  send-gate header, `api/routes/gmail.py`). Every client — native and
  Hermes alike — already receives a conversation id as the first SSE
  event, so reusing it needed no new header, no new event type, and no
  CORS `expose_headers` change. `client_turn_id` closes the one real gap
  (cancelling before that first event) as a request-body field instead — a
  server-minted id returned via a new SSE event or header was considered
  and rejected specifically because either would break the byte-identity
  guarantee a connected client already relies on; a body field costs no
  frame and the client knows its own key before the server could mint one
  anyway.
- **The voice gate is a deliberate, temporary asymmetry, not an oversight.**
  Fixing #611 without it would have been a silent regression on exactly the
  surface (mobile voice) the underlying bug report came from — it would
  have converted "the operator interrupted the assistant" into "the
  assistant kept talking (and billing) anyway." The gate is explicit,
  commented with why, and pinned by a test, so it's a decision on record
  rather than an implicit dependency on old behavior.
- **A visible truncation marker beats either extreme.** Before #592, an
  interrupted turn vanished. After #592 but before #611, it could look
  complete when it wasn't — arguably worse. Marking it is strictly better
  than either: the user (or a later reader of the thread) can tell what
  happened.

## Alternatives Considered

### Resume/replay the stream from an offset on reconnect

Let a reconnecting client ask for "everything since byte N" and replay it.

**Rejected because:** the acceptance criteria only require that the
*finished* reply be readable after reopening the conversation — a plain
`GET` already does that once the turn persists. Replay adds a second
consumption path (live tail vs. historical read) for a benefit (watching a
turn's live output resume mid-stream after a reconnect) nobody asked for.
`active_turn` + polling gives "is it still running" cheaply if that's ever
wanted later, without committing to replay semantics now.

### A durable turn table that survives a process restart

Persist turn state to SQLite so a turn could, in principle, resume after the
server itself restarts.

**Rejected because:** an in-memory `asyncio.Task` cannot survive a process
restart regardless of what's on disk — resuming would mean re-issuing the
underlying LLM/tool calls, not resuming a suspended coroutine, which is a
different and much larger feature (checkpointed agent state). The shutdown
drain (cancel + honest partial) is the correct-sized answer to "the process
is about to exit": lose the least, not pretend to lose nothing.

### Route turns through the existing job queue

`api/services/job_queue.py` already runs background work reliably across
process boundaries.

**Rejected because:** it's thread-backed and has no path to stream results
back to a *specific still-open* HTTP connection — it's built for
fire-and-forget work polled later, not for "keep serving this live SSE
response." Building that path would mean re-deriving most of what
`ChatTurn`/`TurnRegistry` already provide, at more complexity, to reuse
infrastructure that solves a different problem.

### A global concurrency cap on in-flight turns

Bound the number of detached turns directly, independent of the deadline.

**Rejected because:** this is a single-operator box. The deadline (300s
default) plus supersede (asking again cancels the old turn) plus the task's
own `finally` already bound the population in practice; a cap would be
solving for a multi-tenant failure mode that doesn't exist here yet. If that
changes, a cap is a small, separable addition — not a reason to hold up this
change.

## Consequences

### Positive

- A tool-using turn survives the interruptions mobile use makes routine
  (locking the phone, backgrounding the app, a network handoff), and the
  full answer is there when the conversation is reopened.
- A turn that's cut off — for any reason — is never mistaken for a complete
  one; `routing.truncated` also gives the UI a place to hang a "this was cut
  off" affordance later if it wants one.
- An unwatched, runaway turn has both a ceiling (the deadline) and an
  explicit off switch (`/cancel`), so decoupling the turn from the
  connection doesn't reintroduce "impossible to stop."
- Persistence is exactly-once by construction: it lives in one place inside
  the turn's own task, which runs regardless of how many times a client
  connects, disconnects, or reconnects — there is no code path that can
  double-write.

### Negative

- **A disconnected turn now keeps running, and keeps costing, for up to the
  deadline.** This is the deliberate tradeoff #611 exists to make (the
  problem was turns dying too early, not living too long), but it is a real
  change in worst-case spend per abandoned turn on the Anthropic backend,
  bounded by `max_tool_rounds`/`max_tokens` and now also by the 300s
  deadline.
- **Voice barge-in is only correctly free of this tradeoff via the gate,
  not via a general solution.** Until whisper-relay is updated to call
  `/cancel` explicitly, voice's "abandon the stream to cancel" gesture stays
  a special case in `ChatTurn.reader()` rather than the one true cancel
  path every modality shares.
- **The Hermes persister's truncation reason is coarser than the native
  path's.** `_HermesTurnPersister` only knows whether a `done` event was
  observed, not *why* a turn was cut short, so every Hermes truncation
  currently reports `truncation_reason: "stream_error"` — the native path's
  finer `cancelled`/`deadline`/`shutdown`/`stream_error` distinction doesn't
  cross the process boundary.
- **A cancelled native turn's usage isn't recorded.** `AgentResult`'s token
  counts are only available at the end of a completed run; a cancelled turn
  has none to record. This is a known, deliberately deferred gap (filed
  separately), not an oversight.
- **A task cancelled before its first scheduled step never runs its
  `finally` at all — a real `asyncio` behavior, not something introduced
  here.** If `TurnRegistry.shutdown()` (or an explicit cancel) ever ran in
  the exact same event-loop tick as a turn's own `create_task()`, before
  that task had taken even one step, cancelling it would skip
  `_run_turn()`'s `finally` entirely — no partial persisted, no registry
  cleanup for that turn. In practice this can't happen: by the time
  anything could reach for that turn, it has already taken at least one
  step (creating the conversation row, emitting the `conversation_id`
  frame), so the window is theoretical, not reachable through any real
  request/shutdown ordering. Documented here rather than engineered
  around, since `shutdown()` — cancelling every registered turn in one
  pass — is exactly the kind of code a future change might run earlier in
  a turn's life than today's callers do.

## Related Documents

### Design Context

- [ADR-018](018-api-spend-requires-consent.md) — the consent posture this
  change adds a new *duration* to, bounded by the detached-turn deadline
- [Client surfaces](../specs/technical/client-surfaces.md) — the SSE
  contract this ADR keeps byte-identical for a connected client

### Specifications

- [Client surfaces](../specs/technical/client-surfaces.md) — `/cancel`,
  `active_turn`, and the truncation marker as part of the HTTP contract

### Operational

- [Configuration](../guides/configuration.md) —
  `LIFEOS_DETACHED_TURN_TIMEOUT_SECONDS`

### Code References

- `api/services/chat_turns.py` — `ChatTurn`, `TurnRegistry`,
  `TRUNCATION_MARKER`, `truncation_routing()`
- `api/routes/chat.py` — `ask_stream()`'s turn creation/supersede,
  `_run_turn()`'s emit/cancel/finally handling
- `api/routes/_proxy.py` — `make_backend_router()`'s registry-owned pump,
  gated on `make_observer`
- `api/routes/hermes_proxy.py` — `_HermesTurnPersister.bind_turn()`, the
  `done`-observed truncation signal
- `api/routes/conversations.py` — `cancel_conversation_turn()`,
  `active_turn`
- `api/main.py` — the lifespan shutdown drain
