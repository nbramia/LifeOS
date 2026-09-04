"""Browser tests for CRM search cancellation and single-fetch person
selection (#874).

Two request-hygiene habits used to add avoidable round trips on every CRM
interaction: typing in the people search box fired a request per pause but
never cancelled the previous one, so a slower earlier response could
overwrite newer results; and selecting a person issued two back-to-back
`GET /people/{id}` requests (one plain, one with `include_related=true`).

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API. Rather than intercepting network
requests with Playwright's `page.route()` (whose synchronous handlers run on
the single connection event loop, so a deliberate `time.sleep()` delay in
one handler serializes *all* route dispatch instead of modelling two truly
concurrent in-flight requests), this monkey-patches `window.fetch` itself
via an init script. That runs entirely inside the page on real
`setTimeout`/`Promise` concurrency, and — crucially — actually honors
`AbortSignal` the way a real network stack does, so these tests exercise the
real cancellation wiring rather than only the identity-check fallback.

Carries no `requires_server` marker, so it runs at pre-push
(`browser and not requires_server`).
"""
import http.server
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic people — never real CRM data.
PERSON_SLOW = {
    "id": "person-slow-result", "canonical_name": "Slow Query Result",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 10,
    "company": "", "tags": [],
}
PERSON_FAST = {
    "id": "person-fast-result", "canonical_name": "Fast Query Result",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 8,
    "company": "", "tags": [],
}
PERSON_CLICKABLE = {
    "id": "person-clickable", "canonical_name": "Clickable Person",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 5,
    "company": "", "tags": [],
}
PERSON_DETAIL = {
    **PERSON_CLICKABLE,
    "emails": [], "phone_numbers": [], "vault_contexts": [], "sources": [],
    # source_entity_count > 0 while source_entities is empty is the exact
    # mismatch that used to hang the panel on "Loading source entities..."
    # forever (nothing re-rendered it after loadRelatedData() was removed).
    "source_entities": [], "source_entity_count": 3, "relationships": [],
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


# Installed before any page script runs (rules are baked in at generation
# time, not set afterward via page.evaluate() — the page's own init() starts
# fetching on DOMContentLoaded, before an evaluate() call issued after
# page.goto() returns could possibly land, which would otherwise race an
# empty rule set against the page's first real fetch calls).
#
# Replaces window.fetch with a stub driven by `window.__mockRules` (an array
# of {match, body, status, delayMs}, checked in order, first match wins).
# Records every URL fetched into `window.__fetchCalls`. Honors AbortSignal
# for real: aborting rejects with a genuine "AbortError" DOMException,
# exactly like a real in-flight fetch would.
_MOCK_FETCH_INIT_SCRIPT_TEMPLATE = """
(() => {
    window.__fetchCalls = [];
    window.__abortedUrls = [];
    window.__mockRules = %(rules_json)s;
    window.fetch = function(input, init) {
        const url = typeof input === 'string' ? input : input.url;
        window.__fetchCalls.push(url);
        const rule = window.__mockRules.find(r => url.includes(r.match));
        const body = rule ? rule.body : {};
        const status = rule ? (rule.status || 200) : 200;
        const delayMs = rule ? (rule.delayMs || 0) : 0;
        const signal = init && init.signal;

        return new Promise((resolve, reject) => {
            if (signal && signal.aborted) {
                window.__abortedUrls.push(url);
                reject(new DOMException('The operation was aborted.', 'AbortError'));
                return;
            }
            const timer = setTimeout(() => {
                resolve(new Response(JSON.stringify(body), {
                    status: status,
                    headers: { 'Content-Type': 'application/json' },
                }));
            }, delayMs);
            if (signal) {
                signal.addEventListener('abort', () => {
                    clearTimeout(timer);
                    window.__abortedUrls.push(url);
                    reject(new DOMException('The operation was aborted.', 'AbortError'));
                });
            }
        });
    };
})();
"""

_BASE_RULES = [
    {"match": "/api/crm/config", "body": {}},
    {"match": "/api/crm/statistics", "body": {"total_people": 0}},
    {"match": "/api/crm/birthdays/today", "body": {"birthdays": []}},
]


def _goto_me_with_rules(page: Page, crm_base_url, extra_rules):
    """Load /me with window.fetch mocked. `extra_rules` are matched before
    the generic /api/crm/people? fallback, so put more specific rules first."""
    rules = [*extra_rules, *_BASE_RULES, {"match": "/api/crm/people?", "body": EMPTY_PEOPLE_PAGE}]
    script = _MOCK_FETCH_INIT_SCRIPT_TEMPLATE % {"rules_json": json.dumps(rules)}
    page.add_init_script(script)
    page.goto(f"{crm_base_url}/me")
    page.wait_for_selector("#searchInput")
    # Let the initial (empty-search) people load from init() settle before a
    # test starts typing, so it isn't mistaken for a search result.
    page.wait_for_timeout(100)


def _fetch_calls(page: Page):
    return page.evaluate("window.__fetchCalls")


class TestSearchCancellation:
    """A slower earlier search response must never overwrite a newer one."""

    def test_delayed_first_query_does_not_overwrite_second(self, page: Page, crm_base_url):
        _goto_me_with_rules(page, crm_base_url, [
            {"match": "q=slow", "body": {"people": [PERSON_SLOW], "total": 1,
                                          "offset": 0, "count": 1}, "delayMs": 600},
            {"match": "q=fast", "body": {"people": [PERSON_FAST], "total": 1,
                                          "offset": 0, "count": 1}, "delayMs": 0},
        ])

        search_input = page.locator("#searchInput")
        search_input.fill("slow")
        # Let the 300ms debounce fire the first (slow) request.
        page.wait_for_timeout(350)
        search_input.fill("fast")
        # The second (fast) request's debounce fires and its fast response
        # renders well before the first one's artificial 600ms delay elapses.
        expect(page.locator(".person-card .person-name")).to_have_text(
            "Fast Query Result", timeout=3000)

        # Give the slow response's timer time to fire and confirm it didn't
        # overwrite the result once it (would have) arrived.
        page.wait_for_timeout(500)
        expect(page.locator(".person-card .person-name")).to_have_text("Fast Query Result")
        expect(page.locator(".person-card")).to_have_count(1)

    def test_superseded_request_is_actually_aborted(self, page: Page, crm_base_url):
        """The first request's AbortSignal actually fires — not just a
        stale-response guard — proving loadPeople() really cancels the
        in-flight call rather than merely ignoring its eventual result."""
        _goto_me_with_rules(page, crm_base_url, [
            {"match": "q=slow", "body": {"people": [PERSON_SLOW], "total": 1,
                                          "offset": 0, "count": 1}, "delayMs": 5000},
            {"match": "q=fast", "body": {"people": [PERSON_FAST], "total": 1,
                                          "offset": 0, "count": 1}, "delayMs": 0},
        ])

        search_input = page.locator("#searchInput")
        search_input.fill("slow")
        page.wait_for_timeout(350)
        search_input.fill("fast")
        expect(page.locator(".person-card .person-name")).to_have_text(
            "Fast Query Result", timeout=3000)

        # The mock's setTimeout for the slow rule is 5s out; if the abort
        # listener fired, window.__abortedUrls records it immediately —
        # no need to wait anywhere near 5s to know it was cancelled.
        aborted_urls = page.evaluate("window.__abortedUrls")
        assert any("q=slow" in u for u in aborted_urls), (
            f"expected the superseded 'slow' request to be aborted, got: {aborted_urls}"
        )

    def test_abort_error_does_not_show_failure_state(self, page: Page, crm_base_url):
        """A cancelled request must not surface the generic error message.

        The fills below are spaced > 300ms apart (matching the sibling
        tests above) so each one's own debounce actually fires its own
        request — closer spacing (as an earlier version of this test had
        it, all 100ms apart) means only the *last* fill's debounce ever
        fires at all, so nothing is ever superseded or aborted and this
        assertion would hold trivially, proving nothing. "one" and "two"
        are given a long artificial delay so they are still genuinely
        in-flight (and therefore actually get aborted, not just rendered
        over) when the next fill starts.

        The failure-panel check happens between the "two" and "three"
        fills — i.e. right when "two"'s loadPeople() call aborts "one" —
        not after "three" has already resolved. Checking only at the end
        (an earlier version of this test did) is not a real assertion of
        the `AbortError` guard at all: "three"'s own successful render
        happens after, and overwrites, anything the (correctly or
        incorrectly handled) abort of "one"/"two" painted in between, so a
        version of this file with the guard deleted still passes the
        final-state check. Checking mid-race, while "two" is still
        in-flight and nothing has resolved yet to paper over it, is what
        actually pins the guard: without it, "one"'s abort rejection
        overwrites "two"'s own "Loading people..." with the generic
        failure panel on the very next microtask.
        """
        _goto_me_with_rules(page, crm_base_url, [
            {"match": "q=one", "body": {"people": [PERSON_SLOW], "total": 1,
                                         "offset": 0, "count": 1}, "delayMs": 5000},
            {"match": "q=two", "body": {"people": [PERSON_SLOW], "total": 1,
                                         "offset": 0, "count": 1}, "delayMs": 5000},
            {"match": "q=three", "body": {"people": [PERSON_FAST], "total": 1,
                                           "offset": 0, "count": 1}, "delayMs": 0},
        ])

        search_input = page.locator("#searchInput")
        search_input.fill("one")
        page.wait_for_timeout(350)
        search_input.fill("two")
        page.wait_for_timeout(350)  # "two" fires and aborts "one" here

        # "one" is now aborted and "two" is still in flight (its own 5s
        # delay hasn't elapsed) — nothing has resolved yet to overwrite a
        # wrongly-shown failure panel. This is the moment the AbortError
        # guard actually matters.
        expect(page.locator("#peopleList")).not_to_contain_text("Failed to load people")

        search_input.fill("three")

        expect(page.locator(".person-card .person-name")).to_have_text(
            "Fast Query Result", timeout=3000)

        # Prove something was actually superseded/aborted before checking
        # that it didn't surface as a failure — otherwise a version of this
        # test that never exercises cancellation at all would pass
        # trivially (this is exactly what #895's review caught).
        aborted_urls = page.evaluate("window.__abortedUrls")
        assert any("q=one" in u for u in aborted_urls), (
            f"expected the superseded 'one' request to be aborted, got: {aborted_urls}"
        )
        assert any("q=two" in u for u in aborted_urls), (
            f"expected the superseded 'two' request to be aborted, got: {aborted_urls}"
        )

        expect(page.locator("#peopleList")).not_to_contain_text("Failed to load people")


class TestSinglePersonFetch:
    """Selecting a person issues exactly one detail request."""

    def test_selecting_person_issues_one_detail_request(self, page: Page, crm_base_url):
        _goto_me_with_rules(page, crm_base_url, [
            {"match": "/api/crm/people/person-clickable", "body": PERSON_DETAIL},
            {"match": "/api/crm/people?", "body": {"people": [PERSON_CLICKABLE], "total": 1,
                                                     "offset": 0, "count": 1}},
        ])

        expect(page.locator(".person-card .person-name")).to_have_text("Clickable Person")
        page.locator(".person-card").first.click()

        expect(page.locator("#detailContent")).to_be_visible()
        page.wait_for_timeout(300)

        detail_url_re = re.compile(r"/api/crm/people/person-clickable(\?|$)")
        detail_requests = [u for u in _fetch_calls(page) if detail_url_re.search(u)]
        assert len(detail_requests) == 1, (
            f"expected exactly one /people/{{id}} request, got {len(detail_requests)}: "
            f"{detail_requests}"
        )
        assert "include_related=true" in detail_requests[0]

        # PERSON_DETAIL carries source_entities: [] (present but empty) —
        # renderPersonDetail() must treat that as "no source entities" and
        # never leave the panel on the "Loading source entities..."
        # placeholder, which nothing re-renders once loadRelatedData() (the
        # only thing that used to settle it) was removed.
        source_entities_text = page.locator("#sourceEntitiesList").inner_text()
        assert "Loading source entities" not in source_entities_text
        assert "No source entities" in source_entities_text
