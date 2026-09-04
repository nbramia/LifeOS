"""Browser test for the Relationship page's no-partner-configured guard (#891).

Before this fix, opening /relationship with no `partner_person_id` in the CRM
config fired requests with an empty id segment (`/api/crm/people//timeline`)
and left the tone/photo/timeline sections to fail silently. The fix checks
`partner_person_id` before issuing any partner-scoped request and renders an
empty state instead.

Like tests/test_voice_mic_block_ui_browser.py, this serves `web/` itself from
an ephemeral port and stubs every `/api/**` call, so it carries no
`requires_server` marker and runs at pre-push (`browser and not
requires_server`) instead of needing a live API + real data.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Obviously synthetic partner id -- never a real PersonEntity id.
SYNTHETIC_PARTNER_ID = "synthetic-partner-id-891"


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves crm.html at /relationship the way api/main.py's route does."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/relationship", "/relationship/", "/"):
            return str(WEB_DIR / "crm.html")
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
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


def _open_relationship_page(page: Page, crm_base_url: str, *, partner_person_id: str):
    """Load /relationship, stubbing /api/crm/config with the given partner id
    (empty string == not configured) and every other /api/** call with an
    empty-but-valid JSON body. Returns the list of request URLs observed."""
    requests: list[str] = []
    console_errors: list[str] = []

    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    def handler(route):
        requests.append(route.request.url)
        if "/api/crm/config" in route.request.url:
            body = {
                "my_person_id": "synthetic-me-id",
                "partner_person_id": partner_person_id,
                "partner_name": "Synthetic Partner" if partner_person_id else "",
            }
        elif "/timeline" in route.request.url:
            body = {"items": []}
        elif "/people" in route.request.url:
            body = {"people": [], "total": 0, "offset": 0, "count": 0}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(f"{crm_base_url}/relationship")
    page.wait_for_timeout(500)  # let the async init()/showRelationshipDashboard() settle
    return requests, console_errors


class TestNoPartnerConfigured:
    def test_no_request_has_an_empty_id_segment(self, page: Page, crm_base_url):
        requests, _ = _open_relationship_page(page, crm_base_url, partner_person_id="")

        # The specific regression: a timeline request with a missing id segment.
        assert not any("/people//timeline" in url for url in requests), (
            f"found a timeline request with an empty id segment: {requests}"
        )

    def test_no_partner_scoped_requests_fire_at_all(self, page: Page, crm_base_url):
        requests, _ = _open_relationship_page(page, crm_base_url, partner_person_id="")

        assert not any("/relationship/insights" in url for url in requests)
        assert not any("/relationship/tone-analysis-detailed" in url for url in requests)
        assert not any("/timeline" in url for url in requests)
        assert not any("/photos/profile/" in url for url in requests)

    def test_no_console_errors(self, page: Page, crm_base_url):
        _, console_errors = _open_relationship_page(page, crm_base_url, partner_person_id="")
        assert console_errors == [], f"unexpected console errors: {console_errors}"

    def test_renders_empty_state(self, page: Page, crm_base_url):
        _open_relationship_page(page, crm_base_url, partner_person_id="")
        empty_state = page.locator("#relationshipEmptyState")
        expect(empty_state).to_be_visible()
        expect(empty_state).to_contain_text("No partner configured")
        expect(page.locator("#relationshipDashboard")).to_be_hidden()


class TestPartnerConfigured:
    def test_partner_scoped_requests_still_fire(self, page: Page, crm_base_url):
        requests, console_errors = _open_relationship_page(
            page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID,
        )

        assert any("/relationship/insights" in url for url in requests)
        assert any("/relationship/tone-analysis-detailed" in url for url in requests)
        assert any(f"/people/{SYNTHETIC_PARTNER_ID}/timeline" in url for url in requests)
        assert console_errors == [], f"unexpected console errors: {console_errors}"

    def test_dashboard_renders_not_empty_state(self, page: Page, crm_base_url):
        _open_relationship_page(page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID)
        expect(page.locator("#relationshipDashboard")).to_be_visible()
        expect(page.locator("#relationshipEmptyState")).to_be_hidden()
