"""Browser tests for the CRM "Log event" form, mainly at phone width.

Serves web/ from an ephemeral port and stubs window.fetch, so no server or
CRM data is involved. Runs at pre-push (`browser and not requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
PERSON_ID = "person-ana"
ME_ID = "person-me"
PHONE = {"width": 390, "height": 844}

PERSON = {
    "id": PERSON_ID, "canonical_name": "Ana Example", "category": "personal",
    "dunbar_circle": 3, "relationship_strength": 10, "company": "", "tags": [],
    "emails": [], "phone_numbers": [], "vault_contexts": [], "sources": [],
    "source_entities": [], "source_entity_count": 0, "relationships": [],
}
BEN = {"id": "person-ben", "canonical_name": "Ben O'Example", "company": "Acme"}
TIMELINE_EVENT = {
    "id": "i1", "timestamp": "2026-07-04T11:00:00-05:00", "source_type": "calendar",
    "title": "Coffee", "snippet": "Corner Cafe", "source_link": "", "source_badge": "📅",
}

_FETCH_STUB = """
(() => {
    const state = window.__state = { posts: [], gets: [], postStatus: %(post_status)d, postBody: %(post_body)s };
    const person = %(person)s, ben = %(ben)s, event = %(event)s, meId = %(me_id)s;
    const reply = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {
        status, headers: { 'Content-Type': 'application/json' },
    }));
    window.fetch = (input, init = {}) => {
        const url = typeof input === 'string' ? input : input.url;
        if (init.method === 'POST' && url.endsWith('/interactions')) {
            state.posts.push({ url, body: JSON.parse(init.body) });
            return reply(state.postBody, state.postStatus);
        }
        state.gets.push(url);
        if (url.includes('/api/crm/config')) return reply({ my_person_id: meId });
        if (url.includes('/timeline')) {
            const items = state.posts.length ? [event] : [];
            return reply({ items, count: items.length, has_more: false });
        }
        if (url.includes('/api/crm/people?q=be')) return reply({ people: [ben], total: 1, offset: 0, count: 1 });
        if (url.includes('/api/crm/people?')) return reply({ people: [person], total: 1, offset: 0, count: 1 });
        if (url.includes('/api/crm/people/')) {
            const id = decodeURIComponent(url.split('/api/crm/people/')[1].split(/[/?]/)[0]);
            return reply({ ...person, id });
        }
        return reply({ birthdays: [], people: [], total_people: 0 });
    };
})();
"""


class _CrmHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / "crm.html")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url_crm():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CrmHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def open_crm(browser: Browser, base_url_crm):
    contexts = []

    def _open(path=f"/crm/{PERSON_ID}", viewport=PHONE, post_status=201,
              post_body=None) -> Page:
        context = browser.new_context(viewport=viewport, timezone_id="America/Chicago", has_touch=True)
        contexts.append(context)
        page = context.new_page()
        page.add_init_script(_FETCH_STUB % {
            "post_status": post_status,
            "post_body": json.dumps(post_body or {"created": 1, "skipped": 0, "person_ids": [PERSON_ID]}),
            "person": json.dumps(PERSON), "ben": json.dumps(BEN),
            "event": json.dumps(TIMELINE_EVENT), "me_id": json.dumps(ME_ID),
        })
        page.goto(f"{base_url_crm}{path}")
        return page

    yield _open
    for context in contexts:
        context.close()


def _posts(page):
    return page.evaluate("window.__state.posts")


def _open_form(page):
    page.locator("#logEventBtn").click()
    expect(page.locator("#logEventModal")).to_be_visible()


def test_phone_button_floats_in_reach_and_form_fits(open_crm):
    page = open_crm()
    button = page.locator("#logEventBtn")
    expect(button).to_be_visible()
    box = button.bounding_box()
    assert box["y"] + box["height"] <= PHONE["height"]
    assert box["x"] + box["width"] <= PHONE["width"]
    assert box["height"] >= 44

    _open_form(page)

    expect(page.locator("#logEventHeader")).to_have_text("Log event with Ana Example")
    page.locator("#logEventForm").evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
    sheet = page.locator("#logEventForm").bounding_box()
    assert sheet["width"] == PHONE["width"]
    assert sheet["y"] + sheet["height"] == pytest.approx(PHONE["height"], abs=1)
    for field in ["Title", "Location", "Start", "End", "Notes", "Account", "AttendeeSearch"]:
        locator = page.locator(f"#logEvent{field}")
        expect(locator).to_be_visible()
        assert locator.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 16
        assert locator.evaluate("el => el.getBoundingClientRect().right") <= PHONE["width"]
    expect(page.locator("#logEventAccount")).to_have_value("manual")
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_all_fields_are_posted_with_the_local_offset(open_crm):
    page = open_crm()
    _open_form(page)
    page.locator("#logEventTitle").fill("Coffee")
    page.locator("#logEventStart").fill("2026-11-07T11:00")
    page.locator("#logEventEnd").fill("2026-11-07T12:30")
    page.locator("#logEventLocation").fill("Corner Cafe")
    page.locator("#logEventNotes").fill("Talked about the move")
    page.locator("#logEventAccount").fill("partiful")

    search = page.locator("#logEventAttendeeSearch")
    search.fill("ben")
    page.locator(".log-event-result", has_text="Ben O'Example").click()
    search.fill("Cal Outsider")
    search.press("Enter")
    expect(page.locator(".log-event-chip")).to_have_count(2)

    page.locator("#logEventSave").click()
    expect(page.locator("#logEventModal")).to_be_hidden()

    posts = _posts(page)
    assert len(posts) == 1
    assert posts[0]["url"] == f"/api/crm/people/{PERSON_ID}/interactions"
    assert posts[0]["body"] == {
        "title": "Coffee",
        "timestamp": "2026-11-07T11:00:00-06:00",
        "end_time": "2026-11-07T12:30:00-06:00",
        "location": "Corner Cafe",
        "notes": "Talked about the move",
        "source_account": "partiful",
        "attendee_person_ids": ["person-ben"],
        "attendee_names": ["Cal Outsider"],
    }


def test_daylight_time_date_uses_that_dates_offset(open_crm):
    page = open_crm()
    _open_form(page)
    page.locator("#logEventTitle").fill("Coffee")
    page.locator("#logEventStart").fill("2026-07-04T11:00")
    page.locator("#logEventSave").click()
    expect(page.locator("#logEventModal")).to_be_hidden()

    body = _posts(page)[0]["body"]
    assert body["timestamp"] == "2026-07-04T11:00:00-05:00"
    assert body["end_time"] is None
    assert body["source_account"] == "manual"


def test_saved_event_appears_on_the_open_timeline(open_crm):
    page = open_crm(path=f"/crm/{PERSON_ID}/timeline")
    expect(page.locator("#tabTimeline")).to_be_visible()
    _open_form(page)
    page.locator("#logEventTitle").fill("Coffee")
    page.locator("#logEventSave").click()

    expect(page.locator("#timelineContainer")).to_contain_text("Coffee")
    expect(page.locator("#timelineContainer")).to_contain_text("Corner Cafe")


@pytest.mark.parametrize(
    "title,end,error",
    [
        ("", "", "Add a title."),
        ("Coffee", "2026-11-07T10:00", "End time must be after the start time."),
    ],
)
def test_invalid_input_stops_before_posting(open_crm, title, end, error):
    page = open_crm()
    _open_form(page)
    page.locator("#logEventStart").fill("2026-11-07T11:00")
    page.locator("#logEventTitle").fill(title)
    page.locator("#logEventEnd").fill(end)
    page.locator("#logEventSave").click()

    expect(page.locator("#logEventError")).to_have_text(error)
    expect(page.locator("#logEventModal")).to_be_visible()
    assert _posts(page) == []


def test_server_rejection_keeps_the_entry_open_and_shows_why(open_crm):
    page = open_crm(post_status=409, post_body={"detail": "This event is already recorded"})
    _open_form(page)
    page.locator("#logEventTitle").fill("Coffee")
    page.locator("#logEventSave").click()

    expect(page.locator("#logEventError")).to_have_text("This event is already recorded")
    expect(page.locator("#logEventTitle")).to_have_value("Coffee")
    expect(page.locator("#logEventSave")).to_be_enabled()


def test_cancel_discards_and_reopen_starts_clean(open_crm):
    page = open_crm()
    _open_form(page)
    page.locator("#logEventTitle").fill("Half typed")
    page.get_by_role("button", name="Cancel", exact=True).locator("visible=true").click()
    expect(page.locator("#logEventModal")).to_be_hidden()

    _open_form(page)
    expect(page.locator("#logEventTitle")).to_have_value("")
    assert _posts(page) == []


def test_button_is_hidden_on_my_own_page(open_crm):
    page = open_crm(path="/me")
    expect(page.locator("#detailContent")).to_be_visible()
    page.wait_for_timeout(300)
    expect(page.locator("#logEventBtn")).to_be_hidden()


def test_desktop_button_sits_in_the_header(open_crm):
    page = open_crm(viewport={"width": 1280, "height": 800})
    button = page.locator("#logEventBtn")
    expect(button).to_be_visible()
    assert button.evaluate("el => getComputedStyle(el).position") == "static"
    assert button.evaluate("el => !!el.closest('.detail-header')")
