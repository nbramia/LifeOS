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
filters including assignee=me and lane=human_queue combined, and (#859) the
assignment pickers mounted in the drawer — render from the model catalog,
one save per picker, the Open action's success and 409 paths, and that
scheduled cards render no pickers.
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

# Obviously synthetic — same shape GET /api/agents/models returns (#851).
_MODEL_CATALOG = {
    "engines": {
        "claude": [
            {"id": "claude-opus-5", "label": "Claude Opus 5", "pricing": None},
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "pricing": None},
        ],
        "codex": [{"id": "gpt-5.5", "label": "GPT-5.5", "pricing": None}],
        "local": [],
        "hermes": [],
    },
    "refreshed_at": "2026-01-01T00:00:00Z",
    "stale": False,
}

# Assignee-name tags board.js's own drawer strips/re-adds on an assignee
# change (web/agents/board.js ASSIGNEES) — used by the lane stub below to
# mirror plan_lane_move's tag bookkeeping on a drawer-driven assign (#859
# review round 2 finding 1).
_ASSIGNEE_TAGS = {"me", "claude", "codex", "hermes", "local"}


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
                {
                    # #859: assignee claude/codex (unlike t2's "me") — used
                    # by the assignment-picker and Open-action tests.
                    "kind": "task", "id": "t7", "title": "Deploy the release candidate",
                    "notes": "", "status": "todo", "tags": ["claude"], "assignee": "claude",
                    "fields": {}, "context": "Ops", "updated_at": "2026-01-01T00:00:00+00:00",
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
                  lane_response: "list | None" = None, open_calls: "list | None" = None,
                  open_response: "dict | None" = None):
    """Stub d3 (offline CDN) + every /api/ call the page makes.

    `open_calls`: appended with each opened card id (POST
    /api/agents/board/cards/{id}/open). `open_response`, when given, is
    `{"status": <code>, "detail": <str>}` — the 409 failure path (#859);
    omitted defaults to a 200 success. `GET /api/agents/models` is served
    from `_MODEL_CATALOG`.

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

    if open_calls is None:
        open_calls = []

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

        open_match = re.search(r"/api/agents/board/cards/([^/]+)/open$", url)
        if open_match and method == "POST":
            open_calls.append(open_match.group(1))
            if open_response and open_response.get("status", 200) != 200:
                route.fulfill(
                    status=open_response["status"],
                    content_type="application/json",
                    body=json.dumps({"detail": open_response.get("detail", "boom")}),
                )
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
            return

        if re.search(r"/api/agents/models$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_MODEL_CATALOG))
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
                # Mirror plan_lane_move's assignee bookkeeping: the drawer's
                # assignee select PUTs `assignee` alongside `lane` (#859
                # review round 2 finding 1) — a drag move omits the key
                # entirely, so key on `"assignee" in body`, not truthiness,
                # and also clear on an explicit move to unassigned.
                if "assignee" in body or body.get("lane") == "unassigned":
                    assignee = body.get("assignee")
                    for cards in board_state["lanes"].values():
                        for card in cards:
                            if card["id"] != lane_match.group(1):
                                continue
                            others = [t for t in card.get("tags", []) if t.lower() not in _ASSIGNEE_TAGS]
                            card["assignee"] = assignee or None
                            card["tags"] = ([assignee] if assignee else []) + others
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": lane_match.group(1), "lane": landed}))
            else:
                route.fulfill(status=code, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return

        task_match = re.search(r"/api/tasks/([^/]+)$", url)
        if task_match and method == "PUT":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            task_puts.append(body)
            # Mutate the fixture card the way a real PUT would, so a test
            # that reopens the drawer to check a saved value doesn't see
            # the stale fixture (mirrors _move_card_in_state; #859 trap 3).
            task_id = task_match.group(1)
            for cards in board_state["lanes"].values():
                for card in cards:
                    if card["id"] != task_id:
                        continue
                    if "fields" in body and isinstance(body["fields"], dict):
                        fields = card.setdefault("fields", {})
                        for key, value in body["fields"].items():
                            if value is None:
                                fields.pop(key, None)
                            else:
                                fields[key] = value
                    if "notes" in body:
                        card["notes"] = body["notes"]
                    if "tags" in body:
                        card["tags"] = body["tags"]
                    if "context" in body:
                        card["context"] = body["context"]
                    if "description" in body:
                        card["title"] = body["description"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": task_id}))
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
                 schedule_puts=None, board_stream_frames=None, stream_gate=None, lane_response=None,
                 open_calls=None, open_response=None):
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
        open_calls,
        open_response,
    )
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="t1"]')


def _wait_for(predicate, page: Page, timeout_ms=5000, interval_ms=25):
    """Poll `predicate` until it's truthy or the timeout elapses — for
    asserting on a plain Python side effect (e.g. an appended stub call)
    that has no DOM signal Playwright's own `expect(...)` can wait on.

    `page` is required: Playwright Python's sync API only advances its
    internal event loop — and so only delivers an already-arrived
    intercepted request's route callback — from inside a call back into
    Playwright, via a greenlet switch tied to the calling thread. A pure
    `time.sleep()` poll never makes such a call, so a route callback that
    already landed can sit undelivered for the whole timeout regardless of
    what else is happening on the page. `page.wait_for_timeout(...)` is
    itself a Playwright call, so using it as the poll's sleep also serves
    as the pump — no separate `evaluate()` + `time.sleep()` pair needed."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(interval_ms)
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
        _wait_for(lambda: {"name": "Evening briefing"} in schedule_puts, page=page)

        # Blur message straight into the checkbox (never back through title —
        # title's own blur handler compares against the value captured when
        # its DOM node was created, and staying focused inside the drawer
        # deliberately skips re-rendering it (#850 round-2 finding 4), so a
        # second title blur here would re-save the same unchanged value).
        message.fill("Good evening")
        expect(enabled).to_be_checked()
        enabled.uncheck()  # blurs message, then toggles enabled
        _wait_for(lambda: {"message_content": "Good evening"} in schedule_puts, page=page)
        _wait_for(lambda: {"enabled": False} in schedule_puts, page=page)

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
        """#862: withhold the frame behind `stream_gate` (see the sibling
        drawer tests below) so the "still in unassigned" assertion can't
        lose a race with the stub's `retry: 20` reconnect on a slow first
        paint — previously flaky (measured 6/30 and 3/30 in #860's
        verification) because that frame could already have landed by the
        time the first assertion polled."""
        stream_gate = threading.Event()
        moved_board = copy.deepcopy(_board_fixture())
        _move_card_in_state(moved_board, "t1", "in_progress")
        frame = f"event: board\ndata: {json.dumps(moved_board)}\n\n"

        _open_board(page, agents_base_url, board_stream_frames=[frame], stream_gate=stream_gate)
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_be_visible()

        url_before = page.url
        stream_gate.set()
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

    def test_kill_button_cleared_when_linked_session_reaches_terminal_status(self, page: Page, agents_base_url):
        """#850 round-2 finding 3's other half (round-4 finding 1): when the
        card's linked session reaches a terminal status elsewhere (e.g. the
        CLI process exits on its own), the open drawer must drop the stale
        Kill button rather than leaving it behind for a click that would
        404. Every card in _board_fixture() has session: None, so this half
        of updateOpenDrawer's `prevSessionStatus !== freshSessionStatus`
        clause (web/agents/board.js) was never exercised by any test."""
        stream_gate = threading.Event()
        board_state = copy.deepcopy(_board_fixture())
        board_stream_frames: list[str] = []

        for card in board_state["lanes"]["human_queue"]:
            if card["id"] == "t3":
                card["session"] = {
                    "session_id": "s-t3", "status": "running",
                    "host": "test-host", "routing": "claude_code",
                    "model_label": "Sonnet", "source": "claude_code",
                }

        _open_board(
            page, agents_base_url, board_state=board_state,
            board_stream_frames=board_stream_frames, stream_gate=stream_gate,
        )
        page.locator('[data-card-id="t3"]').click()  # t3: pending_question id 1, human_queue
        expect(page.get_by_role("button", name="Kill")).to_be_visible()

        for card in board_state["lanes"]["human_queue"]:
            if card["id"] == "t3":
                card["session"]["status"] = "ended"
        board_stream_frames.append(f"event: board\ndata: {json.dumps(board_state)}\n\n")
        stream_gate.set()

        expect(page.get_by_role("button", name="Kill")).to_have_count(0, timeout=5000)


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


class TestTabSwitching:
    """#850 verify-1 findings 1 and 2: `#board-view { display: flex }`
    (specificity 1-0-0) and `.chips { display: flex }` (0-1-0) each beat
    the generic `[hidden]` rule they were relying on, so setting
    `.hidden = true` on either element never actually hid it. Assert on
    COMPUTED style, not element reachability — Playwright's own visibility
    helpers auto-scroll to an element, which hid this bug from every prior
    test."""

    def _computed_display(self, page: Page, selector: str) -> str:
        return page.evaluate(
            "(sel) => getComputedStyle(document.querySelector(sel)).display", selector,
        )

    def test_graph_tab_hides_board_view_and_chips_and_back(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        assert self._computed_display(page, "#board-view") != "none"
        assert self._computed_display(page, "#graph-chips") == "none"

        page.locator("#tab-btn-graph").click()
        expect(page.locator("#graph-view")).to_be_visible()
        assert self._computed_display(page, "#board-view") == "none"
        assert self._computed_display(page, "#graph-chips") != "none"

        page.locator("#tab-btn-board").click()
        expect(page.locator("#board-view")).to_be_visible()
        assert self._computed_display(page, "#board-view") != "none"
        assert self._computed_display(page, "#graph-chips") == "none"


class TestDragThenClick:
    """#850 verify-1 finding 3: `suppressNextClick` was cleared only inside
    the card's own click handler, but a drop's re-render replaces every
    card node and the drag's trailing click often lands on a different
    element (the drop target lane), so it never reaches that handler — the
    flag then lingers and swallows the operator's NEXT genuine click on the
    same card id. Reproduced for both the success path and a rejected
    (409) move, matching what the verify agent observed live."""

    def test_click_after_successful_drag_opens_drawer(self, page: Page, agents_base_url):
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[200])
        _drag_card(page, "t1", "in_progress")
        expect(page.locator('.board-lane[data-lane="in_progress"] [data-card-id="t1"]')).to_be_visible(timeout=5000)

        page.locator('[data-card-id="t1"]').click()
        expect(page.locator(".drawer-title")).to_have_value("Investigate outage")

    def test_click_after_rejected_drag_opens_drawer(self, page: Page, agents_base_url):
        lane_calls = []
        _open_board(page, agents_base_url, lane_calls=lane_calls, lane_status_code=[409])
        _drag_card(page, "t1", "in_progress")
        expect(page.locator(".toast.error")).to_be_visible(timeout=5000)
        # The rejected move re-renders in place — card never left "unassigned".
        expect(page.locator('.board-lane[data-lane="unassigned"] [data-card-id="t1"]')).to_be_visible()

        page.locator('[data-card-id="t1"]').click()
        expect(page.locator(".drawer-title")).to_have_value("Investigate outage")


class TestAssignmentPickers:
    """#859: web/agents/assignment.js's model/effort/host pickers mounted
    into the task drawer. t7 (assignee claude, lane Assigned) is the fixture
    card for these — t2's assignee "me" can't drive the module's own engine
    select (it only knows claude/codex/local/hermes)."""

    def test_pickers_render_from_model_catalog_with_engine_row_hidden(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t7"]').click()
        assignment = page.locator(".drawer-assignment")
        expect(assignment).to_be_visible()
        # The drawer's OWN Assignee select stays the one assignee writer —
        # the module's engine row must be hidden, not removed, to avoid a
        # second, conflicting assignee control.
        expect(assignment.locator('[data-row="engine"]')).to_be_hidden()
        expect(assignment.locator('[data-row="model"]')).to_be_visible()
        expect(assignment.locator('[data-row="effort"]')).to_be_visible()
        expect(assignment.locator('[data-row="host"]')).to_be_visible()
        # Model options populate asynchronously once GET /api/agents/models
        # resolves — default + 2 claude models from _MODEL_CATALOG.
        expect(assignment.locator("[data-field='model'] option")).to_have_count(3)

    def test_pickers_hidden_for_me_assignee(self, page: Page, agents_base_url):
        # t2 is assignee "me" — the module's ENGINES list (claude/codex/
        # local/hermes) has no "me" entry, so its engine select falls back
        # to no selection and none of model/effort/host accept it.
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t2"]').click()
        assignment = page.locator(".drawer-assignment")
        expect(assignment).to_be_attached()
        expect(assignment.locator('[data-row="model"]')).to_be_hidden()
        expect(assignment.locator('[data-row="effort"]')).to_be_hidden()
        expect(assignment.locator('[data-row="host"]')).to_be_hidden()

    def test_assignee_change_with_focus_held_remounts_pickers_and_open(self, page: Page, agents_base_url):
        """#859 review round 1 finding 1: a native <select> keeps focus
        after firing `change`, so `updateOpenDrawer`'s `!focused` check
        would otherwise skip the rebuild and leave the pickers/Open button
        hidden until focus later left the drawer. board.js's assignee
        handler must re-render explicitly on its success path
        (web/agents/board.js ~L649-657) — drive the select directly (no
        `select_option`, which itself blurs) and assert with no click
        outside the drawer."""
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t1"]').click()
        expect(page.locator(".drawer-title")).to_have_value("Investigate outage")
        page.locator(".drawer-assignee").evaluate(
            "el => { el.focus(); el.value = 'claude'; "
            "el.dispatchEvent(new Event('change', { bubbles: true })); }"
        )
        expect(page.locator(".drawer-assignment [data-row='model']")).to_be_visible()
        expect(page.locator(".drawer-assignment [data-row='model']")).to_have_count(1)
        expect(page.get_by_role("button", name="Open")).to_be_visible()
        expect(page.get_by_role("button", name="Open")).to_have_count(1)

    def test_changing_model_writes_exactly_one_fields_put(self, page: Page, agents_base_url):
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t7"]').click()
        model_select = page.locator(".drawer-assignment [data-field='model']")
        expect(model_select.locator("option")).to_have_count(3)
        model_select.select_option("claude-sonnet-5")
        _wait_for(lambda: any("fields" in p for p in task_puts), page=page)
        page.wait_for_timeout(100)  # let any second, unwanted PUT land before counting
        fields_puts = [p for p in task_puts if "fields" in p]
        assert len(fields_puts) == 1, task_puts
        assert fields_puts[0] == {
            "fields": {"model": "claude-sonnet-5", "effort": None, "host": None, "assigned_by": "board"}
        }

    def test_changing_effort_writes_exactly_one_fields_put(self, page: Page, agents_base_url):
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t7"]').click()
        effort_select = page.locator(".drawer-assignment [data-field='effort']")
        expect(effort_select).to_be_visible()
        effort_select.select_option("high")
        _wait_for(lambda: any("fields" in p for p in task_puts), page=page)
        page.wait_for_timeout(100)  # let any second, unwanted PUT land before counting
        fields_puts = [p for p in task_puts if "fields" in p]
        assert len(fields_puts) == 1, task_puts
        assert fields_puts[0] == {
            "fields": {"model": None, "effort": "high", "host": None, "assigned_by": "board"}
        }

    def test_changing_host_writes_exactly_one_fields_put(self, page: Page, agents_base_url):
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t7"]').click()
        host_input = page.locator(".drawer-assignment [data-field='host']")
        expect(host_input).to_be_visible()
        host_input.fill("build-box-2")
        host_input.press("Tab")  # text input needs an explicit blur to fire `change`
        _wait_for(lambda: any("fields" in p for p in task_puts), page=page)
        page.wait_for_timeout(100)  # let any second, unwanted PUT land before counting
        fields_puts = [p for p in task_puts if "fields" in p]
        assert len(fields_puts) == 1, task_puts
        assert fields_puts[0] == {
            "fields": {"model": None, "effort": None, "host": "build-box-2", "assigned_by": "board"}
        }

    def test_saved_model_reflected_on_reopen(self, page: Page, agents_base_url):
        task_puts = []
        _open_board(page, agents_base_url, task_puts=task_puts)
        page.locator('[data-card-id="t7"]').click()
        model_select = page.locator(".drawer-assignment [data-field='model']")
        expect(model_select.locator("option")).to_have_count(3)
        model_select.select_option("claude-sonnet-5")
        _wait_for(lambda: any("fields" in p for p in task_puts), page=page)
        page.wait_for_timeout(100)  # let any second, unwanted PUT land before reload

        # The stub mutates the fixture card synchronously before fulfilling
        # the PUT (mirrors a real save), so a fresh page load's initial
        # board fetch is guaranteed to see it — reloading avoids racing the
        # client's own async fetchBoard()-after-save against a click-driven
        # close+reopen.
        page.reload()
        page.wait_for_selector('[data-card-id="t1"]')
        page.locator('[data-card-id="t7"]').click()
        reopened_model = page.locator(".drawer-assignment [data-field='model']")
        expect(reopened_model.locator("option")).to_have_count(3)
        expect(reopened_model).to_have_value("claude-sonnet-5")

    def test_open_button_posts_and_shows_success_toast(self, page: Page, agents_base_url):
        open_calls = []
        _open_board(page, agents_base_url, open_calls=open_calls)
        page.locator('[data-card-id="t7"]').click()
        open_btn = page.get_by_role("button", name="Open")
        expect(open_btn).to_be_visible()
        open_btn.click()
        expect(page.locator(".toast:not(.error)")).to_contain_text("Opened.", timeout=5000)
        assert open_calls == ["t7"]

    def test_open_button_409_shows_detail_in_toast(self, page: Page, agents_base_url):
        open_calls = []
        _open_board(
            page, agents_base_url, open_calls=open_calls,
            open_response={"status": 409, "detail": "card is not in Assigned state"},
        )
        page.locator('[data-card-id="t7"]').click()
        page.get_by_role("button", name="Open").click()
        expect(page.locator(".toast.error")).to_contain_text(
            "card is not in Assigned state", timeout=5000,
        )
        assert open_calls == ["t7"]

    def test_open_button_absent_for_non_claude_codex_assignee(self, page: Page, agents_base_url):
        # t2 is Assigned but assignee "me" — Open is only for claude/codex.
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t2"]').click()
        expect(page.locator(".drawer-title")).to_have_value("Ship the release")
        expect(page.get_by_role("button", name="Open")).to_have_count(0)

    def test_open_button_absent_when_not_in_assigned_lane(self, page: Page, agents_base_url):
        # t3 carries the "codex" assignee tag but sits in Human queue, not
        # Assigned — the Open action is scoped to the Assigned lane.
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="t3"]').click()
        expect(page.locator(".drawer-title")).to_have_value("Debug prod issue")
        expect(page.get_by_role("button", name="Open")).to_have_count(0)

    def test_scheduled_card_drawer_has_no_assignment_pickers(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('[data-card-id="s1"]').click()
        expect(page.locator(".drawer-schedule-hint")).to_be_visible()
        expect(page.locator(".drawer-assignment")).to_have_count(0)
        expect(page.locator(".assignment-row")).to_have_count(0)
        expect(page.get_by_role("button", name="Open")).to_have_count(0)
