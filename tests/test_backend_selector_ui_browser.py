"""Browser tests for the three-way LifeOS|Agent|Hermes backend selector (#587).

Drives `web/chat/backend.js` through the real toolbar buttons: conditional
default resolution (hermes if configured, else lifeos — including when the
availability check fails), explicit-choice persistence across a reload, and
per-backend conversation-id isolation.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, because the assertions are about
the JS in *this* checkout and every API call the page makes is intercepted
anyway. That is why it carries no `requires_server` marker, and so runs at
pre-push (`browser and not requires_server`) — see
tests/test_voice_mic_block_ui_browser.py for the same pattern.
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


def _wait_for_backend_ready(page: Page):
    """Await `backend.js`'s `initBackend()` promise (stashed on the bridge as
    `window.lifeChat.backendReady`) — the only reliable signal that default
    resolution has actually finished. Polling DOM state instead is NOT
    equivalent: `#backendLifeos` already carries the `active` class in
    index.html's static, pre-JS markup, and `config.backend` starts `null` (the
    same value the `lifeos` default resolves to) — so a "resolved to lifeos"
    assertion would pass even if `initBackend()` had never run. If the promise
    never resolves, this call times out and the test fails loudly instead of
    silently matching the pre-resolution state.
    """
    page.evaluate(
        "async () => {"
        "  while (!(window.lifeChat && window.lifeChat.backendReady)) {"
        "    await new Promise((r) => setTimeout(r, 10));"
        "  }"
        "  await window.lifeChat.backendReady;"
        "}"
    )


def _open_chat(page: Page, base_url, *, agent_available=False, hermes_available=True,
                hermes_status_fails=False, hermes_status_hangs=False, status_timeout_ms=None,
                session_items=None, personas=None):
    """Load `/chat` with `/api/agent/status` and `/api/hermes/status` stubbed,
    and every other `/api/` call stubbed empty so nothing depends on a running
    server. `session_items` are written to sessionStorage before any app JS
    runs (stored backend preference, seeded per-backend conversation ids).

    `hermes_status_hangs` never responds to `/api/hermes/status` at all —
    simulating a wedged server rather than a fast error response — paired with
    `status_timeout_ms` to shrink `backend.js`'s real 5s abort timeout down to
    something a test can wait out (see the `STATUS_TIMEOUT_MS` testability
    hook in backend.js).

    `personas` stubs `/api/personas` with a specific persona list (the shape
    `GET /api/personas` returns, e.g. `[{"id": "doctor", "label": "Doctor"}]`)
    instead of falling through to the generic `{}` stub — needed to assert
    what `renderPersonaOptions()` does with more than the bare primary persona.
    """
    if status_timeout_ms is not None:
        page.add_init_script(
            f"window.__LIFEOS_TEST_STATUS_TIMEOUT_MS__ = {int(status_timeout_ms)};"
        )
    if session_items:
        page.add_init_script(
            "".join(
                f"window.sessionStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
                for k, v in session_items.items()
            )
        )

    def handler(route):
        url = route.request.url
        if "/api/agent/status" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": agent_available}))
        elif "/api/hermes/status" in url:
            if hermes_status_hangs:
                return  # never fulfill/abort — the request just sits pending
            if hermes_status_fails:
                route.fulfill(status=500, content_type="application/json", body="{}")
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"available": hermes_available}))
        elif personas is not None and "/api/personas" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"personas": personas}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/chat")
    # "attached", not the default "visible" — the whole toggle group is
    # legitimately hidden when neither Agent nor Hermes is configured.
    page.wait_for_selector("#backendLifeos", state="attached")
    _wait_for_backend_ready(page)


class TestDefaultBackendResolution:
    """No stored preference: hermes if available, else lifeos — including on
    a failed/erroring availability check."""

    def test_defaults_to_hermes_when_available(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_available=True)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        expect(page.locator("#backendLifeos")).to_have_class("backend-option")
        assert page.evaluate("window.lifeChat.config.backend") == "hermes"

    def test_defaults_to_lifeos_when_hermes_unavailable(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_available=False)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        expect(page.locator("#backendHermes")).to_have_class("backend-option")
        assert page.evaluate("window.lifeChat.config.backend") is None

    def test_defaults_to_lifeos_when_hermes_status_check_fails(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_status_fails=True)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") is None
        # A failed check leaves hermes looking unconfigured → not offered either.
        expect(page.locator("#backendHermes")).to_be_hidden()

    def test_defaults_to_lifeos_when_hermes_status_check_times_out(self, page: Page, chat_base_url):
        # The status route never responds; backend.js's own abort timeout is
        # shrunk to 100ms (via the testability hook) so this doesn't wait 5s.
        _open_chat(page, chat_base_url, hermes_status_hangs=True, status_timeout_ms=100)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") is None
        expect(page.locator("#backendHermes")).to_be_hidden()

    def test_hermes_option_hidden_when_not_configured(self, page: Page, chat_base_url):
        # agent_available=True keeps the toggle group itself visible, isolating
        # the per-button hide rule from the "hide the whole group" one.
        _open_chat(page, chat_base_url, agent_available=True, hermes_available=False)
        expect(page.locator("#backendHermes")).to_be_hidden()
        expect(page.locator(".backend-toggle")).to_be_visible()


class TestExplicitChoicePersistence:
    """A hand-picked backend survives a reload and is not overridden by the
    conditional default, even when hermes is available."""

    def test_explicit_agent_choice_persists_over_reload(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, agent_available=True, hermes_available=True)
        page.locator("#backendAgent").click()
        expect(page.locator("#backendAgent")).to_have_class("backend-option active")

        page.reload()
        page.wait_for_selector("#backendLifeos", state="attached")
        _wait_for_backend_ready(page)
        expect(page.locator("#backendAgent")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") == "agent"

    def test_stored_lifeos_preference_not_overridden_by_available_hermes(self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, hermes_available=True,
            session_items={"lifeos:chat:backend_mode": "lifeos"},
        )
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") is None


class TestBackendModeUi:
    """Hermes keeps the persona picker visible but hides the per-turn model
    picker; the selector refuses to switch mid-turn."""

    def test_hermes_mode_hides_model_picker_keeps_persona_picker(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_available=True)
        expect(page.locator("#personaPicker")).to_be_visible()
        expect(page.locator("#modelPicker")).to_be_hidden()

    def test_switch_is_refused_while_a_turn_is_in_flight(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, agent_available=True, hermes_available=True)
        # _open_chat() already awaited default resolution; hermes wins (available).
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") == "hermes"
        page.evaluate("window.lifeChat.state.isLoading = true")
        page.locator("#backendAgent").click()
        # The click was refused — still on hermes.
        assert page.evaluate("window.lifeChat.config.backend") == "hermes"
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")


class TestPersonaPickerAcrossBackends:
    """AC (#590): 'The persona picker's contents shall not change based on
    the selected backend' — every persona, orchestrating or not, stays
    selectable on Hermes. The proxy rejects an orchestrating persona's turn
    server-side (api/routes/hermes_proxy.py, test_orchestrating_persona_400_
    and_not_forwarded in tests/test_hermes_proxy.py) rather than the client
    hiding it from the picker."""

    _PERSONAS = [
        {"id": "primary", "label": "Primary", "capabilities": ["handoff", "agent"]},
        {"id": "fitness", "label": "Fitness", "capabilities": []},
        {"id": "doctor", "label": "Doctor", "capabilities": ["handoff", "agent"]},
    ]

    @staticmethod
    def _picker_option_ids(page: Page):
        return page.eval_on_selector_all("#personaPicker option", "opts => opts.map(o => o.value)")

    def test_picker_contents_identical_on_lifeos_and_hermes(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_available=False, personas=self._PERSONAS)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        lifeos_ids = self._picker_option_ids(page)
        # Doctor (orchestrates=true) is offered on lifeos too — proves this
        # isn't an artifact of the stub, but the actual full persona list.
        assert lifeos_ids == ["primary", "fitness", "doctor"]

        _open_chat(page, chat_base_url, hermes_available=True, personas=self._PERSONAS)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        hermes_ids = self._picker_option_ids(page)

        # The AC under test: the picker is identical regardless of backend.
        assert hermes_ids == lifeos_ids


class TestPerBackendConversationIsolation:
    """Each backend's conversation id lives under its own sessionStorage key,
    and switching backends restores the right one."""

    def test_each_backend_restores_its_own_conversation_id(self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, agent_available=True, hermes_available=True,
            session_items={
                "lifeos:chat:backend_mode": "lifeos",
                "lifeos:chat:conv:lifeos:primary": "conv-lifeos-1",
                "lifeos:chat:conv:agent": "conv-agent-1",
                "lifeos:chat:conv:hermes:primary": "conv-hermes-1",
            },
        )
        # _open_chat() already awaited default resolution (stored pref: lifeos).
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-lifeos-1"

        page.locator("#backendAgent").click()
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-agent-1"

        page.locator("#backendHermes").click()
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-hermes-1"

        page.locator("#backendLifeos").click()
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-lifeos-1"

    def test_hermes_conversation_key_is_persona_scoped_and_distinct_from_agent(
            self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, agent_available=True, hermes_available=True,
            session_items={
                "lifeos:chat:backend_mode": "hermes",
                "lifeos:chat:conv:hermes:primary": "conv-hermes-primary",
                "lifeos:chat:conv:agent": "conv-agent-only",
            },
        )
        # _open_chat() already awaited default resolution (stored pref: hermes).
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        # Distinct key from the agent backend's conversation id.
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-hermes-primary"
        page.locator("#backendAgent").click()
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-agent-only"


class TestLifeosRequestBodyContract:
    """Pins the last acceptance criterion: 'a turn sent on the lifeos backend
    shall produce a request body byte-identical to the one produced before
    this change'. #587 must not silently add a `backend` or `model_override`
    field to the common case — a fresh session, default persona, default
    model, no prior conversation."""

    def test_lifeos_turn_posts_byte_identical_body(self, page: Page, chat_base_url):
        # hermes_available=False so default resolution lands on lifeos.
        _open_chat(page, chat_base_url, hermes_available=False)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")

        page.locator("#inputField").fill("hello there")
        with page.expect_request("**/api/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        posted_body = req_info.value.post_data

        # The whole object, not "contains a key" — this must fail if anything
        # ever adds backend: "lifeos" or model_override: "auto" to a lifeos turn.
        # separators matches JSON.stringify()'s compact output (no spaces).
        assert posted_body == json.dumps(
            {"question": "hello there", "persona_id": "primary"}, separators=(",", ":")
        )
