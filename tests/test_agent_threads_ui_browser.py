"""Browser test for the web /chat agent-threads UI (#236, Phase 3).

Drives the Agents panel + "run as agent" composer affordance with the
`/api/agents/*` endpoints mocked via Playwright route interception, so the
spawn → appear → reply flow is deterministic and free of real worker/LLM
side effects. Requires the server serving the chat page on localhost:8000
(like all browser tests in this repo).
"""
import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

DESKTOP_VIEWPORT = {"width": 1280, "height": 800}


def _install_agent_mocks(page: Page, state: dict):
    """Route /api/agents/* to in-memory mock responses driven by `state`."""

    def threads_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"threads": state["threads"], "total": len(state["threads"])}),
        )

    def spawn_handler(route):
        body = json.loads(route.request.post_data or "{}")
        state["spawned"].append(body)
        # The spawned agent now shows up as a running thread.
        state["threads"].insert(0, {
            "session_id": "sess_spawned",
            "task_id": "op_spawned",
            "parent_session_id": None,
            "status": "running",
            "label": body.get("prompt", "agent task")[:40],
            "resumable": False,
        })
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "session_id": "sess_spawned",
                                       "task_id": "op_spawned", "routing": "claude",
                                       "needs_routing": False}))

    def reply_handler(route):
        state["replies"].append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "status": "queued"}))

    page.route("**/api/agents/threads*", threads_handler)
    page.route("**/api/agents/spawn", spawn_handler)
    page.route("**/api/agents/threads/*/reply", reply_handler)


class TestAgentThreadsUI:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        page.set_viewport_size(DESKTOP_VIEWPORT)
        self.state = {
            "threads": [{
                "session_id": "sess_done",
                "task_id": "op_done",
                "parent_session_id": None,
                "status": "completed",
                "label": "draft the landlord email",
                "resumable": True,
            }],
            "spawned": [],
            "replies": [],
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
        # Routing options: auto / local / cloud.
        values = page.locator("#agentRouting option").evaluate_all(
            "els => els.map(e => e.value)"
        )
        assert set(values) == {"auto", "local", "claude"}

    def test_spawn_from_composer_appears_in_panel(self, page: Page):
        page.locator("#inputField").fill("research the best CRMs")
        page.locator("#agentRouting").select_option("claude")
        page.locator("#agentSpawnBtn").click()
        # The mock inserts a running thread; the panel polls/refreshes.
        expect(page.locator(".agent-badge.running")).to_be_visible(timeout=8000)
        assert self.state["spawned"], "spawn POST should have fired"
        assert self.state["spawned"][0]["routing"] == "claude"

    def test_reply_to_completed_thread(self, page: Page):
        # Open the reply box on the completed (resumable) thread and send.
        page.locator("#reply-sess_done").wait_for(state="attached")
        page.locator('.agent-thread-top button[title="Reply"]').click()
        box = page.locator("#reply-sess_done input")
        box.fill("also CC Jane")
        box.press("Enter")
        page.wait_for_timeout(500)
        assert self.state["replies"], "reply POST should have fired"
        assert self.state["replies"][0]["text"] == "also CC Jane"
