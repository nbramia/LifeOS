"""Browser test for the web /chat agent-threads UI (#236, Phase 3 + thread view).

Drives the Agents panel, the "run as agent" composer affordance, and the
click-to-open thread view with the `/api/agents/*` endpoints mocked via
Playwright route interception — so the spawn → appear → open → reply flow is
deterministic and free of real worker/LLM side effects. Requires the server
serving the chat page on localhost:8000 (like all browser tests in this repo).
"""
import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

DESKTOP_VIEWPORT = {"width": 1280, "height": 800}


def _install_agent_mocks(page: Page, state: dict):
    """Single dispatcher for all /api/agents/* calls, driven by `state`."""

    def handler(route):
        req = route.request
        path = req.url.split("?")[0]

        if path.endswith("/spawn"):
            body = json.loads(req.post_data or "{}")
            state["spawned"].append(body)
            state["threads"].insert(0, {
                "session_id": "sess_spawned", "task_id": "op_spawned",
                "parent_session_id": None, "status": "running",
                "label": body.get("prompt", "agent task")[:40], "resumable": False,
            })
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"ok": True, "session_id": "sess_spawned",
                                                  "task_id": "op_spawned", "routing": "claude",
                                                  "needs_routing": False}))

        if path.endswith("/reply"):
            state["replies"].append(json.loads(req.post_data or "{}"))
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"ok": True, "status": "queued"}))

        # /threads/{id} detail (has a segment after threads/)
        m = re.search(r"/threads/([^/]+)$", path)
        if m:
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"thread": state["detail"]["thread"],
                                                  "conversation": state["detail"]["conversation"],
                                                  "events": [], "total": 0}))

        # /threads list
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"threads": state["threads"], "total": len(state["threads"])}))

    page.route("**/api/agents/**", handler)


class TestAgentThreadsUI:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        page.set_viewport_size(DESKTOP_VIEWPORT)
        self.state = {
            "threads": [{
                "session_id": "sess_done", "task_id": "op_done",
                "parent_session_id": None, "status": "completed",
                "label": "draft the landlord email", "resumable": True,
                "routing": "claude", "model_label": "Sonnet",
            }],
            "detail": {
                "thread": {"session_id": "sess_done", "task_id": "op_done", "status": "completed",
                           "label": "draft the landlord email", "resumable": True,
                           "routing": "claude", "model_label": "Sonnet"},
                "conversation": [
                    {"role": "user", "text": "draft the landlord email", "tools": []},
                    {"role": "assistant", "text": "Here's a draft for you.",
                     "tools": [{"name": "lifeos_gmail_draft", "input": {"to": "landlord@example.com"}}]},
                ],
            },
            "spawned": [], "replies": [],
        }
        _install_agent_mocks(page, self.state)
        page.goto("http://localhost:8000")
        page.wait_for_selector("#agentsPanel")

    def test_panel_lists_threads_with_status(self, page: Page):
        expect(page.locator("#agentsThreadsList .agent-thread")).to_have_count(1)
        expect(page.locator(".agent-badge.completed")).to_be_visible()
        expect(page.locator(".agent-thread-label")).to_contain_text("landlord")

    def test_thread_shows_cloud_local_route_indicator(self, page: Page):
        # routing="claude" → a cloud badge in the panel.
        expect(page.locator("#agentsThreadsList .agent-route.cloud")).to_be_visible()
        expect(page.locator("#agentsThreadsList .agent-route.cloud")).to_contain_text("cloud")
        # And in the thread banner once opened.
        page.locator("#agentsThreadsList .agent-thread").first.click()
        expect(page.locator(".agent-thread-banner .agent-route.cloud")).to_be_visible(timeout=8000)

    def test_composer_has_run_as_agent_controls(self, page: Page):
        expect(page.locator("#agentRouting")).to_be_visible()
        expect(page.locator("#agentSpawnBtn")).to_be_visible()
        values = page.locator("#agentRouting option").evaluate_all("els => els.map(e => e.value)")
        assert set(values) == {"auto", "local", "claude"}

    def test_spawn_from_composer_appears_in_panel(self, page: Page):
        page.locator("#inputField").fill("research the best CRMs")
        page.locator("#agentRouting").select_option("claude")
        page.locator("#agentSpawnBtn").click()
        expect(page.locator(".agent-badge.running")).to_be_visible(timeout=8000)
        assert self.state["spawned"] and self.state["spawned"][0]["routing"] == "claude"

    def test_click_thread_opens_conversation_in_main_body(self, page: Page):
        page.locator("#agentsThreadsList .agent-thread").first.click()
        # The thread banner + reconstructed conversation render in the main body.
        expect(page.locator(".agent-thread-banner")).to_be_visible(timeout=8000)
        expect(page.locator("#messages")).to_contain_text("draft the landlord email")
        expect(page.locator("#messages")).to_contain_text("Here's a draft for you.")
        # Tool call shown as collapsible detail.
        expect(page.locator(".agent-tool-details summary")).to_contain_text("lifeos_gmail_draft")

    def test_reply_from_thread_view_continues_thread(self, page: Page):
        page.locator("#agentsThreadsList .agent-thread").first.click()
        expect(page.locator(".agent-thread-banner")).to_be_visible(timeout=8000)
        # The main composer now continues the thread.
        page.locator("#inputField").fill("also CC the property manager")
        page.locator("#sendBtn").click()
        page.wait_for_timeout(500)
        assert self.state["replies"], "reply POST should have fired"
        assert self.state["replies"][0]["text"] == "also CC the property manager"

    def test_small_thread_renders_without_load_earlier(self, page: Page):
        # The 2-turn fixture is below the cap, so no "load earlier" control
        # appears and both turns render — small threads stay unchanged (#270).
        page.locator("#agentsThreadsList .agent-thread").first.click()
        expect(page.locator(".agent-thread-banner")).to_be_visible(timeout=8000)
        expect(page.locator(".thread-load-earlier")).to_have_count(0)
        expect(page.locator("#messages .message")).to_have_count(2)

    def test_tool_details_pre_is_built_lazily(self, page: Page):
        # The collapsed <details> exists but its <pre> is only built on expand,
        # so a thread with many tool calls doesn't serialize every input up-front.
        page.locator("#agentsThreadsList .agent-thread").first.click()
        expect(page.locator(".agent-tool-details summary")).to_contain_text("lifeos_gmail_draft")
        expect(page.locator(".agent-tool-details pre")).to_have_count(0)
        page.locator(".agent-tool-details summary").first.click()
        expect(page.locator(".agent-tool-details pre")).to_contain_text("landlord@example.com")


def _large_conversation(n_turns: int):
    """Alternating user/assistant turns; assistant turns carry a tool call."""
    conv = []
    for i in range(n_turns):
        if i % 2 == 0:
            conv.append({"role": "user", "text": f"user message {i}", "tools": []})
        else:
            conv.append({"role": "assistant", "text": f"assistant message {i}",
                         "tools": [{"name": "lifeos_search", "input": {"q": f"query {i}"}}]})
    return conv


class TestLargeThreadRendering:
    """#270 — a thread with a very large transcript must render bounded, not
    freeze the renderer, while keeping full history reachable."""

    N_TURNS = 90  # well above THREAD_INITIAL_TURNS (30)

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        page.set_viewport_size(DESKTOP_VIEWPORT)
        conv = _large_conversation(self.N_TURNS)
        self.state = {
            "threads": [{
                "session_id": "sess_big", "task_id": "op_big",
                "parent_session_id": None, "status": "completed",
                "label": "huge transcript", "resumable": True,
                "routing": "claude", "model_label": "Sonnet",
            }],
            "detail": {
                "thread": {"session_id": "sess_big", "task_id": "op_big", "status": "completed",
                           "label": "huge transcript", "resumable": True,
                           "routing": "claude", "model_label": "Sonnet"},
                "conversation": conv,
            },
            "spawned": [], "replies": [],
        }
        _install_agent_mocks(page, self.state)
        page.goto("http://localhost:8000")
        page.wait_for_selector("#agentsPanel")

    def test_initial_render_is_capped_to_newest_turns(self, page: Page):
        page.locator("#agentsThreadsList .agent-thread").first.click()
        expect(page.locator(".agent-thread-banner")).to_be_visible(timeout=8000)
        # Only the newest 30 turns render initially, not all 90.
        expect(page.locator("#messages .message")).to_have_count(30)
        # The newest turn is present; an early (hidden) turn is not.
        expect(page.locator("#messages")).to_contain_text(f"message {self.N_TURNS - 1}")
        expect(page.locator("#messages")).not_to_contain_text("user message 0")

    def test_load_earlier_control_reveals_hidden_turns(self, page: Page):
        page.locator("#agentsThreadsList .agent-thread").first.click()
        btn = page.locator(".thread-load-earlier")
        expect(btn).to_be_visible(timeout=8000)
        expect(btn).to_contain_text("60 hidden")  # 90 - 30 shown
        btn.click()
        # Another chunk of 30 loads in; 60 turns now shown, 30 still hidden.
        expect(page.locator("#messages .message")).to_have_count(60)
        expect(btn).to_contain_text("30 hidden")

    def test_full_history_reachable_via_repeated_load(self, page: Page):
        page.locator("#agentsThreadsList .agent-thread").first.click()
        btn = page.locator(".thread-load-earlier")
        expect(btn).to_be_visible(timeout=8000)
        btn.click()  # 60 shown
        btn.click()  # 90 shown — all loaded
        expect(page.locator("#messages .message")).to_have_count(self.N_TURNS)
        # Button removes itself once nothing is left to load.
        expect(page.locator(".thread-load-earlier")).to_have_count(0)
        expect(page.locator("#messages")).to_contain_text("user message 0")

    def test_poll_rerender_preserves_expanded_history(self, page: Page):
        # When new turns land on an active thread (the poll path re-renders),
        # any history the user expanded via "load earlier" must not collapse
        # back to the newest-N tail.
        page.locator("#agentsThreadsList .agent-thread").first.click()
        btn = page.locator(".thread-load-earlier")
        expect(btn).to_be_visible(timeout=8000)
        btn.click()  # 60 shown, earliestShown == 30
        expect(page.locator("#messages .message")).to_have_count(60)
        # Simulate the poll re-render: two new turns appended, re-rendered with
        # the expanded depth preserved (exactly what pollThreadForUpdate does).
        shown_after = page.evaluate(
            """(n) => {
                const conv = [];
                for (let i = 0; i < n + 2; i++) {
                    conv.push({ role: i % 2 ? 'assistant' : 'user', text: 'm' + i, tools: [] });
                }
                renderThreadConversation(conv, threadRender.earliestShown);
                return document.querySelectorAll('#messages .message').length;
            }""",
            self.N_TURNS,
        )
        # Was showing turns [30, 92): the 60 already-visible plus the 2 new ones.
        assert shown_after == 62, f"expected expansion preserved (62), got {shown_after}"
        expect(page.locator("#messages")).to_contain_text("m30")
        expect(page.locator("#messages")).not_to_contain_text("m29")
