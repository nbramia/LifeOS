"""Browser tests for client-side CRM dashboard navigation (#876).

Before this, the Me/Family/Birthdays/Relationship/CRM nav links were plain
anchors: every click was a full document load that re-downloaded the ~750 KB
page, refetched the d3 CDN script, and discarded whatever the previous
dashboard had already fetched. This pins that clicking those links now goes
through `navigateTo()` -> `dispatchRoute()` (a `pushState` plus the same
dispatch the popstate handler already used for person/tab navigation)
instead of letting the browser load the anchor's `href`, that back/forward
still works, that the active nav-link style follows the current view, and
that a dashboard's in-memory caches (in particular `meInteractionsCache`,
via `loadMeInteractions()`) survive a round trip through another dashboard
instead of being silently invalidated by shared UI state (`heatmapYears`)
another dashboard also writes to.

Unlike most of the browser suite this serves `web/crm.html` itself from an
ephemeral port rather than pointing at a running API — every `/api/crm/**`
call the page makes is stubbed, so the assertions are entirely about this
checkout's `web/crm.html` JS. That is why it carries no `requires_server`
marker, and so runs at pre-push (`browser and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic - never a real person id.
MY_PERSON_ID = "person-synthetic-me"

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

SYNTHETIC_ME_PERSON = {
    "id": MY_PERSON_ID,
    "canonical_name": "Synthetic Owner",
    "display_name": "Synthetic Owner",
    "emails": [], "phone_numbers": [], "company": None, "position": None,
    "linkedin_url": None, "category": "self", "vault_contexts": [], "tags": [],
    "birthday": None, "notes": "", "sources": [], "first_seen": None,
    "last_seen": None, "relationship_strength": 0.0, "source_entity_count": 0,
    "meeting_count": 0, "email_count": 0, "mention_count": 0, "message_count": 0,
    "slack_message_count": 0, "dunbar_circle": -1, "source_entities": [],
    "relationships": [],
}

EMPTY_ME_INTERACTIONS = {
    "daily": [], "by_source": {}, "by_month": {}, "by_circle": {},
    "top_contacts": [], "warming": [], "cooling": [], "total_count": 0,
    "relationship_health_score": 0, "health_score_history": [],
    "health_score_average": 0.0, "neglected_contacts": [], "network_growth": [],
    "messaging_by_circle": [], "tracked_relationships": [],
}

EMPTY_PEOPLE_PAGE = {"people": [], "total": 0, "offset": 0, "count": 0}


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves crm.html the way api/main.py does for /me, /family, /crm,
    /birthdays, and /relationship: every non-static path resolves to the
    same file, and the module tree hangs off /static/."""

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
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


def _route_api(page: Page, requests_seen: list):
    """Stub every /api/crm/** call the page makes, and record each request's
    path (query string dropped) so tests can assert on call counts. Anything
    not explicitly modeled below returns `{}` — every loader in crm.html
    treats a missing/empty aggregate defensively (falls back to an empty
    list/dict), so this is enough to exercise all five dashboards without
    error."""

    def handler(route):
        path = urlparse(route.request.url).path
        requests_seen.append(path)

        if path == "/api/crm/config":
            body = {"my_person_id": MY_PERSON_ID}
        elif path == "/api/crm/statistics":
            body = {}
        elif path == "/api/crm/birthdays/today":
            body = {"birthdays": []}
        elif path == "/api/crm/people":
            body = EMPTY_PEOPLE_PAGE
        elif path == f"/api/crm/people/{MY_PERSON_ID}":
            body = SYNTHETIC_ME_PERSON
        elif path == "/api/crm/me/interactions/span":
            body = {"earliest": "2024-01-01T00:00:00+00:00",
                     "latest": "2026-01-01T00:00:00+00:00", "years": 2}
        elif path == "/api/crm/me/interactions":
            body = EMPTY_ME_INTERACTIONS
        elif path == "/api/crm/me/stats":
            body = {"total_people": 0, "total_emails": 0, "total_meetings": 0,
                     "total_messages": 0}
        elif path == "/api/crm/family/members":
            body = {"members": []}
        elif path == "/api/crm/birthdays/all":
            body = {"total_people": 0, "total_dates": 0, "birthdays": []}
        else:
            body = {}

        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/crm/**", handler)


def _prepare(page: Page):
    """Install the d3 stub and API stub before any page script runs, and
    start recording document `load` events (one always fires for the
    initial `page.goto()` - callers should clear the list after the first
    dashboard is up, then assert it stays empty across client-side
    navigation)."""
    page.add_init_script(D3_STUB_JS)
    requests_seen = []
    _route_api(page, requests_seen)
    load_events = []
    page.on("load", lambda: load_events.append(1))
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    return requests_seen, load_events, console_errors


def _goto_me(page: Page, base_url):
    requests_seen, load_events, console_errors = _prepare(page)
    page.goto(f"{base_url}/me")
    expect(page.locator("#meDashboard")).to_be_visible()
    load_events.clear()  # discard the initial document load
    return requests_seen, load_events, console_errors


def _active_pages(page: Page):
    """The set of data-page values currently carrying the `active` class
    among the four dashboard nav links."""
    return page.eval_on_selector_all(
        ".header-right .nav-link.me-link",
        "els => els.filter(e => e.classList.contains('active')).map(e => e.dataset.page)",
    )


class TestNoDocumentReload:
    """Clicking a nav link updates the URL and swaps the dashboard without
    the browser performing a fresh document load."""

    def test_click_through_all_dashboards(self, page: Page, crm_base_url):
        requests_seen, load_events, console_errors = _goto_me(page, crm_base_url)

        assert _active_pages(page) == ["me"]

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"
        assert _active_pages(page) == ["family"]

        page.locator('[data-page="birthdays"]').click()
        expect(page.locator("#birthdaysPage")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/birthdays"
        assert _active_pages(page) == ["birthdays"]

        page.locator('[data-page="relationship"]').click()
        expect(page.locator("#relationshipDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/relationship"
        assert _active_pages(page) == ["relationship"]

        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]

        # The CRM brand link has always redirected to /me on a direct load
        # (init()); clicking it client-side preserves that.
        page.locator('.header-nav a[href="/crm"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]

        assert load_events == [], (
            f"expected zero document loads after the first, got {len(load_events)}"
        )
        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"

    def test_middle_click_is_not_intercepted(self, page: Page, crm_base_url):
        """A modified click (new-tab intent) must be left to the browser,
        not swallowed by navigateTo()'s preventDefault()."""
        _, load_events, _ = _goto_me(page, crm_base_url)

        result = page.evaluate(
            """() => {
                const link = document.querySelector('[data-page="family"]');
                const evt = new MouseEvent('click', { button: 0, ctrlKey: true, cancelable: true });
                const notPrevented = link.dispatchEvent(evt);
                return notPrevented;
            }"""
        )
        # dispatchEvent returns false only if preventDefault() was called.
        assert result is True
        # Since nothing actually followed the href in this synthetic dispatch,
        # the URL/dashboard must be unchanged - proving navigateTo() bailed
        # out before calling pushState()/dispatchRoute().
        assert page.evaluate("window.location.pathname") == "/me"


class TestBackForward:
    """Browser back/forward restores the previous dashboard without a
    document load."""

    def test_back_and_forward_restore_dashboards(self, page: Page, crm_base_url):
        _, load_events, console_errors = _goto_me(page, crm_base_url)

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        page.locator('[data-page="birthdays"]').click()
        expect(page.locator("#birthdaysPage")).to_be_visible()

        page.go_back()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"
        assert _active_pages(page) == ["family"]

        page.go_back()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]

        page.go_forward()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"

        page.go_forward()
        expect(page.locator("#birthdaysPage")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/birthdays"
        assert _active_pages(page) == ["birthdays"]

        assert load_events == [], (
            f"expected zero document loads across back/forward, got {len(load_events)}"
        )
        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestDashboardStatePersists:
    """A Me -> Family -> Me round trip issues no new request for data the
    first Me visit already fetched and cached, even though Family's own
    heatmap window (`heatmapYears`, a global shared with Me's) is 10 years
    while Me's is derived from /me/interactions/span."""

    def test_second_me_visit_does_not_refetch_interactions(self, page: Page, crm_base_url):
        requests_seen, load_events, console_errors = _goto_me(page, crm_base_url)

        def _count(path):
            return len([p for p in requests_seen if p == path])

        # Let the first Me visit's /me/interactions settle.
        for _ in range(100):
            if _count("/api/crm/me/interactions") >= 1:
                break
            page.wait_for_timeout(50)
        assert _count("/api/crm/me/interactions") == 1, (
            "expected exactly one /me/interactions request from the first Me visit, "
            f"got {_count('/api/crm/me/interactions')}"
        )

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        page.wait_for_timeout(200)

        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        # Give any (incorrect) re-fetch time to land before asserting.
        page.wait_for_timeout(300)

        assert _count("/api/crm/me/interactions") == 1, (
            "second Me render must reuse meInteractionsCache instead of "
            f"re-requesting /me/interactions, got {_count('/api/crm/me/interactions')} total calls"
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestDirectLoadsUnchanged:
    """Direct loads (a fresh page.goto(), not a client-side nav) of every
    dashboard URL still render exactly as before, with the correct nav link
    highlighted."""

    @pytest.mark.parametrize("path,element_id,active_page", [
        ("/me", "#meDashboard", "me"),
        ("/family", "#familyDashboard", "family"),
        ("/birthdays", "#birthdaysPage", "birthdays"),
        ("/relationship", "#relationshipDashboard", "relationship"),
    ])
    def test_direct_load_renders_dashboard(self, page: Page, crm_base_url, path, element_id, active_page):
        _, _, console_errors = _prepare(page)
        page.goto(f"{crm_base_url}{path}")
        expect(page.locator(element_id)).to_be_visible()
        assert _active_pages(page) == [active_page]
        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"

    def test_direct_load_of_crm_root_redirects_to_me(self, page: Page, crm_base_url):
        _prepare(page)
        page.goto(f"{crm_base_url}/crm")
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
