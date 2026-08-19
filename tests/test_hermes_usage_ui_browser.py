"""Browser test for Hermes-proxied usage capture reaching the browser (#595).

Confirms the acceptance criterion that a relayed `usage` SSE event -- the
same event shape the native `/api/ask/stream` path emits -- increases the
browser's session cost display with **no change** to
`web/chat/ask-stream.js`'s existing `data.type === 'usage'` handling, when the
event arrives from the Hermes proxy endpoint instead of the native one.

Self-contained like tests/test_voice_mic_block_ui_browser.py: serves web/
itself on an ephemeral port and stubs every `/api/` call, so it needs no
running API and carries no `requires_server` marker (runs at pre-push,
`browser and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# cost_usd (0.00087) is deliberately not what Anthropic sonnet-fallback
# pricing would compute for these token counts -- if the client ever started
# recomputing cost instead of trusting the relayed event, this exact value
# would expose it.
_USAGE_SSE_BODY = (
    b'data: {"type": "conversation_id", "conversation_id": "hermes-browser-1"}\n\n'
    b'data: {"type": "content", "content": "hello from hermes"}\n\n'
    b'data: {"type": "usage", "model": "deepseek-v3-fireworks", '
    b'"input_tokens": 120, "output_tokens": 340, "cost_usd": 0.00087}\n\n'
    b'data: {"type": "done"}\n\n'
)


class _ChatHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the chat SPA the way api/main.py does: `/chat` is index.html
    and the module tree hangs off `/static/`."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/chat", "/"):
            return str(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def chat_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _stub_api(route):
    """Every `/api/` call the SPA makes is stubbed so nothing depends on a
    running server. `/api/hermes/status` reports available so `initBackend()`
    (web/chat/backend.js) resolves its no-stored-preference default to
    `hermes` -- the case this test exercises -- and the ask/stream call for
    that backend returns the usage-bearing SSE body above. Everything else
    (personas, conversations, agent status, ...) gets an empty JSON object,
    which every caller already treats as "nothing yet" rather than an error.
    """
    url = route.request.url
    if "/api/hermes/status" in url:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"available": True}))
    elif "/api/agent/status" in url:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"available": False}))
    elif "/api/hermes/ask/stream" in url:
        route.fulfill(status=200, content_type="text/event-stream", body=_USAGE_SSE_BODY)
    else:
        route.fulfill(status=200, content_type="application/json", body="{}")


def test_hermes_usage_event_updates_session_cost_via_unmodified_client(page: Page, chat_base_url):
    page.route("**/api/**", _stub_api)
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#inputField")

    # window.lifeChat.backendReady (web/chat/main.js) resolves once
    # initBackend() has picked a default backend. With no stored preference
    # and /api/hermes/status reporting available, that default is "hermes" --
    # confirm it before sending so the turn actually exercises the Hermes
    # proxy endpoint under test rather than the native one.
    page.evaluate("() => window.lifeChat.backendReady")
    assert page.evaluate("() => window.lifeChat.config.backend") == "hermes"

    page.locator("#inputField").fill("what's 2+2?")
    page.locator("#inputField").press("Enter")

    # ask-stream.js's `data.type === 'usage'` branch is untouched by #595 --
    # this is the same handler the native path already exercises, now fed by
    # the Hermes proxy's relayed event instead.
    page.wait_for_function("() => document.getElementById('sessionCost').textContent !== '$0.00'")
    assert page.locator("#sessionCost").text_content() == "$0.001"
