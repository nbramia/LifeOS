"""Browser tests for the session-cost display distinguishing an unpriced
turn from a real zero-cost turn (#602).

`web/chat/ask-stream.js`'s `data.type === 'usage'` handler used to do
`state.sessionCost += data.cost_usd || 0` -- a fallback that treats an
absent/non-numeric `cost_usd` exactly like a real `0`. A backend that
genuinely can't price a turn sends no `cost_usd` at all (rather than
inventing a zero), so that fallback silently turned "unknown" into a
confident (and wrong) "free". This file drives four turns through the
native `/api/ask/stream` SSE contract -- a priced turn, a real-zero turn, an
absent-cost turn, and a mixed session -- and asserts the display distinguishes
them: only a turn with an absent/non-numeric cost adds nothing AND marks the
total as a lower bound (a `~` prefix here); a real zero adds nothing and
leaves no mark.

Self-contained like tests/test_voice_mic_block_ui_browser.py and
tests/test_hermes_usage_ui_browser.py: serves web/ itself on an ephemeral
port and stubs every `/api/` call, so it needs no running API and carries no
`requires_server` marker (runs at pre-push, `browser and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _usage_sse(cost_field: str) -> bytes:
    """Build a one-turn SSE body whose `usage` event embeds `cost_field`
    verbatim (e.g. `'"cost_usd": 0.01'`, `'"cost_usd": 0'`, or omitted
    entirely) so each test controls exactly what the `cost_usd` key looks
    like on the wire."""
    usage_body = '{"type": "usage", "model": "claude-haiku-4-5", "input_tokens": 10, "output_tokens": 5'
    if cost_field:
        usage_body += ", " + cost_field
    usage_body += "}"
    return (
        b'data: {"type": "conversation_id", "conversation_id": "native-browser-1"}\n\n'
        b'data: {"type": "content", "content": "hi"}\n\n'
        b"data: " + usage_body.encode() + b"\n\n"
        b'data: {"type": "done"}\n\n'
    )


_PRICED_BODY = _usage_sse('"cost_usd": 0.01')
_ZERO_BODY = _usage_sse('"cost_usd": 0')
_ABSENT_BODY = _usage_sse("")  # no cost_usd key at all


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


class _StubApi:
    """Stubs every `/api/` call. `/api/hermes/status` and `/api/agent/status`
    both report unavailable so `initBackend()` (web/chat/backend.js) resolves
    its no-stored-preference default to `lifeos` -- the native path this file
    exercises -- and `/api/ask/stream` returns whichever body the queue holds
    next, so a test can send multiple turns with different `usage` shapes in
    sequence. Everything else (personas, conversations, ...) gets an empty
    JSON object, which every caller already treats as "nothing yet"."""

    def __init__(self, bodies):
        self._bodies = list(bodies)

    def __call__(self, route):
        url = route.request.url
        if "/api/hermes/status" in url or "/api/agent/status" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"available": False}))
        elif "/api/ask/stream" in url:
            body = self._bodies.pop(0) if self._bodies else _PRICED_BODY
            route.fulfill(status=200, content_type="text/event-stream", body=body)
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")


def _send_turn(page: Page, text: str) -> None:
    page.locator("#inputField").fill(text)
    page.locator("#inputField").press("Enter")


def _wait_settled(page: Page) -> None:
    # ask-stream.js re-enables the composer once the `done` event lands; use
    # that as the settle point before reading the cost display.
    page.wait_for_function("() => !window.lifeChat.state.isLoading")


def test_priced_turn_increments_total_unmarked(page: Page, chat_base_url):
    page.route("**/api/**", _StubApi([_PRICED_BODY]))
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#inputField")
    page.evaluate("() => window.lifeChat.backendReady")
    assert page.evaluate("() => window.lifeChat.config.backend") is None  # lifeos

    _send_turn(page, "what's 2+2?")
    _wait_settled(page)

    assert page.locator("#sessionCost").text_content() == "$0.010"
    assert page.locator("#sessionCost").get_attribute("title") in (None, "")


def test_real_zero_cost_leaves_no_mark(page: Page, chat_base_url):
    page.route("**/api/**", _StubApi([_ZERO_BODY]))
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#inputField")
    page.evaluate("() => window.lifeChat.backendReady")

    _send_turn(page, "free turn please")
    _wait_settled(page)

    # A real zero adds nothing and must be indistinguishable from "no
    # unpriced turns yet" -- no prefix, no tooltip.
    assert page.locator("#sessionCost").text_content() == "$0.000"
    assert page.locator("#sessionCost").get_attribute("title") in (None, "")


def test_absent_cost_marks_total_as_lower_bound(page: Page, chat_base_url):
    page.route("**/api/**", _StubApi([_ABSENT_BODY]))
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#inputField")
    page.evaluate("() => window.lifeChat.backendReady")

    _send_turn(page, "unpriceable model turn")
    _wait_settled(page)

    # Nothing was added (still $0.000), but the total must now read as
    # incomplete -- the whole point being that it can't be mistaken for a
    # cheaper (or free) session.
    cost_el = page.locator("#sessionCost")
    assert cost_el.text_content() == "~$0.000"
    assert "no reported cost" in (cost_el.get_attribute("title") or "")


def test_mixed_session_stays_marked_after_a_later_priced_turn(page: Page, chat_base_url):
    page.route("**/api/**", _StubApi([_ABSENT_BODY, _PRICED_BODY]))
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector("#inputField")
    page.evaluate("() => window.lifeChat.backendReady")

    _send_turn(page, "first, unpriceable")
    _wait_settled(page)
    assert page.locator("#sessionCost").text_content() == "~$0.000"

    _send_turn(page, "second, priced")
    _wait_settled(page)

    # The priced turn's cost is still added on top, but the session contains
    # an unpriced turn so the marker must persist -- a mixed session is a
    # lower bound just like an all-unpriced one.
    assert page.locator("#sessionCost").text_content() == "~$0.010"
