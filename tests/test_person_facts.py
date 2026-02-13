"""
Tests for PersonFacts service.

Covers:
- PersonFact dataclass serialization
- PersonFactStore CRUD operations
- PersonFactExtractor sampling and parsing logic
- Upsert conflict resolution
"""
import pytest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from api.services.person_facts import (
    PersonFact,
    PersonFactStore,
    PersonFactExtractor,
    FACT_CATEGORIES,
    _make_aware,
    get_person_fact_store,
)


class TestMakeAware:
    """Tests for timezone awareness helper."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert _make_aware(None) is None

    def test_naive_datetime_gets_utc(self):
        """Naive datetime gets UTC timezone."""
        naive = datetime(2025, 1, 15, 10, 30, 0)
        aware = _make_aware(naive)
        assert aware.tzinfo == timezone.utc
        assert aware.hour == 10

    def test_aware_datetime_unchanged(self):
        """Already aware datetime is unchanged."""
        aware = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _make_aware(aware)
        assert result == aware


class TestPersonFact:
    """Tests for PersonFact dataclass."""

    def test_default_values(self):
        """New fact has default values."""
        fact = PersonFact()
        assert fact.id  # UUID generated
        assert fact.person_id == ""
        assert fact.confidence == 0.5
        assert fact.confirmed_by_user is False
        assert fact.created_at is not None

    def test_to_dict(self):
        """Fact converts to dict correctly."""
        fact = PersonFact(
            id="test-id",
            person_id="person-123",
            category="family",
            key="spouse_name",
            value="Jane",
            confidence=0.9,
        )
        data = fact.to_dict()

        assert data["id"] == "test-id"
        assert data["person_id"] == "person-123"
        assert data["category"] == "family"
        assert data["key"] == "spouse_name"
        assert data["value"] == "Jane"
        assert data["confidence"] == 0.9
        assert data["category_icon"] == "👨‍👩‍👧"

    def test_to_dict_includes_icon(self):
        """to_dict includes category icon."""
        for category, icon in FACT_CATEGORIES.items():
            fact = PersonFact(category=category)
            data = fact.to_dict()
            assert data["category_icon"] == icon

    def test_from_dict(self):
        """Fact created from dict."""
        data = {
            "id": "test-id",
            "person_id": "person-123",
            "category": "work",
            "key": "current_role",
            "value": "Engineer",
            "confidence": 0.8,
            "source_quote": "I work as an engineer",
            "extracted_at": "2025-01-15T10:30:00+00:00",
            "created_at": "2025-01-15T10:30:00+00:00",
            "confirmed_by_user": False,
        }
        fact = PersonFact.from_dict(data)

        assert fact.id == "test-id"
        assert fact.category == "work"
        assert fact.key == "current_role"
        assert fact.value == "Engineer"
        assert fact.source_quote == "I work as an engineer"

    def test_from_dict_parses_datetimes(self):
        """from_dict parses ISO datetime strings."""
        data = {
            "id": "test-id",
            "person_id": "person-123",
            "category": "dates",
            "key": "birthday",
            "value": "March 15",
            "confidence": 0.9,
            "extracted_at": "2025-01-15T10:30:00+00:00",
            "created_at": "2025-01-15T10:30:00+00:00",
            "confirmed_by_user": False,
        }
        fact = PersonFact.from_dict(data)

        assert isinstance(fact.extracted_at, datetime)
        assert fact.extracted_at.tzinfo is not None

    def test_from_dict_removes_icon(self):
        """from_dict removes computed category_icon."""
        data = {
            "id": "test-id",
            "person_id": "person-123",
            "category": "family",
            "key": "spouse",
            "value": "test",
            "confidence": 0.5,
            "category_icon": "👨‍👩‍👧",  # Should be ignored
            "confirmed_by_user": False,
        }
        fact = PersonFact.from_dict(data)
        assert fact.category == "family"

    def test_from_row_12_columns(self):
        """from_row handles full 12-column schema."""
        row = (
            "fact-id",
            "person-id",
            "interests",
            "hobby",
            "hiking",
            0.85,
            "interaction-123",
            "2025-01-15T10:00:00",
            0,  # confirmed_by_user
            "2025-01-14T09:00:00",
            "I love hiking in the mountains",  # source_quote
            "obsidian://open?vault=Notes&file=Meetings/2025-01-15",  # source_link
        )
        fact = PersonFact.from_row(row)

        assert fact.id == "fact-id"
        assert fact.person_id == "person-id"
        assert fact.category == "interests"
        assert fact.key == "hobby"
        assert fact.value == "hiking"
        assert fact.confidence == 0.85
        assert fact.source_quote == "I love hiking in the mountains"
        assert "obsidian://" in fact.source_link

    def test_from_row_10_columns_legacy(self):
        """from_row handles legacy 10-column schema."""
        row = (
            "fact-id",
            "person-id",
            "work",
            "company",
            "Acme Inc",
            0.9,
            "interaction-456",
            "2025-01-15T10:00:00",
            1,  # confirmed_by_user
            "2025-01-14T09:00:00",
        )
        fact = PersonFact.from_row(row)

        assert fact.id == "fact-id"
        assert fact.source_quote is None
        assert fact.source_link is None
        assert fact.confirmed_by_user is True


class TestPersonFactStore:
    """Tests for PersonFactStore SQLite operations."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary database."""
        db_path = str(tmp_path / "test_facts.db")
        return db_path

    @pytest.fixture
    def store(self, temp_db):
        """Create a fact store with temp database."""
        return PersonFactStore(db_path=temp_db)

    def test_init_creates_table(self, store, temp_db):
        """Store initialization creates table and indexes."""
        import sqlite3
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='person_facts'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_add_fact(self, store):
        """Add stores fact in database."""
        fact = PersonFact(
            person_id="person-123",
            category="family",
            key="spouse_name",
            value="Jane",
            confidence=0.9,
        )
        result = store.add(fact)

        assert result.id == fact.id
        retrieved = store.get_by_id(fact.id)
        assert retrieved is not None
        assert retrieved.value == "Jane"

    def test_get_by_id_not_found(self, store):
        """get_by_id returns None for missing ID."""
        result = store.get_by_id("nonexistent-id")
        assert result is None

    def test_get_for_person(self, store):
        """get_for_person returns all facts for a person."""
        person_id = "person-456"

        # Add multiple facts
        store.add(PersonFact(person_id=person_id, category="family", key="spouse", value="John"))
        store.add(PersonFact(person_id=person_id, category="work", key="company", value="Acme"))
        store.add(PersonFact(person_id=person_id, category="interests", key="hobby", value="chess"))

        # Add fact for different person
        store.add(PersonFact(person_id="other-person", category="work", key="role", value="CEO"))

        facts = store.get_for_person(person_id)
        assert len(facts) == 3
        assert all(f.person_id == person_id for f in facts)

    def test_get_for_person_ordered_by_category(self, store):
        """get_for_person returns facts ordered by category then key."""
        person_id = "person-789"

        store.add(PersonFact(person_id=person_id, category="work", key="company", value="X"))
        store.add(PersonFact(person_id=person_id, category="family", key="spouse", value="Y"))
        store.add(PersonFact(person_id=person_id, category="work", key="role", value="Z"))

        facts = store.get_for_person(person_id)
        categories = [f.category for f in facts]

        # Should be sorted: family, work, work (alphabetical)
        assert categories == sorted(categories)

    def test_upsert_inserts_new(self, store):
        """upsert inserts when fact doesn't exist."""
        fact = PersonFact(
            person_id="person-123",
            category="dates",
            key="birthday",
            value="March 15",
            confidence=0.8,
        )
        result = store.upsert(fact)

        retrieved = store.get_by_id(result.id)
        assert retrieved.value == "March 15"

    def test_upsert_updates_higher_confidence(self, store):
        """upsert updates when new fact has higher confidence."""
        person_id = "person-123"

        # Add initial fact
        initial = PersonFact(
            person_id=person_id,
            category="work",
            key="company",
            value="OldCorp",
            confidence=0.6,
        )
        store.add(initial)

        # Upsert with higher confidence
        updated = PersonFact(
            person_id=person_id,
            category="work",
            key="company",
            value="NewCorp",
            confidence=0.9,
        )
        store.upsert(updated)

        facts = store.get_for_person(person_id)
        assert len(facts) == 1
        assert facts[0].value == "NewCorp"
        assert facts[0].confidence == 0.9

    def test_upsert_skips_lower_confidence(self, store):
        """upsert skips when new fact has lower confidence."""
        person_id = "person-123"

        # Add initial high-confidence fact
        initial = PersonFact(
            person_id=person_id,
            category="work",
            key="company",
            value="HighConfCorp",
            confidence=0.95,
        )
        store.add(initial)

        # Try to upsert with lower confidence
        updated = PersonFact(
            person_id=person_id,
            category="work",
            key="company",
            value="LowConfCorp",
            confidence=0.5,
        )
        store.upsert(updated)

        facts = store.get_for_person(person_id)
        assert len(facts) == 1
        assert facts[0].value == "HighConfCorp"

    def test_upsert_preserves_confirmed(self, store):
        """upsert doesn't override user-confirmed facts."""
        person_id = "person-123"

        # Add and confirm fact
        initial = PersonFact(
            person_id=person_id,
            category="family",
            key="spouse_name",
            value="ConfirmedName",
            confidence=0.7,
            confirmed_by_user=True,
        )
        store.add(initial)

        # Try to upsert with higher confidence
        updated = PersonFact(
            person_id=person_id,
            category="family",
            key="spouse_name",
            value="DifferentName",
            confidence=0.99,
        )
        store.upsert(updated)

        facts = store.get_for_person(person_id)
        assert len(facts) == 1
        assert facts[0].value == "ConfirmedName"

    def test_update_fact(self, store):
        """update modifies existing fact."""
        fact = PersonFact(
            person_id="person-123",
            category="interests",
            key="hobby",
            value="reading",
            confidence=0.7,
        )
        store.add(fact)

        fact.value = "photography"
        fact.confidence = 0.85
        store.update(fact)

        retrieved = store.get_by_id(fact.id)
        assert retrieved.value == "photography"
        assert retrieved.confidence == 0.85

    def test_confirm_fact(self, store, temp_db):
        """confirm marks fact as user-confirmed."""
        import sqlite3

        fact = PersonFact(
            person_id="person-123",
            category="work",
            key="role",
            value="Developer",
            confirmed_by_user=False,
        )
        store.add(fact)

        result = store.confirm(fact.id)
        assert result is True

        # Verify directly in database (from_row has schema order issues with fresh DBs)
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT confirmed_by_user FROM person_facts WHERE id = ?", (fact.id,)
        )
        confirmed = cursor.fetchone()[0]
        conn.close()
        assert confirmed == 1

    def test_confirm_nonexistent_returns_false(self, store):
        """confirm returns False for nonexistent fact."""
        result = store.confirm("nonexistent-id")
        assert result is False

    def test_delete_fact(self, store):
        """delete removes fact from database."""
        fact = PersonFact(
            person_id="person-123",
            category="travel",
            key="destination",
            value="Paris",
        )
        store.add(fact)

        result = store.delete(fact.id)
        assert result is True

        retrieved = store.get_by_id(fact.id)
        assert retrieved is None

    def test_delete_nonexistent_returns_false(self, store):
        """delete returns False for nonexistent fact."""
        result = store.delete("nonexistent-id")
        assert result is False

    def test_delete_for_person(self, store):
        """delete_for_person removes all facts for a person."""
        person_id = "person-to-delete"

        store.add(PersonFact(person_id=person_id, category="work", key="a", value="1"))
        store.add(PersonFact(person_id=person_id, category="work", key="b", value="2"))
        store.add(PersonFact(person_id=person_id, category="work", key="c", value="3"))
        store.add(PersonFact(person_id="other-person", category="work", key="d", value="4"))

        count = store.delete_for_person(person_id)
        assert count == 3

        # Verify deletion
        facts = store.get_for_person(person_id)
        assert len(facts) == 0

        # Other person's facts still exist
        other_facts = store.get_for_person("other-person")
        assert len(other_facts) == 1


class TestPersonFactExtractor:
    """Tests for PersonFactExtractor logic (LLM calls mocked)."""

    @pytest.fixture
    def mock_store(self):
        """Create a mock fact store."""
        store = MagicMock()
        store.upsert.side_effect = lambda f: f  # Return the fact as-is
        return store

    @pytest.fixture
    def extractor(self, mock_store):
        """Create extractor with mock store."""
        return PersonFactExtractor(fact_store=mock_store)

    def test_sample_interactions_small_set_unchanged(self, extractor):
        """Small interaction sets are returned unchanged."""
        interactions = [
            {"id": f"int-{i}", "timestamp": f"2025-01-{i:02d}T10:00:00"}
            for i in range(1, 50)
        ]
        result = extractor._sample_interactions(interactions)
        assert len(result) == len(interactions)

    def test_sample_interactions_large_set_sampled(self, extractor):
        """Large interaction sets are strategically sampled."""
        interactions = [
            {"id": f"int-{i}", "timestamp": f"2025-01-{(i % 28) + 1:02d}T10:00:00", "source_type": "gmail"}
            for i in range(500)
        ]
        result = extractor._sample_interactions(interactions)

        # Should be reduced to max batch size
        assert len(result) < len(interactions)
        assert len(result) <= extractor.MAX_INTERACTIONS_PER_BATCH

    def test_sample_interactions_prioritizes_calendar(self, extractor):
        """Calendar and vault interactions are always included."""
        interactions = [
            {"id": f"gmail-{i}", "timestamp": f"2025-01-01T10:00:00", "source_type": "gmail"}
            for i in range(400)
        ]
        # Add some calendar events
        interactions.extend([
            {"id": f"cal-{i}", "timestamp": f"2024-06-01T10:00:00", "source_type": "calendar"}
            for i in range(10)
        ])

        result = extractor._sample_interactions(interactions)

        # All calendar events should be included
        calendar_ids = [i["id"] for i in result if i["source_type"] == "calendar"]
        assert len(calendar_ids) == 10

    def test_sample_interactions_temporal_diversity(self, extractor):
        """Temporal diversity ensures older interactions appear in samples."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        interactions = []

        # 300 recent (last 6 months)
        for i in range(300):
            interactions.append({
                "id": f"recent-{i}",
                "timestamp": (now - timedelta(days=i)).isoformat(),
                "source_type": "gmail",
            })
        # 100 from 2 years ago
        for i in range(100):
            interactions.append({
                "id": f"mid-{i}",
                "timestamp": (now - timedelta(days=730 + i)).isoformat(),
                "source_type": "gmail",
            })
        # 100 from 4 years ago
        for i in range(100):
            interactions.append({
                "id": f"old-{i}",
                "timestamp": (now - timedelta(days=1460 + i)).isoformat(),
                "source_type": "gmail",
            })

        result = extractor._sample_interactions(interactions)

        # Should have sampled within budget
        assert len(result) <= extractor.MAX_INTERACTIONS_PER_BATCH

        # Older interactions must appear
        result_ids = {i["id"] for i in result}
        old_count = sum(1 for rid in result_ids if rid.startswith("old-"))
        mid_count = sum(1 for rid in result_ids if rid.startswith("mid-"))

        assert old_count > 0, "No old interactions (3+ years) were sampled"
        assert mid_count > 0, "No mid interactions (1-3 years) were sampled"

    def test_create_batches(self, extractor):
        """Interactions are split into batches."""
        interactions = list(range(250))
        batches = extractor._create_batches(interactions, 100)

        assert len(batches) == 3
        assert len(batches[0]) == 100
        assert len(batches[1]) == 100
        assert len(batches[2]) == 50

    def test_format_interactions(self, extractor):
        """Interactions are formatted for LLM prompt."""
        interactions = [
            {
                "id": "int-123",
                "source_type": "gmail",
                "title": "Meeting follow-up",
                "snippet": "Thanks for the great meeting...",
                "timestamp": "2025-01-15T10:00:00",
            }
        ]
        result = extractor._format_interactions(interactions)

        assert "ID:int-123" in result
        assert "[gmail]" in result
        assert "Meeting follow-up" in result
        assert "Thanks for the great meeting" in result

    def test_parse_extraction_response_valid_json(self, extractor):
        """Valid JSON response is parsed correctly with auto-generated key."""
        response = json.dumps({
            "facts": [
                {
                    "category": "family",
                    "value": "Married to Sarah who loves hiking",
                    "quote": "my wife Sarah and I went hiking",
                    "source_id": "int-123",
                }
            ]
        })

        interactions = [{"id": "int-123", "source_link": "obsidian://test"}]
        lookup = {"int-123": interactions[0]}

        facts = extractor._parse_extraction_response(response, "person-id", lookup)

        assert len(facts) == 1
        assert facts[0].category == "family"
        assert len(facts[0].key) == 12  # Auto-generated hash key
        assert facts[0].value == "Married to Sarah who loves hiking"
        assert facts[0].source_quote == "my wife Sarah and I went hiking"
        assert facts[0].confidence == 0.7  # Placeholder confidence

    def test_parse_extraction_response_markdown_json(self, extractor):
        """JSON in markdown code block is extracted."""
        response = '''Here are the facts:
```json
{
  "facts": [
    {
      "category": "work",
      "value": "Works at Acme Inc as an engineer",
      "quote": "I work at Acme Inc",
      "source_id": "int-456"
    }
  ]
}
```
'''
        facts = extractor._parse_extraction_response(response, "person-id", {})

        assert len(facts) == 1
        assert facts[0].value == "Works at Acme Inc as an engineer"
        assert len(facts[0].key) == 12  # Auto-generated hash key

    def test_parse_extraction_response_invalid_category_skipped(self, extractor):
        """Facts with invalid categories are skipped."""
        response = json.dumps({
            "facts": [
                {"category": "invalid_category", "key": "test", "value": "test", "confidence": 0.9},
                {"category": "work", "key": "company", "value": "Valid", "confidence": 0.9},
            ]
        })

        facts = extractor._parse_extraction_response(response, "person-id", {})

        assert len(facts) == 1
        assert facts[0].category == "work"

    def test_parse_extraction_response_missing_fields_skipped(self, extractor):
        """Facts missing required fields are skipped."""
        response = json.dumps({
            "facts": [
                {"category": "work", "value": "", "quote": "q"},  # Empty value
                {"category": "work", "value": "Works as a senior engineer at Google", "quote": "q"},  # Valid
            ]
        })

        facts = extractor._parse_extraction_response(response, "person-id", {})

        assert len(facts) == 1
        assert facts[0].value == "Works as a senior engineer at Google"

    def test_parse_extraction_response_ignores_llm_confidence(self, extractor):
        """LLM-provided confidence is ignored; placeholder 0.7 is used."""
        response = json.dumps({
            "facts": [
                {"category": "work", "value": "Works as a CEO at Acme Corp", "confidence": 0.95, "quote": "q"},
            ]
        })

        facts = extractor._parse_extraction_response(response, "person-id", {})

        assert len(facts) == 1
        assert facts[0].confidence == 0.7  # Placeholder, not 0.95

    def test_parse_extraction_response_invalid_json(self, extractor):
        """Invalid JSON returns empty list."""
        response = "This is not valid JSON {{"

        facts = extractor._parse_extraction_response(response, "person-id", {})

        assert facts == []

    def test_extract_facts_empty_interactions(self, extractor):
        """Empty interactions return empty list."""
        facts = extractor.extract_facts("person-id", "John Doe", [])
        assert facts == []

    def test_format_interactions_for_summary(self, extractor):
        """Interactions are grouped by period for summary."""
        interactions = [
            {"timestamp": "2025-01-15T10:00:00", "source_type": "gmail", "title": "January email", "snippet": "Hi"},
            {"timestamp": "2025-01-20T10:00:00", "source_type": "calendar", "title": "January meeting"},
            {"timestamp": "2024-12-10T10:00:00", "source_type": "gmail", "title": "December email"},
        ]

        result = extractor._format_interactions_for_summary(interactions)

        assert "2025-01" in result
        assert "2024-12" in result
        assert "January email" in result
        assert "December email" in result


class TestValidateAndDedup:
    """Tests for batched Ollama validation + deduplication."""

    @pytest.fixture
    def mock_store(self):
        store = MagicMock()
        store.upsert.side_effect = lambda f: f
        return store

    @pytest.fixture
    def extractor(self, mock_store):
        return PersonFactExtractor(fact_store=mock_store)

    def _make_fact(self, value="Has a daughter named Emma", category="family",
                   quote="my daughter Emma", confidence=0.7, confirmed=False):
        return PersonFact(
            person_id="person-1",
            category=category,
            key=PersonFactExtractor(fact_store=MagicMock())._generate_fact_key(category, value),
            value=value,
            confidence=confidence,
            source_quote=quote,
            confirmed_by_user=confirmed,
        )

    @pytest.mark.asyncio
    async def test_rejects_universal_facts(self, extractor):
        """Ollama rejects universal/obvious facts."""
        fact = self._make_fact(value="Has a mother")
        mock_result = {"decisions": [
            {"candidate": 0, "action": "reject", "reason": "Universal fact"}
        ]}

        with patch("api.services.ollama_client.OllamaClient") as MockClient:
            instance = MockClient.return_value
            instance.is_available_async = AsyncMock(return_value=True)
            instance.generate_json = AsyncMock(return_value=mock_result)

            result = await extractor._validate_and_dedup_ollama([fact], [], "John")

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_deduplicates_against_existing(self, extractor):
        """Ollama returns update for semantically duplicate fact."""
        existing = self._make_fact(value="Has a daughter named Emma")
        existing.key = "existing_key_abc"
        candidate = self._make_fact(value="Daughter Emma plays piano after school")

        mock_result = {"decisions": [
            {"candidate": 0, "action": "update", "updates_existing": 0,
             "evidence_strength": 4, "reason": "More detailed version"}
        ]}

        with patch("api.services.ollama_client.OllamaClient") as MockClient:
            instance = MockClient.return_value
            instance.is_available_async = AsyncMock(return_value=True)
            instance.generate_json = AsyncMock(return_value=mock_result)

            result = await extractor._validate_and_dedup_ollama([candidate], [existing], "John")

        assert len(result) == 1
        assert result[0].key == "existing_key_abc"  # Takes existing key for upsert overwrite
        assert result[0].confidence == 0.8  # evidence_strength 4

    @pytest.mark.asyncio
    async def test_keeps_new_facts(self, extractor):
        """Ollama keeps new unique facts with evidence_strength."""
        fact = self._make_fact(value="Allergic to shellfish and avoids seafood")
        mock_result = {"decisions": [
            {"candidate": 0, "action": "keep", "evidence_strength": 5,
             "reason": "New unique fact"}
        ]}

        with patch("api.services.ollama_client.OllamaClient") as MockClient:
            instance = MockClient.return_value
            instance.is_available_async = AsyncMock(return_value=True)
            instance.generate_json = AsyncMock(return_value=mock_result)

            result = await extractor._validate_and_dedup_ollama([fact], [], "John")

        assert len(result) == 1
        assert result[0].confidence == 0.9  # evidence_strength 5

    @pytest.mark.asyncio
    async def test_respects_confirmed_facts(self, extractor):
        """Update action is converted to keep for confirmed existing facts."""
        existing = self._make_fact(value="Has a daughter named Emma", confirmed=True)
        candidate = self._make_fact(value="Daughter Emma plays piano after school")

        mock_result = {"decisions": [
            {"candidate": 0, "action": "update", "updates_existing": 0,
             "evidence_strength": 4, "reason": "More detail"}
        ]}

        with patch("api.services.ollama_client.OllamaClient") as MockClient:
            instance = MockClient.return_value
            instance.is_available_async = AsyncMock(return_value=True)
            instance.generate_json = AsyncMock(return_value=mock_result)

            result = await extractor._validate_and_dedup_ollama([candidate], [existing], "John")

        assert len(result) == 1
        # Key should NOT be the existing key (kept as new fact instead)
        assert result[0].key != "existing_key_abc"

    @pytest.mark.asyncio
    async def test_fallback_rejects_short_values(self, extractor):
        """Fallback validation rejects values with fewer than 4 words."""
        short = self._make_fact(value="Has dog")
        long = self._make_fact(value="Has a golden retriever named Max")

        result = extractor._fallback_validate([short, long], [])

        assert len(result) == 1
        assert result[0].value == "Has a golden retriever named Max"

    @pytest.mark.asyncio
    async def test_fallback_when_ollama_unavailable(self, extractor):
        """Falls back to rule-based validation when Ollama is down."""
        facts = [
            self._make_fact(value="Has a daughter named Emma who plays soccer"),
            self._make_fact(value="true", category="preferences"),
        ]

        with patch("api.services.ollama_client.OllamaClient") as MockClient:
            instance = MockClient.return_value
            instance.is_available_async = AsyncMock(return_value=False)

            result = await extractor._validate_and_dedup_ollama(facts, [], "John")

        # "true" should be rejected, the other kept with capped confidence
        assert len(result) == 1
        assert result[0].confidence <= 0.7


class TestGenerateFactKey:
    """Tests for the _generate_fact_key helper."""

    @pytest.fixture
    def extractor(self):
        return PersonFactExtractor(fact_store=MagicMock())

    def test_deterministic(self, extractor):
        """Same input produces same key."""
        key1 = extractor._generate_fact_key("family", "Has a daughter named Emma")
        key2 = extractor._generate_fact_key("family", "Has a daughter named Emma")
        assert key1 == key2

    def test_case_insensitive(self, extractor):
        """Different case produces same key."""
        key1 = extractor._generate_fact_key("family", "Has A Daughter Named Emma")
        key2 = extractor._generate_fact_key("family", "has a daughter named emma")
        assert key1 == key2

    def test_punctuation_insensitive(self, extractor):
        """Punctuation differences produce same key."""
        key1 = extractor._generate_fact_key("family", "Has a daughter, Emma!")
        key2 = extractor._generate_fact_key("family", "Has a daughter Emma")
        assert key1 == key2

    def test_different_values_different_keys(self, extractor):
        """Different values produce different keys."""
        key1 = extractor._generate_fact_key("family", "Has a daughter named Emma")
        key2 = extractor._generate_fact_key("family", "Has a son named Jake")
        assert key1 != key2

    def test_key_length(self, extractor):
        """Key is always 12 characters."""
        key = extractor._generate_fact_key("work", "Works at Google as an engineer")
        assert len(key) == 12


class TestFactCategories:
    """Tests for fact category configuration."""

    def test_all_categories_have_icons(self):
        """All expected categories have icons."""
        expected = ["family", "preferences", "background", "interests", "dates", "work", "topics", "travel", "summary"]
        for category in expected:
            assert category in FACT_CATEGORIES
            assert FACT_CATEGORIES[category]  # Not empty

    def test_category_icons_are_emoji(self):
        """All category icons are emoji characters."""
        for category, icon in FACT_CATEGORIES.items():
            # Basic check that icon is non-ASCII (emoji)
            assert any(ord(c) > 127 for c in icon), f"{category} icon '{icon}' is not emoji"


class TestSingletonFunctions:
    """Tests for singleton accessor functions."""

    def test_get_person_fact_store_returns_store(self, tmp_path):
        """get_person_fact_store returns a PersonFactStore."""
        with patch('api.services.person_facts._fact_store', None):
            with patch('api.services.person_facts.get_crm_db_path', return_value=str(tmp_path / "test.db")):
                store = get_person_fact_store()
                assert isinstance(store, PersonFactStore)
