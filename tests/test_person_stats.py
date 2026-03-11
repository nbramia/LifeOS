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

        with patch("api.services.person_entity.get_person_entity_store", return_value=mock_store), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            refresh_person_stats([person_id], save=False)

        # 2 phone + 1 imessage = 3 messages
        assert entity.message_count == 3


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
        assert entity.message_count == 12  # imessage + whatsapp + phone
        assert entity.slack_message_count == 6
        assert entity.photo_count == 8

    def test_slack_maps_to_slack_message_count(self):
        """Verify 'slack' maps to slack_message_count, not message_count."""
        entity = PersonEntity(canonical_name="Test")
        counts = {'slack': 10}
        _apply_counts_to_entity(entity, counts)
        assert entity.slack_message_count == 10
        assert entity.message_count == 0

    def test_photos_maps_to_photo_count(self):
        """Verify 'photos' maps to photo_count."""
        entity = PersonEntity(canonical_name="Test")
        counts = {'photos': 15}
        _apply_counts_to_entity(entity, counts)
        assert entity.photo_count == 15
