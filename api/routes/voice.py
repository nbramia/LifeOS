"""Reverse proxy for the voice gateway (whisper-relay) — #361.

LifeOS owns the unified `/chat` client; voice *transport* lives in the separate
whisper-relay app (STT → backend → TTS). We reverse-proxy ``/api/voice/*`` to
``LIFEOS_VOICE_GATEWAY_URL`` so the browser stays same-origin: one HTTPS/Tailscale
front, one mic permission, no CORS. See ADR-006 and
``docs/specs/technical/client-surfaces.md``.

LifeOS adds **no** voice logic here — it only forwards the request/response,
streaming both directions so SSE turn events and audio clips pass through
unbuffered. The gateway is trusted (localhost); LifeOS is the access-control
front, consistent with the rest of the API's localhost/Tailscale trust model.
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config.settings import settings

router = APIRouter(prefix="/api/voice", tags=["voice"])

# Hop-by-hop headers are connection-specific and must not be forwarded by a proxy
# (RFC 7230 §6.1). Content-Length is dropped too; httpx/Starlette recompute it.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer",
}

# Voice turns run STT → LLM → TTS and can take a while; match whisper-relay's
# generous read budget so long turns aren't cut off.
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=5.0)


def _filter_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _client() -> httpx.AsyncClient:
    """The httpx client used to reach the gateway (a seam for tests)."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def voice_proxy(path: str, request: Request):
    """Forward any ``/api/voice/<path>`` request to the voice gateway, streaming."""
    # Defense-in-depth: never let a crafted path escape the /api/voice/ prefix on
    # the upstream (the gateway exposes only voice/health routes anyway).
    if ".." in path:
        return JSONResponse(status_code=400, content={"error": "invalid path"})

    base = settings.voice_gateway_url.rstrip("/")
    url = f"{base}/api/voice/{path}"

    # Audio uploads are bounded by the gateway (<=25 MB), so reading the body is
    # safe; this also lets httpx set a correct Content-Length for the multipart.
    body = await request.body()

    client = _client()
    try:
        upstream_req = client.build_request(
            request.method,
            url,
            params=request.query_params,
            headers=_filter_headers(request.headers),
            content=body,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"error": "voice gateway unreachable", "detail": str(exc)},
        )

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
