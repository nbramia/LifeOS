# ADR-020: The Voice Detachment Gate Is Lifted

**Status:** Complete
**Last Updated:** 2026-08-21
**Decision:** Accepted

## Context

[ADR-019](019-turn-owned-by-server.md) decoupled a chat turn's lifetime from
its SSE connection, with one deliberate exception: a turn with `modality:
"voice"` was gated out of detachment in `ChatTurn.reader()`. whisper-relay's
adapter (`src/voice_gateway/adapters/lifeos.py` in the whisper-relay repo)
had no way to say "stop" other than abandoning the LifeOS/Hermes stream on
barge-in and hangup, and that abandonment was also the only signal a
disconnect could carry. If voice turns detached like every other turn, that
single gesture would have become ambiguous — every barge-in would silently
run the interrupted turn to completion and, on an API-billed backend, spend
money the operator explicitly declined by interrupting. ADR-019 recorded
this as a narrower fix than the ideal end state, pending a cross-repo
follow-up: whisper-relay calling an explicit cancel endpoint instead of
relying on disconnect-as-cancel.

That follow-up is `whisper-relay#37`, tracked alongside this repo's #616.
It changes the gateway's cancel gesture to call `POST /api/chat/cancel`
with its own `client_turn_id` — the key #611's review already added to
`TurnRegistry` specifically to close the "barge-in before the first SSE
frame" gap, since a brand-new conversation has no `conversation_id` yet for
the gateway to cancel by otherwise. Once the gateway states its cancel
intent explicitly, the reason for treating voice differently from every
other modality no longer holds: a disconnect with no accompanying cancel is
just a hangup or a network switch, not a stop gesture, on any modality.

## Decision

**The modality-keyed detachment gate in `ChatTurn.reader()` is removed.** A
voice-modality turn's disconnect now detaches and survives exactly like a
text turn's — the turn keeps running server-side and persists its full
reply, readable once the conversation is reopened. The gateway's barge-in
and hangup gestures are no longer inferred from a dropped connection; they
are the same explicit `POST /api/chat/cancel` (keyed by `client_turn_id` or
`conversation_id`) every other client already uses, including for a
barge-in landing before the turn's first SSE frame.

`api/routes/_proxy.py`'s Hermes pump shares the same `ChatTurn` class as the
native path, so this single change lifts the gate for both backends at
once — there was never a second, independent gate to remove; the pump's
`_sniff_modality()` only ever fed the one gate this ADR removes, and now
just records the turn's modality for parity with the native path's request.

Nothing else about #611 changes: the explicit cancel endpoints, the
detached-turn deadline, the shutdown drain, and the truncation marker on an
interrupted turn all behave identically regardless of modality now — voice
was the only modality that ever behaved differently, and it no longer does.

## Rationale

- **The premise for the exception was cross-repo, not architectural.**
  ADR-019's gate existed only because whisper-relay had no explicit cancel
  path yet. Once it has one, keeping voice as a special case in
  `ChatTurn.reader()` would be carrying old behavior forward for no reason
  tied to voice itself — the gate was always meant to be temporary (ADR-019
  says so explicitly).
- **Correct barge-in and surviving a hangup are no longer in tension.**
  Before this change, voice had to choose one or the other: detaching would
  break barge-in (nothing would stop a turn the operator interrupted);
  keeping the gate meant a hangup mid-answer still lost the reply outright,
  exactly the problem #611 fixed everywhere else. An explicit cancel signal
  makes both true at once — a real stop request halts generation, and mere
  abandonment (a locked phone, a dropped network) no longer discards work
  in flight.
- **One registry, one gate, one place to remove it.** Because the native
  and Hermes paths share `ChatTurn`/`TurnRegistry`, this is a single,
  surgical change rather than two backends' worth of special-casing to
  unwind.

## Alternatives Considered

### Keep the gate and add a separate "soft cancel" signal for voice

Have the gateway send some lighter-weight "I'm about to disconnect, that
means cancel" signal instead of reusing the general-purpose cancel
endpoints.

**Rejected because:** `POST /api/chat/cancel` and `client_turn_id` already
exist, already close the pre-first-frame gap that motivated them, and
already have the exact semantics wanted here (200 + `cancelled: true|false`,
never a 4xx for an unknown/expired key). Adding a second, voice-specific
cancel mechanism would duplicate that surface for no behavioral gain.

### Lift the gate only on the native path, leave Hermes-relayed voice turns gated

Since the issue that surfaced this asked specifically about `ChatTurn.reader()`.

**Rejected because:** the native and Hermes paths share the exact same
`ChatTurn` instance and gate — there is no separate Hermes-specific gate to
selectively keep. A native-only lift is not actually achievable without
re-introducing a modality check somewhere Hermes turns pass through, which
would just recreate the removed asymmetry in a different place.

## Consequences

### Positive

- Voice now gets the full benefit of #611 (surviving a hangup or network
  switch mid-answer) without giving up correct barge-in — the tradeoff
  ADR-019 flagged as temporary is resolved, not just relocated.
- No client-visible contract change for a gateway already sending
  `client_turn_id` and calling the explicit cancel endpoint on its cancel
  gesture — the SSE byte stream is unaffected either way, exactly as
  ADR-019 already guaranteed for a connected client.

### Negative

- **A whisper-relay release that has not yet adopted the explicit cancel
  call regresses barge-in silently.** Until such a release calls `POST
  /api/chat/cancel` on its cancel gesture, abandoning the stream is no
  longer a stop signal on the LifeOS side — the turn detaches and keeps
  running (and, on an API-billed backend, keeps costing) to completion or
  the detached-turn deadline. This is exactly the risk ADR-019's gate
  existed to avoid, which is why lifting the gate here and shipping the
  corresponding whisper-relay release are sequenced together rather than
  independently.
- **The Hermes truncation reason stays coarse.** ADR-019 already notes
  that a Hermes-relayed turn can only report `truncation_reason:
  "stream_error"`, never the native path's finer distinction. This is
  unchanged by lifting the gate — an explicitly cancelled voice turn
  relayed through Hermes is marked truncated the same way any other
  cancelled Hermes turn is.

## Related Documents

### Design Context

- [ADR-019: A Turn's Lifetime Is Owned by the Server, Not the Connection](019-turn-owned-by-server.md) — the original decision, including the voice gate this ADR removes
- [ADR-018: API Spend Requires Consent](018-api-spend-requires-consent.md) — the consent posture the original gate protected, now upheld by an explicit cancel signal instead of an inferred one

### Specifications

- [Client surfaces](../specs/technical/client-surfaces.md) — "Turn lifetime and cancellation" documents the lifted state and the explicit cancel endpoints

### Code References

- `api/services/chat_turns.py` — `ChatTurn.reader()`, where the gate lived
- `api/routes/_proxy.py` — `_sniff_modality()`, which fed the gate for the Hermes pump and now only records modality for parity
- `api/routes/chat.py` — `ask_stream()`'s turn creation, unaffected in shape, changed only in effect
