"""
Tests for RelationshipSummary module.
"""
import pytest
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from api.services.relationship_summary import (
    ChannelActivity,
    RelationshipSummary,
    get_relationship_summary,
    format_relationship_context,
    RECENT_ACTIVITY_DAYS,
)


class TestChannelActivity:
    """Tests for ChannelActivity dataclass."""

    def test_days_since_last_with_recent_interaction(self):
        """Test days calculation for recent interaction."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        activity = ChannelActivity(
            source_type="gmail",
            count_90d=10,
            last_interaction=yesterday,
            is_recent=True,
        )
        assert activity.days_since_last == 1

    def test_days_since_last_with_old_interaction(self):
        """Test days calculation for older interaction."""
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        activity = ChannelActivity(
            source_type="calendar",
            count_90d=5,
            last_interaction=thirty_days_ago,
            is_recent=False,
        )
        assert activity.days_since_last == 30

    def test_days_since_last_none(self):
        """Test days calculation when no interaction."""
        activity = ChannelActivity(
            source_type="slack",
            count_90d=0,
            last_interaction=None,
            is_recent=False,
        )
        assert activity.days_since_last is None


class TestRelationshipSummary:
    """Tests for RelationshipSummary dataclass."""

    def test_to_dict(self):
        """Test JSON serialization."""
        now = datetime.now(timezone.utc)
        summary = RelationshipSummary(
            person_id="test-123",
            person_name="Test Person",
            relationship_strength=75.5,
            channels=[
                ChannelActivity("gmail", 10, now - timedelta(days=2), True),
                ChannelActivity("imessage", 25, now - timedelta(days=1), True),
            ],
            active_channels=["gmail", "imessage"],
            primary_channel="imessage",
            total_interactions_90d=35,
            last_interaction=now - timedelta(days=1),
            days_since_contact=1,
            facts_count=3,
            has_facts=True,
        )

        data = summary.to_dict()

        assert data["person_id"] == "test-123"
        assert data["person_name"] == "Test Person"
        assert data["relationship_strength"] == 75.5
        assert len(data["channels"]) == 2
        assert data["active_channels"] == ["gmail", "imessage"]
        assert data["primary_channel"] == "imessage"
        assert data["total_interactions_90d"] == 35
        assert data["days_since_contact"] == 1
        assert data["facts_count"] == 3
        assert data["has_facts"] is True

        # Check channel serialization
        gmail_channel = next(c for c in data["channels"] if c["source_type"] == "gmail")
        assert gmail_channel["count_90d"] == 10
        assert gmail_channel["is_recent"] is True
        assert "last_interaction" in gmail_channel


class TestGetRelationshipSummary:
    """Tests for get_relationship_summary function."""

    @pytest.fixture
    def mock_stores(self):
        """Set up mock stores for testing."""
        with patch("api.services.person_entity.get_person_entity_store") as mock_person_store, \
             patch("api.services.interaction_store.get_interaction_store") as mock_interaction_store, \
             patch("api.services.person_facts.get_person_fact_store") as mock_fact_store:

            # Create mock person
            mock_person = MagicMock()
            mock_person.id = "person-123"
            mock_person.display_name = "John Doe"
            mock_person.canonical_name = "John Doe"
            mock_person.relationship_strength = 68.5
            mock_person.last_seen = datetime.now(timezone.utc) - timedelta(days=3)

            # Configure person store
            mock_person_store.return_value.get_by_id.return_value = mock_person

            # Configure interaction store
            now = datetime.now(timezone.utc)
            mock_interaction_store.return_value.get_interaction_counts.return_value = {
                "gmail": 15,
                "imessage": 42,
                "calendar": 8,
            }
            mock_interaction_store.return_value.get_last_interaction_by_source.return_value = {
                "gmail": now - timedelta(days=5),
                "imessage": now - timedelta(days=2),
                "calendar": now - timedelta(days=30),
            }

            # Configure fact store
            mock_fact_store.return_value.get_for_person.return_value = [
                MagicMock(), MagicMock()  # 2 facts
            ]

            yield {
                "person_store": mock_person_store,
                "interaction_store": mock_interaction_store,
                "fact_store": mock_fact_store,
                "person": mock_person,
            }

    def test_get_relationship_summary_success(self, mock_stores):
        """Test successful relationship summary generation."""
        summary = get_relationship_summary("person-123")

        assert summary is not None
        assert summary.person_id == "person-123"
        assert summary.person_name == "John Doe"
        assert summary.relationship_strength == 68.5

        # Check channels are populated
        assert len(summary.channels) == 3

        # Check active channels (imessage is within 7 days)
        assert "imessage" in summary.active_channels
        # gmail at 5 days should also be active
        assert "gmail" in summary.active_channels
        # calendar at 30 days should NOT be active
        assert "calendar" not in summary.active_channels

        # Primary channel should be imessage (highest 90d count)
        assert summary.primary_channel == "imessage"

        # Total interactions
        assert summary.total_interactions_90d == 65  # 15 + 42 + 8

        # Facts
        assert summary.facts_count == 2
        assert summary.has_facts is True

    def test_get_relationship_summary_person_not_found(self, mock_stores):
        """Test when person doesn't exist."""
        mock_stores["person_store"].return_value.get_by_id.return_value = None

        summary = get_relationship_summary("nonexistent")

        assert summary is None

    def test_get_relationship_summary_no_interactions(self, mock_stores):
        """Test for person with no interactions."""
        mock_stores["interaction_store"].return_value.get_interaction_counts.return_value = {}
        mock_stores["interaction_store"].return_value.get_last_interaction_by_source.return_value = {}
        mock_stores["person"].last_seen = None

        summary = get_relationship_summary("person-123")

        assert summary is not None
        assert summary.channels == []
        assert summary.active_channels == []
        assert summary.primary_channel is None
        assert summary.total_interactions_90d == 0
        assert summary.days_since_contact == 999

    def test_get_relationship_summary_facts_error(self, mock_stores):
        """Test graceful handling when fact store fails."""
        mock_stores["fact_store"].return_value.get_for_person.side_effect = Exception("DB error")

        summary = get_relationship_summary("person-123")

        # Should still succeed, just with no facts
        assert summary is not None
        assert summary.facts_count == 0
        assert summary.has_facts is False


class TestFormatRelationshipContext:
    """Tests for format_relationship_context function."""

    def test_format_with_active_channels(self):
        """Test formatting with active channels."""
        now = datetime.now(timezone.utc)
        summary = RelationshipSummary(
            person_id="test-123",
            person_name="Sarah Chen",
            relationship_strength=82.0,
            channels=[
                ChannelActivity("imessage", 50, now - timedelta(days=1), True),
                ChannelActivity("gmail", 20, now - timedelta(days=10), False),
            ],
            active_channels=["imessage"],
            primary_channel="imessage",
            total_interactions_90d=70,
            last_interaction=now - timedelta(days=1),
            days_since_contact=1,
            facts_count=5,
            has_facts=True,
        )

        formatted = format_relationship_context(summary)

        assert "Sarah Chen" in formatted
        assert "82.0/100" in formatted
        assert "imessage" in formatted
        assert "1" in formatted  # days since contact
        assert "5 extracted facts" in formatted

    def test_format_with_no_active_channels(self):
        """Test formatting when no channels are active."""
        summary = RelationshipSummary(
            person_id="test-456",
            person_name="Old Contact",
            relationship_strength=15.0,
            channels=[
                ChannelActivity("gmail", 5, datetime.now(timezone.utc) - timedelta(days=60), False),
            ],
            active_channels=[],
            primary_channel="gmail",
            total_interactions_90d=5,
            last_interaction=datetime.now(timezone.utc) - timedelta(days=60),
            days_since_contact=60,
        )

        formatted = format_relationship_context(summary)

        assert "None recently" in formatted
        assert "dormant" in formatted
