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
import copy
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
                    "kind": "task", "id": "t4", "title": "Ask the operator about timeline",
                    "notes": "", "status": "blocked", "tags": ["human", "me"], "assignee": "me",
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
            "scheduled": [
                {
                    "kind": "schedule", "id": "s1", "name": "Morning briefing",
                    "message_content": "Good morning", "enabled": True,
                    "next_fire_at": "2099-01-01T09:00:00+00:00", "recurring": True,
                    "last_run": None,
                },
            ],
            "review": [],
            "done": [
                {
                    "kind": "task", "id": "t5", "title": "Archive the old runbook",
                    "notes": "", "status": "done", "tags": [], "assignee": None,
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
                {
                    "kind": "task", "id": "t6", "title": "Cancelled duplicate ticket",
                    "notes": "", "status": "cancelled", "tags": [], "assignee": None,
                    "fields": {}, "context": "Inbox", "updated_at": "2026-01-01T00:00:00+00:00",
                    "session": None, "pending_question": None,
                },
            ],
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


def _stub_routes(page: Page, board_state: dict, lane_calls: list, task_puts: list, lane_status_code: list,
                  schedule_puts: list, board_stream_frames: list, stream_gate: "threading.Event | None" = None,
                  lane_response: "list | None" = None):
    """Stub d3 (offline CDN) + every /api/ call the page makes.

    `board_stream_frames`: SSE frame strings (each a full
    "event: board\\ndata: {...}\\n\\n" block), delivered one per *reconnect*
    — the first connection gets none, the second gets frames[0], the third
    frames[1], and so on. A `route.fulfill` response is a single static
    body, so it always closes the EventSource immediately after delivery;
    Chromium auto-reconnects per the SSE spec, and the `retry: 20` directive
    below makes that reconnect fast enough for a test to wait on.

    `stream_gate`: when given, every `/board/stream` connection is answered
    with an empty keep-alive (and a short `retry:` so the client polls back
    soon) for as long as `not stream_gate.is_set()`, checked fresh on each
    connection — never any real frame. Once the test calls
    `stream_gate.set()`, the next connection (at most one retry interval
    later) gets the real frame-delivery behavior. This check is a plain
    non-blocking `Event.is_set()`, called synchronously inside the route
    callback: Playwright Python's sync API dispatches every route callback
    on its one internal driver thread via a greenlet switch, so an actual
    *block* here (`stream_gate.wait()`) — or fulfilling from a separate
    thread to work around that — either freezes every other Playwright call
    the test makes or raises `greenlet.error: Cannot switch to a different
    thread` from `route.fulfill()`. Polling `is_set()` avoids both. Used to
    prove a frame provably arrives only after a specific point in the test
    (e.g. once a drawer is open and mid-edit) instead of racing page load on
    a fixed timer (#850 round-2 finding 5). `board_stream_frames` (and
    `board_state`, if the test wants the two to stay consistent) may be
    mutated by the caller any time before calling `stream_gate.set()` — the
    handler reads them fresh on the connection that delivers them.
    """

    def d3_handler(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")

    page.route("**/d3.v7.min.js", d3_handler)

    stream_attempt = [0]

    def api_handler(route):
        url = route.request.url
        method = route.request.method

        if "/api/agents/board/stream" in url:
            if stream_gate is not None and not stream_gate.is_set():
                route.fulfill(status=200, content_type="text/event-stream", body="retry: 50\n: ok\n\n")
                return
            frame_idx = stream_attempt[0] - 1  # frames start on the 2nd post-gate connection
            frame = board_stream_frames[frame_idx] if 0 <= frame_idx < len(board_stream_frames) else ""
            stream_attempt[0] += 1
            route.fulfill(status=200, content_type="text/event-stream", body=f"retry: 20\n: ok\n\n{frame}")
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
                # `lane_response`, when given, lets a test simulate the
                # server landing a card somewhere other than the requested
                # lane (e.g. a human_queue card that stays put on an
                # assign) — one popped entry per successful PUT (#850
                # round-3 finding 2a).
                landed = lane_response.pop(0) if lane_response else body.get("lane")
                _move_card_in_state(board_state, lane_match.group(1), landed)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": lane_match.group(1), "lane": landed}))
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

        schedule_match = re.search(r"/api/scheduler/([^/]+)$", url)
        if schedule_match and method == "PUT":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            schedule_puts.append(body)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": schedule_match.group(1)}))
            return

        if re.search(r"/api/agents/board$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return

        # Everything else the page might touch (pending-questions answer,
        # session focus/kill, task creation) — a harmless empty JSON body.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _open_board(page: Page, base_url, board_state=None, lane_calls=None, task_puts=None, lane_status_code=None,
                 schedule_puts=None, board_stream_frames=None, stream_gate=None, lane_response=None):
    _stub_routes(
        page,
        board_state if board_state is not None else _board_fixture(),
        lane_calls if lane_calls is not None else [],
        task_puts if task_puts is not None else [],
        lane_status_code if lane_status_code is not None else [200],
        schedule_puts if schedule_puts is not None else [],
        board_stream_frames if board_stream_frames is not None else [],
        stream_gate,
        lane_response,
    )
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="t1"]')


def _wait_for(predicate, timeout_ms=5000, interval_ms=25):
    """Poll `predicate` until it's truthy or the timeout elapses — for
    asserting on a plain Python side effect (e.g. an appended stub call)
    that has no DOM signal Playwright's own `expect(...)` can wait on."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_ms / 1000)
    assert predicate(), f"condition not met within {timeout_ms}ms"


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


class TestDrawerTagsEdit:
    def test_invalid_and_assignee_tokens_are_dropped(self, page: Page, agents_base_url):
        """Round-1 finding 8: the Tags field must not let a vault-comment
        injection or a duplicate assignee token reach the task store. t2 is
        assigned #me — typing an assignee token, a plain word, and an
        HTML-comment-shaped token must save only the plain word alongside
        the real assignee tag."""
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t2"]').click()
        tags = page.locator(".drawer-tags")
        expect(tags).to_be_visible()
        tags.fill("codex foo <!--id:abc-->")
        page.locator(".drawer-title").click()  # blur the tags field
        expect(page.locator(".toast.error")).to_be_visible(timeout=5000)
        expect(tags).to_have_value("foo")
        assert any(
            sorted(p.get("tags") or []) == ["foo", "me"] for p in task_puts
        ), task_puts
        assert not any("<" in t for p in task_puts for t in (p.get("tags") or []))
        assert not any("codex" in (p.get("tags") or []) for p in task_puts)


class TestDrawerAssigneeRevert:
    def test_failed_assignee_change_snaps_select_back(self, page: Page, agents_base_url):
        """Round-1 finding 9: a rejected assignee change (the lane PUT 409s)
        must not leave the unsaved value showing in the select."""
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[500])
        page.locator('[data-card-id="t2"]').click()
        assignee = page.locator(".drawer-assignee")
        expect(assignee).to_have_value("me")
        assignee.select_option("codex")
        expect(page.locator(".toast.error")).to_be_visible(timeout=5000)
        expect(assignee).to_have_value("me")
        assert lane_calls == [{"lane": "assigned", "assignee": "codex"}]


class TestScheduledCardDrawer:
    def test_editing_title_message_and_enabled_all_save_through_scheduler_api(self, page: Page, agents_base_url):
        """Round-1 finding 4: the scheduled card's title, message, and
        enabled checkbox save through PUT /api/scheduler/{id}, not a new
        board write path. Round-2 finding 9: the docstring claimed all
        three but only title was ever exercised — the message textarea's
        blur handler and the enabled checkbox's change handler
        (web/agents/board.js) were untested."""
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        page.locator('[data-card-id="s1"]').click()
        title = page.locator(".drawer-title")
        message = page.locator(".drawer-notes")
        enabled = page.locator('[data-field="enabled"]')
        expect(title).to_have_value("Morning briefing")
        title.fill("Evening briefing")
        message.click()  # blur the title field
        expect(message).to_have_value("Good morning")
        _wait_for(lambda: {"name": "Evening briefing"} in schedule_puts)

        # Blur message straight into the checkbox (never back through title —
        # title's own blur handler compares against the value captured when
        # its DOM node was created, and staying focused inside the drawer
        # deliberately skips re-rendering it (#850 round-2 finding 4), so a
        # second title blur here would re-save the same unchanged value).
        message.fill("Good evening")
        expect(enabled).to_be_checked()
        enabled.uncheck()  # blurs message, then toggles enabled
        _wait_for(lambda: {"message_content": "Good evening"} in schedule_puts)
        _wait_for(lambda: {"enabled": False} in schedule_puts)

        assert schedule_puts == [
            {"name": "Evening briefing"},
            {"message_content": "Good evening"},
            {"enabled": False},
        ]


class TestLiveUpdates:
    """Round-1 finding 12(b): the SSE live-update path itself was never
    driven by a browser test — the stub only ever sent the empty ": ok"
    comment. These deliver a real "event: board" frame on a reconnect."""

    def test_board_frame_moves_a_card_with_no_navigation(self, page: Page, agents_base_url):
        moved_board = copy.deepcopy(_board_fixture())
        _move_card_in_state(moved_board, "t1", "in_progress")
        frame = f"event: board\ndata: {json.dumps(moved_board)}\n\n"

        _open_board(page, agents_base_url, board_stream_frames=[frame])
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_be_visible()

        url_before = page.url
        expect(page.locator('.board-lane[data-lane="in_progress"] [data-card-id="t1"]')).to_be_visible(timeout=8000)
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_have_count(0)
        assert page.url == url_before  # no page reload/navigation happened

    def test_drawer_notes_survive_a_board_frame_while_typing_and_flushes_on_blur(self, page: Page, agents_base_url):
        """Round-2 finding 5 (reworks round-1 finding 12(b)'s test, which was
        a false positive): the prior version delivered its frame on the
        stub's *second* SSE connection, which — thanks to the `retry: 20`
        reconnect — landed ~20ms after page load, before the drawer was
        even opened. The guarded path (updateOpenDrawer's `!focused` check,
        #850 round-2 finding 4) was therefore never entered; the test
        passed even with that check deleted.

        This version withholds every `/board/stream` response behind a
        Python-side `threading.Event` the route handler polls non-blockingly
        (`stream_gate` — see `_stub_routes`'s docstring for why an actual
        block would deadlock Playwright's driver thread), so the frame
        provably cannot arrive until the test releases it — after the
        drawer is open and mid-edit. It also
        changes a field on a DIFFERENT card (t1) so there's an unambiguous,
        drawer-independent signal that the frame was actually applied."""
        stream_gate = threading.Event()
        board_state = _board_fixture()
        board_stream_frames: list[str] = []

        _open_board(
            page, agents_base_url, board_state=board_state,
            board_stream_frames=board_stream_frames, stream_gate=stream_gate,
        )
        page.locator('[data-card-id="t2"]').click()
        notes = page.locator(".drawer-notes")
        title = page.locator(".drawer-title")
        expect(notes).to_be_visible()
        expect(title).to_have_value("Ship the release")
        notes.fill("typed while a tick arrives")  # focus stays in the notes field

        # Mutate the board (a tag on a DIFFERENT card, t1; a title change on
        # the OPEN card, t2) and only now let the withheld stream connection
        # respond — proving the frame arrives after this point, not before.
        for card in board_state["lanes"]["unassigned"]:
            if card["id"] == "t1":
                card["tags"] = ["urgent"]
        for card in board_state["lanes"]["assigned"]:
            if card["id"] == "t2":
                card["title"] = "Ship the release (renamed in the vault)"
        board_stream_frames.append(f"event: board\ndata: {json.dumps(board_state)}\n\n")
        stream_gate.set()

        # Proof the frame was actually applied: an unrelated card (t1, not
        # open in the drawer) picks up its new tag chip in the lane view,
        # which render() always rebuilds regardless of drawer focus.
        expect(page.locator('[data-card-id="t1"] .board-chip-tag')).to_contain_text(
            "urgent", timeout=5000,
        )

        # The open card's drawer must NOT have rebuilt while notes had focus
        # — the typed text survived, and the title hasn't flushed yet.
        expect(notes).to_have_value("typed while a tick arrives")
        expect(title).to_have_value("Ship the release")

        # Leaving the field (not just moving within the drawer — activeElement
        # must actually leave drawerEl) flushes the deferred change.
        page.evaluate("() => document.activeElement.blur()")
        expect(title).to_have_value("Ship the release (renamed in the vault)")

    def test_deferred_frame_flushes_when_focus_leaves_drawer_without_an_edit(self, page: Page, agents_base_url):
        """#850 round-3 finding 1: the drawerEl `focusout` listener itself
        must flush a deferred render once focus actually leaves the drawer
        (blur() to <body>), independent of any field edit. Clicking into
        notes WITHOUT typing means the notes blur handler's own
        short-circuit (`value === card.notes`) never fires a PUT or
        fetchBoard() — the ONLY path left that can flush the deferred
        frame is the focusout listener. This isolates that listener from
        the sibling test above, which types text and so is flushed by the
        notes PUT's own fetchBoard(), never by the listener (verified by
        temporarily deleting the listener: this test fails, the sibling
        test above still passes)."""
        stream_gate = threading.Event()
        board_state = _board_fixture()
        board_stream_frames: list[str] = []

        _open_board(
            page, agents_base_url, board_state=board_state,
            board_stream_frames=board_stream_frames, stream_gate=stream_gate,
        )
        page.locator('[data-card-id="t2"]').click()
        notes = page.locator(".drawer-notes")
        title = page.locator(".drawer-title")
        expect(notes).to_be_visible()
        expect(title).to_have_value("Ship the release")
        notes.click()  # focus only, no typing

        for card in board_state["lanes"]["unassigned"]:
            if card["id"] == "t1":
                card["tags"] = ["urgent"]
        for card in board_state["lanes"]["assigned"]:
            if card["id"] == "t2":
                card["title"] = "Ship the release (renamed in the vault)"
        board_stream_frames.append(f"event: board\ndata: {json.dumps(board_state)}\n\n")
        stream_gate.set()

        # Proof the frame was actually applied: an unrelated card (t1)
        # picks up its new tag chip in the lane view.
        expect(page.locator('[data-card-id="t1"] .board-chip-tag')).to_contain_text(
            "urgent", timeout=5000,
        )
        # Still deferred: the open card's drawer hasn't rebuilt yet.
        expect(title).to_have_value("Ship the release")

        page.evaluate("() => document.activeElement.blur()")
        expect(title).to_have_value("Ship the release (renamed in the vault)")

    def test_action_button_click_survives_a_deferred_frame(self, page: Page, agents_base_url):
        """#850 round-3 finding 1: without a `relatedTarget` guard on the
        focusout listener, mousedown on a drawer action button fires
        focusout while `document.activeElement` is briefly <body> (focus
        hasn't landed on the button yet). If a deferred frame is pending at
        that instant, the drawer gets rebuilt via innerHTML between
        mousedown and mouseup — the click lands on a now-detached node and
        the Answer composer never opens."""
        stream_gate = threading.Event()
        board_state = _board_fixture()
        board_stream_frames: list[str] = []

        _open_board(
            page, agents_base_url, board_state=board_state,
            board_stream_frames=board_stream_frames, stream_gate=stream_gate,
        )
        page.locator('[data-card-id="t3"]').click()  # t3: pending_question id 1
        notes = page.locator(".drawer-notes")
        answer_btn = page.get_by_role("button", name="Answer")
        expect(notes).to_be_visible()
        expect(answer_btn).to_be_visible()
        notes.click()  # focus only, no typing

        for card in board_state["lanes"]["unassigned"]:
            if card["id"] == "t1":
                card["tags"] = ["urgent"]
        for card in board_state["lanes"]["human_queue"]:
            if card["id"] == "t3":
                card["title"] = "Debug prod issue (renamed)"
        board_stream_frames.append(f"event: board\ndata: {json.dumps(board_state)}\n\n")
        stream_gate.set()

        # Proof the frame landed (drawer-independent signal).
        expect(page.locator('[data-card-id="t1"] .board-chip-tag')).to_contain_text(
            "urgent", timeout=5000,
        )

        answer_btn.click()
        expect(page.locator("#answer-title")).to_be_visible(timeout=3000)

    def test_lane_mismatch_toast_shows_landed_lane(self, page: Page, agents_base_url):
        """#850 round-2 finding 2b, untested until now: when the server
        lands a card in a different lane than requested (e.g. a
        human_queue card that stays put on an assign attempt), the client
        must toast the actual landed lane instead of leaving the operator
        to notice the card "snapped back" on its own."""
        lane_calls = []
        _open_board(
            page, agents_base_url, lane_calls=lane_calls,
            lane_response=["human_queue"],
        )
        _drag_card(page, "t4", "assigned")
        expect(page.locator(".toast")).to_contain_text("landed in Human queue", timeout=5000)
        assert lane_calls == [{"lane": "assigned", "assignee": "me"}]
        expect(page.locator('.board-lane[data-lane="human_queue"] [data-card-id="t4"]')).to_be_visible()

    def test_stale_answer_button_cleared_when_pending_question_resolves(self, page: Page, agents_base_url):
        """#850 round-2 finding 3, untested until now: when a card's
        pending_question is cleared elsewhere (the agent gets an answer
        via another channel), the open drawer must swap out the stale
        Answer button rather than leaving it behind for a second click
        that would 404."""
        stream_gate = threading.Event()
        board_state = _board_fixture()
        board_stream_frames: list[str] = []

        _open_board(
            page, agents_base_url, board_state=board_state,
            board_stream_frames=board_stream_frames, stream_gate=stream_gate,
        )
        page.locator('[data-card-id="t3"]').click()  # t3: pending_question id 1, human_queue
        expect(page.get_by_role("button", name="Answer")).to_be_visible()
        expect(page.get_by_role("button", name="Resolve")).to_have_count(0)

        for card in board_state["lanes"]["human_queue"]:
            if card["id"] == "t3":
                card["pending_question"] = None
        board_stream_frames.append(f"event: board\ndata: {json.dumps(board_state)}\n\n")
        stream_gate.set()

        expect(page.get_by_role("button", name="Answer")).to_have_count(0, timeout=5000)
        expect(page.get_by_role("button", name="Resolve")).to_be_visible()


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

    def test_done_lane_visible_by_default_only_cancelled_behind_filter(self, page: Page, agents_base_url):
        """Round-1 finding 3: the "include cancelled" checkbox only hides
        cancelled cards — the Done lane itself (finished tasks) stays
        visible whether it's checked or not."""
        _open_board(page, agents_base_url)
        expect(page.locator('[data-card-id="t5"]')).to_be_visible()
        expect(page.locator('[data-card-id="t6"]')).to_have_count(0)
        page.locator("#board-filter-done").check()
        expect(page.locator('[data-card-id="t5"]')).to_be_visible()
        expect(page.locator('[data-card-id="t6"]')).to_be_visible()

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
