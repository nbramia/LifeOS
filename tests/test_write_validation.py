"""Tests for write-time validation on Interaction and PersonEntity."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.services.interaction_store import (
    Interaction,
    VALID_SOURCE_TYPES,
    UNDATED_SENTINEL,
)
from api.services.person_entity import PersonEntity

pytestmark = pytest.mark.unit


class TestInteractionValidate:
    """Tests for Interaction.validate()."""

    def _make_interaction(self, **overrides):
        defaults = {
            "id": str(uuid.uuid4()),
            "person_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc),
            "source_type": "gmail",
            "title": "Test Email",
        }
        defaults.update(overrides)
        return Interaction(**defaults)

    def test_valid_interaction_passes(self):
        interaction = self._make_interaction()
        interaction.validate()  # Should not raise

    def test_empty_person_id_rejected(self):
        interaction = self._make_interaction(person_id="")
        with pytest.raises(ValueError, match="person_id is required"):
            interaction.validate()

    def test_empty_source_type_rejected(self):
        interaction = self._make_interaction(source_type="")
        with pytest.raises(ValueError, match="source_type is required"):
            interaction.validate()

    def test_invalid_source_type_rejected(self):
        interaction = self._make_interaction(source_type="twitter")
        with pytest.raises(ValueError, match="source_type.*not in"):
            interaction.validate()

    @pytest.mark.parametrize("source_type", sorted(VALID_SOURCE_TYPES))
    def test_all_valid_source_types_accepted(self, source_type):
        interaction = self._make_interaction(source_type=source_type)
        interaction.validate()  # Should not raise

    def test_timestamp_too_old_rejected(self):
        interaction = self._make_interaction(
            timestamp=datetime(1999, 12, 31, tzinfo=timezone.utc)
        )
        with pytest.raises(ValueError, match="timestamp too old"):
            interaction.validate()

    def test_timestamp_in_future_rejected(self):
        interaction = self._make_interaction(
            timestamp=datetime.now(timezone.utc) + timedelta(days=180)
        )
        with pytest.raises(ValueError, match="timestamp is in the future"):
            interaction.validate()

    def test_calendar_future_event_ok(self):
        """Calendar events can be up to ~30 days out; allow 90 days margin."""
        interaction = self._make_interaction(
            source_type="calendar",
            timestamp=datetime.now(timezone.utc) + timedelta(days=30),
        )
        interaction.validate()  # Should not raise

    def test_undated_sentinel_ok(self):
        """UNDATED_SENTINEL (1970-01-01) is allowed for vault notes."""
        interaction = self._make_interaction(timestamp=UNDATED_SENTINEL)
        interaction.validate()  # Should not raise

    def test_year_2000_timestamp_ok(self):
        interaction = self._make_interaction(
            timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        interaction.validate()  # Should not raise


class TestPersonEntityValidate:
    """Tests for PersonEntity.validate()."""

    def test_valid_entity_passes(self):
        entity = PersonEntity(canonical_name="Alex Johnson")
        entity.validate()  # Should not raise

    def test_empty_id_rejected(self):
        entity = PersonEntity(canonical_name="Alex Johnson")
        entity.id = ""
        with pytest.raises(ValueError, match="id is required"):
            entity.validate()

    def test_empty_name_rejected(self):
        entity = PersonEntity(id=str(uuid.uuid4()), canonical_name="")
        with pytest.raises(ValueError, match="canonical_name is required"):
            entity.validate()

    def test_whitespace_only_name_rejected(self):
        entity = PersonEntity(id=str(uuid.uuid4()), canonical_name="   ")
        with pytest.raises(ValueError, match="canonical_name is required"):
            entity.validate()


class TestStoreValidation:
    """Tests that store write paths call validate()."""

    def test_interaction_store_add_rejects_invalid(self, tmp_path):
        from api.services.interaction_store import InteractionStore
        store = InteractionStore(db_path=str(tmp_path / "int.db"))

        interaction = Interaction(
            id=str(uuid.uuid4()),
            person_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            source_type="invalid_type",
            title="Bad Interaction",
        )
        with pytest.raises(ValueError, match="source_type"):
            store.add(interaction)

    def test_interaction_store_batch_add_rejects_invalid(self, tmp_path):
        from api.services.interaction_store import InteractionStore
        store = InteractionStore(db_path=str(tmp_path / "int.db"))

        interactions = [
            Interaction(
                id=str(uuid.uuid4()),
                person_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                source_type="bad_type",
                title="Bad",
            )
        ]
        with pytest.raises(ValueError, match="source_type"):
            store.batch_add(interactions)

    def test_person_entity_store_add_rejects_invalid(self, tmp_path):
        from api.services.person_entity import PersonEntityStore
        store = PersonEntityStore(db_path=str(tmp_path / "crm.db"))

        entity = PersonEntity(canonical_name="")
        with pytest.raises(ValueError, match="canonical_name is required"):
            store.add(entity)

    def test_person_entity_store_update_rejects_invalid(self, tmp_path):
        from api.services.person_entity import PersonEntityStore
        store = PersonEntityStore(db_path=str(tmp_path / "crm.db"))

        entity = PersonEntity(canonical_name="")
        with pytest.raises(ValueError, match="canonical_name is required"):
            store.update(entity)
