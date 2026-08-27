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
from urllib.parse import parse_qs, urlparse

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
                hermes_down=False,
                hermes_status_fails=False, hermes_status_hangs=False, status_timeout_ms=None,
                session_items=None, personas=None, personas_fails=False,
                conversations=None, conversation_list=None):
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

    `personas_fails` answers `/api/personas` with a 500 — a genuine discovery
    *failure*, distinct from the generic `{}` stub any other test falls
    through to (which is a 200 with no `personas` key: a successful response
    that happens to carry zero personas, not a failure to discover any).
    `persona.js` treats those two differently (#607) — only the latter is
    ever persisted as confirmation that a stored persona id is gone.

    `conversations` (#592) maps a conversation id to the body `GET
    /api/conversations/{id}` returns for it (the shape `loadConversation()`
    expects: `{"title": ..., "messages": [{"role": ..., "content": ...}]}`)
    — needed to assert what a backend switch renders for a stored id, instead
    of falling through to the generic `{}` stub.

    `conversation_list` (#607) stubs the body of the sidebar's `GET
    /api/conversations` *list* call (no id in the path) with
    `{"conversations": conversation_list}` — needed to assert what actually
    renders in the sidebar on first load, e.g. a Hermes-tagged thread, rather
    than falling through to the generic `{}` stub (which `loadConversations()`
    treats as an empty list).
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
        conv_id = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if "/api/agent/status" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": agent_available}))
        elif "/api/hermes/status" in url:
            if hermes_status_hangs:
                return  # never fulfill/abort — the request just sits pending
            if hermes_status_fails:
                route.fulfill(status=500, content_type="application/json", body="{}")
            elif hermes_down:
                # Configured but unreachable (#688) — distinct from both
                # "available" and "not configured" via the real server's
                # three-field shape.
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"available": False, "configured": True, "reachable": False}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"available": hermes_available}))
        elif personas_fails and "/api/personas" in url:
            route.fulfill(status=500, content_type="application/json", body="{}")
        elif personas is not None and "/api/personas" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"personas": personas}))
        elif conversations is not None and "/api/conversations/" in url and conv_id in conversations:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(conversations[conv_id]))
        elif conversation_list is not None and "/api/conversations" in url and "/api/conversations/" not in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"conversations": conversation_list}))
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


class TestHermesConfiguredButUnreachable:
    """#688: a Hermes that's configured but down must default `/chat` to
    lifeos (no failed turns at send time) while staying visible — not
    collapsed into the same "hidden" treatment as never-configured."""

    def test_defaults_to_lifeos_not_hermes(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_down=True)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") is None

    def test_hermes_option_stays_visible_but_marked_down(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_down=True)
        expect(page.locator("#backendHermes")).to_be_visible()
        assert page.evaluate(
            "document.body.classList.contains('hermes-down')"
        ) is True

    def test_clicking_the_down_option_does_not_select_it(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_down=True)
        page.locator("#backendHermes").click(force=True)
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        assert page.evaluate("window.lifeChat.config.backend") is None

    def test_reachable_hermes_is_unaffected(self, page: Page, chat_base_url):
        # Sanity check that the down-state machinery doesn't leak into the
        # ordinary configured-and-reachable case.
        _open_chat(page, chat_base_url, hermes_available=True)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        assert page.evaluate(
            "document.body.classList.contains('hermes-down')"
        ) is False


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
    selectable on Hermes. An orchestrating persona's turn reaches the Hermes
    proxy like any other (api/routes/hermes_proxy.py; see
    test_orchestrating_persona_no_longer_rejected in tests/test_hermes_proxy.py,
    #642) rather than the client hiding it from the picker."""

    _PERSONAS = [
        {"id": "primary", "label": "Primary", "capabilities": ["handoff", "agent"], "orchestrates": False},
        {"id": "fitness", "label": "Fitness", "capabilities": [], "orchestrates": False},
        {"id": "doctor", "label": "Doctor", "capabilities": ["handoff", "agent"], "orchestrates": True},
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


class TestOrchestratingPersonaOnHermes:
    """Through #641, an orchestrating persona (e.g. doctor) always ran on
    LifeOS, even with Hermes selected — the spawn path had no Hermes
    equivalent, so the composer diverted its turn to `/api/ask/stream`
    instead of the Hermes proxy. #642 gave Hermes its own way to drive that
    persona (lifeos_agent_spawn, #640) and removed the divert: an
    orchestrating persona's Hermes turn now posts to the Hermes proxy like
    any other persona's. A non-orchestrating persona was already unaffected
    by the old divert, and the agent backend (no persona pass-through at
    all) is unaffected by any of this."""

    _PERSONAS = [
        {"id": "primary", "label": "Primary", "capabilities": ["handoff", "agent"], "orchestrates": False},
        {"id": "fitness", "label": "Fitness", "capabilities": [], "orchestrates": False},
        {"id": "doctor", "label": "Doctor", "capabilities": ["handoff", "agent"], "orchestrates": True},
    ]

    def test_orchestrating_persona_on_hermes_posts_to_hermes_proxy(self, page: Page, chat_base_url):
        # #642: was test_orchestrating_persona_on_hermes_diverts_to_lifeos_
        # endpoint, asserting the opposite — a POST to /api/ask/stream tagged
        # body["backend"] == "hermes". Now there is no divert at all: doctor
        # on Hermes reaches the Hermes proxy exactly like a non-orchestrating
        # persona (test_non_orchestrating_persona_on_hermes_still_posts_to_
        # proxy, below), and carries no `backend` tag — nothing to divert
        # means nothing to tag either.
        _open_chat(page, chat_base_url, hermes_available=True, personas=self._PERSONAS)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        page.locator("#personaPicker").select_option("doctor")

        page.locator("#inputField").fill("fix it")
        with page.expect_request("**/api/hermes/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        body = json.loads(req_info.value.post_data)
        assert body["persona_id"] == "doctor"
        assert "backend" not in body

    def test_non_orchestrating_persona_on_hermes_still_posts_to_proxy(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, hermes_available=True, personas=self._PERSONAS)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        page.locator("#personaPicker").select_option("fitness")

        page.locator("#inputField").fill("log a run")
        with page.expect_request("**/api/hermes/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        body = json.loads(req_info.value.post_data)
        assert body["persona_id"] == "fitness"
        # Never tagged — this turn was never diverted, so there's no LifeOS-
        # native conversation to tag.
        assert "backend" not in body

    def test_primary_on_hermes_still_posts_to_proxy_untagged(self, page: Page, chat_base_url):
        # Primary is the inline orchestrator's own persona, but does not spawn
        # (settings.persona_orchestrates("primary") is False) — it must not be
        # diverted either.
        _open_chat(page, chat_base_url, hermes_available=True, personas=self._PERSONAS)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")

        page.locator("#inputField").fill("hello")
        with page.expect_request("**/api/hermes/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        body = json.loads(req_info.value.post_data)
        assert "backend" not in body

    def test_handoff_but_not_orchestrating_persona_on_hermes_still_posts_to_proxy(self, page: Page, chat_base_url):
        # #643 regression guard, weakened by #642: this used to guard against
        # a capabilities-based inference wrongly diverting a persona carrying
        # `handoff` (like primary/doctor) but `orchestrates: false`. #642
        # removed the divert mechanism entirely, so there's no longer a
        # diversion decision here to get wrong either way — this now just
        # pins the same "reaches the proxy untagged" outcome as
        # test_non_orchestrating_persona_on_hermes_still_posts_to_proxy for a
        # persona with handoff-like capabilities specifically. Kept rather
        # than deleted in case a diversion-style mechanism is ever reintroduced.
        personas = self._PERSONAS + [
            {"id": "scout", "label": "Scout", "capabilities": ["handoff", "agent"], "orchestrates": False},
        ]
        _open_chat(page, chat_base_url, hermes_available=True, personas=personas)
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        page.locator("#personaPicker").select_option("scout")

        page.locator("#inputField").fill("hello")
        with page.expect_request("**/api/hermes/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        body = json.loads(req_info.value.post_data)
        assert body["persona_id"] == "scout"
        # Never tagged — this turn was never diverted, so there's no LifeOS-
        # native conversation to tag.
        assert "backend" not in body

    def test_orchestrating_persona_on_agent_is_not_diverted(self, page: Page, chat_base_url):
        # The agent backend has no persona pass-through at all — select doctor
        # while lifeos (picker visible), then switch to agent; the turn must
        # go to the agent proxy, not get diverted to lifeos.
        _open_chat(page, chat_base_url, agent_available=True, personas=self._PERSONAS)
        page.locator("#personaPicker").select_option("doctor")
        page.locator("#backendAgent").click()
        expect(page.locator("#backendAgent")).to_have_class("backend-option active")

        page.locator("#inputField").fill("fix it")
        with page.expect_request("**/api/agent/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        body = json.loads(req_info.value.post_data)
        assert "persona_id" not in body  # agent backend never sends persona_id
        assert "backend" not in body

    def test_lifeos_turn_still_omits_backend_field(self, page: Page, chat_base_url):
        # Regression guard alongside TestLifeosRequestBodyContract below: a
        # genuine lifeos turn (not diverted) must never carry `backend`.
        _open_chat(page, chat_base_url, hermes_available=False, personas=self._PERSONAS)
        page.locator("#personaPicker").select_option("doctor")
        page.locator("#inputField").fill("fix it")
        with page.expect_request("**/api/ask/stream") as req_info:
            page.locator("#sendBtn").click()
        assert "backend" not in json.loads(req_info.value.post_data)

    def test_switching_to_hermes_keeps_orchestrating_persona_selected(self, page: Page, chat_base_url):
        # AC: switching to Hermes while an orchestrating persona is selected
        # keeps that persona rather than falling back to primary.
        _open_chat(
            page, chat_base_url, hermes_available=True, personas=self._PERSONAS,
            session_items={"lifeos:chat:backend_mode": "lifeos"},
        )
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        page.locator("#personaPicker").select_option("doctor")

        page.locator("#backendHermes").click()
        expect(page.locator("#backendHermes")).to_have_class("backend-option active")
        assert page.locator("#personaPicker").input_value() == "doctor"
        assert page.evaluate("window.lifeChat.config.personaId") == "doctor"

    def test_orchestrates_truth_table_across_backends(self, page: Page, chat_base_url):
        """personaOrchestrates()/personaSupportsHandoff(): neither is ever
        true on Hermes or Agent — only the LifeOS backend actually starts a
        session this client tracks. Through #641, orchestration (not
        handoff) was also true on Hermes because an orchestrating persona's
        turn was diverted back to LifeOS; #642 removed that divert, so
        Hermes now matches Agent here even though the persona itself still
        orchestrates server-side (Hermes just drives it itself, via
        lifeos_agent_spawn, rather than this client tracking a LifeOS-linked
        session for it)."""
        _open_chat(
            page, chat_base_url, agent_available=True, hermes_available=True,
            personas=self._PERSONAS,
            session_items={"lifeos:chat:backend_mode": "lifeos"},
        )
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")
        page.locator("#personaPicker").select_option("doctor")

        def truth():
            return page.evaluate(
                "() => [window.lifeChat.personaOrchestrates(), window.lifeChat.personaSupportsHandoff()]"
            )

        assert truth() == [True, True]  # lifeos: doctor orchestrates AND has handoff

        page.locator("#backendHermes").click()
        assert truth() == [False, False]  # hermes (#642): Hermes drives it itself now, not this client

        page.locator("#backendAgent").click()
        assert truth() == [False, False]  # agent: neither — no persona pass-through at all

        page.locator("#backendLifeos").click()
        assert truth() == [True, True]  # back to lifeos, unchanged

    def test_orchestrates_badge_visible_on_lifeos_only(self, page: Page, chat_base_url):
        # #642: was test_orchestrates_badge_visible_on_lifeos_and_hermes_not_
        # agent, which asserted the badge stayed visible on Hermes too
        # ("still runs on LifeOS, regardless of backend") — no longer true,
        # since a Hermes-selected orchestrating persona no longer runs on
        # LifeOS at all.
        _open_chat(
            page, chat_base_url, agent_available=True, hermes_available=True,
            personas=self._PERSONAS, session_items={"lifeos:chat:backend_mode": "lifeos"},
        )
        badge = page.locator("#orchestratesBadge")
        expect(badge).to_be_hidden()  # primary (default persona) never orchestrates

        page.locator("#personaPicker").select_option("doctor")
        expect(badge).to_be_visible()  # lifeos: this turn really does run on LifeOS

        page.locator("#backendHermes").click()
        expect(badge).to_be_hidden()  # hermes (#642): Hermes drives it itself now, not LifeOS

        page.locator("#backendAgent").click()
        expect(badge).to_be_hidden()  # agent has no persona pass-through at all


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


class TestHermesThreadRendering:
    """#592: a Hermes turn is now persisted server-side like a lifeos one, so
    switching to Hermes with a stored conversation id must render that
    conversation's messages — not the blank view the Agent backend (whose
    history genuinely lives elsewhere) still gets."""

    def test_switching_to_hermes_renders_its_stored_conversation(self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, hermes_available=True,
            session_items={
                "lifeos:chat:backend_mode": "lifeos",
                "lifeos:chat:conv:hermes:primary": "conv-hermes-1",
            },
            conversations={
                "conv-hermes-1": {
                    "title": "A hermes thread",
                    "messages": [
                        {"role": "user", "content": "hello from hermes"},
                        {"role": "assistant", "content": "hi there"},
                    ],
                },
            },
        )
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")

        page.locator("#backendHermes").click()
        expect(page.locator("#chatTitle")).to_have_text("A hermes thread")
        expect(page.locator("#messages")).to_contain_text("hello from hermes")
        expect(page.locator("#messages")).to_contain_text("hi there")

    def test_switching_to_agent_still_shows_a_blank_view(self, page: Page, chat_base_url):
        # Unlike Hermes, the Agent backend's history isn't persisted here —
        # switching to it must not attempt to render a thread.
        _open_chat(
            page, chat_base_url, agent_available=True, hermes_available=True,
            session_items={
                "lifeos:chat:backend_mode": "lifeos",
                "lifeos:chat:conv:agent": "conv-agent-1",
            },
            conversations={
                "conv-agent-1": {
                    "title": "Should not appear",
                    "messages": [{"role": "user", "content": "should not render"}],
                },
            },
        )
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")

        page.locator("#backendAgent").click()
        expect(page.locator("#messages")).not_to_contain_text("should not render")
        assert page.evaluate("window.lifeChat.state.currentConversationId") == "conv-agent-1"


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


class TestSidebarBackendFilter:
    """#596 follow-up: the sidebar's `GET /api/conversations` request must
    carry the selected backend, and must carry the *resolved* one — not
    whatever config.backend happened to be before initBackend()'s async
    default-resolution finished. Regression guard for the gap where
    conversations.js sent only `persona_id`, so the server-side `backend`
    filter (tested directly in tests/test_conversations.py) was never
    actually exercised from the browser and the sidebar showed every
    backend's threads regardless of selection."""

    @staticmethod
    def _conversations_backend_params(page: Page, base_url, **open_chat_kwargs):
        """Opens /chat, collecting every `/api/conversations` request made
        during load, and returns the `backend` query param from each, in
        order. The last entry is what the sidebar ends up showing."""
        requests = []
        page.on(
            "request",
            lambda req: requests.append(parse_qs(urlparse(req.url).query).get("backend", [None])[0])
            if "/api/conversations" in req.url and "/api/conversations/" not in req.url
            else None,
        )
        _open_chat(page, base_url, **open_chat_kwargs)
        return requests

    def test_sidebar_request_carries_resolved_hermes_default(self, page: Page, chat_base_url):
        backends = self._conversations_backend_params(page, chat_base_url, hermes_available=True)
        assert backends, "expected at least one /api/conversations request"
        # The final request — the one whose response the sidebar actually
        # renders — must reflect the resolved default (hermes), not the
        # pre-resolution lifeos assumption loadPersonas() fetched with.
        assert backends[-1] == "hermes"

    def test_sidebar_request_carries_resolved_lifeos_default(self, page: Page, chat_base_url):
        backends = self._conversations_backend_params(page, chat_base_url, hermes_available=False)
        assert backends, "expected at least one /api/conversations request"
        assert backends[-1] == "lifeos"

    def test_sidebar_refetches_with_new_backend_on_switch(self, page: Page, chat_base_url):
        _open_chat(page, chat_base_url, agent_available=True, hermes_available=True,
                    session_items={"lifeos:chat:backend_mode": "lifeos"})
        expect(page.locator("#backendLifeos")).to_have_class("backend-option active")

        with page.expect_request(
            lambda req: "/api/conversations" in req.url and "/api/conversations/" not in req.url
        ) as req_info:
            page.locator("#backendAgent").click()
        params = parse_qs(urlparse(req_info.value.url).query)
        assert params.get("backend") == ["agent"]

        with page.expect_request(
            lambda req: "/api/conversations" in req.url and "/api/conversations/" not in req.url
        ) as req_info:
            page.locator("#backendHermes").click()
        params = parse_qs(urlparse(req_info.value.url).query)
        assert params.get("backend") == ["hermes"]


class TestSidebarInitialLoadRace:
    """#607: the initial sidebar load must wait for backend resolution rather
    than racing it. Before the fix, `persona.js`'s `loadPersonas()` fired an
    unresolved-backend listing (`backend=lifeos`) in parallel with
    `initBackend()`'s corrected one; both requests were in flight
    simultaneously with no guarantee on the order their *responses* landed,
    so whichever settled last silently won the final write to
    `state.allConversations`. Request order alone (asserted by
    `TestSidebarBackendFilter` above) can't catch that — requests are always
    sent in the same order; only responses could arrive out of order. The fix
    removes the early listing entirely, so these tests pin "exactly one
    request, already carrying the resolved backend" rather than re-deriving
    the race.
    """

    @staticmethod
    def _list_request_backends(page: Page, base_url, **open_chat_kwargs):
        requests = []
        page.on(
            "request",
            lambda req: requests.append(parse_qs(urlparse(req.url).query).get("backend", [None])[0])
            if "/api/conversations" in req.url and "/api/conversations/" not in req.url
            else None,
        )
        _open_chat(page, base_url, **open_chat_kwargs)
        return requests

    def test_exactly_one_listing_request_when_default_resolves_to_lifeos(self, page: Page, chat_base_url):
        # Hermes unconfigured: the resolved default (lifeos) is the same value
        # a pre-resolution guess would already use — exactly the "resolves to
        # the default" case the AC calls out. A "list early, then refresh"
        # implementation sends two identical requests here; the fix sends one.
        backends = self._list_request_backends(page, chat_base_url, hermes_available=False)
        assert backends == ["lifeos"]

    def test_exactly_one_listing_request_when_default_resolves_to_hermes(self, page: Page, chat_base_url):
        # Hermes configured: still exactly one request, and it already carries
        # the resolved backend — there's no earlier wrong-backend request left
        # in flight for a slow response to race against.
        backends = self._list_request_backends(page, chat_base_url, hermes_available=True)
        assert backends == ["hermes"]

    def test_hermes_thread_visible_on_first_load_without_switching(self, page: Page, chat_base_url):
        thread = {
            "id": "conv-hermes-boot", "title": "Hermes boot thread",
            "created_at": "2026-08-19T10:00:00", "updated_at": "2026-08-19T10:00:00",
            "message_count": 2, "persona_id": "primary",
        }
        backends = self._list_request_backends(
            page, chat_base_url, hermes_available=True, conversation_list=[thread],
        )
        # The property the issue calls out explicitly: assert the *sent*
        # parameter, not just what renders — with only this one thread present,
        # a wrong filter could still render a correct-looking list by
        # coincidence (that's exactly how the bug shipped undetected).
        assert backends == ["hermes"]
        expect(page.locator(".conversation-title")).to_contain_text("Hermes boot thread")

    def test_reload_produces_the_same_sidebar_as_the_fresh_load(self, page: Page, chat_base_url):
        # Stand-in for the issue's two entry paths (opening /chat directly vs.
        # navigating to CRM and back): both re-run the identical boot sequence,
        # so a reload must resolve to the same backend and render the same list.
        thread = {
            "id": "conv-hermes-boot", "title": "Hermes boot thread",
            "created_at": "2026-08-19T10:00:00", "updated_at": "2026-08-19T10:00:00",
            "message_count": 1, "persona_id": "primary",
        }
        _open_chat(page, chat_base_url, hermes_available=True, conversation_list=[thread])
        expect(page.locator(".conversation-title")).to_contain_text("Hermes boot thread")
        first_backend = page.evaluate("window.lifeChat.config.backend")

        page.reload()
        page.wait_for_selector("#backendLifeos", state="attached")
        _wait_for_backend_ready(page)
        expect(page.locator(".conversation-title")).to_contain_text("Hermes boot thread")
        assert page.evaluate("window.lifeChat.config.backend") == first_backend


class TestSidebarPersonaValidationRace:
    """#607 follow-up: moving the sidebar's single listing into initBackend()
    fixed the backend race, but `config.personaId` isn't fully resolved just
    because `loadPersonas()` has started. `persona.js` restores the stored id
    synchronously, but only *validates* it — falling back to `primary` if the
    fetched persona list doesn't contain it — after its own `/api/personas`
    await. `loadPersonas()` runs unawaited alongside `initBackend()`, so if the
    persona fetch resolves slower than the (near-instant) backend availability
    checks, the single listing could fire while `config.personaId` still holds
    a stale, unvalidated id.

    Simulating that ordering with a timed delay doesn't work here: Playwright's
    sync API dispatches every route callback on one thread via greenlet
    switching, so a callback that blocks (`time.sleep`) blocks *every other*
    route's callback too — it doesn't produce "backend resolves fast, persona
    resolves slow", it just serializes everything behind the sleep. Instead,
    the `/api/personas` route is held open (never fulfilled) until the test
    explicitly releases it, which lets the other routes resolve immediately
    while persona resolution provably hasn't happened yet.
    """

    def test_listing_waits_for_persona_validation_before_firing(self, page: Page, chat_base_url):
        requests = []
        held = {}

        def handler(route):
            url = route.request.url
            if "/api/agent/status" in url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"available": False}))
            elif "/api/hermes/status" in url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"available": False}))
            elif "/api/personas" in url:
                held["route"] = route  # held; released explicitly below
            elif "/api/conversations" in url and "/api/conversations/" not in url:
                requests.append(parse_qs(urlparse(url).query).get("persona_id", [None])[0])
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"conversations": []}))
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")

        # A stored persona id that "/api/personas" (below) will not offer —
        # the reader must fall back to primary, but only once it's actually
        # heard back from that endpoint.
        page.add_init_script(
            "window.sessionStorage.setItem('lifeos:chat:persona_id', 'deleted-bot');"
        )
        page.route("**/api/**", handler)
        page.goto(f"{chat_base_url}/chat")
        page.wait_for_selector("#backendLifeos", state="attached")

        # Give the stubbed (near-instant) backend availability checks ample
        # real time to resolve while /api/personas is still deliberately held
        # open — if the sidebar lists on backend resolution alone, it does so
        # in this window, before persona validation could possibly have run.
        page.wait_for_timeout(300)
        assert requests == [], (
            "the sidebar listed before persona validation finished — it must "
            "wait on BOTH backend and persona resolution, not backend alone"
        )

        # Release "/api/personas": "deleted-bot" isn't in the list, so
        # loadPersonas() must correct config.personaId to "primary" before
        # the (still-pending) listing is allowed to fire.
        held["route"].fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"personas": [{"id": "primary", "label": "Primary"}]}),
        )
        _wait_for_backend_ready(page)  # only resolves once persona is unblocked too
        assert requests == ["primary"]
        assert page.evaluate("window.lifeChat.config.personaId") == "primary"


class TestSidebarListsDespitePersonaFailure:
    """#607 follow-up: `backend.js` now awaits `loadPersonas()`'s promise
    before its single listing, which opens a new zero-listing path if that
    promise ever rejects — `await personasReady` with no catch would throw
    and skip `loadConversations()` entirely, leaving the sidebar permanently
    empty for the whole session (worse than the original bug, which was only
    intermittently wrong). `persona.js` is designed to never reject (every
    risky call is internally try/caught), but the listing must not depend on
    that guarantee holding forever — so `backend.js` swallows a rejection
    with `.catch(() => {})` before proceeding. `__LIFEOS_TEST_FORCE_PERSONAS_REJECT__`
    (mirroring `STATUS_TIMEOUT_MS`'s testability hook) forces the rejection
    this test needs, since every real path in `loadPersonas()` is already
    guarded and can't be made to reject by manipulating stubs alone."""

    def test_listing_still_fires_when_persona_resolution_rejects(self, page: Page, chat_base_url):
        thread = {
            "id": "conv-1", "title": "Should still render", "created_at": "2026-08-19T10:00:00",
            "updated_at": "2026-08-19T10:00:00", "message_count": 1, "persona_id": "primary",
        }
        page.add_init_script("window.__LIFEOS_TEST_FORCE_PERSONAS_REJECT__ = true;")
        _open_chat(page, chat_base_url, hermes_available=False, conversation_list=[thread])
        # backendReady itself must resolve — the swallowed rejection must not
        # propagate out of initBackend() either.
        expect(page.locator(".conversation-title")).to_contain_text("Should still render")


class TestPersonaIdPersistenceOnDiscoveryFailure:
    """#607 follow-up: the in-memory fallback to `primary` when a stored
    persona id can't be confirmed (discovery failed, or succeeded without it)
    is correct for the current boot — every caller needs an answer now. But
    *persisting* that fallback is a separate decision: a transient
    `/api/personas` failure must not permanently overwrite the user's stored
    preference, since discovery might simply succeed on the next load. Only a
    successful response that actually omits the stored id should be written
    back to sessionStorage."""

    STORAGE_KEY = "lifeos:chat:persona_id"

    def test_failed_discovery_does_not_overwrite_stored_persona_id(self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, hermes_available=False,
            session_items={self.STORAGE_KEY: "custom-bot"},
            personas_fails=True,  # a genuine discovery failure, not "zero personas"
        )
        assert page.evaluate("window.lifeChat.config.personaId") == "primary"  # in-memory fallback
        assert page.evaluate(
            f"window.sessionStorage.getItem({json.dumps(self.STORAGE_KEY)})"
        ) == "custom-bot"  # unchanged in storage — never actually confirmed gone

    def test_successful_discovery_without_stored_id_does_overwrite_it(self, page: Page, chat_base_url):
        _open_chat(
            page, chat_base_url, hermes_available=False,
            session_items={self.STORAGE_KEY: "custom-bot"},
            personas=[{"id": "primary", "label": "Primary"}],  # confirmed absent
        )
        assert page.evaluate("window.lifeChat.config.personaId") == "primary"
        assert page.evaluate(
            f"window.sessionStorage.getItem({json.dumps(self.STORAGE_KEY)})"
        ) == "primary"  # overwritten — discovery actually confirmed it's gone
