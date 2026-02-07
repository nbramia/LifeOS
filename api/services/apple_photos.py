"""
Apple Photos Reader - Read-only accessor for Photos.sqlite database.

Provides access to face recognition data from Apple Photos:
- Named people and their face counts
- Photos containing specific people
- Multi-person photos for relationship discovery

Note: Apple Photos uses its own epoch (2001-01-01) for timestamps.
"""
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Seconds from Unix epoch (1970-01-01) to Apple epoch (2001-01-01)
APPLE_EPOCH_OFFSET = 978307200


def apple_timestamp_to_datetime(apple_ts: float | None) -> datetime | None:
    """Convert Apple Core Data timestamp to datetime."""
    if apple_ts is None:
        return None
    unix_ts = apple_ts + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


@dataclass
class PhotosPerson:
    """A named person from Apple Photos face recognition."""
    pk: int
    full_name: str
    display_name: str | None
    face_count: int
    person_uri: str | None  # ZPERSONURI - link to Apple Contacts


@dataclass
class PhotoAsset:
    """A photo asset from Apple Photos."""
    pk: int
    uuid: str
    timestamp: datetime | None
    latitude: float | None
    longitude: float | None
    is_local: bool = True  # True if locally available, False if iCloud-only


@dataclass
class FaceAppearance:
    """A face appearance linking a person to a photo."""
    person_pk: int
    person_name: str
    asset_pk: int
    asset_uuid: str
    timestamp: datetime | None


class ApplePhotosReader:
    """
    Read-only accessor for Apple Photos SQLite database.

    Usage:
        reader = ApplePhotosReader("/path/to/Photos.sqlite")
        people = reader.get_all_people()
        photos = reader.get_photos_for_person(person_pk)
    """

    def __init__(self, db_path: str):
        """
        Initialize reader with path to Photos.sqlite.

        Args:
            db_path: Path to Photos.sqlite (usually in Photos Library.photoslibrary/database/)
        """
        self.db_path = db_path
        if not Path(db_path).exists():
            raise FileNotFoundError(f"Photos database not found: {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """Get a read-only database connection."""
        # Use URI mode for read-only access
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all_people(self) -> list[PhotosPerson]:
        """
        Get all named people from Photos face recognition.

        Returns:
            List of PhotosPerson objects with names and face counts
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT, ZPERSONURI
                FROM ZPERSON
                WHERE ZFULLNAME IS NOT NULL
                  AND ZFACECOUNT > 0
                ORDER BY ZFACECOUNT DESC
            """)
            return [
                PhotosPerson(
                    pk=row["Z_PK"],
                    full_name=row["ZFULLNAME"],
                    display_name=row["ZDISPLAYNAME"],
                    face_count=row["ZFACECOUNT"] or 0,
                    person_uri=row["ZPERSONURI"],
                )
                for row in cursor
            ]
        finally:
            conn.close()

    def get_people_with_contacts(self) -> list[PhotosPerson]:
        """
        Get named people that are linked to Apple Contacts.

        Only returns people with ZPERSONURI set (linked to Contacts).
        This is the reliable matching strategy - skip unlinked people.

        Returns:
            List of PhotosPerson objects with contact links
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT, ZPERSONURI
                FROM ZPERSON
                WHERE ZFULLNAME IS NOT NULL
                  AND ZFACECOUNT > 0
                  AND ZPERSONURI IS NOT NULL
                ORDER BY ZFACECOUNT DESC
            """)
            return [
                PhotosPerson(
                    pk=row["Z_PK"],
                    full_name=row["ZFULLNAME"],
                    display_name=row["ZDISPLAYNAME"],
                    face_count=row["ZFACECOUNT"] or 0,
                    person_uri=row["ZPERSONURI"],
                )
                for row in cursor
            ]
        finally:
            conn.close()

    def get_photos_for_person(self, person_pk: int, limit: int = 100) -> list[PhotoAsset]:
        """
        Get photos containing a specific person.

        Args:
            person_pk: Primary key of the person in ZPERSON table
            limit: Maximum number of photos to return

        Returns:
            List of PhotoAsset objects, newest first
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT
                    a.Z_PK,
                    a.ZUUID,
                    a.ZDATECREATED,
                    a.ZLATITUDE,
                    a.ZLONGITUDE
                FROM ZASSET a
                JOIN ZDETECTEDFACE f ON f.ZASSETFORFACE = a.Z_PK
                WHERE f.ZPERSONFORFACE = ?
                ORDER BY a.ZDATECREATED DESC
                LIMIT ?
            """, (person_pk, limit))

            return [
                PhotoAsset(
                    pk=row["Z_PK"],
                    uuid=row["ZUUID"],
                    timestamp=apple_timestamp_to_datetime(row["ZDATECREATED"]),
                    latitude=row["ZLATITUDE"],
                    longitude=row["ZLONGITUDE"],
                )
                for row in cursor
            ]
        finally:
            conn.close()

    def get_people_in_photo(self, asset_pk: int) -> list[PhotosPerson]:
        """
        Get all named people detected in a specific photo.

        Args:
            asset_pk: Primary key of the photo in ZASSET table

        Returns:
            List of PhotosPerson objects detected in the photo
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT
                    p.Z_PK,
                    p.ZFULLNAME,
                    p.ZDISPLAYNAME,
                    p.ZFACECOUNT,
                    p.ZPERSONURI
                FROM ZPERSON p
                JOIN ZDETECTEDFACE f ON f.ZPERSONFORFACE = p.Z_PK
                WHERE f.ZASSETFORFACE = ?
                  AND p.ZFULLNAME IS NOT NULL
            """, (asset_pk,))

            return [
                PhotosPerson(
                    pk=row["Z_PK"],
                    full_name=row["ZFULLNAME"],
                    display_name=row["ZDISPLAYNAME"],
                    face_count=row["ZFACECOUNT"] or 0,
                    person_uri=row["ZPERSONURI"],
                )
                for row in cursor
            ]
        finally:
            conn.close()

    def get_multi_person_photos(
        self,
        min_people: int = 2,
        limit: int = 5000,
    ) -> Iterator[tuple[PhotoAsset, list[int]]]:
        """
        Get photos with multiple named people (relationship signal).

        These photos indicate people who spend time together.

        Args:
            min_people: Minimum number of named people in photo
            limit: Maximum photos to return

        Yields:
            Tuple of (PhotoAsset, list of person PKs)
        """
        conn = self._get_connection()
        try:
            # First get photos with 2+ named people
            cursor = conn.execute("""
                SELECT
                    a.Z_PK,
                    a.ZUUID,
                    a.ZDATECREATED,
                    a.ZLATITUDE,
                    a.ZLONGITUDE,
                    GROUP_CONCAT(p.Z_PK) as person_pks
                FROM ZASSET a
                JOIN ZDETECTEDFACE f ON f.ZASSETFORFACE = a.Z_PK
                JOIN ZPERSON p ON f.ZPERSONFORFACE = p.Z_PK
                WHERE p.ZFULLNAME IS NOT NULL
                GROUP BY a.Z_PK
                HAVING COUNT(DISTINCT p.Z_PK) >= ?
                ORDER BY a.ZDATECREATED DESC
                LIMIT ?
            """, (min_people, limit))

            for row in cursor:
                asset = PhotoAsset(
                    pk=row["Z_PK"],
                    uuid=row["ZUUID"],
                    timestamp=apple_timestamp_to_datetime(row["ZDATECREATED"]),
                    latitude=row["ZLATITUDE"],
                    longitude=row["ZLONGITUDE"],
                )
                person_pks = [int(pk) for pk in row["person_pks"].split(",")]
                yield asset, person_pks
        finally:
            conn.close()

    def get_faces_since(
        self,
        since: datetime,
        limit: int = 10000,
    ) -> Iterator[FaceAppearance]:
        """
        Get face appearances since a given timestamp.

        Useful for incremental sync.

        Args:
            since: Only return faces from photos after this timestamp
            limit: Maximum faces to return

        Yields:
            FaceAppearance objects
        """
        # Convert to Apple timestamp
        apple_ts = since.timestamp() - APPLE_EPOCH_OFFSET

        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT
                    p.Z_PK as person_pk,
                    p.ZFULLNAME as person_name,
                    a.Z_PK as asset_pk,
                    a.ZUUID as asset_uuid,
                    a.ZDATECREATED as timestamp
                FROM ZDETECTEDFACE f
                JOIN ZPERSON p ON f.ZPERSONFORFACE = p.Z_PK
                JOIN ZASSET a ON f.ZASSETFORFACE = a.Z_PK
                WHERE p.ZFULLNAME IS NOT NULL
                  AND a.ZDATECREATED > ?
                ORDER BY a.ZDATECREATED DESC
                LIMIT ?
            """, (apple_ts, limit))

            for row in cursor:
                yield FaceAppearance(
                    person_pk=row["person_pk"],
                    person_name=row["person_name"],
                    asset_pk=row["asset_pk"],
                    asset_uuid=row["asset_uuid"],
                    timestamp=apple_timestamp_to_datetime(row["timestamp"]),
                )
        finally:
            conn.close()

    def get_person_by_contact_uuid(self, contact_uuid: str) -> PhotosPerson | None:
        """
        Find a Photos person by their linked Apple Contact UUID.

        Args:
            contact_uuid: UUID from Apple Contacts (without :ABPerson suffix)

        Returns:
            PhotosPerson if found, None otherwise
        """
        conn = self._get_connection()
        try:
            # ZPERSONURI format is "UUID:ABPerson"
            cursor = conn.execute("""
                SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT, ZPERSONURI
                FROM ZPERSON
                WHERE ZPERSONURI LIKE ?
                LIMIT 1
            """, (f"{contact_uuid}%",))

            row = cursor.fetchone()
            if row:
                return PhotosPerson(
                    pk=row["Z_PK"],
                    full_name=row["ZFULLNAME"],
                    display_name=row["ZDISPLAYNAME"],
                    face_count=row["ZFACECOUNT"] or 0,
                    person_uri=row["ZPERSONURI"],
                )
            return None
        finally:
            conn.close()

    def get_photo_icloud_status(self, uuid: str) -> bool:
        """
        Check if a photo is available locally or only in iCloud.

        Args:
            uuid: The photo asset UUID

        Returns:
            True if locally available, False if iCloud-only
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT ZCLOUDLOCALSTATE
                FROM ZASSET
                WHERE ZUUID = ?
                LIMIT 1
            """, (uuid,))
            row = cursor.fetchone()
            if row:
                # ZCLOUDLOCALSTATE: 1 = local, 0 = iCloud only
                return row["ZCLOUDLOCALSTATE"] == 1
            return False
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Get summary statistics about the Photos database."""
        conn = self._get_connection()
        try:
            stats = {}

            # Total named people
            cursor = conn.execute("""
                SELECT COUNT(*) FROM ZPERSON WHERE ZFULLNAME IS NOT NULL
            """)
            stats["total_named_people"] = cursor.fetchone()[0]

            # People linked to contacts
            cursor = conn.execute("""
                SELECT COUNT(*) FROM ZPERSON
                WHERE ZFULLNAME IS NOT NULL AND ZPERSONURI IS NOT NULL
            """)
            stats["people_with_contacts"] = cursor.fetchone()[0]

            # Total face detections
            cursor = conn.execute("""
                SELECT COUNT(*) FROM ZDETECTEDFACE f
                JOIN ZPERSON p ON f.ZPERSONFORFACE = p.Z_PK
                WHERE p.ZFULLNAME IS NOT NULL
            """)
            stats["total_face_detections"] = cursor.fetchone()[0]

            # Photos with 2+ named people
            cursor = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT a.Z_PK
                    FROM ZASSET a
                    JOIN ZDETECTEDFACE f ON f.ZASSETFORFACE = a.Z_PK
                    JOIN ZPERSON p ON f.ZPERSONFORFACE = p.Z_PK
                    WHERE p.ZFULLNAME IS NOT NULL
                    GROUP BY a.Z_PK
                    HAVING COUNT(DISTINCT p.Z_PK) >= 2
                )
            """)
            stats["multi_person_photos"] = cursor.fetchone()[0]

            return stats
        finally:
            conn.close()


# Singleton instance
_reader: ApplePhotosReader | None = None


def get_apple_photos_reader(db_path: str | None = None) -> ApplePhotosReader:
    """
    Get the singleton ApplePhotosReader instance.

    Args:
        db_path: Path to Photos.sqlite (uses config default if not provided)

    Returns:
        ApplePhotosReader instance
    """
    global _reader
    if _reader is None:
        if db_path is None:
            from config.settings import settings
            db_path = settings.photos_db_path
        _reader = ApplePhotosReader(db_path)
    return _reader
