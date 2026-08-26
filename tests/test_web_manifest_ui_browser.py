"""Browser tests for the web app manifest + standalone metadata (#727).

`web/home.html` (`/`) lacked `apple-mobile-web-app-capable`, so a Home
Screen shortcut added from the root URL opened in the user's default
browser instead of its own standalone container. The fix adds that meta
(matching `web/index.html` and `web/crm.html`, which already had it) plus a
real web app manifest linked from all three served pages, since Apple
documents the legacy meta alone as deprecated in favor of the manifest's
`display` member.

Like tests/test_mode_pill_ui_browser.py, this serves `web/` itself from an
ephemeral port rather than pointing at a running API — the assertions are
about the markup/manifest in *this* checkout, not live API behavior. No
`requires_server` marker, so it runs at pre-push (`browser and not
requires_server`).
"""
import http.server
import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.slow]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Mimics api/main.py's routing: `/`, `/chat`, `/crm` are pages served
    from web/*.html, `/manifest.webmanifest` is served from web/ with the
    correct extension (so this test's plain file server assigns it a
    manifest-ish type too), and `/static/` maps straight onto web/."""

    _PAGES = {
        "/": "home.html",
        "/chat": "index.html",
        "/crm": "crm.html",
        # crm.html's own JS redirects a bare /crm load to /me (its default
        # dashboard) via window.location.replace — mirror api/main.py's
        # /me route so that redirect doesn't 404 against this test server.
        "/me": "crm.html",
    }

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in self._PAGES:
            return str(WEB_DIR / self._PAGES[path])
        if path == "/manifest.webmanifest":
            return str(WEB_DIR / "manifest.webmanifest")
        if path.startswith("/static/"):
            return str(WEB_DIR / path[len("/static/"):])
        return str(WEB_DIR / path.lstrip("/"))

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def site_base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _open(page: Page, base_url, path):
    """Stub every /api/ call so a page's own JS never depends on a running
    server (mirrors test_mode_pill_ui_browser.py's _open_chat)."""
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({})))
    page.goto(f"{base_url}{path}")


@pytest.mark.parametrize("path", ["/", "/chat", "/crm"])
class TestStandaloneMetaAndManifestLink:
    """Every served entry point must declare the same standalone capability
    and link the same manifest — that's the whole point of #727: all three
    pages should behave identically on a Home Screen shortcut."""

    def test_apple_mobile_web_app_capable(self, page: Page, site_base_url, path):
        _open(page, site_base_url, path)
        meta = page.locator('meta[name="apple-mobile-web-app-capable"]')
        expect(meta).to_have_attribute("content", "yes")

    def test_apple_mobile_web_app_status_bar_style(self, page: Page, site_base_url, path):
        _open(page, site_base_url, path)
        meta = page.locator('meta[name="apple-mobile-web-app-status-bar-style"]')
        expect(meta).to_have_count(1)

    def test_manifest_link_present(self, page: Page, site_base_url, path):
        _open(page, site_base_url, path)
        link = page.locator('link[rel="manifest"]')
        expect(link).to_have_attribute("href", "/manifest.webmanifest")

    def test_apple_touch_icon_link_present(self, page: Page, site_base_url, path):
        _open(page, site_base_url, path)
        link = page.locator('link[rel="apple-touch-icon"]')
        expect(link).to_have_attribute("href", "/static/icons/apple-touch-icon.png")


class TestManifestContent:
    """The manifest itself must parse and point at a real, standalone-ready
    route — not the /static prefix (#727's central trap)."""

    def test_manifest_parses_as_standalone(self, page: Page, site_base_url):
        _open(page, site_base_url, "/chat")
        manifest = page.evaluate(
            "async () => { const r = await fetch('/manifest.webmanifest'); return r.json(); }"
        )
        assert manifest["display"] == "standalone"
        assert manifest["name"]
        assert manifest["short_name"]

    def test_manifest_start_url_and_scope_are_real_routes(self, page: Page, site_base_url):
        _open(page, site_base_url, "/chat")
        manifest = page.evaluate(
            "async () => { const r = await fetch('/manifest.webmanifest'); return r.json(); }"
        )
        # start_url must land on the chat SPA as actually routed (/chat),
        # never the /static prefix.
        assert manifest["start_url"] == "/chat"
        assert not manifest["start_url"].startswith("/static")
        assert manifest["scope"] == "/"

    def test_manifest_icons_resolve_under_static(self, page: Page, site_base_url):
        _open(page, site_base_url, "/chat")
        manifest = page.evaluate(
            "async () => { const r = await fetch('/manifest.webmanifest'); return r.json(); }"
        )
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in manifest["icons"]:
            assert icon["src"].startswith("/static/icons/")
            resp = page.request.get(f"{site_base_url}{icon['src']}")
            assert resp.status == 200
            assert resp.headers.get("content-type", "").startswith("image/")
