"""
Tests for the profile photo endpoint (GET/HEAD /api/photos/profile/{person_id}).

The CRM people list needs to know, and then fetch, which people have a
usable avatar without probing every row with a request method the route
doesn't accept. These tests pin the route-level contract that makes that
possible: HEAD behaves like GET (status, no body), and both 200 and 404
responses carry a cache header the client can rely on to avoid repeat
requests.
"""
from datetime import datetime, timezone
from unittest.mock import patch, PropertyMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


@pytest.fixture
def photos_enabled():
    """The route gates on `settings.photos_enabled`, a property derived from
    whether the Photos library file exists on disk -- force it on so the
    handler itself (not just the disabled short-circuit) gets exercised."""
    from config.settings import Settings
    with patch.object(
        Settings, "photos_enabled", new_callable=PropertyMock, return_value=True
    ):
        yield


def _fake_photo_interaction(source_id="FAKE-UUID-0001"):
    from api.services.interaction_store import Interaction
    return Interaction(
        id="interaction-1",
        person_id="person-1",
        timestamp=datetime.now(timezone.utc),
        source_type="photos",
        title="synthetic test photo",
        source_id=source_id,
    )


class TestProfilePhotoDisabled:
    """Photos not configured at all: the route's short-circuit runs before
    HEAD/GET are distinguished, so both must carry the same status."""

    def test_head_matches_get_status_and_has_no_body(self, client):
        get_resp = client.get("/api/photos/profile/some-person")
        head_resp = client.request("HEAD", "/api/photos/profile/some-person")

        assert head_resp.status_code == get_resp.status_code == 503
        assert head_resp.content == b""


class TestProfilePhotoAvailable:
    """A person with a photo interaction whose thumbnail file exists."""

    def test_get_returns_image_body(self, client, photos_enabled, tmp_path):
        thumb = tmp_path / "thumb.jpeg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

        with patch("api.services.interaction_store.get_interaction_store") as mock_store, \
             patch("api.routes.photos._get_thumbnail_path", return_value=thumb):
            mock_store.return_value.get_for_person.return_value = [_fake_photo_interaction()]
            response = client.get("/api/photos/profile/person-1")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8\xff\xe0fake-jpeg-bytes"
        assert response.headers["content-type"] == "image/jpeg"

    def test_head_returns_200_with_no_body_and_full_header_parity_with_get(
        self, client, photos_enabled, tmp_path
    ):
        """HEAD must return status and headers matching what GET would send
        for the same resource (RFC 9110 SS9.3.2), including `etag`,
        `last-modified`, `accept-ranges`, and the real `content-length` --
        not just status and `cache-control` -- so a client can use HEAD to
        revalidate a cached avatar. The route stacks `@router.get`/
        `@router.head` on the same function so Starlette's `FileResponse`
        handles HEAD natively, giving that header parity for free."""
        thumb = tmp_path / "thumb.jpeg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

        with patch("api.services.interaction_store.get_interaction_store") as mock_store, \
             patch("api.routes.photos._get_thumbnail_path", return_value=thumb):
            mock_store.return_value.get_for_person.return_value = [_fake_photo_interaction()]
            head_resp = client.request("HEAD", "/api/photos/profile/person-1")
            get_resp = client.get("/api/photos/profile/person-1")

        assert head_resp.status_code == get_resp.status_code == 200
        assert head_resp.content == b""
        assert get_resp.content  # GET actually carries the body

        # Headers that must be identical between HEAD and GET (date/server
        # excluded -- not part of this route's contract, and legitimately
        # timing-dependent).
        for header in ("content-length", "content-type", "cache-control", "etag", "last-modified"):
            assert header in get_resp.headers, f"GET response missing {header!r}"
            assert head_resp.headers.get(header) == get_resp.headers.get(header), (
                f"{header!r} differs: HEAD={head_resp.headers.get(header)!r} "
                f"GET={get_resp.headers.get(header)!r}"
            )
        assert "max-age=3600" in head_resp.headers.get("cache-control", "")
        # content-length reflects the real file size, not the (empty) HEAD body.
        assert int(head_resp.headers["content-length"]) == len(get_resp.content)


class TestProfilePhotoMissing:
    """Photos enabled, but no reachable thumbnail for this person -- either
    no photo interactions at all, or none with a thumbnail on disk."""

    def test_get_404_carries_cache_control_of_at_least_one_hour(self, client, photos_enabled):
        with patch("api.services.interaction_store.get_interaction_store") as mock_store:
            mock_store.return_value.get_for_person.return_value = []
            response = client.get("/api/photos/profile/person-without-photo")

        assert response.status_code == 404
        cache_control = response.headers.get("cache-control", "")
        assert "max-age=" in cache_control
        max_age = int(cache_control.split("max-age=")[1].split(",")[0].strip())
        assert max_age >= 3600

    def test_head_404_matches_get_with_no_body(self, client, photos_enabled):
        with patch("api.services.interaction_store.get_interaction_store") as mock_store:
            mock_store.return_value.get_for_person.return_value = []
            head_resp = client.request("HEAD", "/api/photos/profile/person-without-photo")
            get_resp = client.get("/api/photos/profile/person-without-photo")

        assert head_resp.status_code == get_resp.status_code == 404
        assert head_resp.content == b""
        assert head_resp.headers.get("cache-control") == get_resp.headers.get("cache-control")
