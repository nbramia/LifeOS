"""Tests for Photos thumbnail staging in scripts/apple_data_export.py.

The export copies one small thumbnail per exported face-linked asset into
``<export dir>/photos_thumbnails/<FIRST_CHAR>/<UUID>.jpeg`` so the existing
rsync carries them to the Linux server.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._photos_fakes import (
    MASTERS, MEDIUM, SMALL, UUID_A, UUID_B, UUID_C, make_library,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def export_mod():
    import scripts.apple_data_export as export
    return export


def _library(tmp_path, assets):
    return make_library(tmp_path / "home", assets)


def _asset(uuid, derivatives):
    return {
        "uuid": uuid, "kind": 0, "directory": uuid[0], "filename": f"{uuid}.jpeg",
        "original_filename": "x.jpg", "original": None, "derivatives": derivatives,
    }


class TestStagePhotoThumbnails:
    def test_prefers_masters_derivative(self, export_mod, tmp_path):
        library = _library(tmp_path, [
            _asset(UUID_A, {MASTERS: b"masters", SMALL: b"small", MEDIUM: b"medium"}),
        ])
        dest = tmp_path / "staged"
        result = export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        assert (dest / "A" / f"{UUID_A}.jpeg").read_bytes() == b"masters"
        assert result["copied"] == 1

    def test_fallback_order_small_then_medium(self, export_mod, tmp_path):
        library = _library(tmp_path, [
            _asset(UUID_A, {SMALL: b"small", MEDIUM: b"medium"}),
            _asset(UUID_B, {MEDIUM: b"medium-only"}),
        ])
        dest = tmp_path / "staged"
        export_mod._stage_photo_thumbnails(library, {UUID_A, UUID_B}, dest)
        assert (dest / "A" / f"{UUID_A}.jpeg").read_bytes() == b"small"
        assert (dest / "B" / f"{UUID_B}.jpeg").read_bytes() == b"medium-only"

    def test_missing_derivative_is_counted_not_staged(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_C, {})])
        dest = tmp_path / "staged"
        result = export_mod._stage_photo_thumbnails(library, {UUID_C}, dest)
        assert result["missing"] == 1 and result["copied"] == 0
        assert not (dest / "C" / f"{UUID_C}.jpeg").exists()

    def test_second_run_skips_unchanged_files(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_A, {MASTERS: b"masters"})])
        dest = tmp_path / "staged"
        first = export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        staged = dest / "A" / f"{UUID_A}.jpeg"
        mtime = staged.stat().st_mtime_ns
        second = export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        assert (first["copied"], first["skipped"]) == (1, 0)
        assert (second["copied"], second["skipped"]) == (0, 1)
        assert staged.stat().st_mtime_ns == mtime

    def test_changed_source_is_recopied(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_A, {MASTERS: b"masters"})])
        dest = tmp_path / "staged"
        export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        source = library / MASTERS.format(first="A", uuid=UUID_A)
        source.write_bytes(b"a different, longer master")
        result = export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        assert result["copied"] == 1
        assert (dest / "A" / f"{UUID_A}.jpeg").read_bytes() == b"a different, longer master"

    def test_same_size_but_different_mtime_is_recopied(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_A, {MASTERS: b"masters"})])
        dest = tmp_path / "staged"
        export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        source = library / MASTERS.format(first="A", uuid=UUID_A)
        source.write_bytes(b"MASTERS")  # same length
        os.utime(source, (1_700_000_000, 1_700_000_000))
        result = export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        assert result["copied"] == 1
        assert (dest / "A" / f"{UUID_A}.jpeg").read_bytes() == b"MASTERS"

    def test_one_failing_copy_does_not_stop_the_rest(self, export_mod, tmp_path):
        library = _library(tmp_path, [
            _asset(UUID_A, {MASTERS: b"master-a"}),
            _asset(UUID_B, {MASTERS: b"master-b"}),
        ])
        dest = tmp_path / "staged"
        real_copy = export_mod.shutil.copy2

        def flaky(src, dst, *args, **kwargs):
            if UUID_A in str(src):
                raise PermissionError("synthetic failure")
            return real_copy(src, dst, *args, **kwargs)

        with patch.object(export_mod.shutil, "copy2", side_effect=flaky):
            result = export_mod._stage_photo_thumbnails(library, {UUID_A, UUID_B}, dest)
        assert result["failed"] == 1 and result["copied"] == 1
        assert (dest / "B" / f"{UUID_B}.jpeg").read_bytes() == b"master-b"

    def test_staged_files_are_never_deleted(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_A, {MASTERS: b"masters"})])
        dest = tmp_path / "staged"
        stale = dest / "Z" / "ZZZZZZZZ-0000-4000-8000-000000000009.jpeg"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        export_mod._stage_photo_thumbnails(library, {UUID_A}, dest)
        assert stale.read_bytes() == b"stale"

    def test_malformed_uuid_is_never_used_as_a_path(self, export_mod, tmp_path):
        library = _library(tmp_path, [_asset(UUID_A, {MASTERS: b"masters"})])
        # A file the traversal would reach: <library>/outside_4_5005_c.jpeg.
        (library / "outside_4_5005_c.jpeg").write_bytes(b"outside the derivatives tree")
        dest = tmp_path / "staged"
        result = export_mod._stage_photo_thumbnails(library, {"../../../outside"}, dest)
        assert result["missing"] == 1 and result["copied"] == 0
        assert not list(tmp_path.rglob("outside.jpeg"))
        assert not dest.exists() or not list(dest.rglob("*.jpeg"))


class TestExportPhotosFaces:
    def _export(self, export_mod, tmp_path, assets, faces):
        make_library(
            tmp_path, assets,
            people=[(1, "Synthetic Person", 3, "SYNTHETIC-CONTACT:ABPerson")],
            faces=faces,
        )
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_mod.export_photos_faces(dry_run=False)
        return result, export_dir

    def test_thumbnails_are_staged_for_exported_assets_only(self, export_mod, tmp_path):
        assets = [
            _asset(UUID_A, {MASTERS: b"master-a"}),
            _asset(UUID_B, {MASTERS: b"master-b"}),  # no face: not exported
            _asset(UUID_C, {}),
        ]
        result, export_dir = self._export(export_mod, tmp_path, assets, faces=[(1, 1), (3, 1)])

        assert result["status"] == "ok"
        assert result["thumbnails"] == {
            "status": "ok", "copied": 1, "skipped": 0, "missing": 1, "failed": 0,
        }
        staged = export_dir / "photos_thumbnails"
        assert (staged / "A" / f"{UUID_A}.jpeg").read_bytes() == b"master-a"
        assert not (staged / "B").exists()
        exported = json.loads((export_dir / "photos_faces.json").read_text())
        assert exported["face_appearances_count"] == 2

    def test_an_asset_with_several_faces_is_staged_once(self, export_mod, tmp_path):
        assets = [_asset(UUID_A, {MASTERS: b"master-a"})]
        result, _ = self._export(export_mod, tmp_path, assets, faces=[(1, 1), (1, 1)])
        assert result["thumbnails"]["copied"] == 1

    def test_staging_failure_does_not_abort_the_faces_export(self, export_mod, tmp_path):
        assets = [_asset(UUID_A, {MASTERS: b"master-a"})]
        with patch.object(
            export_mod, "_stage_photo_thumbnails", side_effect=RuntimeError("boom")
        ):
            result, export_dir = self._export(export_mod, tmp_path, assets, faces=[(1, 1)])
        assert result["status"] == "ok"
        assert result["thumbnails"]["status"] == "error"
        assert (export_dir / "photos_faces.json").exists()

    def test_dry_run_stages_nothing(self, export_mod, tmp_path):
        make_library(
            tmp_path, [_asset(UUID_A, {MASTERS: b"master-a"})],
            people=[(1, "Synthetic Person", 1, "SYNTHETIC-CONTACT:ABPerson")], faces=[(1, 1)],
        )
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_mod.export_photos_faces(dry_run=True)
        assert result["status"] == "dry_run"
        assert not (export_dir / "photos_thumbnails").exists()

    def test_no_library_is_skipped(self, export_mod, tmp_path):
        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path):
            result = export_mod.export_photos_faces(dry_run=False)
        assert result["status"] == "skipped"


class TestThumbnailsReachTheServer:
    """The thumbnails ride the existing rsync of data/apple-imports/."""

    AGENT = Path(__file__).resolve().parent.parent / "scripts" / "apple_data_agent.sh"

    def test_rsync_copies_the_export_directory_contents_without_delete(self):
        script = self.AGENT.read_text()
        assert 'EXPORT_DIR="${LIFEOS_DIR}/data/apple-imports/"' in script
        assert 'IMPORT_DIR="${LINUX_USER}@${LINUX_SERVER}:${LINUX_LIFEOS}/data/apple-imports/"' in script
        assert "--delete" not in script

    def test_import_ignores_the_thumbnail_subdirectory(self):
        importer = (self.AGENT.parent / "apple_data_import.py").read_text()
        assert "photos_thumbnails" not in importer
        assert "iterdir" not in importer and "rglob" not in importer
