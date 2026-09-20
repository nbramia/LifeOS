"""Browser coverage for the board's derived project presentation and actions."""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _AgentsHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/agents", "/"):
            return str(WEB_DIR / "agents.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def agents_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AgentsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _policy():
    return {
        "assignee": {"allowed": True, "reason": None},
        "fields": {"allowed": True, "reason": None}, "lanes": {},
        "can_start_project": True, "can_plan_project": True,
        "can_complete_project": True, "can_cancel_project": True,
        "can_resume_execution": False,
    }


def _card(card_id, title, **extra):
    card = {
        "kind": "task", "id": card_id, "title": title, "notes": "Synthetic objective",
        "status": "todo", "tags": [], "assignee": "me", "fields": {},
        "context": "Inbox", "updated_at": "2026-09-20T00:00:00+00:00",
        "session": None, "pending_question": None, "policy": _policy(),
        "parent_id": None, "parent_title": None, "is_project": False,
        "child_count": 0, "hierarchy_valid": True, "hierarchy_error": None,
        "project": None,
    }
    card.update(extra)
    return card


def _state():
    parent = _card("project-1", "Release synthetic project", is_project=True, child_count=8,
                   project={
                       "child_count": 8, "resolved_count": 2, "ready_to_close": False,
                       "execution_paused": True, "cancellation_pending": False,
                       "counts": {"done": 1, "awaiting_review": 1, "cancelled": 1,
                                  "blocked": 4, "running": 1, "unassigned": 0, "assigned": 0},
                       "coordinator": {"session_id": "coord-1", "status": "running", "live": True,
                                       "result": "Created a synthetic plan."},
                   })
    child = _card("child-1", "Implement synthetic child", assignee="codex", tags=["codex"],
                  parent_id="project-1", parent_title="Release synthetic project")
    done_child = _card("child-2", "Accepted synthetic child", status="done",
                       tags=["human", "agent-wait-provider", "agent-running", "agent-blocked"],
                       parent_id="project-1", parent_title="Release synthetic project")
    review_child = _card("child-3", "Review synthetic child", status="done", tags=["agent-completed"],
                         parent_id="project-1", parent_title="Release synthetic project")
    cancelled_child = _card("child-cancelled", "Cancelled synthetic child", status="cancelled",
                            tags=["human", "agent-wait-dependency", "agent-running", "agent-blocked"],
                            parent_id="project-1", parent_title="Release synthetic project")
    blocked_children = [
        _card("child-blocked", "Blocked synthetic child", tags=["agent-blocked"],
              parent_id="project-1", parent_title="Release synthetic project"),
        _card("child-human", "Human synthetic child", tags=["human"],
              parent_id="project-1", parent_title="Release synthetic project"),
        _card("child-provider", "Provider synthetic child", tags=["agent-wait-provider"],
              parent_id="project-1", parent_title="Release synthetic project"),
        _card("child-dependency", "Dependency synthetic child", tags=["agent-wait-dependency"],
              parent_id="project-1", parent_title="Release synthetic project"),
    ]
    return {
        "lanes": {"unassigned": [], "assigned": [parent, child], "in_progress": [],
                  "human_queue": [cancelled_child, *blocked_children], "scheduled": [], "review": [review_child],
                  "done": [done_child], "snoozed": []},
        "generated_at": 0, "api_host": "synthetic-host",
    }


def _stub(page: Page, state, seen):
    def d3(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")

    def api(route):
        request = route.request
        url, method = request.url, request.method
        if "/api/agents/board/stream" in url:
            route.fulfill(status=200, content_type="text/event-stream", body="retry: 60000\n: ok\n\n")
        elif url.rstrip("/").endswith("/api/agents/board") and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(state))
        elif "/api/tasks/project-1/children" in url and method == "GET":
            # Match TaskResponse rather than the board-card shape: assignee is
            # derived from tags, and the task title field is ``description``.
            children = [
                {
                    "id": card["id"],
                    "description": card["title"],
                    "status": card["status"],
                    "tags": card["tags"],
                }
                for card in (
                    state["lanes"]["assigned"][1],
                    state["lanes"]["review"][0],
                    state["lanes"]["done"][0],
                    *state["lanes"]["human_queue"],
                )
            ]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tasks": children, "total": 8, "limit": 50, "offset": 0}))
        elif url.rstrip("/").endswith("/api/tasks/project-1/project/cancel") and method == "POST" and request.post_data_json["confirm"] is False:
            seen.append((method, url, request.post_data_json))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "project_id": "project-1", "unfinished_count": 3, "running_count": 1,
                "awaiting_review_count": 1, "children": [], "confirmation_required": True,
            }))
        else:
            seen.append((method, url, request.post_data_json if request.post_data else None))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"complete": True, "id": "child-new"}))

    page.route("**/d3.v7.min.js", d3)
    page.route("**/api/**", api)


def _open(page, base_url, state, seen):
    _stub(page, state, seen)
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="project-1"]')


def test_project_cards_filter_and_navigation(page: Page, agents_base_url):
    state, seen = _state(), []
    _open(page, agents_base_url, state, seen)
    expect(page.locator('[data-card-id="project-1"] .board-chip-project')).to_have_text("project · 2/8 resolved")
    page.select_option("#board-filter-project", "projects")
    expect(page.locator('[data-card-id="project-1"]')).to_be_visible()
    expect(page.locator('[data-card-id="child-1"]')).to_have_count(0)
    page.select_option("#board-filter-project", "all")
    page.locator('[data-card-id="child-1"] .board-chip-parent').click()
    expect(page.locator('#board-drawer [data-field="project-details"]')).to_be_visible()
    child_row = page.locator('#board-drawer [data-field="project-children"] [data-child-id="child-1"]')
    expect(child_row).to_be_visible()
    expect(child_row.locator('.project-child-meta')).to_have_text('codex · todo')
    expect(child_row.locator('[data-action="assign-child"]')).to_have_value('codex')
    for child_id in ("child-blocked", "child-human", "child-provider", "child-dependency"):
        expect(page.locator(f'[data-child-id="{child_id}"] .project-child-meta')).to_have_text(
            'unassigned · blocked'
        )
    expect(page.locator('[data-child-id="child-2"] .project-child-meta')).to_have_text(
        'unassigned · done'
    )
    expect(page.locator('[data-child-id="child-3"] .project-child-meta')).to_have_text(
        'unassigned · awaiting review'
    )
    expect(page.locator('[data-child-id="child-cancelled"] .project-child-meta')).to_have_text(
        'unassigned · cancelled'
    )
    page.locator('#board-drawer [data-action="open-child"]').first.click()
    expect(page.locator('#board-drawer [data-field="parent-navigation"]')).to_be_visible()
    page.locator('#board-drawer [data-action="open-parent"]').click()
    expect(page.locator('#board-drawer [data-field="project-details"]')).to_be_visible()


def test_project_actions_and_relationship_mutations(page: Page, agents_base_url):
    state, seen = _state(), []
    _open(page, agents_base_url, state, seen)
    page.locator('[data-card-id="project-1"]').click()
    page.locator('[data-action="project-start"]').click()
    page.locator('[data-action="project-plan"]').click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-action="project-complete"]').click()
    page.locator('[data-action="project-cancel"]').click()
    expect(page.locator('.modal')).to_contain_text("3 unfinished children")
    page.locator('.modal [data-action="confirm"]').click()
    page.locator('[data-action="project-add-child"]').click()
    page.locator('.modal [data-field="project-prompt"]').fill("New synthetic child")
    page.locator('.modal [data-action="confirm"]').click()
    page.locator('[data-action="project-attach-child"]').click()
    page.locator('.modal [data-field="project-prompt"]').fill("existing-child")
    page.locator('.modal [data-action="confirm"]').click()
    page.locator('[data-child-id="child-1"] [data-action="detach-child"]').click()
    page.locator('[data-child-id="child-1"] [data-action="move-child"]').click()
    page.locator('.modal [data-field="project-prompt"]').fill("next-project")
    page.locator('.modal [data-action="confirm"]').click()

    page.wait_for_timeout(100)
    calls = {(method, url.split("/api", 1)[-1]): body for method, url, body in seen}
    assert calls[("POST", "/tasks/project-1/project/start")] is None
    assert calls[("POST", "/tasks/project-1/project/plan")]["operation_id"]
    assert calls[("POST", "/tasks/project-1/project/complete")] == {"acknowledge_cancelled_children": True}
    assert calls[("POST", "/tasks/project-1/project/cancel")]["confirm"] is True
    assert calls[("POST", "/tasks")]["fields"] == {"parent_id": "project-1"}
    assert calls[("PUT", "/tasks/existing-child")] == {"fields": {"parent_id": "project-1"}}
    assert calls[("PUT", "/tasks/child-1")] == {"fields": {"parent_id": "next-project"}}


def test_pending_project_cancellation_reuses_preview_operation_id(page: Page, agents_base_url):
    state, seen = _state(), []
    state["lanes"]["assigned"][0]["project"]["cancellation_pending"] = True
    retry_operation_id = "cancel-synthetic-retry"

    def api(route):
        request = route.request
        url, method = request.url, request.method
        if url.rstrip("/").endswith("/api/tasks/project-1/project/cancel") and method == "POST":
            body = request.post_data_json
            seen.append(body)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"project_id": "project-1", "operation_id": retry_operation_id,
                 "cancellation_pending": not body["confirm"], "unfinished_count": 1,
                 "running_count": 0, "awaiting_review_count": 0, "children": [],
                 "confirmation_required": True, "complete": body["confirm"]}
            ))
        elif "/api/agents/board/stream" in url:
            route.fulfill(status=200, content_type="text/event-stream", body="retry: 60000\n: ok\n\n")
        elif url.rstrip("/").endswith("/api/agents/board") and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(state))
        elif "/api/tasks/project-1/children" in url and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"tasks": [], "total": 0, "limit": 50, "offset": 0}
            ))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    page.route("**/d3.v7.min.js", lambda route: route.fulfill(
        status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};"
    ))
    page.route("**/api/**", api)
    page.goto(f"{agents_base_url}/agents")
    page.wait_for_selector('[data-card-id="project-1"]')
    page.locator('[data-card-id="project-1"]').click()
    page.locator('[data-action="project-cancel"]').click()
    page.locator('.modal [data-action="confirm"]').click()
    expect(page.locator('.modal')).to_have_count(0)

    assert seen == [
        {"confirm": False, "operation_id": None},
        {"confirm": True, "operation_id": retry_operation_id},
    ]
