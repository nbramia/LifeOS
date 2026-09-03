"""Browser test for the /agents Kanban board (#850).

Serves `web/` itself from an ephemeral port (like
`test_voice_mic_block_ui_browser.py`) rather than pointing at a running API,
and stubs every `/api/` call the page makes — the assertions are about the
JS in `web/agents/board.js` and `web/agents.html`, not the live backend.
No `requires_server` marker, so this runs at pre-push
(`browser and not requires_server`).

Covers: drag between lanes (asserts the stubbed PUT lane body; asserts the
card does NOT move and a toast shows when the stub returns 500), drawer
notes edit + blur (asserts the stubbed PUT /api/tasks body carries `notes`),
and filters including assignee=me and lane=human_queue combined.
"""
import http.server
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _AgentsHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the agents board the way api/main.py does: `/agents` is
    agents.html and the module tree hangs off `/static/`."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/agents", "/"):
            return str(WEB_DIR / "agents.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
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


# Obviously synthetic board fixture. Lane derivation itself is unit-tested in
# tests/test_agent_board.py; this fixture only needs shapes the UI renders.
def _board_fixture():
    return {
        "lanes": {
            "unassigned": [
                {
                    "kind": "task", "id": "t1", "title": "Investigate outage",
                    "notes": "", "status": "todo", "tags": [], "assignee": None,
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "assigned": [
                {
                    "kind": "task", "id": "t2", "title": "Ship the release",
                    "notes": "Draft notes", "status": "todo", "tags": ["me"], "assignee": "me",
                    "fields": {}, "context": "Work", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "in_progress": [],
            "human_queue": [
                {
                    "kind": "task", "id": "t3", "title": "Debug prod issue",
                    "notes": "", "status": "blocked", "tags": ["agent-blocked", "codex"], "assignee": "codex",
                    "fields": {}, "context": "Ops", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None,
                    "pending_question": {"id": 1, "session_id": "s1", "question": "Which environment?", "asked_at": 0, "bot": None},
                },
                {
                    "kind": "task", "id": "t4", "title": "Ask Nathan about timeline",
                    "notes": "", "status": "blocked", "tags": ["human", "me"], "assignee": "me",
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "scheduled": [],
            "review": [],
            "done": [],
        },
        "generated_at": 0,
    }


def _move_card_in_state(board_state: dict, card_id: str, target_lane: str) -> None:
    """Mutate the stub's in-memory board the way a real PUT .../lane would,
    so a successful move's follow-up GET /api/agents/board reflects it —
    otherwise the client's re-fetch-after-success would silently snap the
    card back to its original lane against a static fixture."""
    for lane_id, cards in board_state["lanes"].items():
        for card in list(cards):
            if card["id"] == card_id:
                cards.remove(card)
                board_state["lanes"].setdefault(target_lane, []).append(card)
                return


def _stub_routes(page: Page, board_state: dict, lane_calls: list, task_puts: list, lane_status_code: list):
    """Stub d3 (offline CDN) + every /api/ call the page makes."""

    def d3_handler(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")

    page.route("**/d3.v7.min.js", d3_handler)

    def api_handler(route):
        url = route.request.url
        method = route.request.method

        if "/api/agents/board/stream" in url:
            route.fulfill(status=200, content_type="text/event-stream", body=": ok\n\n")
            return

        lane_match = re.search(r"/api/agents/board/cards/([^/]+)/lane$", url)
        if lane_match and method == "PUT":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            lane_calls.append(body)
            code = lane_status_code[0]
            if code == 200:
                target_lane = body.get("lane")
                _move_card_in_state(board_state, lane_match.group(1), target_lane)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": lane_match.group(1), "lane": target_lane}))
            else:
                route.fulfill(status=code, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return

        if re.search(r"/api/tasks/[^/]+$", url) and method == "PUT":
            try:
                task_puts.append(json.loads(route.request.post_data or "{}"))
            except ValueError:
                task_puts.append({})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": "t2"}))
            return

        if re.search(r"/api/agents/board$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return

        # Everything else the page might touch (pending-questions answer,
        # session focus/kill, task creation) — a harmless empty JSON body.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _open_board(page: Page, base_url, board_state=None, lane_calls=None, task_puts=None, lane_status_code=None):
    _stub_routes(
        page,
        board_state if board_state is not None else _board_fixture(),
        lane_calls if lane_calls is not None else [],
        task_puts if task_puts is not None else [],
        lane_status_code if lane_status_code is not None else [200],
    )
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="t1"]')


def _drag_card(page: Page, card_id: str, target_lane: str):
    card = page.locator(f'[data-card-id="{card_id}"]')
    card_box = card.bounding_box()
    lane_cards = page.locator(f'.board-lane[data-lane="{target_lane}"] .board-lane-cards')
    lane_box = lane_cards.bounding_box()
    page.mouse.move(card_box["x"] + card_box["width"] / 2, card_box["y"] + card_box["height"] / 2)
    page.mouse.down()
    page.mouse.move(lane_box["x"] + lane_box["width"] / 2, lane_box["y"] + 15, steps=10)
    page.mouse.move(lane_box["x"] + lane_box["width"] / 2, lane_box["y"] + 15, steps=1)
    page.mouse.up()


class TestBoardLoad:
    def test_cards_render_in_their_derived_lanes(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_be_visible()
        expect(page.locator('.board-lane[data-lane="assigned"] [data-card-id="t2"]')).to_be_visible()
        expect(page.locator('.board-lane[data-lane="human_queue"] [data-card-id="t3"]')).to_be_visible()
        expect(page.locator('.board-lane[data-lane="human_queue"] [data-card-id="t4"]')).to_be_visible()

    def test_pending_question_shown_on_card(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        card = page.locator('[data-card-id="t3"]')
        expect(card.locator(".board-card-question")).to_contain_text("Which environment?")


class TestDragBetweenLanes:
    def test_drag_issues_lane_put_with_expected_body(self, page: Page, agents_base_url):
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[200])
        _drag_card(page, "t1", "in_progress")
        # A successful move re-fetches the board; wait for the card to land
        # in its new lane rather than asserting on a fixed delay.
        expect(page.locator('.board-lane[data-lane="in_progress"] [data-card-id="t1"]')).to_be_visible(timeout=5000)
        assert lane_calls == [{"lane": "in_progress"}]

    def test_failed_move_does_not_move_card_and_shows_toast(self, page: Page, agents_base_url):
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[500])
        _drag_card(page, "t1", "in_progress")
        expect(page.locator(".toast.error")).to_be_visible(timeout=5000)
        # Card is still where it started — the failed PUT never re-fetched
        # the board, so nothing moved.
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_be_visible()
        expect(page.locator('.board-lane[data-lane="in_progress"] [data-card-id="t1"]')).to_have_count(0)
        assert lane_calls == [{"lane": "in_progress"}]

    def test_dropping_on_assigned_lane_defaults_assignee_to_me(self, page: Page, agents_base_url):
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[200])
        _drag_card(page, "t1", "assigned")
        expect(page.locator('.board-lane[data-lane="assigned"] [data-card-id="t1"]')).to_be_visible(timeout=5000)
        assert lane_calls == [{"lane": "assigned", "assignee": "me"}]


class TestDrawerNotesEdit:
    def test_editing_notes_and_blurring_saves(self, page: Page, agents_base_url):
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t2"]').click()
        notes = page.locator(".drawer-notes")
        expect(notes).to_be_visible()
        notes.fill("Updated notes from the drawer")
        page.locator(".drawer-title").click()  # blur the notes field
        expect(page.locator(".drawer-notes")).to_have_value("Updated notes from the drawer")
        assert any(p.get("notes") == "Updated notes from the drawer" for p in task_puts), task_puts


class TestFilters:
    def test_assignee_me_shows_only_me_cards(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator("#board-filter-assignee").select_option("me")
        expect(page.locator('[data-card-id="t2"]')).to_be_visible()
        expect(page.locator('[data-card-id="t4"]')).to_be_visible()
        expect(page.locator('[data-card-id="t1"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t3"]')).to_have_count(0)

    def test_lane_human_queue_shows_both_agent_and_human_cards(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator("#board-filter-lane").select_option("human_queue")
        expect(page.locator('[data-card-id="t3"]')).to_be_visible()
        expect(page.locator('[data-card-id="t4"]')).to_be_visible()
        expect(page.locator('[data-card-id="t1"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t2"]')).to_have_count(0)

    def test_lane_human_queue_and_assignee_me_shows_only_human_card(self, page: Page, agents_base_url):
        """AC: 'Filtering by lane Human queue and assignee me together shows
        only #human cards.' t3 is agent-blocked/assignee=codex; t4 is the
        #human card, tagged #me — only t4 should remain."""
        _open_board(page, agents_base_url)
        page.locator("#board-filter-lane").select_option("human_queue")
        page.locator("#board-filter-assignee").select_option("me")
        expect(page.locator('[data-card-id="t4"]')).to_be_visible()
        expect(page.locator('[data-card-id="t3"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t1"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t2"]')).to_have_count(0)

    def test_search_filters_by_title(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator("#board-search").fill("outage")
        expect(page.locator('[data-card-id="t1"]')).to_be_visible()
        expect(page.locator('[data-card-id="t2"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t3"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t4"]')).to_have_count(0)

    def test_tag_filter(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator("#board-filter-tag").fill("codex")
        expect(page.locator('[data-card-id="t3"]')).to_be_visible()
        expect(page.locator('[data-card-id="t1"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t2"]')).to_have_count(0)
        expect(page.locator('[data-card-id="t4"]')).to_have_count(0)

    def test_context_filter(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator("#board-filter-context").select_option("Work")
        expect(page.locator('[data-card-id="t2"]')).to_be_visible()
        expect(page.locator('[data-card-id="t1"]')).to_have_count(0)
