"""Agent text-backend proxy (#361).

The `/chat` "Agent" backend talks to the OpenClaw voice-adapter, which speaks the
same `/api/ask/stream` SSE contract as LifeOS but is reached at
``LIFEOS_AGENT_BACKEND_URL`` and may require a bearer token. LifeOS proxies it at
``POST /api/agent/ask/stream``, **adding the token server-side** so it never
reaches the browser. The browser stays same-origin and treats the response
exactly like a normal ask/stream (minus handoff — the agent backend has none).
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import settings

from api.routes._proxy import TIMEOUT, filter_headers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _client() -> httpx.AsyncClient:
    """httpx client for the agent backend (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


@router.get("/status")
async def agent_status():
    """Whether the Agent text backend is configured (drives the UI toggle)."""
    return {"available": bool(settings.agent_backend_url)}


@router.post("/ask/stream")
async def agent_ask_stream(request: Request):
    """Proxy a text turn to the agent backend's /api/ask/stream, streaming SSE."""
    if not settings.agent_backend_url:
        raise HTTPException(status_code=503, detail="agent backend not configured")

    url = f"{settings.agent_backend_url.rstrip('/')}/api/ask/stream"
    # filter_headers() strips any inbound Authorization (and hop-by-hop); the
    # bearer is added server-side below, so a client can never inject it.
    headers = filter_headers(request.headers)
    if settings.agent_backend_token:
        headers["authorization"] = f"Bearer {settings.agent_backend_token}"

    client = _client()
    try:
        upstream_req = client.build_request(
            "POST", url, headers=headers, content=request.stream(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.RequestError, httpx.InvalidURL) as exc:
        await client.aclose()
        logger.warning("agent backend request failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"agent backend unreachable: {exc}")

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=filter_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
