"""Browser tests for the Text|Voice input-mode pill (#684).

Drives `web/chat/voice.js`'s `setVoiceMode()` through the real toolbar pill
that replaced the old mic (🎙️)/keyboard (⌨️) icon toggle: explicit-choice
clicks, the CSS the `voice-mode` body class actually flips (dock vs.
composer), the no-op guard on a redundant click, and persistence across a
reload — same `lifeos:chat:voice_mode` sessionStorage key and resolution
order voice.js has always used.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, because the assertions are about
the JS in *this* checkout and every API call the page makes is intercepted
anyway. That is why it carries no `requires_server` marker, and so runs at
pre-push (`browser and not requires_server`) — see
tests/test_voice_mic_block_ui_browser.py for the same pattern.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

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


def _open_chat(page: Page, base_url, *, path="/chat"):
    """Every `/api/` call the page makes on load is stubbed empty so nothing
    depends on a running server — including `/api/chat/config`, which leaves
    `default_voice` unset (text mode, matching a fresh clone's default)."""
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({})))
    page.goto(f"{base_url}{path}")
    page.wait_for_selector("#modeTextBtn")


def _is_voice_mode(page: Page):
    return page.evaluate("document.body.classList.contains('voice-mode')")


class TestModePillClicks:
    """The Text|Voice pill (#684) is the sole control now wired to
    setVoiceMode() — no mic/keyboard icons remain."""

    def test_default_is_text_mode(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url)
        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option active")
        expect(page.locator("#modeVoiceBtn")).to_have_class("mode-toggle-option")
        assert _is_voice_mode(page) is False

    def test_click_voice_switches_mode_and_ui(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url)

        page.locator("#modeVoiceBtn").click()

        assert _is_voice_mode(page) is True
        expect(page.locator("#modeVoiceBtn")).to_have_class("mode-toggle-option active")
        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option")
        # body.voice-mode is what actually swaps the dock in for the composer
        # (index.html's `body.voice-mode .voice-dock` / `.input-container`
        # display rules) — assert the real observable, not just the class.
        expect(page.locator("#voiceDock")).to_be_visible()
        expect(page.locator(".input-container")).to_be_hidden()
        assert page.evaluate("window.lifeChat.config.voiceMode") is True

    def test_click_text_reverses_mode_and_ui(self, page: Page, chat_base_url):
        # ?mode=voice starts the page in voice mode without going through a
        # click first, isolating the reversal from the switch-to-voice case.
        _open_chat(page, chat_base_url, path="/chat?mode=voice")
        assert _is_voice_mode(page) is True

        page.locator("#modeTextBtn").click()

        assert _is_voice_mode(page) is False
        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option active")
        expect(page.locator("#modeVoiceBtn")).to_have_class("mode-toggle-option")
        expect(page.locator("#voiceDock")).to_be_hidden()
        expect(page.locator(".input-container")).to_be_visible()
        assert page.evaluate("window.lifeChat.config.voiceMode") is False

    def test_clicking_the_active_pill_is_a_noop(self, page: Page, chat_base_url):
        """setVoiceMode() early-returns when the target mode is already in
        effect — pin that a redundant click doesn't re-fire the transition."""
        _open_chat(page, chat_base_url)
        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option active")

        page.locator("#modeTextBtn").click()  # already active — no-op

        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option active")
        expect(page.locator("#modeVoiceBtn")).to_have_class("mode-toggle-option")
        assert _is_voice_mode(page) is False
        assert page.evaluate("window.lifeChat.config.voiceMode") is False


class TestModePillPersistence:
    """The stored preference (`lifeos:chat:voice_mode`, sessionStorage)
    survives a reload — same resolution order voice.js has always used."""

    def test_voice_choice_persists_across_reload(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url)

        page.locator("#modeVoiceBtn").click()
        assert _is_voice_mode(page) is True
        assert page.evaluate(
            "window.sessionStorage.getItem('lifeos:chat:voice_mode')") == "1"

        page.reload()
        page.wait_for_selector("#modeTextBtn")

        assert _is_voice_mode(page) is True
        expect(page.locator("#modeVoiceBtn")).to_have_class("mode-toggle-option active")
        expect(page.locator("#modeTextBtn")).to_have_class("mode-toggle-option")
        assert page.evaluate("window.lifeChat.config.voiceMode") is True
