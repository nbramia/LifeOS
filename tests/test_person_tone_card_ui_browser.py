"""Browser tests for the person-overview "Tone" card in web/crm.html.

Covers the card's states -- not-analyzed, analyzed, computing, no-messages,
in-progress, and failed (see updatePersonToneCard's docstring in
web/crm.html for what drives each) -- that clicking "Analyze"/refresh issues
exactly one request, that the card is hidden on the owner's own page and for
a person with no iMessage history at all (`person.sources` must include
'imessage' -- the tone pipeline only ever reads iMessage, so a
WhatsApp/Slack-only person can never have anything for it to show), and
that switching people cancels a still-in-flight tone request rather than
letting a stale response render over the newly selected person -- including
when the abort itself doesn't actually stop the in-flight response from
arriving, which only the handler's own identity guard (not AbortController)
can defend against.

Like tests/test_crm_request_hygiene_ui_browser.py (which this borrows its
fetch-mock harness from -- see that file's own docstring for why a
Playwright `page.route()` handler, whose synchronous callbacks all run on
one event loop, can't model two genuinely concurrent in-flight requests the
way a real AbortSignal-honoring `window.fetch` replacement can), this serves
`web/crm.html` itself from an ephemeral port and mocks `window.fetch` before
any page script runs. Carries no `requires_server` marker, so it runs at
pre-push (`browser and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic ids/people -- never real CRM data.
MY_PERSON_ID = "person-synthetic-owner"
PERSON_WITH_MESSAGES_ID = "person-synthetic-has-messages"
PERSON_WHATSAPP_ONLY_ID = "person-synthetic-whatsapp-only"
PERSON_A_ID = "person-synthetic-switch-a"
PERSON_B_ID = "person-synthetic-switch-b"


def _person(person_id: str, name: str, message_count: int, sources: list | None = None) -> dict:
    return {
        "id": person_id, "canonical_name": name, "category": "personal",
        "dunbar_circle": 3, "relationship_strength": 5, "company": "", "tags": [],
        "emails": [], "phone_numbers": [], "vault_contexts": [], "sources": sources or [],
        "source_entities": [], "source_entity_count": 0, "relationships": [],
        "message_count": message_count,
    }


MY_PERSON = _person(MY_PERSON_ID, "Synthetic Owner", message_count=99, sources=["imessage"])
PERSON_WITH_MESSAGES = _person(
    PERSON_WITH_MESSAGES_ID, "Has Messages", message_count=42, sources=["imessage"],
)
# Has *some* messages (WhatsApp), but the tone pipeline only ever reads
# source_type="imessage" -- the card must gate on `sources` containing
# 'imessage' specifically, not on message_count/having any messages at all.
PERSON_WHATSAPP_ONLY = _person(
    PERSON_WHATSAPP_ONLY_ID, "WhatsApp Only", message_count=15, sources=["whatsapp"],
)
PERSON_A = _person(PERSON_A_ID, "Switch Target A", message_count=10, sources=["imessage"])
PERSON_B = _person(PERSON_B_ID, "Switch Target B", message_count=10, sources=["imessage"])

NOT_ANALYZED_RESPONSE = {
    "monthly_tones": [], "trend": "not-analyzed", "average": None,
    "analyzed_through": None, "status": "ok", "generated_at": "2026-01-01T00:00:00+00:00",
}
ANALYZED_RESPONSE = {
    "monthly_tones": [
        {"month": "2025-12", "score": 70.0, "status": None},
        {"month": "2026-01", "score": 60.0, "status": "stale"},
    ],
    "trend": "stable-positive", "average": 65.0, "analyzed_through": "2026-01",
    "status": "ok", "generated_at": "2026-01-01T00:00:00+00:00",
}
# An iMessage person with nothing in this particular window -- distinct
# from "not yet analyzed": no button at all, just explainer text.
NO_MESSAGES_RESPONSE = {
    "monthly_tones": [], "trend": "not-analyzed", "average": None,
    "analyzed_through": None, "status": "no-messages", "generated_at": "2026-01-01T00:00:00+00:00",
}
# compute=true acquired the lock but every stale month's LLM call failed and
# nothing was ever stored for this person at all.
FAILED_RESPONSE = {
    "monthly_tones": [{"month": "2026-01", "score": 50.0, "status": "error"}],
    "trend": "not-analyzed", "average": None, "analyzed_through": None,
    "status": "failed", "generated_at": "2026-01-01T00:00:00+00:00",
}
# compute=true timed out waiting for another request's lock on this person.
IN_PROGRESS_RESPONSE = {
    "monthly_tones": [{"month": "2026-01", "score": 50.0, "status": "error"}],
    "trend": "not-analyzed", "average": None, "analyzed_through": None,
    "status": "in-progress", "generated_at": "2026-01-01T00:00:00+00:00",
}


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    """Serves crm.html the way api/main.py does for /me, /crm/{id}, etc."""

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


# Replaces window.fetch with a stub driven by `window.__mockRules` (an array
# of {match, body, status, delayMs}, checked in order, first match wins;
# anything unmatched defaults to {} -- every loader in crm.html treats a
# missing/empty aggregate defensively). Records every URL fetched into
# `window.__fetchCalls`.
#
# Two extra per-rule knobs beyond a single static `body`:
# - `bodies` (list): a rule may return a different body on each successive
#   call matching it (e.g. "not yet analyzed" on the initial load, then a
#   real result on a later poll) -- clamped to the last entry once
#   exhausted, keyed per-rule so unrelated rules keep independent counters.
# - `ignoreAbort` (bool): honors AbortSignal for real by default -- aborting
#   rejects with a genuine "AbortError" DOMException, exactly like a real
#   in-flight fetch would, necessary to prove a superseded tone request is
#   actually cancelled, not merely ignored once it resolves. A rule with
#   `ignoreAbort: true` still records the abort into `window.__abortedUrls`
#   (the app did ask to cancel) but resolves the response after its delay
#   regardless -- simulating an environment where cancellation doesn't
#   actually stop an in-flight response from arriving, so only the
#   handler's own identity guard (not AbortController) can keep it from
#   rendering over whatever superseded it.
_MOCK_FETCH_INIT_SCRIPT_TEMPLATE = """
(() => {
    window.__fetchCalls = [];
    window.__abortedUrls = [];
    window.__mockRules = %(rules_json)s;
    window.fetch = function(input, init) {
        const url = typeof input === 'string' ? input : input.url;
        window.__fetchCalls.push(url);
        const rule = window.__mockRules.find(r => url.includes(r.match));
        let body = {};
        if (rule) {
            if (rule.bodies) {
                rule.__callIndex = rule.__callIndex || 0;
                const idx = Math.min(rule.__callIndex, rule.bodies.length - 1);
                body = rule.bodies[idx];
                rule.__callIndex++;
            } else {
                body = rule.body;
            }
        }
        const status = rule ? (rule.status || 200) : 200;
        const delayMs = rule ? (rule.delayMs || 0) : 0;
        const ignoreAbort = !!(rule && rule.ignoreAbort);
        const signal = init && init.signal;

        return new Promise((resolve, reject) => {
            if (!ignoreAbort && signal && signal.aborted) {
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
                if (ignoreAbort) {
                    signal.addEventListener('abort', () => {
                        window.__abortedUrls.push(url);
                    });
                } else {
                    signal.addEventListener('abort', () => {
                        clearTimeout(timer);
                        window.__abortedUrls.push(url);
                        reject(new DOMException('The operation was aborted.', 'AbortError'));
                    });
                }
            }
        });
    };
})();
"""


# ensurePeopleAndStatsLoaded() fires loadPeople() in the background on
# every route, regardless of which page is shown -- every rule set below
# needs a people-list fallback so that background call doesn't throw.
_PEOPLE_LIST_FALLBACK = {
    "match": "/api/crm/people?", "body": {"people": [], "total": 0, "offset": 0, "count": 0},
}


def _goto_with_rules(page: Page, crm_base_url: str, path: str, extra_rules: list):
    rules = [*extra_rules, _PEOPLE_LIST_FALLBACK]
    script = _MOCK_FETCH_INIT_SCRIPT_TEMPLATE % {"rules_json": json.dumps(rules)}
    page.add_init_script(script)
    page.goto(f"{crm_base_url}{path}")


def _fetch_calls(page: Page):
    return page.evaluate("window.__fetchCalls")


def _tone_calls(page: Page):
    return [u for u in _fetch_calls(page) if "/relationship/tone-analysis" in u]


class TestNotAnalyzedState:
    def test_shows_explainer_and_analyze_button_with_a_single_compute_false_request(
        self, page: Page, crm_base_url,
    ):
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "/relationship/tone-analysis", "body": NOT_ANALYZED_RESPONSE},
        ])

        expect(page.locator("#personToneCard")).to_be_visible()
        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")
        expect(page.locator("#personToneActionBtn")).to_be_enabled()

        tone_calls = _tone_calls(page)
        assert len(tone_calls) == 1, f"expected exactly one request, got {tone_calls}"
        assert "compute=true" not in tone_calls[0]


class TestHiddenCases:
    def test_hidden_on_the_owners_own_page_no_request_at_all(self, page: Page, crm_base_url):
        _goto_with_rules(page, crm_base_url, "/me", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{MY_PERSON_ID}", "body": MY_PERSON},
            {"match": "/relationship/tone-analysis", "body": ANALYZED_RESPONSE},
        ])

        expect(page.locator("#meDashboard")).to_be_visible()
        expect(page.locator("#personToneCard")).to_be_hidden()
        assert _tone_calls(page) == []

    def test_hidden_for_a_whatsapp_only_person_no_request_at_all(
        self, page: Page, crm_base_url,
    ):
        """A person with real message history, just none of it iMessage:
        the card must still be hidden, because `person.sources` (not
        message_count) is what gates it."""
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WHATSAPP_ONLY_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WHATSAPP_ONLY_ID}", "body": PERSON_WHATSAPP_ONLY},
            {"match": "/relationship/tone-analysis", "body": ANALYZED_RESPONSE},
        ])

        expect(page.locator("#detailContent")).to_be_visible()
        expect(page.locator("#personToneCard")).to_be_hidden()
        assert _tone_calls(page) == []


class TestAnalyzeClickAndComputingState:
    def test_click_analyze_shows_computing_then_analyzed_state_one_extra_request(
        self, page: Page, crm_base_url,
    ):
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "compute=true", "body": ANALYZED_RESPONSE, "delayMs": 300},
            {"match": "/relationship/tone-analysis", "body": NOT_ANALYZED_RESPONSE},
        ])

        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")
        assert len(_tone_calls(page)) == 1  # the initial compute=false load

        page.locator("#personToneActionBtn").click()

        # Computing state: disabled button, progress text -- checked before
        # the 300ms delayed response resolves.
        expect(page.locator("#personToneActionBtn")).to_be_disabled()
        expect(page.locator("#personToneContent")).to_contain_text("Analyzing")

        # Analyzed state once the compute=true response resolves.
        expect(page.locator("#personToneActionBtn")).to_have_text("↻", timeout=3000)
        expect(page.locator("#personToneContent")).to_contain_text("stable positive")
        expect(page.locator("#personToneContent")).to_contain_text("through Jan 2026")
        expect(page.locator("#personToneContent svg")).to_be_visible()

        tone_calls = _tone_calls(page)
        assert len(tone_calls) == 2, f"expected exactly one extra request, got {tone_calls}"
        assert "compute=true" in tone_calls[1]


class TestNoMessagesState:
    def test_imessage_person_with_no_messages_in_window_shows_text_and_no_button(
        self, page: Page, crm_base_url,
    ):
        """An iMessage person (the card isn't hidden) whose analysis window
        has no interactions at all: explainer text only, no button --
        distinct from "not yet analyzed", which does show a button."""
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "/relationship/tone-analysis", "body": NO_MESSAGES_RESPONSE},
        ])

        expect(page.locator("#personToneCard")).to_be_visible()
        expect(page.locator("#personToneContent")).to_contain_text(
            "No iMessage in the last 12 months",
        )
        expect(page.locator("#personToneActionBtn")).to_have_count(0)


class TestInProgressState:
    def test_compute_true_in_progress_then_poll_resolves_to_analyzed(
        self, page: Page, crm_base_url,
    ):
        """compute=true returning status "in-progress" (another request
        holds this person's lock) shows progress text -- never the failure
        notice -- then the card re-reads with compute=false every 5s on
        its own, with no further user action, until a real score appears."""
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "compute=true", "body": IN_PROGRESS_RESPONSE},
            {
                "match": "/relationship/tone-analysis",
                "bodies": [NOT_ANALYZED_RESPONSE, ANALYZED_RESPONSE],
            },
        ])

        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")
        page.locator("#personToneActionBtn").click()

        expect(page.locator("#personToneContent")).to_contain_text(
            "already running", timeout=3000,
        )
        expect(page.locator("#personToneContent")).not_to_contain_text("Analysis failed")

        # The 5s auto-poll (compute=false) then returns real data -- no
        # click, no further user action.
        expect(page.locator("#personToneContent")).to_contain_text(
            "through Jan 2026", timeout=8000,
        )
        expect(page.locator("#personToneActionBtn")).to_have_text("↻")

        tone_calls = _tone_calls(page)
        assert len(tone_calls) == 3, (
            f"expected initial load + compute=true + one 5s poll, got {tone_calls}"
        )


class TestFailedState:
    def test_compute_true_failed_status_shows_notice_and_keeps_button(
        self, page: Page, crm_base_url,
    ):
        """A compute=true attempt with status "failed" (lock acquired,
        nothing real ever produced or found) shows the one-line failure
        notice plus the explainer/button -- never a blank or stuck card."""
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "compute=true", "body": FAILED_RESPONSE},
            {"match": "/relationship/tone-analysis", "body": NOT_ANALYZED_RESPONSE},
        ])

        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")
        page.locator("#personToneActionBtn").click()
        expect(page.locator("#personToneContent")).to_contain_text(
            "Analysis failed", timeout=3000,
        )
        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")


class TestFailureNotice:
    def test_recompute_that_changes_nothing_shows_a_one_line_failure_notice(
        self, page: Page, crm_base_url,
    ):
        """A compute=true attempt that comes back with every scored month
        still `status="stale"` (the recompute failed server-side but a
        prior stored result exists, so the top-level status is still "ok")
        must show that stored data plus a one-line notice -- the legacy
        fallback path (personToneWasComputeAttempt + all-stale), distinct
        from the status="failed" case above, which has no scored data at
        all."""
        stale_response = {
            "monthly_tones": [{"month": "2026-01", "score": 55.0, "status": "stale"}],
            "trend": "stable-neutral", "average": 55.0, "analyzed_through": "2026-01",
            "status": "ok", "generated_at": "2026-01-01T00:00:00+00:00",
        }
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "compute=true", "body": stale_response},
            {"match": "/relationship/tone-analysis", "body": stale_response},
        ])

        expect(page.locator("#personToneActionBtn")).to_have_text("↻")
        page.locator("#personToneActionBtn").click()
        expect(page.locator("#personToneContent")).to_contain_text(
            "Analysis failed", timeout=3000,
        )
        # The stored data is still shown alongside the notice, not replaced.
        expect(page.locator("#personToneContent svg")).to_be_visible()


class TestPersonSwitchCancelsStaleRequest:
    def test_switching_person_aborts_the_first_persons_in_flight_request(
        self, page: Page, crm_base_url,
    ):
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_A_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_A_ID}", "body": PERSON_A},
            {"match": f"/api/crm/people/{PERSON_B_ID}", "body": PERSON_B},
            {"match": f"tone-analysis?person_id={PERSON_A_ID}", "body": ANALYZED_RESPONSE, "delayMs": 2000},
            {"match": f"tone-analysis?person_id={PERSON_B_ID}", "body": ANALYZED_RESPONSE, "delayMs": 0},
        ])

        expect(page.locator("#personToneCard")).to_be_visible()
        # Person A's tone request is now in flight with a 2s artificial delay.

        page.evaluate(f"selectPerson('{PERSON_B_ID}')")
        expect(page.locator("#detailName")).to_contain_text("Switch Target B")
        expect(page.locator("#personToneActionBtn")).to_have_text("↻", timeout=3000)

        aborted_urls = page.evaluate("window.__abortedUrls")
        assert any(f"person_id={PERSON_A_ID}" in u for u in aborted_urls), (
            f"expected person A's superseded tone request to be aborted, got: {aborted_urls}"
        )

        # Give A's (aborted) 2s timer time to fire if it hadn't actually
        # been cancelled, so a broken abort couldn't overwrite B's card.
        page.wait_for_timeout(2200)
        expect(page.locator("#personToneContent")).to_contain_text("through Jan 2026")

        tone_calls = _tone_calls(page)
        assert len(tone_calls) == 2, f"expected exactly one request per person, got {tone_calls}"

    def test_ignored_abort_never_lets_a_superseded_response_render_over_the_new_person(
        self, page: Page, crm_base_url,
    ):
        """Simulates an environment where cancelling person A's in-flight
        request doesn't actually stop its response from arriving -- the
        mock still records the abort (the app really did ask to cancel:
        see `ignoreAbort` above), but resolves A's request anyway after
        its delay. The only thing that can still keep A's now-stale
        response from overwriting B's already-rendered card is
        loadPersonToneSummary's own identity guard
        (`personToneController !== controller || selectedPersonId !==
        personId`) -- not AbortController -- so this must fail if that
        guard is ever removed or weakened."""
        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_A_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_A_ID}", "body": PERSON_A},
            {"match": f"/api/crm/people/{PERSON_B_ID}", "body": PERSON_B},
            {
                "match": f"tone-analysis?person_id={PERSON_A_ID}",
                "body": ANALYZED_RESPONSE, "delayMs": 500, "ignoreAbort": True,
            },
            {"match": f"tone-analysis?person_id={PERSON_B_ID}", "body": NOT_ANALYZED_RESPONSE, "delayMs": 0},
        ])

        expect(page.locator("#personToneCard")).to_be_visible()
        # Person A's tone request is in flight (500ms) and will resolve
        # regardless of being aborted.

        page.evaluate(f"selectPerson('{PERSON_B_ID}')")
        expect(page.locator("#detailName")).to_contain_text("Switch Target B")
        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone", timeout=3000)

        aborted_urls = page.evaluate("window.__abortedUrls")
        assert any(f"person_id={PERSON_A_ID}" in u for u in aborted_urls), (
            f"expected person A's tone request to still be flagged as aborted "
            f"(even though the mock ignores it), got: {aborted_urls}"
        )

        # Give A's ignored-abort response time to resolve; the identity
        # guard, not cancellation, must keep it from ever rendering.
        page.wait_for_timeout(700)
        expect(page.locator("#personToneContent")).to_contain_text("No tone analysis yet")
        expect(page.locator("#personToneContent")).not_to_contain_text("through Jan 2026")

        tone_calls = _tone_calls(page)
        assert len(tone_calls) == 2, f"expected exactly one request per person, got {tone_calls}"


class TestNoApplicationConsoleErrors:
    def test_no_console_errors_across_all_four_states(self, page: Page, crm_base_url):
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        _goto_with_rules(page, crm_base_url, f"/crm/{PERSON_WITH_MESSAGES_ID}", [
            {"match": "/api/crm/config", "body": {"my_person_id": MY_PERSON_ID}},
            {"match": f"/api/crm/people/{PERSON_WITH_MESSAGES_ID}", "body": PERSON_WITH_MESSAGES},
            {"match": "compute=true", "body": ANALYZED_RESPONSE},
            {"match": "/relationship/tone-analysis", "body": NOT_ANALYZED_RESPONSE},
        ])
        expect(page.locator("#personToneActionBtn")).to_have_text("Analyze tone")
        page.locator("#personToneActionBtn").click()
        expect(page.locator("#personToneActionBtn")).to_have_text("↻", timeout=3000)

        assert console_errors == [], f"unexpected console errors: {console_errors}"
