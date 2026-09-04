"""Browser tests for people-list avatars.

The list response threads `has_profile_photo` through, and the list's
avatar loader renders a real `<img>` only for flagged people — no probing
loop, no per-person fetch.

Unlike most of the browser suite this serves `web/` itself from an ephemeral
port rather than pointing at a running API, and — because an avatar loads via
a plain `<img src>` rather than `fetch()` — intercepts requests with
Playwright's `page.route()` (which covers image loads) instead of the
`window.fetch` monkey-patch `test_crm_request_hygiene_ui_browser.py` uses for
its abort-timing tests; nothing here needs real concurrency, just to observe
which URLs the browser actually requests. Carries no `requires_server`
marker, so it runs at pre-push (`browser and not requires_server`).

A note on "no console errors": Chromium logs "Failed to load resource: ...
404" to the DevTools console for *any* request (image, fetch, XHR) that
completes with a non-2xx status, regardless of whether the page's JS handles
it — confirmed empirically here, including through `page.route()` mocks. That
browser-level notice cannot be suppressed by application code short of never
making the request in the first place, so the "photo not found" case here
(PERSON_MISSING_THUMB) does trigger it. `_app_console_errors()` filters that
one known, unavoidable browser notice out and asserts nothing else was
logged, which is the meaningful signal: no *application* error (an uncaught
exception, or our own code logging one) resulted from a missing thumbnail.
`page.on("pageerror")` (uncaught JS exceptions) is checked directly with no
filtering, matching the rest of this suite's convention.
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Obviously synthetic people — never real CRM data.
PERSON_HAS_PHOTO = {
    "id": "person-has-photo", "canonical_name": "Has Photo Person",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 10,
    "company": "", "tags": [], "has_profile_photo": True,
}
PERSON_MISSING_THUMB = {
    "id": "person-missing-thumb", "canonical_name": "Missing Thumb Person",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 8,
    "company": "", "tags": [], "has_profile_photo": True,
}
PERSON_NO_PHOTO = {
    "id": "person-no-photo", "canonical_name": "No Photo Person",
    "category": "personal", "dunbar_circle": 3, "relationship_strength": 5,
    "company": "", "tags": [], "has_profile_photo": False,
}

# A real, decodable 2x2 JPEG (generated once with Pillow at authoring
# time -- Pillow itself is not a test-time dependency, these are just
# static bytes). Content doesn't matter, but it must actually decode so
# the <img> fires onload rather than onerror: a decode failure on a 2xx
# response is silent to the console, so a hand-rolled/truncated JPEG here
# would make the "has-photo" assertion flaky with no console error to
# flag it.
FAKE_JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x02\x00\x02\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xb1E\x14Wa\xc8\x7f\xff\xd9'


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


def _open_me_with_stubs(page: Page, crm_base_url, *, people, photos_enabled=True,
                         photo_status_for=None):
    """Loads /me with every `/api/**` call stubbed: the people list, the
    boilerplate config/statistics/birthdays calls init() also fires, and the
    profile photo route itself.

    `photos_enabled` is served from `/api/crm/config`, piggybacking on the
    config request `init()` already awaits rather than a separate
    `/api/photos/stats` round trip.

    `photo_status_for(url) -> int | None` picks the status for a photo
    request; the default (None) fulfills `person-has-photo` with 200 and
    everything else with 404, matching the original 3-person fixture. A
    caller with many flagged people (see `TestLazyLoading`) can override it
    to fulfill every request with 200 instead.

    Returns (photo_requests, photo_methods, console_errors, page_errors):
    the URLs and HTTP methods of every `/api/photos/profile/*` request the
    page actually issued, so a caller can assert both "which people" and
    "never HEAD" from the same load."""
    photo_requests = []
    photo_methods = []

    def _default_photo_status(url):
        return 200 if "person-has-photo" in url else 404

    status_for = photo_status_for or _default_photo_status

    def handler(route):
        url = route.request.url
        if "/api/photos/profile/" in url:
            photo_requests.append(url)
            photo_methods.append(route.request.method)
            status = status_for(url)
            if status == 200:
                route.fulfill(status=200, content_type="image/jpeg", body=FAKE_JPEG_BYTES)
            else:
                route.fulfill(status=status, content_type="application/json",
                              body=json.dumps({"detail": "not found"}))
            return
        if "/api/crm/people?" in url or url.rstrip("/").endswith("/api/crm/people"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"people": people, "total": len(people),
                                            "offset": 0, "count": len(people)}))
            return
        if "/api/crm/config" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"photos_enabled": photos_enabled}))
            return
        if "/api/crm/statistics" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"total_people": len(people)}))
            return
        if "/api/crm/birthdays/today" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"birthdays": []}))
            return
        # Anything else this page might request on load: empty success.
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handler)

    console_errors = []
    page_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{crm_base_url}/me")
    page.wait_for_selector("#searchInput")
    expect(page.locator(".person-card")).to_have_count(len(people))
    # Let onload/onerror settle for the flagged people's <img> elements.
    page.wait_for_timeout(300)

    return photo_requests, photo_methods, console_errors, page_errors


def _app_console_errors(console_errors):
    """Filters out Chromium's unavoidable "Failed to load resource" notice
    for a non-2xx response (see module docstring) so what's left is
    application-level noise only."""
    return [e for e in console_errors if "Failed to load resource" not in e]


def _avatar(page: Page, person_id: str):
    return page.locator(f'.person-avatar[data-person-id="{person_id}"]')


class TestAvatarRendering:
    """Only flagged people get a photo request; never a HEAD probe; a
    missing thumbnail falls back to initials without an application error."""

    def test_exactly_two_photo_requests_for_flagged_people(self, page: Page, crm_base_url):
        photo_requests, photo_methods, console_errors, page_errors = _open_me_with_stubs(
            page, crm_base_url,
            people=[PERSON_HAS_PHOTO, PERSON_MISSING_THUMB, PERSON_NO_PHOTO],
        )

        assert len(photo_requests) == 2, f"expected exactly 2 photo requests, got: {photo_requests}"
        assert any("person-has-photo" in u for u in photo_requests)
        assert any("person-missing-thumb" in u for u in photo_requests)
        assert not any("person-no-photo" in u for u in photo_requests)

        assert page_errors == []
        assert _app_console_errors(console_errors) == []

    def test_no_head_requests_are_ever_issued(self, page: Page, crm_base_url):
        _photo_requests, photo_methods, _console_errors, _page_errors = _open_me_with_stubs(
            page, crm_base_url, people=[PERSON_HAS_PHOTO, PERSON_NO_PHOTO],
        )
        assert photo_methods, "expected at least one photo request"
        assert all(m == "GET" for m in photo_methods), f"expected only GET, got: {photo_methods}"

    def test_avatar_classes_reflect_photo_availability(self, page: Page, crm_base_url):
        _open_me_with_stubs(
            page, crm_base_url,
            people=[PERSON_HAS_PHOTO, PERSON_MISSING_THUMB, PERSON_NO_PHOTO],
        )

        expect(_avatar(page, "person-has-photo")).to_have_class("person-avatar has-photo")
        expect(_avatar(page, "person-has-photo").locator("img")).to_have_count(1)
        # AC 6 ("images load lazily"): mutation-checked -- deleting
        # loading="lazy" from renderAvatarPhotoImg() must fail this.
        expect(_avatar(page, "person-has-photo").locator("img")).to_have_attribute("loading", "lazy")

        # The 404 flips the optimistic has-photo render back to no-photo and
        # removes the broken <img>, leaving initials visible.
        expect(_avatar(page, "person-missing-thumb")).to_have_class("person-avatar no-photo")
        expect(_avatar(page, "person-missing-thumb").locator("img")).to_have_count(0)
        assert _avatar(page, "person-missing-thumb").inner_text().strip() != ""

        expect(_avatar(page, "person-no-photo")).to_have_class("person-avatar no-photo")
        expect(_avatar(page, "person-no-photo").locator("img")).to_have_count(0)


class TestPhotosDisabled:
    """When Photos isn't configured at all, the list must not request any
    avatar image, even for people the list flags as having one."""

    def test_photos_disabled_issues_zero_photo_requests(self, page: Page, crm_base_url):
        photo_requests, _photo_methods, console_errors, page_errors = _open_me_with_stubs(
            page, crm_base_url,
            people=[PERSON_HAS_PHOTO, PERSON_MISSING_THUMB, PERSON_NO_PHOTO],
            photos_enabled=False,
        )

        assert photo_requests == []
        assert page_errors == []
        assert _app_console_errors(console_errors) == []

        for person in (PERSON_HAS_PHOTO, PERSON_MISSING_THUMB, PERSON_NO_PHOTO):
            expect(_avatar(page, person["id"])).to_have_class("person-avatar no-photo")
            expect(_avatar(page, person["id"]).locator("img")).to_have_count(0)


class TestLazyLoadingAtScale:
    """AC 6: rendering 300 flagged people must not fire 300 image requests
    on first paint -- `loading="lazy"` is supposed to defer the ones outside
    (or well past) the viewport until scrolled into view. This is the
    mutation-check the `has-photo` test's single `to_have_attribute` can't
    provide on its own: deleting `loading="lazy"` leaves that assertion's
    target element correct in isolation, but only this test actually proves
    the browser behaves differently because of it -- with the attribute
    removed, all 300 requests fire immediately instead of a small fraction."""

    def test_300_flagged_rows_request_far_fewer_images_at_first_paint(
        self, page: Page, crm_base_url
    ):
        people = [
            {
                "id": f"person-{i}", "canonical_name": f"Person {i}",
                "category": "personal", "dunbar_circle": 3,
                "relationship_strength": 300 - i,
                "company": "", "tags": [], "has_profile_photo": True,
            }
            for i in range(300)
        ]

        photo_requests, _photo_methods, _console_errors, _page_errors = _open_me_with_stubs(
            page, crm_base_url, people=people,
            photo_status_for=lambda _url: 200,
        )

        assert len(people) == 300
        assert 0 < len(photo_requests) < len(people) / 2, (
            f"expected well under {len(people)} image requests at first paint "
            f"(loading=\"lazy\" should defer most off-screen rows), got "
            f"{len(photo_requests)}"
        )
