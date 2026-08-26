"""Browser tests for the wake-confirmation chime in /chat's Listening mode.

When Listening (#710) recognizes the wake word "Hermes", `triggerWakeRecording()`
in `web/chat/voice.js` now plays one randomly-chosen sound from the bundled
set at `web/chat/wake-sounds/` (described by `web/chat/wake-sounds/manifest.json`
-- see docs/guides/voice-setup.md for the set and its attribution) before
handing off to `beginRecordingFromTap()`, the same function a talk-button tap
calls. This suite never touches the real bundled files or the real fetch of
`manifest.json` -- it stubs the manifest response (the repo now ships real
audio under `web/chat/wake-sounds/`, but this suite keeps using fake
filenames for determinism) and intercepts `HTMLMediaElement`'s `src`/`play()`
at the prototype level, so it also stands in for a build that ships without
the bundled assets (the graceful-absence behavior under test).

#725: the chime used to always play via a fresh `new Audio()` -- an element
never unlocked by a user gesture, which iOS/Android's autoplay policy
silently blocks. `playWakeChime()` now branches on `useSharedTtsAudio()`:
mobile plays through the same shared, gesture-unlocked `<audio>` element
(`getTtsAudioElement()`) every other non-gesture voice playback already
uses, via `playUrlOnElement()`; desktop keeps the original fresh-element
path. The `src`/`play()` interception below is prototype-level (not a
constructor wrap alone) specifically so it catches both: the shared
element's `.src` is assigned directly, not via `new Audio(url)`.

Covers:
  - A populated manifest: a chime is attempted against a
    `/static/chat/wake-sounds/` URL, and recording only begins *after* that
    playback resolves -- never before, so the chime can't be captured as the
    user's turn.
  - Guards (baseWakeGuardsOk()) are re-checked after the chime resolves --
    state that changed during playback (e.g. Listening toggled off) still
    blocks the recording that would otherwise follow.
  - A stalled chime resolves via its ~1500ms safety timeout rather than
    hanging the wake indefinitely -- on both the desktop and shared-element
    paths, since they're separate code (playWakeChimeStandalone() vs.
    playWakeChimeShared()).
  - An absent/404 manifest and an explicitly empty `{"sounds": []}` manifest
    both fall through to today's behavior: immediate recording, no audio
    ever attempted, no error surfaced.
  - Mobile (Android UA): the chime plays on the *same* element
    `unlockTtsAudio()` already constructed and unlocked, not a second, still
    -locked one.
  - Desktop (default UA): unchanged -- no shared/unlock element is ever
    touched; the chime still gets its own fresh `<audio>`.
  - The #608 regression guard: a wake match while a real TTS clip is still
    in flight on the shared element must not let the chime steal it (and
    the real clip must finish undisturbed).

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

ANDROID_UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"


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


# These suites measure chime/recording behavior, not dock configuration. The
# shipped dock defaults (2x/Auto/Listening on) would otherwise inject an
# auto-continue re-record after a real submitTurn() (used by the
# clip-in-flight guard test below) and shift what "no chime attempted" means.
# Mirrors tests/test_voice_rate_toggle_playback_ui_browser.py's own baseline
# for the same reason. `_enable_listening()` below flips the checkbox itself
# rather than relying on a shipped `listen: true` default.
_DOCK_BASELINE = (
    "try { window.localStorage.setItem('lifeos:chat:dock_settings', "
    "JSON.stringify({ mute: false, auto: false, fast: false, listen: false })); } "
    "catch (e) {}"
)

# Installed before any app JS runs. window.fetch stubs the wake-word STT
# round-trip (always matches "Hermes" unless a test overrides it), the chime
# manifest fetch (`window.__manifestResponse`, default two fake entries --
# `null` simulates a 404, i.e. the directory not existing at all), and the
# voice-turn SSE stream (`window.__mockAudio`, a one-shot-per-turn queue --
# used only by the clip-in-flight guard test, harmless elsewhere). getUserMedia
# returns a real (silent) MediaStream so WebAudio/MediaRecorder accept it
# without touching real hardware.
#
# Audio interception is at the `HTMLMediaElement.prototype` level (`src`
# setter + `play()`), not just a wrapped `Audio` constructor -- #725's mobile
# path assigns `.src` directly on a *reused* element (`getTtsAudioElement()`),
# it doesn't construct a fresh one per clip the way desktop and the old code
# did. A src assignment under `/static/chat/wake-sounds/` never hits the real
# network (this repo's fixture doesn't ship files matching the fake names
# below, and no real audio decode needs to happen for these tests): it's
# logged to `window.__audioLog`, marked pending, and the element's
# `readyState` is shadowed to report itself immediately decodable, so
# `playUrlOnElement()`'s synchronous-vs-`canplaythrough` branch doesn't wait
# on an event this stub will never fire. A test resolves a pending chime's
# `play()` (a promise that never settles on its own) via
# `window.__resolveChime()`, simulating the clip's `ended` event at a time of
# the test's choosing. Non-chime `src` assignments (the shared element's
# SILENT_WAV unlock ping, and the guard test's real WAV clips) pass straight
# through to native `src`/`play()` -- real (silent, local data-URI) playback,
# real `ended` timing.
_INSTRUMENT_SCRIPT = """
window.__fetchLog = [];
window.__transcribeResponse = 'hermes';
window.__manifestResponse = { sounds: ['chime-a.mp3', 'chime-b.mp3'] };
window.__mockAudio = [];

window.__makeWav = function (ms, sampleRate) {
  sampleRate = sampleRate || 8000;
  var n = Math.round((ms / 1000) * sampleRate);
  var dataBytes = n * 2;
  var buf = new ArrayBuffer(44 + dataBytes);
  var view = new DataView(buf);
  var writeStr = function (offset, str) {
    for (var i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  };
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
  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    var cfg = window.__mockAudio.shift() || { ms: 100, tag: 'default' };
    var clipUrl = window.__makeWav(cfg.ms) + '#' + cfg.tag;
    var sse = 'data: ' + JSON.stringify({ type: 'main_audio', url: clipUrl }) + '\\n\\n';
    sse += 'data: ' + JSON.stringify({ type: 'done', data: { response_text: 'ok ' + cfg.tag } }) + '\\n\\n';
    return Promise.resolve(new Response(sse, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
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

window.__audioLog = [];             // {id, src, chime} for every `.src` assignment
window.__constructedAudioIds = [];  // one entry per `new Audio()` call, in order
window.__playLog = [];              // {id, src, chime} for every play() call
window.__pendingChimeResolvers = [];

(function () {
  var OrigAudio = window.Audio;
  var nextId = 0;
  window.Audio = function (src) {
    var el = new OrigAudio();
    el.__id = ++nextId;
    window.__constructedAudioIds.push(el.__id);
    if (src !== undefined) el.src = src;
    return el;
  };
  window.Audio.prototype = OrigAudio.prototype;

  var proto = HTMLMediaElement.prototype;
  var srcDesc = Object.getOwnPropertyDescriptor(proto, 'src');
  Object.defineProperty(proto, 'src', {
    get: function () { return srcDesc.get.call(this); },
    set: function (v) {
      var isChime = typeof v === 'string' && v.indexOf('/static/chat/wake-sounds/') !== -1;
      this.__pendingChime = isChime;
      window.__audioLog.push({ id: this.__id, src: v || '', chime: isChime });
      if (isChime) {
        try {
          Object.defineProperty(this, 'readyState', {
            configurable: true, value: HTMLMediaElement.HAVE_ENOUGH_DATA,
          });
        } catch (e) { /* ignore */ }
      } else {
        try { delete this.readyState; } catch (e) { /* ignore */ }
        srcDesc.set.call(this, v);
      }
    },
  });

  var origPlay = proto.play;
  proto.play = function () {
    var chime = !!this.__pendingChime;
    window.__playLog.push({ id: this.__id, src: this.src, chime: chime });
    if (chime) {
      var self = this;
      return new Promise(function (resolve) {
        window.__pendingChimeResolvers.push(function () {
          self.dispatchEvent(new Event('ended'));
          resolve();
        });
      });
    }
    return origPlay.call(this);
  };
})();

window.__resolveChime = function () {
  var fns = window.__pendingChimeResolvers;
  window.__pendingChimeResolvers = [];
  fns.forEach(function (fn) { fn(); });
};
"""

_NO_MANIFEST_OVERRIDE = object()


def _open_voice_chat(page: Page, base_url, manifest=_NO_MANIFEST_OVERRIDE, android=False):
    """`manifest=None` simulates an absent/404 manifest (web/chat/wake-sounds/
    not present at all); a dict overrides `window.__manifestResponse` with
    that JSON; omitting it entirely keeps the instrument script's own
    default (two fake chime entries). `android=True` sets a mobile UA so
    `useSharedTtsAudio()` takes the shared-element path (#725)."""
    if android:
        page.add_init_script(
            "Object.defineProperty(navigator, 'userAgent', { value: %r });" % ANDROID_UA
        )
    page.add_init_script(_DOCK_BASELINE)
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
    return page.evaluate("window.__audioLog.some((e) => e.chime)")


class TestChimePlaysBeforeRecording:
    def test_chime_attempted_and_recording_waits_for_it(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _enable_listening(page)

        _fire_wake_check(page)

        page.wait_for_function("window.__audioLog.some((e) => e.chime)")
        # A real chime URL, not the manifest fetch itself.
        chime_url = next(
            e["src"] for e in page.evaluate("window.__audioLog") if e["chime"]
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
        """Never call __resolveChime() -- playWakeChimeStandalone()'s own
        ~1500ms cap must still let recording begin."""
        _open_voice_chat(page, chat_base_url)
        _enable_listening(page)

        _fire_wake_check(page)

        page.wait_for_function("window.__audioLog.some((e) => e.chime)")
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
        page.wait_for_function("window.__audioLog.some((e) => e.chime)")

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


class TestMobileSharedElement:
    """#725: on iOS/Android the chime must route through the shared,
    gesture-unlocked `<audio>` element (getTtsAudioElement()) instead of a
    fresh `new Audio()` -- a fresh element is never unlocked, so mobile
    silently dropped the chime while every other voice playback (which
    already used the shared element) worked fine."""

    def test_chime_plays_on_the_shared_element_not_a_fresh_audio(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        _enable_listening(page)

        _fire_wake_check(page)
        page.wait_for_function("window.__audioLog.some((e) => e.chime)")

        # unlockTtsAudio() (called synchronously in triggerWakeRecording(),
        # before the chime) constructs the one shared element via
        # getTtsAudioElement(). The chime must reuse it, not build a second,
        # still-locked one.
        constructed = page.evaluate("window.__constructedAudioIds")
        assert len(constructed) == 1, (
            f"expected exactly one Audio() construction (the shared unlock element), "
            f"got {len(constructed)} -- the chime built a fresh, un-unlocked element instead "
            f"of reusing the shared one: {constructed}"
        )
        shared_id = constructed[0]

        chime_src_sets = [e for e in page.evaluate("window.__audioLog") if e["chime"]]
        assert chime_src_sets and all(e["id"] == shared_id for e in chime_src_sets), (
            f"chime src was set on a different element than the shared unlock one: {chime_src_sets}"
        )

        chime_plays = [e for e in page.evaluate("window.__playLog") if e["chime"]]
        assert chime_plays and all(e["id"] == shared_id for e in chime_plays), (
            f"chime play() happened on a different element than the shared unlock one: {chime_plays}"
        )

        # The shared element's unlock ping (SILENT_WAV) is a real, non-chime
        # src assignment on that same element, ahead of the chime's -- proof
        # this really is the gesture-unlocked element the mobile fix routes
        # through, not a coincidentally-matching id.
        events = page.evaluate("window.__audioLog")
        assert events[0]["id"] == shared_id and events[0]["chime"] is False, (
            f"expected the shared element's first src assignment to be the SILENT_WAV "
            f"unlock ping, not the chime: {events}"
        )

        page.evaluate("window.__resolveChime()")
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        assert page.evaluate("window.__recorderStartCalls") == 1

    def test_stalled_shared_chime_resolves_via_safety_timeout(self, page: Page, chat_base_url):
        """Never call __resolveChime() -- playWakeChimeShared()'s own
        Promise.race timeout (separate code from the desktop path's) must
        still let recording begin."""
        _open_voice_chat(page, chat_base_url, android=True)
        _enable_listening(page)

        _fire_wake_check(page)
        page.wait_for_function("window.__audioLog.some((e) => e.chime)")
        assert _is_recording(page) is False

        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')",
            timeout=3000,
        )
        assert page.evaluate("window.__recorderStartCalls") == 1


class TestDesktopUnchanged:
    """Desktop has no autoplay-unlock requirement to route around, so
    unlockTtsAudio() no-ops there (useSharedTtsAudio() is false) and the
    chime keeps constructing its own fresh, throwaway `<audio>` exactly like
    before #725."""

    def test_desktop_chime_never_touches_a_shared_unlock_element(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)  # no android=True -- desktop UA
        _enable_listening(page)

        _fire_wake_check(page)
        page.wait_for_function("window.__audioLog.some((e) => e.chime)")

        events = page.evaluate("window.__audioLog")
        # On mobile there's always a prior non-chime (SILENT_WAV) src
        # assignment from unlockTtsAudio() on the element the chime then
        # reuses. Desktop must have none -- unlockTtsAudio() is a no-op
        # there, so the chime's own src-set is the only assignment, on a
        # fresh element built just for it.
        assert not any(not e["chime"] for e in events), (
            f"a non-chime src assignment happened on desktop -- "
            f"unlockTtsAudio() shouldn't touch anything here: {events}"
        )
        assert len(page.evaluate("window.__constructedAudioIds")) == 1

        page.evaluate("window.__resolveChime()")
        page.wait_for_function(
            "document.getElementById('voiceTalkBtn').classList.contains('recording')"
        )
        assert page.evaluate("window.__recorderStartCalls") == 1


class TestClipInFlightGuard:
    """#608 regression guard, applied to the new shared-element chime path:
    a wake match while a real TTS clip is still loading/playing on the
    shared element must not let the chime steal it out from under that
    clip. baseWakeGuardsOk() (checked at the very top of
    triggerWakeRecording(), before the chime's manifest fetch even starts)
    already requires `!clipInFlight`, which playSingleUrl() sets true
    *before* the real clip's own `.src` assignment -- so by the time this
    test observes that assignment, clipInFlight is already guaranteed true."""

    def test_wake_during_a_real_clip_does_not_steal_it(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        _enable_listening(page)

        # Start a real (400ms) turn without awaiting it -- this is what
        # holds clipInFlight true on the shared element for the window this
        # test needs.
        page.evaluate("window.__mockAudio.push({ms: 400, tag: 't1'})")
        page.evaluate("""
            () => {
              window.__turnDone = false;
              window.lifeChatVoice.submitTurn({ transcript: 'hi' })
                .then(() => { window.__turnDone = true; });
            }
        """)
        page.wait_for_function(
            "window.__audioLog.some((e) => !e.chime && e.src.indexOf('t1') !== -1)"
        )

        # Real clip is now in flight. A wake match arriving in this window
        # must be refused, not steal the element.
        _fire_wake_check(page)
        page.wait_for_function("window.__wakeResult !== undefined")

        assert page.evaluate("window.__wakeResult") is False
        assert not any(e["chime"] for e in page.evaluate("window.__audioLog")), (
            "the chime touched the shared element's src while a real clip "
            "was still in flight -- #608 regression"
        )
        assert _is_recording(page) is False

        # The real clip must still finish on its own, undisturbed.
        page.wait_for_function("window.__turnDone === true", timeout=3000)
        real_src_sets = [
            e for e in page.evaluate("window.__audioLog")
            if not e["chime"] and "t1" in e["src"]
        ]
        assert len(real_src_sets) == 1, (
            f"expected exactly one src assignment for the real clip, got "
            f"{len(real_src_sets)} -- it was reassigned mid-flight: {real_src_sets}"
        )
