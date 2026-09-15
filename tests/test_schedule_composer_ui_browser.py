"""Browser test for the Scheduled column's "+" and its create-schedule
composer.

Serves `web/` itself from an ephemeral port and stubs every `/api/` call the
page makes — the assertions are about the JS in `web/agents/board.js`, not
the live backend. No `requires_server` marker, so this runs at pre-push
(`browser and not requires_server`).

Covers: the "+" button appears only on the Scheduled lane and opens the
schedule composer (other lanes keep the ordinary new-card composer); each
trigger mode's generated cron expression, including custom days; switching
a generated mode to Cron prefilling the cron input; the live preview list
rendered from a mocked `POST /api/scheduler/preview` response and its error
path; the `POST /api/scheduler` payload for an endpoint action; a server 422
on create rendered at the relevant field with the composer staying open; and
a successful create closing the composer and revealing the new card.
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


def _board_fixture():
    return {
        "lanes": {
            "unassigned": [], "assigned": [], "in_progress": [], "human_queue": [],
            "scheduled": [], "review": [], "done": [],
        },
        "generated_at": 0,
        "api_host": "primary-host",
    }


def _default_created_card(body):
    """A synthetic `POST /api/scheduler` success response, shaped like
    `renderScheduleCard` (web/agents/board.js) needs to render it."""
    return {
        "id": "new1", "kind": "schedule",
        "name": body.get("name", ""),
        "enabled": body.get("enabled", True),
        "next_fire_at": "2099-01-01T09:00:00+00:00",
        "recurring": body.get("schedule_type") == "cron",
        "last_run": None,
        "schedule_type": body.get("schedule_type"),
        "schedule_value": body.get("schedule_value"),
        "timezone": body.get("timezone", ""),
        "action": body.get("action", "notify"),
        "executor": body.get("executor", ""),
        "bot": body.get("bot", ""),
        "message_content": body.get("message_content", ""),
        "endpoint_config": body.get("endpoint_config"),
    }


def _stub_routes(page: Page, board_state: dict, preview_calls: list, create_calls: list,
                  preview_response=None, preview_status: int = 200,
                  create_response=None, create_status: int = 200):
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
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"bots": ["primary", "alerts"]}))
            return

        if re.search(r"/api/scheduler/preview$", url) and method == "POST":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            preview_calls.append(body)
            payload = preview_response if preview_response is not None else {"next": []}
            route.fulfill(status=preview_status, content_type="application/json", body=json.dumps(payload))
            return

        if re.search(r"/api/scheduler$", url) and method == "POST":
            try:
                body = json.loads(route.request.post_data or "{}")
            except ValueError:
                body = {}
            create_calls.append(body)
            if create_status != 200:
                payload = create_response if create_response is not None else {"detail": "rejected"}
                route.fulfill(status=create_status, content_type="application/json", body=json.dumps(payload))
                return
            new_card = create_response if create_response is not None else _default_created_card(body)
            board_state["lanes"]["scheduled"].append(new_card)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(new_card))
            return

        if re.search(r"/api/agents/board$", url) and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(board_state))
            return

        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", api_handler)


def _open_board(page: Page, base_url, board_state=None, preview_calls=None, create_calls=None,
                 preview_response=None, preview_status=200, create_response=None, create_status=200):
    _stub_routes(
        page,
        board_state if board_state is not None else _board_fixture(),
        preview_calls if preview_calls is not None else [],
        create_calls if create_calls is not None else [],
        preview_response, preview_status, create_response, create_status,
    )
    page.goto(f"{base_url}/agents")
    page.wait_for_selector('.board-lane[data-lane="scheduled"]')


def _open_composer(page: Page):
    page.locator('.board-lane[data-lane="scheduled"] .board-lane-add').click()
    page.wait_for_selector('.modal [data-field="trigger-mode"]')
    return page.locator('.modal')


def _wait_for(predicate, page: Page, timeout_ms=5000, interval_ms=25):
    """Poll `predicate` until it's truthy or the timeout elapses — for
    asserting on a plain Python side effect (e.g. an appended stub call)
    that has no DOM signal Playwright's own `expect(...)` can wait on."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(interval_ms)
    assert predicate(), f"condition not met within {timeout_ms}ms"


class TestAddButton:
    def test_scheduled_lane_add_opens_schedule_composer(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url, preview_response={"next": []})
        modal = _open_composer(page)
        expect(modal.locator("h2")).to_have_text("New schedule")
        expect(modal.locator('[data-field="action"]')).to_be_visible()

    def test_other_lane_add_still_opens_the_ordinary_task_composer(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url)
        page.locator('.board-lane[data-lane="unassigned"] .board-lane-add').click()
        modal = page.locator(".modal")
        expect(modal.locator("h2")).to_have_text("New card")
        expect(modal.locator('[data-field="trigger-mode"]')).to_have_count(0)


class TestTriggerBuilder:
    def test_daily_mode_generates_daily_cron(self, page: Page, agents_base_url):
        preview_calls = []
        _open_board(page, agents_base_url, preview_calls=preview_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="trigger-time"]').fill("09:05")
        _wait_for(lambda: any(c.get("schedule_value") == "5 9 * * *" for c in preview_calls), page)

    def test_weekdays_mode_generates_weekday_cron(self, page: Page, agents_base_url):
        preview_calls = []
        _open_board(page, agents_base_url, preview_calls=preview_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="trigger-mode"]').select_option("weekdays")
        modal.locator('[data-field="trigger-time"]').fill("08:00")
        _wait_for(lambda: any(c.get("schedule_value") == "0 8 * * 1-5" for c in preview_calls), page)

    def test_custom_days_mode_generates_expected_cron(self, page: Page, agents_base_url):
        """Sun-Thu 21:45 -> `45 21 * * 0,1,2,3,4` (0 = Sun)."""
        preview_calls = []
        _open_board(page, agents_base_url, preview_calls=preview_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="trigger-mode"]').select_option("custom")
        for day in ("0", "1", "2", "3", "4"):
            modal.locator(f'[data-field="trigger-day"][value="{day}"]').check()
        modal.locator('[data-field="trigger-time"]').fill("21:45")
        _wait_for(lambda: any(c.get("schedule_value") == "45 21 * * 0,1,2,3,4" for c in preview_calls), page)

    def test_once_mode_produces_once_type_and_local_iso_value(self, page: Page, agents_base_url):
        preview_calls = []
        _open_board(page, agents_base_url, preview_calls=preview_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="trigger-mode"]').select_option("once")
        modal.locator('[data-field="trigger-once"]').fill("2099-06-03T15:05")
        _wait_for(
            lambda: any(
                c.get("schedule_type") == "once" and c.get("schedule_value") == "2099-06-03T15:05"
                for c in preview_calls
            ),
            page,
        )

    def test_switching_from_a_generated_mode_to_cron_prefills_the_generated_expression(
        self, page: Page, agents_base_url,
    ):
        _open_board(page, agents_base_url, preview_response={"next": []})
        modal = _open_composer(page)
        # Daily is the default mode, already carrying its own default time.
        modal.locator('[data-field="trigger-mode"]').select_option("cron")
        expect(modal.locator('[data-field="trigger-cron"]')).to_have_value("0 9 * * *")


class TestPreview:
    def test_preview_list_renders_from_mocked_response(self, page: Page, agents_base_url):
        preview_calls = []
        _open_board(
            page, agents_base_url, preview_calls=preview_calls,
            preview_response={"next": ["2099-01-01T14:00:00+00:00", "2099-01-02T14:00:00+00:00"]},
        )
        modal = _open_composer(page)
        _wait_for(lambda: len(preview_calls) > 0, page)
        entries = modal.locator('[data-field="preview-list"] .drawer-schedule-info')
        expect(entries).to_have_count(2)

    def test_preview_error_shows_the_api_detail(self, page: Page, agents_base_url):
        _open_board(
            page, agents_base_url,
            preview_response={"detail": "Invalid cron expression 'x': bad"}, preview_status=422,
        )
        modal = _open_composer(page)
        error_el = modal.locator('[data-field="preview-error"]')
        expect(error_el).to_be_visible(timeout=5000)
        expect(error_el).to_have_text("Invalid cron expression 'x': bad")


class TestCreate:
    def test_create_payload_for_an_endpoint_schedule(self, page: Page, agents_base_url):
        create_calls = []
        _open_board(page, agents_base_url, create_calls=create_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="name"]').fill("Ping health")
        modal.locator('[data-field="action"]').select_option("endpoint")
        modal.locator('[data-field="endpoint-method"]').select_option("POST")
        modal.locator('[data-field="endpoint-path"]').fill("/api/health")
        modal.locator('[data-field="endpoint-params"]').fill('{"foo": "bar"}')
        create_btn = modal.locator("#new-schedule-create")
        expect(create_btn).to_be_enabled()
        create_btn.click()
        _wait_for(lambda: len(create_calls) == 1, page)
        body = create_calls[0]
        assert body["name"] == "Ping health"
        assert body["action"] == "endpoint"
        assert body["schedule_type"] == "cron"
        assert body["schedule_value"] == "0 9 * * *"
        assert body["endpoint_config"] == {"method": "POST", "endpoint": "/api/health", "params": {"foo": "bar"}}

    def test_create_disabled_until_name_and_trigger_are_complete(self, page: Page, agents_base_url):
        _open_board(page, agents_base_url, preview_response={"next": []})
        modal = _open_composer(page)
        create_btn = modal.locator("#new-schedule-create")
        # Daily mode already has a default time, but Name is still blank.
        expect(create_btn).to_be_disabled()
        modal.locator('[data-field="name"]').fill("Morning ping")
        expect(create_btn).to_be_enabled()
        # Switching to Cron with no expression yet makes the trigger
        # incomplete again.
        modal.locator('[data-field="trigger-mode"]').select_option("cron")
        modal.locator('[data-field="trigger-cron"]').fill("")
        expect(create_btn).to_be_disabled()

    def test_server_422_on_create_is_rendered_at_the_field(self, page: Page, agents_base_url):
        create_calls = []
        _open_board(
            page, agents_base_url, create_calls=create_calls, preview_response={"next": []},
            create_response={"detail": "endpoint_config.endpoint must start with '/api/', got 'bad'"},
            create_status=422,
        )
        modal = _open_composer(page)
        modal.locator('[data-field="name"]').fill("Bad endpoint")
        modal.locator('[data-field="action"]').select_option("endpoint")
        modal.locator('[data-field="endpoint-method"]').select_option("GET")
        modal.locator('[data-field="endpoint-path"]').fill("bad")
        modal.locator("#new-schedule-create").click()
        error_el = modal.locator('[data-field="endpoint-params-error"]')
        expect(error_el).to_have_text("endpoint_config.endpoint must start with '/api/', got 'bad'", timeout=5000)
        # The composer stays open with Create re-enabled for a retry.
        expect(modal).to_be_visible()
        expect(modal.locator("#new-schedule-create")).to_be_enabled()
        assert len(create_calls) == 1

    def test_success_closes_the_composer_and_reveals_the_new_card(self, page: Page, agents_base_url):
        create_calls = []
        _open_board(page, agents_base_url, create_calls=create_calls, preview_response={"next": []})
        modal = _open_composer(page)
        modal.locator('[data-field="name"]').fill("Morning ping")
        modal.locator('[data-field="message-content"]').fill("Good morning")
        modal.locator("#new-schedule-create").click()
        expect(page.locator(".modal-backdrop")).to_have_count(0, timeout=5000)
        new_card = page.locator('[data-card-id="new1"]')
        expect(new_card).to_be_visible(timeout=5000)
        expect(new_card).to_have_class(re.compile(r"reveal-highlight"))
        assert len(create_calls) == 1
