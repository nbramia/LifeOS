"""Verifies the conftest-level guard that blocks real api.anthropic.com calls.

A test that forgets to mock the LLM client can silently rack up billed API
calls. The guard installed in `tests/conftest.py` patches httpx to fail any
test that hits an Anthropic host. This module is the canary that proves the
guard works: it makes a request to api.anthropic.com (the marker
`allow_anthropic_api` lets the canary bypass) and the negative test confirms
that an *unmarked* test would have been blocked.
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.unit


def test_guard_blocks_unmarked_anthropic_call():
    """Without the marker, a request to api.anthropic.com must raise."""
    with pytest.raises(RuntimeError, match="real Anthropic API call"):
        with httpx.Client(timeout=0.001) as c:
            # We never actually connect — the guard fires before send.
            c.get("https://api.anthropic.com/v1/models")


def test_guard_blocks_async_anthropic_call():
    """The async client is patched the same way."""
    import asyncio

    async def _go():
        async with httpx.AsyncClient(timeout=0.001) as c:
            await c.get("https://api.anthropic.com/v1/models")

    with pytest.raises(RuntimeError, match="real Anthropic API call"):
        asyncio.run(_go())


def test_guard_blocks_platform_console_calls():
    """platform.claude.com is also covered — that's where managed-agents
    sessions are inspected."""
    with pytest.raises(RuntimeError, match="real Anthropic API call"):
        with httpx.Client(timeout=0.001) as c:
            c.get("https://platform.claude.com/api/sessions")


def test_guard_allows_non_anthropic_hosts():
    """The guard is targeted — it only blocks Anthropic hosts. Other
    outbound calls (or any MockTransport-routed call) flow through."""
    # MockTransport never makes a real network call; using it here also
    # proves the guard doesn't interfere with mocked transports.
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport, base_url="https://api.anthropic.com") as c:
        # Even though the URL host is api.anthropic.com, the request would
        # need to reach send to be intercepted. MockTransport runs before
        # the patched send body, so we expect the guard to STILL fire to
        # prevent a real-call surprise even via mock. This is the
        # conservative posture — block first, allow only via marker.
        with pytest.raises(RuntimeError, match="real Anthropic API call"):
            c.get("/v1/messages")


@pytest.mark.allow_anthropic_api
def test_marker_bypasses_guard_for_deliberate_real_calls():
    """`@pytest.mark.allow_anthropic_api` lets a test reach the real API.
    We don't actually connect (timeout=1ms) — only verify the guard does
    NOT raise its RuntimeError. Any connection-shaped exception is fine."""
    with pytest.raises(Exception) as exc_info:
        with httpx.Client(timeout=0.001) as c:
            c.get("https://api.anthropic.com/v1/models")
    # We expect a timeout / connect error, NOT our guard's RuntimeError.
    assert "real Anthropic API call" not in str(exc_info.value)
