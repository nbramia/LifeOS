"""Browser tests for a manual talk-button stop in Auto mode (#721).

Bug: in Auto mode, tapping the live record button to stop recording
immediately restarted it. `stopRecordingAndSend()` in `web/chat/voice.js`
routes an empty/silent recording (the only outcome a real tap-to-stop
produces against these tests' silent fake mic stream -- see below) through
`handleSkippedEmptyRecording()`, which used to call `maybeAutoContinue()`
itself, treating "the user tapped stop" the same as "a turn was submitted and
its reply finished playing". That's the only caller of
`stopRecordingAndSend()` (onTalkClick's stop branch), so every empty/silent
recording it produces is, by construction, a manual stop -- there is no
other way to reach it. The fix drops that call: auto-continue's only re-arm
trigger is now `submitTurn()`'s own `maybeAutoContinue()` call after
`await playbackChain` (i.e. once a turn was actually submitted and its reply
finished playing), which this suite also exercises to confirm a later cycle
still re-arms normally after a manual stop.

Like tests/test_voice_listening_wake_word_ui_browser.py and
tests/test_voice_wake_chime_ui_browser.py, this drives recording start/stop
through the real `#voiceTalkBtn` with a fake (silent, but genuinely live)
`getUserMedia` stream and a wrapped `MediaRecorder`, and drives turn
completion through the real `window.lifeChatVoice.submitTurn()` seam
(getUserMedia energy analysis and a real recorded voice can't run headless).
Serves `web/` itself from an ephemeral port rather than a running API (see
tests/test_voice_mic_block_ui_browser.py), so it runs at pre-push (`browser
and not requires_server`).
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


# Installed before any app JS runs. Replaces window.fetch entirely (rather
# than intercepting at the Playwright network layer) so /api/voice/transcribe
# and /api/voice/turn/stream responses are controlled per-test via simple JS
# globals -- same approach as tests/test_voice_listening_wake_word_ui_browser.py.
# Also provides:
#   - A fake getUserMedia -- a real MediaStream from
#     AudioContext.createMediaStreamDestination() (silent, but a genuine live
#     audio track WebAudio/MediaRecorder both accept), so a real tap-to-stop
#     always produces a silent/empty recording -- exactly the outcome this
#     bug is about.
#   - A MediaRecorder.start() call counter, so "did recording restart" is
#     directly observable rather than inferred.
#   - An HTMLMediaElement.play() wrapper counting concurrent real playback
#     (playing -> ended/pause), so a test can wait for a stubbed TTS clip to
#     finish before asserting whether auto-continue re-armed.
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

(function () {
  window.__audioPlayingCount = 0;
  var proto = HTMLMediaElement.prototype;
  var origPlay = proto.play;
  proto.play = function () {
    if (!this.__wired721) {
      this.__wired721 = true;
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


def _dock_baseline(*, auto, listen=False, fast=False, mute=False):
    """A page-load init script pinning an explicit dock baseline -- the
    shipped defaults (2x/Auto/Listening all on) would otherwise inject an
    unrelated wake-word mic hold or shift auto/listen state a given test
    isn't about. Mirrors _DOCK_BASELINE in
    tests/test_voice_rate_toggle_playback_ui_browser.py, parameterized on
    `auto`/`listen` since this suite's whole point is auto-mode behavior."""
    settings = {"mute": mute, "auto": auto, "fast": fast, "listen": listen}
    return (
        "try { window.localStorage.setItem('lifeos:chat:dock_settings', "
        + json.dumps(json.dumps(settings))
        + "); } catch (e) {}"
    )


def _open_voice_chat(page: Page, base_url, *, auto, listen=False):
    page.add_init_script(_dock_baseline(auto=auto, listen=listen))
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")


def _is_recording(page: Page):
    return page.evaluate(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


def _tap_talk(page: Page):
    # force=True: the .recording class drives an infinite CSS pulse
    # (scale animation) on this button, which fails Playwright's default
    # actionability "element is stable" wait on the stop tap.
    page.locator("#voiceTalkBtn").click(force=True)


def _submit_turn(page: Page, transcript="hi"):
    """Fire-and-forget submitTurn() through the exported test seam -- the
    same one tests/test_voice_listening_wake_word_ui_browser.py uses to drive
    a completed turn + reply without a real recorded voice."""
    page.evaluate(
        "(t) => { window.lifeChatVoice.submitTurn({ transcript: t }); }", transcript
    )


class TestManualStopDoesNotRestart:
    """A manual tap-to-stop must stop recording and stay stopped -- not
    immediately restart it -- while Auto is on."""

    def test_manual_stop_does_not_restart_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=True)

        _tap_talk(page)  # start
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        assert page.evaluate("window.__recorderStartCalls") == 1

        _tap_talk(page)  # manual stop -- the fake stream is silent, so this
        # is the empty-recording discard path (handleSkippedEmptyRecording())

        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        # Settle well past MIN_RECORD_MS (300ms) and give a would-be restart
        # every chance to fire before asserting it didn't.
        page.wait_for_timeout(600)

        assert _is_recording(page) is False
        assert page.evaluate("window.__recorderStartCalls") == 1, (
            "recording restarted after a manual stop in Auto mode"
        )

    def test_manual_stop_does_not_submit_a_turn(self, page: Page, chat_base_url):
        """Existing discard semantics for an empty/silent recording are
        unchanged by this fix -- a manual stop with nothing captured must
        not hit /api/voice/turn/stream."""
        _open_voice_chat(page, chat_base_url, auto=True)

        _tap_talk(page)
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        _tap_talk(page)
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        page.wait_for_timeout(600)

        log = page.evaluate("window.__fetchLog")
        assert not any("/api/voice/turn/stream" in u for u in log)


class TestAutoToggleUnaffectedByManualStop:
    """The Auto toggle itself -- checked state and persistence -- must not
    be touched by a manual stop; only the re-arm timing changes."""

    def test_auto_toggle_stays_checked_and_persisted(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=True)
        expect(page.locator("#voiceAuto")).to_be_checked()

        _tap_talk(page)
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        _tap_talk(page)
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        page.wait_for_timeout(600)

        expect(page.locator("#voiceAuto")).to_be_checked()
        stored = page.evaluate(
            "JSON.parse(window.localStorage.getItem('lifeos:chat:dock_settings') || '{}')"
        )
        assert stored.get("auto") is True


class TestCycleAfterManualStopStillReArms:
    """A manual stop suppresses re-arm for that cycle only -- a later,
    genuinely completed turn must still re-arm Auto normally."""

    def test_full_cycle_after_manual_stop_rearms_on_reply_finish(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=True)

        _tap_talk(page)  # start
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        _tap_talk(page)  # manual stop -- silent, discarded, no re-arm
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        page.wait_for_timeout(600)
        assert page.evaluate("window.__recorderStartCalls") == 1

        # A later, natural cycle: a turn is submitted and its reply plays.
        clip_url = page.evaluate("window.__makeWav(300)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        _submit_turn(page)
        page.wait_for_function("window.__audioPlayingCount > 0")
        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        # Auto-continue re-arms once the reply finishes playing.
        page.wait_for_function("window.__recorderStartCalls === 2", timeout=5000)
        assert _is_recording(page) is True


class TestAutoOffPathUnchanged:
    """With Auto off, neither a manual stop nor a completed turn should ever
    restart recording -- unaffected by this fix either way."""

    def test_manual_stop_with_auto_off_does_not_restart(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=False)
        expect(page.locator("#voiceAuto")).not_to_be_checked()

        _tap_talk(page)
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        _tap_talk(page)
        page.wait_for_function(
            "!document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        page.wait_for_timeout(600)

        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_completed_turn_with_auto_off_does_not_rearm(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=False)

        clip_url = page.evaluate("window.__makeWav(300)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        _submit_turn(page)
        page.wait_for_function("window.__audioPlayingCount > 0")
        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        page.wait_for_timeout(300)
        assert page.evaluate("window.__recorderStartCalls") == 0
        assert _is_recording(page) is False


class TestWakeTriggeredTurnStillReArms:
    """The wake word is not a manual stop -- once a wake-triggered turn's
    reply finishes playing, Auto-continue re-arms exactly as it does for a
    tap-started turn."""

    def test_wake_triggers_recording_and_rearms_after_reply(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, auto=True, listen=True)
        page.wait_for_function("window.__gumCalls === 1")  # Listening's own mic hold

        triggered = page.evaluate(
            "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
        )
        assert triggered is True
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStartCalls") == 1

        # The turn that recording produces, completing with a spoken reply --
        # driven through the same submitTurn() seam as the sibling Listening
        # suite (a real recorded voice can't run headless).
        clip_url = page.evaluate("window.__makeWav(300)")
        page.evaluate("(u) => { window.__turnClipUrl = u; }", clip_url)
        _submit_turn(page)
        page.wait_for_function("window.__audioPlayingCount > 0")
        page.wait_for_function("window.__audioPlayingCount === 0", timeout=5000)

        page.wait_for_function("window.__recorderStartCalls === 2", timeout=5000)
        assert _is_recording(page) is True
