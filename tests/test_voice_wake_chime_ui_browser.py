"""Browser tests for the wake-confirmation chime in /chat's Listening mode.

When Listening (#710) recognizes the wake word "Hermes", `triggerWakeRecording()`
in `web/chat/voice.js` now plays one randomly-chosen sound from the bundled
set at `web/chat/wake-sounds/` (described by `web/chat/wake-sounds/manifest.json`
-- see docs/guides/voice-setup.md for the set and its attribution) before
handing off to `beginRecordingFromTap()`, the same function a talk-button tap
calls. This suite never touches the real bundled files or the real fetch of
`manifest.json` -- it stubs the manifest response and intercepts
`Audio.play()` the same way the sibling suite intercepts
`getUserMedia`/`MediaRecorder`, so it also stands in for a build that ships
without the bundled assets (the graceful-absence behavior under test).

Covers:
  - A populated manifest: a chime is attempted against a
    `/static/chat/wake-sounds/` URL, and recording only begins *after* that
    playback resolves -- never before, so the chime can't be captured as the
    user's turn.
  - Guards (baseWakeGuardsOk()) are re-checked after the chime resolves --
    state that changed during playback (e.g. Listening toggled off) still
    blocks the recording that would otherwise follow.
  - A stalled chime resolves via its ~1500ms safety timeout rather than
    hanging the wake indefinitely.
  - An absent/404 manifest and an explicitly empty `{"sounds": []}` manifest
    both fall through to today's behavior: immediate recording, no audio
    ever attempted, no error surfaced.

Like tests/test_voice_listening_wake_word_ui_browser.py, this drives the
pipeline through the real `window.lifeChatVoice.checkForWakeWord(samples,
sampleRate)` seam -- getUserMedia energy analysis can't run headless -- and
serves `web/` itself from an ephemeral port rather than a running API (see
tests/test_voice_mic_block_ui_browser.py for the same pattern), so it runs
at pre-push (`browser and not requires_server`).
"""
import http.server
import json
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


# Installed before any app JS runs. window.fetch stubs the wake-word STT
# round-trip (always matches "Hermes" unless a test overrides it) and the
# chime manifest fetch (`window.__manifestResponse`, default two fake
# entries -- `null` simulates a 404, i.e. the directory not existing at
# all). getUserMedia returns a real (silent) MediaStream so WebAudio/
# MediaRecorder accept it without touching real hardware.
#
# `Audio` is wrapped so a chime clip (URL under /static/chat/wake-sounds/)
# never actually loads a file that doesn't exist in this repo: play()
# records the attempt in `window.__audioLog` and queues a resolver in
# `window.__pendingChimeResolvers` that a test fires explicitly via
# `window.__resolveChime()` to simulate the clip's `ended` event at a time
# of the test's choosing -- letting tests assert on state *during* chime
# playback before finishing it. A chime that's never resolved this way
# exercises playWakeChime()'s own ~1500ms safety timeout instead.
_INSTRUMENT_SCRIPT = """
window.__fetchLog = [];
window.__transcribeResponse = 'hermes';
window.__manifestResponse = { sounds: ['chime-a.mp3', 'chime-b.mp3'] };

window.fetch = function (url, opts) {
  var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  window.__fetchLog.push(urlStr);
  if (urlStr.indexOf('/api/voice/transcribe') !== -1) {
    return Promise.resolve(new Response(JSON.stringify({ transcript: window.__transcribeResponse }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  }
  if (urlStr.indexOf('/static/chat/wake-sounds/manifest.json') !== -1) {
    if (window.__manifestResponse === null) {
      return Promise.resolve(new Response('Not Found', { status: 404 }));
    }
    return Promise.resolve(new Response(JSON.stringify(window.__manifestResponse), {
      status: 200, headers: { 'Content-Type': 'application/json' },
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

window.__audioLog = [];
window.__pendingChimeResolvers = [];
(function () {
  var OrigAudio = window.Audio;
  window.Audio = function (src) {
    var a = new OrigAudio();
    window.__audioLog.push(src || '');
    var isChime = typeof src === 'string' && src.indexOf('/static/chat/wake-sounds/') !== -1;
    if (isChime) {
      // Deliberately never assign the real `src` -- this repo commits no
      // real audio into the test fixture, and setting `.src` on a genuine
      // <audio> element would make the browser actually fetch it, 404 against
      // the ephemeral test server, and fire a *real* native `error` event
      // before the test ever calls window.__resolveChime(). Playback is
      // instead driven entirely by that explicit call.
      a.play = function () {
        window.__pendingChimeResolvers.push(function () { a.dispatchEvent(new Event('ended')); });
        return Promise.resolve();
      };
    } else if (src !== undefined) {
      a.src = src;
    }
    return a;
  };
  window.Audio.prototype = OrigAudio.prototype;
})();
window.__resolveChime = function () {
  var fns = window.__pendingChimeResolvers;
  window.__pendingChimeResolvers = [];
  fns.forEach(function (fn) { fn(); });
};
"""

_NO_MANIFEST_OVERRIDE = object()


def _open_voice_chat(page: Page, base_url, manifest=_NO_MANIFEST_OVERRIDE):
    """`manifest=None` simulates an absent/404 manifest (web/chat/wake-sounds/
    not present at all); a dict overrides `window.__manifestResponse` with
    that JSON; omitting it entirely keeps the instrument script's own
    default (two fake chime entries)."""
    page.add_init_script(_INSTRUMENT_SCRIPT)
    if manifest is not _NO_MANIFEST_OVERRIDE:
        # Runs after the instrument script's own init script (both are
        # add_init_script calls, applied in registration order), so this
        # simply overwrites the default before any app JS reads it.
        page.add_init_script(f"window.__manifestResponse = {json.dumps(manifest)};")
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceListen")


def _enable_listening(page: Page):
    page.locator("#voiceListen").check()
    page.wait_for_function("window.__gumCalls === 1")


def _fire_wake_check(page: Page):
    """Fire-and-forget: checkForWakeWord() may not resolve until a chime the
    test controls finishes, so this can't be a blocking `page.evaluate()`
    the way the sibling suite's `_check_wake()` is. The result lands in
    `window.__wakeResult` for tests that need it."""
    page.evaluate("""
        () => {
          window.__wakeResult = undefined;
          window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)
            .then((r) => { window.__wakeResult = r; });
        }
    """)


def _is_recording(page: Page):
    return page.evaluate(
        "document.getElementById('voiceTalkBtn').classList.contains('recording')"
    )


def _chime_attempted(page: Page):
    return page.evaluate(
        "window.__audioLog.some((u) => u && u.indexOf('/static/chat/wake-sounds/') !== -1)"
    )


class TestChimePlaysBeforeRecording:
    def test_chime_attempted_and_recording_waits_for_it(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _enable_listening(page)

        _fire_wake_check(page)

        page.wait_for_function(
            "window.__audioLog.some((u) => u && u.indexOf('/static/chat/wake-sounds/') !== -1)"
        )
        # A real chime URL, not the manifest fetch itself.
        chime_url = next(
            u for u in page.evaluate("window.__audioLog")
            if u and "/static/chat/wake-sounds/" in u
        )
        assert chime_url in (
            "/static/chat/wake-sounds/chime-a.mp3",
            "/static/chat/wake-sounds/chime-b.mp3",
        )

        # Chime is still "playing" -- recording must not have started yet.
        assert _is_recording(page) is False
        assert page.evaluate("window.__recorderStartCalls") == 0

        page.evaluate("window.__resolveChime()")

        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        assert page.evaluate("window.__recorderStartCalls") == 1
        page.wait_for_function("window.__wakeResult === true")

    def test_stalled_chime_resolves_via_safety_timeout(self, page: Page, chat_base_url):
        """Never call __resolveChime() -- playWakeChime()'s own ~1500ms cap
        must still let recording begin."""
        _open_voice_chat(page, chat_base_url)
        _enable_listening(page)

        _fire_wake_check(page)

        page.wait_for_function(
            "window.__audioLog.some((u) => u && u.indexOf('/static/chat/wake-sounds/') !== -1)"
        )
        assert _is_recording(page) is False

        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')",
            timeout=3000,
        )
        assert page.evaluate("window.__recorderStartCalls") == 1


class TestGuardsRecheckedAfterChime:
    def test_listening_toggled_off_during_chime_blocks_the_recording(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _enable_listening(page)

        _fire_wake_check(page)
        page.wait_for_function(
            "window.__audioLog.some((u) => u && u.indexOf('/static/chat/wake-sounds/') !== -1)"
        )

        page.locator("#voiceListen").uncheck()
        page.evaluate("window.__resolveChime()")

        page.wait_for_function("window.__wakeResult !== undefined")
        assert page.evaluate("window.__wakeResult") is False
        assert _is_recording(page) is False
        assert page.evaluate("window.__recorderStartCalls") == 0


class TestNoManifestIsIdentitcalToNoChime:
    def test_absent_manifest_404_records_immediately_no_audio(self, page: Page, chat_base_url):
        """Simulates a build without the bundled wake-sounds assets:
        web/chat/wake-sounds/ doesn't exist, so the manifest fetch 404s."""
        _open_voice_chat(page, chat_base_url, manifest=None)  # None -> 404 branch
        _enable_listening(page)

        result = page.evaluate(
            "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
        )

        assert result is True
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStartCalls") == 1
        assert _chime_attempted(page) is False

    def test_empty_sounds_list_records_immediately_no_audio(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, manifest={"sounds": []})
        _enable_listening(page)

        result = page.evaluate(
            "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
        )

        assert result is True
        assert _is_recording(page) is True
        assert page.evaluate("window.__recorderStartCalls") == 1
        assert _chime_attempted(page) is False

    def test_malformed_manifest_records_immediately_no_error(self, page: Page, chat_base_url):
        """Not an array under `sounds` -- must degrade to "no chime", not
        throw and break the wake."""
        _open_voice_chat(page, chat_base_url, manifest={"sounds": "not-an-array"})
        _enable_listening(page)

        errors = []
        page.on("pageerror", lambda exc: errors.append(exc))

        result = page.evaluate(
            "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
        )

        assert result is True
        assert _is_recording(page) is True
        assert _chime_attempted(page) is False
        assert errors == []
