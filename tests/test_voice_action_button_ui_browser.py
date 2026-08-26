"""Browser tests for the `?record=1` Action Button deep-link param (#731).

An iPhone Action Button routes through Shortcuts, which can only open a URL
-- so `/chat` accepts `?record=1` (alongside `?mode=voice`) to begin an
actual recording on page load, the same code path a manual tap on the talk
button uses, rather than only arming Listening's wake-word mic hold as
`?mode=voice` alone does. `resolveExplicitVoiceMode()`/`?mode=voice` were
covered before this by tests/test_voice_mic_block_ui_browser.py and friends;
this file is about the new `maybeAutoStartRecording()` in `web/chat/voice.js`
specifically:

- `?mode=voice&record=1` together actually starts recording, unprompted.
- `?mode=voice` alone never does (the pre-existing, unchanged behavior).
- `?record=1` alone (no voice mode) never does either -- the param has no
  effect on its own.
- A blocked mic (insecure context, here) fails closed: the same
  `micBlockReason()` message a manual tap would get, not a silent hang.

Like tests/test_voice_mic_block_ui_browser.py and
tests/test_voice_manual_stop_auto_mode_ui_browser.py, this serves `web/`
itself from an ephemeral port with a faked (but genuinely live) getUserMedia
stream, so it runs at pre-push (`browser and not requires_server`).
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


# A real (silent) live MediaStream from WebAudio, which MediaRecorder accepts
# -- same trick tests/test_voice_manual_stop_auto_mode_ui_browser.py uses,
# since a headless browser has no real microphone. Counts calls so a test can
# assert recording started with *no* explicit tap.
_FAKE_GUM_SCRIPT = """
window.__gumCalls = 0;
navigator.mediaDevices.getUserMedia = function () {
  window.__gumCalls += 1;
  var ctx = new (window.AudioContext || window.webkitAudioContext)();
  var dest = ctx.createMediaStreamDestination();
  return Promise.resolve(dest.stream);
};
"""

# Only what micBlockReason() inspects first -- an insecure context is
# detected before getUserMedia is ever reached, so the fake stream above is
# irrelevant to this scenario (mirrors tests/test_voice_mic_block_ui_browser.py).
_INSECURE_SCRIPT = """
Object.defineProperty(window, 'isSecureContext', { value: false });
"""


def _dock_baseline(*, listen=False, auto=False):
    """Pins an explicit dock baseline so the shipped defaults (Listening/Auto
    on) don't open their own unrelated mic hold and confound the assertions
    below -- mirrors the same helper in
    tests/test_voice_manual_stop_auto_mode_ui_browser.py."""
    settings = {"mute": False, "auto": auto, "fast": False, "listen": listen}
    return (
        "try { window.localStorage.setItem('lifeos:chat:dock_settings', "
        + json.dumps(json.dumps(settings))
        + "); } catch (e) {}"
    )


def _open(page: Page, base_url, path, *, extra_script=None):
    page.add_init_script(_dock_baseline())
    if extra_script:
        page.add_init_script(extra_script)
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({})))
    page.goto(f"{base_url}{path}")
    # state="attached", not the default "visible": in the record=1-alone
    # scenario voice mode never engages, so the dock (and this button) stays
    # hidden by the same CSS that hides it in ordinary text mode.
    page.wait_for_selector("#voiceTalkBtn", state="attached")


def _is_recording(page: Page):
    return page.evaluate(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


class TestRecordParamStartsRecording:
    def test_mode_voice_and_record_1_starts_recording_unprompted(self, page: Page, chat_base_url):
        _open(page, chat_base_url, "/chat?mode=voice&record=1", extra_script=_FAKE_GUM_SCRIPT)
        page.wait_for_function("document.getElementById('voiceTalkBtn').classList.contains('recording')")
        assert _is_recording(page)
        assert page.evaluate("window.__gumCalls") >= 1


class TestRecordParamNeverImpliedOrStandalone:
    def test_mode_voice_alone_does_not_start_recording(self, page: Page, chat_base_url):
        _open(page, chat_base_url, "/chat?mode=voice", extra_script=_FAKE_GUM_SCRIPT)
        page.wait_for_timeout(300)  # let any async init settle
        assert not _is_recording(page)

    def test_record_1_alone_without_voice_mode_does_not_start_recording(self, page: Page, chat_base_url):
        _open(page, chat_base_url, "/chat?record=1", extra_script=_FAKE_GUM_SCRIPT)
        page.wait_for_timeout(300)
        assert not _is_recording(page)
        assert page.evaluate("window.__gumCalls") == 0


class TestRecordParamFailsClosed:
    def test_blocked_mic_reports_the_normal_message_not_a_silent_hang(self, page: Page, chat_base_url):
        _open(page, chat_base_url, "/chat?mode=voice&record=1", extra_script=_INSECURE_SCRIPT)
        expect(page.locator("#statusText")).to_have_text("Mic blocked — this page is not on HTTPS")
        expect(page.locator(".message.assistant")).to_contain_text("not on HTTPS")
        assert not _is_recording(page)
