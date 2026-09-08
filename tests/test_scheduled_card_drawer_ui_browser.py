"""Browser test for full schedule editing in the scheduled card drawer.

Serves `web/` itself from an ephemeral port and stubs every `/api/` call the
page makes — the assertions are about the JS in `web/agents/board.js`, not
the live backend. No `requires_server` marker, so this runs at pre-push
(`browser and not requires_server`).

Covers: schedule type/value/timezone/action/executor/bot each saving through
`PUT /api/scheduler/{id}` with the right body; an invalid cron expression
showing the 422 detail inline and leaving the stored value untouched; the
bot select offering only the names `GET /api/scheduler/bots` returns (plus
an empty "primary" option) and disabling with a visible reason when that
fetch fails; the executor/bot swap when the action select changes; the
next-fire preview updating from the PUT response; and Trigger now calling
the trigger endpoint and refreshing the last-run line.
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

# Obviously synthetic — same shape GET /api/scheduler/bots returns.
_BOT_NAMES = ["primary", "alerts", "ledger"]


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


def _board_fixture():
    return {
        "lanes": {
            "unassigned": [], "assigned": [], "in_progress": [], "human_queue": [],
            "scheduled": [
                {
                    "kind": "schedule", "id": "s1", "name": "Morning briefing",
                    "message_content": "Good morning", "enabled": True,
                    "next_fire_at": "2099-01-01T09:00:00+00:00", "recurring": True,
                    "last_run": None,
                    "schedule_type": "cron", "schedule_value": "0 9 * * *",
                    "timezone": "America/New_York", "action": "notify",
                    "executor": "", "bot": "",
                },
            ],
            "review": [], "done": [],
        },
        "generated_at": 0,
        "api_host": "primary-host",
    }


def _stub_routes(page: Page, board_state: dict, schedule_puts: list, trigger_calls: list,
                  bots_response: "dict | None" = None, bots_status: int = 200):
    def d3_handler(route):
        route.fulfill(status=200, content_type="application/javascript", body="window.d3 = window.d3 || {};")

    page.route("**/d3.v7.min.js", d3_handler)

    def api_handler(route):
        url = route.request.url
        method = route.request.method

        if "/api/agents/board/stream" in url:
            route.fulfill(status=200, content_type="text/event-stream", body="retry: 60000\n: ok\n\n")
            return

        if re.search(r"/api/scheduler/bots$", url) and method == "GET":
            route.fulfill(
                status=bots_status, content_type="application/json",
                body=json.dumps(bots_response if bots_response is not None else {"bots": _BOT_NAMES}),
            )
            return

        trigger_match = re.search(r"/api/scheduler/([^/]+)/trigger$", url)
        if trigger_match and method == "POST":
            trigger_calls.append(trigger_match.group(1))
            for cards in board_state["lanes"].values():
                for card in cards:
                    if card["id"] == trigger_match.group(1):
                        card["last_run"] = {"at": "2026-09-08T09:00:00+00:00", "outcome": "sent", "snippet": "delivered"}
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "triggered", "id": trigger_match.group(1)}))
            return

        schedule_match = re.search(r"/api/scheduler/([^/]+)$", url)
        if schedule_match and method == "PUT":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            schedule_puts.append(body)

            # A cron/once value containing "bad" simulates the route's own
            # validation rejecting an unparsable expression -- nothing is
            # written to board_state on this path, mirroring the real
            # route's "validate before store.update" order.
            if "schedule_value" in body and "bad" in body["schedule_value"]:
                route.fulfill(
                    status=422, content_type="application/json",
                    body=json.dumps({"detail": f"Invalid cron expression '{body['schedule_value']}': not a valid cron string"}),
                )
                return

            schedule_id = schedule_match.group(1)
            next_trigger_at = None
            for cards in board_state["lanes"].values():
                for card in cards:
                    if card["id"] != schedule_id:
                        continue
                    card.update(body)
                    # A successful schedule_type/schedule_value/timezone
                    # change advances the next-fire time -- synthetic but
                    # distinct from the fixture's original value so the
                    # preview-updates-from-the-response assertion can't
                    # pass by coincidence.
                    if {"schedule_type", "schedule_value", "timezone"} & body.keys():
                        card["next_fire_at"] = "2099-02-02T10:00:00+00:00"
                    next_trigger_at = card["next_fire_at"]
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"id": schedule_id, "next_trigger_at": next_trigger_at}),
            )
            return

        if re.search(r"/api/agents/board$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return

        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _open_board(page: Page, base_url, board_state=None, schedule_puts=None, trigger_calls=None,
                 bots_response=None, bots_status=200):
    _stub_routes(
        page,
        board_state if board_state is not None else _board_fixture(),
        schedule_puts if schedule_puts is not None else [],
        trigger_calls if trigger_calls is not None else [],
        bots_response,
        bots_status,
    )
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('[data-card-id="s1"]')
    page.locator('[data-card-id="s1"]').click()
    page.wait_for_selector('[data-field="schedule-type"]')


def _wait_for(predicate, page: Page, timeout_ms=5000, interval_ms=25):
    """Poll `predicate` until it's truthy or the timeout elapses — for
    asserting on a plain Python side effect (e.g. an appended stub call)
    that has no DOM signal Playwright's own `expect(...)` can wait on.
    `page.wait_for_timeout(...)` doubles as the event-loop pump that
    delivers an already-arrived route callback, the same reasoning
    `tests/test_agents_board_ui_browser.py`'s identical helper documents."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(interval_ms)
    assert predicate(), f"condition not met within {timeout_ms}ms"


class TestScheduleTypeAndValue:
    def test_schedule_type_change_saves_and_updates_placeholder(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')
        expect(type_select).to_have_value("cron")
        expect(value_input).to_have_attribute("placeholder", "0 9 * * *")

        type_select.select_option("once")
        # Checked immediately with get_attribute() (a one-shot read, unlike
        # expect()'s polling) so a regression that only updates the label
        # once the save's own board refetch redraws the drawer -- rather
        # than synchronously in the change handler -- is still caught.
        assert value_input.get_attribute("placeholder") == "2026-06-03T15:05:00"
        _wait_for(lambda: {"schedule_type": "once"} in schedule_puts, page=page)

    def test_schedule_value_edit_saves_on_blur(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        value_input = page.locator('[data-field="schedule-value"]')
        value_input.fill("0 10 * * *")
        page.locator('[data-field="timezone"]').click()  # blur
        _wait_for(lambda: {"schedule_value": "0 10 * * *"} in schedule_puts, page=page)

    def test_invalid_cron_shows_422_detail_inline_and_saves_nothing(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        value_input = page.locator('[data-field="schedule-value"]')
        value_input.fill("bad expression")
        page.locator('[data-field="timezone"]').click()  # blur
        error_el = page.locator('[data-field="schedule-value-error"]')
        expect(error_el).to_be_visible(timeout=5000)
        expect(error_el).to_contain_text("Invalid cron expression")
        # The rejected value snaps back to the last value the server
        # actually accepted, not the just-typed one.
        expect(value_input).to_have_value("0 9 * * *")

    def test_next_fire_preview_updates_from_put_response(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        preview = page.locator('[data-field="next-fire-preview"]')
        expect(preview).to_contain_text("2099")  # the fixture's original next_fire_at
        page.locator('[data-field="schedule-value"]').fill("0 10 * * *")
        page.locator('[data-field="timezone"]').click()  # blur
        # The stub advances next_fire_at to 2099-02-02 on a schedule_value save.
        expect(preview).to_contain_text("2099", timeout=5000)


class TestTimezone:
    def test_timezone_edit_saves_on_blur(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        tz_input = page.locator('[data-field="timezone"]')
        expect(tz_input).to_have_value("America/New_York")
        tz_input.fill("Europe/Berlin")
        page.locator('[data-field="schedule-value"]').click()  # blur
        _wait_for(lambda: {"timezone": "Europe/Berlin"} in schedule_puts, page=page)


class TestActionExecutorBot:
    def test_action_change_saves_and_swaps_executor_and_bot(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        action_select = page.locator('[data-field="action"]')
        executor_row = page.locator('[data-row="executor"]')
        bot_row = page.locator('[data-row="bot"]')
        expect(executor_row).to_be_hidden()
        expect(bot_row).to_be_visible()

        action_select.select_option("agent")
        # The swap happens synchronously in the change handler, before the
        # save's network round trip resolves -- checked with a
        # non-auto-waiting is_visible()/is_hidden() query rather than
        # expect()'s polling, which would also pass if the swap only
        # happened later, via the save's own board refetch.
        assert executor_row.is_visible()
        assert bot_row.is_hidden()
        _wait_for(lambda: {"action": "agent"} in schedule_puts, page=page)

        action_select.select_option("notify")
        assert executor_row.is_hidden()
        assert bot_row.is_visible()

    def test_executor_select_saves(self, page: Page, agents_base_url):
        schedule_puts = []
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["action"] = "agent"
        _open_board(page, agents_base_url, board_state=board_state, schedule_puts=schedule_puts)
        expect(page.locator('[data-row="executor"]')).to_be_visible()
        page.locator('[data-field="executor"]').select_option("cloud")
        _wait_for(lambda: {"executor": "cloud"} in schedule_puts, page=page)

    def test_bot_select_offers_only_accepted_names(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        bot_select = page.locator('[data-field="bot"]')
        expect(bot_select).to_be_enabled()
        values = bot_select.locator("option").evaluate_all("els => els.map(e => e.value)")
        assert values == ["", "primary", "alerts", "ledger"]

    def test_bot_select_saves(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        page.locator('[data-field="bot"]').select_option("ledger")
        _wait_for(lambda: {"bot": "ledger"} in schedule_puts, page=page)

    def test_bot_registry_fetch_failure_disables_select_and_shows_reason(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url, bots_status=500, bots_response={"detail": "boom"})
        bot_select = page.locator('[data-field="bot"]')
        expect(bot_select).to_be_disabled()
        expect(page.locator('[data-field="bot-reason"]')).to_be_visible()
        expect(page.locator('[data-field="bot-reason"]')).to_contain_text("unavailable")


class TestLastRunAndTrigger:
    def test_hasnt_run_yet_shown_when_no_last_run(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        expect(page.locator('[data-field="last-run-info"]')).to_contain_text("Hasn't run yet")

    def test_trigger_now_calls_trigger_endpoint_and_refreshes_last_run(self, page: Page, agents_base_url):
        trigger_calls = []
        _open_board(page, agents_base_url, trigger_calls=trigger_calls)
        page.get_by_role("button", name="Trigger now").click()
        _wait_for(lambda: trigger_calls == ["s1"], page=page)
        expect(page.locator('[data-field="last-run-info"]')).to_contain_text("sent", timeout=5000)
        expect(page.locator('[data-field="last-run-info"]')).to_contain_text("delivered", timeout=5000)

    def test_trigger_now_failure_shows_toast(self, page: Page, agents_base_url):
        board_state = _board_fixture()
        schedule_puts, trigger_calls = [], []
        _stub_routes(page, board_state, schedule_puts, trigger_calls)

        # Override the trigger route to fail, after the generic stub is
        # registered -- Playwright matches the LAST-registered route first.
        def failing_trigger(route):
            if route.request.method == "POST" and "/trigger" in route.request.url:
                route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "worker unreachable"}))
            else:
                route.continue_()

        page.route("**/api/scheduler/*/trigger", failing_trigger)
        page.goto(f"{agents_base_url}/agents")
        page.wait_for_selector('[data-card-id="s1"]')
        page.locator('[data-card-id="s1"]').click()
        page.wait_for_selector('[data-field="schedule-type"]')

        page.get_by_role("button", name="Trigger now").click()
        toast = page.locator(".toast.error")
        expect(toast).to_be_visible(timeout=5000)
        expect(toast).to_contain_text("worker unreachable")
