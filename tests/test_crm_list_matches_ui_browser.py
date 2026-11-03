"""The person page looks a person up in lists only when asked, and renders rows as text.

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

PEOPLE = {
    name.split()[0].lower(): {
        "id": name.split()[0].lower(), "canonical_name": name, "display_name": name,
        "category": "personal", "dunbar_circle": 3, "relationship_strength": 10, "company": "", "tags": [],
    }
    for name in ("Robin Gamma", "Quillon Example")
}
MARKUP = '<img src=x onerror="window.listRowRan=true">'
ROBIN_MATCHES = {
    "matches": [
        {"list_name": "Donors 2020", "row": f"Robin | {MARKUP}", "match_type": "email"},
        {"list_name": "Prospects", "row": "Robin Gamma | $5", "match_type": "name"},
    ],
    "truncated": True,
}
ENABLED = {"photos_enabled": False, "list_lookup_enabled": True}


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


class CrmPage:
    """The CRM with stubbed API calls, recording every list lookup."""

    def __init__(self, page: Page, base_url: str, config=ENABLED):
        self.page = page
        self.config = config
        self.lookups: list[str] = []
        self.held: dict[str, object] = {}
        self.responses = {"robin": ROBIN_MATCHES, "quillon": {"matches": [], "truncated": False}}
        page.route("**/api/**", self._handle)
        page.goto(f"{base_url}/crm")
        expect(page.locator(".person-card")).to_have_count(2)

    def _handle(self, route):
        url = route.request.url.split("?", 1)[0].rstrip("/")
        if "/list-matches" in url:
            person_id = url.split("/people/")[1].split("/")[0]
            self.lookups.append(person_id)
            if person_id in self.held:
                self.held[person_id] = route
                return
            if self.responses[person_id] is None:
                route.fulfill(status=503, content_type="application/json", body="{}")
                return
            body = self.responses[person_id]
        elif url.endswith("/api/crm/people"):
            body = {"people": list(PEOPLE.values()), "total": 2, "offset": 0, "count": 2}
        elif "/api/crm/people/" in url and url.rsplit("/", 1)[-1] in PEOPLE:
            body = {**PEOPLE[url.rsplit("/", 1)[-1]], "source_entities": [], "source_entity_count": 0}
        elif "/api/crm/config" in url:
            if self.config is None:
                route.fulfill(status=500, content_type="application/json", body="{}")
                return
            body = self.config
        elif "/api/crm/birthdays/today" in url:
            body = {"birthdays": []}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def open_person(self, name):
        self.page.locator(".person-card", has_text=name).click()
        expect(self.page.locator("#detailContent")).to_contain_text(name)

    def check(self):
        self.page.locator("#listMatchesBtn").click()


@pytest.fixture
def crm(page: Page, crm_base_url):
    return CrmPage(page, crm_base_url)


@pytest.mark.parametrize("config", [{"photos_enabled": False, "list_lookup_enabled": False}, None])
def test_the_control_is_hidden_with_no_bulk_folder_or_no_config(page: Page, crm_base_url, config):
    crm = CrmPage(page, crm_base_url, config=config)
    crm.open_person("Robin Gamma")
    expect(page.locator("#listMatchesBtn")).to_be_hidden()


def test_nothing_is_looked_up_until_asked(crm):
    crm.open_person("Robin Gamma")
    expect(crm.page.locator("#listMatchesBtn")).to_be_visible()
    expect(crm.page.locator(".list-match")).to_have_count(0)
    assert crm.lookups == []


def test_rows_show_their_list_and_strength_as_text_never_markup(crm):
    crm.open_person("Robin Gamma")
    crm.check()
    matches = crm.page.locator(".list-match")
    expect(matches).to_have_count(3)
    expect(matches.nth(0)).to_have_text(f"Donors 2020 (Exact email): Robin | {MARKUP}")
    expect(matches.nth(1)).to_have_text("Prospects (Name only, weaker match): Robin Gamma | $5")
    expect(matches.nth(2)).to_contain_text("there may be more")
    expect(crm.page.locator("#listMatchesContent img")).to_have_count(0)
    assert crm.page.evaluate("window.listRowRan") is None
    assert crm.lookups == ["robin"]


def test_no_match_and_failure_say_so(crm):
    crm.open_person("Quillon Example")
    crm.check()
    expect(crm.page.locator("#listMatchesContent")).to_have_text("Not found on any list.")
    crm.responses["quillon"] = None
    crm.check()
    expect(crm.page.locator("#listMatchesContent")).to_have_text("Could not check lists.")


def test_moving_to_another_person_clears_the_rows(crm):
    crm.open_person("Robin Gamma")
    crm.check()
    expect(crm.page.locator(".list-match")).to_have_count(3)
    crm.open_person("Quillon Example")
    expect(crm.page.locator(".list-match")).to_have_count(0)


def test_a_late_answer_never_lands_on_another_persons_page(crm):
    crm.held["robin"] = None
    crm.open_person("Robin Gamma")
    crm.check()
    expect(crm.page.locator("#listMatchesContent")).to_have_text("Checking lists...")
    crm.open_person("Quillon Example")
    expect(crm.page.locator(".list-match")).to_have_count(0)

    crm.held["robin"].fulfill(status=200, content_type="application/json", body=json.dumps(ROBIN_MATCHES))
    crm.page.wait_for_timeout(200)

    expect(crm.page.locator(".list-match")).to_have_count(0)
