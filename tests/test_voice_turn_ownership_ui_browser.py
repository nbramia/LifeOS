"""Browser tests for turn-ownership across overlapping voice turns (#832).

`submitTurn()`'s cleanup (the `finally`, the AbortError branch, mid-stream-
drop recovery, terminal failure) and the SSE handlers in `consumeTurnStream()`
all touch module-level state shared across turns -- `activeTurnId`,
`activeTurnAbort`, `voiceBusy`, `state.isLoading`, the thinking placeholder,
the user-transcript bubble, the Cancel button, the status text. A turn whose
own async work (network retry, SSE stream, mid-stream recovery poll) settles
*after* a newer turn has already started must not touch any of that -- the
newer turn now owns it. `isOwnTurn()` (an identity check against the settling
turn's own captured `AbortController`, the pattern #827 introduced for one
branch and #832 generalized to every exit path) is what prevents it.

Drives `submitTurn()`/`cancelActiveTurn()` directly (the seam voice.js
exports so a headless harness can run turns without a real mic -- same
pattern as tests/test_voice_transcript_ui_browser.py). Two turns are put in
flight at once by firing `submitTurn()` twice without ever cancelling the
first -- a real trigger per #832's own issue (a slow server response, or a
double Retry tap, reconciling after the user has already moved on) -- and
each gets its own independently-gated SSE stream via `window.__turnQueue`,
so "the older turn settles AFTER the newer one already exists" is an
observable, deterministic state rather than a timing race.

Serves `web/` itself from an ephemeral port and replaces `window.fetch`
outright -- the same self-contained pattern as
tests/test_voice_transcript_ui_browser.py and
tests/test_voice_network_resilience_ui_browser.py. No `requires_server`
marker, so this runs at pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs. Each POST to /api/voice/turn/stream
# consumes the NEXT entry of window.__turnQueue, in call order -- so firing
# submitTurn() twice gives the first call turn 0's frames/gate and the second
# call turn 1's, independently. A '__gate' entry in a turn's frame list
# stalls its stream until window.__releaseTurn(<that turn's queue index>) is
# called; '__error' errors the stream's controller in place instead of
# enqueuing an SSE frame (a mid-stream connection drop, not a clean end).
_FETCH_MOCK = """
// Dock toggles default to auto-continue/listening ON, which makes voice.js
// re-acquire the mic once a turn finishes -- getUserMedia can't run headless.
// Muted playback for the same reason.
try {
  window.localStorage.setItem(
    'lifeos:chat:dock_settings',
    JSON.stringify({ mute: true, auto: false, fast: false, listen: false })
  );
} catch (e) { /* storage unavailable -- assertions below will say so */ }

window.__turnQueue = [];
window.__turnCallCount = 0;
window.__gatesOpen = {};
window.__cancelLog = [];
window.__releaseTurn = function (i) { window.__gatesOpen[i] = true; };

(function () {
  window.fetch = function (url, opts) {
    var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
    var signal = opts && opts.signal;
    function abortError() { return new DOMException('Turn cancelled', 'AbortError'); }

    if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
      var idx = window.__turnCallCount++;
      var frames = (window.__turnQueue[idx] || {}).frames || [];
      function waitForGate() {
        return new Promise(function (resolve, reject) {
          (function poll() {
            if (signal && signal.aborted) return reject(abortError());
            if (window.__gatesOpen[idx]) return resolve();
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
            if (frame === '__error') {
              return controller.error(new TypeError('Failed to fetch'));
            }
            controller.enqueue(enc.encode('data: ' + JSON.stringify(frame) + '\\n\\n'));
            // A real macrotask between frames, so the client observably
            // processes one before the next arrives.
            setTimeout(next, 10);
          })();
        },
      });
      return Promise.resolve(new Response(body, {
        status: 200, headers: { 'Content-Type': 'text/event-stream' },
      }));
    }

    if (urlStr.indexOf('/cancel') !== -1) {
      var m = urlStr.match(/\\/turn\\/([^/]+)\\/cancel/);
      window.__cancelLog.push(m ? decodeURIComponent(m[1]) : urlStr);
      return Promise.resolve(new Response('{}', { status: 200 }));
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


def _open_voice_chat(page: Page, base_url):
    page.add_init_script(_FETCH_MOCK)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")
    _wait_for_backend_ready(page)


def _set_queue(page: Page, turns):
    """`turns` is a list of {"frames": [...]} dicts, consumed in fetch-call
    order -- turns[0] is whichever submitTurn() call fires first."""
    page.evaluate(
        "(q) => { window.__turnQueue = JSON.parse(q); window.__turnCallCount = 0; "
        "window.__gatesOpen = {}; window.__cancelLog = []; }",
        json.dumps(turns),
    )


def _fire_turn(page: Page):
    """Starts submitTurn() without awaiting it, so mid-turn state can be
    inspected. Playwright only awaits a *returned* promise. Never passes a
    blob (headless -- nothing here exercises heldRecording)."""
    page.evaluate("() => { window.lifeChatVoice.submitTurn({ blob: null }); }")


def _release(page: Page, idx: int):
    page.evaluate("(i) => window.__releaseTurn(i)", idx)


def _thread(page: Page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#messages .message')].map("
        "  (m) => [m.className, (m.querySelector('.message-content')||{}).textContent || ''])"
    )


def _user_texts(page: Page):
    return [text for cls, text in _thread(page) if "user" in cls]


def _assistant_texts(page: Page):
    return [text for cls, text in _thread(page) if "assistant" in cls]


def _is_loading(page: Page):
    return page.evaluate("() => window.lifeChat.state.isLoading")


class TestOverlappingTurnOwnership:
    """AC (#832): a stale turn settling late must not clobber a newer turn's
    id/abort/busy/loading state, and Cancel must still target the newer
    turn."""

    def test_late_success_does_not_clobber_the_newer_turn(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_queue(page, [
            {"frames": [
                "__gate",
                {"type": "started", "turn_id": "turn-A-stale"},
                {"type": "done", "data": {"transcript": "first", "response_text": "first-response"}},
            ]},
            {"frames": [
                {"type": "started", "turn_id": "turn-B"},
                {"type": "transcript", "text": "second"},
                "__gate",
                {"type": "done", "data": {"transcript": "second", "response_text": "second-response"}},
            ]},
        ])
        _fire_turn(page)  # turn A -- stalls before it ever processes a frame
        _fire_turn(page)  # turn B -- overlaps A; becomes the current turn
        expect(page.locator("#messages .message.user")).to_have_text("second")
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn visible")
        expect(page.locator("#statusText")).to_have_text("Thinking…")

        # A's 'started' event (a decoy turn_id) and its 'done' both arrive
        # only now, well after B took over.
        _release(page, 0)
        page.wait_for_timeout(200)  # let A's now-unblocked promise chain settle

        # Every bit of B's still-in-flight bookkeeping must be untouched:
        # A's 'started' must not have overwritten activeTurnId, its 'done'
        # must not have rendered "first"/"first-response" or reset the
        # thinking placeholder/Cancel button/status text B still owns. The
        # only assistant-side element left is still B's own typing placeholder.
        assert _user_texts(page) == ["second"]
        expect(page.locator("#messages .message.assistant")).to_have_count(1)
        expect(page.locator("#messages .message.assistant .typing")).to_have_count(1)
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn visible")
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        assert _is_loading(page) is True

        # Cancel must still target the NEWER turn (activeTurnId === 'turn-B',
        # not A's decoy) -- proof activeTurnId/activeTurnAbort survived.
        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        assert page.evaluate("() => window.__cancelLog") == ["turn-B"]
        expect(page.locator("#messages .message.user")).to_have_count(0)
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _is_loading(page) is False

    def test_late_server_cancel_does_not_clobber_the_newer_turns_bubble(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_queue(page, [
            {"frames": [
                {"type": "started", "turn_id": "turn-A"},
                {"type": "transcript", "text": "first"},
                "__gate",
                {"type": "cancelled"},
            ]},
            {"frames": [
                {"type": "started", "turn_id": "turn-B"},
                {"type": "transcript", "text": "second"},
                "__gate",
            ]},
        ])
        _fire_turn(page)  # turn A -- renders "first", then stalls before its cancel frame
        expect(page.locator("#messages .message.user")).to_have_text("first")
        _fire_turn(page)  # turn B -- overlaps A; becomes the current turn
        expect(page.locator("#messages .message.user")).to_have_count(2)
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn visible")
        assert _user_texts(page) == ["first", "second"]

        # A's server-side cancel arrives only now. Unguarded, clearUserTranscript()
        # would delete whatever turnTranscriptEl currently points at -- B's
        # "second" bubble, not A's already-rendered "first" -- exactly the bug
        # #832 describes ("a slow server cancel arriving after the user has
        # moved on").
        _release(page, 0)
        page.wait_for_timeout(200)

        assert _user_texts(page) == ["first", "second"]
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn visible")
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        assert _is_loading(page) is True

        # Cancel still targets B, not stale A.
        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        assert page.evaluate("() => window.__cancelLog") == ["turn-B"]
        # B's own (uncompleted, undone) bubble is dropped by the real cancel;
        # A's already-rendered "first" is untouched history either way.
        assert _user_texts(page) == ["first"]
        expect(page.locator("#statusText")).to_have_text("Ready")

    def test_late_definitive_failure_does_not_clobber_the_newer_turn(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_queue(page, [
            {"frames": ["__gate", {"type": "error", "message": "boom"}]},
            {"frames": [
                {"type": "started", "turn_id": "turn-B"},
                {"type": "transcript", "text": "second"},
                "__gate",
            ]},
        ])
        _fire_turn(page)  # turn A -- stalls before its definitive server error
        _fire_turn(page)  # turn B -- overlaps A; becomes the current turn
        expect(page.locator("#messages .message.user")).to_have_text("second")
        expect(page.locator("#statusText")).to_have_text("Thinking…")

        # A's definitive 'error' frame arrives only now.
        _release(page, 0)
        page.wait_for_timeout(200)

        # A's failure must never surface -- no "⚠️ boom" message, no failed-
        # status row, and B's own status/thinking/cancel state untouched.
        assert "⚠️ boom" not in "".join(_assistant_texts(page))
        expect(page.locator(".voice-turn-status.failed")).to_have_count(0)
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        expect(page.locator("#messages .message.assistant .typing")).to_have_count(1)
        assert _is_loading(page) is True

        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        assert page.evaluate("() => window.__cancelLog") == ["turn-B"]
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _is_loading(page) is False

    def test_single_turn_success_is_unaffected(self, page: Page, chat_base_url):
        """Sanity check alongside the overlapping-turn cases above: with no
        second turn ever started, a lone turn's own cleanup still runs
        exactly as before -- the ownership check is never false for it."""
        _open_voice_chat(page, chat_base_url)
        _set_queue(page, [
            {"frames": [
                {"type": "started", "turn_id": "turn-solo"},
                {"type": "transcript", "text": "solo"},
                {"type": "done", "data": {"transcript": "solo", "response_text": "solo-response"}},
            ]},
        ])
        _fire_turn(page)
        expect(page.locator("#messages .message.assistant")).to_have_text("solo-response")
        expect(page.locator("#statusText")).to_have_text("Ready")
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn")
        assert _is_loading(page) is False
