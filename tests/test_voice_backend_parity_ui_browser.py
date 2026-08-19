"""Browser tests for spoken-turn backend/persona parity (#593).

Drives `web/chat/voice.js`'s `submitTurn()` directly -- the seam it's exported
for specifically so a headless harness can drive a turn without a real mic
(getUserMedia/MediaRecorder don't run headless) -- across the three text
backends, asserting the exact fields a spoken turn sends to
`POST /api/voice/turn/stream`. Companion to
tests/test_backend_selector_ui_browser.py, which covers the equivalent
field assembly for the *text* path (askStream()).

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, and it replaces `window.fetch`
outright rather than intercepting at the Playwright network layer. That gets
two things a network-level intercept can't: the exact FormData object
voice.js built (no multipart reparsing), and a fetch call log appended
synchronously -- before any await -- so a test asserting a call did NOT
happen (the pending-question-polling gate) isn't racing an in-flight
promise. See tests/test_voice_mic_block_ui_browser.py and
tests/test_backend_selector_ui_browser.py for the same serve-web-directly
pattern. No `requires_server` marker, so this runs at pre-push
(`browser and not requires_server`).
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


# Installed before any app JS runs. Replaces window.fetch entirely so every
# call the SPA makes on load (personas, backend status, chat config) resolves
# without a real server. A call to /api/voice/turn/stream is special-cased:
# its FormData is captured as-is (not reparsed off the wire) and a canned SSE
# `done` frame is returned so submitTurn() completes normally.
# `window.__fetchLog` records every call (url, method) the instant fetch() is
# invoked -- synchronously, before any await -- specifically so a test
# asserting a call did NOT happen isn't racing an in-flight promise.
_FETCH_MOCK = """
window.__fetchLog = [];
window.__voiceFormCaptures = [];
window.__voiceMockDone = { type: 'done', data: { response_text: 'ok' } };
(function () {
  window.fetch = function (url, opts) {
    var urlStr = typeof url === 'string' ? url : (url && url.url) || String(url);
    var method = (opts && opts.method) || 'GET';
    window.__fetchLog.push({ url: urlStr, method: method });
    if (urlStr.indexOf('/api/voice/turn/stream') !== -1) {
      var fields = {};
      if (opts && opts.body && typeof opts.body.entries === 'function') {
        var it = opts.body.entries();
        var entry = it.next();
        while (!entry.done) {
          fields[entry.value[0]] = entry.value[1];
          entry = it.next();
        }
      }
      window.__voiceFormCaptures.push(fields);
      var sse = 'data: ' + JSON.stringify(window.__voiceMockDone) + '\\n\\n';
      // A real macrotask delay (not just a resolved-promise microtask chain)
      // -- needed so a fire-and-forget submitTurn() call (see _fire_turn())
      // reliably leaves a window where the in-flight status text can be
      // observed before the turn completes.
      return new Promise(function (resolve) {
        setTimeout(function () {
          resolve(new Response(sse, {
            status: 200, headers: { 'Content-Type': 'text/event-stream' },
          }));
        }, 30);
      });
    }
    return Promise.resolve(new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
  };
})();
"""


def _wait_for_backend_ready(page: Page):
    """Same wait test_backend_selector_ui_browser.py uses: `config.backend`
    starts null (the same value the lifeos default resolves to), so only
    awaiting `initBackend()`'s stashed promise proves resolution actually ran."""
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


def _set_backend(page: Page, backend):
    """`backend`: None (lifeos), 'agent', or 'hermes' -- mirrors backend.js's
    own null-means-lifeos convention for config.backend."""
    page.evaluate("(b) => { window.lifeChat.config.backend = b; }", backend)


def _set_persona(page: Page, persona_id, personas=None):
    page.evaluate(
        "(args) => { window.lifeChat.config.personaId = args.id; "
        "window.lifeChat.config.personas = args.personas || []; }",
        {"id": persona_id, "personas": personas},
    )


def _set_model(page: Page, model):
    page.evaluate("(m) => { window.lifeChat.config.model = m; }", model)


def _set_mock_done(page: Page, done_data):
    page.evaluate("(d) => { window.__voiceMockDone = { type: 'done', data: d }; }", done_data)


def _submit_turn(page: Page, transcript="hello"):
    """Awaits submitTurn() to completion -- Playwright awaits a returned
    promise before resolving the call."""
    page.evaluate("(t) => window.lifeChatVoice.submitTurn({ transcript: t })", transcript)


def _fire_turn(page: Page, transcript="hello"):
    """Starts submitTurn() but does not await it, so the in-flight status
    text (set synchronously before submitTurn's first await) can be
    inspected. The wrapper itself returns undefined synchronously, and
    Playwright only awaits a *returned* promise."""
    page.evaluate(
        "(t) => { window.lifeChatVoice.submitTurn({ transcript: t }); }", transcript
    )


def _last_voice_form(page: Page):
    return page.evaluate("window.__voiceFormCaptures[window.__voiceFormCaptures.length - 1]")


def _polled_conversation(page: Page, conv_id):
    """Whether a GET to /api/conversations/{conv_id} happened -- the exact
    request shape pending-question.js's conversationEndpoint() builds."""
    log = page.evaluate("window.__fetchLog")
    target = f"/api/conversations/{conv_id}"
    return any(entry["method"] == "GET" and entry["url"].endswith(target) for entry in log)


# An orchestrating persona (e.g. doctor), matching personaOrchestrates()'s own
# gate: non-primary + a persona carrying the `handoff` capability.
_ORCHESTRATING_PERSONA = {"id": "doctor", "label": "Doctor", "capabilities": ["handoff"]}


class TestSpokenTurnFieldsAcrossBackends:
    """AC: persona rides along for lifeos and hermes; model_override stays
    lifeos-only; a lifeos turn's fields are unchanged from today."""

    def test_hermes_backend_sends_persona_but_not_model_override(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "hermes")
        _set_persona(page, "fitness")
        _set_model(page, "opus")
        _submit_turn(page)
        form = _last_voice_form(page)
        assert form["backend"] == "hermes"
        assert form["persona_id"] == "fitness"
        assert "model_override" not in form

    def test_lifeos_backend_fields_unchanged(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, None)
        _set_persona(page, "fitness")
        _set_model(page, "opus")
        _submit_turn(page)
        form = _last_voice_form(page)
        assert form["backend"] == "lifeos"
        assert form["persona_id"] == "fitness"
        assert form["model_override"] == "opus"

    def test_lifeos_backend_omits_default_model_pick(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, None)
        _set_persona(page, "fitness")
        _set_model(page, "auto")
        _submit_turn(page)
        form = _last_voice_form(page)
        assert "model_override" not in form

    def test_agent_backend_keeps_dropping_persona_and_model(self, page: Page, chat_base_url):
        """Out of scope (#593): the Agent backend keeps its current
        field-dropping behavior -- it has no persona pass-through on either
        surface, mirroring askStream()'s `backend !== 'agent'` gate."""
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "agent")
        _set_persona(page, "fitness")
        _set_model(page, "opus")
        _submit_turn(page)
        form = _last_voice_form(page)
        assert form["backend"] == "agent"
        assert "persona_id" not in form
        assert "model_override" not in form


class TestStatusTextPerBackend:
    """AC: in-flight status text reflects the selected backend across all
    three, not just two."""

    def test_lifeos_status_text(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, None)
        _fire_turn(page)
        expect(page.locator("#statusText")).to_have_text("Thinking…")

    def test_agent_status_text(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "agent")
        _fire_turn(page)
        expect(page.locator("#statusText")).to_have_text("Agent thinking…")

    def test_hermes_status_text(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "hermes")
        _fire_turn(page)
        expect(page.locator("#statusText")).to_have_text("Hermes thinking…")


class TestOrchestratingPersonaPollingGate:
    """AC: an orchestrating persona's spoken turn on the Hermes backend must
    not start pending-question polling. Unlike the text path (#596), voice
    has no client-side diversion of a Hermes-selected orchestrating turn back
    to lifeos -- such a persona_id reaching the Hermes proxy is rejected
    there with a 400 (hermes_proxy.py) rather than spawning anything, so
    there is never a session to poll for."""

    def test_lifeos_backend_starts_polling(self, page: Page, chat_base_url):
        """Positive control: the gate isn't just always-false."""
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, None)
        _set_persona(page, "doctor", personas=[_ORCHESTRATING_PERSONA])
        _set_mock_done(page, {"conversation_id": "conv-593-lifeos", "response_text": "ok"})
        _submit_turn(page)
        assert _polled_conversation(page, "conv-593-lifeos")

    def test_hermes_backend_does_not_start_polling(self, page: Page, chat_base_url):
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "hermes")
        _set_persona(page, "doctor", personas=[_ORCHESTRATING_PERSONA])
        _set_mock_done(page, {"conversation_id": "conv-593-hermes", "response_text": "ok"})
        _submit_turn(page)
        assert not _polled_conversation(page, "conv-593-hermes")

    def test_agent_backend_does_not_start_polling(self, page: Page, chat_base_url):
        """Sanity check: personaOrchestrates() is already false for the agent
        backend (no persona pass-through at all), so this was already true
        before #593 -- pinned here alongside the hermes case for contrast."""
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "agent")
        _set_persona(page, "doctor", personas=[_ORCHESTRATING_PERSONA])
        _set_mock_done(page, {"conversation_id": "conv-593-agent", "response_text": "ok"})
        _submit_turn(page)
        assert not _polled_conversation(page, "conv-593-agent")

    def test_hermes_backend_non_orchestrating_persona_does_not_poll(self, page: Page, chat_base_url):
        """A plain (non-orchestrating) persona on Hermes never polls either --
        proves the hermes case above isn't vacuously true for every persona."""
        _open_voice_chat(page, chat_base_url)
        _set_backend(page, "hermes")
        _set_persona(page, "fitness", personas=[{"id": "fitness", "label": "Fitness", "capabilities": []}])
        _set_mock_done(page, {"conversation_id": "conv-593-hermes-plain", "response_text": "ok"})
        _submit_turn(page)
        assert not _polled_conversation(page, "conv-593-hermes-plain")
