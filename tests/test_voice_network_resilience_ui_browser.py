"""Browser tests for voice-turn network resilience on weak/intermittent
connections (#801).

The operator's report: on flaky internet a voice submission "simply fails and
I have to start over" -- re-speaking the whole message. This suite covers the
policy `web/chat/voice.js` now implements:

- **Initial-submission retry** (`postTurnStart()`): up to 3 automatic retries
  with jittered backoff (~1s/3s/9s) on network-class failures only (a fetch()
  rejection, or this repo's own 502 -- see `api/routes/voice.py`'s
  `voice_proxy()`, which raises 502 ONLY when the gateway itself was
  unreachable). A 4xx never retries.
- **The recording is held** (`heldRecording` in voice.js) until the turn
  *definitively* completes -- success, an explicit cancel, or an explicit
  dismiss -- never merely because a submission attempt failed. A failed turn
  renders a Retry affordance that resubmits the SAME blob into the SAME
  bubble (never a second one).
- **Mid-stream drops** (after the initial POST answered `ok` but before
  `done`/`error`/`cancelled` arrived) are NOT auto-retried -- the turn may
  still be running server-side. `handleMidStreamDrop()` polls
  `GET /api/voice/audio/{turn_id}` (HEAD) briefly, since that's the only
  thing whisper-relay's `voice_gateway/` exposes that can answer "did it
  finish" for an in-flight turn_id (confirmed by reading `cancel.py`/
  `storage.py`/`routes/voice.py` there -- no status endpoint exists). Found
  -> the turn completed, recovered via the existing tap-to-replay affordance.
  Not found -> explicit-Retry-only, same as any other failure.

Drives `submitTurn()`/`cancelActiveTurn()` directly (the seam voice.js
exports so a headless harness can run a turn without a real mic -- same
pattern as tests/test_voice_transcript_ui_browser.py, which this file's
harness is adapted from). Uses Playwright's Clock API (`page.clock`) to fast-
forward the real backoff/poll timers deterministically instead of sleeping
through them for real.

Serves `web/` itself from an ephemeral port and replaces `window.fetch`
outright -- the same self-contained pattern as
tests/test_voice_transcript_ui_browser.py and
tests/test_voice_mic_block_ui_browser.py. No `requires_server` marker, so
this runs at pre-push (`browser and not requires_server`).
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


# Installed before any app JS runs. Controls:
#   - window.__turnStreamBehavior: how POST /api/voice/turn/stream responds.
#       'succeed'       -> 200 + window.__voiceFrames (SSE)
#       'network-fail'  -> reject the fetch (offline/DNS/connection-reset
#                          class failure) -- retryable
#       '502'           -> HTTP 502 (this repo's own "gateway unreachable")
#                          -- retryable
#       '400'           -> HTTP 400 -- never retryable
#       'midstream-drop'-> 200 + streams window.__voiceFrames, then the
#                          ReadableStream controller errors (simulating a
#                          connection drop after bytes already arrived)
#     'network-fail'/'502' fail this many times (window.__failCount), then
#     fall back to 'succeed' behavior -- lets one test cover both "recovers
#     after N failures" and "never recovers" by setting failCount high.
#   - window.__audioProbeBehavior: how HEAD /api/voice/audio/{turn_id}
#     responds -- 'found' (200) or 'not-found' (404, always).
#   - window.__fetchLog: [{method, url}] for every fetch call, so a test can
#     assert attempt counts and verbs directly instead of inferring them.
_FETCH_MOCK = """
try {
  window.localStorage.setItem(
    'lifeos:chat:dock_settings',
    JSON.stringify({ mute: true, auto: false, fast: false, listen: false })
  );
} catch (e) { /* storage unavailable -- assertions below will say so */ }

window.__fetchLog = [];
window.__turnStreamBehavior = 'succeed';
window.__failCount = 0;
window.__audioProbeBehavior = 'not-found';
window.__voiceFrames = [
  { type: 'started', turn_id: 'turn-1' },
  { type: 'transcript', text: 'remind me to call mom' },
  { type: 'done', data: { transcript: 'remind me to call mom', response_text: 'ok' } },
];

function sseBody(frames, dropAfter) {
  return new ReadableStream({
    start: function (controller) {
      var enc = new TextEncoder();
      var i = 0;
      (function next() {
        if (i >= frames.length) {
          if (dropAfter) {
            controller.error(new TypeError('Failed to fetch'));
          } else {
            controller.close();
          }
          return;
        }
        var frame = frames[i++];
        controller.enqueue(enc.encode('data: ' + JSON.stringify(frame) + '\\n\\n'));
        setTimeout(next, 0);
      })();
    },
  });
}

window.fetch = function (url, opts) {
  var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
  var method = (opts && opts.method) || 'GET';
  window.__fetchLog.push({ method: method, url: urlStr });

  if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
    var behavior = window.__turnStreamBehavior;
    if ((behavior === 'network-fail' || behavior === '502') && window.__failCount > 0) {
      window.__failCount -= 1;
      if (behavior === 'network-fail') {
        return Promise.reject(new TypeError('Failed to fetch'));
      }
      return Promise.resolve(new Response('{"detail":"voice gateway unreachable"}', {
        status: 502, headers: { 'Content-Type': 'application/json' },
      }));
    }
    if (behavior === '400') {
      return Promise.resolve(new Response('{"detail":"bad request"}', {
        status: 400, headers: { 'Content-Type': 'application/json' },
      }));
    }
    if (behavior === 'midstream-drop') {
      return Promise.resolve(new Response(sseBody(window.__voiceFrames, true), {
        status: 200, headers: { 'Content-Type': 'text/event-stream' },
      }));
    }
    return Promise.resolve(new Response(sseBody(window.__voiceFrames, false), {
      status: 200, headers: { 'Content-Type': 'text/event-stream' },
    }));
  }

  if (urlStr.indexOf('/api/voice/audio/') !== -1) {
    var ok = window.__audioProbeBehavior === 'found';
    return Promise.resolve(new Response(ok ? 'RIFF' : 'not found', { status: ok ? 200 : 404 }));
  }

  if (urlStr.indexOf('/api/voice/transcribe') !== -1) {
    return Promise.resolve(new Response(JSON.stringify({ transcript: window.__transcribeResponse || '' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  }

  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
};
"""


def _wait_for_backend_ready(page: Page):
    page.evaluate(
        "async () => {"
        "  while (!(window.lifeChat && window.lifeChat.backendReady)) {"
        "    await new Promise((r) => setTimeout(r, 10));"
        "  }"
        "  await window.lifeChat.backendReady;"
        "}"
    )


def _open_voice_chat(page: Page, base_url, dock_settings=None):
    page.add_init_script(_FETCH_MOCK)
    if dock_settings is not None:
        # Runs after _FETCH_MOCK's own localStorage.setItem (init scripts run
        # in registration order), so this wins as the effective dock config
        # for this page load.
        page.add_init_script(
            "window.localStorage.setItem('lifeos:chat:dock_settings', '%s')"
            % json.dumps(dock_settings)
        )
    page.goto(f"{base_url}/chat?mode=voice")
    page.wait_for_selector("#voiceTalkBtn")
    _wait_for_backend_ready(page)


def _fire_turn(page: Page, **kwargs):
    """Starts submitTurn() without awaiting it, so mid-turn state (a pending
    retry, a mid-stream drop) is inspectable. Playwright only awaits a
    *returned* promise, and this call is fire-and-forget on purpose.

    Always supplies a real (fake-content) Blob -- FormData.append()'s 3-arg
    form requires an actual Blob, which a JSON-serialized Python value can
    never satisfy, and #801's retry/held-recording mechanics only have
    something to exercise when the turn genuinely carries one."""
    page.evaluate(
        "(extra) => { "
        "  var blob = new Blob(['fake-audio-bytes'], { type: 'audio/webm' }); "
        "  window.lifeChatVoice.submitTurn(Object.assign({ blob: blob, mime: 'audio/webm' }, extra)); "
        "}",
        kwargs,
    )


def _fetch_count(page: Page, url_substr):
    return page.evaluate(
        "(s) => window.__fetchLog.filter((c) => c.url.indexOf(s) !== -1).length", url_substr
    )


def _thread(page: Page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#messages .message')].map("
        "  (m) => [m.className, (m.querySelector('.message-content')||{}).textContent || ''])"
    )


def _user_texts(page: Page):
    return [text for cls, text in _thread(page) if "user" in cls]


def _fast_forward_past_backoff(page: Page, base_ms, expect_fetch_count):
    """Advances the fake clock past one jittered backoff window (base_ms,
    +25% margin over the actual +/-20% jitter), then waits for the resulting
    retry attempt to actually land. fetch() itself resolves via a real
    microtask -- not something the fake clock controls -- so this doesn't
    assume the timer firing and the next attempt land in the same tick."""
    page.clock.fast_forward(int(base_ms * 1.25) + 100)
    page.wait_for_function(
        "(n) => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/turn/stream') !== -1).length >= n",
        arg=expect_fetch_count,
    )


class TestInitialSubmissionRetry:
    """postTurnStart()'s backoff ladder: fetch()-rejection and this repo's
    own 502 are retryable; 4xx never is."""

    def test_recovers_after_transient_failures(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = 'network-fail'; window.__failCount = 2; }")
        _fire_turn(page)

        _fast_forward_past_backoff(page, 1000, 2)
        _fast_forward_past_backoff(page, 3000, 3)

        expect(page.locator("#messages .message.user")).to_have_text("remind me to call mom")
        assert _fetch_count(page, "/api/voice/turn/stream") == 3

    def test_502_is_retried_like_a_network_failure(self, page: Page, chat_base_url):
        """api/routes/voice.py's own 502 means "gateway unreachable" -- never
        a turn that actually ran -- so it gets the same treatment as a raw
        fetch() rejection."""
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = '502'; window.__failCount = 1; }")
        _fire_turn(page)

        _fast_forward_past_backoff(page, 1000, 2)

        expect(page.locator("#messages .message.user")).to_have_text("remind me to call mom")
        assert _fetch_count(page, "/api/voice/turn/stream") == 2

    def test_permanent_failure_holds_the_recording_and_offers_retry(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = 'network-fail'; window.__failCount = 999; }")
        _fire_turn(page)

        # 1 initial attempt + 3 retries = 4, at backoffs 1s/3s/9s.
        _fast_forward_past_backoff(page, 1000, 2)
        _fast_forward_past_backoff(page, 3000, 3)
        _fast_forward_past_backoff(page, 9000, 4)

        expect(page.locator(".voice-turn-status.failed")).to_be_visible()
        expect(page.locator(".voice-turn-retry-btn")).to_be_visible()
        assert _fetch_count(page, "/api/voice/turn/stream") == 4
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is True

    def test_4xx_never_retries(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = '400'; }")
        _fire_turn(page)

        expect(page.locator(".voice-turn-status.failed")).to_be_visible()
        assert _fetch_count(page, "/api/voice/turn/stream") == 1
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is True


class TestMidStreamDrop:
    """A failure AFTER the initial POST answered `ok` never auto-retries --
    handleMidStreamDrop() polls GET /api/voice/audio/{turn_id} (the one thing
    whisper-relay exposes that can answer "did it finish") instead."""

    def test_recovered_when_the_probe_finds_the_completed_audio(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.evaluate(
            "() => { "
            "window.__turnStreamBehavior = 'midstream-drop'; "
            "window.__audioProbeBehavior = 'found'; "
            "window.__voiceFrames = ["
            "  { type: 'started', turn_id: 'turn-mid-1' },"
            "  { type: 'transcript', text: 'call the plumber' }"
            "];"
            "}"
        )
        _fire_turn(page)

        expect(page.locator("#messages .message.assistant")).to_contain_text("tap to hear it")
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is False
        # Never auto-retried -- only the one initial submission attempt.
        assert _fetch_count(page, "/api/voice/turn/stream") == 1
        assert _fetch_count(page, "/api/voice/audio/turn-mid-1") == 1
        # HEAD, not GET -- the probe must never download the clip just to
        # check whether it exists.
        methods = page.evaluate(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/audio/turn-mid-1') !== -1)"
            ".map(c => c.method)"
        )
        assert methods == ["HEAD"]

    def test_unresolved_drop_is_explicit_retry_only_never_auto(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate(
            "() => { "
            "window.__turnStreamBehavior = 'midstream-drop'; "
            "window.__audioProbeBehavior = 'not-found'; "
            "window.__voiceFrames = ["
            "  { type: 'started', turn_id: 'turn-mid-2' },"
            "  { type: 'transcript', text: 'call the plumber' }"
            "];"
            "}"
        )
        _fire_turn(page)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/audio/turn-mid-2') !== -1).length >= 1"
        )
        # 3 poll attempts, 1500ms apart -- fast-forward past both remaining
        # intervals, confirming each poll actually landed before the next.
        page.clock.fast_forward(1600)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/audio/turn-mid-2') !== -1).length >= 2"
        )
        page.clock.fast_forward(1600)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/audio/turn-mid-2') !== -1).length >= 3"
        )

        expect(page.locator(".voice-turn-status.failed")).to_be_visible()
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is True
        # Confirms no blind resubmit happened despite the connection drop --
        # still exactly the one initial POST.
        assert _fetch_count(page, "/api/voice/turn/stream") == 1
        assert _fetch_count(page, "/api/voice/audio/turn-mid-2") == 3


class TestRetryReconciliation:
    """A failed-then-retried turn renders exactly one bubble."""

    def test_retry_tap_reconciles_into_the_same_bubble(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.evaluate(
            "() => { window.__voiceFrames = ["
            "  { type: 'transcript', text: 'first try' },"
            "  { type: 'error', message: 'boom' }"
            "]; }"
        )
        _fire_turn(page)

        expect(page.locator("#messages .message.user")).to_have_text("first try")
        expect(page.locator(".voice-turn-retry-btn")).to_be_visible()
        assert _user_texts(page) == ["first try"]

        page.evaluate(
            "() => { window.__voiceFrames = ["
            "  { type: 'transcript', text: 'first try' },"
            "  { type: 'done', data: { transcript: 'first try', response_text: 'ok now' } }"
            "]; }"
        )
        page.locator(".voice-turn-retry-btn").click()

        expect(page.locator("#messages .message.user")).to_have_count(1)
        expect(page.locator("#messages .message.user")).to_have_text("first try")
        expect(page.locator(".voice-turn-status.failed")).to_have_count(0)
        assert _user_texts(page) == ["first try"]
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is False

    def test_dismiss_discards_the_recording_without_retrying(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.evaluate("() => { window.__turnStreamBehavior = '400'; }")
        _fire_turn(page)
        expect(page.locator(".voice-turn-dismiss-btn")).to_be_visible()

        page.locator(".voice-turn-dismiss-btn").click()

        expect(page.locator(".voice-turn-status")).to_have_count(0)
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is False
        # Dismissing must not itself resubmit anything.
        assert _fetch_count(page, "/api/voice/turn/stream") == 1


class TestInteractionProofs:
    """The five behaviors #801 calls out explicitly, each independent of the
    retry/failure mechanics already covered above."""

    def test_cancel_aborts_a_pending_retry_and_discards_the_recording(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = 'network-fail'; window.__failCount = 999; }")
        _fire_turn(page)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/turn/stream') !== -1).length === 1"
        )
        expect(page.locator("#statusText")).to_contain_text("Retrying")

        page.evaluate("() => window.lifeChatVoice.cancelActiveTurn()")
        # Even letting every backoff elapse afterward, no further attempt
        # fires -- the abort tore down the pending wait, not just the UI.
        page.clock.fast_forward(15000)

        assert _fetch_count(page, "/api/voice/turn/stream") == 1
        assert page.evaluate("() => window.lifeChatVoice.hasHeldRecording()") is False
        expect(page.locator("#statusText")).to_have_text("Ready")
        assert _thread(page) == []

    def test_wake_detection_does_not_trigger_during_retry_backoff(self, page: Page, chat_base_url):
        # Listening has to actually be enabled for baseWakeGuardsOk() to have
        # anything to prove -- otherwise it's already false for an unrelated
        # reason and this test would pass for the wrong cause.
        _open_voice_chat(
            page, chat_base_url,
            dock_settings={"mute": True, "auto": False, "fast": False, "listen": True},
        )
        page.clock.install()
        page.evaluate(
            "() => { "
            "window.__turnStreamBehavior = 'network-fail'; window.__failCount = 999; "
            "window.__transcribeResponse = 'hermes'; "
            "}"
        )
        _fire_turn(page)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/turn/stream') !== -1).length === 1"
        )
        # voiceBusy stays true for the whole submitTurn() call, retries
        # included -- baseWakeGuardsOk() (which every wake trigger re-checks,
        # see triggerWakeRecording() in voice.js) already keys off it, so a
        # "Hermes" burst arriving mid-backoff must not start a new recording,
        # even though the transcript genuinely matches.
        triggered = page.evaluate(
            "() => window.lifeChatVoice.checkForWakeWord(new Float32Array(160), 16000)"
        )
        assert triggered is False
        assert page.evaluate("document.getElementById('voiceTalkBtn').classList.contains('recording')") is False

    def test_idle_timeout_cannot_fire_while_a_retry_is_pending(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        page.clock.install()
        page.evaluate("() => { window.__turnStreamBehavior = 'network-fail'; window.__failCount = 999; }")
        _fire_turn(page)
        page.wait_for_function(
            "() => window.__fetchLog.filter(c => c.url.indexOf('/api/voice/turn/stream') !== -1).length === 1"
        )
        # No active recording exists during a retry (it already ended before
        # submitTurn() was called), so the idle-timeout finalizer -- gated on
        # `isRecording` -- can only ever be a no-op here.
        before = _fetch_count(page, "/api/voice/turn/stream")
        page.evaluate("() => window.lifeChatVoice.finalizeIdleTimeout()")
        page.wait_for_timeout(20)
        assert _fetch_count(page, "/api/voice/turn/stream") == before

    def test_auto_continue_does_not_rearm_into_a_failed_state(self, page: Page, chat_base_url):
        _open_voice_chat(
            page, chat_base_url,
            dock_settings={"mute": True, "auto": True, "fast": False, "listen": False},
        )
        page.evaluate("() => { window.__turnStreamBehavior = '400'; }")
        _fire_turn(page)

        expect(page.locator(".voice-turn-status.failed")).to_be_visible()
        # maybeAutoContinue() is only ever reached after a successful
        # playbackChain -- a failed turn's catch block never calls it, so no
        # new recording should start even with Auto on.
        page.wait_for_timeout(100)
        assert page.evaluate("document.getElementById('voiceTalkBtn').classList.contains('recording')") is False
        expect(page.locator("#statusText")).to_have_text("Error")

    def test_a_second_recording_replaces_the_held_one(self, page: Page, chat_base_url):
        """#801: "one held blob, replaced by the next recording -- not an
        unbounded queue." A fresh (non-retry) submitTurn() call clears the
        prior failed turn's status row; its Retry affordance is gone even
        though the bubble/text stays as thread history."""
        _open_voice_chat(page, chat_base_url)
        page.evaluate(
            "() => { window.__voiceFrames = ["
            "  { type: 'transcript', text: 'first' },"
            "  { type: 'error', message: 'boom' }"
            "]; }"
        )
        _fire_turn(page)
        expect(page.locator(".voice-turn-status.failed")).to_be_visible()

        page.evaluate(
            "() => { window.__voiceFrames = ["
            "  { type: 'transcript', text: 'second' },"
            "  { type: 'done', data: { transcript: 'second', response_text: 'ok' } }"
            "]; }"
        )
        _fire_turn(page)

        expect(page.locator(".voice-turn-status.failed")).to_have_count(0)
        assert _user_texts(page) == ["first", "second"]
