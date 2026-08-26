"""Hermes text-backend proxy (#587) + persona/modality/turn envelope (#590, #591).

`/chat`'s third text backend: Hermes, an agent harness running as a gateway
(same box or reached over the tailnet), which speaks the same `/api/ask/stream`
SSE contract as LifeOS and the Agent backend. LifeOS proxies it at
``POST /api/hermes/ask/stream``, **adding the token server-side** so it never
reaches the browser. Empty ``LIFEOS_HERMES_BACKEND_URL`` disables it entirely —
`GET /api/hermes/status` then reports unavailable and `/chat` behaves exactly as
it does today.

Unlike the Agent backend, Hermes has no way to resolve a LifeOS persona id or
the current per-turn context (date/time, task tags, etc.) on its own, so this
route resolves both here and attaches the result to the forwarded body as a
`lifeos_context` envelope (the cross-repo contract pinned on issue #590,
extended with a `turn` sibling by #591) before forwarding. That means this
route buffers the request body instead of streaming it straight through — the
only place that happens among the text-backend proxies. The status/
bearer-injection/streaming-response logic is otherwise shared with the Agent
backend via `make_backend_router()` in `_proxy.py`.

#644: `POST /api/hermes/resolve-persona`, at the bottom of this module, is a
second, standalone entry point into the same persona resolution — for
Hermes's own Telegram front door, which never passes through the proxy above
(that would couple its Telegram availability to LifeOS being reachable).
Hermes calls it directly with the raw message text; both entry points share
`_resolve_lifeos_context()` so persona/voice/turn resolution has exactly one
implementation. See that section for the request/response contract, the
`@persona` selection grammar, and (a #644 follow-up) reply-thread persona
inheritance via `HermesPersonaThreadStore`
(`api/services/hermes_persona_thread_store.py`) and its companion
`POST /api/hermes/register-persona-message`.

#642 CROSS-REPO CONTRACT CHANGE: `lifeos_context.persona.orchestrates` can now
be `true` (previously always `false` — see `_build_envelope`'s comment on that
field). Its meaning has changed too: no longer "a LifeOS bug leaked an
orchestrating persona through, fail loudly" (the #590 contract's original
reading, back when the guard below made `true` impossible in practice) but
"this persona supervises workers through tools" — the intended path now that
#640 gives Hermes a real way to act on it. As of this writing, Hermes's
`lifeos_adapter/envelope.py` still raises `OrchestratingPersonaError` on
`true` (a fatal exception, not a warning) — tracked as `nbramia/hermes#57` to
make that informational instead. **This merge is gated on hermes#57 shipping
and deploying first**; landing this side alone would 500 every doctor turn on
Hermes rather than run one.
"""

import hmac
import json
import logging
import re
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

# Imported (not just re-exported through _proxy.py) so tests can monkeypatch
# `hermes_proxy.settings.hermes_backend_url` / `hermes_backend_token` directly —
# `settings` is a shared singleton, so the factory in `_proxy.py` sees the same
# mutated object.
from config.settings import settings  # noqa: F401

from api.routes._proxy import TIMEOUT, make_backend_router
from api.routes.chat import AskStreamRequest, journal_capture_gate, resolve_effective_persona_id
from api.services.agent_system_prompt import build_turn_context
from api.services.chat_turns import TRUNCATION_MARKER, get_turn_registry, truncation_routing
from api.services.conversation_store import get_store
from api.services.conversation_titler import schedule_retitle
from api.services.hermes_persona_thread_store import get_persona_thread_store
from api.services.journal_capture import JOURNAL_PERSONA_ID
from api.services.model_readout import record_hermes_chat_turn_model
from api.services.usage_store import get_usage_store

logger = logging.getLogger(__name__)

# Bound on an externally-minted conversation id before it reaches a SQL
# INSERT (#592). Hermes is a configured, trusted upstream, but its stream is
# still untrusted input crossing a process boundary — bounding the length
# costs nothing and the native path's own ids (uuid4, 36 chars) are nowhere
# near it.
_MAX_CONVERSATION_ID_LEN = 200


def _coerce_token_count(value: object) -> Optional[int]:
    """A `usage` event's token count is well-formed only as a plain int
    (`bool` is a `int` subclass in Python, so it's excluded explicitly).
    Anything else (missing, float, string) makes the event partial — the
    caller drops it rather than guessing."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _client() -> httpx.AsyncClient:
    """httpx client for the Hermes backend (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


def _resolve_caller_session_id(conversation_id: Optional[str], persona_id: str) -> str:
    """Hermes's `lifeos_agent_*` identity for this turn (#640).

    `SessionStore` is imported locally (not at module top) so tests can
    patch `api.services.agent_worker.session_store.SessionStore` in place —
    the same isolation pattern `api/routes/conversations.py` uses for the
    same class.

    `persona_id` (#684 review) is forwarded as the root session's `bot`
    ownership tag (`None` for `"primary"`, matching the convention every
    Telegram-spawned session already uses) — see
    `resolve_hermes_caller_session_id`'s docstring for why this matters: a
    `lifeos_agent_spawn` descendant of this session inherits it, which is
    what lets a doctor-persona Hermes conversation's spawned workers route
    their status/blocked notices back to the doctor bot instead of silently
    falling back to the primary one.
    """
    from api.services.agent_worker.hermes_session import resolve_hermes_caller_session_id
    from api.services.agent_worker.session_store import SessionStore

    bot = None if persona_id in (None, "primary") else persona_id
    return resolve_hermes_caller_session_id(SessionStore(), conversation_id, bot=bot)


def _resolve_lifeos_context(
    persona_id: str, *, modality: str, conversation_id: Optional[str], apply_voice_rules: bool,
) -> dict:
    """Resolve one persona id into the `lifeos_context` envelope's contents.

    This is the ONE place persona/voice/turn-context resolution happens for
    Hermes (#644's AC: "Persona resolution shall have exactly one source of
    truth") — extracted out of `_build_envelope` (the `/api/hermes/ask/stream`
    proxy, browser/voice-selected persona) so `resolve_persona_from_text`
    below (the Hermes-Telegram `@tag` path) can share it verbatim instead of
    re-implementing preamble/voice/label/turn lookups a second time. Purely
    extracted — every line below is unchanged from `_build_envelope`'s
    previous inline body, so the existing proxy path's behavior is untouched.

    `apply_voice_rules` is passed in rather than derived from `modality` here
    because the two callers gate it differently: `_build_envelope` mirrors
    `ask_stream`'s `modality == "voice" and request.persona_id` gate (a voice
    turn that omitted persona_id and fell back to "primary" gets no voice
    rules, even if primary's file defines some); `resolve_persona_from_text`
    only ever calls this with an explicitly-tagged persona_id, so for it
    `apply_voice_rules` is simply `modality == "voice"`.

    Raises `HTTPException(400)` for an unknown `persona_id` — the same
    contract `_build_envelope` documented inline before this extraction.
    """
    # surface="hermes" (#642): an orchestrating persona (e.g. doctor) has a
    # Hermes-specific preamble (config/personas/doctor.hermes.md, #641)
    # describing a Claude Code worker driven via lifeos_agent_spawn, not the
    # plain Telegram/web body's "you have shell access" framing — Hermes has
    # neither a shell nor that tool under the plain framing. A persona with no
    # `.hermes.md` variant (the common case) resolves to exactly the body this
    # returned before this parameter existed.
    preamble = settings.resolve_persona(persona_id, surface="hermes")
    if preamble is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona_id: {persona_id!r}")
    # #642: an orchestrating persona used to be rejected here with a 400 —
    # Hermes had no way to drive a background Claude Code session, so routing
    # one to LifeOS instead was a client-side decision (#596). #640 gave
    # Hermes that capability (lifeos_agent_spawn + a per-conversation
    # caller_session_id), so the persona now reaches Hermes like any other;
    # the surface-specific preamble resolved above is what actually tells it
    # to spawn and supervise a worker instead of answering inline.

    voice_rules = list(settings.persona_voice(persona_id)) if apply_voice_rules else []

    # No fallback: list_http_personas() draws "primary" plus every entry in
    # the same settings.telegram_bots registry resolve_persona() just matched
    # persona_id against above, so a lookup miss here can't happen for an id
    # that already passed validation.
    label = next(p.label for p in settings.list_http_personas() if p.id == persona_id)

    turn = build_turn_context(persona_id, conversation_id)
    # `caller_session_id` (#640) is added here, on the envelope's copy of
    # `turn`, rather than folded into `build_turn_context()` itself —
    # that function is shared with the plain `GET /api/chat/turn-context`
    # endpoint, which has no agent-worker session to hand out and must stay
    # untouched. It belongs in `turn`, never `persona`: `persona` is stable
    # across a conversation and prompt-cacheable, and while this value IS
    # stable across a conversation's turns (see hermes_session.py), putting
    # a session-identity concern in the cacheable half would still be the
    # wrong layering — `persona` describes the bot, `turn` describes this
    # request. Additive at schema_version 1 (no version bump): a consumer
    # that doesn't know this key ignores it exactly as it would any other
    # unrecognized field.
    turn["caller_session_id"] = _resolve_caller_session_id(conversation_id, persona_id)

    return {
        "schema_version": 1,
        "modality": modality,
        "persona": {
            "id": persona_id,
            "label": label,
            "preamble": preamble,
            "voice_rules": voice_rules,
            # #642 CROSS-REPO CONTRACT CHANGE: this can now be `true` (e.g.
            # doctor). The #590 contract told Hermes to fail loudly if it ever
            # saw `true` here, back when the 400 guard above made that
            # impossible in practice — `true` meant "a LifeOS bug leaked an
            # orchestrating persona through," so refusing was correct. That
            # guard is gone: #640 gave Hermes its own way to drive a
            # background Claude Code session (lifeos_agent_spawn), so `true`
            # now means "this persona supervises workers through tools" — the
            # intended path, not a bug. Sent as `true` deliberately (not
            # hardcoded `false` to keep the old tripwire quiet) precisely
            # because the field is derived and honest: doctor genuinely
            # orchestrates, and that's the entire point of routing it here.
            # Hermes's `lifeos_adapter/envelope.py` still raises
            # `OrchestratingPersonaError` (fatal) on `true` as of this
            # writing — `nbramia/hermes#57` makes that informational instead.
            # THIS MERGE IS GATED ON hermes#57 shipping and deploying first.
            "orchestrates": settings.persona_orchestrates(persona_id),
        },
        # A sibling of `persona`, never merged into it (#591) — `persona` is
        # stable across a conversation and cacheable; `turn` changes every
        # turn. Built by the same function the turn-context endpoint uses, so
        # the two can't drift apart. Note: `personal_context` here resolves
        # from `persona_id` alone, unlike the native path's Telegram-preamble
        # reverse lookup above — Hermes turns always carry a persona_id (or
        # default to "primary"), so that reverse lookup doesn't apply here.
        "turn": turn,
    }


def _build_envelope(raw_body: bytes) -> bytes:
    """Attach `lifeos_context` to the forwarded body, resolving the persona.

    Runs before the request reaches httpx, so a malformed body or a bad
    persona is a clean 400 — mirroring the ordering `ask_stream` in
    `api/routes/chat.py` uses for the native path (resolve persona before the
    stream opens). Every field the browser sent is preserved untouched; this
    adds exactly one top-level key.
    """
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    # Reuse the native request model's validation (attachment size/type caps,
    # persona length bound) rather than reimplementing it — see AskStreamRequest
    # in api/routes/chat.py.
    try:
        parsed = AskStreamRequest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}")

    # Same registry-backed resolution the native /api/ask/stream uses, and the
    # same default-to-primary behavior when no persona_id is sent.
    persona_id = parsed.persona_id or "primary"
    modality = "voice" if (parsed.modality or "").strip().lower() == "voice" else "text"
    # Spoken-style rules apply only on voice turns, matching the exact gate
    # ask_stream() uses in api/routes/chat.py: `modality == "voice" and
    # request.persona_id`. Gating on the *raw* parsed.persona_id (not the
    # primary-defaulted `persona_id` above) matters: a voice turn that omits
    # persona_id entirely gets no voice rules natively, even though primary's
    # own persona file could define some — mirror that rather than inventing
    # a rule for the omitted-persona case.
    apply_voice_rules = modality == "voice" and bool(parsed.persona_id)

    data["lifeos_context"] = _resolve_lifeos_context(
        persona_id,
        modality=modality,
        conversation_id=parsed.conversation_id,
        apply_voice_rules=apply_voice_rules,
    )
    return json.dumps(data).encode("utf-8")


def _journal_capture_prelude(raw_body: bytes) -> list:
    """`pre_send` hook (#685): mirrors `ask_stream`'s native journal-capture
    gate (api/routes/chat.py) on this proxy's relay path, via the shared
    `journal_capture_gate` + `resolve_effective_persona_id` helpers — the
    same one-source-of-truth argument `_resolve_lifeos_context()` already
    makes for persona resolution. Before #685, a journal-persona turn sent
    through this proxy (which `/chat` reaches by default whenever Hermes is
    available) was relayed to Hermes and never captured at all — #674's gap,
    resurrected on a surface nobody had checked.

    Deliberately does NOT reuse `_build_envelope`'s own `parsed.persona_id or
    "primary"` shorthand (adversarial-review follow-up): that ignores a raw
    `persona` preamble entirely, which is exactly the shape `chat_via_api()`
    and the ring ingest send (issue #684 is what points those callers at
    this proxy) — approximating persona resolution that way would silently
    stop capturing a raw-preamble journal turn the moment #684 lands, the
    same bug class a third time. `resolve_effective_persona_id()` reverse-
    maps a raw `persona` the same way `ask_stream()` does, and rejects the
    same malformed shapes (`persona_id` and `persona` both set; an
    unrecognized/empty `persona_id`) with the same `HTTPException(400)` —
    raised here, before the backend is contacted, exactly like
    `journal_capture_gate`'s own `HTTPException(500)` below.

    `make_backend_router`'s `pre_send` contract runs this before the backend
    is contacted, so either exception means Hermes is never sent the turn —
    no false success, and the ring ingest's idempotency key
    (api/routes/journal_ingest.py) stays unburned exactly as it does on the
    native path.

    Reparses `raw_body` independently rather than sharing `_build_envelope`'s
    parse — the same "each hook is self-contained" pattern `_make_persister`
    already uses for this identical raw body. A body that fails to parse or
    fails Pydantic validation here returns no frames rather than raising:
    `_build_envelope` (this router's `transform_body`) is what turns THAT
    kind of malformed body into a 400, and it runs on the same raw body
    regardless of hook order — only the persona-shape checks above need to
    live here too, since `_build_envelope` doesn't perform them.
    """
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    try:
        parsed = AskStreamRequest.model_validate(data)
    except ValidationError:
        return []

    effective_pid = resolve_effective_persona_id(parsed.persona_id, parsed.persona)
    result = journal_capture_gate(effective_pid, parsed.question)
    if result is None:
        return []
    # Same event shape the native path emits (api/routes/chat.py) — the ring
    # ingest and chat_via_api() require this exact event as proof of
    # capture, on either path alike.
    return [
        "data: " + json.dumps({
            "type": "journal_capture",
            "path": result.path,
            "created": result.created,
        }) + "\n\n"
    ]


class _HermesTurnPersister:
    """Read-only tee (#592, extended by #595) that reconstructs a Hermes turn
    from the bytes relayed to the browser and writes it to the conversation
    and usage stores, without altering, buffering, or delaying that relay.

    `_proxy.py`'s relay loop calls `observe()` with a copy of each chunk
    right *before* that chunk is handed to the browser, and calls
    `finalize()` exactly once when the relay ends — normal completion or an
    early disconnect alike. Hermes speaks the same SSE contract the native
    `/api/ask/stream` path emits (see api/routes/chat.py): `data: {...}\\n\\n`
    frames, a `conversation_id` event once, zero or more `content` events
    carrying incremental text, and at most one `usage` event carrying token
    counts, cost, and model. A chunk is a network read, not a frame, so
    frames can split across chunk boundaries — `observe()` reassembles them
    from a buffer rather than assuming one frame per chunk.

    `observe()` does no I/O: it only reassembles frames and buffers parsed
    text/usage data in memory (#592 review — a store call here, in the
    relay's per-chunk hot path, let a slow or locked db stall delivery of
    this stream's own next chunk; `ConversationStore._connect()`'s 10s busy
    timeout made that concrete). Every store write happens exactly once, in
    `finalize()`, after every byte of this turn has already been handed to
    the browser (or the client disconnected and no more are coming) — so a
    slow write can delay this request's own teardown but never one of its
    bytes.

    Every store call is wrapped: a persistence failure is logged and
    swallowed here so it can never surface as a broken turn.

    Usage capture (#595) shares this same observer rather than adding a
    second one over the same stream: the `usage` event's cost is recorded
    **verbatim**, never recomputed from the token counts — the cost
    calculator (`agent_worker/pricing.py`'s `cost_for`, #656) only knows
    Anthropic pricing and would misprice a non-Anthropic upstream model
    badly. A malformed or partial `usage` event (missing/wrong-typed model or
    token counts) is ignored rather than raised; a well-formed event with no
    `cost_usd` is recorded with a zero cost rather than an invented one.

    #611: `_proxy.py`'s pump now runs as a detached, registry-owned
    background task, so `finalize()` fires on the REAL end of the turn (the
    upstream connection closing) rather than on an early client disconnect —
    a disconnected browser no longer truncates what gets persisted here (see
    `bind_turn()`). What can still genuinely truncate a Hermes turn is the
    upstream connection itself ending before a `done` event ever arrived
    (Hermes crashed, was killed, or the connection dropped mid-turn) — this
    class can't tell that apart from an ordinary network hiccup on its own,
    so it tracks only whether `done` was observed and lets `finalize()`
    decide: seen it, persist verbatim (as always); never saw it, append
    `TRUNCATION_MARKER` and mark `routing.truncated` — the same visible
    signal the native path gives a cancelled/errored turn, via the same
    `truncation_routing()` helper.
    """

    _FRAME_SEP = b"\n\n"

    def __init__(self, *, question: str, persona_id: str):
        self._question = question
        self._persona_id = persona_id
        self._buf = b""
        self._conversation_id_seen = False
        self._conversation_id: Optional[str] = None
        self._content_parts: list[str] = []
        self._usage_captured = False
        self._usage_model: Optional[str] = None
        self._usage_input_tokens = 0
        self._usage_output_tokens = 0
        self._usage_cost_usd = 0.0
        self._usage_unpriced = False
        # #611: whether a `done` event was ever observed -- see the class
        # docstring's #611 paragraph. finalize() treats "never saw done" as
        # a genuine truncation, distinct from #592's disconnect-truncation
        # (which #611 eliminated for this class).
        self._done_seen = False
        # #611: the ChatTurn this persister's turn is registered under, once
        # `_proxy.py`'s detached pump has one to hand it (bind_turn() is
        # called right after construction, before any observe()). Kept so
        # `_handle_event` can register the turn's conversation id with the
        # shared registry the moment it's observed -- the same "learn the id
        # from the first SSE frame" pattern the native path uses for a
        # brand-new conversation, just triggered by an observed frame here
        # instead of a locally-created row.
        self._turn = None

    def bind_turn(self, turn) -> None:
        """Hook `_proxy.py`'s detached pump calls once it creates this
        turn's `ChatTurn` — see the class docstring. If a `conversation_id`
        was somehow already observed before this call (shouldn't happen in
        practice: bind_turn() runs immediately after construction, before
        the pump's first `observe()`), bind it immediately rather than
        waiting for a `conversation_id` event that will never arrive again."""
        self._turn = turn
        if self._conversation_id is not None:
            get_turn_registry().bind(turn, self._conversation_id)

    def observe(self, chunk: bytes) -> None:
        """Non-blocking, in-memory-only frame reassembly — see the class
        docstring for why no store call belongs here."""
        try:
            self._buf += chunk
            # SSE frames may be separated by CRLF (`\r\n\r\n`) as well as
            # bare LF (`\n\n`) — an intermediary is free to rewrite line
            # endings even though the Hermes adapter itself only emits LF
            # today (#592 review: without this, a CRLF-framed stream never
            # matched `_FRAME_SEP` and nothing was ever persisted, silently).
            # Collapsing CRLF to LF here lets the existing separator check
            # and per-line split below handle both without duplicating the
            # frame/line parsing logic.
            self._buf = self._buf.replace(b"\r\n", b"\n")
            while self._FRAME_SEP in self._buf:
                frame, self._buf = self._buf.split(self._FRAME_SEP, 1)
                self._handle_frame(frame)
        except Exception:
            logger.warning("hermes turn persistence: failed to parse a relayed chunk", exc_info=True)

    def _handle_frame(self, frame: bytes) -> None:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[len(b"data:"):].strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "conversation_id" and not self._conversation_id_seen:
            # Recorded once regardless of outcome — the backend sends this
            # event at most once per turn, so there's no later event to
            # retry against.
            self._conversation_id_seen = True
            conv_id = event.get("conversation_id")
            if isinstance(conv_id, str) and conv_id:
                if len(conv_id) > _MAX_CONVERSATION_ID_LEN:
                    # Don't truncate (#592 review): the browser holds the
                    # verbatim upstream id and will later request it in full
                    # via GET /api/conversations/{id}. A row created under a
                    # truncated id would silently diverge from that — the
                    # browser's own thread could never be found on reload.
                    # Treat this as a logged persistence failure for the
                    # turn instead of creating a mismatched row.
                    logger.warning(
                        "hermes turn persistence: conversation id is %d chars, "
                        "over the %d-char cap - dropping this turn's "
                        "persistence rather than storing it under a "
                        "truncated, mismatched id",
                        len(conv_id), _MAX_CONVERSATION_ID_LEN,
                    )
                else:
                    self._conversation_id = conv_id
                    # #611: the turn becomes cancellable/supersedable by this
                    # id the moment it's known -- before this, it exists
                    # (the pump is already running) but isn't reachable by
                    # conversation id yet, a sub-second window acceptable
                    # per the #611 design.
                    if self._turn is not None:
                        get_turn_registry().bind(self._turn, conv_id)
        elif etype == "content":
            content = event.get("content")
            if isinstance(content, str):
                self._content_parts.append(content)
        elif etype == "done":
            # #611: the backend's own signal that this turn ran to a normal
            # completion -- see the class docstring's #611 paragraph.
            self._done_seen = True
        elif etype == "usage" and not self._usage_captured:
            # Not "seen once" like conversation_id above: a malformed usage
            # event (below) leaves `_usage_captured` False, so a later
            # well-formed one can still be captured rather than being
            # permanently shadowed by an earlier bad one.
            self._handle_usage(event)

    def _handle_usage(self, event: dict) -> None:
        """Validate and capture a `usage` event's fields (#595). Cost is
        taken verbatim from upstream — never recomputed here — and a
        missing/non-numeric cost records as zero rather than a guess. A
        malformed or partial event (bad model or token counts) is dropped
        silently; it must never raise or interrupt the relay.

        `unpriced` (#613) tracks *why* the recorded cost is zero: an absent
        or non-numeric `cost_usd` (this upstream genuinely couldn't price
        the turn) vs. a real reported `0` (a free model) — see the same
        presence-and-type distinction #602 gives the live SSE display.
        Persisted alongside the cost so a later reader can tell the two
        apart, which the bare `cost_usd` column alone cannot."""
        model = event.get("model")
        input_tokens = _coerce_token_count(event.get("input_tokens"))
        output_tokens = _coerce_token_count(event.get("output_tokens"))
        if not isinstance(model, str) or not model or input_tokens is None or output_tokens is None:
            return
        cost = event.get("cost_usd")
        priced = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        cost_usd = float(cost) if priced else 0.0
        self._usage_captured = True
        self._usage_model = model
        self._usage_input_tokens = input_tokens
        self._usage_output_tokens = output_tokens
        self._usage_cost_usd = cost_usd
        self._usage_unpriced = not priced
        # #658: the model that actually answered this turn, per Hermes's own
        # report — see model_readout.py's module docstring for why this is
        # the only trustworthy live signal for the "hermes_chat" surface.
        record_hermes_chat_turn_model(model)

    def finalize(self) -> None:
        """Write this turn to the stores, once each. Runs after the pump has
        drained upstream to its real end (#611 — no longer just "however far
        the browser stuck around for"), so it's the only place in this class
        a blocking store call is allowed to happen.

        Conversation persistence and usage persistence are independent: a
        turn with no `conversation_id`/content still records usage if a
        `usage` event arrived, and vice versa — neither gates the other.
        """
        if self._conversation_id is not None and self._content_parts:
            conv_id = self._conversation_id
            content = "".join(self._content_parts)
            # #611: no `done` event ever arrived -- the upstream connection
            # ended (or is still running when this fires, e.g. a
            # cancellation) without confirming the turn actually finished.
            # Mark it the same way the native path marks a cancelled/errored
            # turn, so a genuinely truncated Hermes reply is never presented
            # as if it were whole.
            routing = None
            if not self._done_seen:
                content += TRUNCATION_MARKER
                routing = truncation_routing("stream_error")
            try:
                store = get_store()
                store.create_conversation(
                    conv_id=conv_id, persona_id=self._persona_id, backend="hermes",
                )
                store.add_message(conv_id, "user", self._question)
            except Exception:
                logger.warning("hermes turn persistence: failed to create conversation %r", conv_id, exc_info=True)
            else:
                try:
                    store.add_message(conv_id, "assistant", content, routing=routing)
                except Exception:
                    logger.warning("hermes turn persistence: failed to save assistant reply", exc_info=True)
                else:
                    # Shared post-turn titling seam (conversation_titler.py) —
                    # same call the native path and the voice tee make; no-ops
                    # unless this is exactly the 2nd user message.
                    schedule_retitle(conv_id)

        if self._usage_captured:
            try:
                get_usage_store().record_usage(
                    model=self._usage_model,
                    input_tokens=self._usage_input_tokens,
                    output_tokens=self._usage_output_tokens,
                    cost_usd=self._usage_cost_usd,
                    conversation_id=self._conversation_id,
                    unpriced=self._usage_unpriced,
                )
            except Exception:
                logger.warning("hermes turn persistence: failed to record usage", exc_info=True)


def _make_persister(raw_body: bytes) -> Optional[_HermesTurnPersister]:
    """Build this turn's persistence tee (#592) from the same raw, pre-
    transform body `_build_envelope` already validated — a malformed body or
    an unknown persona 400s there before this is ever reached (an
    orchestrating persona no longer does, #642), so only minimal reparsing
    (question text + persona id) is needed here.
    """
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    question = data.get("question")
    if not isinstance(question, str):
        return None
    persona_id = data.get("persona_id")
    if not isinstance(persona_id, str) or not persona_id:
        persona_id = "primary"
    return _HermesTurnPersister(question=question, persona_id=persona_id)


router = make_backend_router(
    prefix="/api/hermes",
    tag="hermes",
    backend_label="hermes",
    url_attr="hermes_backend_url",
    token_attr="hermes_backend_token",
    client_factory=lambda: _client(),
    transform_body=_build_envelope,
    make_observer=_make_persister,
    pre_send=_journal_capture_prelude,
)


# ---------------------------------------------------------------------------
# `POST /api/hermes/resolve-persona` (#644) — persona selection for Hermes's
# own Telegram front door, which never passes through the proxy above (that
# would couple its availability to LifeOS being up — see #658/#644's "two
# decisions already made"). Hermes instead calls this endpoint directly with
# the raw message text; the response tells it which persona (if any) was
# selected, the text with the selector stripped, and the same
# `lifeos_context` envelope `_build_envelope` above would attach for that
# persona — computed by the SAME `_resolve_lifeos_context` helper, so this
# and the proxy path can never resolve a persona differently.
#
# Resolution is an ORDERED rule, not a single check (#644 follow-up — reply-
# thread persona inheritance):
#   1. An explicit `@persona` prefix on this message wins, always — even
#      inside an existing persona thread, so replying with a new tag switches
#      personas mid-thread.
#   2. Else, if this message is a reply (Telegram's native reply-to) to a
#      message whose persona is known, inherit it.
#   3. Else, no persona — byte-identical to today.
# The mapping behind rule 2 (`HermesPersonaThreadStore`,
# api/services/hermes_persona_thread_store.py) is the one place that state
# lives, deliberately on the LifeOS side: putting it in Hermes would make
# persona resolution two-sourced again. `POST /api/hermes/register-persona-
# message`, below `resolve_persona_from_text`, is how Hermes anchors its OWN
# reply's message id to the same persona — needed because that id isn't
# known until after Hermes has already sent the reply to Telegram, which is
# necessarily after this endpoint already returned.
# ---------------------------------------------------------------------------

# Selection mechanism (Nathan's decision, documented in
# docs/specs/technical/client-surfaces.md): a per-message `@name` prefix, no
# persisted state — every message is judged independently. A tag is
# recognized only when it opens the message, the character right after `@`
# is a letter, and it's immediately followed by whitespace or the end of the
# message. Every configured persona id is `[a-z0-9_-]+` and, in practice,
# always letter-led (`_BOT_NAME_RE` in config/settings.py permits more, but
# no persona is actually named e.g. "123") — requiring a leading letter is a
# deliberate heuristic, not a hard technical constraint, chosen so an
# ordinary message that merely *starts* with `@` (a bare "@", "@3pm meeting",
# "@ what's up") is never mistaken for a tag attempt. Punctuation glued
# directly onto the tag (`@doctor,`) is likewise not recognized — the
# grammar matches the one documented form (`@doctor <message>`) rather than
# guessing at variants.
_PERSONA_TAG_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", re.DOTALL)


def _parse_persona_tag(text: str) -> tuple[Optional[str], str]:
    """Split a raw Hermes-Telegram message into an optional persona tag
    (lowercased) and the remaining text — see the prefix-grammar comment
    above `_PERSONA_TAG_RE`.

    Returns `(None, text)` unchanged when no tag-shaped prefix is present.
    A tag-shaped prefix that doesn't match any *configured* persona is still
    returned here (lowercased, not discarded) — `resolve_persona_from_text`
    below is responsible for treating that as a deliberate error rather than
    silently falling back to "no persona selected", which would make a typo
    indistinguishable from working.
    """
    match = _PERSONA_TAG_RE.match(text)
    if not match:
        return None, text
    tag = match.group(1).lower()
    remainder = (match.group(2) or "").strip()
    return tag, remainder


class HermesPersonaResolveRequest(BaseModel):
    """Body for `POST /api/hermes/resolve-persona` — a raw, not-yet-split
    Hermes-Telegram message. `text` stands in for `AskStreamRequest.question`:
    no question is being answered here, only a persona (if any) resolved.

    `chat_id`/`message_id`/`reply_to_message_id` are opaque string ids
    (Hermes stringifies Telegram's integer ids the same way `conversation_id`
    is already an opaque string elsewhere on this contract) used only for
    reply-thread persona inheritance (#644 follow-up):
    - `reply_to_message_id`, when this message is a Telegram reply, is looked
      up in `HermesPersonaThreadStore` (scoped to `chat_id`) to inherit a
      persona when this message carries no explicit `@tag` of its own.
    - `chat_id` + `message_id` (THIS message's own id) are where the
      resolved persona, if any, gets recorded — so a later reply to *this*
      message can itself inherit. All three are optional: omitting them
      degrades to the base (non-threaded) contract, tag or nothing.
    """
    text: str
    modality: Optional[str] = None
    conversation_id: Optional[str] = None
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None


def _check_hermes_inbound_auth(request: Request) -> None:
    """Bearer-token gate for the inbound Hermes-to-LifeOS direction, shared
    by `/resolve-persona` and `/register-persona-message`.

    Reuses `hermes_backend_token` — the same shared secret already
    authenticating the outbound LifeOS-to-Hermes call `_build_envelope`'s
    proxy makes — rather than inventing a second Hermes-specific token for
    the reverse direction. Disabled (503) until a token is configured,
    mirroring `api/routes/fitness.py`'s `_check_ingest_auth`: a fresh clone
    has these endpoints closed until an operator deliberately sets one, since
    unlike the outbound direction (where an empty token just means "send no
    auth to a backend I trust"), an empty token here would mean "accept
    unauthenticated input from the network."
    """
    expected = settings.hermes_backend_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Hermes persona resolution disabled: set LIFEOS_HERMES_BACKEND_TOKEN to enable.",
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@router.post("/resolve-persona")
async def resolve_persona_from_text(request: Request):
    """Resolve a Hermes-Telegram message's persona: explicit `@tag` first,
    then reply-thread inheritance, then nothing — see the ordered-rule
    comment above this section.

    Response shape: `{"persona_id": str | None, "text": str, "lifeos_context":
    dict | None}`. Neither a tag nor an inheritable reply -> `persona_id`/
    `lifeos_context` are both `None` and `text` is returned byte-identical to
    the input `text` field — Hermes's untagged path needs nothing from this
    endpoint and should behave exactly as it did before this endpoint
    existed. A resolved persona (tag or inherited) resolves `lifeos_context`
    via `_resolve_lifeos_context` (the same helper `_build_envelope` uses for
    `/api/hermes/ask/stream`); a tag also strips itself from `text`, while an
    inherited persona leaves `text` untouched (there was no prefix to strip).
    An `@`-prefixed token that isn't a known persona id is a 400, not a
    silent fall-through to "no persona" — a typo must not look like it
    worked. An unknown or expired `reply_to_message_id` is NOT an error —
    silently falls through to "no persona", the same as a thread that
    started before this feature existed.
    """
    _check_hermes_inbound_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        parsed = HermesPersonaResolveRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}")

    tag, remainder = _parse_persona_tag(parsed.text)
    if tag is not None:
        # Rule 1: an explicit tag always wins, even over an inheritable
        # reply — this is how a thread switches personas mid-stream.
        persona_id = tag
        resolved_text = remainder
    elif parsed.reply_to_message_id and parsed.chat_id:
        # Rule 2: inherit the replied-to message's persona, if LifeOS still
        # has it on record. A miss (unknown id, expired, cross-chat, or a
        # thread from before this feature shipped) is not an error — it's
        # rule 3, indistinguishable from "no tag at all".
        persona_id = get_persona_thread_store().lookup(parsed.chat_id, parsed.reply_to_message_id)
        resolved_text = parsed.text
    else:
        persona_id = None
        resolved_text = parsed.text

    if persona_id is None:
        # Rule 3.
        return {"persona_id": None, "text": parsed.text, "lifeos_context": None}

    if persona_id == JOURNAL_PERSONA_ID:
        # #685 adversarial-review follow-up: this endpoint resolves a
        # persona WITHOUT running a turn — Hermes's own Telegram front door
        # calls it to build an envelope BEFORE deciding what, if anything,
        # it does with the message — so there is no ask/stream turn here for
        # the journal-capture gate to hook (that gate lives on the relay
        # above, keyed off a persona id that's actually about to drive a
        # turn). Silently resolving `journal` here would either double-
        # capture once Hermes relays the real turn through that proxy, or
        # capture text that never becomes a turn at all (a reply Hermes
        # never sends, a tag with no follow-up). Until this surface has its
        # own capture/proof protocol, refuse it visibly rather than let
        # Hermes reply "Logged." with nothing on disk — the #674/#685
        # signature a third time, this time silent since nothing here would
        # even try to write. Applies identically whether `journal` came from
        # an explicit `@journal` tag (rule 1) or thread inheritance (rule
        # 2) — deliberately checked after both, once persona_id is settled,
        # rather than duplicated in each branch above.
        raise HTTPException(
            status_code=400,
            detail="journal capture isn't available via the Hermes Telegram bot; use the journal bot",
        )

    modality = "voice" if (parsed.modality or "").strip().lower() == "voice" else "text"
    # A resolved persona — tag or inherited — is always the "chose a
    # persona" case for this endpoint, unlike `_build_envelope`'s
    # default-to-primary path, so there's no "omitted persona_id" ambiguity
    # to gate voice_rules on here.
    context = _resolve_lifeos_context(
        persona_id, modality=modality, conversation_id=parsed.conversation_id, apply_voice_rules=(modality == "voice"),
    )

    # Anchor THIS message under its resolved persona too, so a later reply to
    # it — whether it arrived via an explicit tag or by inheriting one —
    # itself becomes a valid inheritance target. This is what makes a
    # reply-to-a-reply chain inherit transitively with no special case: every
    # link records itself the same way.
    if parsed.chat_id and parsed.message_id:
        get_persona_thread_store().record(parsed.chat_id, parsed.message_id, persona_id)

    return {"persona_id": persona_id, "text": resolved_text, "lifeos_context": context}


class HermesRegisterPersonaMessageRequest(BaseModel):
    """Body for `POST /api/hermes/register-persona-message` — anchors a
    message Hermes itself authored (its reply to a resolved persona turn) to
    that persona, so a later reply threaded off the BOT's message inherits
    too, not just one threaded off the user's original tagged message.

    Needed as a second call, after the fact: the bot reply's own message id
    isn't known until Telegram has accepted it, which is necessarily after
    `/resolve-persona` already returned.
    """
    chat_id: str
    message_id: str
    persona_id: str


@router.post("/register-persona-message")
async def register_persona_message(request: Request):
    """Anchor a Hermes-authored message id to the persona it was sent under.

    `persona_id` must be a currently-configured persona (the same check
    `_resolve_lifeos_context` applies) — defense in depth against a stale or
    malformed id polluting the thread table, even though in practice Hermes
    only ever passes back an id this same service handed it moments earlier
    from `/resolve-persona`.
    """
    _check_hermes_inbound_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        parsed = HermesRegisterPersonaMessageRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}")

    if settings.resolve_persona(parsed.persona_id, surface="hermes") is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona_id: {parsed.persona_id!r}")

    get_persona_thread_store().record(parsed.chat_id, parsed.message_id, parsed.persona_id)
    return {"ok": True}
