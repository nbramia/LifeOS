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

// GET .../audio/{turnId} -- handleMidStreamDrop()'s pollForCompletedAudio().
// Gate release-controlled like the SSE streams above, for the same reason
// (independence from whatever submitTurn()'s own supersession abort does to
// the signal); __audioProbeSawSignal records whether a signal was actually
// passed, so a test can prove the wiring (#832/F5) without racing an abort's
// timing against the gate.
window.__audioGateOpen = false;
window.__audioFound = false;
window.__audioProbeSawSignal = false;

(function () {
  window.fetch = function (url, opts) {
    var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);

    if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
      var idx = window.__turnCallCount++;
      var frames = (window.__turnQueue[idx] || {}).frames || [];
      // Deliberately NOT abort-aware (unlike the other voice test files'
      // gates): submitTurn() now actively aborts a superseded turn's own
      // AbortController the moment a newer turn starts (#832/F2), which
      // would otherwise tear this stream down before a '__gate'-held frame
      // ever reaches handleEvent -- exactly the frame these tests exist to
      // deliver. A frame already in flight over the wire can still arrive
      // after a client-side abort in the real world (the same race #832/F1's
      // consumeTurnStream()-entry comment describes for postTurnStart()), so
      // gate release alone -- never the signal -- controls delivery here,
      // to test that race and the supersession-abort path independently.
      function waitForGate() {
        return new Promise(function (resolve) {
          (function poll() {
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
            if (i >= frames.length) return controller.close();
            var frame = frames[i++];
            if (frame === '__gate') {
              return waitForGate().then(next);
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

    if (urlStr.indexOf('/api/voice/audio/') !== -1) {
      if (opts && opts.signal) window.__audioProbeSawSignal = true;
      return new Promise(function (resolve) {
        (function poll() {
          if (window.__audioGateOpen) {
            resolve(new Response(null, { status: window.__audioFound ? 200 : 404 }));
            return;
          }
          setTimeout(poll, 10);
        })();
      });
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
    inspected -- Playwright only awaits a promise page.evaluate() itself
    returns. The promise is stashed on window.__turns (in fire order, which
    matches window.__turnQueue's fetch-call order: submitTurn()'s first
    await is its own POST) so _await_turn() can wait on it precisely later,
    instead of a sleep-and-hope (#832/F4). Never passes a blob (headless --
    nothing here exercises heldRecording)."""
    page.evaluate(
        "() => { window.__turns = window.__turns || []; "
        "window.__turns.push(window.lifeChatVoice.submitTurn({ blob: null })); }"
    )


def _release(page: Page, idx: int):
    page.evaluate("(i) => window.__releaseTurn(i)", idx)


def _await_turn(page: Page, idx: int):
    """Awaits the idx-th fired turn's own submitTurn() call settling --
    submitTurn() always resolves (its catch never rethrows), so this is safe
    to call unconditionally once that turn is expected to have finished
    settling (typically right after releasing its gate)."""
    page.evaluate("(i) => window.__turns[i]", idx)


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
        _await_turn(page, 0)  # wait for A's own submitTurn() promise to settle, never a sleep (#832/F4)

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

        # A's late 'started' frame carried its id, and by then it was already
        # superseded -- consumeTurnStream()'s 'started' handler POSTs a
        # best-effort cancel for it right then (#832/F2), since the earlier
        # supersession abort (fired when B started, before A's id was known)
        # could only close the connection, not tell the server to stop.
        assert page.evaluate("() => window.__cancelLog") == ["turn-A-stale"]

        # Cancel must still target the NEWER turn (activeTurnId === 'turn-B',
        # not A's decoy) -- proof activeTurnId/activeTurnAbort survived.
        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        assert page.evaluate("() => window.__cancelLog") == ["turn-A-stale", "turn-B"]
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
        # A's id was already known (its 'started' frame arrived before B ever
        # started), so B's own supersession logic could cancel it immediately
        # -- no need to wait for anything server-side (#832/F2).
        assert page.evaluate("() => window.__cancelLog") == ["turn-A"]

        # A's server-side cancel arrives only now anyway (a real, if now rarer,
        # race -- see the fetch mock's own comment on why gate release, not
        # the abort signal, controls delivery here). Unguarded,
        # clearUserTranscript() would delete whatever turnTranscriptEl
        # currently points at -- B's "second" bubble, not A's already-
        # rendered "first" -- exactly the bug #832 describes ("a slow server
        # cancel arriving after the user has moved on").
        _release(page, 0)
        _await_turn(page, 0)

        assert _user_texts(page) == ["first", "second"]
        expect(page.locator("#voiceCancelBtn")).to_have_class("voice-cancel-btn visible")
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        assert _is_loading(page) is True

        # Cancel still targets B, not stale A.
        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        assert page.evaluate("() => window.__cancelLog") == ["turn-A", "turn-B"]
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
        _await_turn(page, 0)

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

    def test_cancel_button_click_targets_the_newer_turn(self, page: Page, chat_base_url):
        """Every other test here calls cancelActiveTurn() directly (the
        exported test seam). This one instead drives the real Cancel button
        (onTalkClick's sibling handler, voice.js:492-495 -- `if (voiceBusy ||
        clipInFlight) cancelActiveTurn()`), so the UI wiring itself is
        proven, not just the function it calls (#832/F10)."""
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

        page.locator("#voiceCancelBtn").click()

        # The click reaches cancelActiveTurn() (voiceBusy is true for B), which
        # targets whatever is CURRENT -- turn-B, never A's decoy id.
        assert page.evaluate("() => window.__cancelLog") == ["turn-B"]
        expect(page.locator("#messages .message.user")).to_have_count(0)
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _is_loading(page) is False

        # A's late success still must not resurrect anything once it arrives.
        _release(page, 0)
        _await_turn(page, 0)
        assert _thread(page) == []
        expect(page.locator("#statusText")).to_have_text("Ready")

    def test_midstream_drop_poll_is_abort_aware(self, page: Page, chat_base_url):
        """#832/F5: pollForCompletedAudio() used to run its own ~3s budget
        deaf to cancellation -- neither its inter-attempt sleep nor its HEAD
        fetch carried the turn's own AbortController signal, so nothing
        stopped it early: not a Cancel tap, not a newer turn's own
        supersession abort. Proven here by checking the signal was actually
        passed through to the probe's fetch call, not by racing an abort
        against the poll's timing (flaky either way this could be fixed)."""
        _open_voice_chat(page, chat_base_url)
        _set_queue(page, [
            {"frames": [
                {"type": "started", "turn_id": "turn-A"},
                {"type": "transcript", "text": "first"},
                "__error",
            ]},
        ])
        _fire_turn(page)
        expect(page.locator("#statusText")).to_have_text("Reconnecting…")
        assert page.evaluate("() => window.__audioProbeSawSignal") is True
        page.evaluate("() => { window.__audioGateOpen = true; }")  # let it finish cleanly
        _await_turn(page, 0)

    @pytest.mark.parametrize("audio_found", [True, False])
    def test_midstream_drop_poll_completing_late_does_not_clobber_the_newer_turn(
            self, page: Page, chat_base_url, audio_found):
        """Production's real trigger for this overlap is cancelActiveTurn()
        firing while this poll is in flight, then a fresh recording starting
        (see test_cancel_button_click_targets_the_newer_turn and the abort-
        wiring test above) -- but handleMidStreamDrop()'s own isOwnTurn
        checks are the backstop regardless of what aborts what, so this
        proves THEM directly: even if this turn's poll were to reach a
        verdict despite already being superseded (the audio-probe gate here
        is deliberately release-only, decoupled from the abort signal, same
        reasoning as the SSE streams' own gates above), neither a 'found' nor
        a 'not found' verdict may act on shared state B now owns (#832/F5)."""
        _open_voice_chat(page, chat_base_url)
        page.evaluate("(found) => { window.__audioFound = found; }", audio_found)
        _set_queue(page, [
            {"frames": [
                {"type": "started", "turn_id": "turn-A"},
                {"type": "transcript", "text": "first"},
                "__error",
            ]},
            {"frames": [
                {"type": "started", "turn_id": "turn-B"},
                {"type": "transcript", "text": "second"},
                "__gate",
            ]},
        ])
        _fire_turn(page)  # turn A -- mid-stream drop right after 'transcript'
        expect(page.locator("#statusText")).to_have_text("Reconnecting…")

        _fire_turn(page)  # turn B -- overlaps A; becomes the current turn
        # A's own bubble ("first") isn't retracted by a mid-stream drop
        # (matching handleTerminalFailure()'s existing "keeps the user
        # bubble" behavior on any failure) -- both are present at this point.
        expect(page.locator("#messages .message.user")).to_have_count(2)
        assert _user_texts(page) == ["first", "second"]

        # A's poll resolves only now, well after B took over.
        page.evaluate("() => { window.__audioGateOpen = true; }")
        _await_turn(page, 0)

        # Neither outcome may touch B: no "tap to hear it" message (the
        # `found` branch), no failed-status row (the `!found` branch), and
        # B's own status/thinking/cancel state untouched either way.
        assert _user_texts(page) == ["first", "second"]
        assert "tap to hear it" not in "".join(_assistant_texts(page))
        expect(page.locator(".voice-turn-status.failed")).to_have_count(0)
        expect(page.locator("#statusText")).to_have_text("Thinking…")
        expect(page.locator("#messages .message.assistant .typing")).to_have_count(1)
        assert _is_loading(page) is True

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


# --- post-playback continuation guard (df9bf88, #832 follow-up) -----------
#
# submitTurn()'s tail after `await playbackChain` -- showCancel(false);
# setStatus('', 'Ready'); await maybeAutoContinue() -- runs only if this turn
# is still current. Tapping Cancel while a completed turn's reply is still
# playing (cancelActiveTurn()'s "stop playback" use, once `turnDone`) is
# exactly what settles playbackChain early and ALSO already resets
# activeTurnAbort itself -- so by the time this resumes, isOwnTurn() is
# already false regardless of whether some OTHER turn has started. Unguarded,
# maybeAutoContinue() would re-arm a fresh recording anyway (the #721
# regression this guard prevents, in the "stop playback" variant rather than
# the "manual recording stop" one #721 was originally about) -- confirmed by
# checking getUserMedia call count directly, not by asserting silence.
#
# Needs real playback (unlike the rest of this file, which runs muted) to put
# submitTurn() genuinely inside `await playbackChain` when Cancel is tapped,
# so this uses the real-WAV-clip + instrumented-Audio-element pattern
# tests/test_voice_manual_stop_auto_mode_ui_browser.py and
# tests/test_voice_rate_toggle_playback_ui_browser.py already establish,
# rather than this file's own gate-driven SSE mock.
_PLAYBACK_INSTRUMENT_SCRIPT = """
window.__turnClipUrl = null;
window.fetch = function (url, opts) {
  var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    var sse = 'data: ' + JSON.stringify({ type: 'started', turn_id: 'turn-playback' }) + '\\n\\n'
            + 'data: ' + JSON.stringify({ type: 'main_audio', url: window.__turnClipUrl }) + '\\n\\n'
            + 'data: ' + JSON.stringify({ type: 'done', data: { response_text: 'ok' } }) + '\\n\\n';
    return Promise.resolve(new Response(sse, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' },
    }));
  }
  if (urlStr.indexOf('/cancel') !== -1) {
    return Promise.resolve(new Response('{}', { status: 200 }));
  }
  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
};

window.__gumCalls = 0;
navigator.mediaDevices.getUserMedia = function (constraints) {
  window.__gumCalls += 1;
  var ctx = new (window.AudioContext || window.webkitAudioContext)();
  var dest = ctx.createMediaStreamDestination();
  return Promise.resolve(dest.stream);
};

(function () {
  window.__audioPlayingCount = 0;
  var proto = HTMLMediaElement.prototype;
  var origPlay = proto.play;
  proto.play = function () {
    if (!this.__wired832) {
      this.__wired832 = true;
      this.addEventListener('playing', function () { window.__audioPlayingCount += 1; });
      var onEnd = function () { window.__audioPlayingCount = Math.max(0, window.__audioPlayingCount - 1); };
      this.addEventListener('ended', onEnd);
      this.addEventListener('pause', onEnd);
    }
    var p = origPlay.call(this);
    p.catch(function () {});
    return p;
  };
})();

window.__makeWav = function (ms, sampleRate) {
  sampleRate = sampleRate || 8000;
  var n = Math.round((ms / 1000) * sampleRate);
  var dataBytes = n * 2;
  var buf = new ArrayBuffer(44 + dataBytes);
  var view = new DataView(buf);
  function writeStr(offset, str) {
    for (var i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  }
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, dataBytes, true);
  var bytes = new Uint8Array(buf);
  var binary = '';
  var chunk = 0x8000;
  for (var i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return 'data:audio/wav;base64,' + btoa(binary);
};
"""


def _open_playback_chat(page: Page, base_url, *, auto):
    settings = {"mute": False, "auto": auto, "fast": False, "listen": False}
    page.add_init_script(
        "try { window.localStorage.setItem('lifeos:chat:dock_settings', "
        + json.dumps(json.dumps(settings))
        + "); } catch (e) {}"
    )
    page.add_init_script(_PLAYBACK_INSTRUMENT_SCRIPT)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")


class TestPostPlaybackCancelGuard:
    def test_cancel_during_playback_does_not_rearm_auto_continue(self, page: Page, chat_base_url):
        _open_playback_chat(page, chat_base_url, auto=True)
        clip_url = page.evaluate("window.__makeWav(3000)")  # long enough to tap Cancel mid-playback
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        page.evaluate("() => { window.lifeChatVoice.submitTurn({ transcript: 'hi' }); }")

        page.wait_for_function("window.__audioPlayingCount > 0")
        page.locator("#voiceCancelBtn").click()  # the real "stop playback" tap
        page.wait_for_function("window.__audioPlayingCount === 0")

        # Settle well past submitTurn()'s own resumption and give a would-be
        # re-arm every chance to fire before asserting it didn't.
        page.wait_for_timeout(600)
        assert page.evaluate("window.__gumCalls") == 0, (
            "auto-continue re-armed a recording after Cancel stopped mid-reply playback"
        )
        expect(page.locator("#statusText")).to_have_text("Ready")
