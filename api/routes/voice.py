"""Reverse proxy for the voice gateway (whisper-relay) — #361.

LifeOS owns the unified `/chat` client; voice *transport* lives in the separate
whisper-relay app (STT → backend → TTS). We reverse-proxy ``/api/voice/*`` to
``LIFEOS_VOICE_GATEWAY_URL`` so the browser stays same-origin: one HTTPS/Tailscale
front, one mic permission, no CORS. See ADR-016 and
``docs/specs/technical/client-surfaces.md``.

LifeOS adds **no** voice logic here — it only forwards the request/response,
streaming both directions so SSE turn events and audio clips pass through
unbuffered. The gateway is trusted (localhost) and its URL is server config (not
user-controllable, so no SSRF); LifeOS is the access-control front, consistent
with the rest of the API's localhost/Tailscale trust model.
"""

import logging
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings

from api.routes._proxy import TIMEOUT, filter_headers as _filter_headers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _client() -> httpx.AsyncClient:
    """The httpx client used to reach the gateway (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


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

    client = _client()
    try:
        upstream_req = client.build_request(
            request.method,
            url,
            params=request.query_params,
            headers=_filter_headers(request.headers),
            # Stream the request body upstream rather than buffering it, so a
            # large audio upload can't OOM LifeOS (the gateway enforces its own
            # size cap once it receives the stream).
            content=request.stream() if request.method == "POST" else None,
        )
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.RequestError, httpx.InvalidURL) as exc:
        await client.aclose()
        logger.warning("voice gateway request failed (%s %s): %s", request.method, path, exc)
        raise HTTPException(status_code=502, detail=f"voice gateway unreachable: {exc}")

    async def relay():
        try:
            # Raw bytes pass through unchanged (preserves any Content-Encoding,
            # flushes SSE/audio chunks as they arrive).
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=_filter_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
