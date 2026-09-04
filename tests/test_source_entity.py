"""Tests for SourceEntity and SourceEntityStore."""
import sqlite3
import tempfile
import pytest
from datetime import datetime, timedelta, timezone

from api.services.source_entity import (
    SourceEntity,
    SourceEntityStore,
    LINK_STATUS_AUTO,
    LINK_STATUS_CONFIRMED,
    LINK_STATUS_REJECTED,
    create_gmail_source_entity,
    create_calendar_source_entity,
    create_imessage_source_entity,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield f.name


@pytest.fixture
def store(temp_db):
    """Create a SourceEntityStore with temp database."""
    return SourceEntityStore(db_path=temp_db)


class TestSourceEntity:
    """Tests for SourceEntity dataclass."""

    def test_create_source_entity(self):
        """Test basic source entity creation."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
        )

        assert entity.source_type == "gmail"
        assert entity.source_id == "msg123"
        assert entity.observed_name == "John Doe"
        assert entity.observed_email == "john@example.com"
        assert entity.canonical_person_id is None
        assert entity.link_status == LINK_STATUS_AUTO

    def test_source_badge(self):
        """Test source badge emoji lookup."""
        gmail = SourceEntity(source_type="gmail")
        assert gmail.source_badge == "📧"

        calendar = SourceEntity(source_type="calendar")
        assert calendar.source_badge == "📅"

        slack = SourceEntity(source_type="slack")
        assert slack.source_badge == "💬"

    def test_is_linked(self):
        """Test is_linked property."""
        unlinked = SourceEntity(source_type="gmail")
        assert not unlinked.is_linked

        linked = SourceEntity(
            source_type="gmail",
            canonical_person_id="person123",
            link_status=LINK_STATUS_CONFIRMED,
        )
        assert linked.is_linked

        rejected = SourceEntity(
            source_type="gmail",
            canonical_person_id="person123",
            link_status=LINK_STATUS_REJECTED,
        )
        assert not rejected.is_linked

    def test_to_dict_from_dict(self):
        """Test serialization roundtrip."""
        now = datetime.now(timezone.utc)
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
            observed_at=now,
            metadata={"thread_id": "thread456"},
        )

        data = entity.to_dict()
        restored = SourceEntity.from_dict(data)

        assert restored.source_type == entity.source_type
        assert restored.source_id == entity.source_id
        assert restored.observed_name == entity.observed_name
        assert restored.observed_email == entity.observed_email
        assert restored.metadata == entity.metadata


class TestSourceEntityStore:
    """Tests for SourceEntityStore."""

    def test_add_entity(self, store):
        """Test adding a source entity."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_name="John Doe",
            observed_email="john@example.com",
        )

        added = store.add(entity)
        assert added.id == entity.id

        # Verify it's stored
        retrieved = store.get_by_id(entity.id)
        assert retrieved is not None
        assert retrieved.observed_name == "John Doe"

    def test_get_by_source(self, store):
        """Test getting entity by source type and ID."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            observed_email="john@example.com",
        )
        store.add(entity)

        retrieved = store.get_by_source("gmail", "msg123")
        assert retrieved is not None
        assert retrieved.observed_email == "john@example.com"

        # Non-existent
        not_found = store.get_by_source("gmail", "nonexistent")
        assert not_found is None

    def test_link_to_person(self, store):
        """Test linking a source entity to a canonical person."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
        )
        store.add(entity)

        success = store.link_to_person(
            entity.id,
            "person456",
            confidence=0.95,
            status=LINK_STATUS_CONFIRMED,
        )
        assert success

        retrieved = store.get_by_id(entity.id)
        assert retrieved.canonical_person_id == "person456"
        assert retrieved.link_confidence == 0.95
        assert retrieved.link_status == LINK_STATUS_CONFIRMED

    def test_unlink(self, store):
        """Test unlinking a source entity."""
        # Note: validate_person=False bypasses person existence check for unit testing
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg123",
            canonical_person_id="person456",
            link_confidence=1.0,
            link_status=LINK_STATUS_CONFIRMED,
        )
        store.add(entity, validate_person=False)

        success = store.unlink(entity.id)
        assert success

        retrieved = store.get_by_id(entity.id)
        assert retrieved.canonical_person_id is None
        assert retrieved.link_confidence == 0.0

    def test_get_for_person(self, store):
        """Test getting all entities for a canonical person."""
        # Add entities for different people
        # Note: validate_person=False bypasses person existence check for unit testing
        for i in range(3):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
            )
            store.add(entity, validate_person=False)

        entity = SourceEntity(
            source_type="gmail",
            source_id="msg99",
            canonical_person_id="person2",
        )
        store.add(entity, validate_person=False)

        # Get for person1
        entities = store.get_for_person("person1")
        assert len(entities) == 3

        # Get for person2
        entities = store.get_for_person("person2")
        assert len(entities) == 1

    def test_get_unlinked(self, store):
        """Test getting unlinked entities."""
        # Add linked and unlinked entities
        # Note: validate_person=False bypasses person existence check for unit testing
        linked = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            canonical_person_id="person1",
        )
        store.add(linked, validate_person=False)

        unlinked = SourceEntity(
            source_type="gmail",
            source_id="msg2",
        )
        store.add(unlinked)

        entities = store.get_unlinked()
        assert len(entities) == 1
        assert entities[0].source_id == "msg2"

    def test_get_by_email(self, store):
        """Test getting entities by observed email."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            observed_email="John@Example.com",
        )
        store.add(entity)

        # Case-insensitive
        entities = store.get_by_email("john@example.com")
        assert len(entities) == 1

    def test_statistics(self, store):
        """Test getting statistics."""
        # Add some entities
        # Note: validate_person=False bypasses person existence check for unit testing
        for i in range(3):
            store.add(SourceEntity(
                source_type="gmail",
                source_id=f"gmail{i}",
                canonical_person_id="person1" if i < 2 else None,
            ), validate_person=False)

        store.add(SourceEntity(
            source_type="calendar",
            source_id="cal1",
            canonical_person_id="person1",
        ), validate_person=False)

        stats = store.get_statistics()
        assert stats["total_entities"] == 4
        assert stats["linked_entities"] == 3
        assert stats["unlinked_entities"] == 1
        assert stats["by_source"]["gmail"] == 3
        assert stats["by_source"]["calendar"] == 1


class _CountingConnection:
    """Wraps a sqlite3.Connection and counts .execute() calls, so tests can
    assert how many round trips a store method actually issues."""

    def __init__(self, real_conn):
        self._real_conn = real_conn
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._real_conn.execute(*args, **kwargs)

    def close(self):
        self._real_conn.close()


class TestGetForPeopleBatch:
    """Tests for SourceEntityStore.get_for_people_batch(): batches the
    per-page source-entity fetch that compute_person_category()'s
    include_related=False fallback needs, instead of one query per person."""

    def _add_entities(self, store, canonical_person_id: str, count: int, source_type: str = "gmail"):
        now = datetime.now(timezone.utc)
        for i in range(count):
            store.add(SourceEntity(
                source_type=source_type,
                source_id=f"{canonical_person_id}-{i}",
                canonical_person_id=canonical_person_id,
                observed_at=now - timedelta(minutes=i),
            ), validate_person=False)

    def test_matches_get_for_person_content_and_order(self, store):
        """Batch result for each ID is identical (content and order) to
        calling get_for_person(id, limit=N) individually."""
        self._add_entities(store, "person-a", 5, source_type="gmail")
        self._add_entities(store, "person-b", 3, source_type="slack")

        batch = store.get_for_people_batch(["person-a", "person-b", "person-c"], limit_per_person=500)

        assert [e.id for e in batch["person-a"]] == [e.id for e in store.get_for_person("person-a", limit=500)]
        assert [e.id for e in batch["person-b"]] == [e.id for e in store.get_for_person("person-b", limit=500)]
        # No source entities at all -> simply absent, matching get_for_person's []
        assert "person-c" not in batch
        assert store.get_for_person("person-c", limit=500) == []

    def test_truncates_to_limit_per_person_like_get_for_person(self, store):
        """limit_per_person caps each person's list the same way
        get_for_person(id, limit=N) does -- most recent N, in the same order."""
        self._add_entities(store, "person-a", 10)

        batch = store.get_for_people_batch(["person-a"], limit_per_person=3)
        single = store.get_for_person("person-a", limit=3)

        assert len(batch["person-a"]) == 3
        assert [e.id for e in batch["person-a"]] == [e.id for e in single]

    def test_dedupes_repeated_ids_in_input(self, store):
        """Passing the same ID multiple times does not duplicate its entities
        or blow up -- the result is keyed by ID, so repeats collapse."""
        self._add_entities(store, "person-a", 2)

        batch = store.get_for_people_batch(["person-a", "person-a", "person-a"], limit_per_person=500)
        assert len(batch) == 1
        assert len(batch["person-a"]) == 2

    def test_empty_input_returns_empty_dict(self, store):
        assert store.get_for_people_batch([], limit_per_person=500) == {}

    def test_chunks_across_batch_chunk_size_boundary(self, store, monkeypatch):
        """More IDs than fit in one compound statement still return complete,
        correct results by issuing multiple chunked queries."""
        monkeypatch.setattr(store, "_BATCH_CHUNK_SIZE", 3)

        ids = [f"person-{i}" for i in range(7)]  # forces 3 chunks: 3+3+1
        for pid in ids:
            self._add_entities(store, pid, 2)

        batch = store.get_for_people_batch(ids, limit_per_person=500)

        assert set(batch.keys()) == set(ids)
        for pid in ids:
            assert [e.id for e in batch[pid]] == [e.id for e in store.get_for_person(pid, limit=500)]

    def test_issues_one_query_for_a_page_within_chunk_size(self, store, monkeypatch):
        """A page of IDs that fits within one chunk issues exactly one query
        to the database -- not one query per person."""
        ids = [f"person-{i}" for i in range(50)]
        for pid in ids:
            self._add_entities(store, pid, 2)

        counting_conn = _CountingConnection(store._get_connection())
        monkeypatch.setattr(store, "_get_connection", lambda: counting_conn)

        store.get_for_people_batch(ids, limit_per_person=500)

        assert counting_conn.execute_count == 1


class TestRematchingEligibility:
    """Tests for the #507 backoff retry policy on capped source entities.

    `store.add()` never persists match_attempted_at/match_attempt_count (those
    are only ever set via `record_match_attempt`), so tests write them
    directly via raw SQL to simulate an entity with a specific attempt
    history at a specific point in the past.
    """

    def _set_attempts(self, store, entity_id, count, days_ago):
        """Backdate an entity's match attempt bookkeeping for test setup."""
        attempted_at = (
            datetime.now(timezone.utc) - timedelta(days=days_ago)
        ).isoformat()
        conn = sqlite3.connect(store.db_path)
        try:
            conn.execute(
                "UPDATE source_entities SET match_attempt_count = ?, "
                "match_attempted_at = ? WHERE id = ?",
                (count, attempted_at, entity_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_fresh_entity_still_eligible_after_min_days(self, store):
        """No regression: an entity with 0 prior attempts behaves as before."""
        entity = SourceEntity(source_type="gmail", source_id="fresh1")
        store.add(entity)
        self._set_attempts(store, entity.id, count=0, days_ago=31)

        results = store.get_unlinked_for_rematching(min_days_since_attempt=30, max_attempts=3)
        assert entity.id in [e.id for e in results]

    def test_fresh_entity_not_eligible_within_min_days(self, store):
        """No regression: an entity attempted 5 days ago is still on cooldown."""
        entity = SourceEntity(source_type="gmail", source_id="fresh2")
        store.add(entity)
        self._set_attempts(store, entity.id, count=1, days_ago=5)

        results = store.get_unlinked_for_rematching(min_days_since_attempt=30, max_attempts=3)
        assert entity.id not in [e.id for e in results]

    def test_capped_entity_outside_backoff_window_not_eligible(self, store):
        """A capped entity (count=3) attempted only 10 days ago must wait."""
        entity = SourceEntity(source_type="gmail", source_id="capped_recent")
        store.add(entity)
        self._set_attempts(store, entity.id, count=3, days_ago=10)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, backoff_multiplier=3,
        )
        assert entity.id not in [e.id for e in results]

    def test_capped_entity_past_backoff_window_is_eligible(self, store):
        """A capped entity (count=3) past the 30-day window (attempt 4) is eligible again."""
        entity = SourceEntity(source_type="gmail", source_id="capped_past")
        store.add(entity)
        self._set_attempts(store, entity.id, count=3, days_ago=31)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, backoff_multiplier=3,
        )
        assert entity.id in [e.id for e in results]

    def test_backoff_grows_with_attempt_count(self, store):
        """count=4 (attempt 5) needs the *next* backoff tier (90d), not just 30d."""
        entity = SourceEntity(source_type="gmail", source_id="capped_grown")
        store.add(entity)
        # Past the count=3 tier's 30-day window, but not past count=4's 90-day window.
        self._set_attempts(store, entity.id, count=4, days_ago=40)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, backoff_multiplier=3,
        )
        assert entity.id not in [e.id for e in results]

        # Same entity, but now 91 days since last attempt clears the 90-day tier.
        self._set_attempts(store, entity.id, count=4, days_ago=91)
        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, backoff_multiplier=3,
        )
        assert entity.id in [e.id for e in results]

    def test_per_run_cap_limits_capped_entities(self, store):
        """max_capped_per_run bounds how many capped entities surface per call."""
        ids = []
        for i in range(10):
            entity = SourceEntity(source_type="gmail", source_id=f"capped_bulk{i}")
            store.add(entity)
            # All well past the 30-day window for count=3, so all are eligible
            # under backoff alone.
            self._set_attempts(store, entity.id, count=3, days_ago=60)
            ids.append(entity.id)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, max_capped_per_run=4,
        )
        assert len(results) == 4
        assert all(r.id in ids for r in results)

    def test_per_run_cap_does_not_limit_uncapped_entities(self, store):
        """The per-run cap only applies to capped (count >= max_attempts) entities."""
        for i in range(5):
            entity = SourceEntity(source_type="gmail", source_id=f"uncapped{i}")
            store.add(entity)
            self._set_attempts(store, entity.id, count=1, days_ago=31)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3, max_capped_per_run=0,
        )
        assert len(results) == 5

    def test_count_capped_backlog(self, store):
        """count_capped_backlog reports the total stuck population, not just eligible."""
        for i in range(3):
            entity = SourceEntity(source_type="gmail", source_id=f"backlog{i}")
            store.add(entity)
            # Recent attempt — not eligible under backoff, but still "capped".
            self._set_attempts(store, entity.id, count=3, days_ago=1)

        uncapped = SourceEntity(source_type="gmail", source_id="not_capped")
        store.add(uncapped)
        self._set_attempts(store, uncapped.id, count=1, days_ago=1)

        assert store.count_capped_backlog(max_attempts=3) == 3


class TestBlocklistedExcludedFromRematching:
    """
    Blocklisted marketing addresses must never enter the rematching pool.

    They can never resolve to a person, but the linking script recorded a match
    attempt on every skip, so they were re-queued under backoff forever and made
    up 70% of the reported capped backlog (#550).
    """

    # A marketing ESP domain, not a personal address.
    BLOCKLISTED_EMAIL = "campaign@mailchimp.com"

    def _set_attempts(self, store, entity_id, count, days_ago):
        attempted = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE source_entities SET match_attempt_count = ?, match_attempted_at = ? "
            "WHERE id = ?",
            (count, attempted, entity_id),
        )
        conn.commit()
        conn.close()

    def test_blocklisted_entity_excluded_from_pool(self, store):
        """A blocklisted entity is never returned for rematching."""
        blocked = SourceEntity(
            source_type="gmail",
            source_id="promo1",
            observed_email=self.BLOCKLISTED_EMAIL,
        )
        store.add(blocked)
        self._set_attempts(store, blocked.id, count=3, days_ago=60)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3,
        )
        assert blocked.id not in [e.id for e in results]

    def test_non_blocklisted_entity_still_eligible(self, store):
        """The filter is targeted — an ordinary address is unaffected."""
        ordinary = SourceEntity(
            source_type="gmail",
            source_id="real1",
            observed_email="sam@example.com",
        )
        store.add(ordinary)
        self._set_attempts(store, ordinary.id, count=3, days_ago=60)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3,
        )
        assert ordinary.id in [e.id for e in results]

    def test_entity_without_email_still_eligible(self, store):
        """No observed_email means not blocklisted — may still match on name/phone."""
        no_email = SourceEntity(source_type="imessage", source_id="sms1")
        store.add(no_email)
        self._set_attempts(store, no_email.id, count=3, days_ago=60)

        results = store.get_unlinked_for_rematching(
            min_days_since_attempt=30, max_attempts=3,
        )
        assert no_email.id in [e.id for e in results]

    def test_capped_backlog_excludes_blocklisted(self, store):
        """The reported backlog counts only work that can actually drain."""
        blocked = SourceEntity(
            source_type="gmail",
            source_id="promo2",
            observed_email=self.BLOCKLISTED_EMAIL,
        )
        store.add(blocked)
        self._set_attempts(store, blocked.id, count=3, days_ago=1)

        ordinary = SourceEntity(
            source_type="gmail",
            source_id="real2",
            observed_email="sam@example.com",
        )
        store.add(ordinary)
        self._set_attempts(store, ordinary.id, count=3, days_ago=1)

        assert store.count_capped_backlog(max_attempts=3) == 1
        assert store.count_capped_backlog(
            max_attempts=3, exclude_blocklisted=False,
        ) == 2


class TestLinkMethod:
    """Tests for link_method provenance tracking."""

    def test_link_method_in_dataclass(self):
        """link_method field defaults to None."""
        entity = SourceEntity(source_type="gmail", source_id="msg1")
        assert entity.link_method is None

    def test_link_method_set_on_creation(self):
        """link_method can be set at creation time."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        assert entity.link_method == "email_exact"

    def test_link_method_persisted_on_add(self, store):
        """link_method is stored and retrieved via add/get."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="name_fuzzy",
        )
        store.add(entity)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "name_fuzzy"

    def test_link_method_persisted_on_update(self, store):
        """link_method is preserved through update."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        store.add(entity)

        entity.link_method = "phone_exact"
        store.update(entity)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "phone_exact"

    def test_link_to_person_with_method(self, store):
        """link_to_person sets link_method when provided."""
        entity = SourceEntity(source_type="gmail", source_id="msg1")
        store.add(entity)

        store.link_to_person(
            entity.id, "person1", confidence=0.9, method="email_exact",
        )

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "email_exact"

    def test_link_to_person_preserves_existing_method(self, store):
        """link_to_person without method keeps existing link_method (COALESCE)."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="name_fuzzy",
        )
        store.add(entity)

        store.link_to_person(entity.id, "person1", confidence=0.95)

        retrieved = store.get_by_id(entity.id)
        assert retrieved.link_method == "name_fuzzy"

    def test_link_method_in_to_dict(self):
        """link_method appears in to_dict output."""
        entity = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="email_exact",
        )
        d = entity.to_dict()
        assert d["link_method"] == "email_exact"

    def test_link_method_roundtrip_from_dict(self):
        """link_method survives to_dict → from_dict roundtrip."""
        original = SourceEntity(
            source_type="gmail",
            source_id="msg1",
            link_method="phone_exact",
        )
        restored = SourceEntity.from_dict(original.to_dict())
        assert restored.link_method == "phone_exact"

    def test_get_low_confidence_filter_by_link_method(self, store):
        """get_low_confidence can filter by link_method."""
        for i, method in enumerate(["email_exact", "name_fuzzy", "name_fuzzy"]):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
                link_confidence=0.7,
                link_method=method,
            )
            store.add(entity, validate_person=False)

        all_low = store.get_low_confidence(max_confidence=0.85)
        assert len(all_low) == 3

        fuzzy_only = store.get_low_confidence(max_confidence=0.85, link_method="name_fuzzy")
        assert len(fuzzy_only) == 2

        exact_only = store.get_low_confidence(max_confidence=0.85, link_method="email_exact")
        assert len(exact_only) == 1

    def test_count_low_confidence_filter_by_link_method(self, store):
        """count_low_confidence can filter by link_method."""
        for i, method in enumerate(["email_exact", "name_fuzzy"]):
            entity = SourceEntity(
                source_type="gmail",
                source_id=f"msg{i}",
                canonical_person_id="person1",
                link_confidence=0.6,
                link_method=method,
            )
            store.add(entity, validate_person=False)

        assert store.count_low_confidence(max_confidence=0.85) == 2
        assert store.count_low_confidence(max_confidence=0.85, link_method="name_fuzzy") == 1
        assert store.count_low_confidence(max_confidence=0.85, link_method="nonexistent") == 0


class TestFactoryFunctions:
    """Tests for source entity factory functions."""

    def test_create_gmail_source_entity(self):
        """Test Gmail source entity factory."""
        entity = create_gmail_source_entity(
            message_id="msg123",
            sender_email="John@Example.com",
            sender_name="John Doe",
        )

        assert entity.source_type == "gmail"
        assert entity.source_id == "msg123"
        assert entity.observed_email == "john@example.com"  # Lowercased
        assert entity.observed_name == "John Doe"

    def test_create_calendar_source_entity(self):
        """Test Calendar source entity factory."""
        entity = create_calendar_source_entity(
            event_id="event123",
            attendee_email="Jane@Example.com",
            attendee_name="Jane Smith",
        )

        assert entity.source_type == "calendar"
        assert entity.source_id == "event123:Jane@Example.com"
        assert entity.observed_email == "jane@example.com"

    def test_create_phone_source_entity(self):
        """Phone factory uses the canonical ``phone_{e164}`` source_id and
        sets observed_phone (issue #228 — single definition of the format).
        """
        from api.services.source_entity import create_phone_source_entity

        entity = create_phone_source_entity(
            phone="+15551234567",
            observed_name="Mom",
        )

        assert entity.source_type == "phone"
        assert entity.source_id == "phone_+15551234567"
        assert entity.observed_phone == "+15551234567"
        assert entity.observed_name == "Mom"
        # observed_at defaults to "now" when not supplied.
        assert entity.observed_at is not None
        # Email is irrelevant here.
        assert entity.observed_email is None

    def test_create_phone_source_entity_minimal(self):
        """Only ``phone`` is required; everything else takes safe defaults."""
        from api.services.source_entity import create_phone_source_entity

        entity = create_phone_source_entity(phone="+15558675309")
        assert entity.source_id == "phone_+15558675309"
        assert entity.observed_name is None
        assert entity.metadata == {}

    def test_create_imessage_source_entity_phone(self):
        """Test iMessage source entity with phone number."""
        entity = create_imessage_source_entity(
            handle="+15551234567",
            display_name="Mom",
        )

        assert entity.source_type == "imessage"
        assert entity.observed_phone == "+15551234567"
        assert entity.observed_email is None
        assert entity.observed_name == "Mom"

    def test_create_imessage_source_entity_email(self):
        """Test iMessage source entity with email."""
        entity = create_imessage_source_entity(
            handle="john@example.com",
        )

        assert entity.source_type == "imessage"
        assert entity.observed_email == "john@example.com"
        assert entity.observed_phone is None
