"""Reverse proxy for the voice gateway (whisper-relay) — #361.

LifeOS owns the unified `/chat` client; voice *transport* lives in the separate
whisper-relay app (STT → backend → TTS). We reverse-proxy ``/api/voice/*`` to
``LIFEOS_VOICE_GATEWAY_URL`` so the browser stays same-origin: one HTTPS/Tailscale
front, one mic permission, no CORS. See ADR-016 and
``docs/specs/technical/client-surfaces.md``.

LifeOS adds **no** voice/turn logic here — it only forwards the request/response,
streaming both directions so SSE turn events and audio clips pass through
unbuffered. The gateway is trusted (localhost) and its URL is server config (not
user-controllable, so no SSRF); LifeOS is the access-control front, consistent
with the rest of the API's localhost/Tailscale trust model.

The ONE exception (#711, see ADR-021): ``POST turn/stream`` is additionally
tee'd into the same conversation store the Hermes text route uses, since a
Hermes-backend voice turn otherwise never touches a LifeOS persister at all —
see ``_VoiceTurnPersister`` below for why and its double-write guard. Every
other path (cancel, audio clips, anything the gateway adds later) is the
unmodified pass-through this module always was.
"""

import json
import logging
from typing import Optional
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings

from api.routes._proxy import TIMEOUT, filter_headers as _filter_headers
from api.services.conversation_store import get_store
from api.services.conversation_titler import schedule_retitle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

# Same bound `hermes_proxy.py` applies to an externally-minted conversation id
# before it reaches a SQL INSERT (#592) — the gateway is trusted, but its
# stream (and, transitively, whatever backend it relayed) is still untrusted
# input crossing a process boundary.
_MAX_CONVERSATION_ID_LEN = 200


def _client() -> httpx.AsyncClient:
    """The httpx client used to reach the gateway (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


class _VoiceTurnPersister:
    """Read-only tee (#711) on ``POST turn/stream``'s relayed SSE response,
    persisting a Hermes-backend voice turn to the same conversation store the
    Hermes text route uses (``api/services/conversation_store.py``, via
    ``api/routes/hermes_proxy.py``'s own tee for the text path).

    Why the tee lives HERE, not at a backend-specific seam: whisper-relay's
    Hermes adapter (``voice_gateway/adapters/hermes_backend.py`` in the
    whisper-relay checkout) calls the Hermes harness's own
    ``/api/ask/stream`` directly, never LifeOS's ``/api/hermes/ask/stream``
    proxy — confirmed by reading that adapter, not assumed. So
    ``_HermesTurnPersister`` (hermes_proxy.py) never sees a Hermes voice
    turn at all; this repo's client-surfaces.md documents the gateway
    routing hermes voice turns through LifeOS's Hermes proxy as the *target*
    cross-repo contract (`nbramia/whisper-relay#32`), not what the deployed
    gateway does today. This proxy (`/api/voice/*`) is the one seam every
    voice turn passes through regardless of backend or which call path the
    gateway used internally, since the browser only ever talks to
    ``/api/voice/turn/stream`` — so it's where a backend-agnostic tee has to
    live until whisper-relay#32 ships.

    Double-write guard: a ``lifeos``-backend turn IS already persisted, by
    the native orchestrator — the gateway's lifeos adapter
    (``voice_gateway/adapters/lifeos.py``) calls LifeOS's own
    ``/api/ask/stream``, the exact native path every other lifeos-backend
    turn takes, and that call (and its persistence) completes before the
    gateway ever emits this turn's ``done`` event. An ``agent``-backend turn
    is never persisted anywhere, by design — its conversation history isn't
    LifeOS-owned (see client-surfaces.md's Agent-backend section). So this
    tee writes ONLY when the turn's ``backend`` form field read exactly
    ``"hermes"``; every other value — including one this tee failed to read
    — is a no-op, never a guess.

    Persistence trigger: the terminal ``done`` event's ``data``, the turn
    contract's one **authoritative** field (client-surfaces.md, "Voice turn
    contract"). A turn that errors, is cancelled, or whose upstream
    connection ends before ``done`` arrives — including a future bare-
    transcribe/wake-check call (#710) that never reaches a real answer —
    never has a ``done`` event observed, so ``finalize()`` writes nothing:
    no junk conversation for a turn that produced no real response.

    ``observe()`` does no I/O — in-memory SSE frame reassembly only,
    mirroring ``_HermesTurnPersister`` in hermes_proxy.py. ``finalize()`` —
    the only place a store call happens — runs once the relay has ended
    (normal completion or an early disconnect alike), after every byte
    already reached the browser, so persistence can never delay a chunk in
    flight. Every store call is wrapped so a persistence failure is logged
    and swallowed, never surfacing as a broken turn.
    """

    _FRAME_SEP = b"\n\n"

    def __init__(self, *, backend: str, persona_id: str):
        self._backend = backend
        self._persona_id = persona_id
        self._buf = b""
        self._done_data: Optional[dict] = None

    def observe(self, chunk: bytes) -> None:
        """Non-blocking, in-memory-only frame reassembly."""
        try:
            self._buf += chunk
            # CRLF-framed SSE is possible even though the gateway only emits
            # LF today — same defensive normalization _HermesTurnPersister
            # applies, for the same reason (an intermediary could rewrite
            # line endings).
            self._buf = self._buf.replace(b"\r\n", b"\n")
            while self._FRAME_SEP in self._buf:
                frame, self._buf = self._buf.split(self._FRAME_SEP, 1)
                self._handle_frame(frame)
        except Exception:
            logger.warning(
                "voice turn persistence: failed to parse a relayed chunk", exc_info=True
            )

    def _handle_frame(self, frame: bytes) -> None:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[len(b"data:"):].strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "done":
                continue
            data = event.get("data")
            if isinstance(data, dict):
                # The gateway sends at most one `done` per turn; if it
                # somehow sent more, the first one observed wins rather than
                # a later one silently overwriting it.
                if self._done_data is None:
                    self._done_data = data

    def finalize(self) -> None:
        """Write this turn to the conversation store, once, iff it was a
        Hermes-backend turn that actually produced a `done` event."""
        if self._backend != "hermes" or self._done_data is None:
            return
        conv_id = self._done_data.get("conversation_id")
        transcript = self._done_data.get("transcript")
        response_text = self._done_data.get("response_text")
        if not isinstance(conv_id, str) or not conv_id:
            return
        if len(conv_id) > _MAX_CONVERSATION_ID_LEN:
            logger.warning(
                "voice turn persistence: conversation id is %d chars, over "
                "the %d-char cap - dropping this turn's persistence rather "
                "than storing it under a truncated, mismatched id",
                len(conv_id), _MAX_CONVERSATION_ID_LEN,
            )
            return
        if not isinstance(transcript, str) or not transcript:
            return
        if not isinstance(response_text, str) or not response_text:
            return
        try:
            store = get_store()
            store.create_conversation(conv_id=conv_id, persona_id=self._persona_id, backend="hermes")
            store.add_message(conv_id, "user", transcript)
        except Exception:
            logger.warning(
                "voice turn persistence: failed to create conversation %r", conv_id, exc_info=True
            )
            return
        try:
            store.add_message(conv_id, "assistant", response_text)
        except Exception:
            logger.warning("voice turn persistence: failed to save assistant reply", exc_info=True)
        else:
            # Shared post-turn titling seam (conversation_titler.py) — same
            # call the native path and the Hermes proxy tee make; no-ops
            # unless this is exactly the 2nd user message.
            schedule_retitle(conv_id)


async def _build_persister(request: Request) -> "_VoiceTurnPersister":
    """Best-effort read of the turn/stream request's `backend`/`persona_id`
    form fields (#711), for the persister's double-write guard.

    Requires the request body already buffered (`await request.body()`) by
    the caller — Starlette's `Request.form()` then parses the cached bytes
    rather than re-reading the (already-consumed) ASGI stream. A parse
    failure (malformed body, unexpected content-type) leaves `backend` at
    its "lifeos" default, which is exactly "don't persist" — a request this
    tee can't understand is the gateway's problem to reject, not this one's
    to guess about.
    """
    backend = "lifeos"
    persona_id = "primary"
    form = None
    try:
        form = await request.form()
        raw_backend = form.get("backend")
        if isinstance(raw_backend, str) and raw_backend.strip():
            backend = raw_backend.strip().lower()
        raw_persona = form.get("persona_id")
        if isinstance(raw_persona, str) and raw_persona.strip():
            persona_id = raw_persona.strip()
    except Exception:
        logger.warning(
            "voice turn persistence: failed to parse turn/stream form fields", exc_info=True
        )
    finally:
        if form is not None:
            await form.close()
    return _VoiceTurnPersister(backend=backend, persona_id=persona_id)


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def voice_proxy(path: str, request: Request):
    """Forward any ``/api/voice/<path>`` request to the voice gateway, streaming."""
    # Basic guard against an obviously crafted path. The gateway routes by its
    # declared routes (no filesystem traversal is reachable) and its host is
    # fixed config, so this is just hygiene, not a security boundary.
    if ".." in path or ".." in unquote(path):
        raise HTTPException(status_code=400, detail="invalid path")

    base = settings.voice_gateway_url.rstrip("/")
    url = f"{base}/api/voice/{path}"

    # #711: only `POST turn/stream` is tee'd for persistence — every other
    # path (cancel, audio clips, any future gateway route) stays the
    # unbuffered pass-through this proxy always was. Buffering the request
    # body only for this one, well-known endpoint (rather than streaming it,
    # as every other path still does) is a deliberate, scoped trade: the
    # `backend`/`persona_id` fields this tee's double-write guard depends on
    # are ordinary multipart form fields, not something a streamed body can
    # be inspected for without buffering it. See ADR-021.
    is_turn_stream = path == "turn/stream" and request.method == "POST"

    persister: Optional[_VoiceTurnPersister] = None
    request_body: Optional[bytes] = None
    if is_turn_stream:
        request_body = await request.body()
        persister = await _build_persister(request)

    client = _client()
    try:
        upstream_req = client.build_request(
            request.method,
            url,
            params=request.query_params,
            headers=_filter_headers(request.headers),
            content=(
                request_body if is_turn_stream
                # Stream the request body upstream rather than buffering it, so
                # a large upload can't OOM LifeOS (the gateway enforces its own
                # size cap once it receives the stream) — unchanged for every
                # path except turn/stream, above.
                else (request.stream() if request.method == "POST" else None)
            ),
        )
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.RequestError, httpx.InvalidURL) as exc:
        await client.aclose()
        logger.warning("voice gateway request failed (%s %s): %s", request.method, path, exc)
        raise HTTPException(status_code=502, detail=f"voice gateway unreachable: {exc}")

    async def relay():
        try:
            # Raw bytes pass through unchanged (preserves any Content-Encoding,
            # flushes SSE/audio chunks as they arrive) — observing (when a
            # persister is set) never alters what's yielded to the browser.
            async for chunk in upstream.aiter_raw():
                if persister is not None:
                    persister.observe(chunk)
                yield chunk
        finally:
            if persister is not None:
                persister.finalize()
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=_filter_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
