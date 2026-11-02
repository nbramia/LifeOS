"""Face-centred avatar crops of Photos thumbnails.

``cropped_avatar`` cuts a square headshot around a face out of a thumbnail and
caches it on disk under ``settings.photos_profile_crops_dir``. It returns None
whenever a crop cannot be made (no Pillow, unreadable image, bad geometry, an
image that does not frame the photo like the frame the face was measured on), so
the caller serves the uncropped thumbnail instead. A wrong crop is worse than
none.

The crop is cut from the staged higher-resolution profile source of the asset
(``settings.photos_profile_sources_dir``) when there is one, else from the
thumbnail; either must have the aspect ratio of the frame the face was measured
on. Images are read upright (EXIF orientation applied), the way a browser shows
the uncropped thumbnail.

Geometry: a face is ``(x, y, size)`` with ``x``/``y`` the face box centre as
fractions of the image width and height (origin top-left) and ``size`` the box
side as a fraction of the image's longer side, all on the frame as Photos shows
it (the orientation is already applied, so no per-orientation mapping applies).
"""
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Crop side as a multiple of the face box side: head, hair and some margin.
FACE_CROP_SCALE = 3.0
# The crop centre sits this fraction of the crop side above the face box
# centre, so hair gets room.
CROP_LIFT = 0.08
# Longest edge of a stored crop, in pixels. A smaller source is not upscaled.
CROP_MAX_PX = 256
CROP_JPEG_QUALITY = 88


def crop_box(
    width: int, height: int, x: float, y: float, size: float
) -> Optional[tuple[int, int, int]]:
    """``(left, top, side)`` of a square crop inside a ``width`` x ``height``
    image, or None when the geometry is unusable.

    The crop is ``FACE_CROP_SCALE`` face boxes wide, at most the image's
    shorter side, and slides to stay inside the image rather than padding.
    """
    if width <= 0 or height <= 0 or not 0 <= x <= 1 or not 0 <= y <= 1 or not size > 0:
        return None
    side = int(round(min(size * max(width, height) * FACE_CROP_SCALE, width, height)))
    side = max(side, 1)
    centre_x = x * width
    centre_y = y * height - CROP_LIFT * side
    left = min(max(int(round(centre_x - side / 2)), 0), width - side)
    top = min(max(int(round(centre_y - side / 2)), 0), height - side)
    return left, top, side


# An image may differ from the frame the face was measured on by this much
# (relative aspect ratio, to allow for rounding) before it is rejected.
ASPECT_TOLERANCE = 0.02


def _staged_source(asset: str) -> Optional[Path]:
    """The staged higher-resolution source of an asset, if there is one."""
    source = settings.photos_profile_sources_dir / asset[0].upper() / f"{asset.upper()}.jpeg"
    return source if source.is_file() else None


def _upright_image(path: Path, frame: tuple[int, int]):
    """The image at ``path`` with its EXIF orientation applied, or None when it
    cannot be read or does not frame the photo like ``frame``."""
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except OSError:
        return None
    ratio = (image.width / image.height) / (frame[0] / frame[1])
    return image if abs(ratio - 1) <= ASPECT_TOLERANCE else None


def _cache_path(
    person_id: str, thumb_path: Path, source: Optional[Path],
    x: float, y: float, size: float, frame: tuple[int, int],
) -> Path:
    """Where this person's crop of this face of this photo lives.

    The file name covers everything the pixels depend on, so a changed pick,
    face, thumbnail, source image or crop setting maps to a new file; a
    person's directory holds only their current one.
    """
    stats = [thumb_path.stat()] + ([source.stat()] if source else [])
    key = "|".join(map(str, (
        FACE_CROP_SCALE, CROP_LIFT, CROP_MAX_PX, CROP_JPEG_QUALITY,
        f"{x:.5f}", f"{y:.5f}", f"{size:.5f}", *frame,
        *[(st.st_mtime_ns, st.st_size) for st in stats],
    )))
    person = _person_dir_name(person_id)
    name = hashlib.sha256(key.encode()).hexdigest()[:24]
    return settings.photos_profile_crops_dir / person / f"{name}.jpeg"


def cropped_avatar(person_id: str, thumb_path: Path, pick: dict) -> Optional[Path]:
    """The cached face crop for a pick, creating it when missing; None on any
    failure."""
    try:
        x, y, size = pick["x"], pick["y"], pick["size"]
        frame = (int(pick["fw"]), int(pick["fh"]))
        if min(frame) <= 0:
            return None
        source = _staged_source(pick["asset"])
        target = _cache_path(person_id, thumb_path, source, x, y, size, frame)
        if target.is_file():
            return target

        from PIL import Image

        image = None
        for candidate in (source, thumb_path):
            image = candidate and _upright_image(candidate, frame)
            if image:
                break
        if image is None:
            return None
        box = crop_box(image.width, image.height, x, y, size)
        if box is None:
            return None
        left, top, side = box
        crop = image.convert("RGB").crop((left, top, left + side, top + side))
        if side > CROP_MAX_PX:
            crop = crop.resize((CROP_MAX_PX, CROP_MAX_PX), Image.LANCZOS)

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp:
                crop.save(tmp, "JPEG", quality=CROP_JPEG_QUALITY)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        for stale in target.parent.glob("*.jpeg"):
            if stale != target:
                stale.unlink(missing_ok=True)
        return target
    except Exception as e:
        logger.warning(f"Profile photo crop failed: {type(e).__name__}")
        return None


def _person_dir_name(person_id: str) -> str:
    return hashlib.sha256(person_id.encode()).hexdigest()[:16]


def prune(picks: dict) -> None:
    """Remove staged sources and cached crops that no pick refers to.

    ``picks`` is the full picks mapping, so a source is kept while any person's
    pick uses its asset and a person's crop directory while the person has a pick.
    """
    keep_assets = {
        str(entry["asset"]).upper()
        for entries in picks.values() for entry in entries if isinstance(entry, dict) and "asset" in entry
    }
    sources = settings.photos_profile_sources_dir
    if sources.is_dir():
        for staged in sources.glob("*/*.jpeg"):
            if staged.stem.upper() not in keep_assets:
                staged.unlink(missing_ok=True)
    keep_people = {_person_dir_name(person_id) for person_id in picks}
    crops = settings.photos_profile_crops_dir
    if crops.is_dir():
        for person_dir in crops.iterdir():
            if person_dir.is_dir() and person_dir.name not in keep_people:
                shutil.rmtree(person_dir, ignore_errors=True)
