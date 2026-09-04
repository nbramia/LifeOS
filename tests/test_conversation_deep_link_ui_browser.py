"""Browser test for the `?conversation=<id>` deep link's URL-encoding
(round 1 review, PR #858, finding #5).

`web/chat/conversations.js`'s `loadConversation()` — the function
`main.js`'s `maybeOpenDeepLinkedConversation()` calls with the raw
`?conversation=` query value — builds its `GET /api/conversations/<id>`
fetch path by raw string interpolation. Every other conversation-id fetch
site (`ask-stream.js`, `pending-question.js`) already `encodeURIComponent`s
the id; this one didn't, so an id containing a path-meaningful character
(e.g. `/`) silently corrupted the request path instead of reaching the
conversation the operator actually meant to open.

Serves `web/` itself from an ephemeral port with every `/api/` call
intercepted, so it runs at pre-push (`browser and not requires_server`) —
same pattern as tests/test_voice_action_button_ui_browser.py and
tests/test_backend_selector_ui_browser.py.
"""
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _ChatHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the chat SPA the way api/main.py does: `/chat` is index.html and
    the module tree hangs off `/static/`."""

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


def test_conversation_deep_link_encodes_id_in_fetch_path(page: Page, chat_base_url):
    """A `?conversation=` id containing a `/` — meaningful in a URL path —
    must be percent-encoded before it reaches the fetch call. Unencoded, it
    would split the request into extra path segments instead of the single
    `/api/conversations/<id>` GET the endpoint expects."""
    raw_id = "abc/def"
    requests: list[str] = []

    def handler(route):
        url = route.request.url
        if "/api/conversations/" in url:
            requests.append(url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"title": "t", "messages": []}),
        )

    page.route("**/api/**", handler)
    page.goto(f"{chat_base_url}/chat?conversation={quote(raw_id, safe='')}")

    # main.js awaits window.lifeChat.backendReady before opening the deep
    # link, so the conversation-detail fetch doesn't fire on first paint —
    # poll for it rather than asserting immediately.
    for _ in range(100):
        if requests:
            break
        page.wait_for_timeout(50)

    assert requests, "no /api/conversations/<id> request was made"
    request_url = requests[0]
    assert request_url.endswith(f"/api/conversations/{quote(raw_id, safe='')}")
    assert not request_url.endswith(f"/api/conversations/{raw_id}")  # unencoded would 404 mid-path
