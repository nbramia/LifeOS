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
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

PARTNER_PERSON_ID = "person-synthetic-partner"

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

# A second, non-owner person -- used by the /birthdays-entry test to prove
# the sidebar/stats actually populate rather than being stuck on their
# loading state (#909 review finding 3).
SYNTHETIC_OTHER_PERSON = {
    "id": "person-synthetic-other",
    "canonical_name": "Synthetic Friend",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 5,
    "company": "", "tags": [],
}

EMPTY_ME_INTERACTIONS = {
    "daily": [], "by_source": {}, "by_month": {}, "by_circle": {},
    "top_contacts": [], "warming": [], "cooling": [], "total_count": 0,
    "relationship_health_score": 0, "health_score_history": [],
    "health_score_average": 0.0, "neglected_contacts": [], "network_growth": [],
    "messaging_by_circle": [], "tracked_relationships": [],
}

# The four dashboard containers that must never be visible at the same time
# -- a leak here is the class of bug #909 review finding 2 caught
# (Relationship left rendered underneath Family).
_DASHBOARD_IDS = ["#meDashboard", "#familyDashboard", "#relationshipDashboard", "#birthdaysPage"]


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


def _route_api(page: Page, requests_seen: list, partner_configured: bool = True):
    """Stub every /api/crm/** call the page makes, and record each request's
    path and query params (`{"path": ..., "query": ...}`, `query` from
    `parse_qs`) so tests can assert on call counts and on the parameters a
    request carried. Anything not explicitly modeled below returns `{}` —
    every loader in crm.html treats a missing/empty aggregate defensively
    (falls back to an empty list/dict), so this is enough to exercise all
    five dashboards without error.

    `partner_configured=False` models a fresh install (no
    `partner_person_id` set) -- the default is `True` so most tests exercise
    the real Relationship dashboard rather than its no-partner empty state;
    `TestRelationshipNoPartnerChrome` below explicitly wants the empty-state
    path (#909 review follow-up finding 1: a fresh install is not a corner
    case)."""

    def handler(route):
        parsed = urlparse(route.request.url)
        path = parsed.path
        requests_seen.append({"path": path, "query": parse_qs(parsed.query)})

        if path == "/api/crm/config":
            # partner_person_id set (by default) so showRelationshipDashboard()
            # renders the real dashboard rather than the no-partner empty
            # state (#899) -- keeps the Relationship hops representative of
            # a configured install, not the degenerate case.
            body = {"my_person_id": MY_PERSON_ID}
            if partner_configured:
                body["partner_person_id"] = PARTNER_PERSON_ID
                body["partner_name"] = "Synthetic Partner"
        elif path == "/api/crm/statistics":
            body = {"total_people": 2}
        elif path == "/api/crm/birthdays/today":
            body = {"birthdays": []}
        elif path == "/api/crm/people":
            body = {"people": [SYNTHETIC_OTHER_PERSON], "total": 1, "offset": 0, "count": 1}
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


def _prepare(page: Page, partner_configured: bool = True):
    """Install the d3 stub and API stub before any page script runs, and
    start recording document `load` events (one always fires for the
    initial `page.goto()` - callers should clear the list after the first
    dashboard is up, then assert it stays empty across client-side
    navigation)."""
    page.add_init_script(D3_STUB_JS)
    requests_seen = []
    _route_api(page, requests_seen, partner_configured=partner_configured)
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


def _visible_dashboards(page: Page):
    """Which of the four dashboard containers are currently visible.

    #909 review finding 2/5: a leaked dashboard (e.g. Relationship still
    rendered underneath Family) is invisible to a test that only checks the
    *target* container is shown -- this checks all four every time so a
    stacked leak fails immediately, and finding 5's mutation test (deleting
    `showFamilyDashboard()`'s `#meDashboard`-hiding lines) actually fails
    the suite instead of passing it.
    """
    return [sel for sel in _DASHBOARD_IDS if page.locator(sel).is_visible()]


def _assert_only_visible(page: Page, expected_id: str):
    assert _visible_dashboards(page) == [expected_id], (
        f"expected only {expected_id} visible, got {_visible_dashboards(page)}"
    )


class TestNoDocumentReload:
    """Clicking a nav link updates the URL and swaps the dashboard without
    the browser performing a fresh document load."""

    def test_click_through_all_dashboards(self, page: Page, crm_base_url):
        requests_seen, load_events, console_errors = _goto_me(page, crm_base_url)

        assert _active_pages(page) == ["me"]
        _assert_only_visible(page, "#meDashboard")

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"
        assert _active_pages(page) == ["family"]
        _assert_only_visible(page, "#familyDashboard")

        page.locator('[data-page="birthdays"]').click()
        expect(page.locator("#birthdaysPage")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/birthdays"
        assert _active_pages(page) == ["birthdays"]
        _assert_only_visible(page, "#birthdaysPage")

        page.locator('[data-page="relationship"]').click()
        expect(page.locator("#relationshipDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/relationship"
        assert _active_pages(page) == ["relationship"]
        _assert_only_visible(page, "#relationshipDashboard")

        # Relationship -> Family and Family -> Relationship (#909 review
        # finding 2/5): showFamilyDashboard() used to leave the Relationship
        # dashboard rendered underneath it, invisible to a test that only
        # ever entered Family from Me/Birthdays.
        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"
        _assert_only_visible(page, "#familyDashboard")

        page.locator('[data-page="relationship"]').click()
        expect(page.locator("#relationshipDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/relationship"
        _assert_only_visible(page, "#relationshipDashboard")

        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]
        _assert_only_visible(page, "#meDashboard")

        # The CRM brand link has always redirected to /me on a direct load
        # (init()); clicking it client-side preserves that.
        page.locator('.header-nav a[href="/crm"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]
        _assert_only_visible(page, "#meDashboard")

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

    def test_birthdays_click_resets_a_filtered_query_string(self, page: Page, crm_base_url):
        """A nav-link click is a no-op only when the target is *exactly*
        where the page already is -- pathname alone isn't enough: from
        /birthdays?day=03-15 (Timeline tab, filtered), clicking Birthdays
        again must land on plain /birthdays, Calendar tab, matching what a
        full document reload to the bare URL would have shown (#909 review
        finding 6)."""
        _, load_events, console_errors = _prepare(page)
        page.goto(f"{crm_base_url}/birthdays?day=03-15")
        expect(page.locator("#birthdaysPage")).to_be_visible()
        expect(page.locator('.birthdays-tab[data-tab="timeline"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        load_events.clear()

        page.locator('[data-page="birthdays"]').click()
        expect(page.locator("#birthdaysPage")).to_be_visible()
        assert page.evaluate("window.location.href").endswith("/birthdays"), (
            "clicking Birthdays from a filtered URL must clear the query string"
        )
        expect(page.locator('.birthdays-tab[data-tab="heatmap"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        expect(page.locator('.birthdays-tab[data-tab="timeline"]')).not_to_have_class(
            re.compile(r"\bactive\b")
        )

        assert load_events == []
        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


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
        _assert_only_visible(page, "#familyDashboard")

        page.go_back()
        expect(page.locator("#meDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/me"
        assert _active_pages(page) == ["me"]
        _assert_only_visible(page, "#meDashboard")

        page.go_forward()
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/family"
        _assert_only_visible(page, "#familyDashboard")

        page.go_forward()
        expect(page.locator("#birthdaysPage")).to_be_visible()
        assert page.evaluate("window.location.pathname") == "/birthdays"
        assert _active_pages(page) == ["birthdays"]
        _assert_only_visible(page, "#birthdaysPage")

        assert load_events == [], (
            f"expected zero document loads across back/forward, got {len(load_events)}"
        )
        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestDashboardStatePersists:
    """A Me -> Family -> Me -> Family round trip issues no new request for
    data each dashboard's first visit already fetched and cached, even
    though Family's own heatmap window (`heatmapYears`, a global shared
    with Me's) is 10 years while Me's is derived from
    /me/interactions/span.

    Also covers the CRM performance follow-up review finding 18: the owner's
    person detail fetch, /api/crm/me/stats, and /api/crm/family/members must
    each fire at most once per session too, not just /me/interactions."""

    def test_second_me_visit_does_not_refetch_interactions(self, page: Page, crm_base_url):
        requests_seen, load_events, console_errors = _goto_me(page, crm_base_url)

        def _count(path):
            return len([r for r in requests_seen if r["path"] == path])

        # Let the first Me visit's /me/interactions settle.
        for _ in range(100):
            if _count("/api/crm/me/interactions") >= 1:
                break
            page.wait_for_timeout(50)
        assert _count("/api/crm/me/interactions") == 1, (
            "expected exactly one /me/interactions request from the first Me visit, "
            f"got {_count('/api/crm/me/interactions')}"
        )
        assert _count("/api/crm/me/stats") == 1
        assert _count(f"/api/crm/people/{MY_PERSON_ID}") == 1

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        page.wait_for_timeout(200)
        assert _count("/api/crm/family/members") == 1, (
            "expected exactly one /family/members request from the first Family visit, "
            f"got {_count('/api/crm/family/members')}"
        )

        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        # Give any (incorrect) re-fetch time to land before asserting.
        page.wait_for_timeout(300)

        assert _count("/api/crm/me/interactions") == 1, (
            "second Me render must reuse meInteractionsCache instead of "
            f"re-requesting /me/interactions, got {_count('/api/crm/me/interactions')} total calls"
        )
        assert _count("/api/crm/me/stats") == 1, (
            "second Me render must reuse meStatsCache instead of "
            f"re-requesting /me/stats, got {_count('/api/crm/me/stats')} total calls"
        )
        assert _count(f"/api/crm/people/{MY_PERSON_ID}") == 1, (
            "second Me render must reuse personDetailCache with no background "
            f"revalidation, got {_count(f'/api/crm/people/{MY_PERSON_ID}')} total calls"
        )

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        page.wait_for_timeout(300)
        assert _count("/api/crm/family/members") == 1, (
            "second Family render must reuse the cached member list instead of "
            f"re-requesting /family/members, got {_count('/api/crm/family/members')} total calls"
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


class TestYearsSelectorRespectsUserOverride:
    """The Me dashboard's heatmap years dropdown must not be clobbered by
    ensureMeHeatmapYears()'s span re-assertion (#909 review finding 1):
    picking a year count fetches that window and keeps showing it, rather
    than silently reverting to the span-derived default on the very call the
    selection triggered."""

    def test_selecting_years_fetches_that_window_and_keeps_the_selection(
        self, page: Page, crm_base_url,
    ):
        requests_seen, _, console_errors = _goto_me(page, crm_base_url)

        def _me_interactions_days_back_values():
            return [
                r["query"].get("days_back", [None])[0]
                for r in requests_seen if r["path"] == "/api/crm/me/interactions"
            ]

        # Let the initial span-derived request (mocked span years=2, i.e.
        # days_back=737) land before selecting a different window.
        for _ in range(100):
            if _me_interactions_days_back_values():
                break
            page.wait_for_timeout(20)
        assert "737" in _me_interactions_days_back_values()

        select = page.locator("#heatmapYearsSelect")
        select.select_option("3")
        page.wait_for_timeout(300)

        # 3 years -> days_back = 3*365 + 7 = 1102.
        assert "1102" in _me_interactions_days_back_values(), (
            f"expected a /me/interactions request with days_back=1102 (3 years) "
            f"after selecting '3', got days_back values: {_me_interactions_days_back_values()}"
        )

        # The selection itself must stick -- not get silently reverted back
        # to the span-derived "2" by the very call it triggered.
        assert select.input_value() == "3"
        expect(page.locator("#heatmapTitle")).to_have_text("3-Year Interaction History")

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestBirthdaysEntryPoint:
    """Entering the app at /birthdays (a realistic bookmark/share target)
    and then navigating to another dashboard must not leave the sidebar and
    header stats stuck on their loading state forever (#909 review
    finding 3): /birthdays intentionally skips loadPeople()/loadStatistics()
    in init() for a faster initial render, and nothing previously
    compensated once the user moved on to a dashboard that does need them."""

    def test_family_and_me_load_people_and_stats_after_entering_at_birthdays(
        self, page: Page, crm_base_url,
    ):
        _, _, console_errors = _prepare(page)
        page.goto(f"{crm_base_url}/birthdays")
        expect(page.locator("#birthdaysPage")).to_be_visible()

        page.locator('[data-page="family"]').click()
        expect(page.locator("#familyDashboard")).to_be_visible()
        expect(page.locator("#totalPeople")).to_have_text("2", timeout=3000)
        expect(page.locator("#peopleList")).not_to_contain_text("Loading people...", timeout=3000)
        expect(page.locator("#peopleList")).to_contain_text("Synthetic Friend")

        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        expect(page.locator("#totalPeople")).to_have_text("2")
        expect(page.locator("#peopleList")).to_contain_text("Synthetic Friend")

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestRelationshipNoPartnerChrome:
    """Family -> Relationship must not leak Family's hero stats or member
    selector onto the no-partner empty state (#909 review follow-up
    finding 1). A fresh install has no partner configured by default, so
    this is not a corner case -- every other test in this file configures a
    partner (see `_route_api`'s default) specifically so the Relationship
    hops exercise the real dashboard; this one deliberately doesn't."""

    def test_family_chrome_hidden_after_navigating_to_relationship(self, page: Page, crm_base_url):
        _prepare(page, partner_configured=False)
        page.goto(f"{crm_base_url}/family")
        expect(page.locator("#familyDashboard")).to_be_visible()
        expect(page.locator("#familyHeroStats")).to_be_visible()

        page.locator('[data-page="relationship"]').click()
        expect(page.locator("#relationshipEmptyState")).to_be_visible()
        expect(page.locator("#relationshipDashboard")).to_be_hidden()

        # The bug: these two, plus the detail header, used to still show
        # Family's content underneath/around the empty state.
        expect(page.locator("#familyHeroStats")).to_be_hidden()
        expect(page.locator("#familySelectorContainer")).to_be_hidden()
        expect(page.locator("#detailName")).to_have_text("Relationship Dashboard")


class TestActiveLinkAndTabRegressions:
    """Regression tests for two one-line fixes from the #909 review that
    shipped without their own test the first time (review finding 3):
    selecting a person must clear whichever dashboard link was highlighted,
    and landing on a person's URL with no tab segment must show Overview
    even if a different tab was showing a moment ago."""

    def test_selecting_a_person_from_me_clears_the_active_dashboard_link(
        self, page: Page, crm_base_url,
    ):
        _, _, console_errors = _goto_me(page, crm_base_url)
        assert _active_pages(page) == ["me"]

        page.evaluate(f"selectPerson('{SYNTHETIC_OTHER_PERSON['id']}')")
        expect(page.locator("#personContentGrid")).to_be_visible()
        assert _active_pages(page) == [], (
            "selecting a person must clear the Me link, not leave it highlighted"
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"

    def test_selecting_a_person_from_family_clears_the_active_dashboard_link(
        self, page: Page, crm_base_url,
    ):
        _, _, console_errors = _prepare(page)
        page.goto(f"{crm_base_url}/family")
        expect(page.locator("#familyDashboard")).to_be_visible()
        assert _active_pages(page) == ["family"]

        page.evaluate(f"selectPerson('{SYNTHETIC_OTHER_PERSON['id']}')")
        expect(page.locator("#personContentGrid")).to_be_visible()
        assert _active_pages(page) == [], (
            "selecting a person must clear the Family link, not leave it highlighted"
        )

    def test_me_from_a_no_tab_url_shows_overview_not_a_stale_tab(self, page: Page, crm_base_url):
        """Landing on /me (no tab segment) after /me/timeline was showing
        must reset to the Overview tab, not leave Timeline displayed."""
        _, _, console_errors = _goto_me(page, crm_base_url)

        page.evaluate("window.history.pushState({}, '', '/me/timeline')")
        page.evaluate("dispatchRoute()")
        expect(page.locator("#tabTimeline")).to_be_visible()

        page.locator('[data-page="me"]').click()
        expect(page.locator("#tabOverview")).to_be_visible()
        expect(page.locator("#tabTimeline")).to_be_hidden()
        expect(page.locator('.tab[data-tab="overview"]')).to_have_class(re.compile(r"\bactive\b"))

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestToneRefreshFailureNotice:
    """Regression test for the CRM performance follow-up review finding 19:
    when every month in a refresh=true response comes back `status:
    "stale"` (the server couldn't recompute a single one and fell back to
    stored scores for all of them), the Tone Evolution card must show one
    small notice -- the per-point dimmed `(stale)` markers alone are easy
    to miss. An ordinary first load that happens to be all-stale (nothing
    computed yet this session, not a failed refresh) must NOT show it."""

    STALE_TONE_RESPONSE = {
        "monthly_tones": [
            {"month": "2026-06", "score": 60, "combined_score": 60, "status": "stale"},
            {"month": "2026-07", "score": 62, "combined_score": 62, "status": "stale"},
        ],
        "user_average": 60, "partner_average": 62,
        "combined_trend": "stable", "user_trend": "stable", "partner_trend": "stable",
    }

    def test_all_stale_refresh_response_shows_failure_notice(self, page: Page, crm_base_url):
        _, _, console_errors = _prepare(page)

        def tone_handler(route):
            # Both responses are all-stale -- the *only* difference between
            # them is whether the request was a refresh=true one. If the
            # first-load fixture had no stale months, "must not show on
            # first load" would pass regardless of whether the code checks
            # toneCacheWasRefreshAttempt at all (the allStale half of the
            # condition would already be false). Making both fixtures
            # all-stale isolates that half: the only thing that can be
            # preventing the notice on first load is the refresh-attempt
            # flag.
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.STALE_TONE_RESPONSE))

        page.route("**/api/crm/relationship/tone-analysis-detailed**", tone_handler)

        page.goto(f"{crm_base_url}/relationship")
        expect(page.locator("#relationshipDashboard")).to_be_visible()
        expect(page.locator("#toneTimelineViz svg")).to_be_visible()
        # A normal (non-refresh) first load must never show the notice, even
        # though this fixture's initial response is all-stale too.
        expect(page.locator(".tone-refresh-failed-notice")).to_have_count(0)

        page.locator("#toneRefreshBtn").click()
        expect(page.locator(".tone-refresh-failed-notice")).to_be_visible()
        expect(page.locator(".tone-refresh-failed-notice")).to_have_text(
            "Refresh failed; showing stored scores"
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestBirthdaysNavigateToPersonIsClientSide:
    """Regression test for the CRM performance follow-up review finding 16:
    `navigateToPerson()` (used by the Birthdays timeline's person rows) set
    `window.location.href` directly, forcing a full document reload instead
    of going through the same client-side `navigateTo()`/`dispatchRoute()`
    path every other CRM link uses (#876)."""

    def test_clicking_a_birthday_person_does_not_reload_the_document(
        self, page: Page, crm_base_url,
    ):
        _, load_events, console_errors = _prepare(page)

        def birthdays_all_handler(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "total_people": 1,
                    "total_dates": 1,
                    "birthdays": {
                        "03-15": [
                            {"id": SYNTHETIC_OTHER_PERSON["id"], "name": "Synthetic Friend"},
                        ],
                    },
                }),
            )

        page.route("**/api/crm/birthdays/all", birthdays_all_handler)

        page.goto(f"{crm_base_url}/birthdays?day=03-15")
        expect(page.locator("#birthdaysPage")).to_be_visible()
        expect(page.locator(".birthday-timeline-person")).to_be_visible()
        load_events.clear()

        page.locator(".birthday-timeline-person").click()

        expect(page.locator("#personContentGrid")).to_be_visible()
        expect(page.locator("#birthdaysPage")).to_be_hidden()
        assert page.evaluate("window.location.pathname") == f"/crm/{SYNTHETIC_OTHER_PERSON['id']}"
        assert load_events == [], (
            f"expected zero document loads, got {len(load_events)}"
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestDirectLoadTabHighlight:
    """Regression test for the CRM performance follow-up review finding 15:
    `switchTab()` used to select `[data-tab="${tabName}"]` unscoped, and the
    Birthdays page's `.birthdays-tab[data-tab="timeline"]` button sits
    earlier in the DOM than the person-detail tab bar's own
    `.tab[data-tab="timeline"]` div -- so `document.querySelector` picked
    the Birthdays button first and the actual detail Timeline tab never got
    its `active` class on a direct load of `/crm/{id}/timeline`."""

    def test_direct_load_of_person_timeline_highlights_the_detail_tab(
        self, page: Page, crm_base_url,
    ):
        _, _, console_errors = _prepare(page)
        page.goto(f"{crm_base_url}/crm/{SYNTHETIC_OTHER_PERSON['id']}/timeline")
        expect(page.locator("#tabTimeline")).to_be_visible()

        expect(page.locator('.tab[data-tab="timeline"]')).to_have_class(re.compile(r"\bactive\b"))
        # The Birthdays page's same-named tab must never be the one that
        # picked up `active` -- it isn't even the visible page here.
        expect(page.locator('.birthdays-tab[data-tab="timeline"]')).not_to_have_class(
            re.compile(r"\bactive\b")
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"


class TestNotesEditInvalidatesPersonCache:
    """Regression test for the CRM performance follow-up review's round-2
    nit 6: saveNotes() must invalidate the cached person after a
    successful save, or navigating away and back within the same session
    renders the pre-edit cached copy instead of what was just saved.

    Proven without any timing race by making the post-edit GET hang
    forever: a correctly-invalidated cache takes the no-cache path on the
    second visit, which shows the loading skeleton (name stuck on
    "Loading...") while that fetch is pending; an un-invalidated cache
    renders the stale cached person synchronously instead, with no fetch
    ever issued at all -- these two end states never converge, so there is
    nothing to race."""

    def test_saving_notes_invalidates_the_person_cache(self, page: Page, crm_base_url):
        _, _, console_errors = _goto_me(page, crm_base_url)

        saved = {"done": False}
        pending_second_fetch = {"count": 0}

        def person_handler(route):
            if route.request.method == "PATCH":
                saved["done"] = True
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "ok"}))
                return
            if saved["done"]:
                # Deliberately never fulfilled -- its existence alone proves
                # a fetch was issued (cache miss); it must never resolve
                # within this test, so a background self-heal can't
                # coincidentally make a missing invalidation look correct.
                pending_second_fetch["count"] += 1
                return
            body = dict(SYNTHETIC_OTHER_PERSON)
            body["notes"] = "before-save"
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        # Precise patterns only -- a broad `**id**` glob also catches the
        # page's other person-scoped background loaders (facts, timeline
        # aggregation, fact extraction), which aren't part of what this test
        # is pinning and would otherwise get miscounted as extra pending
        # fetches below.
        person_url = f"**/api/crm/people/{SYNTHETIC_OTHER_PERSON['id']}"
        page.route(person_url, person_handler)
        page.route(f"{person_url}?include_related=true", person_handler)

        # Not awaited on the JS side by design below -- page.evaluate() would
        # otherwise block on selectPerson()'s returned promise, which never
        # resolves once the second fetch starts hanging.
        page.evaluate(f"() => {{ selectPerson('{SYNTHETIC_OTHER_PERSON['id']}'); }}")
        expect(page.locator("#personContentGrid")).to_be_visible()
        expect(page.locator("#notesArea")).to_have_value("before-save")

        page.fill("#notesArea", "after-save")
        page.dispatch_event("#notesArea", "input")
        page.click("#notesSaveBtn")
        expect(page.locator("#notesSaveBtn")).to_be_hidden()
        assert saved["done"] is True

        # Navigate away and back within the session.
        page.locator('[data-page="me"]').click()
        expect(page.locator("#meDashboard")).to_be_visible()
        page.evaluate(f"() => {{ selectPerson('{SYNTHETIC_OTHER_PERSON['id']}'); }}")

        page.wait_for_timeout(300)
        assert pending_second_fetch["count"] == 1, (
            "expected exactly one (permanently pending) refetch after the cache "
            f"invalidation, got {pending_second_fetch['count']} -- a cache hit "
            "would issue none at all"
        )
        assert page.locator("#detailName").inner_text() == "Loading...", (
            "expected the no-cache loading skeleton after an invalidated cache; "
            "a cache hit would render the stale cached person synchronously instead"
        )

        unexpected_errors = [e for e in console_errors if "d3" not in e.lower()]
        assert not unexpected_errors, f"Unexpected console errors: {unexpected_errors}"
