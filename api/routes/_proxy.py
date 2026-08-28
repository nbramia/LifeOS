"""Shared helpers for the streaming reverse proxies (voice, agent, hermes) —
#361, generalized in #587.

Keeps the security-relevant header-filtering and timeout in one place so the
voice, agent, and hermes proxies stay in lockstep.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings
from api.services.chat_turns import get_turn_registry

logger = logging.getLogger(__name__)

# Reachability probe cache (#688): base_url -> (expires_at monotonic, reachable).
# A backend can be *configured* (a URL is set) while its process is actually
# down, and treating "configured" alone as "available" left `/chat` defaulting
# to a dead backend and failing every turn at send time instead of falling
# back to lifeos. Caching with a short TTL means /status never adds a
# round-trip to every page load and never hammers a downed backend either —
# it's re-probed at most once per TTL window. Keyed by base_url (not by
# backend) so agent and hermes, if both opted in, never collide.
_REACHABILITY_CACHE: dict[str, tuple[float, bool]] = {}
_REACHABILITY_CACHE_TTL_SECONDS = 30
_REACHABILITY_PROBE_TIMEOUT = httpx.Timeout(2.0)
# One lock per base_url so concurrent /status calls that land in the same
# expiry window (e.g. two browser tabs loading at once) share a single
# in-flight probe instead of each firing their own — without this, "hammers
# a downed backend" was only true for strictly sequential callers, not
# concurrent ones.
_REACHABILITY_LOCKS: dict[str, asyncio.Lock] = {}


async def _probe_reachable(client_factory: Callable[[], httpx.AsyncClient], base_url: str, backend_label: str) -> bool:
    """Cheap, cached reachability probe for a configured backend (#688).

    Any HTTP response at all — even a 404 — proves the process behind
    `base_url` is up and answering; this isn't assumed to expose `/health`,
    so only a connection-level failure (refused, timed out, DNS failure)
    counts as unreachable. Uses the same `client_factory()` seam the real
    proxy call uses (with a short per-request timeout override) so tests can
    route this through the same in-process ASGI transport, no sockets
    involved either way.
    """
    now = time.monotonic()
    cached = _REACHABILITY_CACHE.get(base_url)
    if cached and cached[0] > now:
        return cached[1]

    lock = _REACHABILITY_LOCKS.setdefault(base_url, asyncio.Lock())
    async with lock:
        # Re-check now that we hold the lock: whoever got here first may
        # have already refreshed the cache while we were waiting.
        now = time.monotonic()
        cached = _REACHABILITY_CACHE.get(base_url)
        if cached and cached[0] > now:
            return cached[1]

        reachable = False
        client = client_factory()
        try:
            await client.get(f"{base_url.rstrip('/')}/health", timeout=_REACHABILITY_PROBE_TIMEOUT)
            reachable = True
        except (httpx.RequestError, httpx.InvalidURL) as e:
            logger.warning(f"{backend_label} backend at {base_url} is configured but unreachable: {e}")
        finally:
            await client.aclose()

        _REACHABILITY_CACHE[base_url] = (now + _REACHABILITY_CACHE_TTL_SECONDS, reachable)
        return reachable

# Voice/agent turns run STT → LLM → TTS and can take a while; a generous read
# budget so long turns aren't cut off.
TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=5.0)

# Hop-by-hop headers are connection-specific and must not be forwarded by a proxy
# (RFC 7230 §6.1). Content-Length is dropped too; httpx/Starlette recompute it.
# `authorization` is stripped so a client can never inject upstream credentials —
# each proxy adds its own auth (or none) server-side.
HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "authorization",
})


def filter_headers(headers) -> dict:
    """Drop hop-by-hop (and inbound auth) headers before forwarding."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def _sniff_modality(raw_body: bytes) -> str:
    """Best-effort read of the request's `modality` field (#611), straight
    off the same raw pre-transform body `transform_body`/`make_observer`
    already receive — generic across whatever backend uses this router, not
    Hermes-specific. Recorded on the `ChatTurn` for parity with the native
    path's own turn (and, until #616, used to gate a relayed voice turn out
    of detachment the same way ChatTurn.reader() gated the native path — that
    gate is now lifted, so this no longer affects cancellation on its own);
    anything unparseable defaults to "text" rather than raising, since a
    malformed body is `transform_body`'s problem to reject, not this one's."""
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "text"
    if not isinstance(data, dict):
        return "text"
    return "voice" if (data.get("modality") or "").strip().lower() == "voice" else "text"


def _sniff_client_turn_id(raw_body: bytes) -> Optional[str]:
    """Best-effort read of the request's opaque `client_turn_id` field
    (#611 review), the same way `_sniff_modality` reads `modality` — off
    the raw pre-transform body, generic across whatever backend uses this
    router. `None` for anything missing, wrong-typed, or unparseable;
    length/character validation already happened at the native
    `AskStreamRequest` layer (this is Hermes's own copy of that same
    request), so this is a plain read, not re-validation."""
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    client_turn_id = data.get("client_turn_id")
    return client_turn_id if isinstance(client_turn_id, str) and client_turn_id else None


def make_backend_router(
    *, prefix, tag, backend_label, url_attr, token_attr, client_factory,
    transform_body: Optional[Callable[[bytes], bytes]] = None,
    make_observer: Optional[Callable[[bytes], Optional[object]]] = None,
    pre_send: Optional[Callable[[bytes], list]] = None,
    probe_reachability: bool = False,
):
    """Build a `status` + `ask/stream` reverse-proxy router for a text backend.

    Originally the Agent proxy's own routes (`agent_proxy.py`, #361); pulled out
    here so a second backend (Hermes, #587) can mount the identical status/503/502
    and bearer-injection behavior without duplicating it. `url_attr`/`token_attr`
    are `settings` field names, read fresh on every request (so tests that
    monkeypatch `settings.<url_attr>` take effect with no extra wiring).
    `client_factory` is the caller's own `_client()` seam — passed in (rather
    than imported) so each backend module keeps an independently-monkeypatchable
    `_client`.

    `transform_body` (#590) lets a caller rewrite the JSON body before it's
    forwarded — the Hermes route uses it to attach the `lifeos_context`
    envelope. Its absence (the Agent route's default) keeps the body an
    unbuffered `request.stream()`, exactly as before; supplying it buffers the
    body via `request.body()` so it can be parsed, rewritten, and re-serialized.
    It may raise `HTTPException` (e.g. a 400 for a bad persona) — that happens
    before `client_factory()` runs, so a rejected turn never reaches the
    backend.

    `make_observer` (#592) is a read-only tee on the relayed response: called
    once per request with the same raw, pre-transform body `transform_body`
    receives, it returns either `None` (nothing to observe) or an object with
    `observe(chunk: bytes) -> None` and `finalize() -> None`. `observe()` is
    called with a copy of each chunk *before* that chunk is yielded to the
    browser, and must never alter it — the chunk object handed to `yield` is
    always the untouched original, so the browser's bytes are unaffected.
    Calling `observe()` first (rather than after, as an earlier version of
    this hook did) matters for an early client disconnect: closing this
    generator raises `GeneratorExit` at the point it's currently suspended
    (the `yield`), which skips any code written *after* that yield in the
    same loop iteration — so a chunk already handed to the browser could be
    lost to the tee. Observing before the yield means it's captured
    regardless of what happens after. `observe()` itself must be
    non-blocking and in-memory-only (the implementation's job, not this
    contract's) — nothing here awaits it or bounds its cost, so any I/O
    inside it would stall this relay loop, and with it every other chunk
    still queued behind it, before the browser sees the next byte.
    `finalize()` always runs when the relay ends, including an early client
    disconnect, so partial content is still handed off — this is the place
    a slower operation (e.g. a store write) belongs, since by the time it
    runs there is no further byte left to delay. Its absence (the Agent
    route's default) leaves the relay loop exactly as it was before this
    parameter existed. Sharing `transform_body`'s "only buffer when actually
    needed" gate means Hermes (which already buffers for the envelope)
    doesn't pay for a second `request.body()` read, and Agent (neither hook
    set) still never buffers.

    `make_observer` being set ALSO gates a second behavior (#611): the
    upstream drain runs as a registry-owned background pump
    (`api/services/chat_turns.py`) rather than the browser-facing generator
    itself, so a disconnect no longer aborts it mid-relay — the pump keeps
    draining upstream and calls `observer.observe()`/`finalize()` exactly as
    before, now from its own `finally` instead of the response generator's.
    The client-visible bytes and their order are unaffected either way: the
    browser reads from a `ChatTurn.reader()` that receives the identical
    sequence of chunks the plain `relay()` below would have yielded it. This
    is why Agent (no observer, ever) is untouched by #611 — detaching a
    relay nothing persists would only spend money with nothing to show for
    it — and why an observer that returns `None` for a given request (a
    `make_observer` configured but declining to observe this one) falls
    through to the plain, still-client-tied `relay()` for that request.

    `pre_send` (#685) runs a side-effecting precondition — the journal
    persona's deterministic capture is the motivating case — before the
    backend is contacted at all. Called once per request with the same raw,
    pre-transform body the other two hooks receive, AFTER `transform_body`/
    `make_observer` but BEFORE the upstream request is built: it may raise
    `HTTPException` (e.g. a capture write failure) and that happens before a
    single byte reaches the backend, exactly like `transform_body`'s own
    HTTPException case above — no false success is possible, and whatever
    "this delivery already happened" bookkeeping the caller does downstream
    (e.g. an idempotency key) stays unclaimed. On success it returns a list
    of frames (of whatever type `emit()`/`yield` already accept elsewhere in
    this module — an SSE `data: ...` string, in practice) to hand the
    browser BEFORE any backend byte. An empty list — the default for every
    request this hook doesn't apply to — changes nothing; its absence (every
    other backend) leaves this router exactly as it was before this
    parameter existed.

    A non-empty return ALSO changes how this request's backend call itself
    is handled (#685 adversarial-review follow-up): a caller like
    `chat_via_api()` (api/services/telegram.py) or the ring ingest
    (api/routes/journal_ingest.py) treats a non-200 response as "nothing
    happened" and never inspects its body, so if the backend call could
    still turn into an HTTP-level 502 or a passed-through non-200 status,
    proof that already reached disk (or wherever `pre_send`'s side effect
    landed) would be silently discarded along with it. So once there ARE
    prelude frames, `client_factory()`/the upstream call is made only AFTER
    they're already queued for the browser, the response is unconditionally
    a 200 SSE stream, and a connect failure or a non-200 upstream response
    becomes an in-stream `{"type": "error", "message": ...}` event — the
    same shape `api/routes/chat.py`'s native path already emits and
    `chat_via_api()` already folds into its answer — rather than an
    exception or a forwarded status code. This is why the plain path below
    builds the upstream request unconditionally before deciding how to
    relay it: that ordering is only wrong when there's a precondition's
    proof to protect, i.e. only when `pre_send` is set AND actually returned
    something for this request. Every other request (no `pre_send`, or one
    that returns `[]` for this particular request — e.g. every non-journal
    Hermes turn) takes the plain path, completely unchanged.

    `probe_reachability` (#688) opts `/status` into a cached reachability
    probe (see `_probe_reachable`) rather than reporting configuration alone.
    Default False leaves a caller (the Agent backend, as of this writing)
    byte-identical to before this parameter existed: `available` is exactly
    `bool(getattr(settings, url_attr))`, no network call. True (Hermes) makes
    `available` true only when the backend is BOTH configured and reachable,
    and adds `configured`/`reachable` fields so a caller can distinguish "not
    set up" from "set up but down" — `available` alone collapsed those into
    one silent-failure mode.
    """
    router = APIRouter(prefix=prefix, tags=[tag])

    @router.get("/status")
    async def status():
        """Whether this text backend is configured (and, if
        `probe_reachability`, reachable) — drives the UI selector."""
        base_url = getattr(settings, url_attr)
        configured = bool(base_url)
        if probe_reachability and configured:
            reachable = await _probe_reachable(client_factory, base_url, backend_label)
        else:
            reachable = configured
        return {
            "available": configured and reachable,
            "configured": configured,
            "reachable": reachable,
        }

    @router.post("/ask/stream")
    async def ask_stream(request: Request):
        """Proxy a text turn to the backend's /api/ask/stream, streaming SSE."""
        base_url = getattr(settings, url_attr)
        if not base_url:
            raise HTTPException(status_code=503, detail=f"{backend_label} backend not configured")

        url = f"{base_url.rstrip('/')}/api/ask/stream"
        # filter_headers() strips any inbound Authorization (and hop-by-hop); the
        # bearer is added server-side below, so a client can never inject it.
        headers = filter_headers(request.headers)
        token = getattr(settings, token_attr)
        if token:
            headers["authorization"] = f"Bearer {token}"

        # Buffer + rewrite the body only when the caller asked for it (Hermes);
        # otherwise stream straight through unbuffered (Agent), unchanged from
        # before #590. transform_body may raise HTTPException (e.g. a bad
        # persona) — that happens before client_factory(), so nothing is sent.
        # make_observer (#592) and pre_send (#685) share this same raw body
        # rather than a second/third request.body() read.
        raw_body = await request.body() if (transform_body or make_observer or pre_send) else None
        body = transform_body(raw_body) if transform_body else request.stream()
        observer = make_observer(raw_body) if make_observer else None
        # pre_send may itself raise HTTPException (e.g. a journal capture
        # write failure) — like transform_body's, that happens before
        # client_factory() below, so the backend is never contacted.
        prelude_frames = pre_send(raw_body) if pre_send else []

        client = client_factory()

        if prelude_frames:
            # #685 adversarial-review follow-up — see make_backend_router's
            # docstring on `pre_send` for the full reasoning. Short version:
            # a precondition's proof (journal capture) has already succeeded
            # by the time there are frames here, and that proof must reach
            # the caller no matter what the backend then does, so the
            # backend call is made INSIDE the turn (after the frames are
            # already queued) and its failure becomes an in-stream event
            # rather than an HTTP-level 502/passed-through status.
            #
            # Reuses the same turn-registry/detached-pump machinery (#611)
            # the observer branch below uses, so a journal turn survives a
            # client disconnect and is cancellable the same way any other
            # turn is.
            client_turn_id = _sniff_client_turn_id(raw_body)
            if client_turn_id:
                get_turn_registry().cancel_by_client_turn_id(client_turn_id)
            turn = get_turn_registry().create(
                conversation_id=None,
                modality=_sniff_modality(raw_body),
                client_turn_id=client_turn_id,
            )
            if hasattr(observer, "bind_turn"):
                observer.bind_turn(turn)

            async def _pump_guaranteed():
                try:
                    for frame in prelude_frames:
                        await turn.emit(frame)
                    try:
                        upstream_req = client.build_request(
                            "POST", url, headers=headers, content=body,
                        )
                        upstream = await client.send(upstream_req, stream=True)
                    except (httpx.RequestError, httpx.InvalidURL) as exc:
                        logger.warning(
                            "%s backend request failed after a precondition "
                            "already succeeded: %s", backend_label, exc,
                        )
                        await turn.emit(
                            "data: " + json.dumps({
                                "type": "error",
                                "message": f"{backend_label} backend unreachable: {exc}",
                            }) + "\n\n"
                        )
                        return
                    if upstream.status_code >= 400:
                        logger.warning(
                            "%s backend returned HTTP %s after a precondition "
                            "already succeeded", backend_label, upstream.status_code,
                        )
                        await turn.emit(
                            "data: " + json.dumps({
                                "type": "error",
                                "message": f"{backend_label} backend returned HTTP {upstream.status_code}",
                            }) + "\n\n"
                        )
                        await upstream.aclose()
                        return
                    try:
                        async for chunk in upstream.aiter_raw():
                            if observer is not None:
                                observer.observe(chunk)
                            await turn.emit(chunk)
                    finally:
                        await upstream.aclose()
                finally:
                    if observer is not None:
                        observer.finalize()
                    await client.aclose()
                    get_turn_registry().pop(turn)
                    await turn.close()

            turn.task = asyncio.create_task(_pump_guaranteed())
            return StreamingResponse(
                turn.reader(), status_code=200, media_type="text/event-stream",
            )

        try:
            upstream_req = client.build_request(
                "POST", url, headers=headers, content=body,
            )
            upstream = await client.send(upstream_req, stream=True)
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            await client.aclose()
            logger.warning("%s backend request failed: %s", backend_label, exc)
            raise HTTPException(status_code=502, detail=f"{backend_label} backend unreachable: {exc}")

        if observer is not None:
            # Registry-owned pump (#611): gated on `observer` (not just
            # `make_observer` being configured) — this is the ONLY backend
            # with something to persist for THIS request; if `make_observer`
            # returned None (e.g. an unparseable body it chose not to raise
            # on) there's nothing to gain from detaching, so that case falls
            # through to the plain `relay()` below unchanged. The Agent
            # backend never sets `make_observer` at all, so `observer` is
            # always None there and it always takes the plain path —
            # detaching would only spend money on a relay nothing observes.
            # Supersede (#611 review): a reused client_turn_id cancels
            # whatever turn is still holding it, same as the native path —
            # a stale/duplicate key resolves to the newest claimant, never
            # a stray old one.
            client_turn_id = _sniff_client_turn_id(raw_body)
            if client_turn_id:
                get_turn_registry().cancel_by_client_turn_id(client_turn_id)
            turn = get_turn_registry().create(
                conversation_id=None,
                modality=_sniff_modality(raw_body),
                client_turn_id=client_turn_id,
            )
            if hasattr(observer, "bind_turn"):
                observer.bind_turn(turn)

            async def _pump():
                try:
                    async for chunk in upstream.aiter_raw():
                        # Observe before emitting (#592 review, preserved) —
                        # see make_backend_router's docstring above for why
                        # the order matters for an early client disconnect.
                        observer.observe(chunk)
                        await turn.emit(chunk)
                finally:
                    observer.finalize()
                    await upstream.aclose()
                    await client.aclose()
                    get_turn_registry().pop(turn)
                    await turn.close()

            turn.task = asyncio.create_task(_pump())
            return StreamingResponse(
                turn.reader(),
                status_code=upstream.status_code,
                headers=filter_headers(upstream.headers),
                media_type=upstream.headers.get("content-type"),
            )

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    # Observe before yielding (#592 review) — see
                    # make_backend_router's docstring for why the order
                    # matters for an early client disconnect.
                    if observer is not None:
                        observer.observe(chunk)
                    yield chunk
            finally:
                if observer is not None:
                    observer.finalize()
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            headers=filter_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )

    return router
