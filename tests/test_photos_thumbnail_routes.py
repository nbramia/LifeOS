"""Tests for serving Photos thumbnails without a Photos library.

Thumbnails staged by the Apple Data Agent export live in
``settings.photos_thumbnails_dir`` (<FIRST_CHAR>/<UUID>.jpeg). The thumbnail,
profile, and person routes and the CRM config flags work from that directory
alone.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest

from tests._photos_fakes import FAKE_JPEG, UUID_A, UUID_B, UUID_C

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


@pytest.fixture
def staged_dir(tmp_path):
    """An empty staging directory wired in as settings.photos_thumbnails_dir,
    with no local Photos library."""
    from config.settings import Settings
    directory = tmp_path / "photos_thumbnails"
    directory.mkdir()
    with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                      return_value=directory), \
         patch.object(Settings, "photos_enabled", new_callable=PropertyMock,
                      return_value=False):
        yield directory


def stage(directory: Path, uuid: str, data: bytes = FAKE_JPEG) -> Path:
    path = directory / uuid[0] / f"{uuid}.jpeg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def store(tmp_path):
    """A real interaction store in a temp DB, patched in as the singleton."""
    from api.services.interaction_store import InteractionStore
    real = InteractionStore(str(tmp_path / "interactions.db"), strict=False)
    with patch("api.services.interaction_store.get_interaction_store", return_value=real):
        yield real


def add_photo(store, asset_uuid, person_id="person-1", pk=7, when=None):
    from api.services.interaction_store import Interaction
    store.add(Interaction(
        id=f"i-{asset_uuid}-{person_id}",
        person_id=person_id,
        timestamp=when or datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
        source_type="photos",
        title="Photo",
        source_link=f"photos://asset/{asset_uuid}",
        source_id=f"{asset_uuid}:{pk}",
    ))


class TestThumbnailRoute:
    def test_serves_staged_thumbnail_without_a_library(self, client, staged_dir):
        stage(staged_dir, UUID_A)
        response = client.get(f"/api/photos/thumbnail/{UUID_A}")
        assert response.status_code == 200
        assert response.content == FAKE_JPEG
        assert response.headers["content-type"] == "image/jpeg"
        assert "max-age" in response.headers["cache-control"]

    def test_lowercase_uuid_finds_the_uppercase_file(self, client, staged_dir):
        stage(staged_dir, UUID_A)
        assert client.get(f"/api/photos/thumbnail/{UUID_A.lower()}").status_code == 200

    def test_missing_thumbnail_is_404_not_503(self, client, staged_dir):
        stage(staged_dir, UUID_A)
        response = client.get(f"/api/photos/thumbnail/{UUID_B}")
        assert response.status_code == 404

    def test_empty_staging_directory_is_404(self, client, staged_dir):
        assert client.get(f"/api/photos/thumbnail/{UUID_A}").status_code == 404

    @pytest.mark.parametrize("bad", [
        "not-a-uuid",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fetc",
        "A" * 35,
        "A" * 37,
        "A" * 35 + "*",
        "A" * 35 + "/",
        "A" * 35 + "%00",
        "G" * 36,
    ])
    def test_invalid_uuids_are_rejected(self, client, staged_dir, bad):
        stage(staged_dir, UUID_A)
        response = client.get(f"/api/photos/thumbnail/{bad}")
        assert response.status_code in (400, 404)
        assert response.content != FAKE_JPEG

    @pytest.mark.parametrize("bad", ["not-a-uuid", "A" * 35, "A" * 35 + "*", "G" * 36])
    def test_malformed_uuid_is_a_400(self, client, staged_dir, bad):
        assert client.get(f"/api/photos/thumbnail/{bad}").status_code == 400

    def test_traversal_cannot_reach_files_outside_the_staging_directory(
        self, client, staged_dir, tmp_path
    ):
        # A file the glob/`..` tricks would hit if the UUID were not validated.
        (tmp_path / "secret.jpeg").write_bytes(b"secret")
        for bad in ("..", "..%2Fsecret", "A" * 35 + "%2F..%2F..%2Fsecret"):
            response = client.get(f"/api/photos/thumbnail/{bad}")
            assert response.content != b"secret"

    def test_falls_through_to_the_library_when_not_staged(self, client, staged_dir, tmp_path):
        library_thumb = tmp_path / "from-library.jpeg"
        library_thumb.write_bytes(b"library-bytes")
        with patch("api.routes.photos._get_thumbnail_path", return_value=library_thumb):
            response = client.get(f"/api/photos/thumbnail/{UUID_A}")
        assert response.status_code == 200
        assert response.content == b"library-bytes"

    def test_staged_thumbnail_wins_over_the_library(self, client, staged_dir, tmp_path):
        stage(staged_dir, UUID_A, b"staged-bytes")
        library_thumb = tmp_path / "from-library.jpeg"
        library_thumb.write_bytes(b"library-bytes")
        with patch("api.routes.photos._get_thumbnail_path", return_value=library_thumb):
            response = client.get(f"/api/photos/thumbnail/{UUID_A}")
        assert response.content == b"staged-bytes"


class TestProfileRoute:
    def test_serves_a_staged_thumbnail_without_a_library(self, client, staged_dir, store):
        add_photo(store, UUID_A)
        stage(staged_dir, UUID_A)
        response = client.get("/api/photos/profile/person-1")
        assert response.status_code == 200
        assert response.content == FAKE_JPEG
        assert "max-age=3600" in response.headers["cache-control"]

    def test_uses_the_asset_uuid_not_the_scoped_source_id(self, client, staged_dir, store):
        # source_id is "<asset_uuid>:<person_pk>"; the file is named by the asset UUID.
        add_photo(store, UUID_A, pk=1234)
        stage(staged_dir, UUID_A, b"asset-file")
        assert client.get("/api/photos/profile/person-1").content == b"asset-file"

    def test_skips_assets_without_a_thumbnail(self, client, staged_dir, store):
        add_photo(store, UUID_A, when=datetime(2024, 6, 1, tzinfo=timezone.utc))
        add_photo(store, UUID_B, when=datetime(2024, 5, 1, tzinfo=timezone.utc))
        stage(staged_dir, UUID_B, b"older-photo")
        assert client.get("/api/photos/profile/person-1").content == b"older-photo"

    def test_no_thumbnails_is_a_cached_404_not_503(self, client, staged_dir, store):
        add_photo(store, UUID_A)
        response = client.get("/api/photos/profile/person-1")
        assert response.status_code == 404
        assert "max-age=3600" in response.headers["cache-control"]

    def test_a_hostile_source_id_cannot_reach_files_outside_the_staging_directory(
        self, client, staged_dir, store, tmp_path
    ):
        from api.services.interaction_store import Interaction
        # "<dir>/./../SECRET.jpeg" would escape if the asset id were not validated.
        (tmp_path / "SECRET.jpeg").write_bytes(b"secret")
        store.add(Interaction(
            id="i-evil", person_id="person-1",
            timestamp=datetime(2024, 5, 1, tzinfo=timezone.utc),
            source_type="photos", title="Photo", source_id="../secret:1",
        ))
        response = client.get("/api/photos/profile/person-1")
        assert response.status_code == 404
        assert response.content != b"secret"

    def test_head_matches_get(self, client, staged_dir, store):
        add_photo(store, UUID_A)
        stage(staged_dir, UUID_A)
        head = client.request("HEAD", "/api/photos/profile/person-1")
        get = client.get("/api/photos/profile/person-1")
        assert head.status_code == get.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == get.headers["content-length"]


class TestPersonRoute:
    def test_works_without_a_library_and_returns_asset_uuids(self, client, staged_dir, store):
        add_photo(store, UUID_A, pk=11)
        add_photo(store, UUID_B, pk=11)
        response = client.get("/api/photos/person/person-1?limit=10")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert {p["uuid"] for p in body["photos"]} == {UUID_A, UUID_B}

    def test_date_filter(self, client, staged_dir, store):
        add_photo(store, UUID_A, when=datetime(2024, 5, 1, 12, tzinfo=timezone.utc))
        add_photo(store, UUID_B, when=datetime(2024, 6, 2, 12, tzinfo=timezone.utc))
        body = client.get("/api/photos/person/person-1?date=2024-05-01").json()
        assert [p["uuid"] for p in body["photos"]] == [UUID_A]

    def test_unknown_person_is_an_empty_list(self, client, staged_dir, store):
        body = client.get("/api/photos/person/nobody").json()
        assert body["photos"] == [] and body["count"] == 0

    def test_library_dependent_routes_still_require_the_library(self, client, staged_dir):
        assert client.get("/api/photos/people").status_code == 503
        assert client.post("/api/photos/sync").status_code == 503


class TestSharedRoute:
    """Photo interactions carry ``<asset_uuid>:<person_pk>``, so two people in
    the same photo have different source_ids; the route matches on the asset
    UUID and reads only the interactions table."""

    def test_matches_across_people_on_the_asset_uuid(self, client, staged_dir, store):
        add_photo(store, UUID_A, person_id="person-1", pk=11)
        add_photo(store, UUID_A, person_id="person-2", pk=22)
        add_photo(store, UUID_B, person_id="person-1", pk=11)
        add_photo(store, UUID_C, person_id="person-2", pk=22)
        response = client.get("/api/photos/shared/person-1/person-2")
        assert response.status_code == 200
        body = response.json()
        assert body["shared_photo_count"] == 1
        assert [p["uuid"] for p in body["photos"]] == [UUID_A]

    def test_works_without_a_library(self, client, staged_dir, store):
        response = client.get("/api/photos/shared/person-1/person-2")
        assert response.status_code == 200
        assert response.json()["shared_photo_count"] == 0

    def test_limit_caps_photos_but_not_the_count(self, client, staged_dir, store):
        for asset in (UUID_A, UUID_B, UUID_C):
            add_photo(store, asset, person_id="person-1", pk=1)
            add_photo(store, asset, person_id="person-2", pk=2)
        body = client.get("/api/photos/shared/person-1/person-2?limit=2").json()
        assert body["shared_photo_count"] == 3
        assert len(body["photos"]) == 2

    def test_a_photo_with_no_shared_person_is_excluded(self, client, staged_dir, store):
        add_photo(store, UUID_A, person_id="person-1", pk=1)
        add_photo(store, UUID_B, person_id="person-2", pk=2)
        body = client.get("/api/photos/shared/person-1/person-2").json()
        assert body["photos"] == [] and body["shared_photo_count"] == 0


class TestCrmConfigFlags:
    """photos_enabled is true when a library exists OR staged thumbnails
    exist; photos_originals_enabled follows the origin SSH host setting."""

    @pytest.mark.parametrize("library, thumbnails, expected", [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ])
    def test_photos_enabled_is_library_or_thumbnails(
        self, client, tmp_path, library, thumbnails, expected
    ):
        from config.settings import Settings
        directory = tmp_path / "photos_thumbnails"
        directory.mkdir()
        if thumbnails:
            stage(directory, UUID_A)
        with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                          return_value=directory), \
             patch.object(Settings, "photos_enabled", new_callable=PropertyMock,
                          return_value=library):
            body = client.get("/api/crm/config").json()
        assert body["photos_enabled"] is expected

    def test_missing_staging_directory_counts_as_no_thumbnails(self, client, tmp_path):
        from config.settings import Settings
        with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                          return_value=tmp_path / "does-not-exist"), \
             patch.object(Settings, "photos_enabled", new_callable=PropertyMock,
                          return_value=False):
            assert client.get("/api/crm/config").json()["photos_enabled"] is False

    def test_empty_subdirectories_do_not_count(self, client, tmp_path):
        from config.settings import Settings
        directory = tmp_path / "photos_thumbnails"
        (directory / "A").mkdir(parents=True)
        with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                          return_value=directory), \
             patch.object(Settings, "photos_enabled", new_callable=PropertyMock,
                          return_value=False):
            assert client.get("/api/crm/config").json()["photos_enabled"] is False

    @pytest.mark.parametrize("host, expected", [("", False), ("   ", False), ("mac-host", True)])
    def test_photos_originals_enabled_follows_the_ssh_host(
        self, client, monkeypatch, host, expected
    ):
        from config.settings import settings
        monkeypatch.setattr(settings, "photos_origin_ssh_host", host)
        body = client.get("/api/crm/config").json()
        assert body["photos_originals_enabled"] is expected
        assert isinstance(body["photos_enabled"], bool)

    def test_settings_photos_enabled_semantics_are_unchanged(self, tmp_path):
        """settings.photos_enabled still means "the library database exists";
        staged thumbnails alone do not flip it, so routes that need the real
        library keep returning 503."""
        from config.settings import Settings
        directory = tmp_path / "photos_thumbnails"
        stage(directory, UUID_A)
        with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                          return_value=directory):
            instance = Settings(LIFEOS_PHOTOS_PATH=str(tmp_path / "no-library"))
            assert instance.photos_enabled is False
            assert instance.photos_thumbnails_available is True


class TestLibraryPathExpansion:
    """A tilde-prefixed LIFEOS_PHOTOS_PATH resolves for the library file
    lookups the same way settings.photos_db_path resolves it."""

    @pytest.fixture
    def tilde_library(self, tmp_path, monkeypatch):
        from config.settings import settings
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "photos_library_path", "~/Synthetic.photoslibrary")
        return tmp_path / "Synthetic.photoslibrary"

    def test_thumbnail_lookup_expands_the_tilde(self, tilde_library):
        from api.routes.photos import _get_thumbnail_path
        thumb = tilde_library / "resources" / "derivatives" / UUID_A[0] / f"{UUID_A}_1_105_c.jpeg"
        thumb.parent.mkdir(parents=True)
        thumb.write_bytes(FAKE_JPEG)
        assert _get_thumbnail_path(UUID_A) == thumb

    def test_original_lookup_expands_the_tilde(self, tilde_library):
        from api.routes.photos import _get_photo_file_path
        original = tilde_library / "originals" / UUID_A[0] / f"{UUID_A}.jpeg"
        original.parent.mkdir(parents=True)
        original.write_bytes(FAKE_JPEG)
        assert _get_photo_file_path(UUID_A) == original
