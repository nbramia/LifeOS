"""Tests that the web app manifest and its icons are served correctly (#727,
extended by #731 for the designed icon set + maskable variant).

A Home Screen shortcut on iOS only opens standalone when the manifest is
reachable at the URL the HTML pages actually link, with a content type
browsers honor (`application/manifest+json` — not the OS-dependent guess
StaticFiles would make for a bare .webmanifest extension under /static),
and with `display: standalone` plus a `start_url` that resolves to a real
route rather than the /static prefix.
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    from api.main import app
    return TestClient(app)


class TestManifestServing:
    def test_manifest_route_returns_200(self, client):
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200

    def test_manifest_content_type_is_manifest_json(self, client):
        # Some browsers ignore a manifest served with the wrong content
        # type — this is the whole reason the route sets media_type
        # explicitly instead of relying on the /static mount.
        response = client.get("/manifest.webmanifest")
        assert response.headers["content-type"] == "application/manifest+json"

    def test_manifest_parses_and_declares_standalone(self, client):
        response = client.get("/manifest.webmanifest")
        manifest = response.json()
        assert manifest["display"] == "standalone"
        assert manifest["name"] == "LifeOS"
        assert manifest["short_name"]

    def test_manifest_start_url_is_a_real_route_not_static(self, client):
        response = client.get("/manifest.webmanifest")
        manifest = response.json()
        start_url = manifest["start_url"]
        assert not start_url.startswith("/static")
        # The route itself must exist and serve the chat SPA.
        page = client.get(start_url)
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]

    def test_manifest_scope_is_a_real_route(self, client):
        response = client.get("/manifest.webmanifest")
        manifest = response.json()
        scope = manifest["scope"]
        assert not scope.startswith("/static")
        page = client.get(scope)
        assert page.status_code == 200


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

    def test_manifest_icons_all_resolve(self, client):
        manifest = client.get("/manifest.webmanifest").json()
        assert len(manifest["icons"]) >= 2
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in manifest["icons"]:
            resp = client.get(icon["src"])
            assert resp.status_code == 200
            assert resp.headers["content-type"] == icon["type"]

    def test_manifest_declares_a_maskable_icon(self, client):
        # Android adaptive icons crop to a circle/squircle/rounded-square mask
        # of the launcher's choosing -- without a "maskable" entry, Android
        # instead applies that mask to the plain "any" icon, which has no
        # padding for it and gets clipped (#731).
        manifest = client.get("/manifest.webmanifest").json()
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
        # cleanly (#731).
        response = client.get("/static/icons/apple-touch-icon.png")
        img = Image.open(io.BytesIO(response.content))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            alpha = img.convert("RGBA").split()[3]
            assert alpha.getextrema() == (255, 255), "apple-touch-icon must have no transparent/translucent pixels"
        else:
            assert img.mode in ("RGB", "L"), f"unexpected opaque-icon mode: {img.mode}"


class TestServedPagesLinkManifestAndDeclareStandalone:
    @pytest.mark.parametrize("path", ["/", "/chat", "/crm"])
    def test_page_links_manifest_and_apple_touch_icon(self, client, path):
        html = client.get(path).text
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">' in html

    @pytest.mark.parametrize("path", ["/", "/chat", "/crm"])
    def test_page_declares_standalone_capable(self, client, path):
        html = client.get(path).text
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html
