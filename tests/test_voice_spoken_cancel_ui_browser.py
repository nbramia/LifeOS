"""Browser tests for the spoken-cancel discard while recording (#722).

Rides on #718's smart-turn endpointing candidate-pause transcript rather than
opening a second detection path, timer, or STT call: `checkEndpointCandidate()`
in `web/chat/voice.js` now checks the SAME transcript it already fetches from
`POST /api/voice/transcribe` for a cancel utterance BEFORE the completeness
decision (`isTranscriptComplete()`). A cancel verdict discards the recording
through `stopRecordingAndSend({ discard: true })` -- the same teardown
`finalizeEndpointing()` uses for a normal complete verdict, just routed past
`submitTurn()` into `handleSkippedEmptyRecording()`, the existing no-submit
path a silent/empty recording already uses. That's also why a spoken cancel
never re-arms auto-continue (#721): the only re-arm trigger is
`submitTurn()`'s own `maybeAutoContinue()` call after a reply plays, which a
discarded recording never reaches.

`isCancelUtterance()`, the pure trailing-anchored matcher, is exported (like
`isTranscriptComplete()`) and tested directly with no page/recording/network
round-trip -- the false-positive case ("cancel my 3pm with Dana" must NOT
match; a naive substring/`includes()` check would wrongly eat it) is the
single most important case in that table.

Same seam pattern and instrumentation as
tests/test_voice_endpointing_ui_browser.py (`window.lifeChatVoice.
checkEndpointCandidate(samples, sampleRate)`, a deferrable
`/api/voice/transcribe` stub, and MediaRecorder start/stop + turn/stream call
counters) -- the live onaudioprocess VAD this pipeline sits on can't run
headless either. No `requires_server` marker (serves `web/` itself from an
ephemeral port), so this runs at pre-push (`browser and not requires_server`).
"""
import http.server
import threading
from pathlib import Path

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


# Same instrumentation as tests/test_voice_endpointing_ui_browser.py -- a fake
# getUserMedia/MediaRecorder, a fetch stub for /api/voice/transcribe
# (deferrable via window.__deferTranscribe/__resolveTranscribe) and
# /api/voice/turn/stream (counted via window.__turnStreamCalls -- a spoken
# cancel must never increment this), and MediaRecorder start/stop counters
# (stopMediaRecorder() calls .stop() unconditionally the moment
# stopRecordingAndSend() reaches it, regardless of what the silence-detection
# later decides to do with the resulting blob, so this is a reliable signal
# for "did a finalize/discard actually happen" without needing genuine
# non-silent recorded audio -- see that file's docstring).
_INSTRUMENT_SCRIPT = """
window.__fetchLog = [];
window.__transcribeResponse = 'turn off the lights.';
window.__deferTranscribe = false;
window.__pendingTranscribeResolvers = [];
window.__turnStreamCalls = 0;

function __transcribeResponseBody() {
  return new Response(JSON.stringify({ transcript: window.__transcribeResponse }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

window.fetch = function (url, opts) {
  var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  window.__fetchLog.push(urlStr);
  if (urlStr.indexOf('/api/voice/transcribe') !== -1) {
    if (window.__deferTranscribe) {
      return new Promise(function (resolve) {
        window.__pendingTranscribeResolvers.push(function () { resolve(__transcribeResponseBody()); });
      });
    }
    return Promise.resolve(__transcribeResponseBody());
  }
  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    window.__turnStreamCalls += 1;
    var sse = 'data: ' + JSON.stringify({ type: 'done', data: { response_text: 'ok' } }) + '\\n\\n';
    return Promise.resolve(new Response(sse, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' },
    }));
  }
  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
};
window.__resolveTranscribe = function () {
  var fns = window.__pendingTranscribeResolvers;
  window.__pendingTranscribeResolvers = [];
  fns.forEach(function (fn) { fn(); });
};

window.__gumCalls = 0;
navigator.mediaDevices.getUserMedia = function (constraints) {
  window.__gumCalls += 1;
  var ctx = new (window.AudioContext || window.webkitAudioContext)();
  var dest = ctx.createMediaStreamDestination();
  return Promise.resolve(dest.stream);
};

(function () {
  window.__recorderStartCalls = 0;
  window.__recorderStopCalls = 0;
  var OrigMR = window.MediaRecorder;
  function WrappedMR(stream, opts) {
    var mr = opts ? new OrigMR(stream, opts) : new OrigMR(stream);
    var origStart = mr.start.bind(mr);
    mr.start = function (ms) { window.__recorderStartCalls += 1; return origStart(ms); };
    var origStop = mr.stop.bind(mr);
    mr.stop = function () { window.__recorderStopCalls += 1; return origStop(); };
    return mr;
  }
  WrappedMR.isTypeSupported = OrigMR.isTypeSupported.bind(OrigMR);
  window.MediaRecorder = WrappedMR;
})();
"""


def _open_voice_chat(page: Page, base_url):
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceListen")


# Listening (#710) ships on by default and acquires its own mic stream the
# instant voice mode is entered -- seeded off here so the only recording in
# play is the one _start_recording() below starts, mirroring
# tests/test_voice_endpointing_ui_browser.py's identical helper.
def _open_voice_chat_listening_off(page: Page, base_url):
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.add_init_script(
        "window.localStorage.setItem('lifeos:chat:dock_settings', "
        "JSON.stringify({ mute: false, auto: true, fast: true, listen: false }));"
    )
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceListen")


def _start_recording(page: Page):
    """Auto ships on by default -- this just taps the talk button and waits
    for a real (fake-stream) recording to actually be in progress."""
    page.locator("#voiceTalkBtn").click()
    page.wait_for_function(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


def _is_recording(page: Page):
    return page.evaluate(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


def _check_candidate(page: Page, transcript):
    """Sets the stubbed transcribe response, then runs the real
    checkEndpointCandidate() pipeline with a throwaway synthetic clip --
    content doesn't matter, the network response is stubbed regardless.
    Returns the verdict: True/False (completeness), None (suspended/stale/
    unreachable), or 'cancelled' (a spoken-cancel discard, #722)."""
    page.evaluate("(t) => { window.__transcribeResponse = t; }", transcript)
    return page.evaluate(
        "() => window.lifeChatVoice.checkEndpointCandidate(new Float32Array(160), 16000)"
    )


class TestCancelUtteranceMatcher:
    """isCancelUtterance() is a pure function -- no page interaction beyond
    loading the module, no recording, no network. Trailing-anchored and
    normalized (lowercase, strip trailing punctuation): matches only when the
    utterance ITSELF, or the recording's trailing words, are a cancel phrase.
    The false-positive cases (a real request that happens to contain
    "cancel") are the whole point of this feature -- a naive substring/
    `includes()` match would wrongly eat them."""

    @pytest.mark.parametrize("transcript,expected", [
        # True positives -- exact phrases from the constant, in various
        # realistic transcript shapes (standalone, trailing punctuation,
        # multi-word phrase, leading filler before the phrase).
        ("cancel", True),
        ("Cancel.", True),
        ("Cancel!", True),
        ("CANCEL", True),
        ("cancel that", True),
        ("Cancel that.", True),
        ("never mind", True),
        ("Never mind.", True),
        ("nevermind", True),
        ("forget it", True),
        ("Forget it.", True),
        ("scratch that", True),
        ("Scratch that.", True),
        ("Actually, never mind", True),
        ("Uh, scratch that.", True),
        ("Wait -- cancel", True),
        # False positives -- "cancel" (or a cancel phrase's words) appears,
        # but NOT trailing the transcript, or trailing a longer real request.
        ("cancel my 3pm with Dana", False),
        ("Cancel my 3pm with Dana.", False),
        ("don't cancel the meeting", False),
        ("Please don't cancel my flight.", False),
        ("cancel the reminder about the dentist", False),
        ("never mind that, actually go ahead and book it", False),
        ("scratch that idea, let's try something else", False),
        # No match at all.
        ("Turn off the lights.", False),
        ("", False),
        ("   ", False),
    ])
    def test_matcher_cases(self, page: Page, chat_base_url, transcript, expected):
        _open_voice_chat(page, chat_base_url)
        result = page.evaluate(
            "(t) => window.lifeChatVoice.isCancelUtterance(t)", transcript
        )
        assert result is expected, f"isCancelUtterance({transcript!r}) == {result}, expected {expected}"


class TestCancelDiscardsRecording:
    """A candidate transcript that IS a cancel utterance stops recording and
    submits nothing -- checked before the completeness decision, so it never
    even reaches isTranscriptComplete()."""

    def test_bare_cancel_discards_and_stops_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "Cancel.")

        assert result == "cancelled"
        page.wait_for_function("window.__recorderStopCalls === 1")
        assert _is_recording(page) is False

    def test_cancel_never_reaches_turn_stream(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        _check_candidate(page, "never mind")
        page.wait_for_function("window.__recorderStopCalls === 1")
        # Give any errant submit path a moment to fire before asserting it didn't.
        page.wait_for_timeout(300)

        assert page.evaluate("window.__turnStreamCalls") == 0
        assert not any(
            "/api/voice/turn/stream" in u for u in page.evaluate("window.__fetchLog")
        )

    def test_cancel_tears_down_endpointing_cleanly(self, page: Page, chat_base_url):
        """No lingering timer/graph after a discard -- the talk button
        starts a fresh recording normally afterward, the same signal
        tests/test_voice_endpointing_ui_browser.py's stale-check test uses.
        Also checked directly via isEndpointTapActive() (#734's tap
        inventory) rather than only inferred through the restart."""
        _open_voice_chat_listening_off(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is True

        _check_candidate(page, "scratch that")
        page.wait_for_function("window.__recorderStopCalls === 1")
        assert _is_recording(page) is False
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is False

        _start_recording(page)
        page.wait_for_function("window.__recorderStartCalls === 2")
        assert page.evaluate("window.__gumCalls") == 1  # never a second getUserMedia call


class TestCancelDoesNotReArmAutoContinue:
    """A spoken cancel is a user-initiated stop (#721's rationale) -- it must
    not immediately restart recording, even with Auto on."""

    def test_cancel_does_not_restart_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.__recorderStartCalls") == 1

        _check_candidate(page, "cancel")
        page.wait_for_function("window.__recorderStopCalls === 1")
        # Settle well past MIN_RECORD_MS (300ms) and give a would-be restart
        # every chance to fire before asserting it didn't.
        page.wait_for_timeout(600)

        assert _is_recording(page) is False
        assert page.evaluate("window.__recorderStartCalls") == 1, (
            "recording restarted immediately after a spoken cancel"
        )

    def test_later_manual_cycle_still_rearms_normally(self, page: Page, chat_base_url):
        """Control: a spoken cancel only suppresses re-arm for ITS cycle --
        auto-continue itself is not broken. A later manually-started
        recording is unaffected."""
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        _check_candidate(page, "cancel")
        page.wait_for_function("window.__recorderStopCalls === 1")
        page.wait_for_timeout(600)
        assert page.evaluate("window.__recorderStartCalls") == 1

        _start_recording(page)
        page.wait_for_function("window.__recorderStartCalls === 2")
        assert _is_recording(page) is True


class TestFalsePositiveRequestSubmitsNormally:
    """A real request that happens to contain "cancel" mid-sentence is NOT
    treated as a cancellation -- completeness rules apply exactly as they
    would with no cancel phrase involved."""

    def test_cancel_mid_request_is_not_cancelled(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "Cancel my 3pm with Dana.")

        assert result is True, "a legitimate request containing \"cancel\" was wrongly discarded"
        page.wait_for_function("window.__recorderStopCalls === 1")

    def test_dont_cancel_request_is_not_cancelled(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "Please don't cancel my flight.")

        assert result is True
        page.wait_for_function("window.__recorderStopCalls === 1")

    def test_cancel_mid_request_without_terminal_punctuation_keeps_recording(
            self, page: Page, chat_base_url):
        """Same false-positive transcript, but incomplete per the existing
        heuristic (no terminal punctuation) -- proves the cancel check didn't
        short-circuit the normal completeness path either way."""
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "cancel my 3pm with Dana")

        assert result is False
        assert page.evaluate("window.__recorderStopCalls") == 0
        assert _is_recording(page) is True


class TestStaleTokenCancel:
    """A stale (superseded) candidate check must never discard a DIFFERENT,
    newer recording than the one it was transcribing -- the same guard
    tests/test_voice_endpointing_ui_browser.py's TestCancelledDuringCheck
    exercises for a "complete" verdict, here for a cancel verdict."""

    def test_stale_cancel_does_not_discard_newer_recording(
            self, page: Page, chat_base_url):
        _open_voice_chat_listening_off(page, chat_base_url)
        _start_recording(page)

        page.evaluate("() => { window.__deferTranscribe = true; }")
        page.evaluate("""
            () => {
              window.__candidateResult = undefined;
              window.lifeChatVoice.checkEndpointCandidate(new Float32Array(160), 16000)
                .then((r) => { window.__candidateResult = r; });
            }
        """)
        page.wait_for_function(
            "window.__fetchLog.some((u) => u.indexOf('/api/voice/transcribe') !== -1)"
        )
        assert _is_recording(page) is True  # the check hasn't resolved yet

        # The user taps stop for real while the candidate check is still in
        # flight -- the SAME manual-stop path a talk-button tap always uses,
        # independent of endpointing/cancel-detection entirely. force=True:
        # the .recording class drives an infinite CSS pulse animation, which
        # fails Playwright's default "element is stable" actionability wait.
        page.locator("#voiceTalkBtn").click(force=True)
        page.wait_for_function("window.__recorderStopCalls === 1")

        # A manual stop stays stopped, even with Auto on (#721) -- so start
        # the next recording the way a user would, by tapping again. This is
        # the case the token guard exists for: a *different* recording is
        # now live than the one the in-flight check transcribed.
        _start_recording(page)
        page.wait_for_function("window.__recorderStartCalls === 2")
        page.wait_for_function("window.__gumCalls === 1")  # never a second getUserMedia call

        # Now let the STALE check (for the recording that already ended)
        # resolve as a cancel -- it must not discard the NEW recording Auto-
        # continue just started.
        page.evaluate("(t) => { window.__transcribeResponse = t; }", "cancel")
        page.evaluate("() => { window.__resolveTranscribe(); }")

        page.wait_for_function("window.__candidateResult !== undefined")
        assert page.evaluate("window.__candidateResult") is None, (
            "a stale cancel verdict discarded a DIFFERENT, newer recording "
            "than the one it was transcribing"
        )
        # Only the manual stop ever called .stop() -- the stale check's late
        # cancel verdict did not trigger a second discard.
        assert page.evaluate("window.__recorderStopCalls") == 1
        assert _is_recording(page) is True  # the NEW (Auto-continue) recording is untouched


class TestCancelRequiresAutoMode:
    """Endpointing (and therefore cancel detection, which rides its
    candidate-pause transcript) only ever runs while Auto is on -- a candidate
    check with Auto off must not even reach the transcribe route, exactly as
    tests/test_voice_endpointing_ui_browser.py's TestAutoModeGating covers
    for the completeness path."""

    def test_auto_off_cancel_phrase_not_detected(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()
        _start_recording(page)

        result = _check_candidate(page, "cancel")

        assert result is None
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStopCalls") == 0
        assert not any(
            "/api/voice/transcribe" in u for u in page.evaluate("window.__fetchLog")
        ), "a candidate check reached the transcribe route with Auto off"
