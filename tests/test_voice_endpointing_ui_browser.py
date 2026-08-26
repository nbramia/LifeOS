"""Browser tests for smart turn endpointing in auto-mode voice recording (#718).

While the **Auto** dock toggle is on, `web/chat/voice.js` now decides *when a
recording ends* on its own, layered on top of Auto-continue's existing
"reopen the mic after a reply" behavior. Pipeline: an energy VAD on the live
recording stream (the same technique `handleListenFrame()` uses for #710 wake
detection) detects trailing silence following speech; once it crosses
`LIFEOS_VOICE_ENDPOINT_SILENCE_MS`, the recording-so-far is transcribed via
the same bare-STT route (`POST /api/voice/transcribe`) Listening's wake check
already uses; `isTranscriptComplete()` decides whether that sounds like a
finished thought. Complete finalizes through `stopRecordingAndSend()` -- the
exact function a manual stop-tap calls, never a parallel submit path.
Incomplete keeps recording. `LIFEOS_VOICE_ENDPOINT_HARD_CAP_MS` of continuous
silence finalizes regardless, so an ambiguous/unreachable check can never
hang the mic open.

Like the sibling wake-word/wake-chime suites, the live onaudioprocess VAD
can't run headless without real audio hardware, so these tests drive the
pipeline from its exported test seams -- `window.lifeChatVoice.
checkEndpointCandidate(samples, sampleRate)` and `...finalizeEndpointing()`
-- the same seam pattern `checkForWakeWord()` established
(tests/test_voice_listening_wake_word_ui_browser.py). Both are the REAL
functions the live silence timer calls once its thresholds are crossed;
nothing here is a parallel/fake implementation of that logic.

`isTranscriptComplete()`, the pure completeness heuristic, is tested directly
with no page/recording/network involved at all -- exported specifically to
make the word list testable on its own (see its doc comment in
`web/chat/voice.js`).

No `requires_server` marker (serves `web/` itself from an ephemeral port, the
same pattern as tests/test_voice_mic_block_ui_browser.py), so this runs at
pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs. Mirrors
# tests/test_voice_listening_wake_word_ui_browser.py's instrument script
# (fake getUserMedia/MediaRecorder, a fetch stub) with two additions:
#   - /api/voice/transcribe can be deferred (window.__deferTranscribe) and
#     resolved on demand (window.__resolveTranscribe()) so a test can pause
#     mid-round-trip and act (e.g. a manual stop) before it completes --
#     the same pattern test_voice_wake_chime_ui_browser.py uses for the
#     chime's `ended` event.
#   - A MediaRecorder.stop() call counter (window.__recorderStopCalls),
#     alongside the existing start-call counter -- stopMediaRecorder() in
#     voice.js calls `.stop()` unconditionally the moment
#     stopRecordingAndSend() reaches it, regardless of what the resulting
#     blob's silence-detection later decides to do with it, so this is a
#     reliable, audio-content-independent signal for "did a finalize/stop
#     actually happen" without needing genuine non-silent recorded audio
#     (MediaRecorder audio content is not reliable headless -- see
#     tests/test_voice_rate_toggle_playback_ui_browser.py's docstring).
#   - A /api/voice/turn/stream call counter (window.__turnStreamCalls).
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
# instant voice mode is entered -- before a test's first `page.evaluate()`/
# `.uncheck()` call could possibly race it off. Tests that assert on
# `__gumCalls` (endpointing must never be a SECOND getUserMedia call) seed
# Listening off in localStorage before the page loads, so it never starts at
# all -- unrelated to whatever this test is actually exercising.
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
    Returns the completeness verdict (True/False/None)."""
    page.evaluate("(t) => { window.__transcribeResponse = t; }", transcript)
    return page.evaluate(
        "() => window.lifeChatVoice.checkEndpointCandidate(new Float32Array(160), 16000)"
    )


class TestCompletenessHeuristic:
    """isTranscriptComplete() is a pure function -- no page interaction
    beyond loading the module, no recording, no network."""

    @pytest.mark.parametrize("transcript,expected", [
        ("Turn off the lights.", True),
        ("Is it raining outside?", True),
        ("Wow!", True),
        ('He said "stop."', True),
        ("turn off the lights", False),          # no terminal punctuation
        ("I want to go to the store and", False),  # trailing conjunction
        ("I think, um", False),                  # trailing filler
        ("coffee or", False),
        ("if it's sunny then", False),
        ("I was going to say... i mean", False),  # trailing filler phrase
        ("but that's okay.", True),               # "but" mid-sentence is fine; trailing word governs
        ("SO", False),                            # case-insensitive filler match
        ("", False),                              # empty transcript
        ("   ", False),                           # whitespace-only transcript
    ])
    def test_heuristic_cases(self, page: Page, chat_base_url, transcript, expected):
        _open_voice_chat(page, chat_base_url)
        result = page.evaluate(
            "(t) => window.lifeChatVoice.isTranscriptComplete(t)", transcript
        )
        assert result is expected, f"isTranscriptComplete({transcript!r}) == {result}, expected {expected}"


class TestAutoModeGating:
    """Endpointing only ever runs while Auto + voice mode + an actual
    recording are all true."""

    def test_auto_off_no_endpointing(self, page: Page, chat_base_url):
        """Auto off -> today's manual-stop-only behavior, byte-identical: a
        candidate check must not even reach the transcribe route."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()
        _start_recording(page)

        result = _check_candidate(page, "Turn off the lights.")

        assert result is None
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStopCalls") == 0
        assert not any(
            "/api/voice/transcribe" in u for u in page.evaluate("window.__fetchLog")
        ), "a candidate check reached the transcribe route with Auto off"

    def test_no_second_getusermedia_call(self, page: Page, chat_base_url):
        """Endpointing taps the SAME recording stream -- it must never
        acquire its own mic. Listening (#710, on by default) holds its own
        separate stream unrelated to this, so it's seeded off here to
        isolate endpointing's own mic usage specifically."""
        _open_voice_chat_listening_off(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.__gumCalls") == 1

        _check_candidate(page, "Turn off the lights.")

        assert page.evaluate("window.__gumCalls") == 1


class TestCandidateCompleteness:
    """The stubbed transcribe response drives the real completeness
    decision: complete finalizes (through the manual-stop path), incomplete
    keeps recording."""

    def test_complete_transcript_finalizes(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "Turn off the lights.")

        assert result is True
        page.wait_for_function("window.__recorderStopCalls === 1")

    def test_incomplete_transcript_keeps_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "I want to go to the store and")

        assert result is False
        assert page.evaluate("window.__recorderStopCalls") == 0
        assert _is_recording(page) is True
        # Still the same recording -- not restarted/duplicated.
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_empty_transcript_keeps_recording(self, page: Page, chat_base_url):
        """An unparseable/empty transcript (e.g. the gateway route not
        shipped yet, #relay-not-yet-shipped) must not be treated as
        complete."""
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        result = _check_candidate(page, "")

        assert result is False
        assert page.evaluate("window.__recorderStopCalls") == 0
        assert _is_recording(page) is True


class TestHardCapFinalize:
    """finalizeEndpointing() is the exact function real continuous silence
    crossing HARD_CAP_MS calls -- exercised directly since a browser test
    can't wait out multiple real seconds of silence through the live audio
    graph."""

    def test_hard_cap_finalizes(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        page.evaluate("() => window.lifeChatVoice.finalizeEndpointing()")

        page.wait_for_function("window.__recorderStopCalls === 1")

    def test_finalize_while_not_recording_is_a_noop(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)

        page.evaluate("() => window.lifeChatVoice.finalizeEndpointing()")

        assert page.evaluate("window.__recorderStopCalls") == 0


class TestCancelledDuringCheck:
    """An in-flight candidate check must not resurrect/duplicate-finalize a
    turn the user has already ended a different way -- guards are re-checked
    after every await, the same pattern triggerWakeRecording() uses after
    its chime."""

    def test_manual_stop_during_check_wins_stale_check_is_a_noop(
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

        # The user taps stop for real while the candidate check is still
        # in flight -- the SAME manual-stop path a talk-button tap always
        # uses, independent of endpointing entirely. force=True: the
        # .recording class drives an infinite CSS pulse animation, which
        # fails Playwright's default "element is stable" actionability wait.
        page.locator("#voiceTalkBtn").click(force=True)
        page.wait_for_function("window.__recorderStopCalls === 1")

        # A manual stop stays stopped, even with Auto on (#721) -- so start
        # the next recording the way a user would, by tapping again. This is
        # the case the token guard exists for: a *different* recording is now
        # live than the one the in-flight check transcribed.
        _start_recording(page)
        page.wait_for_function("window.__recorderStartCalls === 2")
        page.wait_for_function("window.__gumCalls === 1")  # never a second getUserMedia call

        # Now let the STALE check (for the recording that already ended)
        # resolve as "complete" -- it must not finalize the NEW recording
        # Auto-continue just started.
        page.evaluate("(t) => { window.__transcribeResponse = t; }", "Turn off the lights.")
        page.evaluate("() => { window.__resolveTranscribe(); }")

        page.wait_for_function("window.__candidateResult !== undefined")
        assert page.evaluate("window.__candidateResult") is None, (
            "a stale candidate check resurrected/finalized a turn after the user "
            "had already manually stopped it"
        )
        # Only the manual stop ever called .stop() -- the stale check's late
        # "complete" verdict did not trigger a second finalize.
        assert page.evaluate("window.__recorderStopCalls") == 1
        assert _is_recording(page) is True  # the NEW (Auto-continue) recording is untouched

    def test_no_change_during_check_still_finalizes_normally(
            self, page: Page, chat_base_url):
        """Control: with nothing interrupting it, a deferred-then-resolved
        check behaves exactly like an immediate one."""
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)

        page.evaluate("() => { window.__deferTranscribe = true; }")
        page.evaluate("(t) => { window.__transcribeResponse = t; }", "Turn off the lights.")
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

        page.evaluate("() => { window.__resolveTranscribe(); }")

        page.wait_for_function("window.__candidateResult !== undefined")
        assert page.evaluate("window.__candidateResult") is True
        page.wait_for_function("window.__recorderStopCalls === 1")


class TestBargeInSuspension:
    """Endpointing only applies while actually recording -- never during TTS
    playback, and never when there's no recording at all."""

    def test_candidate_check_during_playback_not_recording_is_a_noop(
            self, page: Page, chat_base_url):
        """Not recording at all (e.g. mid-turn/TTS playback) -> no candidate
        decision is possible."""
        _open_voice_chat(page, chat_base_url)
        assert _is_recording(page) is False

        result = _check_candidate(page, "Turn off the lights.")

        assert result is None
        assert page.evaluate("window.__recorderStopCalls") == 0


class TestEndpointTapTeardown:
    """The endpointing ScriptProcessorNode (#734's tap #3 -- see the
    audio-taps inventory above ensureAudioContext() in voice.js) must be
    connected only while a recording it governs is actually in progress, and
    disconnected on every path that ends that recording -- a live callback
    left behind after the recording ends is exactly the class of bug #734
    fixed for the wake tap. `isEndpointTapActive()` checks the tap directly
    (`!!endpointProcessor`) rather than inferring teardown indirectly."""

    def test_active_while_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is False

        _start_recording(page)

        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is True

    def test_torn_down_on_manual_stop(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is True

        # force=True: the .recording class drives an infinite CSS pulse
        # animation, which fails Playwright's default actionability wait.
        page.locator("#voiceTalkBtn").click(force=True)
        page.wait_for_function("window.__recorderStopCalls === 1")

        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is False

    def test_torn_down_on_hard_cap_finalize(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is True

        page.evaluate("() => window.lifeChatVoice.finalizeEndpointing()")
        page.wait_for_function("window.__recorderStopCalls === 1")

        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is False

    def test_torn_down_on_candidate_complete_finalize(self, page: Page, chat_base_url):
        """The "submit" path -- a candidate check verdicting complete finalizes
        through the same stopRecordingAndSend() call a manual stop uses."""
        _open_voice_chat(page, chat_base_url)
        _start_recording(page)
        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is True

        result = _check_candidate(page, "Turn off the lights.")
        assert result is True
        page.wait_for_function("window.__recorderStopCalls === 1")

        assert page.evaluate("window.lifeChatVoice.isEndpointTapActive()") is False
