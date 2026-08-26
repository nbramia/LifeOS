"""Browser tests for #617: stopAllAudio() orphaning an in-flight playback
promise when replay interrupts a live turn.

Found while fixing #608, deliberately left out of that PR to keep it
surgical (see the issue for the full reachability analysis). Reproduction:

`stopAllAudio()` (`web/chat/voice.js`) stops every element in `activeAudios`
by nulling `onended`/`onerror` and pausing -- it never *fires* those
handlers. If one of those elements has a genuinely in-flight playback
promise at that moment (from `playSingleUrl()`/`playUrlOnElement()`, still
waiting on `onended`/`onerror` to settle it), that promise is abandoned: it
never resolves or rejects. `enqueueClip()` chains each clip onto the
module-level `playbackChain`, and `submitTurn()` does `await playbackChain`
before it can reach its own `finally` (which resets `voiceBusy` and
`state.isLoading`) -- so an abandoned clip promise hangs the *entire live
turn*, not just that one clip.

A normal talk-button tap can't reach this: `onTalkClick`'s
`if (voiceBusy || state.isLoading) return;` guard fires first, and
`voiceBusy` stays true for a turn's own audio too (reset only in
`submitTurn()`'s `finally`, after `await playbackChain`). The only
reachable path is **replay**: `attachReplay()` wires a plain click listener
onto each past message bubble with no `voiceBusy` gate at all, and
`playUrls()` (which `replayMessage()` calls) does `stopAllAudio()`
unconditionally -- including while a *different*, still-speaking live turn
owns the audio element(s) in `activeAudios`.

This reproduces on both the shared-audio-element path (iOS/Android,
`playUrlOnElement()`) and the desktop one-`Audio`-per-clip path
(`playSingleUrl()`'s own executor) -- `stopAllAudio()` iterates
`activeAudios` the same way regardless of platform, so neither is
special-cased in the fix.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port and drives `submitTurn()` directly (exported from voice.js for exactly
this -- see tests/test_voice_backend_parity_ui_browser.py), since
getUserMedia/MediaRecorder don't run headless. No `requires_server` marker,
so this runs at pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs. Real HTMLMediaElement.play()/`.src`
# instrumentation logging to window.__events, tagged so a test can tell which
# logical clip a 'playing'/'ended' event belongs to even though the shared
# element reuses one <audio> object across clips.
_INSTRUMENT_SCRIPT = """
window.__events = [];
window.__gen = 0;

window.__makeWav = function (ms, sampleRate) {
  sampleRate = sampleRate || 8000;
  const n = Math.round((ms / 1000) * sampleRate);
  const dataBytes = n * 2;
  const buf = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buf);
  const writeStr = (offset, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
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
  const bytes = new Uint8Array(buf);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return 'data:audio/wav;base64,' + btoa(binary);
};

(function () {
  // `new Audio(url)` sets the src *content attribute* directly rather than
  // going through the `.src` IDL property setter below -- rewrap it so the
  // desktop (one-Audio-per-clip) path's construction is instrumented too.
  const OrigAudio = window.Audio;
  window.__audioElements = [];
  window.Audio = function (url) {
    const el = new OrigAudio();
    window.__audioElements.push(el);
    if (url !== undefined) el.src = url;
    return el;
  };
  window.Audio.prototype = OrigAudio.prototype;
})();

(function () {
  const proto = HTMLMediaElement.prototype;
  const srcDesc = Object.getOwnPropertyDescriptor(proto, 'src');
  Object.defineProperty(proto, 'src', {
    get() { return srcDesc.get.call(this); },
    set(v) {
      this.__gen = ++window.__gen;
      window.__events.push({ type: 'src-set', gen: this.__gen, src: v || '', t: performance.now() });
      return srcDesc.set.call(this, v);
    },
  });
  const origPlay = proto.play;
  proto.play = function () {
    const gen = this.__gen;
    window.__events.push({ type: 'play-call', gen, src: this.src, t: performance.now() });
    if (!this.__wired) {
      this.__wired = true;
      for (const ev of ['playing', 'ended', 'error', 'abort']) {
        this.addEventListener(ev, () => window.__events.push({ type: ev, gen: this.__gen, t: performance.now() }));
      }
    }
    const p = origPlay.call(this);
    p.catch(() => {});
    return p;
  };
})();
"""

_FETCH_MOCK = """
window.__mockAudio = [];
window.fetch = function (url, opts) {
  const urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    const cfg = window.__mockAudio.shift() || { ms: 100, tag: 'default' };
    const clipUrl = window.__makeWav(cfg.ms) + '#' + cfg.tag;
    const sse = 'data: ' + JSON.stringify({ type: 'main_audio', url: clipUrl }) + '\\n\\n'
      + 'data: ' + JSON.stringify({ type: 'done', data: { response_text: 'ok ' + cfg.tag, audio_url: clipUrl } }) + '\\n\\n';
    return Promise.resolve(new Response(sse, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
  }
  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
};
"""

ANDROID_UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"


# These suites measure playback and replay behavior, not dock configuration.
# The shipped dock defaults (2x/Auto/Listening on) would otherwise inject an
# auto-continue turn and a wake-word mic hold into every scenario and shift the
# counts they assert, so each page starts from an explicit, quiet baseline:
# every toggle off. Dock-default behavior itself is covered in
# tests/test_voice_listening_wake_word_ui_browser.py.
_DOCK_BASELINE = (
    "try { window.localStorage.setItem('lifeos:chat:dock_settings', "
    "JSON.stringify({ mute: false, auto: false, fast: false, listen: false })); } "
    "catch (e) {}"
)


def _open_voice_chat(page: Page, base_url, *, android=False):
    if android:
        page.add_init_script(
            "Object.defineProperty(navigator, 'userAgent', { value: %r });" % ANDROID_UA
        )
    page.add_init_script(_DOCK_BASELINE)
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.add_init_script(_FETCH_MOCK)
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")


def _queue_clip(page: Page, ms, tag):
    page.evaluate("(a) => window.__mockAudio.push({ ms: a[0], tag: a[1] })", [ms, tag])


def _submit_turn(page: Page, transcript="hi"):
    """Awaits submitTurn() to completion, with a generous timeout so a
    genuinely hung turn fails the test loudly instead of hanging the suite."""
    page.evaluate(
        """
        (t) => Promise.race([
          window.lifeChatVoice.submitTurn({ transcript: t }),
          new Promise((_, reject) => setTimeout(() => reject(new Error('submitTurn timed out')), 5000)),
        ])
        """,
        transcript,
    )


def _start_turn_without_awaiting(page: Page, transcript, promise_var):
    """Kicks off submitTurn() and stashes its promise on `window[promise_var]`
    without awaiting it -- lets the test proceed while the turn's own
    playback is still in flight."""
    page.evaluate(
        "(a) => { window[a[1]] = window.lifeChatVoice.submitTurn({ transcript: a[0] }); }",
        [transcript, promise_var],
    )


def _wait_for_playing(page: Page, tag, timeout=3000):
    page.wait_for_function(
        """
        (tag) => {
          const srcByGen = {};
          for (const e of window.__events) if (e.type === 'play-call') srcByGen[e.gen] = e.src || '';
          return window.__events.some((e) => e.type === 'playing' && (srcByGen[e.gen] || '').indexOf(tag) !== -1);
        }
        """,
        arg=tag,
        timeout=timeout,
    )


def _await_with_timeout(page: Page, promise_var, timeout_ms=2000):
    """'resolved' if `window[promise_var]` settles (fulfilled OR rejected --
    the AC is that it settles at all, not which way) before timeout_ms,
    else 'TIMEOUT'."""
    return page.evaluate(
        """
        (a) => Promise.race([
          window[a[0]].then(() => 'resolved', () => 'resolved'),
          new Promise((resolve) => setTimeout(() => resolve('TIMEOUT'), a[1])),
        ])
        """,
        [promise_var, timeout_ms],
    )


class TestReplayDuringLiveTurnPlaybackDoesNotHangTheTurn:
    """AC: a live turn's clip is playing, replay is tapped on a different
    message, and the live turn's own submitTurn() call settles (not hangs)
    afterward."""

    def test_shared_audio_element_path(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)

        # Turn 0 completes fully -- gives us an earlier, replayable bubble.
        _queue_clip(page, 60, "t0")
        _submit_turn(page, "zero")

        # Turn 1 starts. Don't await it to completion -- it needs to still
        # be genuinely playing when we interrupt it.
        _queue_clip(page, 500, "t1")
        _start_turn_without_awaiting(page, "one", "__turn1Promise")
        _wait_for_playing(page, "t1")

        # Tap replay on the EARLIER (turn 0) message -- not the one
        # currently speaking. This is the only reachable path (a normal
        # talk-button tap can't reach stopAllAudio() while voiceBusy is true).
        page.locator(".message.assistant.replayable", has_text="ok t0").click()

        result = _await_with_timeout(page, "__turn1Promise")
        assert result == "resolved", (
            "turn 1's own submitTurn() never settled after replay interrupted "
            "its still-playing clip -- stopAllAudio() orphaned the in-flight "
            "playback promise (#617)"
        )

        # No spurious failure bubble -- the interrupt must land on the benign
        # side of isBenignPlaybackError(), not trigger reportPlaybackFailed().
        expect(page.locator(".message.assistant")).to_have_count(2)

        # And the turn is genuinely done, not just "settled" in a broken
        # state: voiceBusy/state.isLoading are reset, so a real next turn works.
        _queue_clip(page, 150, "t2")
        _submit_turn(page, "two")
        events = page.evaluate("window.__events")
        src_by_gen = {e["gen"]: e.get("src", "") for e in events if e["type"] == "play-call"}
        assert any(
            e["type"] == "ended" and "t2" in src_by_gen.get(e["gen"], "") for e in events
        ), "turn 2 never played after the interrupted turn 1 -- state left stuck"

    def test_desktop_one_element_per_clip_path(self, page: Page, chat_base_url):
        """Same defect, non-shared path: stopAllAudio() nulls onended/onerror
        on every element in activeAudios regardless of platform, so the
        desktop one-Audio()-per-clip path is exposed the same way."""
        _open_voice_chat(page, chat_base_url, android=False)

        _queue_clip(page, 60, "t0")
        _submit_turn(page, "zero")

        _queue_clip(page, 500, "t1")
        _start_turn_without_awaiting(page, "one", "__turn1Promise")
        _wait_for_playing(page, "t1")

        page.locator(".message.assistant.replayable", has_text="ok t0").click()

        result = _await_with_timeout(page, "__turn1Promise")
        assert result == "resolved", (
            "turn 1's own submitTurn() never settled after replay interrupted "
            "its still-playing clip on the desktop per-clip Audio path (#617)"
        )
        expect(page.locator(".message.assistant")).to_have_count(2)
