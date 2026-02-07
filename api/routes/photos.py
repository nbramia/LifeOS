"""
Apple Photos API endpoints for LifeOS.

Provides endpoints for querying Photos face recognition data
and syncing to LifeOS CRM.
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from config.settings import settings

router = APIRouter(prefix="/api/photos", tags=["photos"])


class PhotoResponse(BaseModel):
    """Response model for a photo."""
    uuid: str
    timestamp: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source_link: str


class PhotosForPersonResponse(BaseModel):
    """Response for photos of a person."""
    person_id: str
    photos: list[PhotoResponse]
    count: int


class CoAppearanceResponse(BaseModel):
    """Response for co-appearance data."""
    person_a_id: str
    person_b_id: str
    shared_photo_count: int
    photos: list[PhotoResponse]


class PhotosPersonResponse(BaseModel):
    """Response for a Photos person."""
    pk: int
    full_name: str
    display_name: Optional[str] = None
    face_count: int
    has_contact_link: bool
    matched_entity_id: Optional[str] = None


class PhotosStatsResponse(BaseModel):
    """Response for Photos statistics."""
    total_named_people: int
    people_with_contacts: int
    total_face_detections: int
    multi_person_photos: int
    photos_enabled: bool


class SyncResponse(BaseModel):
    """Response for sync operation."""
    success: bool
    stats: dict
    message: str


def _check_photos_enabled():
    """Check if Photos integration is available."""
    if not settings.photos_enabled:
        raise HTTPException(
            status_code=503,
            detail="Photos integration not available. Photos library not mounted or accessible."
        )


@router.get("/stats", response_model=PhotosStatsResponse)
async def get_photos_stats():
    """
    Get statistics about the Photos library.

    Returns counts of named people, face detections, and multi-person photos.
    """
    if not settings.photos_enabled:
        return PhotosStatsResponse(
            total_named_people=0,
            people_with_contacts=0,
            total_face_detections=0,
            multi_person_photos=0,
            photos_enabled=False,
        )

    try:
        from api.services.apple_photos import get_apple_photos_reader

        reader = get_apple_photos_reader()
        stats = reader.get_stats()

        return PhotosStatsResponse(
            total_named_people=stats.get("total_named_people", 0),
            people_with_contacts=stats.get("people_with_contacts", 0),
            total_face_detections=stats.get("total_face_detections", 0),
            multi_person_photos=stats.get("multi_person_photos", 0),
            photos_enabled=True,
        )
    except FileNotFoundError:
        return PhotosStatsResponse(
            total_named_people=0,
            people_with_contacts=0,
            total_face_detections=0,
            multi_person_photos=0,
            photos_enabled=False,
        )


@router.get("/people", response_model=list[PhotosPersonResponse])
async def list_photos_people(
    linked_only: bool = Query(
        default=True,
        description="Only return people linked to Apple Contacts"
    ),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    List people recognized in Photos.

    Returns face recognition data from Apple Photos with optional
    filtering for people linked to Contacts (more reliable matching).
    """
    _check_photos_enabled()

    from api.services.apple_photos import get_apple_photos_reader
    from api.services.apple_photos_sync import ApplePhotosSync

    reader = get_apple_photos_reader()

    if linked_only:
        people = reader.get_people_with_contacts()
    else:
        people = reader.get_all_people()

    # Match to PersonEntity
    syncer = ApplePhotosSync(photos_reader=reader)
    results = []

    for person in people[:limit]:
        entity_id = None
        if person.person_uri:
            entity_id = syncer.match_photos_person_to_entity(person)

        results.append(PhotosPersonResponse(
            pk=person.pk,
            full_name=person.full_name,
            display_name=person.display_name,
            face_count=person.face_count,
            has_contact_link=person.person_uri is not None,
            matched_entity_id=entity_id,
        ))

    return results


@router.get("/person/{person_id}", response_model=PhotosForPersonResponse)
async def get_photos_for_person(
    person_id: str,
    date: Optional[str] = Query(None, description="Filter by date (YYYY-MM-DD)"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Get photos containing a specific person.

    Requires a LifeOS PersonEntity ID. Returns photos where this
    person's face was recognized.

    Optionally filter by date (YYYY-MM-DD) to get photos from a specific day.
    """
    _check_photos_enabled()

    from api.services.interaction_store import get_interaction_store

    interaction_store = get_interaction_store()

    # Get photo interactions for this person
    # Use high limit to get all photos (default 1000 is too low for people with many photos)
    interactions = interaction_store.get_for_person(
        person_id,
        source_type="photos",
        limit=10000,  # Support up to 10k photos per person
    )

    # Filter by date if provided
    if date:
        interactions = [
            i for i in interactions
            if i.timestamp and i.timestamp.strftime('%Y-%m-%d') == date
        ]

    photos = []
    for interaction in interactions[:limit]:
        photos.append(PhotoResponse(
            uuid=interaction.source_id or "",
            timestamp=interaction.timestamp.isoformat() if interaction.timestamp else None,
            latitude=None,  # Not stored in interaction
            longitude=None,
            source_link=interaction.source_link,
        ))

    return PhotosForPersonResponse(
        person_id=person_id,
        photos=photos,
        count=len(photos),
    )


@router.get("/shared/{person_a_id}/{person_b_id}", response_model=CoAppearanceResponse)
async def get_shared_photos(
    person_a_id: str,
    person_b_id: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Get photos where two people appear together.

    Returns photos where both people's faces were recognized.
    """
    _check_photos_enabled()

    from api.services.interaction_store import get_interaction_store

    interaction_store = get_interaction_store()

    # Get photo interactions for both people
    photos_a = interaction_store.get_for_person(person_a_id, source_type="photos")
    photos_b = interaction_store.get_for_person(person_b_id, source_type="photos")

    # Find shared photos by source_id (asset UUID)
    uuids_a = {i.source_id for i in photos_a if i.source_id}
    shared_uuids = {i.source_id for i in photos_b if i.source_id and i.source_id in uuids_a}

    # Get details for shared photos
    photos = []
    for interaction in photos_a:
        if interaction.source_id in shared_uuids and len(photos) < limit:
            photos.append(PhotoResponse(
                uuid=interaction.source_id or "",
                timestamp=interaction.timestamp.isoformat() if interaction.timestamp else None,
                latitude=None,
                longitude=None,
                source_link=interaction.source_link,
            ))

    return CoAppearanceResponse(
        person_a_id=person_a_id,
        person_b_id=person_b_id,
        shared_photo_count=len(shared_uuids),
        photos=photos,
    )


@router.post("/sync", response_model=SyncResponse)
async def trigger_photo_sync(
    incremental: bool = Query(
        default=True,
        description="If true, only sync new photos since last sync"
    ),
):
    """
    Trigger Photos sync to LifeOS CRM.

    Creates SourceEntity and Interaction records for matched people
    in Photos face recognition data.
    """
    _check_photos_enabled()

    from api.services.apple_photos_sync import sync_apple_photos

    try:
        # For now, always do full sync (incremental requires tracking state)
        stats = sync_apple_photos(since=None)

        return SyncResponse(
            success=True,
            stats=stats,
            message=f"Synced {stats.get('person_matches', 0)} people, "
                    f"created {stats.get('interactions_created', 0)} interactions"
        )
    except Exception as e:
        return SyncResponse(
            success=False,
            stats={"error": str(e)},
            message=f"Sync failed: {e}"
        )


def _get_thumbnail_path(uuid: str) -> Path | None:
    """
    Find a thumbnail/derivative for a photo UUID.

    Photos library stores derivatives in resources/derivatives/{first_char}/
    with naming pattern: UUID_1_105_c.jpeg (or similar suffixes).

    Args:
        uuid: Photo asset UUID

    Returns:
        Path to thumbnail file if found, None otherwise
    """
    if not uuid:
        return None

    library_path = Path(settings.photos_library_path)
    derivatives_base = library_path / "resources" / "derivatives"

    if not derivatives_base.exists():
        return None

    # First character of UUID determines subdirectory
    first_char = uuid[0].upper()
    derivatives_dir = derivatives_base / first_char

    if not derivatives_dir.exists():
        return None

    # Look for any derivative matching this UUID
    # Common suffixes: _1_105_c.jpeg (small), _1_102_o.jpeg (medium)
    for suffix in ["_1_105_c.jpeg", "_1_102_o.jpeg", "_1_201_a.jpeg", ".THM"]:
        thumb_path = derivatives_dir / f"{uuid}{suffix}"
        if thumb_path.exists():
            return thumb_path

    # Try glob pattern as fallback
    matches = list(derivatives_dir.glob(f"{uuid}*"))
    if matches:
        # Prefer jpeg over THM
        for match in matches:
            if match.suffix.lower() in [".jpeg", ".jpg"]:
                return match
        return matches[0]

    return None


@router.get("/thumbnail/{uuid}")
async def get_photo_thumbnail(uuid: str):
    """
    Get a thumbnail for a photo by UUID.

    Returns the thumbnail image from the Photos library's derivatives folder.
    Returns 404 if no thumbnail is available.
    Returns 410 (Gone) if photo is in iCloud only (use X-iCloud-Only header to detect).
    """
    _check_photos_enabled()

    thumb_path = _get_thumbnail_path(uuid)
    if thumb_path is None:
        # Check if photo is in iCloud only
        from api.services.apple_photos import get_apple_photos_reader
        try:
            reader = get_apple_photos_reader()
            is_local = reader.get_photo_icloud_status(uuid)
            if not is_local:
                # Photo exists but is in iCloud only - return 410 with indicator
                raise HTTPException(
                    status_code=410,
                    detail="icloud-only",
                    headers={"X-iCloud-Only": "true"}
                )
        except HTTPException:
            raise  # Re-raise HTTP exceptions
        except Exception:
            pass  # If we can't check, just return 404

        raise HTTPException(
            status_code=404,
            detail=f"No thumbnail available for photo {uuid}"
        )

    # Determine media type
    suffix = thumb_path.suffix.lower()
    media_type = "image/jpeg"
    if suffix == ".png":
        media_type = "image/png"
    elif suffix == ".heic":
        media_type = "image/heic"
    elif suffix == ".thm":
        media_type = "image/jpeg"

    return FileResponse(
        thumb_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"}  # Cache for 1 day
    )


@router.get("/profile/{person_id}")
async def get_profile_photo(person_id: str):
    """
    Get a profile photo thumbnail for a person.

    Returns the most recent photo of the person that has a thumbnail available.
    Use this for avatars/profile pictures in the UI.
    """
    _check_photos_enabled()

    from api.services.interaction_store import get_interaction_store

    interaction_store = get_interaction_store()

    # Get photo interactions for this person, most recent first
    interactions = interaction_store.get_for_person(
        person_id,
        source_type="photos",
    )

    # Find the first one with an available thumbnail
    for interaction in interactions[:50]:  # Check up to 50 most recent
        if interaction.source_id:
            thumb_path = _get_thumbnail_path(interaction.source_id)
            if thumb_path and thumb_path.exists():
                suffix = thumb_path.suffix.lower()
                media_type = "image/jpeg"
                if suffix == ".png":
                    media_type = "image/png"

                return FileResponse(
                    thumb_path,
                    media_type=media_type,
                    headers={"Cache-Control": "public, max-age=3600"}  # Cache for 1 hour
                )

    # No photo available
    raise HTTPException(
        status_code=404,
        detail=f"No profile photo available for person {person_id}"
    )


def _get_photo_file_path(uuid: str) -> Path | None:
    """
    Get the original file path for a photo by UUID.

    Checks both originals folder and queries the database for filename.
    """
    if not uuid:
        return None

    library_path = Path(settings.photos_library_path)
    originals_base = library_path / "originals"

    if not originals_base.exists():
        return None

    # First character determines subdirectory
    first_char = uuid[0].upper()
    originals_dir = originals_base / first_char

    if not originals_dir.exists():
        return None

    # Try common extensions
    for ext in [".jpeg", ".jpg", ".heic", ".png", ".mov", ".mp4"]:
        file_path = originals_dir / f"{uuid}{ext}"
        if file_path.exists():
            return file_path

    return None


@router.get("/open/{uuid}")
async def open_photo_in_app(uuid: str):
    """
    Open a specific photo in the Photos app or Preview.

    Tries in order:
    1. Open the original file directly in Preview (best quality)
    2. Open the thumbnail/derivative if original is in iCloud only
    3. Fall back to opening Photos app
    """
    _check_photos_enabled()

    import subprocess

    # Try to find and open the original file
    file_path = _get_photo_file_path(uuid)
    if file_path and file_path.exists():
        try:
            # Open the file with the default app (Preview for images)
            subprocess.run(
                ["open", str(file_path)],
                capture_output=True,
                timeout=5,
            )
            return {"success": True, "message": f"Opened {file_path.name}"}
        except Exception:
            pass  # Fall through to thumbnail

    # Try opening the thumbnail/derivative (often available even for iCloud photos)
    thumb_path = _get_thumbnail_path(uuid)
    if thumb_path and thumb_path.exists():
        try:
            subprocess.run(
                ["open", str(thumb_path)],
                capture_output=True,
                timeout=5,
            )
            return {"success": True, "message": f"Opened thumbnail (original in iCloud)"}
        except Exception:
            pass  # Fall through to Photos app

    # Fall back to opening Photos app
    script = '''
    tell application "Photos"
        activate
    end tell
    '''

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
        return {"success": True, "message": "Photos app opened (photo may be in iCloud)"}
    except subprocess.TimeoutExpired:
        return {"success": True, "message": "Photos app launched"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to open Photos: {e}"
        )
