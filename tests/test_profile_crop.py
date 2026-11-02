"""Tests for face-centred avatar crops (api/services/profile_crop.py) and their
use by GET/HEAD /api/photos/profile/{person_id}.

Images are synthetic: flat colours with a marked patch where the "face" is.
"""
import sys
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest
from PIL import Image

from api.services import profile_crop, profile_photo

pytestmark = pytest.mark.unit

ASSET = "AAAAAAAA-0000-4000-8000-000000000001"
RED, BLUE, GREEN, WHITE = (220, 20, 20), (20, 20, 220), (20, 200, 20), (255, 255, 255)


@pytest.fixture
def dirs(tmp_path):
    """Thumbnails, sources, crops and the picks file under tmp_path."""
    from config.settings import Settings
    profile_photo._cache = (None, {})
    thumbs, sources, crops = tmp_path / "thumbs", tmp_path / "sources", tmp_path / "crops"
    with patch.object(Settings, "photos_thumbnails_dir", new_callable=PropertyMock, return_value=thumbs), \
         patch.object(Settings, "photos_profile_sources_dir", new_callable=PropertyMock, return_value=sources), \
         patch.object(Settings, "photos_profile_crops_dir", new_callable=PropertyMock, return_value=crops), \
         patch.object(Settings, "photos_profile_picks_path", new_callable=PropertyMock,
                      return_value=tmp_path / "data" / "picks.json"), \
         patch.object(Settings, "photos_enabled", new_callable=PropertyMock, return_value=False):
        yield {"thumbs": thumbs, "sources": sources, "crops": crops}
    profile_photo._cache = (None, {})


def _image(path: Path, size, patches=(), background=WHITE):
    """Write a JPEG of ``size`` with filled ``patches``: (box, colour)."""
    image = Image.new("RGB", size, background)
    for box, colour in patches:
        image.paste(colour, box)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=95)
    return path


def _thumb(dirs, asset=ASSET, size=(400, 300), patches=()):
    return _image(dirs["thumbs"] / asset[0] / f"{asset}.jpeg", size, patches)


def _source(dirs, asset=ASSET, size=(800, 600), patches=()):
    return _image(dirs["sources"] / asset[0] / f"{asset}.jpeg", size, patches)


def _pick(x, y, size, asset=ASSET):
    return {"asset": asset, "x": x, "y": y, "size": size}


def _centre_colour(path):
    with Image.open(path) as image:
        return image.getpixel((image.width // 2, image.height // 2))


def _close(a, b, tolerance=40):
    return all(abs(p - q) <= tolerance for p, q in zip(a, b))


@pytest.fixture
def plain_constants():
    """Crop settings that make the expected numbers easy to state."""
    with patch.object(profile_crop, "FACE_CROP_SCALE", 2.0), \
         patch.object(profile_crop, "CROP_LIFT", 0.0), \
         patch.object(profile_crop, "CROP_MAX_PX", 256):
        yield


class TestCropBox:
    def test_a_centred_face_gets_a_centred_square_of_scale_face_boxes(self, plain_constants):
        # face box = 0.1 * 1000 = 100 px; crop = 2 face boxes = 200 px
        assert profile_crop.crop_box(1000, 1000, 0.5, 0.5, 0.1) == (400, 400, 200)

    @pytest.mark.parametrize("width,height", [(2000, 1000), (1000, 2000)])
    def test_the_face_size_is_a_fraction_of_the_longer_side(self, plain_constants, width, height):
        # 0.1 of the longer side (2000) = 200 px; crop = 400 px either way.
        side = profile_crop.crop_box(width, height, 0.5, 0.5, 0.1)[2]
        assert side == 400

    def test_the_position_is_a_fraction_of_each_axis_with_the_origin_top_left(self, plain_constants):
        left, top, side = profile_crop.crop_box(2000, 1000, 0.25, 0.2, 0.05)
        # face box 100 px, crop 200 px, centred on (500, 200)
        assert (left, top, side) == (400, 100, 200)

    def test_the_crop_follows_y_downwards(self, plain_constants):
        high = profile_crop.crop_box(1000, 1000, 0.5, 0.2, 0.1)
        low = profile_crop.crop_box(1000, 1000, 0.5, 0.8, 0.1)
        assert high[1] == 100 and low[1] == 700

    @pytest.mark.parametrize("x,expected_left", [(0.02, 0), (0.98, 800)])
    def test_the_crop_slides_to_stay_inside_horizontally(self, plain_constants, x, expected_left):
        left, _, side = profile_crop.crop_box(1000, 1000, x, 0.5, 0.1)
        assert (left, side) == (expected_left, 200)

    @pytest.mark.parametrize("y,expected_top", [(0.02, 0), (0.98, 800)])
    def test_the_crop_slides_to_stay_inside_vertically(self, plain_constants, y, expected_top):
        _, top, side = profile_crop.crop_box(1000, 1000, 0.5, y, 0.1)
        assert (top, side) == (expected_top, 200)

    def test_the_crop_never_exceeds_the_shorter_side(self, plain_constants):
        left, top, side = profile_crop.crop_box(800, 600, 0.5, 0.5, 0.9)
        assert side == 600 and top == 0 and left == 100

    def test_the_crop_is_lifted_by_a_fraction_of_its_side(self):
        with patch.object(profile_crop, "FACE_CROP_SCALE", 2.0), \
             patch.object(profile_crop, "CROP_LIFT", 0.1):
            left, top, side = profile_crop.crop_box(1000, 1000, 0.5, 0.5, 0.1)
        assert (left, top, side) == (400, 380, 200)  # centre moved up 0.1 * 200

    @pytest.mark.parametrize("args", [
        (0, 100, 0.5, 0.5, 0.1), (100, 0, 0.5, 0.5, 0.1),
        (100, 100, -0.1, 0.5, 0.1), (100, 100, 1.1, 0.5, 0.1),
        (100, 100, 0.5, -0.1, 0.1), (100, 100, 0.5, 1.1, 0.1),
        (100, 100, 0.5, 0.5, 0), (100, 100, 0.5, 0.5, -0.1), (100, 100, 0.5, 0.5, float("nan")),
    ])
    def test_unusable_geometry_has_no_box(self, args):
        assert profile_crop.crop_box(*args) is None

    def test_every_box_lies_inside_the_image(self):
        for width, height in [(480, 360), (360, 480), (500, 500), (1000, 40)]:
            for x in (0, 0.01, 0.3, 0.5, 0.99, 1):
                for y in (0, 0.01, 0.3, 0.5, 0.99, 1):
                    for size in (0.01, 0.1, 0.5, 1):
                        left, top, side = profile_crop.crop_box(width, height, x, y, size)
                        assert 0 <= left and 0 <= top and side >= 1
                        assert left + side <= width and top + side <= height


class TestCroppedAvatar:
    def test_the_crop_is_a_square_around_the_face(self, dirs, plain_constants):
        # 400x300 thumbnail with a red patch at (100..140, 130..170): centre (120, 150)
        thumb = _thumb(dirs, patches=[((100, 130, 140, 170), RED)])
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.3, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.width == image.height == 80  # 2 * (0.1 * 400)
        assert _close(_centre_colour(crop), RED)

    def test_y_counts_from_the_top(self, dirs, plain_constants):
        # red patch near the top, blue near the bottom of a 300 px tall image
        thumb = _thumb(dirs, patches=[((180, 30, 220, 70), RED), ((180, 230, 220, 270), BLUE)])
        near_top = profile_crop.cropped_avatar("top", thumb, _pick(0.5, 50 / 300, 0.1))
        near_bottom = profile_crop.cropped_avatar("bottom", thumb, _pick(0.5, 250 / 300, 0.1))
        assert _close(_centre_colour(near_top), RED)
        assert _close(_centre_colour(near_bottom), BLUE)

    def test_x_counts_from_the_left(self, dirs, plain_constants):
        thumb = _thumb(dirs, patches=[((30, 130, 70, 170), RED), ((330, 130, 370, 170), BLUE)])
        left = profile_crop.cropped_avatar("left", thumb, _pick(50 / 400, 0.5, 0.1))
        right = profile_crop.cropped_avatar("right", thumb, _pick(350 / 400, 0.5, 0.1))
        assert _close(_centre_colour(left), RED)
        assert _close(_centre_colour(right), BLUE)

    def test_a_face_at_the_frame_edge_still_yields_a_full_square(self, dirs, plain_constants):
        thumb = _thumb(dirs, patches=[((0, 0, 30, 30), GREEN)])
        crop = profile_crop.cropped_avatar("edge", thumb, _pick(0.02, 0.02, 0.1))
        with Image.open(crop) as image:
            assert image.width == image.height == 80
            assert _close(image.getpixel((5, 5)), GREEN)

    def test_a_large_crop_is_downscaled_to_the_maximum(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(2000, 1500))
        crop = profile_crop.cropped_avatar("big", thumb, _pick(0.5, 0.5, 0.2))
        with Image.open(crop) as image:
            assert image.size == (profile_crop.CROP_MAX_PX, profile_crop.CROP_MAX_PX)

    def test_a_small_crop_is_not_upscaled(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300))
        crop = profile_crop.cropped_avatar("small", thumb, _pick(0.5, 0.5, 0.05))
        with Image.open(crop) as image:
            assert image.size == (40, 40)

    def test_the_crop_is_a_jpeg_under_the_crops_directory(self, dirs):
        crop = profile_crop.cropped_avatar("p1", _thumb(dirs), _pick(0.5, 0.5, 0.1))
        assert crop.suffix == ".jpeg" and dirs["crops"] in crop.parents
        with Image.open(crop) as image:
            assert image.format == "JPEG"


class TestCropCache:
    def test_a_repeat_request_reuses_the_file_without_decoding(self, dirs):
        thumb = _thumb(dirs)
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with patch("PIL.Image.open", side_effect=AssertionError("decoded again")):
            assert profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1)) == first

    @pytest.mark.parametrize("changed", [_pick(0.6, 0.5, 0.1), _pick(0.5, 0.6, 0.1), _pick(0.5, 0.5, 0.2)])
    def test_a_changed_face_makes_a_new_crop_and_drops_the_old_one(self, dirs, changed):
        thumb = _thumb(dirs)
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        second = profile_crop.cropped_avatar("p1", thumb, changed)
        assert second != first
        assert not first.exists()
        assert list(second.parent.iterdir()) == [second]

    def test_a_different_pick_asset_makes_a_new_crop(self, dirs):
        other = "BBBBBBBB-0000-4000-8000-000000000002"
        first = profile_crop.cropped_avatar("p1", _thumb(dirs), _pick(0.5, 0.5, 0.1))
        second = profile_crop.cropped_avatar("p1", _thumb(dirs, asset=other), _pick(0.5, 0.5, 0.1, asset=other))
        assert second != first and not first.exists()

    def test_a_replaced_thumbnail_makes_a_new_crop(self, dirs, plain_constants):
        thumb = _thumb(dirs, patches=[((180, 130, 220, 170), RED)])
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        _thumb(dirs, size=(500, 375), patches=[((230, 160, 270, 200), BLUE)])
        second = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        assert second != first
        assert _close(_centre_colour(second), BLUE)

    def test_a_new_staged_source_makes_a_new_crop(self, dirs):
        thumb = _thumb(dirs)
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        _source(dirs)
        second = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        assert second != first and not first.exists()

    def test_a_changed_crop_setting_makes_a_new_crop(self, dirs):
        thumb = _thumb(dirs)
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with patch.object(profile_crop, "FACE_CROP_SCALE", profile_crop.FACE_CROP_SCALE + 1):
            second = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        assert second != first

    def test_people_keep_separate_crops_of_the_same_photo(self, dirs):
        thumb = _thumb(dirs)
        a = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        b = profile_crop.cropped_avatar("p2", thumb, _pick(0.2, 0.5, 0.1))
        assert a.exists() and b.exists() and a.parent != b.parent

    def test_a_person_id_never_reaches_the_file_system_as_a_path(self, dirs):
        crop = profile_crop.cropped_avatar("../../etc/passwd", _thumb(dirs), _pick(0.5, 0.5, 0.1))
        assert dirs["crops"] in crop.parents and ".." not in crop.parts

    def test_no_temporary_files_are_left_behind(self, dirs):
        crop = profile_crop.cropped_avatar("p1", _thumb(dirs), _pick(0.5, 0.5, 0.1))
        assert [p.name for p in crop.parent.iterdir()] == [crop.name]


class TestCropSource:
    def test_a_staged_source_with_the_thumbnails_framing_is_cropped_instead(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300), patches=[((180, 130, 220, 170), RED)])
        _source(dirs, size=(800, 600), patches=[((360, 260, 440, 340), RED)])
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.size == (160, 160)  # 2 * 0.1 * 800: cut from the source

    def test_a_lowercase_asset_id_finds_the_staged_source(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300))
        _source(dirs, size=(800, 600))
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1, asset=ASSET.lower()))
        with Image.open(crop) as image:
            assert image.size == (160, 160)

    def test_replacing_the_thumbnail_with_another_framing_drops_the_source_crop(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300))
        _source(dirs, size=(800, 600))
        first = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        _thumb(dirs, size=(300, 300))  # a square thumbnail does not frame the photo like the 4:3 source
        second = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        assert second != first
        with Image.open(second) as image:
            assert image.size == (60, 60)  # cut from the new thumbnail

    def test_a_source_with_a_different_aspect_ratio_is_ignored(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300))
        _source(dirs, size=(600, 600))
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.size == (80, 80)  # cut from the thumbnail

    def test_a_source_within_the_aspect_tolerance_is_used(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(480, 360))
        _source(dirs, size=(1024, 770))  # 1.3299 vs 1.3333
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.width == 205

    def test_a_source_just_outside_the_aspect_tolerance_is_ignored(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(480, 360))
        _source(dirs, size=(1024, 740))  # 1.3838 vs 1.3333
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.width == 96


class TestCropFallbacks:
    def test_without_pillow_there_is_no_crop(self, dirs):
        thumb = _thumb(dirs)
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            assert profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1)) is None
        assert not dirs["crops"].exists()

    def test_an_unreadable_image_has_no_crop(self, dirs):
        thumb = dirs["thumbs"] / ASSET[0] / f"{ASSET}.jpeg"
        thumb.parent.mkdir(parents=True)
        thumb.write_bytes(b"not an image")
        assert profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1)) is None

    def test_a_missing_thumbnail_has_no_crop(self, dirs):
        assert profile_crop.cropped_avatar("p1", dirs["thumbs"] / "nope.jpeg", _pick(0.5, 0.5, 0.1)) is None

    @pytest.mark.parametrize("pick", [
        {"asset": ASSET, "x": 1.5, "y": 0.5, "size": 0.1},
        {"asset": ASSET, "x": 0.5, "y": 0.5, "size": 0},
        {"asset": ASSET, "x": 0.5, "y": 0.5},
        {"asset": ASSET, "x": None, "y": 0.5, "size": 0.1},
    ])
    def test_unusable_geometry_has_no_crop(self, dirs, pick):
        assert profile_crop.cropped_avatar("p1", _thumb(dirs), pick) is None

    @pytest.mark.parametrize("pick", [_pick(1.5, 0.5, 0.1), _pick(0.5, 0.5, 0)])
    def test_out_of_range_geometry_is_declined_without_a_warning(self, dirs, pick, caplog):
        with caplog.at_level("WARNING"):
            assert profile_crop.cropped_avatar("p1", _thumb(dirs), pick) is None
        assert not caplog.records

    def test_a_failed_write_has_no_crop_and_leaves_nothing_behind(self, dirs):
        thumb = _thumb(dirs)
        with patch("PIL.Image.Image.save", side_effect=OSError("disk full")):
            assert profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1)) is None
        person_dirs = list(dirs["crops"].iterdir())
        assert all(not list(d.iterdir()) for d in person_dirs)

    def test_an_unreadable_source_falls_back_to_the_thumbnail(self, dirs, plain_constants):
        thumb = _thumb(dirs, size=(400, 300))
        bad = dirs["sources"] / ASSET[0] / f"{ASSET}.jpeg"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"not an image")
        crop = profile_crop.cropped_avatar("p1", thumb, _pick(0.5, 0.5, 0.1))
        with Image.open(crop) as image:
            assert image.size == (80, 80)  # cut from the thumbnail


class TestProfileRouteCrop:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def _get(self, client, method="GET", person="person-1"):
        with patch("api.services.interaction_store.get_interaction_store") as store:
            store.return_value.get_for_person.return_value = []
            return client.request(method, f"/api/photos/profile/{person}")

    def _picks(self, *entries):
        profile_photo.write_picks({"person-1": list(entries)})

    def test_a_pick_with_a_face_is_served_cropped(self, client, dirs):
        _thumb(dirs, patches=[((100, 130, 140, 170), RED)])
        self._picks(_pick(0.3, 0.5, 0.1))
        response = self._get(client)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        from io import BytesIO
        with Image.open(BytesIO(response.content)) as image:
            assert image.width == image.height and image.width < 300
            assert _close(image.getpixel((image.width // 2, image.height // 2)), RED)

    def test_a_pick_without_geometry_is_served_uncropped(self, client, dirs):
        thumb = _thumb(dirs)
        self._picks({"asset": ASSET})
        assert self._get(client).content == thumb.read_bytes()

    def test_a_pick_without_geometry_is_never_sent_to_the_cropper(self, client, dirs):
        _thumb(dirs)
        self._picks({"asset": ASSET})
        with patch("api.services.profile_crop.cropped_avatar") as cropper:
            self._get(client)
        cropper.assert_not_called()

    def test_a_failed_crop_serves_the_thumbnail(self, client, dirs):
        thumb = _thumb(dirs)
        self._picks(_pick(0.3, 0.5, 0.1))
        with patch("api.services.profile_crop.cropped_avatar", return_value=None):
            response = self._get(client)
        assert response.status_code == 200 and response.content == thumb.read_bytes()

    def test_without_pillow_the_thumbnail_is_served(self, client, dirs):
        thumb = _thumb(dirs)
        self._picks(_pick(0.3, 0.5, 0.1))
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            response = self._get(client)
        assert response.status_code == 200 and response.content == thumb.read_bytes()

    def test_a_malformed_pick_entry_is_skipped(self, client, dirs):
        thumb = _thumb(dirs)
        profile_photo.write_picks({"person-1": ["not-a-dict", {"asset": ASSET}]})
        assert self._get(client).content == thumb.read_bytes()

    def test_head_matches_get_for_a_cropped_avatar(self, client, dirs):
        _thumb(dirs)
        self._picks(_pick(0.3, 0.5, 0.1))
        get_response = self._get(client)
        head_response = self._get(client, method="HEAD")
        assert head_response.status_code == get_response.status_code == 200
        assert head_response.content == b"" and get_response.content
        for header in ("content-length", "content-type", "cache-control", "etag", "last-modified"):
            assert head_response.headers[header] == get_response.headers[header], header
        assert "max-age=3600" in get_response.headers["cache-control"]
        assert int(head_response.headers["content-length"]) == len(get_response.content)

    def test_repeat_requests_do_not_decode_the_image_again(self, client, dirs):
        _thumb(dirs)
        self._picks(_pick(0.3, 0.5, 0.1))
        first = self._get(client)
        with patch("PIL.Image.open", side_effect=AssertionError("decoded again")):
            second = self._get(client)
        assert second.content == first.content

    def test_a_changed_pick_is_served_after_the_picks_file_changes(self, client, dirs, plain_constants):
        _thumb(dirs, patches=[((100, 130, 140, 170), RED), ((300, 130, 340, 170), BLUE)])
        self._picks(_pick(0.3, 0.5, 0.1))
        from io import BytesIO
        with Image.open(BytesIO(self._get(client).content)) as image:
            assert _close(image.getpixel((image.width // 2, image.height // 2)), RED)
        self._picks(_pick(0.8, 0.5, 0.1))
        with Image.open(BytesIO(self._get(client).content)) as image:
            assert _close(image.getpixel((image.width // 2, image.height // 2)), BLUE)
