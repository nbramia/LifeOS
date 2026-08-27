"""Browser tests for eager user-transcript rendering on spoken turns (#758).

A spoken turn used to put the user's words in the thread only after the whole
SSE stream finished (the terminal `done` payload), so the thread sat empty
while the assistant thought. `web/chat/voice.js` now renders the user bubble on
the relay's `transcript` event -- emitted the moment STT lands -- matching what
the text path does at send time (askStream()).

Drives `submitTurn()` directly, the seam voice.js exports so a headless harness
can run a turn without a real mic. The fetch mock returns a genuinely
incremental `ReadableStream` body gated on a test-controlled release, so
"rendered *before* the turn completed" is an observable state rather than a
race.

Serves `web/` itself from an ephemeral port and replaces `window.fetch`
outright -- the same self-contained pattern as
tests/test_voice_backend_parity_ui_browser.py and
tests/test_voice_mic_block_ui_browser.py. No `requires_server` marker, so this
runs at pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs. Every SPA call on load resolves against a
# canned `{}`; /api/voice/turn/stream gets a streamed SSE body built from
# `window.__voiceFrames`. The literal string '__gate' in that list is a
# barrier: the stream stalls there until `window.__releaseTurn()` is called, so
# a test can assert on the thread mid-turn without racing the `done` frame.
_FETCH_MOCK = """
// Dock toggles default to auto-continue/listening ON, which makes voice.js
// re-acquire the mic once a turn finishes -- getUserMedia can't run headless,
// and the resulting "⚠️ …" bubble is noise for a test asserting on thread
// contents. Muted playback for the same reason.
try {
  window.localStorage.setItem(
    'lifeos:chat:dock_settings',
    JSON.stringify({ mute: true, auto: false, fast: false, listen: false })
  );
} catch (e) { /* storage unavailable — the assertions below will say so */ }
window.__voiceFrames = [
  { type: 'started', turn_id: 'turn-1' },
  { type: 'transcript', text: 'remind me to call mom' },
  '__gate',
  { type: 'done', data: { transcript: 'remind me to call mom', response_text: 'ok' } },
];
window.__gateOpen = false;
window.__releaseTurn = function () { window.__gateOpen = true; };
(function () {
  window.fetch = function (url, opts) {
    var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
    var signal = opts && opts.signal;
    function abortError() { return new DOMException('Turn cancelled', 'AbortError'); }
    if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
      var frames = window.__voiceFrames;
      // The signal is honored so a cancelled turn tears the stream down the way
      // a real fetch would, instead of leaving a zombie turn to finish later.
      function waitForGate() {
        return new Promise(function (resolve, reject) {
          (function poll() {
            if (signal && signal.aborted) return reject(abortError());
            if (window.__gateOpen) return resolve();
            setTimeout(poll, 10);
          })();
        });
      }
      var body = new ReadableStream({
        start: function (controller) {
          var enc = new TextEncoder();
          var i = 0;
          (function next() {
            if (signal && signal.aborted) return controller.error(abortError());
            if (i >= frames.length) return controller.close();
            var frame = frames[i++];
            if (frame === '__gate') {
              return waitForGate().then(next, function (e) { controller.error(e); });
            }
            controller.enqueue(enc.encode('data: ' + JSON.stringify(frame) + '\\n\\n'));
            // A real macrotask between frames, so the client observably
            // processes the transcript frame before the next one arrives.
            setTimeout(next, 10);
          })();
        },
      });
      return Promise.resolve(new Response(body, {
        status: 200, headers: { 'Content-Type': 'text/event-stream' },
      }));
    }
    return Promise.resolve(new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  };
})();
"""


def _wait_for_backend_ready(page: Page):
    """`config.backend` starts null (the same value the lifeos default resolves
    to), so only awaiting initBackend()'s stashed promise proves resolution ran."""
    page.evaluate(
        "async () => {"
        "  while (!(window.lifeChat && window.lifeChat.backendReady)) {"
        "    await new Promise((r) => setTimeout(r, 10));"
        "  }"
        "  await window.lifeChat.backendReady;"
        "}"
    )


def _open_voice_chat(page: Page, base_url, frames=None):
    page.add_init_script(_FETCH_MOCK)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")
    _wait_for_backend_ready(page)
    if frames is not None:
        page.evaluate(
            "(f) => { window.__voiceFrames = JSON.parse(f); }", json.dumps(frames)
        )


def _fire_turn(page: Page, **kwargs):
    """Starts submitTurn() without awaiting it, so the mid-turn thread can be
    inspected. Playwright only awaits a *returned* promise."""
    page.evaluate("(a) => { window.lifeChatVoice.submitTurn(a); }", kwargs)


def _release(page: Page):
    page.evaluate("() => window.__releaseTurn()")


def _thread(page: Page):
    """The thread as (class, text) pairs, in DOM order -- enough to assert both
    ordering and de-duplication."""
    return page.evaluate(
        "() => [...document.querySelectorAll('#messages .message')].map("
        "  (m) => [m.className, (m.querySelector('.message-content')||{}).textContent || ''])"
    )


def _user_texts(page: Page):
    return [text for cls, text in _thread(page) if "user" in cls]


class TestEagerTranscriptRendering:
    """AC: the user's transcribed message lands in the thread as soon as the
    turn is submitted, not only once the assistant's response completes."""

    def test_transcript_event_renders_user_bubble_before_the_reply(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _fire_turn(page, blob=None)
        # The turn is still in flight (gate closed, no `done` frame yet).
        expect(page.locator("#messages .message.user")).to_have_text("remind me to call mom")
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        # ...and the assistant side is still just the typing placeholder.
        expect(page.locator("#messages .message.assistant .typing")).to_have_count(1)
        _release(page)
        expect(page.locator("#messages .message.assistant")).to_have_text("ok")

    def test_user_bubble_precedes_the_thinking_placeholder(self, page: Page, chat_base_url):
        """showThinking() runs before the transcript is known, so the eager
        bubble has to be inserted ahead of the placeholder, not after it."""
        _open_voice_chat(page, chat_base_url)
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_count(1)
        classes = [cls for cls, _ in _thread(page)]
        assert "user" in classes[0], classes
        assert "assistant" in classes[1], classes
        _release(page)

    def test_caller_supplied_transcript_renders_at_submit(self, page: Page, chat_base_url):
        """A transcript handed to submitTurn() needs no STT round trip, so it
        should be in the thread before the request even resolves."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=["__gate", {"type": "done", "data": {"transcript": "hi", "response_text": "ok"}}],
        )
        _fire_turn(page, transcript="hi")
        expect(page.locator("#messages .message.user")).to_have_text("hi")
        _release(page)
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _user_texts(page) == ["hi"]

    def test_done_does_not_duplicate_the_streamed_bubble(self, page: Page, chat_base_url):
        """`done` repeats the transcript; it must reconcile the existing bubble
        rather than append a second one."""
        _open_voice_chat(page, chat_base_url)
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_count(1)
        _release(page)
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _user_texts(page) == ["remind me to call mom"]

    def test_done_transcript_wins_when_it_revises_the_streamed_one(self, page: Page, chat_base_url):
        """`done` stays authoritative (client-surfaces.md), so a revised
        transcript updates the bubble in place."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=[
                {"type": "transcript", "text": "call mom"},
                "__gate",
                {"type": "done", "data": {"transcript": "call Mom back", "response_text": "ok"}},
            ],
        )
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_text("call mom")
        _release(page)
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _user_texts(page) == ["call Mom back"]

    def test_turn_without_a_transcript_renders_no_user_bubble(self, page: Page, chat_base_url):
        _open_voice_chat(
            page,
            chat_base_url,
            frames=["__gate", {"type": "done", "data": {"response_text": "ok"}}],
        )
        _fire_turn(page, blob=None)
        _release(page)
        expect(page.locator("#messages .message.assistant")).to_have_text("ok")
        assert _user_texts(page) == []

    def test_cancelling_the_turn_removes_the_user_bubble(self, page: Page, chat_base_url):
        """A cancelled turn is never persisted, so it leaves no trace -- same as
        before the eager render existed."""
        _open_voice_chat(page, chat_base_url)
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_count(1)
        page.locator("#voiceCancelBtn").click()
        expect(page.locator("#messages .message.user")).to_have_count(0)
        expect(page.locator("#statusText")).to_have_text("Ready")
        # The aborted stream must not resurrect the turn once the gate opens.
        _release(page)
        page.wait_for_timeout(200)
        assert _thread(page) == []

    def test_server_cancelled_frame_removes_the_bubble_without_local_cancel(
            self, page: Page, chat_base_url):
        """A turn's lifetime is server-owned (#611) -- it can be cancelled from
        elsewhere (another tab/device on the same conversation), not only via
        this tab's own Cancel button. The eagerly-rendered bubble must not
        survive that path either, even though this tab's activeTurnAbort was
        never triggered locally."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=[
                {"type": "transcript", "text": "remind me to call mom"},
                {"type": "cancelled"},
            ],
        )
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_count(1)
        page.wait_for_function(
            "document.querySelectorAll('#messages .message.user').length === 0"
        )

    def test_failed_turn_keeps_the_user_bubble(self, page: Page, chat_base_url):
        """A turn that errors out is not a cancellation -- the user really did
        say this, so the bubble stays (matching askStream()'s text-path
        behavior on error: the user's own message is never retracted, only
        the assistant side reports the failure)."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=[{"type": "transcript", "text": "remind me to call mom"}],
        )
        _fire_turn(page, blob=None)
        expect(page.locator("#messages .message.user")).to_have_text("remind me to call mom")
        expect(page.locator("#statusText")).to_have_text("Error")
        expect(page.locator("#messages .message.assistant")).to_be_visible()
        assert _user_texts(page) == ["remind me to call mom"]

    def test_cancel_after_done_stops_playback_without_removing_the_bubble(
            self, page: Page, chat_base_url):
        """The Cancel button doubles as "stop playback" once a reply has
        already landed -- voiceBusy/clipInFlight stay true across
        `await playbackChain` in submitTurn(), so a tap there routes through
        the same cancelActiveTurn() a mid-turn cancel does. That tap must
        stop audio, not delete the transcript bubble for a turn that already
        completed and persisted server-side (distinguished via the `turnDone`
        flag, set once `done` is processed)."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=[
                {"type": "transcript", "text": "remind me to call mom"},
                {"type": "done", "data": {"transcript": "remind me to call mom", "response_text": "ok"}},
            ],
        )
        page.evaluate("() => window.lifeChatVoice.submitTurn({ blob: null })")
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _user_texts(page) == ["remind me to call mom"]

        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")

        assert _user_texts(page) == ["remind me to call mom"]

    def test_a_second_turn_keeps_the_first_turns_bubble(self, page: Page, chat_base_url):
        """The per-turn bubble handle is reset at submit, so turn two appends
        rather than overwriting turn one."""
        _open_voice_chat(
            page,
            chat_base_url,
            frames=[
                {"type": "transcript", "text": "first"},
                {"type": "done", "data": {"transcript": "first", "response_text": "ok"}},
            ],
        )
        page.evaluate("() => window.lifeChatVoice.submitTurn({ blob: null })")
        page.evaluate(
            "(f) => { window.__voiceFrames = JSON.parse(f); }",
            json.dumps([
                {"type": "transcript", "text": "second"},
                {"type": "done", "data": {"transcript": "second", "response_text": "ok"}},
            ]),
        )
        page.evaluate("() => window.lifeChatVoice.submitTurn({ blob: null })")
        assert _user_texts(page) == ["first", "second"]
