"""Browser test for the /agents host filter + side panel fields (#849).

Serves `web/` itself on an ephemeral port and stubs every `/api/` call, the
same server-free pattern as `tests/test_voice_mic_block_ui_browser.py` —
the assertions are about the JS in *this* checkout, and `/api/agents/snapshot`
is the only response that matters, so a live LifeOS API isn't needed. That is
why this carries no `requires_server` marker and runs at pre-push
(`browser and not requires_server`).

`/agents` loads d3 from a CDN (`https://d3js.org/d3.v7.min.js`) — only
`/api/**` is intercepted here (same as the voice test), so that request goes
to the real network like it does for every other browser test against this
page family.
"""
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
        if path == "/agents":
            return str(WEB_DIR / "agents.html")
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


# Two sessions on two different hosts — one carries branch + prompt_preview
# (a cli_sessions row merged onto/registered without a local transcript),
# the other is a bare local session so the "single host" collapse case
# (host filter hidden) isn't accidentally exercised by every test.
SNAPSHOT = {
    "sessions": [
        {
            "session_id": "cc:host-filter-target",
            "task_id": "t1",
            "status": "running",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "laptop-a",
            "branch": "feat/synthetic-branch",
            "prompt_preview": "refactor the synthetic widget",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "hosttest-alpha-session",
            "model_label": "Sonnet",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
        {
            "session_id": "sess_worker_on_apihost",
            "task_id": "t2",
            "status": "running",
            "status_inferred": False,
            "routing": "local",
            "source": "lifeos_agent",
            "host": "api-host",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 5,
            "total_output_tokens": 5,
            "total_dollars": 0.0,
            "spawn_depth": 0,
            "label": "hosttest-beta-session",
            "model_label": "Local",
            "decoded_cwd": "/home/synthetic/proj-b",
        },
        # A cli_sessions row whose SessionEnd event already fired — must be
        # treated as terminal (hidden by default, shown with "include
        # finished" on) rather than staying in the live view forever (#849
        # round-1 finding: 'ended' was missing from the frontend TERMINAL set).
        {
            "session_id": "cc:host-filter-ended",
            "task_id": "t3",
            "status": "ended",
            "status_inferred": False,
            "routing": "claude_code",
            "source": "claude_code",
            "host": "laptop-a",
            "branch": "feat/synthetic-branch",
            "prompt_preview": "wrap up the synthetic widget",
            "started_at": 1000,
            "last_activity_at": 2000,
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_dollars": 0.01,
            "spawn_depth": 0,
            "label": "hosttest-gamma-session",
            "model_label": "Sonnet",
            "decoded_cwd": "/home/synthetic/proj-a",
        },
    ],
    "edges": [],
    "generated_at": 1234567890,
}


def _open_agents(page: Page, base_url):
    def handler(route):
        if "/api/agents/snapshot" in route.request.url:
            body = SNAPSHOT
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/agents")
    page.wait_for_selector("#filter-host")


class TestHostFilter:
    def test_filter_host_lists_every_host_from_the_snapshot(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        select = page.locator("#filter-host")
        options = select.locator("option").all_inner_texts()
        assert "all" in options
        assert "laptop-a" in options
        assert "api-host" in options

    def test_selecting_a_host_hides_sessions_from_other_hosts(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        # Both sessions are recent enough to be visible under the default
        # 30-minute recency filter only if last_activity_at is "now" — the
        # fixture's timestamps are fixed epoch seconds in the past, so
        # widen recency to "all time" first.
        page.select_option("#filter-recency", "all")
        page.select_option("#filter-host", "laptop-a")
        page.wait_for_timeout(400)  # let the filtered re-render settle
        nodes = page.locator(".node")
        expect(nodes).to_have_count(1)


class TestEndedStatus:
    # The fixture has 3 sessions total: two 'running' and one 'ended'
    # (cc:host-filter-ended). 'ended' must be treated as terminal — hidden
    # by default and only shown once "include finished" is checked (#849
    # round-1 finding: 'ended' was missing from the frontend TERMINAL set,
    # so a closed CLI session stayed in the live view indefinitely).
    def test_ended_session_hidden_by_default(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(2)

    def test_ended_session_shown_when_include_finished_is_checked(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#filter-terminal").check()
        page.wait_for_timeout(400)
        expect(page.locator(".node")).to_have_count(3)


class TestPanelFields:
    def test_panel_shows_host_branch_and_prompt_preview(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#search-input").fill("hosttest-alpha-session")
        result = page.locator(".search-result", has_text="hosttest-alpha-session")
        expect(result).to_be_visible()
        result.click()

        panel = page.locator("#panel")
        expect(panel.locator('[data-field="host"]')).to_have_text("laptop-a")
        expect(panel.locator('[data-field="branch-hint"]')).to_contain_text("feat/synthetic-branch")
        expect(panel.locator('[data-field="prompt-preview-hint"]')).to_contain_text(
            "refactor the synthetic widget")

    def test_panel_omits_branch_and_prompt_fields_when_absent(self, page: Page, agents_base_url):
        _open_agents(page, agents_base_url)
        page.select_option("#filter-recency", "all")
        page.locator("#search-input").fill("hosttest-beta-session")
        result = page.locator(".search-result", has_text="hosttest-beta-session")
        expect(result).to_be_visible()
        result.click()

        panel = page.locator("#panel")
        expect(panel.locator('[data-field="host"]')).to_have_text("api-host")
        expect(panel.locator('[data-field="branch-hint"]')).to_have_count(0)
        expect(panel.locator('[data-field="prompt-preview-hint"]')).to_have_count(0)
