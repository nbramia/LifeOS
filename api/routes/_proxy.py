"""Shared helpers for the streaming reverse proxies (voice, agent) — #361.

Keeps the security-relevant header-filtering and timeout in one place so the
voice and agent proxies stay in lockstep.
"""

import httpx

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
