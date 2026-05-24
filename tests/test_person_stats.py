"""Tests for person stats refresh - counts and timestamp updates."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from api.services.person_entity import PersonEntity
from api.services.person_stats import (
    refresh_person_stats,
    _apply_counts_to_entity,
)


def _create_interaction_db(db_path: str, interactions: list[tuple]) -> None:
    """Create a test interactions database with given rows."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            person_id TEXT,
            timestamp TEXT,
            source_type TEXT,
            title TEXT,
            snippet TEXT,
            source_link TEXT,
            source_id TEXT,
            created_at TEXT
        )
    """)
    conn.executemany("""
        INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, interactions)
    conn.commit()
    conn.close()


def _create_crm_db(db_path: str, source_entities: list[tuple]) -> None:
    """Create a test crm database with source_entities rows."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_entities (
            id TEXT PRIMARY KEY,
            canonical_person_id TEXT,
            observed_at TEXT,
            source_type TEXT
        )
    """)
    conn.executemany("""
        INSERT INTO source_entities (id, canonical_person_id, observed_at, source_type)
        VALUES (?, ?, ?, ?)
    """, source_entities)
    conn.commit()
    conn.close()


class TestRefreshUpdatesLastSeen:
    """Test that refresh_person_stats updates last_seen from interactions."""

    def test_refresh_updates_last_seen_from_interactions(self, tmp_path):
        """last_seen should match MAX(timestamp) from interactions."""
        person_id = "test-person-001"
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=7)

        # Create interaction DB with timestamps
        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            ("i1", person_id, last_week.isoformat(), "imessage", "msg1", "", None, "src1", now.isoformat()),
            ("i2", person_id, yesterday.isoformat(), "whatsapp", "msg2", "", None, "src2", now.isoformat()),
        ])

        # Create entity with stale last_seen
        entity = PersonEntity(
            id=person_id,
            canonical_name="Alex Test",
            last_seen=last_week - timedelta(days=30),
        )

        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = set()
        mock_store.get_all.return_value = [entity]

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            # No crm.db - only interactions
            mock_path.return_value.exists.return_value = False

            result = refresh_person_stats([person_id], save=False)

        assert result['updated'] == 1
        # last_seen should be approximately yesterday (the MAX timestamp)
        assert entity.last_seen is not None
        assert abs((entity.last_seen - yesterday).total_seconds()) < 2

    def test_refresh_uses_earliest_first_seen(self, tmp_path):
        """first_seen should be the earliest across interactions and source_entities."""
        person_id = "test-person-002"
        now = datetime.now(timezone.utc)
        interaction_first = now - timedelta(days=30)
        source_entity_first = now - timedelta(days=90)

        # Interaction DB (newer first_seen)
        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            ("i1", person_id, interaction_first.isoformat(), "gmail", "email", "", None, "src1", now.isoformat()),
        ])

        # CRM DB with older source_entity
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, [
            ("se1", person_id, source_entity_first.isoformat(), "linkedin"),
        ])

        entity = PersonEntity(id=person_id, canonical_name="Alex Test")
        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = set()

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path", return_value=Path(crm_db)):
            refresh_person_stats([person_id], save=False)

        # first_seen should be the source_entity date (older)
        assert entity.first_seen is not None
        assert abs((entity.first_seen - source_entity_first).total_seconds()) < 2

    def test_future_calendar_capped_at_now(self, tmp_path):
        """Future calendar events should not set last_seen in the future."""
        person_id = "test-person-003"
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=30)
        past = now - timedelta(days=5)

        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            ("i1", person_id, past.isoformat(), "imessage", "msg", "", None, "src1", now.isoformat()),
            ("i2", person_id, future.isoformat(), "calendar", "future mtg", "", None, "src2", now.isoformat()),
        ])

        entity = PersonEntity(id=person_id, canonical_name="Alex Test")
        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = set()

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            refresh_person_stats([person_id], save=False)

        # last_seen should be capped at approximately now, not in the future
        assert entity.last_seen is not None
        assert entity.last_seen <= now + timedelta(seconds=5)

    def test_phone_source_type_counted(self, tmp_path):
        """'phone' source_type should contribute to message_count (not 'sms')."""
        person_id = "test-person-004"
        now = datetime.now(timezone.utc)

        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            ("i1", person_id, now.isoformat(), "phone", "call", "", None, "src1", now.isoformat()),
            ("i2", person_id, now.isoformat(), "phone", "call2", "", None, "src2", now.isoformat()),
            ("i3", person_id, now.isoformat(), "imessage", "msg", "", None, "src3", now.isoformat()),
        ])

        entity = PersonEntity(id=person_id, canonical_name="Alex Test")
        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = set()

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            refresh_person_stats([person_id], save=False)

        # 2 phone + 1 imessage = 3 messages
        assert entity.message_count == 3


class TestMergedLegacyIds:
    """Regression: full refresh used to corrupt canonicals that had legacy
    (pre-merge) person_ids still attached to interaction rows. The loop would
    process the canonical first (correct counts), then process the legacy ID,
    resolve it via get_by_id() to the *same canonical*, and overwrite with the
    legacy ID's under-counts and older last_seen. Fix: aggregate by canonical
    before applying."""

    def test_full_refresh_aggregates_legacy_into_canonical(self, tmp_path):
        canonical_id = "canonical-001"
        legacy_id = "legacy-002"
        now = datetime.now(timezone.utc)
        legacy_time = now - timedelta(days=400)
        canonical_time = now - timedelta(days=1)

        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            # 5 emails on the canonical, recent
            *[(f"c{i}", canonical_id, canonical_time.isoformat(), "gmail",
               "e", "", None, f"src-c{i}", now.isoformat()) for i in range(5)],
            # 2 emails on the legacy id, old
            *[(f"l{i}", legacy_id, legacy_time.isoformat(), "gmail",
               "e", "", None, f"src-l{i}", now.isoformat()) for i in range(2)],
        ])

        # Canonical entity. get_by_id follows the merge map: both
        # canonical_id and legacy_id return the SAME entity.
        entity = PersonEntity(id=canonical_id, canonical_name="Merged Person")

        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = set()
        mock_store.get_all.return_value = [entity]

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            refresh_person_stats(person_ids=None, save=False)

        # Counts must be SUM (7), not REPLACED with the legacy under-count (2).
        assert entity.email_count == 7
        # last_seen must be the recent canonical timestamp, not the 400-day-old
        # legacy timestamp.
        assert abs((entity.last_seen - canonical_time).total_seconds()) < 2

    def test_targeted_refresh_handles_legacy_id(self, tmp_path):
        """Targeted refresh called with a legacy ID should update the canonical
        with the union of counts, not just the legacy subset."""
        canonical_id = "canonical-003"
        legacy_id = "legacy-004"
        now = datetime.now(timezone.utc)

        int_db = str(tmp_path / "interactions.db")
        _create_interaction_db(int_db, [
            ("c1", canonical_id, now.isoformat(), "gmail", "e", "", None, "src-c1", now.isoformat()),
            ("c2", canonical_id, now.isoformat(), "gmail", "e", "", None, "src-c2", now.isoformat()),
            ("l1", legacy_id, now.isoformat(), "gmail", "e", "", None, "src-l1", now.isoformat()),
        ])

        entity = PersonEntity(id=canonical_id, canonical_name="Merged Person")
        mock_store = MagicMock()
        mock_store.get_by_id.return_value = entity
        mock_store.get_legacy_ids.return_value = {legacy_id}

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            # Caller passes only the legacy id; the store's reverse merge
            # lookup expands to the canonical + every sibling legacy ID,
            # so counts should reflect ALL interactions for the canonical.
            refresh_person_stats([legacy_id], save=False)

        assert entity.email_count == 3


class TestApplyCountsToEntity:
    """Tests for _apply_counts_to_entity mapping."""

    def test_phone_maps_to_message_count(self):
        """Verify 'phone' (not 'sms') maps to message_count."""
        entity = PersonEntity(canonical_name="Test")
        counts = {'phone': 5, 'imessage': 3, 'whatsapp': 2}
        _apply_counts_to_entity(entity, counts)
        assert entity.message_count == 10

    def test_all_source_types(self):
        entity = PersonEntity(canonical_name="Test")
        counts = {
            'gmail': 10,
            'calendar': 5,
            'vault': 3,
            'granola': 2,
            'imessage': 7,
            'whatsapp': 4,
            'phone': 1,
            'slack': 6,
            'photos': 8,
        }
        _apply_counts_to_entity(entity, counts)
        assert entity.email_count == 10
        assert entity.meeting_count == 5
        assert entity.mention_count == 5  # vault + granola
        assert entity.message_count == 18  # imessage + whatsapp + phone + slack
        assert entity.photo_count == 8

    def test_slack_maps_to_message_count(self):
        """Verify 'slack' maps to message_count (slack_message_count is managed by slack_sync)."""
        entity = PersonEntity(canonical_name="Test")
        counts = {'slack': 10}
        _apply_counts_to_entity(entity, counts)
        assert entity.message_count == 10
        assert entity.slack_message_count == 0  # Not touched by _apply_counts

    def test_photos_maps_to_photo_count(self):
        """Verify 'photos' maps to photo_count."""
        entity = PersonEntity(canonical_name="Test")
        counts = {'photos': 15}
        _apply_counts_to_entity(entity, counts)
        assert entity.photo_count == 15
