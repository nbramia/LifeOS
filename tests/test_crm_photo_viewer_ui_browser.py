"""Browser tests for the CRM photo viewer (web/crm.html).

Clicking a photo thumbnail in a person's timeline opens an in-page viewer: the
thumbnail at once, then a larger preview from the Photos Mac when on-demand
originals are configured, with a "Download original" button whose state
reflects what is on the Mac.

Like test_crm_avatar_photos_ui_browser.py this serves ``web/`` from an
ephemeral port and stubs every ``/api/**`` request with Playwright's
``page.route()``; it carries no ``requires_server`` marker, so it runs in the
``browser-free`` lane.
"""
import base64
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# A decodable 1x1 PNG; the stubs serve it for every image request.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

UUID_1 = "AAAAAAAA-0000-4000-8000-000000000001"
UUID_2 = "BBBBBBBB-0000-4000-8000-000000000002"
UUID_VIDEO = "DDDDDDDD-0000-4000-8000-000000000004"
EVIL_UUID = 'x" onerror="window.__xss=1" data-x="'

INFO_AVAILABLE = {
    "available": True, "filename": "IMG_0001.HEIC", "size_bytes": 3_145_728,
    "kind": "photo", "previewable": True,
}


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


class Api:
    """Stubbed backend: per-UUID behaviour plus a record of what was requested."""

    def __init__(self, *, originals_enabled=True):
        self.originals_enabled = originals_enabled
        self.person_photos = [{"uuid": UUID_1, "timestamp": "2024-05-01T12:00:00+00:00"}]
        self.thumbnail_status = {}      # uuid -> 200 | 404 | 410
        self.info = {}                  # uuid -> dict | (status, headers)
        self.hold_previews = False      # park preview requests until released
        self.hold_info = set()          # uuids whose info request is parked
        self.parked_info = []
        self.preview_status = 200
        self.requests = []
        self.parked = []

    def paths(self, prefix):
        return [r for r in self.requests if r.startswith(prefix)]

    def handler(self, route):
        url = route.request.url
        path = url.split("//", 1)[1].split("/", 1)[1].split("?", 1)[0]
        path = "/" + path
        self.requests.append(path)

        def json_body(body, status=200, headers=None):
            route.fulfill(status=status, content_type="application/json",
                          headers=headers or {}, body=json.dumps(body))

        if path == "/api/crm/config":
            return json_body({"photos_enabled": True, "photos_originals_enabled": self.originals_enabled})
        if path.startswith("/api/photos/person/"):
            return json_body({"person_id": "p", "photos": self.person_photos,
                              "count": len(self.person_photos)})
        if path.startswith("/api/photos/thumbnail/"):
            uuid = path.rsplit("/", 1)[1]
            status = self.thumbnail_status.get(uuid, 200)
            if status == 200:
                return route.fulfill(status=200, content_type="image/png", body=PIXEL)
            return json_body({"detail": "x"}, status=status)
        if path.startswith("/api/photos/original/") and path.endswith("/info"):
            uuid = path.split("/")[4]
            if uuid in self.hold_info:
                self.parked_info.append(route)
                return
            entry = self.info.get(uuid, INFO_AVAILABLE)
            if isinstance(entry, tuple):
                status, headers = entry
                return json_body({"detail": "x"}, status=status, headers=headers)
            return json_body(entry)
        if path.startswith("/api/photos/original/"):
            return route.fulfill(
                status=200, content_type="image/heic", body=b"synthetic-original",
                headers={"Content-Disposition": 'attachment; filename="IMG_0001.HEIC"'},
            )
        if path.startswith("/api/photos/preview/"):
            if self.hold_previews:
                self.parked.append(route)
                return
            if self.preview_status != 200:
                return json_body({"detail": "x"}, status=self.preview_status)
            return route.fulfill(status=200, content_type="image/png", body=PIXEL)
        if path.startswith("/api/crm/people"):
            return json_body({"people": [], "total": 0, "offset": 0, "count": 0})
        if path.startswith("/api/crm/statistics"):
            return json_body({"total_people": 0})
        if path.startswith("/api/crm/birthdays/today"):
            return json_body({"birthdays": []})
        return json_body({})

    def release_previews(self):
        for route in self.parked:
            route.fulfill(status=200, content_type="image/png", body=PIXEL)
        self.parked = []


def open_page(page: Page, crm_base_url, api: Api):
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.route("**/api/**", api.handler)
    page.goto(f"{crm_base_url}/me")
    page.wait_for_selector("#searchInput")
    page.wait_for_function("typeof openPhotoViewer === 'function'")
    page.wait_for_timeout(200)
    return page_errors


def show_photo_group(page: Page, date="2024-05-01"):
    """Render a photo group into the page and expand it, the way a person's
    timeline does."""
    page.evaluate(
        """(date) => {
            selectedPersonId = 'person-1';
            const host = document.createElement('div');
            host.id = 'photoHost';
            host.innerHTML = renderPhotoGroup({date, items: [{timestamp: date + 'T12:00:00+00:00'}]});
            document.body.appendChild(host);
        }""",
        date,
    )
    page.locator(".photo-group-header").click()
    expect(page.locator(".photo-card")).not_to_have_count(0)


def viewer(page: Page):
    return page.locator("#photoViewer")


def img_stage(page: Page):
    return page.locator("#photoViewerImg").get_attribute("data-stage")


class TestPhotoCard:
    def test_card_offers_the_viewer_not_open_in_photos(self, page: Page, crm_base_url):
        api = Api()
        errors = open_page(page, crm_base_url, api)
        show_photo_group(page)

        expect(page.locator(".photo-view-btn")).to_have_text("View")
        assert "Open in Photos" not in page.content()
        assert api.paths("/api/photos/open") == []
        assert errors == []

    def test_clicking_the_thumbnail_opens_the_viewer(self, page: Page, crm_base_url):
        api = Api()
        open_page(page, crm_base_url, api)
        show_photo_group(page)

        page.locator("img.photo-thumbnail").click()
        expect(viewer(page)).to_be_visible()
        assert page.locator("#photoViewerImg").get_attribute("src") is not None

    def test_the_view_button_opens_the_viewer(self, page: Page, crm_base_url):
        api = Api()
        open_page(page, crm_base_url, api)
        show_photo_group(page)
        page.locator(".photo-view-btn").click()
        expect(viewer(page)).to_be_visible()

    def test_a_hostile_uuid_cannot_break_out_of_the_markup(self, page: Page, crm_base_url):
        api = Api()
        api.person_photos = [{"uuid": EVIL_UUID, "timestamp": "2024-05-01T12:00:00+00:00"}]
        errors = open_page(page, crm_base_url, api)
        show_photo_group(page)

        assert page.evaluate("window.__xss") is None
        assert page.locator("img.photo-thumbnail").get_attribute("data-uuid") == EVIL_UUID
        page.locator(".photo-view-btn").click()
        expect(viewer(page)).to_be_visible()
        assert page.evaluate("window.__xss") is None
        assert errors == []
        # The id travels percent-encoded in every URL.
        assert not any('"' in r for r in api.requests)


class TestViewerBehaviour:
    def test_thumbnail_first_then_preview(self, page: Page, crm_base_url):
        api = Api()
        api.hold_previews = True
        open_page(page, crm_base_url, api)
        show_photo_group(page)

        page.locator("img.photo-thumbnail").click()
        expect(viewer(page)).to_be_visible()
        assert img_stage(page) == "thumbnail"
        assert f"/api/photos/thumbnail/{UUID_1}" in page.locator("#photoViewerImg").get_attribute("src")
        page.wait_for_function("document.getElementById('photoViewerDownload').hidden === false")
        assert img_stage(page) == "thumbnail"

        api.release_previews()
        page.wait_for_function(
            "document.getElementById('photoViewerImg').dataset.stage === 'preview'"
        )
        assert f"/api/photos/preview/{UUID_1}" in page.locator("#photoViewerImg").get_attribute("src")

    def test_close_button_escape_and_backdrop_close_it(self, page: Page, crm_base_url):
        api = Api()
        open_page(page, crm_base_url, api)
        show_photo_group(page)
        opener = page.locator("img.photo-thumbnail")

        opener.click()
        expect(viewer(page)).to_be_visible()
        page.locator("#photoViewerClose").click()
        expect(viewer(page)).to_be_hidden()

        opener.click()
        expect(viewer(page)).to_be_visible()
        page.keyboard.press("Escape")
        expect(viewer(page)).to_be_hidden()

        opener.click()
        expect(viewer(page)).to_be_visible()
        page.mouse.click(5, 5)  # the dimmed backdrop, not the image
        expect(viewer(page)).to_be_hidden()

    def test_clicking_the_image_does_not_close_it(self, page: Page, crm_base_url):
        api = Api()
        open_page(page, crm_base_url, api)
        show_photo_group(page)
        page.locator("img.photo-thumbnail").click()
        page.locator("#photoViewerImg").click()
        expect(viewer(page)).to_be_visible()

    def test_a_late_preview_for_a_closed_photo_is_ignored(self, page: Page, crm_base_url):
        api = Api()
        api.hold_previews = True
        open_page(page, crm_base_url, api)
        page.evaluate(f"openPhotoViewer('{UUID_1}')")
        page.wait_for_function("document.getElementById('photoViewerDownload').hidden === false")
        page.evaluate("closePhotoViewer()")
        page.evaluate(f"openPhotoViewer('{UUID_2}')")
        page.wait_for_function(
            "document.getElementById('photoViewerDownload').hidden === false"
        )
        # Release both parked previews (photo 1's, then photo 2's).
        first = api.parked[0]
        first.fulfill(status=200, content_type="image/png", body=PIXEL)
        page.wait_for_timeout(300)
        assert img_stage(page) == "thumbnail"
        assert UUID_2 in page.locator("#photoViewerImg").get_attribute("src")


    def test_a_late_info_for_a_closed_photo_is_ignored(self, page: Page, crm_base_url):
        api = Api()
        api.hold_info = {UUID_1}
        open_page(page, crm_base_url, api)
        page.evaluate(f"openPhotoViewer('{UUID_1}')")
        page.evaluate("closePhotoViewer()")
        page.evaluate(f"openPhotoViewer('{UUID_2}')")
        expect(page.locator("#photoViewerDownload")).to_be_enabled()

        # Photo 1's info arrives late, saying its original is missing.
        api.parked_info[0].fulfill(
            status=200, content_type="application/json",
            body=json.dumps(dict(INFO_AVAILABLE, available=False)),
        )
        page.wait_for_timeout(300)
        expect(page.locator("#photoViewerDownload")).to_be_enabled()
        expect(page.locator("#photoViewerNote")).to_have_text("")
        assert page.locator("#photoViewerDownload").get_attribute("data-url").endswith(UUID_2)


    def test_a_late_failed_info_for_a_closed_photo_is_ignored(self, page: Page, crm_base_url):
        api = Api()
        api.hold_info = {UUID_1}
        open_page(page, crm_base_url, api)
        page.evaluate(f"openPhotoViewer('{UUID_1}')")
        page.evaluate("closePhotoViewer()")
        page.evaluate(f"openPhotoViewer('{UUID_2}')")
        expect(page.locator("#photoViewerDownload")).to_be_enabled()

        api.parked_info[0].fulfill(
            status=503, content_type="application/json",
            headers={"X-Photos-Origin": "unreachable"}, body='{"detail": "x"}',
        )
        page.wait_for_timeout(300)
        expect(page.locator("#photoViewerNote")).to_have_text("")


class TestOriginStates:
    def open_viewer(self, page, crm_base_url, api, uuid=UUID_1):
        errors = open_page(page, crm_base_url, api)
        page.evaluate(f"openPhotoViewer('{uuid}')")
        expect(viewer(page)).to_be_visible()
        return errors

    def test_available_original_enables_download(self, page: Page, crm_base_url):
        api = Api()
        self.open_viewer(page, crm_base_url, api)
        button = page.locator("#photoViewerDownload")
        expect(button).to_be_enabled()
        expect(button).to_contain_text("Download original")
        expect(button).to_contain_text("3.0 MB")
        expect(page.locator("#photoViewerNote")).to_have_text("")

        with page.expect_download() as download:
            button.click()
        assert download.value.suggested_filename == "IMG_0001.HEIC"
        assert api.paths(f"/api/photos/original/{UUID_1}") != []

    def test_original_missing_shows_preview_and_a_disabled_button(self, page: Page, crm_base_url):
        api = Api()
        api.info[UUID_1] = dict(INFO_AVAILABLE, available=False, size_bytes=None)
        self.open_viewer(page, crm_base_url, api)

        expect(page.locator("#photoViewerDownload")).to_be_disabled()
        expect(page.locator("#photoViewerNote")).to_have_text("Original isn't on the Mac")
        page.wait_for_function(
            "document.getElementById('photoViewerImg').dataset.stage === 'preview'"
        )

    def test_mac_offline_keeps_the_thumbnail_and_says_so(self, page: Page, crm_base_url):
        api = Api()
        api.info[UUID_1] = (503, {"X-Photos-Origin": "unreachable"})
        errors = self.open_viewer(page, crm_base_url, api)

        expect(page.locator("#photoViewerNote")).to_have_text("The Mac is offline")
        expect(page.locator("#photoViewerDownload")).to_be_hidden()
        assert img_stage(page) == "thumbnail"
        page.wait_for_timeout(200)
        assert api.paths("/api/photos/preview/") == []
        assert errors == []

    def test_not_in_the_mac_library_is_a_quiet_note(self, page: Page, crm_base_url):
        api = Api()
        api.info[UUID_1] = (404, {})
        self.open_viewer(page, crm_base_url, api)
        expect(page.locator("#photoViewerNote")).to_have_text("This photo isn't available on the Mac")
        expect(page.locator("#photoViewerDownload")).to_be_hidden()

    def test_unconfigured_server_response_is_thumbnail_only_without_error(
        self, page: Page, crm_base_url
    ):
        api = Api()
        api.info[UUID_1] = (503, {"X-Photos-Origin": "unconfigured"})
        errors = self.open_viewer(page, crm_base_url, api)
        page.wait_for_timeout(200)
        expect(page.locator("#photoViewerNote")).to_have_text("")
        expect(page.locator("#photoViewerDownload")).to_be_hidden()
        assert errors == []

    def test_originals_disabled_makes_no_origin_requests(self, page: Page, crm_base_url):
        api = Api(originals_enabled=False)
        self.open_viewer(page, crm_base_url, api)
        page.wait_for_timeout(300)
        assert api.paths("/api/photos/original/") == []
        assert api.paths("/api/photos/preview/") == []
        expect(page.locator("#photoViewerDownload")).to_be_hidden()
        expect(page.locator("#photoViewerNote")).to_have_text("")
        assert img_stage(page) == "thumbnail"

    def test_video_gets_a_download_but_no_preview(self, page: Page, crm_base_url):
        api = Api()
        api.info[UUID_VIDEO] = dict(INFO_AVAILABLE, kind="video", filename="MOV_0004.MOV")
        self.open_viewer(page, crm_base_url, api, uuid=UUID_VIDEO)
        expect(page.locator("#photoViewerDownload")).to_be_enabled()
        page.wait_for_timeout(300)
        assert api.paths("/api/photos/preview/") == []
        assert img_stage(page) == "thumbnail"
        assert page.locator("video").count() == 0

    def test_failed_preview_keeps_the_thumbnail(self, page: Page, crm_base_url):
        api = Api()
        api.preview_status = 404
        errors = self.open_viewer(page, crm_base_url, api)
        page.wait_for_timeout(300)
        assert img_stage(page) == "thumbnail"
        expect(viewer(page)).to_be_visible()
        assert errors == []


class TestPlaceholders:
    def test_missing_thumbnail_becomes_a_camera_placeholder_that_opens_the_viewer(
        self, page: Page, crm_base_url
    ):
        api = Api()
        api.thumbnail_status[UUID_1] = 404
        errors = open_page(page, crm_base_url, api)
        show_photo_group(page)

        placeholder = page.locator(".photo-thumbnail-placeholder")
        expect(placeholder).to_have_count(1)
        expect(placeholder).not_to_have_class("photo-thumbnail-placeholder icloud-photo")
        placeholder.click()
        expect(viewer(page)).to_be_visible()
        # The thumbnail is missing, but a preview from the Mac still shows.
        page.wait_for_function(
            "document.getElementById('photoViewerImg').dataset.stage === 'preview'"
        )
        assert errors == []

    def test_icloud_only_thumbnail_keeps_its_placeholder(self, page: Page, crm_base_url):
        api = Api()
        api.thumbnail_status[UUID_1] = 410
        open_page(page, crm_base_url, api)
        show_photo_group(page)
        expect(page.locator(".photo-thumbnail-placeholder.icloud-photo")).to_have_count(1)

    def test_placeholder_is_inert_without_originals(self, page: Page, crm_base_url):
        api = Api(originals_enabled=False)
        api.thumbnail_status[UUID_1] = 404
        open_page(page, crm_base_url, api)
        show_photo_group(page)
        page.locator(".photo-thumbnail-placeholder").click()
        expect(viewer(page)).to_be_hidden()
