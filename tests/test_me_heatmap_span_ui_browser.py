"""Browser test for the Me dashboard's span-driven heatmap window (#871).

The Me page used to always request a fixed 10 years of interaction history
(`days_back=3657`) before shrinking the displayed heatmap to the actual data
afterward — the exact "fetch-then-shrink" flow #871 replaces with a cheap
`/api/crm/me/interactions/span` call made first. This pins that the page
actually does that: it calls the span endpoint, then derives the
`/me/interactions` request's `days_back` from the returned `years` instead of
a hardcoded 10.

Unlike most of the browser suite this serves `web/crm.html` itself from an
ephemeral port rather than pointing at a running API, and stubs every
`/api/crm/**` call the page makes on load — the assertion is about the
network request `web/crm.html`'s own JS makes, not server behavior. That is
why it carries no `requires_server` marker, and so runs at pre-push
(`browser and not requires_server`). Keep it that way — reaching for a live
server here would silently drop this regression from the push gate.
"""
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MY_PERSON_ID = "person-synthetic-me"
SPAN_YEARS = 3  # Arbitrary, distinctive from the old hardcoded default of 10


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves crm.html the way api/main.py does: every CRM route (/me,
    /family, /crm/...) is the same static file."""

    def translate_path(self, path):
        return str(WEB_DIR / "crm.html")

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


# Neutralizes the d3.js CDN dependency (crm.html loads it via <script src>)
# so this test needs no external network access and d3 chart calls elsewhere
# on the page (unrelated to this test's assertions) don't throw. Any property
# access or call on the stub returns the stub itself, so arbitrary chaining
# like d3.select(x).attr(y).style(z) always succeeds.
D3_STUB_JS = """
(function() {
    let stub;
    stub = new Proxy(function() { return stub; }, {
        get(target, prop) {
            if (prop === 'then') return undefined;
            return () => stub;
        }
    });
    window.d3 = stub;
})();
"""

SYNTHETIC_PERSON = {
    "id": MY_PERSON_ID,
    "canonical_name": "Synthetic Owner",
    "display_name": "Synthetic Owner",
    "emails": [],
    "phone_numbers": [],
    "company": None,
    "position": None,
    "linkedin_url": None,
    "category": "self",
    "vault_contexts": [],
    "tags": [],
    "birthday": None,
    "notes": "",
    "sources": [],
    "first_seen": None,
    "last_seen": None,
    "relationship_strength": 0.0,
    "source_entity_count": 0,
    "meeting_count": 0,
    "email_count": 0,
    "mention_count": 0,
    "message_count": 0,
    "slack_message_count": 0,
    "dunbar_circle": -1,
    "source_entities": [],
    "relationships": [],
}

EMPTY_ME_INTERACTIONS = {
    "daily": [],
    "by_source": {},
    "by_month": {},
    "by_circle": {},
    "top_contacts": [],
    "warming": [],
    "cooling": [],
    "total_count": 0,
    "relationship_health_score": 0,
    "health_score_history": [],
    "health_score_average": 0.0,
    "neglected_contacts": [],
    "network_growth": [],
    "messaging_by_circle": [],
    "tracked_relationships": [],
}


def _route_api(page: Page, requests_seen: list):
    """Stub every /api/crm/** call the Me page makes on load, and record each
    request's path + query params so the test can inspect what was asked
    for."""

    def handler(route):
        url = route.request.url
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        requests_seen.append({"path": path, "query": query})

        if path == "/api/crm/config":
            body = {"my_person_id": MY_PERSON_ID}
        elif path == "/api/crm/me/interactions/span":
            body = {
                "earliest": "2019-01-01T00:00:00+00:00",
                "latest": "2026-08-01T00:00:00+00:00",
                "years": SPAN_YEARS,
            }
        elif path == "/api/crm/me/interactions":
            body = EMPTY_ME_INTERACTIONS
        elif path == "/api/crm/me/stats":
            body = {"total_people": 0, "total_emails": 0, "total_meetings": 0, "total_messages": 0}
        elif path == "/api/crm/statistics":
            body = {}
        elif path == "/api/crm/people":
            body = {"people": [], "count": 0, "total": 0, "offset": 0, "has_more": False}
        elif path == f"/api/crm/people/{MY_PERSON_ID}":
            body = SYNTHETIC_PERSON
        elif path == f"/api/crm/people/{MY_PERSON_ID}/timeline/aggregated":
            body = {"days": []}
        else:
            body = {}

        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/crm/**", handler)


def test_me_page_sizes_heatmap_from_span(page: Page, crm_base_url):
    """The Me page's /me/interactions request carries days_back derived from
    /me/interactions/span's `years`, not the old hardcoded 10-year default."""
    requests_seen = []
    page.add_init_script(D3_STUB_JS)
    _route_api(page, requests_seen)

    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    page.goto(f"{crm_base_url}/me")

    # Poll until the span-driven /me/interactions request lands (or time out).
    def _saw_me_interactions():
        return any(r["path"] == "/api/crm/me/interactions" for r in requests_seen)

    for _ in range(100):
        if _saw_me_interactions():
            break
        page.wait_for_timeout(50)

    span_requests = [r for r in requests_seen if r["path"] == "/api/crm/me/interactions/span"]
    me_interactions_requests = [r for r in requests_seen if r["path"] == "/api/crm/me/interactions"]

    assert span_requests, "Me page never called /api/crm/me/interactions/span"
    assert me_interactions_requests, "Me page never called /api/crm/me/interactions"

    expected_days_back = str(SPAN_YEARS * 365 + 7)
    days_back_values = {
        r["query"].get("days_back", [None])[0] for r in me_interactions_requests
    }
    assert days_back_values == {expected_days_back}, (
        f"Expected every /me/interactions request to carry days_back={expected_days_back} "
        f"(derived from span.years={SPAN_YEARS}), got {days_back_values}"
    )

    # Never regress to the old fixed-10-years request (3657 = 10*365 + 7).
    assert "3657" not in days_back_values

    unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
    assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"
