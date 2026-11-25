"""CRM renders contact fields (email, name, tags) as text, never as markup.

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

HOSTILE_EMAIL = '"<img src=x onerror=window.__fired=1>//"@sample.invalid'
HOSTILE_NAME = '<img src=x onerror=window.__fired=1>Sample\' "Person'
HOSTILE_TAG = "');window.__fired=1;('\"><svg onload=window.__fired=1>"

HOSTILE = {
    "id": "hostile", "canonical_name": HOSTILE_NAME, "display_name": HOSTILE_NAME,
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 5, "company": "",
    "emails": [HOSTILE_EMAIL], "phone_numbers": ["+12125550100"], "tags": [HOSTILE_TAG],
}
NORMAL = {
    "id": "normal", "canonical_name": "Robin Gamma", "display_name": "Robin Gamma",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 5, "company": "",
    "emails": ["robin@sample.invalid"], "phone_numbers": ["+12125550101"], "tags": ["friend"],
}
PEOPLE = {p["id"]: p for p in (HOSTILE, NORMAL)}


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


def _open_person(page: Page, base_url: str, person_id: str) -> None:
    def handler(route):
        url = route.request.url.split("?", 1)[0].rstrip("/")
        if url.endswith("/api/crm/people"):
            body = {"people": list(PEOPLE.values()), "total": len(PEOPLE), "offset": 0,
                    "count": len(PEOPLE)}
        elif url.rsplit("/", 1)[0].endswith("/api/crm/people") and url.rsplit("/", 1)[1] in PEOPLE:
            body = PEOPLE[url.rsplit("/", 1)[1]]
        elif "/api/crm/config" in url:
            body = {"photos_enabled": False}
        elif "/api/crm/birthdays/today" in url:
            body = {"birthdays": []}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.route("**/api/**", handler)
    page.goto(f"{base_url}/me")
    page.wait_for_selector("#searchInput")
    expect(page.locator(".person-card")).to_have_count(len(PEOPLE))
    page.evaluate("window.__fired = 0")
    page.evaluate(f"selectPerson('{person_id}')")
    expect(page.locator("#detailTagsChips .detail-tag-chip")).to_have_count(1)
    page._dialogs = dialogs


def _assert_inert(page: Page) -> None:
    page.wait_for_timeout(300)
    assert page.evaluate("window.__fired") == 0
    assert page._dialogs == []
    assert page.locator("img[src='x'], svg[onload]").count() == 0


def test_hostile_contact_fields_render_as_text(page: Page, crm_base_url):
    _open_person(page, crm_base_url, "hostile")
    _assert_inert(page)
    assert HOSTILE_EMAIL in page.locator("#detailContactQuick").inner_text()
    assert HOSTILE_NAME in page.locator("#detailName").inner_text()
    assert HOSTILE_TAG in page.locator("#detailTagsChips").inner_text()
    assert HOSTILE_EMAIL in page.locator("#contactInfoCompact").inner_text()


def test_tag_removal_receives_the_exact_tag_text(page: Page, crm_base_url):
    _open_person(page, crm_base_url, "hostile")
    page.evaluate("window.removeTag = (tag) => { window.__removed = tag; }")
    page.locator("#detailTagsChips .remove-tag").click()
    assert page.evaluate("window.__removed") == HOSTILE_TAG
    _assert_inert(page)


def test_mobile_details_render_contact_fields_as_text(page: Page, crm_base_url):
    _open_person(page, crm_base_url, "normal")
    page.evaluate("(p) => updateMobileDetails(p)", HOSTILE)
    _assert_inert(page)
    assert page.locator("img[src='x']").count() == 0


def test_normal_address_keeps_a_working_mailto_link(page: Page, crm_base_url):
    _open_person(page, crm_base_url, "normal")
    link = page.locator("#detailContactQuick a[href^='mailto:']")
    expect(link).to_have_attribute("href", "mailto:robin@sample.invalid")
    assert "robin@sample.invalid" in link.inner_text()
    tel = page.locator("#detailContactQuick a[href^='tel:']")
    expect(tel).to_have_attribute("href", "tel:+12125550101")
