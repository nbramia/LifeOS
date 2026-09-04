"""Browser test for the Relationship page's no-partner-configured guard.

Opening /relationship with no `partner_person_id` in the CRM config must not
fire requests with an empty id segment (`/api/crm/people//timeline`) or leave
the tone/photo/timeline sections to fail silently: the page checks
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
from playwright.sync_api import Browser, Page, expect

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


def _open_relationship_page(
    page: Page, crm_base_url: str, *, partner_person_id: str, tone_response: dict = None,
):
    """Load /relationship, stubbing /api/crm/config with the given partner id
    (empty string == not configured) and every other /api/** call with an
    empty-but-valid JSON body. `tone_response`, if given, overrides the
    stubbed body for the tone-analysis-detailed call specifically. Returns
    the list of request URLs observed and any console errors logged."""
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
        elif "tone-analysis-detailed" in route.request.url and tone_response is not None:
            body = tone_response
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

        # No timeline request may carry a missing id segment.
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

    def test_navigating_to_a_person_hides_the_empty_state(self, page: Page, crm_base_url):
        """The person-detail render path must hide the empty state, or it
        keeps rendering inside an unrelated person's page after navigating
        away from /relationship."""
        _open_relationship_page(page, crm_base_url, partner_person_id="")
        expect(page.locator("#relationshipEmptyState")).to_be_visible()

        # Simulate clicking a person in the sidebar (selectPerson is the
        # click handler every person-list row wires to).
        page.evaluate("selectPerson('synthetic-person-x')")
        page.wait_for_timeout(300)

        expect(page.locator("#relationshipEmptyState")).to_be_hidden()


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


# A tone-analysis-detailed response with one error month sandwiched between
# two normal ones -- used by both the error-marker test and (implicitly) to
# confirm normal months still render.
_TONE_RESPONSE_WITH_ERROR_MONTH = {
    "monthly_tones": [
        {"month": "2026-05", "user_score": 80.0, "partner_score": 80.0,
         "combined_score": 80.0, "user_sample_count": 5, "partner_sample_count": 5,
         "status": None},
        {"month": "2026-06", "user_score": 50.0, "partner_score": 50.0,
         "combined_score": 50.0, "user_sample_count": 0, "partner_sample_count": 0,
         "status": "error"},
        {"month": "2026-07", "user_score": 20.0, "partner_score": 20.0,
         "combined_score": 20.0, "user_sample_count": 5, "partner_sample_count": 5,
         "status": None},
    ],
    "user_trend": "declining", "partner_trend": "declining", "combined_trend": "declining",
    "user_average": 50.0, "partner_average": 50.0, "generated_at": "2026-07-15T00:00:00+00:00",
}


class TestToneChartErrorMonth:
    """A status="error" month must render with a distinct marker, not a
    fabricated score of 50 that would be indistinguishable from a
    genuinely neutral month and would disagree with the summary average
    (which already excludes it server-side)."""

    def test_error_month_gets_a_distinct_marker_not_a_fabricated_point(self, page: Page, crm_base_url):
        _open_relationship_page(
            page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID,
            tone_response=_TONE_RESPONSE_WITH_ERROR_MONTH,
        )
        page.wait_for_timeout(300)

        error_markers = page.locator(".tone-data-point-error")
        expect(error_markers).to_have_count(1)

        marker_title = error_markers.first.locator("title").text_content()
        assert "2026-06" in marker_title
        assert "not analysed" in marker_title.lower()

    def test_error_month_is_not_drawn_as_a_real_data_point(self, page: Page, crm_base_url):
        _open_relationship_page(
            page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID,
            tone_response=_TONE_RESPONSE_WITH_ERROR_MONTH,
        )
        page.wait_for_timeout(300)

        titles = page.locator(".tone-data-point title").all_text_contents()
        assert not any("2026-06" in t for t in titles), (
            f"error month plotted as a real data point: {titles}"
        )
        # The two normal months on either side of it must still render.
        assert any("2026-05" in t for t in titles)
        assert any("2026-07" in t for t in titles)


# A tone-analysis-detailed response with one stale month (a real, stored
# score the server couldn't refresh this load) between two fresh ones.
_TONE_RESPONSE_WITH_STALE_MONTH = {
    "monthly_tones": [
        {"month": "2026-05", "user_score": 80.0, "partner_score": 80.0,
         "combined_score": 80.0, "user_sample_count": 5, "partner_sample_count": 5,
         "status": None},
        {"month": "2026-06", "user_score": 62.0, "partner_score": 58.0,
         "combined_score": 60.0, "user_sample_count": 4, "partner_sample_count": 4,
         "status": "stale"},
        {"month": "2026-07", "user_score": 20.0, "partner_score": 20.0,
         "combined_score": 20.0, "user_sample_count": 5, "partner_sample_count": 5,
         "status": None},
    ],
    "user_trend": "declining", "partner_trend": "declining", "combined_trend": "declining",
    "user_average": 54.0, "partner_average": 52.7, "generated_at": "2026-07-15T00:00:00+00:00",
}


class TestToneChartStaleMonth:
    """A stale month whose recompute failed must still be plotted with its
    real, stored score (never discarded), but rendered distinctly from a
    fresh month -- dimmed, rather than indistinguishable from current
    data."""

    def test_stale_month_is_plotted_with_its_real_score_dimmed(self, page: Page, crm_base_url):
        _open_relationship_page(
            page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID,
            tone_response=_TONE_RESPONSE_WITH_STALE_MONTH,
        )
        page.wait_for_timeout(300)

        # Unlike an error month, a stale month IS a real data point -- no
        # "not analysed" gap marker for it.
        expect(page.locator(".tone-data-point-error")).to_have_count(0)

        stale_points = page.locator(".tone-data-point-stale")
        expect(stale_points).to_have_count(1)

        stale_title = stale_points.first.locator("title").text_content()
        assert "2026-06" in stale_title
        assert "(stale)" in stale_title.lower()

        # Dimmed relative to a normal point.
        stale_opacity = stale_points.first.get_attribute("opacity")
        assert stale_opacity is not None
        assert float(stale_opacity) < 1.0

        # The two fresh months on either side render as normal, full-opacity
        # points with no "(stale)" suffix.
        normal_points = page.locator(".tone-data-point:not(.tone-data-point-stale)")
        expect(normal_points).to_have_count(2)
        normal_titles = normal_points.all_text_contents()
        assert not any("stale" in t.lower() for t in normal_titles)


class TestToneChartMonthLabel:
    """`new Date(t.month + '-01')` parses "YYYY-MM-01" as UTC midnight, and
    `toLocaleDateString` then renders in the browser's local timezone --
    west of UTC that rolls back to the last day of the *previous* month
    (e.g. "2026-04" rendered as "Mar"), so the month label must compensate.
    Verified with an explicit negative-UTC-offset timezone so the test
    actually exercises this instead of happening to pass under a UTC
    host."""

    def test_month_label_matches_the_month_key_in_a_negative_utc_offset_timezone(
        self, browser: Browser, crm_base_url,
    ):
        context = browser.new_context(timezone_id="America/Los_Angeles")
        try:
            page = context.new_page()
            tone_response = {
                "monthly_tones": [
                    {"month": "2026-04", "user_score": 70.0, "partner_score": 70.0,
                     "combined_score": 70.0, "user_sample_count": 3, "partner_sample_count": 3,
                     "status": None},
                ],
                "user_trend": "stable-neutral", "partner_trend": "stable-neutral",
                "combined_trend": "stable-neutral", "user_average": 70.0, "partner_average": 70.0,
                "generated_at": "2026-04-15T00:00:00+00:00",
            }
            _open_relationship_page(
                page, crm_base_url, partner_person_id=SYNTHETIC_PARTNER_ID,
                tone_response=tone_response,
            )
            page.wait_for_timeout(300)

            label_texts = page.locator("#toneTimelineViz svg text").all_text_contents()
            assert "Apr" in label_texts, f"expected an 'Apr' label for 2026-04, got: {label_texts}"
            assert "Mar" not in label_texts, (
                f"month label off-by-one regression: rendered 'Mar' for 2026-04 in a "
                f"negative-UTC-offset timezone: {label_texts}"
            )
        finally:
            context.close()
