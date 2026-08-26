# ADR-021: Tee Voice Turns Into the Conversation Store at the Proxy Seam

**Status:** Complete
**Last Updated:** 2026-08-25
**Decision:** Accepted

## Context

A Hermes-backend voice conversation in `/chat` disappeared on page refresh —
it never reached `conversations.db` (#711). Text turns on Hermes persist
(`POST /api/hermes/ask/stream`'s `_HermesTurnPersister`, `api/routes/
hermes_proxy.py`), and voice turns on the `lifeos` backend persist (the
native orchestrator, reached when whisper-relay's `lifeos` adapter calls
LifeOS's own `/api/ask/stream`). Voice turns on the `hermes` backend fell
through both: [ADR-016](016-voice-gateway-reverse-proxy.md) made LifeOS's
`/api/voice/*` route a pure pass-through with no voice logic at all, and
whisper-relay's Hermes adapter (`voice_gateway/adapters/hermes_backend.py`
in the whisper-relay checkout) calls the Hermes harness's own
`/api/ask/stream` directly — never LifeOS's `/api/hermes/ask/stream` proxy.
Confirmed by reading that adapter, not assumed: `docs/specs/technical/
client-surfaces.md`'s "Topology" paragraph documents the gateway routing a
Hermes voice turn through LifeOS's Hermes proxy as the *target* cross-repo
contract (`nbramia/whisper-relay#32`), not what the deployed gateway
actually does. So no LifeOS persister was ever in the loop for a Hermes
voice turn — not a persistence bug in an existing tee, a genuine gap.

whisper-relay is out of scope for this fix (a separate repo, and the
cross-repo contract change it would need is tracked by whisper-relay#32
independently). The fix has to live entirely on the LifeOS side, and has to
work regardless of whether or when whisper-relay adopts #32.

## Decision

`api/routes/voice.py`'s reverse proxy grows exactly one exception to
ADR-016's "no voice logic" rule: `POST turn/stream` is additionally tee'd
into the same `ConversationStore` the Hermes text route uses
(`_VoiceTurnPersister`), triggered by the turn contract's one authoritative
event (`done`). Every other path the proxy handles — cancel, audio-clip
serving, anything the gateway adds later — is unchanged: still the
unbuffered, no-logic pass-through ADR-016 describes.

The tee only ever writes when the turn's `backend` form field read exactly
`"hermes"`. A `lifeos`-backend turn is already persisted (the gateway's
`lifeos` adapter calls LifeOS's own `/api/ask/stream`, and that call — with
its persistence — completes before the gateway emits this turn's `done`);
writing it again here would double it. An `agent`-backend turn is never
persisted anywhere, by design (its history isn't LifeOS-owned). Extracting
`backend` reliably means buffering `turn/stream`'s multipart body (Starlette's
`Request.form()`, after `await request.body()`) rather than streaming it
straight through as every other path still does — the field can land after
a multi-megabyte audio part in the browser's `FormData` encoding order, so a
bounded peek at the start of the stream isn't reliable, and correctness of
the double-write guard matters more here than the OOM-hardening ADR-016's
generic streaming default buys against a trusted, same-Tailscale-network
upload.

## Rationale

- **The proxy is the one seam every voice turn passes through, regardless of
  backend or which call path the gateway used internally.** The browser only
  ever talks to `/api/voice/turn/stream`; whatever whisper-relay does behind
  that (call LifeOS, call Hermes directly, someday call something else) is
  invisible to the browser and equally invisible to this tee — which is
  exactly why it can be backend-agnostic without needing whisper-relay's
  cooperation.
- **Reuse, not a second write path.** The tee calls the exact same
  `ConversationStore.create_conversation`/`add_message` the Hermes text
  route's `_HermesTurnPersister` calls, with the same idempotent-create +
  append grouping semantics — a multi-turn voice session groups into one
  conversation the same way a multi-turn text session does, because it's
  the same store contract.
- **`done` is the only authoritative signal.** The turn contract
  (client-surfaces.md, "Voice turn contract") already designates `done`'s
  `data` as authoritative over everything the earlier `transcript`/`response`
  events carried. Gating persistence on it, rather than accumulating from
  the earlier events, means a turn that errors, is cancelled, or whose
  connection drops before `done` — including a future bare-transcribe/
  wake-check call (#710) — persists nothing: no junk conversation for a turn
  that produced no real answer.
- **Buffering `turn/stream`'s body is scoped, not a reversal of ADR-016's
  concern.** Only this one, well-known endpoint buffers; cancel and
  audio-clip serving are untouched. A voice turn's audio upload is bounded
  by the gateway's own size cap and by what a push-to-talk recording
  realistically is (seconds, not the unbounded stream the streaming default
  was hardened against) — this is the same trade `hermes_proxy.py` already
  made for its (smaller) JSON bodies, applied here for the same reason:
  something in the body needs to be read before the call can be forwarded
  correctly.
- **Non-blocking observe, store writes only in `finalize()`.** The tee
  mirrors `_HermesTurnPersister`'s split exactly: `observe()` only
  reassembles SSE frames in memory; the one store write happens in
  `finalize()`, after the relay has already handed every byte to the
  browser (or ended early) — so persistence can never add latency to a
  chunk in flight, and the client-visible byte stream is provably identical
  whether the tee finds anything to persist or not (see the "streamed bytes
  unchanged" tests referenced below).

## Alternatives Considered

### Fix whisper-relay to call LifeOS's Hermes proxy for hermes-backend voice turns

Ship whisper-relay#32 (or equivalent): change `HTTPHermesBackendClient.ask()`
to call `POST /api/hermes/ask/stream` instead of the Hermes harness
directly, so `_HermesTurnPersister` picks up voice turns automatically —
already the documented target contract.

**Rejected for this issue specifically because:** whisper-relay is a
separate repo and out of scope here; the fix needs to work today, on the
currently-deployed gateway, without a coordinated cross-repo release. This
ADR's tee and whisper-relay#32 are not mutually exclusive — if #32 ships
later, `_HermesTurnPersister` would persist the turn via the internal
`/api/hermes/ask/stream` call, and this tee's `create_conversation`/
`add_message` calls become idempotent no-ops against the same row/messages
already written moments earlier (a redundant but harmless double-append of
identical content is the residual risk, not a broken read — an accepted
gap, not silently fixed, tracked for whisper-relay#32's eventual landing
rather than solved by adding a second guard against a contract this repo
doesn't control).

### Sniff `backend` from a bounded prefix of the streamed request, keeping the whole path unbuffered

Tee only the first N KB of the incoming multipart stream while still
forwarding it unbuffered, parsing whatever metadata fields land within that
window.

**Rejected because:** the browser's `FormData` encoding order
(`web/chat/voice.js`) appends `audio` before `backend`, so the field the
double-write guard depends on can land after a multi-megabyte audio part —
a bounded peek is not reliably correct, and an incorrect guard (silently
skipping a Hermes turn's persistence, or worse, wrongly persisting an
`agent` turn) is worse than the bounded buffering cost this alternative was
trying to avoid.

### Content-based dedup instead of reading `backend`

Skip writing a turn's `done` data if `ConversationStore` already has a
matching row for that `conversation_id`, without ever reading the request's
`backend` field.

**Rejected because:** it can't distinguish "already persisted by the
orchestrator" (lifeos) from "never meant to be persisted" (agent) — both
look identical (no existing row) to a dedup check, so it would start
persisting `agent`-backend turns, a behavior change nothing asked for and
client-surfaces.md documents as intentionally absent.

## Consequences

### Positive

- A Hermes-backend voice conversation now survives a page refresh, with the
  same sidebar/grouping behavior a Hermes text conversation already has.
- No second write path: one store, one grouping contract, shared with the
  text route.
- Works regardless of whisper-relay's version or which internal call path
  it uses for the `hermes` backend — the fix doesn't depend on a
  cross-repo release.
- A failed, cancelled, or STT-only/bare-transcribe (#710) call creates no
  junk conversation.

### Negative

- **ADR-016's "no voice logic" claim is no longer literally true for one
  endpoint.** `POST turn/stream` now carries persistence logic and buffers
  its request body, unlike every other path this proxy still forwards
  unbuffered and untouched — a deliberate, narrow exception, not a reversal
  of the reverse-proxy decision itself.
- **A latent double-append if whisper-relay#32 ships without revisiting this
  tee.** Once the gateway's Hermes adapter calls LifeOS's own Hermes proxy,
  a hermes-backend voice turn would be observed — and its user/assistant
  messages appended — by both `_HermesTurnPersister` (via the internal
  call) and this tee (via the outer `turn/stream` response), doubling the
  message rows for that turn (the conversation row itself stays a single
  idempotent `create_conversation` call). Not a broken read today; a
  follow-up once whisper-relay#32 lands.
- **`turn/stream`'s upload is no longer purely streamed.** The whole
  request body is buffered in memory before being forwarded, sized by
  whatever audio the browser recorded — bounded in practice, but a real
  memory cost this endpoint didn't have before.

## Related Documents

### Design Context

- [ADR-016: Reverse-Proxy the Voice Gateway Through LifeOS](016-voice-gateway-reverse-proxy.md) — the decision this amends; still true for every path except `turn/stream`
- [ADR-020: The Voice Detachment Gate Is Lifted](020-voice-cancel-gate-lifted.md) — the most recent prior change to voice's turn-lifetime behavior, unaffected by this one

### Specifications

- [client-surfaces.md](../specs/technical/client-surfaces.md) — "Voice transport (reverse proxy)" (the turn contract's authoritative `done` event) and "Hermes" (`_HermesTurnPersister`'s text-path persistence contract, reused verbatim here)

### Code References

- `api/routes/voice.py` — `_VoiceTurnPersister`, `_build_persister`, and the `is_turn_stream` gate in `voice_proxy()`
- `api/routes/hermes_proxy.py` — `_HermesTurnPersister`, the text-path persister this tee mirrors
- `api/services/conversation_store.py` — the shared `ConversationStore` both persisters write to
- `tests/test_voice_proxy.py` — the persistence tee's unit tests (double-write guards, grouping, failed-turn no-op, byte-identity)
