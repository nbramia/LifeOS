"""Browser tests for the /agents board's multi-select and bulk action bar.

Follows tests/test_agents_board_ui_browser.py's stub pattern: serves `web/`
from an ephemeral port and stubs every `/api/` call the page makes, so the
assertions are about the JS in `web/agents/board.js` and
`web/agents/card_actions.js`, not a live backend. No `requires_server`
marker, so this runs at pre-push (`browser and not requires_server`).

Covers: modifier-click select/deselect without opening the drawer, the bulk
bar appearing/hiding (and swapping with the assignee tray), a plain click
clearing the selection, Escape and the clear control, each of the four bulk
actions (Assign, Tag, Mark Done, Delete) including a mixed batch where the
server refuses one card, protected-tag preservation, a claimed card's
kill-before-delete path, selection surviving a live re-render and pruning a
removed card, a modifier-held pointer press never starting a drag, and a
modifier click winning over an armed assignee-tray selection.
"""
import http.server
import json
import re
import threading
import time
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


_ASSIGNEE_TAGS = {"me", "claude", "codex", "hermes", "local", "cloud"}
_PROTECTED_TAGS = _ASSIGNEE_TAGS | {
    "cloud-haiku", "cloud-sonnet", "agent-running", "agent-blocked",
    "agent-completed", "agent-failed", "agent-budget-exceeded",
    "agent-reassigned", "accepted",
}


# Obviously synthetic board fixture, tailored to this file's scenarios:
#   t1 — plain unassigned card, selectable.
#   t2 — plain "me"-assigned card, selectable.
#   t3 — claimed, agent-owned, WITH a live killable session (in_progress,
#        #codex + #agent-running, plus an ordinary editable #followup tag) —
#        the claimed-delete-kill and protected-tag-preservation target, and
#        the assign-refusal target. The #followup tag exists so a bulk Tag
#        write's REQUEST BODY can be asserted to still carry it — the
#        stub's tags-merge route reconstructs protected tags from the
#        card's own prior state regardless of what the client actually
#        sends, so only a client-sent editable tag proves the client itself
#        preserved it.
#   t4 — a Review card (#agent-completed) — Mark Done's Accept-transition
#        target.
#   t5 — a plain Assigned card, not Done — Mark Done's plain lane-move
#        target.
#   t6 — already Done — Mark Done's no-op target.
#   t9 — a plain unassigned card removed mid-test to prove pruning.
#   s1 — a schedule card — never selectable.
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
                {
                    "kind": "task", "id": "t9", "title": "Triage the backlog",
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
                {
                    "kind": "task", "id": "t5", "title": "Rotate the credentials",
                    "notes": "", "status": "todo", "tags": ["local"], "assignee": "local",
                    "fields": {}, "context": "Ops", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "in_progress": [
                {
                    "kind": "task", "id": "t3", "title": "Migrate the database",
                    "notes": "", "status": "in_progress", "tags": ["codex", "agent-running", "followup"],
                    "assignee": "codex",
                    "fields": {}, "context": "Ops", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": {"session_id": "s3", "status": "running", "source": "local", "host": "worker-box"},
                    "pending_question": None,
                },
            ],
            "human_queue": [],
            "scheduled": [
                {
                    "kind": "schedule", "id": "s1", "name": "Morning briefing",
                    "message_content": "Good morning", "enabled": True,
                    "next_fire_at": "2099-01-01T09:00:00+00:00", "recurring": True,
                    "last_run": None,
                },
            ],
            "review": [
                {
                    "kind": "task", "id": "t4", "title": "Write the release notes",
                    "notes": "", "status": "done", "tags": ["hermes", "agent-completed"],
                    "assignee": "hermes",
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "done": [
                {
                    "kind": "task", "id": "t6", "title": "Archive the old runbook",
                    "notes": "", "status": "done", "tags": [], "assignee": None,
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
        },
        "generated_at": 0,
        "api_host": "primary-host",
    }


def _find_card(board_state, card_id):
    for cards in board_state["lanes"].values():
        for card in cards:
            if card["id"] == card_id:
                return card
    return None


def _move_card_in_state(board_state, card_id, target_lane):
    for lane_id, cards in board_state["lanes"].items():
        for card in list(cards):
            if card["id"] == card_id:
                cards.remove(card)
                board_state["lanes"].setdefault(target_lane, []).append(card)
                return


def _remove_card_from_state(board_state, card_id):
    for cards in board_state["lanes"].values():
        for card in list(cards):
            if card["id"] == card_id:
                cards.remove(card)
                return


def _stub_routes(page: Page, board_state, calls, refuse_ids=None, refuse_detail="card is claimed",
                  board_stream_frames=None, stream_gate=None):
    """Stub d3 (offline CDN) plus every `/api/` call the multi-select bar can
    make. `calls` is a dict of lists this appends every observed request to,
    keyed `lane_puts`, `task_puts`, `tag_puts`, `accept_calls`, `kill_calls`,
    `task_deletes`, `call_log` (ordered `(kind, id)` tuples, for asserting a
    kill happened before its delete). `refuse_ids`, when given, makes ANY
    card-mutating endpoint (tags PUT, task PUT, lane PUT, accept POST) 409 for
    that card id instead of applying the write — the mixed-batch policy
    refusal a bulk action must report per card while the rest still apply.
    `board_stream_frames`/`stream_gate` mirror
    tests/test_agents_board_ui_browser.py's live-update gating: every
    `/board/stream` connection gets an empty keep-alive while
    `stream_gate` is unset, and the first connection after it's set
    delivers `board_stream_frames[0]`.
    """
    refuse_ids = refuse_ids or set()

    def d3_handler(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")

    page.route("**/d3.v7.min.js", d3_handler)

    stream_attempt = [0]

    def refused(route):
        route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": refuse_detail}))

    def api_handler(route):
        url = route.request.url
        method = route.request.method

        if "/api/agents/board/stream" in url:
            if stream_gate is not None and not stream_gate.is_set():
                route.fulfill(status=200, content_type="text/event-stream", body="retry: 30\n: ok\n\n")
                return
            frames = board_stream_frames or []
            frame_idx = stream_attempt[0] - 1
            frame = frames[frame_idx] if 0 <= frame_idx < len(frames) else ""
            stream_attempt[0] += 1
            route.fulfill(status=200, content_type="text/event-stream", body=f"retry: 20\n: ok\n\n{frame}")
            return

        if re.search(r"/api/agents/models$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json",
                           body=json.dumps({"engines": {"claude": [], "codex": [], "local": [], "hermes": []}}))
            return
        if re.search(r"/api/agents/hosts$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"hosts": []}))
            return

        accept_match = re.search(r"/api/agents/board/cards/([^/]+)/accept$", url)
        if accept_match and method == "POST":
            card_id = accept_match.group(1)
            calls["accept_calls"].append(card_id)
            if card_id in refuse_ids:
                refused(route)
                return
            _move_card_in_state(board_state, card_id, "done")
            card = _find_card(board_state, card_id)
            card["status"] = "done"
            card["tags"] = [t for t in card.get("tags", []) if t != "agent-completed"] + ["accepted"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": card_id, "lane": "done"}))
            return

        lane_match = re.search(r"/api/agents/board/cards/([^/]+)/lane$", url)
        if lane_match and method == "PUT":
            card_id = lane_match.group(1)
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            calls["lane_puts"].append(body)
            if card_id in refuse_ids:
                refused(route)
                return
            _move_card_in_state(board_state, card_id, body.get("lane"))
            route.fulfill(status=200, content_type="application/json",
                           body=json.dumps({"id": card_id, "lane": body.get("lane")}))
            return

        tag_match = re.search(r"/api/agents/board/cards/([^/]+)/tags$", url)
        if tag_match and method == "PUT":
            card_id = tag_match.group(1)
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            calls["tag_puts"].append(body)
            if card_id in refuse_ids:
                refused(route)
                return
            card = _find_card(board_state, card_id)
            if card is None:
                route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "card not found"}))
                return
            requested = body.get("tags") or []
            preserved = [t for t in card.get("tags", []) if str(t).lstrip("#").lower() in _PROTECTED_TAGS]
            card["tags"] = [*preserved, *requested]
            route.fulfill(status=200, content_type="application/json",
                           body=json.dumps({"id": card_id, "lane": card_id, "status": card["status"], "tags": card["tags"]}))
            return

        task_match = re.search(r"/api/tasks/([^/]+)$", url)
        if task_match and method == "PUT":
            card_id = task_match.group(1)
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            calls["task_puts"].append({"_card_id": card_id, **body})
            if card_id in refuse_ids:
                refused(route)
                return
            card = _find_card(board_state, card_id)
            if card is not None:
                if "tags" in body:
                    card["tags"] = body["tags"]
                    assignee_tags = [t for t in body["tags"] if str(t).lower() in _ASSIGNEE_TAGS]
                    card["assignee"] = assignee_tags[0] if assignee_tags else None
                if "notes" in body:
                    card["notes"] = body["notes"]
                if "description" in body:
                    card["title"] = body["description"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": card_id}))
            return

        if task_match and method == "DELETE":
            card_id = task_match.group(1)
            calls["task_deletes"].append(card_id)
            calls["call_log"].append(("task_delete", card_id))
            _remove_card_from_state(board_state, card_id)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "deleted", "id": card_id}))
            return

        kill_match = re.search(r"/api/agents/sessions/([^/]+)/kill$", url)
        if kill_match and method == "POST":
            session_id = kill_match.group(1)
            calls["kill_calls"].append(session_id)
            calls["call_log"].append(("kill", session_id))
            route.fulfill(status=200, content_type="application/json",
                           body=json.dumps({"killed": [session_id], "failures": []}))
            return

        if re.search(r"/api/agents/board$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return

        # Everything else the page might touch — a harmless empty JSON body.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _new_calls():
    return {
        "lane_puts": [], "task_puts": [], "tag_puts": [], "accept_calls": [],
        "kill_calls": [], "task_deletes": [], "call_log": [],
    }


def _open_board(page: Page, base_url, board_state=None, calls=None, **kwargs):
    board_state = board_state if board_state is not None else _board_fixture()
    calls = calls if calls is not None else _new_calls()
    _stub_routes(page, board_state, calls, **kwargs)
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="t1"]')
    return board_state, calls


def _wait_for(predicate, page: Page, timeout_ms=5000, interval_ms=25):
    """Poll `predicate` until truthy or the timeout elapses, pumping
    Playwright's event loop via `page.wait_for_timeout` between checks (a
    plain `time.sleep` never delivers an already-arrived route callback —
    see tests/test_agents_board_ui_browser.py's identical helper)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(interval_ms)
    assert predicate(), f"condition not met within {timeout_ms}ms"


def _card(page, card_id):
    return page.locator(f'[data-card-id="{card_id}"]')


class TestModifierSelect:
    def test_modifier_click_selects_without_opening_drawer(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        expect(_card(page, "t1")).to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(page.locator("#board-drawer-backdrop")).to_be_hidden()
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")

    def test_second_modifier_click_adds_and_reclicking_deselects(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t2").click(modifiers=["Control"])
        expect(_card(page, "t1")).to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(_card(page, "t2")).to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(page.locator("#board-bulk-count")).to_have_text("2 selected")
        _card(page, "t1").click(modifiers=["Control"])
        expect(_card(page, "t1")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")


class TestBulkBarVisibility:
    def test_bar_shows_with_selection_and_hides_the_assignee_tray(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(page.locator("#board-drop-tray")).to_be_visible()
        _card(page, "t1").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-bar")).to_be_visible()
        expect(page.locator("#board-drop-tray")).to_be_hidden()

    def test_bar_hides_again_once_selection_empties(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t1").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(page.locator("#board-drop-tray")).to_be_visible()


class TestClearingSelection:
    def test_plain_click_clears_selection_and_opens_drawer(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t2").click()
        expect(_card(page, "t1")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(page.locator("#board-drawer-backdrop")).to_be_visible()
        expect(page.locator(".drawer-title")).to_have_value("Ship the release")

    def test_escape_clears_selection_when_no_drawer_or_modal_open(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-bar")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(_card(page, "t1")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))

    def test_clear_control_empties_selection(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t2").click(modifiers=["Control"])
        page.locator("#board-bulk-clear").click()
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(_card(page, "t1")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(_card(page, "t2")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))

    def test_keyboard_activation_clears_selection_and_opens_drawer(self, page: Page, agents_base_url):
        """Enter/Space on a focused card mirrors the plain-click branch: it
        clears any active selection before opening that card's drawer,
        rather than leaving a stale selection (and bulk bar) underneath the
        newly opened drawer."""
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        expect(_card(page, "t1")).to_have_class(re.compile(r"\bboard-card-selected\b"))
        _card(page, "t2").focus()
        page.keyboard.press("Enter")
        expect(_card(page, "t1")).not_to_have_class(re.compile(r"\bboard-card-selected\b"))
        expect(page.locator("#board-bulk-bar")).to_be_hidden()
        expect(page.locator("#board-drawer-backdrop")).to_be_visible()
        expect(page.locator(".drawer-title")).to_have_value("Ship the release")

    def test_escape_closes_drawer_with_title_textarea_focused(self, page: Page, agents_base_url):
        """Escape's existing drawer-close precedence must still win even
        when the drawer's own title field holds focus — the title
        textarea's Enter-to-save keydown handler only intercepts Enter, so
        Escape still bubbles to the document-level handler that closes the
        drawer, rather than falling through to (a no-op) clear-selection."""
        _open_board(page, agents_base_url)
        _card(page, "t2").click()
        expect(page.locator("#board-drawer-backdrop")).to_be_visible()
        page.locator(".drawer-title").click()
        page.keyboard.press("Escape")
        expect(page.locator("#board-drawer-backdrop")).to_be_hidden()


class TestBulkAssign:
    def test_assign_applies_to_every_selected_card_via_task_put(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t2").click(modifiers=["Control"])
        page.locator("#board-bulk-assign").click()
        page.locator("#board-bulk-assign-popover [data-assignee='hermes']").click()
        _wait_for(lambda: len(calls["task_puts"]) == 2, page)
        expect(page.locator(".toast")).to_have_text("Assigned 2 of 2.")
        # t2 already carried tag "me" — the write must drop it, not just add
        # "hermes" alongside it (assignment is exclusive, one tag at a time).
        t2_put = next(b for b in calls["task_puts"] if b["_card_id"] == "t2")
        assert "hermes" in t2_put["tags"] and "me" not in t2_put["tags"]

    def test_mixed_batch_reports_refusal_and_still_applies_the_rest(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url, refuse_ids={"t3"}, refuse_detail="card is claimed")
        _card(page, "t2").click(modifiers=["Control"])
        _card(page, "t3").click(modifiers=["Control"])
        page.locator("#board-bulk-assign").click()
        page.locator("#board-bulk-assign-popover [data-assignee='hermes']").click()
        _wait_for(lambda: len(calls["task_puts"]) == 2, page)
        toast = page.locator(".toast.error")
        expect(toast).to_be_visible()
        text = toast.inner_text()
        assert "Assigned 1 of 2" in text
        assert "Migrate the database" in text and "card is claimed" in text
        # The refused card's assignee must be untouched.
        assert _find_card(board_state, "t3")["assignee"] == "codex"
        assert _find_card(board_state, "t2")["assignee"] == "hermes"


class TestBulkTag:
    def test_tag_preserves_protected_tags_on_a_claimed_card(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url)
        _card(page, "t3").click(modifiers=["Control"])
        page.locator("#board-bulk-tag").click()
        page.locator("#board-bulk-tag-popover [data-field='tag-search']").fill("urgent")
        page.locator("#board-bulk-tag-popover .board-bulk-popover-create").click()
        _wait_for(lambda: len(calls["tag_puts"]) == 1, page)
        expect(page.locator(".toast")).to_have_text("Tagged 1 of 1.")
        # Assert the CLIENT'S REQUEST body, not just the resulting card
        # state — the stub's tags-merge route reconstructs protected tags
        # from the card's own prior state regardless of what the client
        # actually sent, so a client that dropped "followup" (t3's ordinary
        # editable tag) and sent only the new tag would still leave the
        # card carrying its protected tags, passing an assertion against
        # `board_state` alone without ever proving the client preserved its
        # own editable tags.
        sent_tags = calls["tag_puts"][0]["tags"]
        assert "followup" in sent_tags
        assert "urgent" in sent_tags
        # The request must never carry a protected tag either — the drawer's
        # own tag picker sends only the editable set and lets the server
        # merge protected tags back in.
        assert "codex" not in sent_tags and "agent-running" not in sent_tags
        tags = _find_card(board_state, "t3")["tags"]
        assert "urgent" in tags and "followup" in tags
        assert "agent-running" in tags and "codex" in tags


class TestBulkMarkDone:
    def test_review_card_accepts_and_others_move_through_lane_endpoint(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url)
        # Done is hidden by the board's default lane filter — reveal it so
        # t6 (already Done) is selectable at all.
        page.locator("#board-lane-filter-btn").click()
        page.locator("#board-lane-filter-options input[value='done']").check()
        _card(page, "t4").click(modifiers=["Control"])  # review
        _card(page, "t5").click(modifiers=["Control"])  # plain assigned
        _card(page, "t6").click(modifiers=["Control"])  # already done
        page.locator("#board-bulk-done").click()
        _wait_for(lambda: len(calls["accept_calls"]) == 1 and len(calls["lane_puts"]) == 1, page)
        expect(page.locator(".toast")).to_have_text("Marked done 3 of 3.")
        assert calls["accept_calls"] == ["t4"]
        assert calls["lane_puts"][0] == {"lane": "done"}
        assert _find_card(board_state, "t4")["status"] == "done"
        assert "accepted" in _find_card(board_state, "t4")["tags"]


class TestBulkDelete:
    def test_single_confirmation_and_kill_before_delete_for_a_claimed_card(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t3").click(modifiers=["Control"])  # has a live, killable session
        page.locator("#board-bulk-delete").click()
        modal = page.locator(".modal-backdrop")
        expect(modal).to_have_count(1)
        expect(modal.locator("h2")).to_have_text("Delete 2 cards?")
        modal.locator("#bulk-delete-confirm").click()
        _wait_for(lambda: len(calls["task_deletes"]) == 2, page)
        expect(page.locator(".modal-backdrop")).to_have_count(0)
        expect(page.locator(".toast")).to_have_text("Deleted 2 of 2.")
        assert calls["kill_calls"] == ["s3"]
        # The claimed card's session must be killed before its own delete.
        kill_index = calls["call_log"].index(("kill", "s3"))
        delete_index = calls["call_log"].index(("task_delete", "t3"))
        assert kill_index < delete_index
        assert _find_card(board_state, "t1") is None
        assert _find_card(board_state, "t3") is None


class TestSelectionAcrossRerender:
    def test_selection_survives_rerender_and_prunes_a_removed_card(self, page: Page, agents_base_url):
        gate = threading.Event()
        board_state, calls = _open_board(page, agents_base_url, stream_gate=gate, board_stream_frames=[])
        _card(page, "t1").click(modifiers=["Control"])
        _card(page, "t9").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-count")).to_have_text("2 selected")

        _remove_card_from_state(board_state, "t9")
        frame = f"event: board\ndata: {json.dumps(board_state)}\n\n"
        # Re-register with the removal frame now available — Playwright runs
        # the most-recently-registered matching route handler first, and
        # this handler always fulfills, so it fully supersedes the one
        # `_open_board` installed (mirrors the original board-UI test
        # file's own two-phase gate pattern).
        _stub_routes(page, board_state, calls, board_stream_frames=[frame], stream_gate=gate)
        gate.set()

        _wait_for(lambda: _card(page, "t9").count() == 0, page, timeout_ms=8000)
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")
        expect(_card(page, "t1")).to_have_class(re.compile(r"\bboard-card-selected\b"))


class TestBulkActionUsesLiveCardState:
    def test_bulk_tag_request_reflects_a_tag_added_via_sse_after_selection(self, page: Page, agents_base_url):
        """selectedTaskCards() reads `allCards()` — the live `board` state —
        fresh every time a bulk action runs, rather than a card snapshot
        captured at selection time. Proven by adding a tag to a selected
        card through a live SSE update after it's selected, then confirming
        the bulk Tag write's request body includes that SSE-added tag
        alongside the new one — a stale click-time snapshot would only ever
        send the new tag."""
        gate = threading.Event()
        board_state, calls = _open_board(page, agents_base_url, stream_gate=gate, board_stream_frames=[])
        _card(page, "t1").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")

        card = _find_card(board_state, "t1")
        card["tags"] = ["sse-added"]
        frame = f"event: board\ndata: {json.dumps(board_state)}\n\n"
        _stub_routes(page, board_state, calls, board_stream_frames=[frame], stream_gate=gate)
        gate.set()
        _wait_for(lambda: page.locator('[data-card-id="t1"] .board-chip-tag').count() > 0, page, timeout_ms=8000)
        expect(_card(page, "t1")).to_have_class(re.compile(r"\bboard-card-selected\b"))

        page.locator("#board-bulk-tag").click()
        page.locator("#board-bulk-tag-popover [data-field='tag-search']").fill("livecheck")
        page.locator("#board-bulk-tag-popover .board-bulk-popover-create").click()
        _wait_for(lambda: len(calls["tag_puts"]) == 1, page)
        sent_tags = calls["tag_puts"][0]["tags"]
        assert "sse-added" in sent_tags
        assert "livecheck" in sent_tags


class TestBulkActionInFlightGuard:
    def test_double_click_mark_done_fires_one_write_per_card(self, page: Page, agents_base_url):
        """Two `click` events dispatched back-to-back on Mark Done — the
        race a real double-click (or two clicks while a slow/loaded server
        holds the first fan-out's requests open) opens — must only ever
        start one fan-out. `dispatchEvent` (unlike Playwright's own
        `.click()`, which requires the element to be enabled) still invokes
        `addEventListener` callbacks on a disabled button, so this reaches
        the handler's own in-flight check rather than being blocked purely
        by the `disabled` attribute — proving the guard itself, not just
        the button's disabled affordance."""
        board_state, calls = _open_board(page, agents_base_url)
        _card(page, "t5").click(modifiers=["Control"])  # plain Assigned card, not Review/Done
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")
        page.evaluate("""
            () => {
                const btn = document.getElementById('board-bulk-done');
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            }
        """)
        _wait_for(lambda: len(calls["lane_puts"]) >= 1, page)
        # Give a wrongly-started second fan-out time to also land before
        # asserting there's only one.
        page.wait_for_timeout(200)
        assert len(calls["lane_puts"]) == 1

    def test_double_click_delete_opens_exactly_one_modal(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        _card(page, "t1").click(modifiers=["Control"])
        expect(page.locator("#board-bulk-count")).to_have_text("1 selected")
        page.evaluate("""
            () => {
                const btn = document.getElementById('board-bulk-delete');
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            }
        """)
        expect(page.locator(".modal-backdrop")).to_have_count(1)


class TestModifierPointerdownNoDrag:
    def test_modifier_held_press_never_starts_a_drag(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        card = _card(page, "t1")
        box = card.bounding_box()
        target_lane = page.locator('.board-lane[data-lane="assigned"] .board-lane-cards')
        target_box = target_lane.bounding_box()
        page.keyboard.down("Control")
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(target_box["x"] + target_box["width"] / 2, target_box["y"] + 15, steps=10)
        # A real drag would show a ghost element and the target lane's
        # drag-over highlight; neither should ever appear for a
        # modifier-held press.
        assert page.locator(".board-card-ghost").count() == 0
        assert not page.locator('.board-lane[data-lane="assigned"]').evaluate(
            "el => el.classList.contains('drag-over')"
        )
        # Native click synthesis only fires when mouseup lands back over the
        # same element mousedown started on — return there before releasing,
        # so the trailing click actually reaches the card (a real drag would
        # never need this, since it never depends on a native click at all).
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=5)
        page.mouse.up()
        page.keyboard.up("Control")
        # The card stayed in Unassigned — no lane PUT was ever issued — and
        # the trailing click instead toggled the selection.
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_have_count(1)
        expect(card).to_have_class(re.compile(r"\bboard-card-selected\b"))


class TestScheduledCardNotSelectable:
    def test_modifier_click_on_schedule_card_opens_drawer_as_usual(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        # Scheduled cards live in a lane hidden by default — reveal it.
        page.locator("#board-lane-filter-btn").click()
        page.locator("#board-lane-filter-options input[value='scheduled']").check()
        schedule_card = _card(page, "s1")
        expect(schedule_card).to_have_count(1)
        schedule_card.click(modifiers=["Control"])
        expect(page.locator("#board-drawer-backdrop")).to_be_visible()
        expect(page.locator("#board-bulk-bar")).to_be_hidden()


class TestModifierClickOverridesArmedTray:
    def test_modifier_click_selects_instead_of_assigning_with_tray_filter_active(self, page: Page, agents_base_url):
        board_state, calls = _open_board(page, agents_base_url)
        # Clicking an assignee-tray control sets the shared assignee filter
        # (its "selected"/pressed visual state) — the closest thing to an
        # "armed" tray control this board has. A modifier click on a card
        # afterward must select it, not assign it, and must leave the tray
        # filter alone.
        page.locator(".board-assignee-drop[data-assignee='me']").click()
        expect(page.locator(".board-assignee-drop[data-assignee='me']")).to_have_class(re.compile(r"\bselected\b"))
        # t2 already carries assignee "me", so it stays visible under this
        # filter (t1, unassigned, would be filtered out of the DOM entirely).
        _card(page, "t2").click(modifiers=["Control"])
        expect(_card(page, "t2")).to_have_class(re.compile(r"\bboard-card-selected\b"))
        assert calls["task_puts"] == []
        assert calls["lane_puts"] == []
        expect(page.locator(".board-assignee-drop[data-assignee='me']")).to_have_class(re.compile(r"\bselected\b"))
