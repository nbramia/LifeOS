"""Tests that each page's web app manifest and the shared icon set are
served correctly.

A Home Screen shortcut on iOS only opens standalone when the manifest is
reachable at the URL the HTML pages actually link, with a content type
browsers honor (`application/manifest+json` — not the OS-dependent guess
StaticFiles would make for a bare .webmanifest extension under /static),
and with `display: standalone` plus a `start_url` that resolves to a real
route rather than the /static prefix. Each page carries its own manifest:
an installed icon opens its manifest's `start_url` under its manifest's
name, so one manifest shared across pages would give every icon the same
destination and the same label.
"""
import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

pytestmark = pytest.mark.unit

# The route each page is served at -> its own manifest, and the short label
# iOS puts under its Home Screen icon.
PAGES = {
    "/": ("/manifests/home.webmanifest", "LifeOS"),
    "/chat": ("/manifests/chat.webmanifest", "Chat"),
    "/crm": ("/manifests/crm.webmanifest", "CRM"),
    "/agents": ("/manifests/agents.webmanifest", "Agents"),
    "/journal": ("/manifests/journal.webmanifest", "Journal"),
    "/journal/trends": ("/manifests/journal-trends.webmanifest", "Trends"),
}


@pytest.fixture(scope="module")
def client():
    from api.main import app
    return TestClient(app)


def _head_tag(html: str, tag: str, attribute: str, value: str) -> bool:
    """Whether the page declares `<tag ... attribute="value" ...>`.

    Matched by pattern rather than exact string because the pages differ in
    attribute order, indentation, and whether they self-close their tags.
    """
    pattern = rf'<{tag}[^>]*\b{attribute}="{re.escape(value)}"'
    return re.search(pattern, html) is not None


class TestManifestServing:
    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_returns_200_with_manifest_content_type(self, client, route):
        # Some browsers ignore a manifest served with the wrong content
        # type — this is the whole reason the route sets media_type
        # explicitly instead of relying on the /static mount.
        response = client.get(PAGES[route][0])
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/manifest+json"

    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_declares_standalone_and_a_name(self, client, route):
        manifest = client.get(PAGES[route][0]).json()
        assert manifest["display"] == "standalone"
        assert manifest["name"]
        assert manifest["short_name"]

    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_start_url_is_its_own_page(self, client, route):
        manifest = client.get(PAGES[route][0]).json()
        assert manifest["start_url"] == route
        # `id` is what the OS keys an installed app on; leaving it to default
        # would tie an app's identity to a `start_url` that may later move.
        assert manifest["id"] == route
        page = client.get(route)
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]

    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_scope_is_a_real_route(self, client, route):
        manifest = client.get(PAGES[route][0]).json()
        scope = manifest["scope"]
        assert not scope.startswith("/static")
        assert client.get(scope).status_code == 200

    def test_installed_pages_are_distinguishable_from_each_other(self, client):
        """Every page installs the same LifeOS mark, so the manifest name is
        the only thing telling two icons apart on a Home Screen."""
        manifests = [client.get(href).json() for href, _ in PAGES.values()]
        names = [m["name"] for m in manifests]
        short_names = [m["short_name"] for m in manifests]
        assert len(set(names)) == len(names), names
        assert len(set(short_names)) == len(short_names), short_names

    def test_chat_manifest_is_also_served_at_the_bare_path(self, client):
        """Home Screen icons installed from `/manifest.webmanifest` keep
        fetching it."""
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/manifest+json"
        assert response.json() == client.get("/manifests/chat.webmanifest").json()

    @pytest.mark.parametrize("slug", ["nope", "Chat", "..%2Fmanifest"])
    def test_unknown_manifest_is_not_found(self, client, slug):
        assert client.get(f"/manifests/{slug}.webmanifest").status_code == 404


class TestIconServing:
    @pytest.mark.parametrize("path", [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/icons/icon-maskable-512.png",
    ])
    def test_icon_returns_200_with_png_content_type(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_icons_all_resolve(self, client, route):
        manifest = client.get(PAGES[route][0]).json()
        assert len(manifest["icons"]) >= 2
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in manifest["icons"]:
            resp = client.get(icon["src"])
            assert resp.status_code == 200
            assert resp.headers["content-type"] == icon["type"]

    @pytest.mark.parametrize("route", list(PAGES))
    def test_manifest_declares_a_maskable_icon(self, client, route):
        # Android adaptive icons crop to a circle/squircle/rounded-square mask
        # of the launcher's choosing -- without a "maskable" entry, Android
        # instead applies that mask to the plain "any" icon, which has no
        # padding for it and gets clipped.
        manifest = client.get(PAGES[route][0]).json()
        maskable = [icon for icon in manifest["icons"] if icon.get("purpose") == "maskable"]
        assert maskable, "manifest must declare at least one purpose=maskable icon"
        for icon in maskable:
            resp = client.get(icon["src"])
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/png"

    def test_apple_touch_icon_is_fully_opaque(self, client):
        # iOS composites this icon on its own opaque rounded rect with no
        # transparency of its own -- any alpha channel (even a
        # near-invisible anti-aliased edge left over from SVG rasterization)
        # would show through as a border artifact rather than being masked
        # cleanly.
        response = client.get("/static/icons/apple-touch-icon.png")
        img = Image.open(io.BytesIO(response.content))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            alpha = img.convert("RGBA").split()[3]
            assert alpha.getextrema() == (255, 255), "apple-touch-icon must have no transparent/translucent pixels"
        else:
            assert img.mode in ("RGB", "L"), f"unexpected opaque-icon mode: {img.mode}"


class TestServedPagesLinkManifestAndDeclareStandalone:
    @pytest.mark.parametrize("route", list(PAGES))
    def test_page_links_its_own_manifest_and_the_apple_touch_icon(self, client, route):
        # iOS reads `apple-touch-icon` for the Home Screen and never the SVG
        # favicon, so a page without one gets a generated letter tile.
        html = client.get(route).text
        assert _head_tag(html, "link", "href", PAGES[route][0])
        assert _head_tag(html, "link", "href", "/static/icons/apple-touch-icon.png")

    @pytest.mark.parametrize("route", list(PAGES))
    def test_page_declares_standalone_capable(self, client, route):
        html = client.get(route).text
        assert _head_tag(html, "meta", "name", "apple-mobile-web-app-capable")
        assert _head_tag(html, "meta", "name", "apple-mobile-web-app-status-bar-style")

    @pytest.mark.parametrize("route", list(PAGES))
    def test_page_labels_its_home_screen_icon(self, client, route):
        """Without this meta iOS labels the icon with the page's full
        `<title>`, which several of these are too long for."""
        html = client.get(route).text
        label = re.search(
            r'<meta name="apple-mobile-web-app-title" content="([^"]+)"', html,
        )
        assert label, f"{route} declares no apple-mobile-web-app-title"
        assert label.group(1) == PAGES[route][1]
