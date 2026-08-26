"""Browser tests for the "Listening" wake-word dock toggle (#710).

Covers the fourth dock checkbox end to end at the JS layer: it renders
unchecked by default, persists like its siblings (mute/2x/auto), holds and
releases its own mic stream (never the one the talk button reuses across
taps), and its post-capture pipeline -- STT round-trip, wake-word fuzzy
match, and entering recording -- is reachable and correctly gated even
though headless Chromium can't produce a real spoken "Hermes" for the VAD's
energy analysis to detect.

The real getUserMedia energy analysis (handleListenFrame() in
web/chat/voice.js) can't run headless without genuine audio hardware, so
these tests drive the pipeline from its exported test seam,
`window.lifeChatVoice.checkForWakeWord(samples, sampleRate)` -- the same
seam pattern `submitTurn()` established for the talk-button/turn path (see
tests/test_voice_backend_parity_ui_browser.py). It is the *real* function a
captured burst reaches via finishBurst(); nothing here is a parallel
mechanism.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API (see
tests/test_voice_mic_block_ui_browser.py for the same pattern). No
`requires_server` marker, so this runs at pre-push (`browser and not
requires_server`).
"""
import http.server
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


# Installed before any app JS runs. Replaces window.fetch entirely (rather
# than intercepting at the Playwright network layer) so /api/voice/transcribe
# and /api/voice/turn/stream responses are controlled per-test via simple JS
# globals, matching tests/test_voice_backend_parity_ui_browser.py's approach.
# Also provides:
#   - A fake getUserMedia -- a real MediaStream from
#     AudioContext.createMediaStreamDestination() (silent, but a genuine live
#     audio track WebAudio/MediaRecorder both accept), with call/track-stop
#     counters so tests can assert the mic was actually requested/released
#     without touching real hardware or a browser fake-device flag.
#   - A MediaRecorder.start() call counter, so "did a second recording start"
#     is directly observable rather than inferred.
#   - An HTMLMediaElement.play() wrapper counting concurrent real playback
#     (playing -> ended/pause), so a test can wait for a stubbed TTS clip to
#     be genuinely, audibly in flight before asserting suspension.
_INSTRUMENT_SCRIPT = """
window.__fetchLog = [];
window.__transcribeResponse = 'hermes';
window.__turnClipUrl = null;

window.fetch = function (url, opts) {
  var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  window.__fetchLog.push(urlStr);
  if (urlStr.indexOf('/api/voice/transcribe') !== -1) {
    return Promise.resolve(new Response(JSON.stringify({ transcript: window.__transcribeResponse }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  }
  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    var sse = 'data: ' + JSON.stringify({ type: 'main_audio', url: window.__turnClipUrl }) + '\\n\\n'
            + 'data: ' + JSON.stringify({ type: 'done', data: { response_text: 'ok' } }) + '\\n\\n';
    return Promise.resolve(new Response(sse, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' },
    }));
  }
  if (urlStr.indexOf('/api/chat/config') !== -1) {
    // Auto ships on by default and several tests here open a real
    // (fake-stream) recording -- the idle timeout (#723) runs off a genuine
    // onaudioprocess tap ticking on real wall-clock time, unrelated to
    // anything this test drives directly. An hour keeps it from ever
    // firing mid-test on a slow/loaded run.
    return Promise.resolve(new Response(JSON.stringify({ voice_idle_timeout_ms: 3600000 }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  }
  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
};

window.__gumCalls = 0;
window.__stopCalls = 0;
window.__lastGumConstraints = null;
navigator.mediaDevices.getUserMedia = function (constraints) {
  window.__gumCalls += 1;
  window.__lastGumConstraints = constraints;
  var ctx = new (window.AudioContext || window.webkitAudioContext)();
  var dest = ctx.createMediaStreamDestination();
  var stream = dest.stream;
  stream.getTracks().forEach(function (track) {
    var origStop = track.stop.bind(track);
    track.stop = function () { window.__stopCalls += 1; origStop(); };
  });
  return Promise.resolve(stream);
};

(function () {
  window.__recorderStartCalls = 0;
  var OrigMR = window.MediaRecorder;
  function WrappedMR(stream, opts) {
    var mr = opts ? new OrigMR(stream, opts) : new OrigMR(stream);
    var origStart = mr.start.bind(mr);
    mr.start = function (ms) { window.__recorderStartCalls += 1; return origStart(ms); };
    return mr;
  }
  WrappedMR.isTypeSupported = OrigMR.isTypeSupported.bind(OrigMR);
  window.MediaRecorder = WrappedMR;
})();

// Overrides AudioContext.prototype's `state`/suspend()/resume() with a
// JS-tracked stand-in, defaulting new contexts to 'running' (#734). Real
// Chrome autoplay policy suspends a context created with no prior user
// gesture on the page until later explicitly resumed or auto-unlocked by
// the browser's per-origin media-engagement heuristics (accumulated real
// usage history a fresh/headless browser never has) -- neither of those
// apply to a synthetic Playwright session, so a *real* listenAudioCtx here
// would start (and stay) 'suspended' regardless of the fix under test,
// which is the wrong baseline: the bug this suite tests is about a tap
// that IS running contending with playback, matching the real device the
// issue was filed from. The override only changes what `.state` reports
// and what suspend()/resume() toggle -- createScriptProcessor/
// createMediaStreamSource/etc. all still hit the real implementation, so
// the live onaudioprocess graph keeps working exactly as before.
(function () {
  window.__ctxSuspendCalls = 0;
  window.__ctxResumeCalls = 0;
  var proto = (window.AudioContext || window.webkitAudioContext).prototype;
  var stateMap = new WeakMap();
  Object.defineProperty(proto, 'state', {
    configurable: true,
    get: function () { return stateMap.has(this) ? stateMap.get(this) : 'running'; },
  });
  proto.suspend = function () {
    window.__ctxSuspendCalls += 1;
    stateMap.set(this, 'suspended');
    return Promise.resolve();
  };
  proto.resume = function () {
    window.__ctxResumeCalls += 1;
    stateMap.set(this, 'running');
    return Promise.resolve();
  };
})();

(function () {
  window.__audioPlayingCount = 0;
  var proto = HTMLMediaElement.prototype;
  var origPlay = proto.play;
  proto.play = function () {
    if (!this.__wired710) {
      this.__wired710 = true;
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


def _open_voice_chat(page: Page, base_url):
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceListen")


def _check_wake(page: Page, transcript):
    """Sets the stubbed /api/voice/transcribe response, then runs the real
    post-capture pipeline (STT call -> match -> maybe trigger) with a
    throwaway synthetic clip -- content doesn't matter, the network response
    is stubbed regardless. Returns whether it triggered a recording."""
    page.evaluate("(t) => { window.__transcribeResponse = t; }", transcript)
    return page.evaluate(
        "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
    )


def _is_recording(page: Page):
    return page.evaluate(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


class TestListeningToggleBasics:
    """Renders checked by default and persists like its siblings."""

    def test_default_is_on(self, page: Page, chat_base_url):
        """Listening ships on, alongside Auto and 2x, so voice mode is
        conversational without touching the dock. Mute is the one dock
        toggle that stays off by default."""
        _open_voice_chat(page, chat_base_url)
        expect(page.locator("#voiceListen")).to_be_checked()
        expect(page.locator("#voiceAuto")).to_be_checked()
        expect(page.locator("#voiceFast")).to_be_checked()
        expect(page.locator("#voiceMute")).not_to_be_checked()

    def test_unchecking_persists_across_reload(self, page: Page, chat_base_url):
        """A stored choice beats the default in both directions — opting out
        of Listening has to survive a reload, or the default would silently
        re-enable the mic hold on every visit."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").uncheck()

        stored = page.evaluate(
            "JSON.parse(window.localStorage.getItem('lifeos:chat:dock_settings') || '{}')"
        )
        assert stored.get("listen") is False

        page.reload()
        page.wait_for_selector("#voiceListen")

        expect(page.locator("#voiceListen")).not_to_be_checked()


class TestListeningMicLifecycle:
    """Only meaningful in voice mode; releases the mic entirely on
    toggle-off or on leaving voice mode.

    #740 note: the constraints assertions below check `__lastGumConstraints`
    -- the object actually passed to `navigator.mediaDevices.getUserMedia()`
    -- rather than reading the resulting track's `getConstraints()`/
    `getSettings()`. Verified empirically (real Chromium, not a guess): a
    track from `AudioContext.createMediaStreamDestination()` -- what the fake
    `getUserMedia()` stub above returns, since headless Chromium has no real
    microphone to hand back a genuine device track -- reports
    `getConstraints() === {}` and a `getSettings()` with only generic
    WebAudio fields (channelCount/deviceId/latency/sampleRate/sampleSize),
    none of echoCancellation/noiseSuppression/autoGainControl, regardless of
    what was requested. A synthetic stream never had real device constraints
    applied to it in the first place, so asserting on it would test nothing;
    the call-site object is the only place this suite can meaningfully
    observe what was requested."""

    def test_entering_voice_mode_requests_mic_with_the_default_on(
            self, page: Page, chat_base_url):
        """Listening is on by default, so the mic hold is acquired by entering
        voice mode itself — no dock click required. #740: the wake stream is
        requested with echoCancellation/noiseSuppression/autoGainControl all
        explicitly off — unlike the plain `{ audio: true }` the recording
        path still uses (see WAKE_STREAM_CONSTRAINTS in voice.js)."""
        _open_voice_chat(page, chat_base_url)
        page.wait_for_function("window.__gumCalls === 1")

        assert page.evaluate("window.__lastGumConstraints") == {
            "audio": {
                "echoCancellation": False,
                "noiseSuppression": False,
                "autoGainControl": False,
            }
        }

    def test_re_enabling_after_opting_out_requests_mic_again(
            self, page: Page, chat_base_url):
        """The enable transition still acquires a fresh hold after a user has
        turned Listening off — the path a non-default user takes."""
        _open_voice_chat(page, chat_base_url)
        page.wait_for_function("window.__gumCalls === 1")

        page.locator("#voiceListen").uncheck()
        page.wait_for_function("window.__stopCalls > 0")

        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 2")

        assert page.evaluate("window.__lastGumConstraints") == {
            "audio": {
                "echoCancellation": False,
                "noiseSuppression": False,
                "autoGainControl": False,
            }
        }

    def test_leaving_voice_mode_stops_the_tracks(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")
        assert page.evaluate("window.__stopCalls") == 0

        page.locator("#modeTextBtn").click()  # leave voice mode

        page.wait_for_function("window.__stopCalls > 0")

    def test_unchecking_stops_the_tracks(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        page.locator("#voiceListen").uncheck()

        page.wait_for_function("window.__stopCalls > 0")

    def test_reentering_voice_mode_reacquires_the_mic(self, page: Page, chat_base_url):
        """Toggle stays on; the mic hold follows voice mode, not a fresh click."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        page.locator("#modeTextBtn").click()
        page.wait_for_function("window.__stopCalls > 0")

        page.locator("#modeVoiceBtn").click()

        page.wait_for_function("window.__gumCalls === 2")

    def test_leaving_voice_mode_releases_the_talk_buttons_mic_too(
            self, page: Page, chat_base_url):
        """#724: `micStream` -- the talk button's own stream, acquired lazily
        by requestMicInGesture() the first time it's needed -- used to never
        be released at all: applyVoiceMode() only ever tore down Listening's
        separate hold on leaving voice mode, so once a session had recorded
        even once the mic stayed live regardless of mode for the rest of the
        page's life. Verified with Listening off throughout so the only live
        stream in play is the record path's own -- isolates this from
        TestListeningMicLifecycle's other tests, which are all about
        Listening's stream instead."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").uncheck()
        page.wait_for_function("window.__stopCalls > 0")  # Listening's own hold releasing
        stop_calls_before = page.evaluate("window.__stopCalls")

        page.locator("#voiceTalkBtn").click()
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        # force=True: the .recording class drives an infinite CSS pulse that
        # can fail Playwright's default actionability "element is stable"
        # wait on the stop tap (see _tap_talk() in the sibling suite).
        page.locator("#voiceTalkBtn").click(force=True)  # stop
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        # Stopping the recorder itself never touches the stream's tracks.
        assert page.evaluate("window.__stopCalls") == stop_calls_before

        page.locator("#modeTextBtn").click()  # leave voice mode

        page.wait_for_function(f"window.__stopCalls > {stop_calls_before}")


class TestListeningWakeMatch:
    """A stubbed STT response drives the real match/trigger pipeline."""

    def test_matching_transcript_triggers_the_talk_button_recording_path(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")
        assert _is_recording(page) is False

        triggered = _check_wake(page, "Hermes")

        assert triggered is True
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStartCalls") == 1

    @pytest.mark.parametrize(
        "transcript",
        ["hermes", "HERMES", "Hermès", "Hermes,", "her mes", "Hermie's"],
    )
    def test_whisper_isms_still_match(self, page: Page, chat_base_url, transcript):
        """Accents, case, trailing punctuation, a stray mid-word space, and a
        one-letter-off mishear -- see matchesWakeWord()'s docstring in
        web/chat/voice.js. A fresh `page` per case (pytest-playwright's
        default function scope) so triggering one doesn't leave the module's
        `isRecording` state suspending the next."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        assert _check_wake(page, transcript) is True

    def test_non_matching_transcript_does_not_trigger(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        triggered = _check_wake(page, "thermostat")

        assert triggered is False
        assert _is_recording(page) is False
        assert page.evaluate("window.__recorderStartCalls") == 0

    def test_unrelated_word_containing_a_similar_run_does_not_trigger(
            self, page: Page, chat_base_url):
        """Control on the edit-distance tolerance -- a transcript with no
        word actually close to "hermes" must not slip through."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        triggered = _check_wake(page, "turn on the lights please")

        assert triggered is False


class TestListeningSuspension:
    """Detection is suspended while recording, while a turn is in flight, and
    while TTS is playing -- and resumes cleanly afterward."""

    def test_suspended_while_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        # A real recording, started the same way a talk-button tap would.
        page.locator("#voiceTalkBtn").click()
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        assert page.evaluate("window.__recorderStartCalls") == 1

        triggered = _check_wake(page, "Hermes")

        assert triggered is False, "a wake match fired while a manual recording was in progress"
        # Still just the one recording -- not restarted/duplicated.
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_suspended_while_tts_is_playing_and_resumes_after(self, page: Page, chat_base_url):
        """Isolates the TTS self-trigger guard. Auto is switched off here on
        purpose: with Auto on (the default) auto-continue starts recording the
        instant playback ends, which legitimately keeps wake detection
        suspended — that interaction is pinned separately below."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")

        clip_url = page.evaluate("window.__makeWav(500)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        # Fire-and-forget: submitTurn() itself isn't under test here, just the
        # real audio playback it kicks off.
        page.evaluate("() => { window.lifeChatVoice.submitTurn({ transcript: 'hi' }); }")
        page.wait_for_function("window.__audioPlayingCount > 0")

        triggered = _check_wake(page, "Hermes")

        assert triggered is False, "a wake match fired while the assistant's own reply was playing"
        assert page.evaluate("window.__recorderStartCalls") == 0

        # Resumes once playback ends -- not suspended forever.
        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        triggered_after = _check_wake(page, "Hermes")

        assert triggered_after is True, "Listening did not resume after TTS playback ended"
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_wake_tap_graph_itself_suspends_during_playback_and_resumes_after(
            self, page: Page, chat_base_url):
        """#734: the wake tap's callback must actually stop firing while a
        clip plays, not merely have its output ignored (the old bug --
        canDetectWake() already returned False here, but the still-connected
        ScriptProcessorNode kept its onaudioprocess callback running on the
        main thread, contending with playback). isListenTapRunning() checks
        the graph's own AudioContext.state directly, and __gumCalls/
        __stopCalls confirm the suspend/resume cycle never touches the mic
        permission -- no second getUserMedia, no track stop."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()  # isolate from auto-continue, as above
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")
        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === true")

        clip_url = page.evaluate("window.__makeWav(500)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        page.evaluate("() => { window.lifeChatVoice.submitTurn({ transcript: 'hi' }); }")
        page.wait_for_function("window.__audioPlayingCount > 0")

        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === false")
        # The mic hold itself is untouched by the suspend -- no re-prompt, no
        # second acquisition, no track stop.
        assert page.evaluate("window.__gumCalls") == 1
        assert page.evaluate("window.__stopCalls") == 0

        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === true")
        assert page.evaluate("window.__gumCalls") == 1
        assert page.evaluate("window.__stopCalls") == 0

        # And detection genuinely works again, not just the graph being
        # nominally "running" -- a real wake match still triggers recording.
        triggered = _check_wake(page, "Hermes")
        assert triggered is True
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_wake_track_itself_disables_during_playback_and_reenables_after(
            self, page: Page, chat_base_url):
        """#740: #734 believed listenAudioCtx.suspend() (above) fully
        deactivated the wake tap during playback, but suspend() only stops
        the graph's *processing* -- the underlying MediaStreamTrack keeps
        capturing regardless, which is the actual mechanism behind the
        popping that persisted after #734 shipped (confirmed on real
        hardware: it correlates exactly with the Listening toggle).
        isListenTrackEnabled() checks the wake stream's own track.enabled
        state directly -- independent of, and in addition to, the context
        suspend/resume this suite already covers -- and __gumCalls/
        __stopCalls confirm disabling the track never touches the mic
        permission: no second getUserMedia, no track stop, so no re-prompt
        across the cycle."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()  # isolate from auto-continue, as above
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")
        page.wait_for_function("() => window.lifeChatVoice.isListenTrackEnabled() === true")

        clip_url = page.evaluate("window.__makeWav(500)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        page.evaluate("() => { window.lifeChatVoice.submitTurn({ transcript: 'hi' }); }")
        page.wait_for_function("window.__audioPlayingCount > 0")

        page.wait_for_function("() => window.lifeChatVoice.isListenTrackEnabled() === false")
        assert page.evaluate("window.__gumCalls") == 1
        assert page.evaluate("window.__stopCalls") == 0

        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        page.wait_for_function("() => window.lifeChatVoice.isListenTrackEnabled() === true")
        assert page.evaluate("window.__gumCalls") == 1
        assert page.evaluate("window.__stopCalls") == 0

        # Wake detection genuinely works again after the track re-enables --
        # not just the flag flipping back.
        triggered = _check_wake(page, "Hermes")
        assert triggered is True
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_wake_tap_graph_suspends_during_recording_and_resumes_after(
            self, page: Page, chat_base_url):
        """#724: the same main-thread-contention bug #734 fixed for TTS
        playback also applied to the recording window -- canDetectWake()'s
        own `!isRecording` guard already made detection a no-op the entire
        time a recording was in progress, but `listenProcessor` itself
        stayed connected and running regardless, contending with the
        recorder's/endpointer's own taps for nothing. Same isListenTapRunning()
        seam as the playback-suspension test above, this time driven by a
        real recording started the same way a talk-button tap would."""
        _open_voice_chat(page, chat_base_url)
        page.locator("#voiceAuto").uncheck()  # isolate from auto-continue
        page.locator("#voiceListen").check()
        page.wait_for_function("window.__gumCalls === 1")
        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === true")

        page.locator("#voiceTalkBtn").click()
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )

        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === false")
        # The record path's own (separate, #724-documented) stream acquisition
        # -- Listening's own hold is untouched, no re-prompt, no track stop.
        assert page.evaluate("window.__gumCalls") == 2
        assert page.evaluate("window.__stopCalls") == 0

        # force=True: the .recording class drives an infinite CSS pulse that
        # can fail Playwright's default actionability "element is stable"
        # wait on the stop tap (see _tap_talk() in the sibling suite).
        page.locator("#voiceTalkBtn").click(force=True)  # stop
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )

        page.wait_for_function("() => window.lifeChatVoice.isListenTapRunning() === true")
        assert page.evaluate("window.__gumCalls") == 2
        assert page.evaluate("window.__stopCalls") == 0

        # And detection genuinely works again -- a real wake match still
        # triggers a second recording.
        triggered = _check_wake(page, "Hermes")
        assert triggered is True
        assert page.evaluate("window.__recorderStartCalls") == 2

    def test_auto_continue_takes_over_after_tts_instead_of_the_wake_word(
            self, page: Page, chat_base_url):
        """With both defaults on, the two features don't fight: when a reply
        finishes, Auto is already recording the next utterance, so a wake match
        is correctly suppressed rather than restarting or duplicating it. The
        wake word is for re-entering an idle conversation, not for continuing
        an active one."""
        _open_voice_chat(page, chat_base_url)
        expect(page.locator("#voiceAuto")).to_be_checked()
        expect(page.locator("#voiceListen")).to_be_checked()
        page.wait_for_function("window.__gumCalls === 1")

        clip_url = page.evaluate("window.__makeWav(500)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        page.evaluate("() => { window.lifeChatVoice.submitTurn({ transcript: 'hi' }); }")
        page.wait_for_function("window.__audioPlayingCount > 0")
        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        # Auto-continue owns the mic now.
        page.wait_for_function("window.__recorderStartCalls === 1", timeout=5000)
        assert _is_recording(page) is True

        triggered = _check_wake(page, "Hermes")

        assert triggered is False, "a wake match fired while auto-continue was already recording"
        assert page.evaluate("window.__recorderStartCalls") == 1
