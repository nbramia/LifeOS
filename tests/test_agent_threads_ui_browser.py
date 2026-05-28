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
            }],
            "detail": {
                "thread": {"session_id": "sess_done", "task_id": "op_done", "status": "completed",
                           "label": "draft the landlord email", "resumable": True},
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
