"""Shared helpers for the streaming reverse proxies (voice, agent, hermes) —
#361, generalized in #587.

Keeps the security-relevant header-filtering and timeout in one place so the
voice, agent, and hermes proxies stay in lockstep.
"""

import logging
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings

logger = logging.getLogger(__name__)

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


def make_backend_router(
    *, prefix, tag, backend_label, url_attr, token_attr, client_factory,
    transform_body: Optional[Callable[[bytes], bytes]] = None,
    make_observer: Optional[Callable[[bytes], Optional[object]]] = None,
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
    """
    router = APIRouter(prefix=prefix, tags=[tag])

    @router.get("/status")
    async def status():
        """Whether this text backend is configured (drives the UI selector)."""
        return {"available": bool(getattr(settings, url_attr))}

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
        # make_observer (#592) shares this same raw body rather than a second
        # request.body() read.
        raw_body = await request.body() if (transform_body or make_observer) else None
        body = transform_body(raw_body) if transform_body else request.stream()
        observer = make_observer(raw_body) if make_observer else None

        client = client_factory()
        try:
            upstream_req = client.build_request(
                "POST", url, headers=headers, content=body,
            )
            upstream = await client.send(upstream_req, stream=True)
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            await client.aclose()
            logger.warning("%s backend request failed: %s", backend_label, exc)
            raise HTTPException(status_code=502, detail=f"{backend_label} backend unreachable: {exc}")

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
