"""Browser test for the /chat voice mic-block diagnostics (#516).

Drives `web/chat/voice.js`'s `micBlockReason()` through the real talk button.
Each of the four preconditions (secure context, getUserMedia, MediaRecorder, a
supported mime type) used to collapse into one "Mic unavailable (HTTPS required)"
message; this pins that each now reports its own cause, and that an insecure
context offers a tappable link to the configured HTTPS origin.

Unlike the rest of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, because the assertions are about the
JS in *this* checkout and every API call the page makes is intercepted anyway.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Obviously synthetic — never a real tailnet name.
SECURE_URL = "https://your-machine.your-tailnet.ts.net"


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


def _open_voice_chat(page: Page, base_url, *, env_script, secure_url=SECURE_URL):
    """Load /chat in voice mode with the browser capabilities `env_script` fakes.

    `/api/chat/config` returns `secure_url`; every other API call the SPA makes
    on load is stubbed empty so nothing depends on a running server.
    """
    page.add_init_script(env_script)

    def handler(route):
        if "/api/chat/config" in route.request.url:
            body = {"default_voice": True, "secure_url": secure_url}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")
    page.locator("#voiceTalkBtn").click()


# `isSecureContext` is read-only and true on localhost, so each scenario
# redefines exactly the globals `micBlockReason()` inspects.
INSECURE = """
Object.defineProperty(window, 'isSecureContext', { value: false });
"""
NO_GETUSERMEDIA = """
Object.defineProperty(navigator, 'mediaDevices', { value: undefined });
"""
NO_MEDIARECORDER = """
Object.defineProperty(navigator, 'mediaDevices',
                      { value: { getUserMedia: () => Promise.resolve({}) } });
delete window.MediaRecorder;
"""
NO_MIME = """
Object.defineProperty(navigator, 'mediaDevices',
                      { value: { getUserMedia: () => Promise.resolve({}) } });
window.MediaRecorder = function () {};
window.MediaRecorder.isTypeSupported = () => false;
"""


class TestMicBlockReasons:
    """Each precondition names itself instead of blaming HTTPS."""

    def test_insecure_context_offers_https_link(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, env_script=INSECURE)
        expect(page.locator("#statusText")).to_have_text("Mic blocked — this page is not on HTTPS")
        link = page.locator(".message.assistant a.source-link")
        expect(link).to_have_text("🔒 Open over HTTPS")
        # Same page on the secure origin: path and query both preserved.
        expect(link).to_have_attribute("href", f"{SECURE_URL}/chat?mode=voice")

    def test_insecure_context_without_secure_url_has_no_link(self, page: Page, chat_base_url):
        """A fresh clone with no TAILNET_HTTPS_URL still gets an accurate message."""
        _open_voice_chat(page, chat_base_url, env_script=INSECURE, secure_url="")
        expect(page.locator("#statusText")).to_have_text("Mic blocked — this page is not on HTTPS")
        expect(page.locator(".message.assistant")).to_contain_text("not on HTTPS")
        expect(page.locator(".message.assistant a.source-link")).to_have_count(0)

    def test_missing_getusermedia_is_not_reported_as_https(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, env_script=NO_GETUSERMEDIA)
        expect(page.locator("#statusText")).to_have_text(
            "Mic unavailable — this browser exposes no microphone API")
        expect(page.locator(".message.assistant a.source-link")).to_have_count(0)

    def test_missing_mediarecorder_is_not_reported_as_https(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, env_script=NO_MEDIARECORDER)
        expect(page.locator("#statusText")).to_have_text(
            "Mic unavailable — this browser has no MediaRecorder")

    def test_unsupported_mime_is_not_reported_as_https(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, env_script=NO_MIME)
        expect(page.locator("#statusText")).to_have_text(
            "Mic unavailable — no supported audio format in this browser")
