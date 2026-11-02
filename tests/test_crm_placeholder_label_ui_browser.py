"""A person named only after a phone or address shows the name that came with it.

Serves `web/` from an ephemeral port with every `/api/**` call stubbed, so it
carries no `requires_server` marker.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

PHONE_ONLY = {
    "id": "phone-only", "canonical_name": "+12125550173", "display_name": "Quillon",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 10, "company": "", "tags": [],
}
FULL_NAME = {
    "id": "full-name", "canonical_name": "Robin Gamma", "display_name": "Robin (Sample Labs)",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 8, "company": "", "tags": [],
}
NUMBER_ONLY = {
    "id": "number-only", "canonical_name": "+12125550174", "display_name": "+12125550174",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 6, "company": "", "tags": [],
}
PEOPLE = [PHONE_ONLY, FULL_NAME, NUMBER_ONLY]


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / "crm.html")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def crm_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CrmHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _open_people(page: Page, base_url: str) -> None:
    def handler(route):
        url = route.request.url
        if "/api/crm/people?" in url or url.rstrip("/").endswith("/api/crm/people"):
            body = {"people": PEOPLE, "total": len(PEOPLE), "offset": 0, "count": len(PEOPLE)}
        elif "/api/crm/config" in url:
            body = {"photos_enabled": False}
        elif "/api/crm/birthdays/today" in url:
            body = {"birthdays": []}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{base_url}/me")
    page.wait_for_selector("#searchInput")
    expect(page.locator(".person-card")).to_have_count(len(PEOPLE))


def _names(page: Page) -> list[str]:
    return page.locator(".person-name").all_inner_texts()


def test_phone_placeholder_shows_its_word_in_the_people_list(page: Page, crm_base_url):
    _open_people(page, crm_base_url)
    assert "Quillon" in _names(page)
    assert PHONE_ONLY["canonical_name"] not in _names(page)


def test_a_real_name_is_shown_as_it_is(page: Page, crm_base_url):
    _open_people(page, crm_base_url)
    assert "Robin Gamma" in _names(page)
    assert "Robin (Sample Labs)" not in _names(page)


def test_a_number_with_no_word_stays_a_number(page: Page, crm_base_url):
    _open_people(page, crm_base_url)
    assert NUMBER_ONLY["canonical_name"] in _names(page)
