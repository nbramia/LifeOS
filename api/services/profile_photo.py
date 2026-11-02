"""Profile photo selection: which of a person's photos makes the best avatar.

A Photos face appearance record carries a few facts about the photo
(``visible_faces``, ``asset_hidden``, ``face_size``, ``face_x``, ``face_y``,
``face_quality``, ``eyes_closed``, ``key_face``) next to ``asset_uuid`` and
``timestamp``. The Apple Data Agent export and the local Photos reader emit the
same shape, and both feed ``build_picks``.

Ranking, best first (each element breaks ties in the one before):

1. a face at least ``MIN_USABLE_FACE_SIZE`` of the image's longer side, so the
   crop around it has the pixels to look sharp; if no photo clears it, this
   step ties and the order below decides
2. fewest visible faces in the photo (solo, then two-person, then groups)
3. a face at least ``MIN_AVATAR_FACE_SIZE`` of the image's longer side
4. eyes open
5. Apple's key face for the person
6. higher face quality, in ``QUALITY_LEVELS`` coarse steps
7. newer photo

A trashed or hidden asset is never picked. A record without usable facts (no
visible-face count, or a face with no measured size) ranks after every record
that has them, so a person whose records carry no facts gets no pick and the
profile route keeps serving their most recent photo.

Ranking needs only the standard library, so the Apple Data Agent export loads
this file by path to choose which photos to stage in higher resolution; the
settings import is deferred to the functions that touch the picks file.

The picks live in one small JSON file (``settings.photos_profile_picks_path``):
``{person_id: [{"asset": uuid, "x": .., "y": .., "size": ..}, ...]}``, best
first, only assets whose thumbnail is available when the file is written. ``x``,
``y`` and ``size`` frame the person's face for ``api.services.profile_crop``
and are absent when the face has no measured geometry. The profile route reads
the file with one dict lookup per avatar request.
"""
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# A face smaller than this makes a crop too small to look sharp: the face is
# 3 face boxes' worth of crop (profile_crop.FACE_CROP_SCALE) of the staged image,
# which is 1024 px on the long edge for most photos, so 0.05 gives a crop of about
# 150 px, the 160 device pixels of the 80 px detail avatar. A 2048 px staged image
# doubles it; the 480 px thumbnail alone halves it.
MIN_USABLE_FACE_SIZE = 0.05
# A face this large (fraction of the image's longer side) or larger can read as
# an avatar. About the median face in single-face photos of a real library.
MIN_AVATAR_FACE_SIZE = 0.10
# Face quality (0..1, higher is better) is compared in this many steps, so
# recency decides between near-equal faces.
QUALITY_LEVELS = 10
# Quality Photos did not score (missing or negative) counts as the lowest.
UNKNOWN_QUALITY = 0.0
# Ranked assets kept per person, so a missing thumbnail falls to the next one.
MAX_STORED_PICKS = 5


@dataclass(frozen=True)
class Appearance:
    """One photo of a person, with the facts the ranking uses."""
    asset_uuid: str
    timestamp: float = 0.0
    visible_faces: Optional[int] = None
    face_size: Optional[float] = None
    face_x: Optional[float] = None
    face_y: Optional[float] = None
    quality: Optional[float] = None
    eyes_closed: bool = False
    key_face: bool = False
    asset_hidden: bool = False

    @property
    def has_facts(self) -> bool:
        return (self.visible_faces or 0) >= 1 and (self.face_size or 0) > 0

    @property
    def has_geometry(self) -> bool:
        return (
            self.face_x is not None and 0 <= self.face_x <= 1
            and self.face_y is not None and 0 <= self.face_y <= 1
        )


def appearance_from_record(record: dict) -> Optional[Appearance]:
    """An Appearance from an export-shaped record, or None without an asset."""
    asset_uuid = record.get("asset_uuid")
    if not asset_uuid:
        return None
    timestamp = 0.0
    if record.get("timestamp"):
        try:
            timestamp = datetime.fromisoformat(record["timestamp"]).timestamp()
        except (ValueError, TypeError):
            pass
    return Appearance(
        asset_uuid=asset_uuid,
        timestamp=timestamp,
        visible_faces=record.get("visible_faces"),
        face_size=record.get("face_size"),
        face_x=record.get("face_x"),
        face_y=record.get("face_y"),
        quality=record.get("face_quality"),
        eyes_closed=bool(record.get("eyes_closed")),
        key_face=bool(record.get("key_face")),
        asset_hidden=bool(record.get("asset_hidden")),
    )


def rank_key(appearance: Appearance) -> tuple:
    """Sort key: smaller is a better avatar."""
    if not appearance.has_facts:
        return (1, False, 0, False, False, False, 0, -appearance.timestamp, appearance.asset_uuid)
    quality = appearance.quality
    if quality is None or quality < 0:
        quality = UNKNOWN_QUALITY
    return (
        0,
        appearance.face_size < MIN_USABLE_FACE_SIZE,
        appearance.visible_faces,
        appearance.face_size < MIN_AVATAR_FACE_SIZE,
        appearance.eyes_closed,
        not appearance.key_face,
        -round(quality * QUALITY_LEVELS),
        -appearance.timestamp,
        appearance.asset_uuid,
    )


def select_profile_photos(
    appearances: Iterable[Appearance],
    has_thumbnail: Callable[[str], bool],
    limit: int = MAX_STORED_PICKS,
) -> list[Appearance]:
    """Up to ``limit`` appearances of distinct assets, best avatar first.

    Only visible (not trashed or hidden) assets with a thumbnail qualify. Empty
    when the best qualifying asset has no facts, because the person's records
    carry none.
    """
    picks: list[Appearance] = []
    chosen: set[str] = set()
    for appearance in sorted(appearances, key=rank_key):
        if (
            appearance.asset_hidden
            or appearance.asset_uuid in chosen
            or not has_thumbnail(appearance.asset_uuid)
        ):
            continue
        if not picks and not appearance.has_facts:
            return []
        picks.append(appearance)
        chosen.add(appearance.asset_uuid)
        if len(picks) == limit:
            break
    return picks


def pick_entry(appearance: Appearance) -> dict:
    """The stored form of a pick: the asset plus its face geometry, if any."""
    entry: dict = {"asset": appearance.asset_uuid}
    if appearance.has_facts and appearance.has_geometry:
        entry.update(x=appearance.face_x, y=appearance.face_y, size=appearance.face_size)
    return entry


def thumbnail_available(asset_uuid: str) -> bool:
    """Whether the profile route could serve a thumbnail for this asset."""
    from api.routes.photos import _find_thumbnail_path

    return _find_thumbnail_path(asset_uuid) is not None


def build_picks(
    records_by_person: dict[str, Iterable[dict]],
    has_thumbnail: Callable[[str], bool] = thumbnail_available,
) -> dict[str, list[dict]]:
    """``{person_id: ranked pick entries}`` for people that have a pick."""
    picks: dict[str, list[dict]] = {}
    for person_id, records in records_by_person.items():
        appearances = [a for a in map(appearance_from_record, records) if a]
        chosen = select_profile_photos(appearances, has_thumbnail)
        if chosen:
            picks[person_id] = [pick_entry(a) for a in chosen]
    return picks


def write_picks(picks: dict[str, list[dict]]) -> None:
    """Replace the picks file atomically.

    No picks means the data had no usable facts (an export from a Mac that
    could not read them, a host without a Photos library), so the file stays.
    """
    if not picks:
        logger.info("No profile photo picks to store; keeping the existing picks file")
        return
    from config.settings import settings

    path = settings.photos_profile_picks_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(picks, tmp)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


_cache: tuple = (None, {})


def load_picks() -> dict[str, list[dict]]:
    """The picks file's contents, re-read only when the file changes."""
    global _cache
    from config.settings import settings

    path = settings.photos_profile_picks_path
    try:
        stat = path.stat()
    except OSError:
        return {}
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    if _cache[0] == signature:
        return _cache[1]
    try:
        picks = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Profile photo picks file is unreadable; ignoring it")
        return {}
    if not isinstance(picks, dict):
        return {}
    _cache = (signature, picks)
    return picks
