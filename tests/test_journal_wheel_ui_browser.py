"""Browser test for the /journal emotion-wheel view (#212).

Serves `web/` itself from an ephemeral port and stubs every `/api/journal/`
call, so this runs without a live server — same pattern as
test_voice_mic_block_ui_browser.py. Carries no `requires_server` marker, so
it's part of the pre-push gate (`browser and not requires_server`).

All stubbed data below is invented — no real journal content.
"""
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _JournalHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the journal page the way api/main.py does: `/journal` is
    journal.html, everything else hangs off `/static/` or the web root."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/journal", "/"):
            return str(WEB_DIR / "journal.html")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def journal_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JournalHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


_NONEMPTY_RESPONSE = {
    "window": "all-time",
    "start_date": None,
    "end_date": "2026-06-30",
    "entry_count": 4,
    "wheel": [
        {
            "value": "Happy",
            "count": 3,
            "children": [
                {"value": "Cozy", "count": 2, "children": []},
                {"value": "Giddy", "count": 1, "children": []},
            ],
        },
        {"value": "Bad", "count": 1, "children": [{"value": "Wobbly", "count": 1, "children": []}]},
    ],
}

_EMPTY_RESPONSE = {
    "window": "day",
    "start_date": "2026-06-30",
    "end_date": "2026-06-30",
    "entry_count": 0,
    "wheel": [],
}


def _open_journal(page: Page, base_url, *, response_by_window=None, default=_NONEMPTY_RESPONSE):
    response_by_window = response_by_window or {}

    def handler(route):
        url = urlparse(route.request.url)
        if "/api/journal/emotions" in url.path:
            window = parse_qs(url.query).get("window", ["all-time"])[0]
            body = response_by_window.get(window, default)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/journal")
    page.wait_for_selector("#sampleBanner")


class TestJournalWheelView:
    def test_default_window_renders_sample_size_and_legend(self, page: Page, journal_base_url):
        _open_journal(page, journal_base_url)
        expect(page.locator("#sampleBanner")).to_contain_text("4")
        expect(page.locator("#sampleBanner")).to_contain_text("journal entries")
        # One legend row per top-level emotion.
        expect(page.locator(".legend-row")).to_have_count(2)
        expect(page.locator(".legend-row").first).to_contain_text("Happy")
        # Wheel wedges rendered as SVG paths, at least one per tree node.
        expect(page.locator("#wheelSvg path")).to_have_count(5)

    def test_thin_sample_gets_a_visible_caveat(self, page: Page, journal_base_url):
        thin_response = {**_NONEMPTY_RESPONSE, "entry_count": 2, "wheel": [
            {"value": "Sad", "count": 2, "children": []},
        ]}
        _open_journal(page, journal_base_url, default=thin_response)
        expect(page.locator("#sampleBanner")).to_contain_text("small sample")

    def test_empty_window_shows_no_entries_message_and_no_wheel(self, page: Page, journal_base_url):
        _open_journal(page, journal_base_url, default=_EMPTY_RESPONSE)
        expect(page.locator("#sampleBanner")).to_contain_text("No journal entries")
        expect(page.locator("#wheelSvg path")).to_have_count(0)
        expect(page.locator(".legend-row")).to_have_count(0)

    def test_window_pill_click_refetches_and_updates_banner(self, page: Page, journal_base_url):
        by_window = {
            "all-time": _NONEMPTY_RESPONSE,
            "day": _EMPTY_RESPONSE,
        }
        _open_journal(page, journal_base_url, response_by_window=by_window)
        expect(page.locator("#sampleBanner")).to_contain_text("4")

        page.locator('.window-pill[data-window="day"]').click()
        expect(page.locator("#sampleBanner")).to_contain_text("No journal entries")
        expect(page.locator('.window-pill[data-window="day"]')).to_have_class("window-pill active")
        expect(page.locator('.window-pill[data-window="all-time"]')).not_to_have_class(
            "window-pill active")
