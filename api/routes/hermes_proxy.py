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
"""

import json
import logging
from typing import Optional

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

# Imported (not just re-exported through _proxy.py) so tests can monkeypatch
# `hermes_proxy.settings.hermes_backend_url` / `hermes_backend_token` directly —
# `settings` is a shared singleton, so the factory in `_proxy.py` sees the same
# mutated object.
from config.settings import settings  # noqa: F401

from api.routes._proxy import TIMEOUT, make_backend_router
from api.routes.chat import AskStreamRequest
from api.services.agent_system_prompt import build_turn_context
from api.services.conversation_store import get_store
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
    preamble = settings.resolve_persona(persona_id)
    if preamble is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona_id: {persona_id!r}")
    if settings.persona_orchestrates(persona_id):
        # Orchestrating personas (e.g. doctor) drive a background Claude Code
        # session — a LifeOS-native capability Hermes doesn't have. Routing
        # those turns to LifeOS instead is a client-side decision (#596); this
        # persona reaching the proxy means that routing didn't happen.
        raise HTTPException(
            status_code=400,
            detail=f"persona_id {persona_id!r} orchestrates and cannot be forwarded to the hermes backend",
        )

    # Spoken-style rules apply only on voice turns, matching the exact gate
    # ask_stream() uses in api/routes/chat.py: `modality == "voice" and
    # request.persona_id`. Gating on the *raw* parsed.persona_id (not the
    # primary-defaulted `persona_id` above) matters: a voice turn that omits
    # persona_id entirely gets no voice rules natively, even though primary's
    # own persona file could define some — mirror that rather than inventing
    # a rule for the omitted-persona case.
    modality = "voice" if (parsed.modality or "").strip().lower() == "voice" else "text"
    voice_rules = (
        list(settings.persona_voice(persona_id)) if modality == "voice" and parsed.persona_id else []
    )

    # No fallback: list_http_personas() draws "primary" plus every entry in
    # the same settings.telegram_bots registry resolve_persona() just matched
    # persona_id against above, so a lookup miss here can't happen for an id
    # that already passed validation.
    label = next(p.label for p in settings.list_http_personas() if p.id == persona_id)

    data["lifeos_context"] = {
        "schema_version": 1,
        "modality": modality,
        "persona": {
            "id": persona_id,
            "label": label,
            "preamble": preamble,
            "voice_rules": voice_rules,
            # Derived, not hardcoded: the guard above already rejects an
            # orchestrating persona with a 400 before this point, so this is
            # always False in practice today. But the pinned contract tells
            # Hermes to fail loudly if it ever sees `true` here — a hardcoded
            # False would lie to that check if the guard above ever moved,
            # weakened, or got reordered. Deriving costs nothing and keeps
            # this field honest regardless.
            "orchestrates": settings.persona_orchestrates(persona_id),
        },
        # A sibling of `persona`, never merged into it (#591) — `persona` is
        # stable across a conversation and cacheable; `turn` changes every
        # turn. Built by the same function the turn-context endpoint uses, so
        # the two can't drift apart. Note: `personal_context` here resolves
        # from `persona_id` alone, unlike the native path's Telegram-preamble
        # reverse lookup above — Hermes turns always carry a persona_id (or
        # default to "primary"), so that reverse lookup doesn't apply here.
        "turn": build_turn_context(persona_id),
    }
    return json.dumps(data).encode("utf-8")


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
    calculator (`api/services/cost_tracker.py`) only knows Anthropic pricing
    and would misprice a non-Anthropic upstream model badly. A malformed or
    partial `usage` event (missing/wrong-typed model or token counts) is
    ignored rather than raised; a well-formed event with no `cost_usd` is
    recorded with a zero cost rather than an invented one.
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
        elif etype == "content":
            content = event.get("content")
            if isinstance(content, str):
                self._content_parts.append(content)
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
        silently; it must never raise or interrupt the relay."""
        model = event.get("model")
        input_tokens = _coerce_token_count(event.get("input_tokens"))
        output_tokens = _coerce_token_count(event.get("output_tokens"))
        if not isinstance(model, str) or not model or input_tokens is None or output_tokens is None:
            return
        cost = event.get("cost_usd")
        cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0
        self._usage_captured = True
        self._usage_model = model
        self._usage_input_tokens = input_tokens
        self._usage_output_tokens = output_tokens
        self._usage_cost_usd = cost_usd

    def finalize(self) -> None:
        """Write this turn to the stores, once each. Runs after the relay
        has already handed off every byte it's going to (normal completion
        or an early client disconnect), so it's the only place in this
        class a blocking store call is allowed to happen.

        Conversation persistence and usage persistence are independent: a
        turn with no `conversation_id`/content still records usage if a
        `usage` event arrived, and vice versa — neither gates the other.
        """
        if self._conversation_id is not None and self._content_parts:
            conv_id = self._conversation_id
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
                    store.add_message(conv_id, "assistant", "".join(self._content_parts))
                except Exception:
                    logger.warning("hermes turn persistence: failed to save assistant reply", exc_info=True)

        if self._usage_captured:
            try:
                get_usage_store().record_usage(
                    model=self._usage_model,
                    input_tokens=self._usage_input_tokens,
                    output_tokens=self._usage_output_tokens,
                    cost_usd=self._usage_cost_usd,
                    conversation_id=self._conversation_id,
                )
            except Exception:
                logger.warning("hermes turn persistence: failed to record usage", exc_info=True)


def _make_persister(raw_body: bytes) -> Optional[_HermesTurnPersister]:
    """Build this turn's persistence tee (#592) from the same raw, pre-
    transform body `_build_envelope` already validated — a malformed body or
    an unknown/orchestrating persona 400s there before this is ever reached,
    so only minimal reparsing (question text + persona id) is needed here.
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
)
