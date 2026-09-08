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
                    # pass by coincidence. An enabled change mirrors the
                    # real store's own recompute: disabling clears the
                    # next fire, re-enabling advances it the same way.
                    if {"schedule_type", "schedule_value", "timezone"} & body.keys():
                        card["next_fire_at"] = "2099-02-02T10:00:00+00:00"
                    elif "enabled" in body:
                        card["next_fire_at"] = "2099-02-02T10:00:00+00:00" if body["enabled"] else None
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
    def test_schedule_type_change_updates_label_and_placeholder_without_saving(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')
        value_label = page.locator('[data-field="schedule-value-label"]')
        expect(type_select).to_have_value("cron")
        expect(value_input).to_have_attribute("placeholder", "0 9 * * *")

        type_select.select_option("once")
        # Checked immediately with get_attribute() (a one-shot read, unlike
        # expect()'s polling) so a regression that only updates the label
        # via a save's own board refetch redraw -- rather than
        # synchronously in the select's own event listener -- is still
        # caught.
        assert value_input.get_attribute("placeholder") == "2026-06-03T15:05:00"
        assert value_label.text_content() == "When (ISO datetime)"
        # A type change alone must never reach the server on its own --
        # the still-cron schedule_value sitting in the field wouldn't
        # parse as a datetime, so writing the type without a matching
        # value would strand the schedule. Pump the event loop briefly so
        # a save the select's own listener wrongly issued would have time
        # to arrive.
        page.wait_for_timeout(300)
        assert schedule_puts == []

    def test_schedule_type_conversion_saves_type_and_value_together(self, page: Page, agents_base_url):
        """The operator can still convert a schedule from cron to once (and
        back): selecting the new type and then entering a value that
        parses under it sends both fields in one write, and the drawer's
        preview reflects the real next fire time the conversion produces."""
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')
        preview = page.locator('[data-field="next-fire-preview"]')
        initial_text = preview.text_content()

        type_select.select_option("once")
        value_input.fill("2026-06-03T15:05:00")
        page.locator('[data-field="timezone"]').click()  # blur
        _wait_for(
            lambda: {"schedule_type": "once", "schedule_value": "2026-06-03T15:05:00"} in schedule_puts,
            page=page,
        )
        # The stub advances next_fire_at to a real, distinct fire time on a
        # successful combined save -- checked by inequality against the
        # captured initial text, the same reasoning
        # test_next_fire_preview_updates_from_put_response documents.
        _wait_for(lambda: preview.text_content() != initial_text, page=page)

    def test_rejected_type_conversion_reverts_both_type_and_value(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')

        type_select.select_option("once")
        value_input.fill("bad expression")
        page.locator('[data-field="timezone"]').click()  # blur
        error_el = page.locator('[data-field="schedule-value-error"]')
        expect(error_el).to_be_visible(timeout=5000)
        expect(value_input).to_have_value("0 9 * * *")
        expect(type_select).to_have_value("cron")

    def test_type_conversion_to_once_updates_the_trigger_button_label(self, page: Page, agents_base_url):
        """Converting a `cron` entry to `once` in the open drawer must
        flip the Trigger now button's disclosure the moment the combined
        type/value save succeeds -- not only on a later reopen -- since
        the drawer holding focus blocks the board's own periodic
        redraw."""
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')
        button = page.locator('[data-action="trigger-now"]')
        expect(button).to_have_text("Trigger now")

        type_select.select_option("once")
        value_input.fill("2026-06-03T15:05:00")
        page.locator('[data-field="timezone"]').click()  # blur
        _wait_for(
            lambda: {"schedule_type": "once", "schedule_value": "2026-06-03T15:05:00"} in schedule_puts,
            page=page,
        )
        expect(button).to_contain_text("disables")

    def test_type_conversion_to_cron_updates_the_trigger_button_label(self, page: Page, agents_base_url):
        """The reverse direction: converting a `once` entry to `cron`
        must drop the one-off disclosure once the save succeeds, rather
        than continuing to warn about consuming a schedule that is now
        recurring."""
        schedule_puts = []
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["schedule_type"] = "once"
        board_state["lanes"]["scheduled"][0]["schedule_value"] = "2099-01-01T09:00:00"
        _open_board(page, agents_base_url, board_state=board_state, schedule_puts=schedule_puts)
        type_select = page.locator('[data-field="schedule-type"]')
        value_input = page.locator('[data-field="schedule-value"]')
        button = page.locator('[data-action="trigger-now"]')
        expect(button).to_contain_text("disables")

        type_select.select_option("cron")
        value_input.fill("0 10 * * *")
        page.locator('[data-field="timezone"]').click()  # blur
        _wait_for(
            lambda: {"schedule_type": "cron", "schedule_value": "0 10 * * *"} in schedule_puts,
            page=page,
        )
        expect(button).to_have_text("Trigger now")

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
        initial_text = preview.text_content()
        assert "2099" in initial_text  # the fixture's original next_fire_at

        page.locator('[data-field="schedule-value"]').fill("0 10 * * *")
        # Blur onto the timezone field, which stays inside the drawer -- so
        # a subsequent board refetch's own redraw is skipped (the drawer
        # still holds focus) and can't stand in for the direct
        # preview-from-response update this waits on.
        page.locator('[data-field="timezone"]').click()
        # The stub advances next_fire_at to 2099-02-02 on a schedule_value
        # save -- checked by inequality against the captured initial text,
        # not by substring, since both the original and the new date
        # contain "2099" and so wouldn't otherwise distinguish "updated"
        # from "never touched".
        _wait_for(lambda: preview.text_content() != initial_text, page=page)


class TestEnabledToggle:
    def test_disabling_refreshes_the_next_fire_preview(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        preview = page.locator('[data-field="next-fire-preview"]')
        initial_text = preview.text_content()
        assert "2099" in initial_text  # the fixture's original next_fire_at

        # Unchecking the box keeps it focused, the same way the drawer's
        # other in-place controls do -- so a subsequent board refetch's
        # own redraw is skipped and can't stand in for the direct
        # preview-from-response update this waits on.
        page.locator('[data-field="enabled"]').uncheck()
        _wait_for(lambda: preview.text_content() == "Not scheduled to fire again.", page=page)


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
        # The swap happens synchronously in the select's own event
        # listener, ahead of the save's network round trip -- checked with
        # a non-auto-waiting is_visible()/is_hidden() query rather than
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

    def test_executor_select_shows_the_empty_default_route_option_when_unset(self, page: Page, agents_base_url):
        """An `agent` schedule with executor:"" means the agent worker's own
        default route, not `local` -- the select must show its own empty
        option selected rather than falling through to `local`, the first
        option in the list, the way a plain <select> does when nothing
        carries the `selected` attribute."""
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["action"] = "agent"
        board_state["lanes"]["scheduled"][0]["executor"] = ""
        _open_board(page, agents_base_url, board_state=board_state)
        executor_select = page.locator('[data-field="executor"]')
        expect(executor_select).to_have_value("")
        values = executor_select.locator("option").evaluate_all("els => els.map(e => e.value)")
        assert values[0] == ""
        assert "local" in values

    def test_bot_select_offers_only_accepted_names(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        bot_select = page.locator('[data-field="bot"]')
        expect(bot_select).to_be_enabled()
        values = bot_select.locator("option").evaluate_all("els => els.map(e => e.value)")
        assert values == ["", "primary", "alerts", "ledger"]
        # The empty option and the registry's own "primary" row must read
        # distinguishably -- both meaning "use the primary bot" isn't a
        # reason for them to look identical in the dropdown.
        labels = bot_select.locator("option").evaluate_all("els => els.map(e => e.textContent)")
        assert labels[0] == "default (primary)"
        assert labels[1] == "primary"
        assert labels[0] != labels[1]

    def test_bot_select_shows_an_orphaned_stored_name_as_unknown(self, page: Page, agents_base_url):
        """A bot name stored on the schedule but absent from the registry
        response (e.g. the bot was renamed) must stay visible and selected,
        flagged unknown, rather than the select falling to a blank
        selection with no signal that the name is unresolvable."""
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["bot"] = "retired-bot"
        _open_board(page, agents_base_url, board_state=board_state)
        bot_select = page.locator('[data-field="bot"]')
        expect(bot_select).to_be_enabled()
        expect(bot_select).to_have_value("retired-bot")
        option = bot_select.locator('option[value="retired-bot"]')
        assert option.text_content() == "retired-bot (unknown)"
        assert option.get_attribute("data-unknown") == "true"

    def test_bot_select_saves(self, page: Page, agents_base_url):
        schedule_puts = []
        _open_board(page, agents_base_url, schedule_puts=schedule_puts)
        page.locator('[data-field="bot"]').select_option("ledger")
        _wait_for(lambda: {"bot": "ledger"} in schedule_puts, page=page)

    def test_bot_registry_fetch_failure_keeps_the_stored_value_and_shows_the_reason(self, page: Page, agents_base_url):
        # The drawer's <select> ships `disabled` in the template, so
        # to_be_disabled() alone would pass even if the failure branch
        # below never ran. A stored, non-empty bot name and asserting it
        # (plus the failure branch's own option list, not the template's
        # empty default) survives the failed fetch proves the branch
        # actually executed.
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["bot"] = "ledger"
        _open_board(page, agents_base_url, board_state=board_state, bots_status=500, bots_response={"detail": "boom"})
        bot_select = page.locator('[data-field="bot"]')
        expect(bot_select).to_be_disabled()
        expect(bot_select).to_have_value("ledger")
        values = bot_select.locator("option").evaluate_all("els => els.map(e => e.value)")
        assert values == ["", "ledger"]
        # A registry-fetch failure doesn't mean the stored name is
        # unresolvable -- only that the registry couldn't be checked -- so
        # the option must read the plain name, not the orphan label the
        # "registry loaded and doesn't list it" case uses below.
        option = bot_select.locator('option[value="ledger"]')
        assert option.text_content() == "ledger"
        assert option.get_attribute("data-unknown") is None
        reason = page.locator('[data-field="bot-reason"]')
        expect(reason).to_be_visible()
        expect(reason).to_contain_text("unavailable")


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

    def test_trigger_now_label_discloses_consumption_for_a_once_schedule(self, page: Page, agents_base_url):
        """Firing a `once` schedule through this button consumes it (marks
        it disabled, clears its next fire) -- the label discloses that
        before the operator clicks, rather than the click being the first
        the operator learns of it."""
        board_state = _board_fixture()
        board_state["lanes"]["scheduled"][0]["schedule_type"] = "once"
        board_state["lanes"]["scheduled"][0]["schedule_value"] = "2099-01-01T09:00:00"
        _open_board(page, agents_base_url, board_state=board_state)
        button = page.locator('[data-action="trigger-now"]')
        expect(button).to_contain_text("disables")

    def test_trigger_now_label_plain_for_a_cron_schedule(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)  # fixture default schedule_type is "cron"
        button = page.locator('[data-action="trigger-now"]')
        expect(button).to_have_text("Trigger now")

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
