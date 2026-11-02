"""Tests for profile photo selection (api/services/profile_photo.py) and the
places that feed and serve it: the Apple Data Agent export, the Photos import,
the local Photos sync, and GET/HEAD /api/photos/profile/{person_id}.

All people, assets, and bytes are synthetic.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from api.services import profile_photo
from tests._photos_fakes import UUID_A, UUID_B, UUID_C, make_library

pytestmark = pytest.mark.unit

JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg-bytes\xff\xd9"


def _uuid(n: int) -> str:
    return f"{n:08X}-0000-4000-8000-{n:012X}"


def _ts(day: int) -> str:
    return datetime(2024, 1, day, tzinfo=timezone.utc).isoformat()


def _rec(n, day, faces=1, size=0.2, quality=0.5, eyes_closed=False, key=False):
    """An export-shaped appearance record for asset number ``n``."""
    return {
        "asset_uuid": _uuid(n),
        "timestamp": _ts(day),
        "visible_faces": faces,
        "face_size": size,
        "face_quality": quality,
        "eyes_closed": eyes_closed,
        "key_face": key,
    }


def _picks(records, has_thumbnail=lambda u: True):
    return profile_photo.build_picks({"person": records}, has_thumbnail).get("person", [])


def _best(records, **kwargs):
    picks = _picks(records, **kwargs)
    return picks[0] if picks else None


class TestRanking:
    def test_solo_beats_a_newer_group_shot(self):
        solo_old = _rec(1, day=1, faces=1)
        group_new = _rec(2, day=20, faces=6, size=0.3, quality=0.9)
        assert _best([group_new, solo_old]) == _uuid(1)

    def test_two_faces_beat_a_group_when_there_is_no_solo(self):
        pair = _rec(1, day=1, faces=2)
        group = _rec(2, day=20, faces=5, size=0.3, quality=0.9)
        trio = _rec(3, day=10, faces=3)
        assert _picks([group, trio, pair])[:3] == [_uuid(1), _uuid(3), _uuid(2)]

    def test_a_large_solo_beats_a_tiny_solo(self):
        tiny = _rec(1, day=20, faces=1, size=0.02, quality=0.9)
        large = _rec(2, day=1, faces=1, size=0.15, quality=0.1)
        assert _best([tiny, large]) == _uuid(2)

    def test_a_face_exactly_at_the_minimum_counts_as_large(self):
        at_minimum = _rec(1, day=1, faces=1, size=profile_photo.MIN_AVATAR_FACE_SIZE)
        just_under = _rec(2, day=20, faces=1, size=profile_photo.MIN_AVATAR_FACE_SIZE - 0.01)
        assert _best([just_under, at_minimum]) == _uuid(1)

    def test_a_tiny_solo_still_beats_a_large_pair(self):
        tiny_solo = _rec(1, day=1, faces=1, size=0.02)
        large_pair = _rec(2, day=20, faces=2, size=0.4)
        assert _best([large_pair, tiny_solo]) == _uuid(1)

    def test_open_eyes_beat_closed_eyes_even_at_lower_quality(self):
        closed = _rec(1, day=20, eyes_closed=True, quality=0.9)
        open_ = _rec(2, day=1, quality=0.2)
        assert _best([closed, open_]) == _uuid(2)

    def test_apples_key_face_beats_a_better_scored_face(self):
        plain = _rec(1, day=20, quality=0.9)
        key = _rec(2, day=1, quality=0.3, key=True)
        assert _best([plain, key]) == _uuid(2)

    def test_higher_quality_wins_across_quality_levels(self):
        low = _rec(1, day=20, quality=0.2)
        high = _rec(2, day=1, quality=0.8)
        assert _best([low, high]) == _uuid(2)

    def test_near_equal_quality_falls_to_recency(self):
        older = _rec(1, day=1, quality=0.51)
        newer = _rec(2, day=20, quality=0.50)
        assert _best([older, newer]) == _uuid(2)

    @pytest.mark.parametrize("unscored", [None, -1.0])
    def test_unscored_quality_ranks_below_any_scored_quality(self, unscored):
        scored = _rec(1, day=1, quality=0.3)
        unknown = _rec(2, day=20, quality=unscored)
        assert _picks([unknown, scored]) == [_uuid(1), _uuid(2)]

    def test_unscored_quality_ties_with_a_scored_zero(self):
        scored_zero = _rec(1, day=1, quality=0.0)
        unscored_newer = _rec(2, day=20, quality=-1.0)
        assert _picks([scored_zero, unscored_newer]) == [_uuid(2), _uuid(1)]

    def test_unscored_faces_still_rank_by_recency(self):
        older = _rec(1, day=1, quality=-1.0)
        newer = _rec(2, day=20, quality=-1.0)
        assert _picks([older, newer]) == [_uuid(2), _uuid(1)]

    def test_a_face_without_a_measured_size_ranks_after_measured_groups(self):
        unmeasured_solo = _rec(1, day=20, faces=1, size=0.0, quality=-1.0)
        measured_group = _rec(2, day=1, faces=4, size=0.05)
        assert _picks([unmeasured_solo, measured_group]) == [_uuid(2), _uuid(1)]

    def test_a_photo_with_no_visible_faces_ranks_last(self):
        excluded = _rec(1, day=20, faces=0)
        group = _rec(2, day=1, faces=8, size=0.05)
        assert _picks([excluded, group]) == [_uuid(2)] + [_uuid(1)]

    def test_ties_resolve_deterministically_by_asset(self):
        a, b = _rec(1, day=5), _rec(2, day=5)
        assert _picks([a, b]) == _picks([b, a]) == [_uuid(1), _uuid(2)]


class TestSelection:
    def test_the_best_asset_without_a_thumbnail_falls_to_the_next(self):
        records = [_rec(1, day=1, faces=1), _rec(2, day=2, faces=2), _rec(3, day=3, faces=3)]
        assert _picks(records, has_thumbnail=lambda u: u != _uuid(1)) == [_uuid(2), _uuid(3)]

    def test_no_thumbnails_means_no_pick(self):
        assert _picks([_rec(1, day=1)], has_thumbnail=lambda u: False) == []

    def test_records_without_facts_yield_no_pick(self):
        old_format = [
            {"asset_uuid": _uuid(n), "timestamp": _ts(n), "latitude": None, "longitude": None}
            for n in (1, 2, 3)
        ]
        assert _picks(old_format) == []

    def test_a_person_whose_only_faced_assets_lack_thumbnails_gets_no_pick(self):
        faced = _rec(1, day=1)
        old = {"asset_uuid": _uuid(2), "timestamp": _ts(9)}
        assert _picks([faced, old], has_thumbnail=lambda u: u == _uuid(2)) == []

    def test_picks_are_capped(self):
        picks = _picks([_rec(n, day=n) for n in range(1, 10)])
        assert len(picks) == profile_photo.MAX_STORED_PICKS

    def test_an_asset_with_two_faces_of_the_person_is_picked_once(self):
        assert _picks([_rec(1, day=2), _rec(1, day=2), _rec(2, day=1)]) == [_uuid(1), _uuid(2)]

    def test_records_without_facts_that_follow_faced_ones_rank_by_recency(self):
        faced = _rec(1, day=1)
        old = {"asset_uuid": _uuid(2), "timestamp": _ts(2)}
        new = {"asset_uuid": _uuid(3), "timestamp": _ts(9)}
        assert _picks([old, faced, new]) == [_uuid(1), _uuid(3), _uuid(2)]

    def test_records_without_an_asset_are_ignored(self):
        assert _picks([{"asset_uuid": None, "visible_faces": 1, "face_size": 0.3}]) == []

    def test_an_unparseable_timestamp_ranks_oldest(self):
        broken = {**_rec(1, day=1), "timestamp": "not-a-date"}
        assert _picks([broken, _rec(2, day=2)]) == [_uuid(2), _uuid(1)]


class TestPicksFile:
    @pytest.fixture(autouse=True)
    def picks_path(self, tmp_path):
        from config.settings import Settings
        profile_photo._cache = (None, {})
        with patch.object(Settings, "photos_profile_picks_path", new_callable=PropertyMock,
                          return_value=tmp_path / "data" / "picks.json") as p:
            yield p.return_value
        profile_photo._cache = (None, {})

    def test_round_trip(self, picks_path):
        profile_photo.write_picks({"p1": [_uuid(1), _uuid(2)]})
        assert profile_photo.load_picks() == {"p1": [_uuid(1), _uuid(2)]}

    def test_missing_file_loads_as_empty(self):
        assert profile_photo.load_picks() == {}

    def test_writing_no_picks_removes_the_file(self, picks_path):
        profile_photo.write_picks({"p1": [_uuid(1)]})
        profile_photo.write_picks({})
        assert not picks_path.exists()
        assert profile_photo.load_picks() == {}

    def test_a_rewrite_is_picked_up_without_a_restart(self):
        profile_photo.write_picks({"p1": [_uuid(1)]})
        assert profile_photo.load_picks()["p1"] == [_uuid(1)]
        profile_photo.write_picks({"p1": [_uuid(2)], "p2": [_uuid(3)]})
        assert profile_photo.load_picks() == {"p1": [_uuid(2)], "p2": [_uuid(3)]}

    def test_an_unchanged_file_is_not_re_read(self, picks_path):
        profile_photo.write_picks({"p1": [_uuid(1)]})
        profile_photo.load_picks()
        with patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            assert profile_photo.load_picks() == {"p1": [_uuid(1)]}

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_a_corrupt_file_is_ignored(self, picks_path, content):
        picks_path.parent.mkdir(parents=True)
        picks_path.write_text(content)
        assert profile_photo.load_picks() == {}

    def test_a_failed_write_leaves_no_temp_file_and_keeps_the_old_file(self, picks_path):
        profile_photo.write_picks({"p1": [_uuid(1)]})
        with patch("api.services.profile_photo.json.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                profile_photo.write_picks({"p1": [_uuid(2)]})
        assert json.loads(picks_path.read_text()) == {"p1": [_uuid(1)]}
        assert [p.name for p in picks_path.parent.iterdir()] == [picks_path.name]


# ── Export and local reader against a synthetic Photos.sqlite ────────────────

def _library(tmp_path, faces, *, extra_assets=()):
    """A library with person 1 ('Synthetic Person', key face = face 1) and
    assets A..C plus extras. ``faces`` rows: dict(asset_pk, person_pk, **cols)."""
    assets = [
        {"uuid": u, "kind": 0, "directory": u[0], "filename": f"{u}.jpeg",
         "original_filename": "x.jpg", "original": None, "derivatives": {}}
        for u in (UUID_A, UUID_B, UUID_C, *extra_assets)
    ]
    library = make_library(
        tmp_path, assets,
        people=[(1, "Synthetic Person", 3, "SYNTHETIC-CONTACT:ABPerson"),
                (2, "Other Person", 1, "OTHER-CONTACT:ABPerson")],
    )
    conn = sqlite3.connect(str(library / "database" / "Photos.sqlite"))
    for statement in (
        "ALTER TABLE ZASSET ADD COLUMN ZTRASHEDSTATE INTEGER DEFAULT 0",
        "ALTER TABLE ZASSET ADD COLUMN ZHIDDEN INTEGER DEFAULT 0",
        "ALTER TABLE ZPERSON ADD COLUMN ZKEYFACE INTEGER",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZHIDDEN INTEGER DEFAULT 0",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZASSETVISIBLE INTEGER DEFAULT 1",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZISINTRASH INTEGER DEFAULT 0",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZDETECTIONTYPE INTEGER DEFAULT 1",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZSIZE REAL",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZQUALITY REAL",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZISLEFTEYECLOSED INTEGER",
        "ALTER TABLE ZDETECTEDFACE ADD COLUMN ZISRIGHTEYECLOSED INTEGER",
    ):
        conn.execute(statement)
    for pk, face in enumerate(faces, start=1):
        face = dict(face)
        asset_pk, person_pk = face.pop("asset_pk"), face.pop("person_pk")
        conn.execute(
            "INSERT INTO ZDETECTEDFACE (Z_PK, ZASSETFORFACE, ZPERSONFORFACE) VALUES (?, ?, ?)",
            (pk, asset_pk, person_pk),
        )
        for column, value in face.items():
            conn.execute(f"UPDATE ZDETECTEDFACE SET {column} = ? WHERE Z_PK = ?", (value, pk))
    conn.execute("UPDATE ZPERSON SET ZKEYFACE = 1 WHERE Z_PK = 1")
    conn.commit()
    conn.close()
    return library


FACES = [
    # face 1: person 1 alone on asset 1, Apple's key face
    dict(asset_pk=1, person_pk=1, ZSIZE=0.25, ZQUALITY=0.7, ZISLEFTEYECLOSED=0, ZISRIGHTEYECLOSED=0),
    # asset 2: person 1 with person 2 (eyes closed) and an unnamed face
    dict(asset_pk=2, person_pk=1, ZSIZE=0.05, ZQUALITY=-1.0, ZISLEFTEYECLOSED=1, ZISRIGHTEYECLOSED=0),
    dict(asset_pk=2, person_pk=2, ZSIZE=0.06, ZQUALITY=0.4),
    dict(asset_pk=2, person_pk=None, ZSIZE=0.07, ZQUALITY=0.4),
    # asset 3: person 1 with a hidden face, a not-visible face, a trashed face, and a pet
    dict(asset_pk=3, person_pk=1, ZSIZE=0.3, ZQUALITY=0.8),
    dict(asset_pk=3, person_pk=None, ZSIZE=0.1, ZHIDDEN=1),
    dict(asset_pk=3, person_pk=None, ZSIZE=0.1, ZASSETVISIBLE=0),
    dict(asset_pk=3, person_pk=None, ZSIZE=0.1, ZISINTRASH=1),
    dict(asset_pk=3, person_pk=None, ZSIZE=0.1, ZDETECTIONTYPE=3),
]


class TestExportFacts:
    def _export(self, tmp_path, faces=FACES, library_fn=_library):
        import scripts.apple_data_export as export
        library_fn(tmp_path, faces)
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export.export_photos_faces(dry_run=False)
        assert result["status"] == "ok"
        data = json.loads((export_dir / "photos_faces.json").read_text())
        return {a["asset_uuid"]: a for a in data["face_appearances"] if a["person_pk"] == 1}

    def test_a_solo_photo_carries_its_face_facts(self, tmp_path):
        solo = self._export(tmp_path)[UUID_A]
        assert solo["visible_faces"] == 1
        assert solo["face_size"] == pytest.approx(0.25)
        assert solo["face_quality"] == pytest.approx(0.7)
        assert not solo["eyes_closed"]
        assert solo["key_face"]

    def test_every_visible_unhidden_human_face_counts(self, tmp_path):
        group = self._export(tmp_path)[UUID_B]
        assert group["visible_faces"] == 3
        assert group["eyes_closed"]
        assert group["face_quality"] == pytest.approx(-1.0)
        assert not group["key_face"]

    def test_hidden_invisible_trashed_and_pet_faces_do_not_count(self, tmp_path):
        assert self._export(tmp_path)[UUID_C]["visible_faces"] == 1

    @pytest.mark.parametrize("column", ["ZTRASHEDSTATE", "ZHIDDEN"])
    def test_a_trashed_or_hidden_asset_has_no_visible_faces(self, tmp_path, column):
        def library_with_flag(root, faces):
            library = _library(root, faces)
            conn = sqlite3.connect(str(library / "database" / "Photos.sqlite"))
            conn.execute(f"UPDATE ZASSET SET {column} = 1 WHERE ZUUID = ?", (UUID_A,))
            conn.commit()
            conn.close()

        appearances = self._export(tmp_path, library_fn=library_with_flag)
        assert appearances[UUID_A]["visible_faces"] == 0
        assert appearances[UUID_B]["visible_faces"] == 3

    @pytest.mark.parametrize("column,value", [("ZHIDDEN", 1), ("ZASSETVISIBLE", 0), ("ZISINTRASH", 1)])
    def test_a_subject_face_that_is_not_visible_has_no_visible_faces(self, tmp_path, column, value):
        # Asset 2 keeps two other visible faces, so only the subject's own
        # flag can zero the count.
        faces = [FACES[0], dict(FACES[1], **{column: value})] + FACES[2:]
        appearances = self._export(tmp_path, faces)
        assert appearances[UUID_B]["visible_faces"] == 0

    @pytest.mark.parametrize("left,right", [(1, 0), (0, 1), (1, 1)])
    def test_either_eye_closed_marks_the_face_eyes_closed(self, tmp_path, left, right):
        faces = [dict(FACES[0], ZISLEFTEYECLOSED=left, ZISRIGHTEYECLOSED=right)] + FACES[1:]
        assert self._export(tmp_path, faces)[UUID_A]["eyes_closed"]

    def test_a_library_without_the_face_columns_still_exports_appearances(self, tmp_path):
        import scripts.apple_data_export as export
        library = make_library(
            tmp_path,
            [{"uuid": UUID_A, "kind": 0, "directory": "A", "filename": "x.jpeg",
              "original_filename": "x.jpg", "original": None, "derivatives": {}}],
            people=[(1, "Synthetic Person", 1, "SYNTHETIC-CONTACT:ABPerson")],
            faces=[(1, 1)],
        )
        assert library
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            export.export_photos_faces(dry_run=False)
        appearance = json.loads((export_dir / "photos_faces.json").read_text())["face_appearances"][0]
        assert appearance["asset_uuid"] == UUID_A
        assert "visible_faces" not in appearance

    def test_the_local_reader_matches_the_export(self, tmp_path):
        from api.services.apple_photos import ApplePhotosReader
        exported = self._export(tmp_path)
        reader = ApplePhotosReader(str(tmp_path / "Pictures" / "Photos Library.photoslibrary"
                                       / "database" / "Photos.sqlite"))
        local = {r["asset_uuid"]: r for r in reader.get_profile_appearances(1)}
        assert set(local) == set(exported)
        for asset_uuid, record in local.items():
            for fact in ("timestamp", "visible_faces", "face_size", "face_quality"):
                assert record[fact] == exported[asset_uuid][fact], (asset_uuid, fact)
            for flag in ("eyes_closed", "key_face"):
                assert bool(record[flag]) == bool(exported[asset_uuid][flag]), (asset_uuid, flag)

    def test_the_reader_returns_nothing_for_a_library_without_the_columns(self, tmp_path):
        from api.services.apple_photos import ApplePhotosReader
        library = make_library(tmp_path, [], people=[(1, "Synthetic Person", 1, "X:ABPerson")])
        reader = ApplePhotosReader(str(library / "database" / "Photos.sqlite"))
        assert reader.get_profile_appearances(1) == []

    def test_exported_facts_rank_the_solo_photo_first(self, tmp_path):
        appearances = list(self._export(tmp_path).values())
        assert _best(appearances) == UUID_A


# ── Import and local sync write the picks ────────────────────────────────────

@pytest.fixture
def staged(tmp_path):
    """Staged thumbnails + picks path under tmp_path; returns a stager."""
    from config.settings import Settings
    thumbs = tmp_path / "thumbs"
    profile_photo._cache = (None, {})
    with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock,
                      return_value=thumbs), \
         patch.object(Settings, "photos_profile_picks_path", new_callable=PropertyMock,
                      return_value=tmp_path / "data" / "picks.json"), \
         patch.object(Settings, "photos_enabled", new_callable=PropertyMock,
                      return_value=False):
        def stage(asset_uuid):
            path = thumbs / asset_uuid[0] / f"{asset_uuid}.jpeg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(JPEG)
        yield stage
    profile_photo._cache = (None, {})


class TestImportWritesPicks:
    def _run(self, tmp_path, faces, existing_sources=False):
        import scripts.apple_data_import as imp
        (tmp_path / "imports").mkdir(exist_ok=True)
        (tmp_path / "imports" / "photos_faces.json").write_text(json.dumps({
            "exported_at": _ts(1),
            "people": [{"photos_pk": 7, "full_name": "Synthetic Person", "contact_uuid": "C-1"}],
            "face_appearances": [{"person_pk": 7, **face} for face in faces],
        }))
        se_store = MagicMock()
        contact = MagicMock(canonical_person_id="person-1")
        se_store.get_by_source.side_effect = lambda kind, sid: (
            contact if kind == "contacts" else (object() if existing_sources else None)
        )
        with patch.object(imp, "IMPORT_DIR", tmp_path / "imports"), \
             patch("api.services.source_entity.get_source_entity_store", return_value=se_store), \
             patch("api.services.interaction_store.get_interaction_store", return_value=MagicMock()), \
             patch("api.services.person_entity.get_person_entity_store", return_value=MagicMock()):
            return imp.import_photos_faces(dry_run=False)

    def test_the_best_avatar_is_stored_for_the_matched_person(self, tmp_path, staged):
        for n in (1, 2):
            staged(_uuid(n))
        self._run(tmp_path, [
            {k: v for k, v in _rec(1, day=20, faces=6, size=0.3).items()},
            {k: v for k, v in _rec(2, day=1, faces=1).items()},
        ])
        assert profile_photo.load_picks() == {"person-1": [_uuid(2), _uuid(1)]}

    def test_picks_use_every_exported_appearance_not_only_new_ones(self, tmp_path, staged):
        staged(_uuid(1))
        self._run(tmp_path, [_rec(1, day=1)], existing_sources=True)
        assert profile_photo.load_picks() == {"person-1": [_uuid(1)]}

    def test_an_export_without_facts_removes_stale_picks(self, tmp_path, staged):
        staged(_uuid(1))
        profile_photo.write_picks({"person-1": [_uuid(1)]})
        self._run(tmp_path, [{"asset_uuid": _uuid(1), "timestamp": _ts(1)}])
        assert profile_photo.load_picks() == {}

    def test_assets_without_a_staged_thumbnail_are_never_picked(self, tmp_path, staged):
        staged(_uuid(2))
        self._run(tmp_path, [_rec(1, day=1, faces=1), _rec(2, day=2, faces=4, size=0.05)])
        assert profile_photo.load_picks() == {"person-1": [_uuid(2)]}


class TestLocalSyncWritesPicks:
    def test_matched_people_get_picks_from_the_local_library(self, tmp_path, staged):
        from api.services.apple_photos import ApplePhotosReader
        from api.services.apple_photos_sync import ApplePhotosSync
        library = _library(tmp_path, FACES)
        for u in (UUID_A, UUID_B, UUID_C):
            staged(u)
        syncer = ApplePhotosSync()
        syncer._get_photos_reader = lambda: ApplePhotosReader(
            str(library / "database" / "Photos.sqlite")
        )
        syncer._write_profile_picks({1: "person-1"})
        assert profile_photo.load_picks() == {"person-1": [UUID_A, UUID_C, UUID_B]}

    def test_sync_all_writes_the_picks(self, tmp_path, staged):
        from api.services.apple_photos import ApplePhotosReader
        from api.services.apple_photos_sync import ApplePhotosSync
        library = _library(tmp_path, FACES)
        staged(UUID_A)
        syncer = ApplePhotosSync()
        syncer._get_photos_reader = lambda: ApplePhotosReader(
            str(library / "database" / "Photos.sqlite")
        )
        syncer._get_source_store = MagicMock()
        syncer._get_interaction_store = MagicMock()
        syncer.match_photos_person_to_entity = lambda person: "person-1"
        syncer._sync_person_photos = MagicMock(return_value=(0, 0))
        syncer.sync_all()
        assert profile_photo.load_picks() == {"person-1": [UUID_A]}

    def test_a_failing_reader_does_not_break_the_sync(self, staged):
        from api.services.apple_photos_sync import ApplePhotosSync
        syncer = ApplePhotosSync()
        syncer._get_photos_reader = MagicMock(side_effect=RuntimeError("boom"))
        syncer._write_profile_picks({1: "person-1"})  # must not raise


# ── The profile route ────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def _interaction(source_id):
    from api.services.interaction_store import Interaction
    return Interaction(
        id="interaction-1", person_id="person-1", timestamp=datetime.now(timezone.utc),
        source_type="photos", title="synthetic test photo", source_id=source_id,
    )


class TestProfileRoute:
    def _stage_bytes(self, stage, asset_uuid, data):
        stage(asset_uuid)
        from config.settings import settings
        (settings.photos_thumbnails_dir / asset_uuid[0] / f"{asset_uuid}.jpeg").write_bytes(data)

    def _get(self, client, method="GET", person="person-1", interactions=()):
        with patch("api.services.interaction_store.get_interaction_store") as store:
            store.return_value.get_for_person.return_value = list(interactions)
            return client.request(method, f"/api/photos/profile/{person}")

    def test_the_stored_pick_is_served_instead_of_the_most_recent_photo(self, client, staged):
        self._stage_bytes(staged, _uuid(1), b"recent-group")
        self._stage_bytes(staged, _uuid(2), b"best-solo")
        profile_photo.write_picks({"person-1": [_uuid(2)]})
        response = self._get(client, interactions=[_interaction(f"{_uuid(1)}:7")])
        assert response.status_code == 200
        assert response.content == b"best-solo"
        assert response.headers["content-type"] == "image/jpeg"
        assert "max-age=3600" in response.headers["cache-control"]

    def test_a_pick_without_a_thumbnail_falls_to_the_next_pick(self, client, staged):
        self._stage_bytes(staged, _uuid(3), b"second-pick")
        profile_photo.write_picks({"person-1": [_uuid(2), _uuid(3)]})
        assert self._get(client).content == b"second-pick"

    def test_picks_without_thumbnails_fall_back_to_the_most_recent_photo(self, client, staged):
        self._stage_bytes(staged, _uuid(1), b"recent")
        profile_photo.write_picks({"person-1": [_uuid(2)]})
        response = self._get(client, interactions=[_interaction(f"{_uuid(1)}:7")])
        assert response.content == b"recent"

    def test_a_person_without_a_pick_gets_the_most_recent_photo(self, client, staged):
        self._stage_bytes(staged, _uuid(1), b"recent")
        profile_photo.write_picks({"someone-else": [_uuid(2)]})
        response = self._get(client, interactions=[_interaction(f"{_uuid(1)}:7")])
        assert response.content == b"recent"

    def test_no_picks_file_keeps_the_existing_behaviour(self, client, staged):
        self._stage_bytes(staged, _uuid(1), b"recent")
        response = self._get(client, interactions=[_interaction(f"{_uuid(1)}:7")])
        assert response.status_code == 200 and response.content == b"recent"

    def test_nothing_available_is_a_cached_404(self, client, staged):
        profile_photo.write_picks({"person-1": [_uuid(2)]})
        response = self._get(client)
        assert response.status_code == 404
        assert "max-age=3600" in response.headers["cache-control"]

    def test_head_matches_get_for_a_picked_photo(self, client, staged):
        self._stage_bytes(staged, _uuid(2), b"best-solo")
        profile_photo.write_picks({"person-1": [_uuid(2)]})
        get_response = self._get(client)
        head_response = self._get(client, method="HEAD")
        assert head_response.status_code == get_response.status_code == 200
        assert head_response.content == b"" and get_response.content
        for header in ("content-length", "content-type", "cache-control", "etag", "last-modified"):
            assert head_response.headers[header] == get_response.headers[header], header

    def test_a_pick_lookup_does_not_read_interactions(self, client, staged):
        self._stage_bytes(staged, _uuid(2), b"best-solo")
        profile_photo.write_picks({"person-1": [_uuid(2)]})
        with patch("api.services.interaction_store.get_interaction_store") as store:
            client.get("/api/photos/profile/person-1")
        store.return_value.get_for_person.assert_not_called()
