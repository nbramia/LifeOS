"""Browser tests for the 2x-toggle spoken-playback regression (#608).

Reported symptom: TTS worked for the first replies in a conversation, then
stopped -- later replies printed text but were never spoken. The transition
coincided with toggling the **2x** control.

Reproduction (see the PR description for the full trace): the actual defect
is not in `getPlaybackRate()`/`syncActivePlaybackRates()` -- toggling the
rate while a clip is genuinely mid-playback works fine and applies to the
clip already sounding. The real bug is in `unlockTtsAudio()`, which the talk
button calls on *every* tap, unconditionally, on the shared-audio-element
path used on iOS/Android (`useSharedTtsAudio()`). If a tap lands while a real
clip is still loading on that shared `<audio>` element -- its
`oncanplaythrough` handler armed but not yet fired -- `unlockTtsAudio()`
reassigns `.src` to a silent unlock ping out from under it. The real clip's
handler is never cleared, so it fires against the *silent* resource once
that becomes ready instead: `playUrlOnElement()`'s promise resolves, the
turn completes normally (text renders, no error), and the real clip is
simply never heard. `getPlaybackRate()` correlates with the bug only because
the user is more likely to tap talk again shortly after speeding a reply up
-- see `test_interrupting_tap_during_clip_load_...` below for the isolated
trigger, with no rate toggle involved at all.

Fix: `unlockTtsAudio()` now no-ops while a real clip is loading or playing on
the shared element, instead of unconditionally stealing it, via a new
`clipInFlight` flag (see `TestStrandedIsPlayingAfterAFailedTurn` below for why
that flag replaced the turn-lifecycle `isPlaying` it was first built on top
of, rather than just patching `isPlaying`'s own reset gap). Also adds the AC's
"say so" requirement: a genuine (non-benign) playback failure now surfaces in
the thread instead of only a console warning.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, and drives `submitTurn()`
directly (exported from voice.js for exactly this -- see
tests/test_voice_backend_parity_ui_browser.py) since getUserMedia/
MediaRecorder don't run headless. No `requires_server` marker, so this runs
at pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs (add_init_script). Gives each test:
#   - window.__makeWav(ms) -- a real, short, silent WAV data: URI of the
#     given duration, tagged with a #turnN fragment so instrumentation can
#     tell which logical clip is which even though the shared element reuses
#     one <audio> object across clips.
#   - Real HTMLMediaElement.play()/`.src` instrumentation logging to
#     window.__events, so a test can assert actual playback (a real
#     'playing' -> 'ended' span with plausible duration), not merely that
#     `.play()` was called -- a rejected play() promise is silent, and a
#     test that only checks the call would pass while nothing is audible.
#   - An opt-in interrupt hook: when window.__armInterrupt is true, the
#     moment `.src` is set to a URL containing the tag in
#     window.__interruptOnTag, it synchronously clicks a target button --
#     reproducing a tap landing in the exact instant a clip is loading.
#   - An opt-in forced-rejection hook: any src containing 'FORCEFAIL' makes
#     `.play()` reject with a configurable, non-benign DOMException.
_INSTRUMENT_SCRIPT = """
window.__events = [];
window.__gen = 0;
window.__armInterrupt = false;
window.__interruptOnTag = '';
window.__interruptSelector = '#voiceTalkBtn';
window.__forceFailName = 'NotSupportedError';

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
      const result = srcDesc.set.call(this, v);
      if (window.__armInterrupt && (v || '').indexOf(window.__interruptOnTag) !== -1) {
        window.__armInterrupt = false;
        document.querySelector(window.__interruptSelector).click();
        window.__events.push({ type: 'interrupt-fired', gen: this.__gen, t: performance.now() });
      }
      return result;
    },
  });
  const origPlay = proto.play;
  proto.play = function () {
    const gen = this.__gen;
    window.__events.push({ type: 'play-call', gen, src: this.src, rate: this.playbackRate, t: performance.now() });
    if (!this.__wired) {
      this.__wired = true;
      for (const ev of ['playing', 'ended', 'error', 'abort']) {
        this.addEventListener(ev, () => window.__events.push({ type: ev, gen: this.__gen, t: performance.now() }));
      }
    }
    if ((this.src || '').indexOf('FORCEFAIL') !== -1) {
      return Promise.reject(new DOMException('forced failure', window.__forceFailName));
    }
    const p = origPlay.call(this);
    p.catch(() => {});
    return p;
  };
})();
"""


def _fetch_mock_script(mock_var):
    """`window[mock_var]` is a queue of {ms, tag, dropDone} consumed one per
    turn -- each becomes that turn's `main_audio` clip. SSE-formats the
    response the way the real voice gateway does. `dropDone: true` omits the
    `done` event entirely (simulating a dropped stream/server-side error
    right after the audio event), which makes consumeTurnStream() return
    null and submitTurn() throw 'Turn ended without a response' -- while the
    clip that same audio event started is still genuinely loading/playing."""
    return f"""
    window.{mock_var} = [];
    window.fetch = function (url, opts) {{
      const urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
      if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {{
        const cfg = window.{mock_var}.shift() || {{ ms: 100, tag: 'default' }};
        const clipUrl = window.__makeWav(cfg.ms) + '#' + cfg.tag;
        let sse = 'data: ' + JSON.stringify({{ type: 'main_audio', url: clipUrl }}) + '\\n\\n';
        if (!cfg.dropDone) {{
          sse += 'data: ' + JSON.stringify({{ type: 'done', data: {{ response_text: 'ok ' + cfg.tag }} }}) + '\\n\\n';
        }}
        return Promise.resolve(new Response(sse, {{ status: 200, headers: {{ 'Content-Type': 'text/event-stream' }} }}));
      }}
      return Promise.resolve(new Response('{{}}', {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }}));
    }};
    """


ANDROID_UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"


def _open_voice_chat(page: Page, base_url, *, android=False, mock_var="__mockAudio"):
    if android:
        page.add_init_script(
            "Object.defineProperty(navigator, 'userAgent', { value: %r });" % ANDROID_UA
        )
    page.add_init_script(_INSTRUMENT_SCRIPT)
    page.add_init_script(_fetch_mock_script(mock_var))
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")


def _queue_clip(page: Page, ms, tag, mock_var="__mockAudio", drop_done=False):
    page.evaluate(
        "(a) => window[a[0]].push({ms: a[1], tag: a[2], dropDone: a[3]})",
        [mock_var, ms, tag, drop_done],
    )


def _submit_turn(page: Page, transcript="hi"):
    """Awaits submitTurn() to completion (Playwright awaits a returned
    promise), with a generous timeout so a genuinely hung turn fails the
    test loudly instead of hanging the suite."""
    page.evaluate(
        """
        (t) => Promise.race([
          window.lifeChatVoice.submitTurn({ transcript: t }),
          new Promise((_, reject) => setTimeout(() => reject(new Error('submitTurn timed out')), 5000)),
        ])
        """,
        transcript,
    )


def _events(page: Page):
    return page.evaluate("window.__events")


def _playing_span(events, tag_substring=None):
    """The (playing_t, ended_t) pair for the first 'playing'->'ended' span
    whose *src at the time of the play-call* contains tag_substring (or the
    first span at all, if not given). None if no such span completed."""
    play_call_srcs = {e["gen"]: e.get("src", "") for e in events if e["type"] == "play-call"}
    playing = {e["gen"]: e["t"] for e in events if e["type"] == "playing"}
    for e in events:
        if e["type"] != "ended":
            continue
        gen = e["gen"]
        if gen not in playing:
            continue
        src = play_call_srcs.get(gen, "")
        if tag_substring and tag_substring not in src:
            continue
        return (playing[gen], e["t"])
    return None


class TestToggleMidPlaybackKeepsSpeaking:
    """AC: toggling 2x mid-conversation must not stop later replies from
    being spoken, on the shared-audio-element path (iOS/Android)."""

    def test_toggle_after_a_clip_finishes_does_not_break_the_next_one(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        _queue_clip(page, 200, "t1")
        _submit_turn(page, "one")
        page.locator("#voiceFast").click()  # toggle 2x between turns, not mid-clip
        _queue_clip(page, 200, "t2")
        _submit_turn(page, "two")

        span = _playing_span(_events(page), "t2")
        assert span is not None, "turn 2's clip never reached a playing->ended span"
        assert (span[1] - span[0]) > 50, f"turn 2 barely played ({span[1] - span[0]:.1f}ms) -- suspiciously short"

    def test_toggle_while_a_clip_is_actively_playing_still_lets_the_next_one_speak(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        _queue_clip(page, 300, "t1")
        page.evaluate(
            """
            async (t) => {
              const p = window.lifeChatVoice.submitTurn({ transcript: t });
              const start = performance.now();
              while (!window.__events.some(e => e.type === 'playing') && performance.now() - start < 2000) {
                await new Promise(r => setTimeout(r, 5));
              }
              document.getElementById('voiceFast').click();  // toggle mid-clip
              await p;
            }
            """,
            "one",
        )
        _queue_clip(page, 200, "t2")
        _submit_turn(page, "two")

        events = _events(page)
        span1 = _playing_span(events, "t1")
        span2 = _playing_span(events, "t2")
        assert span1 is not None, "turn 1's own clip never finished playing after the mid-clip toggle"
        assert span2 is not None, "turn 2 was never spoken after toggling 2x mid-conversation (#608)"
        assert (span2[1] - span2[0]) > 50

    def test_toggling_back_to_1x_keeps_playback_working(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        page.locator("#voiceFast").click()
        _queue_clip(page, 200, "t1")
        _submit_turn(page, "one")
        page.locator("#voiceFast").click()  # back to 1x
        _queue_clip(page, 200, "t2")
        _submit_turn(page, "two")

        span = _playing_span(_events(page), "t2")
        assert span is not None
        assert (span[1] - span[0]) > 50

    def test_desktop_one_element_per_clip_path_is_unaffected(self, page: Page, chat_base_url):
        """Control: the AC requires the fix to hold on the shared-element
        path *without* regressing the desktop (one-`Audio`-per-clip) path,
        which unlockTtsAudio() never touches."""
        _open_voice_chat(page, chat_base_url, android=False)
        _queue_clip(page, 200, "t1")
        _submit_turn(page, "one")
        page.locator("#voiceFast").click()
        _queue_clip(page, 200, "t2")
        _submit_turn(page, "two")

        span = _playing_span(_events(page), "t2")
        assert span is not None
        assert (span[1] - span[0]) > 50


class TestSmallestTrigger:
    """The isolated defect: no rate toggle involved at all. A tap landing
    while a real clip is still *loading* on the shared element (before its
    own 'playing' event) silently swaps in unlockTtsAudio()'s silent ping,
    and the real clip is never heard -- even though the turn completes
    normally with no error. This is what toggling 2x merely made more likely
    to happen (a faster reply narrows the window before the user's next tap,
    or they tap right after toggling to check it worked)."""

    def test_interrupting_tap_during_clip_load_does_not_swap_in_the_silent_unlock_ping(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        _queue_clip(page, 300, "t1")
        page.evaluate(
            """
            () => { window.__armInterrupt = true; window.__interruptOnTag = 't1'; }
            """
        )
        _submit_turn(page, "one")

        events = _events(page)
        # Exactly one real-content src assignment for turn 1 -- if the bug
        # were present, unlockTtsAudio() would inject a second src-set to
        # the SILENT_WAV constant in between.
        real_src_sets = [e for e in events if e["type"] == "src-set" and "t1" in e.get("src", "")]
        assert len(real_src_sets) == 1, (
            f"expected exactly one src assignment for turn 1's clip, got {len(real_src_sets)} "
            f"-- the shared element's src was reassigned mid-load (events={events})"
        )

        span = _playing_span(events, "t1")
        assert span is not None, "turn 1's clip never actually played"
        duration = span[1] - span[0]
        # Pre-fix, the hijacked silent ping "ends" in a few ms; the real
        # 300ms clip finishing in anything near that would mean it was
        # swapped out, not genuinely played to completion.
        assert duration > 150, f"turn 1's clip finished suspiciously fast ({duration:.1f}ms) -- likely swapped for the silent unlock ping"


class TestStrandedIsPlayingAfterAFailedTurn:
    """A second, independent bug found while fixing the first, in an earlier
    version of this fix that guarded unlockTtsAudio() with the pre-existing
    `isPlaying` flag: `isPlaying` is set true by the `status_audio`/
    `main_audio` SSE events, but its success-path reset (`isPlaying = false`
    after `await playbackChain`) sat inside submitTurn()'s `try`, after the
    point where a turn that throws post-audio-event (a dropped stream, a
    server-side `error` event) exits. Unlike voiceBusy/state.isLoading,
    nothing in the `catch`/`finally` covered it -- stranding `isPlaying`
    true. Confirmed by reading the code, not conjecture, and independently
    by tracing the actual events.

    That stranding does NOT, empirically, produce "TTS silenced until
    reload": the very next tap's `if (isPlaying) { stopAllAudio(); return; }`
    branch in onTalkClick reset the flag (that's the cancel/replay-interrupt
    path, not a talk-to-record tap). But that self-correction fired at the
    wrong time -- before the real clip that triggered `isPlaying` had
    necessarily finished -- which could re-open the exact #608 race the
    first fix closed. `test_unlock_...` below pins that the unlock still
    works once the flag would otherwise be stuck.

    Simply moving the reset into `finally` looked like the fix, but traded
    that bug for a worse one: clips already handed to `playbackChain` keep
    playing after the SSE loop throws (the chain isn't cancelled by the
    generator unwinding), so a `finally`-only reset creates a window where
    real audio is audibly playing while the turn-lifecycle flag already
    reads "not playing" -- exactly when onTalkClick's stop-vs-record branch
    needs it most. `test_a_tap_while_the_reply_is_still_playing_stops_it_...`
    below pins that a tap in that window stops the audio rather than
    recording over it.

    The actual fix collapses `isPlaying` into `clipInFlight` entirely --
    tracked by playSingleUrl() itself, tied via `.finally()` to that
    specific clip's own promise, used by both unlockTtsAudio() and the
    stop-vs-record checks. There is no separate turn-lifecycle flag left to
    go stale."""

    def test_unlock_still_fires_on_the_next_tap_after_a_turn_fails_post_audio_event(
            self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        # Turn 1: a main_audio event fires (clipInFlight goes true once
        # playSingleUrl() starts) and then the stream ends with no `done`
        # event -- submitTurn() throws "Turn ended without a response",
        # hitting catch+finally.
        _queue_clip(page, 150, "t1", drop_done=True)
        _submit_turn(page, "one")

        # Give the clip itself (independent of the failed turn) time to
        # actually finish playing, so this exercises "the flag outlived the
        # thing it was supposed to track" rather than "a tap landed while
        # audio was still genuinely in flight" (that's TestSmallestTrigger's
        # scenario, correctly still guarded either way).
        page.wait_for_timeout(400)

        events_before = len(_events(page))
        # A real tap of the talk button, exactly as a user would do to start
        # the next turn -- exercises unlockTtsAudio()'s guard for real.
        page.locator("#voiceTalkBtn").click()

        new_events = _events(page)[events_before:]
        unlock_play_calls = [
            e for e in new_events
            if e["type"] == "play-call" and "UklGRigAAABXQVZFZm10" in e.get("src", "")
        ]
        assert unlock_play_calls, (
            "unlockTtsAudio() did not attempt its silent-ping play() on the tap "
            "following a turn that failed after its audio event had already "
            "finished playing -- a stale guard flag is blocking it (#608 follow-up)"
        )

        # And the practical consequence: the next real turn still speaks.
        _queue_clip(page, 200, "t2")
        _submit_turn(page, "two")
        span = _playing_span(_events(page), "t2")
        assert span is not None, "turn 2 never played after a turn failed post-audio-event"
        assert (span[1] - span[0]) > 100, "turn 2's clip finished suspiciously fast -- likely hijacked by the silent unlock ping"

    def test_a_tap_while_the_reply_is_still_playing_stops_it_rather_than_recording_over_it(
            self, page: Page, chat_base_url):
        """A turn errors right after its audio event (so its own control
        flow is already done -- voiceBusy/state.isLoading are both false)
        while the clip that event started is still genuinely, audibly
        playing. A tap in that window must stop the audio (onTalkClick's
        `clipInFlight` branch), not fall through to start recording over
        the assistant's own still-sounding reply.

        This passes on unmodified `main` too -- there, `isPlaying` is never
        reset on this path at all, so it's accidentally still true and the
        tap happens to hit the stop branch anyway. It is not a guard against
        `main`. It guards against the *intermediate*, obvious-looking fix
        this PR considered and rejected: resetting `isPlaying` in `finally`.
        That reset is exactly what reopens this failure mode, because
        clips already handed to `playbackChain` keep playing after the SSE
        loop throws -- the turn-lifecycle flag goes stale exactly when this
        check needs it to still read "yes, something is playing." See the
        class docstring."""
        _open_voice_chat(page, chat_base_url, android=True)
        # A getUserMedia call would mean onTalkClick fell through to
        # beginRecordingFromTap() instead of stopping the still-playing clip.
        page.evaluate(
            """
            () => {
              window.__gumCalls = 0;
              navigator.mediaDevices.getUserMedia = () => {
                window.__gumCalls += 1;
                return Promise.reject(new DOMException('no mic in test', 'NotFoundError'));
              };
            }
            """
        )
        _queue_clip(page, 600, "t1", drop_done=True)
        _submit_turn(page, "one")

        # The clip is long (600ms) and the turn's own throw/catch/finally
        # happen almost immediately -- tap well within the clip's run, long
        # before it could have naturally ended.
        page.wait_for_timeout(150)

        page.locator("#voiceTalkBtn").click()
        page.wait_for_timeout(20)

        assert page.evaluate("window.__gumCalls") == 0, (
            "talk tap started recording over the assistant's own still-playing "
            "reply instead of stopping it -- clipInFlight-style guard is stale"
        )
        # Direct confirmation the audio itself was actually paused (not just
        # that recording wasn't attempted) -- stopAllAudio()'s effect. The
        # shared element is never inserted into the document (`new Audio()`,
        # not `document.createElement` + append), so it has to be found via
        # the constructor-wrapper registry rather than querySelectorAll.
        audio_state = page.evaluate("() => window.__audioElements.map(a => a.paused)")
        assert audio_state and all(audio_state), (
            f"the shared audio element is still playing after the tap ({audio_state}) "
            "-- the tap did not actually stop it"
        )


class TestGenuinePlaybackFailureIsReported:
    """AC: if speech genuinely cannot be played, the interface must say so
    rather than silently rendering text only -- the same idiom voice.js
    already uses for mic-block reasons (#516)."""

    def test_a_rejected_play_promise_surfaces_in_the_thread(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url, android=True)
        page.evaluate("() => { window.__forceFailName = 'NotSupportedError'; }")
        _queue_clip(page, 200, "FORCEFAIL")
        _submit_turn(page, "one")

        # submitTurn() always renders the response text as one assistant
        # bubble; a genuine (non-benign) playback failure adds a second one.
        expect(page.locator(".message.assistant")).to_have_count(2)
        expect(page.locator(".message.assistant").last).to_contain_text("Couldn")

    def test_a_benign_rejection_does_not_spam_an_error_bubble(self, page: Page, chat_base_url):
        """Control: a cancel-shaped rejection (AbortError/NotAllowedError)
        is not treated as a genuine failure -- matches isBenignPlaybackError()."""
        _open_voice_chat(page, chat_base_url, android=True)
        page.evaluate("() => { window.__forceFailName = 'AbortError'; }")
        _queue_clip(page, 200, "FORCEFAIL")
        _submit_turn(page, "one")

        # Just the response-text bubble -- no extra warning for a benign abort.
        expect(page.locator(".message.assistant")).to_have_count(1)
