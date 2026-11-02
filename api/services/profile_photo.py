"""Profile photo selection: which of a person's photos makes the best avatar.

A Photos face appearance record carries a few facts about the photo
(``visible_faces``, ``face_size``, ``face_quality``, ``eyes_closed``,
``key_face``) next to ``asset_uuid`` and ``timestamp``. The Apple Data Agent
export and the local Photos reader emit the same shape, and both feed
``build_picks``.

Ranking, best first (each element breaks ties in the one before):

1. fewest visible faces in the photo (solo, then two-person, then groups)
2. a face at least ``MIN_AVATAR_FACE_SIZE`` of the frame wide
3. eyes open
4. Apple's key face for the person
5. higher face quality, in ``QUALITY_LEVELS`` coarse steps
6. newer photo

A record without usable facts (no visible-face count, or a face with no
measured size) ranks after every record that has them, so a person whose
records carry no facts gets no pick and the profile route keeps serving their
most recent photo.

The picks live in one small JSON file (``settings.photos_profile_picks_path``):
``{person_id: [asset_uuid, ...]}``, best first, only assets whose thumbnail is
available when the file is written. The profile route reads it with one dict
lookup per avatar request.
"""
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# A face this wide (fraction of the frame) or wider can read as an avatar. About
# the median face width in single-face photos of a real library.
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
    quality: Optional[float] = None
    eyes_closed: bool = False
    key_face: bool = False

    @property
    def has_facts(self) -> bool:
        return (self.visible_faces or 0) >= 1 and (self.face_size or 0) > 0


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
        quality=record.get("face_quality"),
        eyes_closed=bool(record.get("eyes_closed")),
        key_face=bool(record.get("key_face")),
    )


def rank_key(appearance: Appearance) -> tuple:
    """Sort key: smaller is a better avatar."""
    if not appearance.has_facts:
        return (1, 0, False, False, False, 0, -appearance.timestamp, appearance.asset_uuid)
    quality = appearance.quality
    if quality is None or quality < 0:
        quality = UNKNOWN_QUALITY
    return (
        0,
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
) -> list[str]:
    """Up to ``limit`` distinct asset UUIDs, best avatar first.

    Only assets with a thumbnail qualify. Empty when the best qualifying
    asset has no facts, because the person's records carry none.
    """
    picks: list[str] = []
    for appearance in sorted(appearances, key=rank_key):
        if appearance.asset_uuid in picks or not has_thumbnail(appearance.asset_uuid):
            continue
        if not picks and not appearance.has_facts:
            return []
        picks.append(appearance.asset_uuid)
        if len(picks) == limit:
            break
    return picks


def thumbnail_available(asset_uuid: str) -> bool:
    """Whether the profile route could serve a thumbnail for this asset."""
    from api.routes.photos import _find_thumbnail_path

    return _find_thumbnail_path(asset_uuid) is not None


def build_picks(
    records_by_person: dict[str, Iterable[dict]],
    has_thumbnail: Callable[[str], bool] = thumbnail_available,
) -> dict[str, list[str]]:
    """``{person_id: ranked asset UUIDs}`` for people that have a pick."""
    picks: dict[str, list[str]] = {}
    for person_id, records in records_by_person.items():
        appearances = [a for a in map(appearance_from_record, records) if a]
        chosen = select_profile_photos(appearances, has_thumbnail)
        if chosen:
            picks[person_id] = chosen
    return picks


def write_picks(picks: dict[str, list[str]]) -> None:
    """Replace the picks file atomically; remove it when there are no picks."""
    path = settings.photos_profile_picks_path
    if not picks:
        path.unlink(missing_ok=True)
        return
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


def load_picks() -> dict[str, list[str]]:
    """The picks file's contents, re-read only when the file changes."""
    global _cache
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
