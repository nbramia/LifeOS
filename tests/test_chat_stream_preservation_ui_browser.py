"""Self-contained browser regressions for partial chat stream failures.

The synthetic SSE routes model both native terminal contracts: a handled
provider failure sends content, ``error``, and ``done``; an outer stream
failure sends content and ``error`` without ``done``. Both must leave useful
content visible, show the error status, release the composer, and allow a
follow-up turn. The page and every API call are local/stubbed so this test has
no production-server or personal-data dependency.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VIEWPORTS = [
    pytest.param({"width": 1280, "height": 800}, id="desktop"),
    pytest.param({"width": 390, "height": 844}, id="mobile"),
]


class _ChatHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the chat SPA and its ES module tree from this checkout."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/chat", "/"):
            return str(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):
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


def _sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def _install_api_mocks(page: Page, state: dict):
    """Use native LifeOS routing and return a synthetic stream per question."""

    def handler(route):
        url = route.request.url
        if "/api/hermes/status" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": False}))
            return
        if "/api/agent/status" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": False}))
            return
        if "/api/ask/stream" in url:
            body = json.loads(route.request.post_data or "{}")
            question = body.get("question", "")
            state["questions"].append(question)
            if question == "synthetic handled failure":
                state["contracts"].append("content-error-done")
                stream = _sse(
                    {"type": "conversation_id", "conversation_id": "synthetic-preservation"},
                    {"type": "content", "content": "Useful handled result."},
                    {"type": "error", "message": "The request could not be completed."},
                    {"type": "done"},
                )
            elif question == "synthetic outer failure":
                state["contracts"].append("content-error")
                stream = _sse(
                    {"type": "conversation_id", "conversation_id": "synthetic-preservation"},
                    {"type": "content", "content": "Useful outer partial result."},
                    {"type": "error", "message": "The request could not be completed."},
                )
            else:
                stream = _sse(
                    {"type": "conversation_id", "conversation_id": "synthetic-preservation"},
                    {"type": "content", "content": "Synthetic follow-up succeeded."},
                    {"type": "done"},
                )
            route.fulfill(status=200, content_type="text/event-stream", body=stream)
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize(
    "failure_question,expected_content,expected_contract",
    [
        ("synthetic handled failure", "Useful handled result.", "content-error-done"),
        ("synthetic outer failure", "Useful outer partial result.", "content-error"),
    ],
)
def test_partial_stream_content_survives_error_and_followup(
    page: Page,
    chat_base_url,
    viewport,
    failure_question,
    expected_content,
    expected_contract,
):
    state = {"questions": [], "contracts": []}
    page.set_viewport_size(viewport)
    _install_api_mocks(page, state)
    page.goto(f"{chat_base_url}/chat")
    page.wait_for_selector(".welcome")

    attach = page.locator("#attachBtn")
    attach_box = attach.bounding_box()
    assert attach_box["width"] >= 44
    assert attach_box["height"] >= 44
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )

    page.locator("#inputField").fill(failure_question)
    page.locator("#sendBtn").click()
    page.wait_for_function(
        "() => document.getElementById('statusText')?.textContent === 'Error' "
        "&& !document.querySelector('.typing')",
        timeout=30000,
    )

    expect(page.locator(".message.assistant .message-content").first).to_have_text(
        expected_content
    )
    expect(page.locator("#statusText")).to_have_text("Error")
    expect(page.locator("#sendBtn")).to_be_enabled()
    expect(page.locator("#stopBtn")).not_to_have_class("visible")
    assert state["contracts"] == [expected_contract]

    page.locator("#inputField").fill("synthetic follow-up")
    page.locator("#sendBtn").click()
    expect(page.locator(".message.assistant .message-content").last).to_have_text(
        "Synthetic follow-up succeeded.", timeout=30000
    )
    expect(page.locator("#statusText")).to_have_text("Ready")
    expect(page.locator("#sendBtn")).to_be_enabled()
    expect(page.locator("#stopBtn")).not_to_have_class("visible")
