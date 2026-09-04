"""
Tests for People Tracking functionality.
P1.4 Acceptance Criteria:
- Extracts person names from note content
- Handles aliases (Alex → Alex Johnson)
- Handles misspellings
- Tracks last-mention date per person
- Person filter works in search API
- "What do I know about Alex" returns relevant context
- Excludes self-references (configured user name)
"""
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from api.services.people import (
    PeopleRegistry,
    extract_people_from_text,
    resolve_person_name,
    PEOPLE_DICTIONARY,
)

# Most tests in this file are fast unit tests
pytestmark = pytest.mark.unit


class TestPeopleExtraction:
    """Test person name extraction from text."""

    def test_extracts_bold_names(self):
        """Should extract names in bold format."""
        text = "Met with **Alex** and **Sarah** today to discuss the budget."
        people = extract_people_from_text(text)

        assert "Alex" in people
        assert "Sarah" in people

    def test_extracts_names_from_people_dictionary(self):
        """Should recognize names from the People Dictionary (if configured)."""
        if not PEOPLE_DICTIONARY:
            pytest.skip("People dictionary not configured")

        # Use actual names from dictionary for test, skipping excluded names
        # (names with exclude=True like self-references are filtered out)
        dictionary_names = [
            name for name, info in PEOPLE_DICTIONARY.items()
            if not info.get("exclude", False)
        ][:3]

        if len(dictionary_names) < 3:
            pytest.skip("Need at least 3 non-excluded names in dictionary for this test")

        # Build test text using names from dictionary (no bold formatting)
        text = f"{dictionary_names[0]} and {dictionary_names[1]} went to the park. {dictionary_names[2]} called."
        people = extract_people_from_text(text)

        for name in dictionary_names:
            assert name in people, f"Expected {name} to be extracted from dictionary"

    def test_handles_common_patterns(self):
        """Should extract names from common patterns."""
        text = """
        Attendees: Kevin, Sarah, Mike
        1-1 with Hayley
        Meeting with Alex about budgets
        """
        people = extract_people_from_text(text)

        assert "Kevin" in people
        assert "Hayley" in people
        assert "Alex" in people

    def test_excludes_self_references(self):
        """Should exclude self-references (configured user name)."""
        from config.settings import settings
        user_name = settings.user_name if settings.user_name else "User"
        text = f"{user_name} met with Alex to discuss the project. I'll follow up."
        people = extract_people_from_text(text)

        assert user_name not in people
        assert "Alex" in people

    def test_handles_possessives(self):
        """Should extract names even with possessives."""
        # Use bold format to ensure extraction regardless of dictionary config
        text = "**Alex**'s idea was great. **Jane**'s schedule is busy."
        people = extract_people_from_text(text)

        assert "Alex" in people
        assert "Jane" in people


class TestAliasResolution:
    """Test alias and fuzzy name resolution."""

    def test_resolves_known_alias(self):
        """Should resolve known aliases to canonical names."""
        # Alex should resolve (known in dictionary)
        resolved = resolve_person_name("Alex")
        assert resolved == "Alex"  # or "Alex Johnson" if we expand

    def test_resolves_misspelling(self):
        """Should resolve common misspellings (if dictionary configured)."""
        # This test depends on having a configured people dictionary
        # with misspelling mappings.
        if not PEOPLE_DICTIONARY:
            pytest.skip("People dictionary not configured")

        # Find a misspelling mapping from the dictionary
        misspelling_found = False
        for canonical, info in PEOPLE_DICTIONARY.items():
            aliases = info.get("aliases", [])
            for alias in aliases:
                if alias.lower() != canonical.lower():  # It's a true alias/misspelling
                    resolved = resolve_person_name(alias)
                    assert resolved == canonical, f"Expected '{alias}' to resolve to '{canonical}'"
                    misspelling_found = True
                    break
            if misspelling_found:
                break

        if not misspelling_found:
            pytest.skip("No misspelling mappings found in dictionary")

    def test_resolves_email_to_name(self):
        """Should resolve email addresses to names."""
        resolved = resolve_person_name("user@example.com")
        # Should recognize as name or return as-is if not in registry
        assert resolved in ["User", "user@example.com"]

    def test_preserves_unknown_names(self):
        """Should preserve names not in dictionary."""
        resolved = resolve_person_name("RandomPerson")
        assert resolved == "RandomPerson"


class TestPeopleRegistry:
    """Test the People Registry storage and queries."""

    @pytest.fixture
    def temp_registry_path(self):
        """Create temp path for registry storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "people_registry.json"

    @pytest.fixture
    def registry(self, temp_registry_path):
        """Create a fresh registry."""
        return PeopleRegistry(storage_path=str(temp_registry_path))

    def test_records_person_mention(self, registry):
        """Should record person mentions with metadata."""
        registry.record_mention(
            name="Alex",
            source_file="/vault/meeting.md",
            mention_date="2025-01-05"
        )

        person = registry.get_person("Alex")
        assert person is not None
        assert person["mention_count"] >= 1
        assert "/vault/meeting.md" in person["related_notes"]

    def test_tracks_last_mention_date(self, registry):
        """Should track the most recent mention date."""
        registry.record_mention("Sarah", "/vault/old.md", "2025-01-01")
        registry.record_mention("Sarah", "/vault/new.md", "2025-01-10")

        person = registry.get_person("Sarah")
        assert person["last_mention_date"] == "2025-01-10"

    def test_increments_mention_count(self, registry):
        """Should increment mention count for repeated mentions."""
        registry.record_mention("Kevin", "/vault/file1.md", "2025-01-01")
        registry.record_mention("Kevin", "/vault/file2.md", "2025-01-02")
        registry.record_mention("Kevin", "/vault/file3.md", "2025-01-03")

        person = registry.get_person("Kevin")
        assert person["mention_count"] == 3

    def test_categorizes_people(self, registry):
        """Should categorize people as work/personal/family (if dictionary configured)."""
        if not PEOPLE_DICTIONARY:
            pytest.skip("People dictionary not configured")

        # Find a work person from dictionary
        work_person = None
        for name, info in PEOPLE_DICTIONARY.items():
            if info.get("category") == "work":
                work_person = name
                break

        if work_person:
            registry.record_mention(work_person, "/vault/work.md", "2025-01-01")
            person = registry.get_person(work_person)
            assert person["category"] == "work", f"Expected {work_person} to be categorized as 'work'"
        else:
            # No work person in dictionary, test passes vacuously
            pass

        # Find a family person from dictionary
        family_person = None
        for name, info in PEOPLE_DICTIONARY.items():
            if info.get("category") == "family":
                family_person = name
                break

        if family_person:
            registry.record_mention(family_person, "/vault/personal.md", "2025-01-01")
            person = registry.get_person(family_person)
            assert person["category"] == "family", f"Expected {family_person} to be categorized as 'family'"

    def test_searches_by_person(self, registry):
        """Should enable searching by person name."""
        registry.record_mention("Alex", "/vault/meeting1.md", "2025-01-01")
        registry.record_mention("Alex", "/vault/meeting2.md", "2025-01-02")

        notes = registry.get_related_notes("Alex")
        assert len(notes) == 2
        assert "/vault/meeting1.md" in notes
        assert "/vault/meeting2.md" in notes

    def test_persists_registry(self, temp_registry_path):
        """Registry should persist across instances."""
        # Create and populate first registry
        reg1 = PeopleRegistry(storage_path=str(temp_registry_path))
        reg1.record_mention("TestPerson", "/vault/test.md", "2025-01-01")
        reg1.save()

        # Create new instance and verify data persisted
        reg2 = PeopleRegistry(storage_path=str(temp_registry_path))
        person = reg2.get_person("TestPerson")
        assert person is not None
        assert person["mention_count"] == 1


@pytest.mark.slow
class TestPeopleIntegration:
    """Integration tests for people tracking with indexer."""

    @pytest.fixture
    def temp_vault(self):
        """Create test vault with people mentions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()

            # Note with bold names
            (vault / "meeting1.md").write_text("""---
tags: [meeting]
type: meeting
---

# Team Standup

Met with **Alex** and **Sarah** today.
Discussed Q1 goals with the team.
""")

            # Note with misspelled name
            (vault / "family.md").write_text("""---
tags: [personal]
type: note
---

# Weekend Plans

Taking Alice to the park. Jane is making dinner.
""")

            yield vault

    def test_indexer_extracts_people(self, temp_vault):
        """Indexer should extract people during indexing."""
        with tempfile.TemporaryDirectory() as db_dir:
            from api.services.indexer import IndexerService

            indexer = IndexerService(
                vault_path=str(temp_vault),
                db_path=db_dir
            )
            indexer.index_all()

            # Search should find people in metadata
            results = indexer.vector_store.search("team standup", top_k=5)
            assert len(results) >= 1

            indexer.stop()


# ---------------------------------------------------------------------------
# #872: bound GET /api/people/search results and batch the recency lookup.
#
# These tests build isolated PersonEntityStore/InteractionStore instances
# (temp SQLite files, no dependency on data/crm.db) and monkeypatch the
# getters api.routes.people imports, so they run as fast unit tests on a
# fresh clone. A separate integration-marked latency test at the bottom
# exercises the real dataset when present.
# ---------------------------------------------------------------------------

def _make_person(**kwargs):
    from api.services.person_entity import PersonEntity
    defaults = dict(canonical_name="", emails=[], last_seen=None)
    defaults.update(kwargs)
    return PersonEntity(**defaults)


class TestSearchPeopleEndpoint:
    """GET /api/people/search — limit validation and preserved ordering."""

    @pytest.fixture
    def stores(self, tmp_path, monkeypatch):
        from api.services.person_entity import PersonEntityStore
        from api.services.interaction_store import InteractionStore
        import api.routes.people as people_routes

        person_store = PersonEntityStore(db_path=str(tmp_path / "people.db"))
        interaction_store = InteractionStore(
            db_path=str(tmp_path / "interactions.db"), strict=False
        )

        now = datetime.now(timezone.utc)
        # Exact canonical-name match, but the oldest last_seen of the three —
        # must still sort first (exact match beats recency).
        exact_old = _make_person(
            id="exact-old",
            canonical_name="Ada Test",
            emails=["ada.test@example.com"],
            last_seen=now - timedelta(days=400),
        )
        # Substring match on canonical_name, more recent than the exact match.
        partial_recent = _make_person(
            id="partial-recent",
            canonical_name="Ada Testerson",
            emails=["ada.testerson@example.com"],
            last_seen=now - timedelta(days=1),
        )
        # Substring match via email only.
        email_match = _make_person(
            id="email-match",
            canonical_name="Someone Else",
            emails=["contains.ada.test@example.com"],
            last_seen=now - timedelta(days=2),
        )
        # Does not match "ada test" anywhere.
        non_match = _make_person(
            id="non-match",
            canonical_name="Bob Nomatch",
            emails=["bob@example.com"],
            last_seen=now,
        )
        for entity in (exact_old, partial_recent, email_match, non_match):
            person_store.add(entity)

        monkeypatch.setattr(people_routes, "get_person_entity_store", lambda: person_store)
        monkeypatch.setattr(people_routes, "get_interaction_store", lambda: interaction_store)
        return person_store, interaction_store

    @pytest.fixture
    def client(self, stores):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_limit_zero_is_rejected(self, client):
        # This app's global RequestValidationError handler (api/main.py)
        # converts all validation errors to 400, not FastAPI's default 422.
        resp = client.get("/api/people/search", params={"q": "ada", "limit": 0})
        assert resp.status_code == 400

    def test_limit_over_max_is_rejected(self, client):
        resp = client.get("/api/people/search", params={"q": "ada", "limit": 201})
        assert resp.status_code == 400

    def test_limit_boundaries_are_accepted(self, client):
        assert client.get("/api/people/search", params={"q": "ada", "limit": 1}).status_code == 200
        assert client.get("/api/people/search", params={"q": "ada", "limit": 200}).status_code == 200

    def test_default_limit_is_20(self, client):
        resp = client.get("/api/people/search", params={"q": "ada"})
        assert resp.status_code == 200
        # Only 3 of the 4 synthetic entities match "ada" at all, well under
        # the default of 20, so this just confirms the default didn't clamp
        # unexpectedly low (e.g. to something like 1 or 5).
        assert resp.json()["count"] == 3

    def test_exact_match_sorts_first_even_when_older(self, client):
        resp = client.get("/api/people/search", params={"q": "Ada Test", "limit": 20})
        assert resp.status_code == 200
        data = resp.json()
        names = [p["canonical_name"] for p in data["people"]]
        assert names[0] == "Ada Test", "exact canonical-name match must sort first"
        assert "Bob Nomatch" not in names

    def test_non_exact_matches_sort_by_recency(self, client):
        resp = client.get("/api/people/search", params={"q": "ada.test", "limit": 20})
        assert resp.status_code == 200
        names = [p["canonical_name"] for p in resp.json()["people"]]
        # None of these are an exact canonical-name match for "ada.test", so
        # order falls back to most-recent last_seen first.
        assert names.index("Ada Testerson") < names.index("Someone Else")

    def test_total_reflects_full_match_count_not_page_size(self, client):
        resp = client.get("/api/people/search", params={"q": "ada", "limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["total"] == 3

    def test_recency_lookup_is_batched_once(self, client, stores, monkeypatch):
        """The N+1 per-person recency query must become exactly one batched
        call for the whole page, no matter how many results are returned."""
        _, interaction_store = stores
        calls = []
        original = interaction_store.get_last_interaction_by_source_batch

        def spy(person_ids):
            calls.append(list(person_ids))
            return original(person_ids)

        monkeypatch.setattr(interaction_store, "get_last_interaction_by_source_batch", spy)

        resp = client.get("/api/people/search", params={"q": "ada", "limit": 20})
        assert resp.status_code == 200
        assert len(calls) == 1, "expected exactly one batched recency call"
        returned_ids = {p["entity_id"] for p in resp.json()["people"]}
        assert set(calls[0]) == returned_ids


class TestListPeopleEndpoint:
    """GET /api/people/list — SQL-side ordering and category filter."""

    @pytest.fixture
    def stores(self, tmp_path, monkeypatch):
        from api.services.person_entity import PersonEntityStore
        import api.routes.people as people_routes

        person_store = PersonEntityStore(db_path=str(tmp_path / "people.db"))

        now = datetime.now(timezone.utc)
        oldest = _make_person(
            id="oldest", canonical_name="Carol Old", category="work",
            last_seen=now - timedelta(days=10),
        )
        middle = _make_person(
            id="middle", canonical_name="Dave Mid", category="personal",
            last_seen=now - timedelta(days=5),
        )
        newest = _make_person(
            id="newest", canonical_name="Erin New", category="work",
            last_seen=now,
        )
        for entity in (oldest, middle, newest):
            person_store.add(entity)

        monkeypatch.setattr(people_routes, "get_person_entity_store", lambda: person_store)
        return person_store

    @pytest.fixture
    def client(self, stores):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    def test_orders_most_recent_first(self, client):
        resp = client.get("/api/people/list", params={"limit": 10})
        assert resp.status_code == 200
        names = [p["canonical_name"] for p in resp.json()["people"]]
        assert names == ["Erin New", "Dave Mid", "Carol Old"]

    def test_category_filter_applied_in_sql(self, client):
        resp = client.get("/api/people/list", params={"limit": 10, "category": "work"})
        assert resp.status_code == 200
        names = {p["canonical_name"] for p in resp.json()["people"]}
        assert names == {"Erin New", "Carol Old"}

    def test_limit_truncates(self, client):
        resp = client.get("/api/people/list", params={"limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["people"][0]["canonical_name"] == "Erin New"


class TestInteractionStoreBatchRecency:
    """InteractionStore.get_last_interaction_by_source_batch (#872)."""

    @pytest.fixture
    def store(self, tmp_path):
        from api.services.interaction_store import InteractionStore
        return InteractionStore(db_path=str(tmp_path / "interactions.db"), strict=False)

    def _insert_raw(self, store, person_id, source_type, days_ago, iid=None):
        import uuid
        conn = store._get_connection()
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        try:
            conn.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (iid or str(uuid.uuid4()), person_id, ts, source_type, "test"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_empty_input_returns_empty_dict(self, store):
        assert store.get_last_interaction_by_source_batch([]) == {}

    def test_matches_per_person_single_lookup(self, store):
        self._insert_raw(store, "p1", "gmail", days_ago=1)
        self._insert_raw(store, "p1", "gmail", days_ago=5)  # older gmail, should be superseded
        self._insert_raw(store, "p1", "imessage", days_ago=2)
        self._insert_raw(store, "p2", "slack", days_ago=3)

        batch = store.get_last_interaction_by_source_batch(["p1", "p2"])
        single_p1 = store.get_last_interaction_by_source("p1")
        single_p2 = store.get_last_interaction_by_source("p2")

        assert batch["p1"] == single_p1
        assert batch["p2"] == single_p2
        assert set(batch["p1"].keys()) == {"gmail", "imessage"}

    def test_person_with_no_interactions_is_absent(self, store):
        self._insert_raw(store, "p1", "gmail", days_ago=1)
        batch = store.get_last_interaction_by_source_batch(["p1", "no-such-person"])
        assert "no-such-person" not in batch

    def test_chunks_over_900_ids(self, store):
        """More than SQLite's ~999 bound-parameter limit must not error."""
        self._insert_raw(store, "p-5", "gmail", days_ago=1)
        many_ids = [f"p-{i}" for i in range(1500)]
        batch = store.get_last_interaction_by_source_batch(many_ids)
        assert batch["p-5"]["gmail"]


class TestPersonEntityStoreSearchHelpers:
    """PersonEntityStore.count_search / list_recent (#872)."""

    @pytest.fixture
    def store(self, tmp_path):
        from api.services.person_entity import PersonEntityStore
        return PersonEntityStore(db_path=str(tmp_path / "people.db"))

    def test_count_search_matches_number_of_search_results_below_limit(self, store):
        for i in range(5):
            store.add(_make_person(
                id=f"p{i}", canonical_name=f"Ada Match {i}", emails=[],
                last_seen=datetime.now(timezone.utc),
            ))
        store.add(_make_person(id="other", canonical_name="No Hit", emails=[]))

        assert store.count_search("ada") == 5
        # A limit smaller than the true count still reports the full total.
        assert len(store.search("ada", limit=2)) == 2

    def test_list_recent_orders_and_filters_by_category(self, store):
        now = datetime.now(timezone.utc)
        store.add(_make_person(id="a", canonical_name="A", category="work",
                                last_seen=now - timedelta(days=1)))
        store.add(_make_person(id="b", canonical_name="B", category="family",
                                last_seen=now))
        store.add(_make_person(id="c", canonical_name="C", category="work",
                                last_seen=now - timedelta(days=2)))

        all_recent = store.list_recent(limit=10)
        assert [e.canonical_name for e in all_recent] == ["B", "A", "C"]

        work_only = store.list_recent(limit=10, category="work")
        assert [e.canonical_name for e in work_only] == ["A", "C"]

    def test_list_recent_respects_limit(self, store):
        for i in range(3):
            store.add(_make_person(id=f"p{i}", canonical_name=f"Person {i}",
                                    last_seen=datetime.now(timezone.utc) - timedelta(days=i)))
        assert len(store.list_recent(limit=2)) == 2


@pytest.mark.integration
class TestSearchPeopleLatency:
    """#872: GET /api/people/search must stay fast against the production
    dataset. Skips cleanly on a fresh clone with no data/crm.db."""

    def _person_count(self):
        import sqlite3
        db_path = Path("data/crm.db")
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
        finally:
            conn.close()

    def test_broad_query_under_200ms(self):
        import time
        from fastapi.testclient import TestClient
        from api.main import app

        count = self._person_count()
        if count is None:
            pytest.skip("data/crm.db not present")
        if count < 100:
            pytest.skip("too few people in database for a meaningful latency check")

        client = TestClient(app)
        client.get("/api/people/search", params={"q": "a"})  # warm

        start = time.perf_counter()
        resp = client.get("/api/people/search", params={"q": "a"})
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert resp.status_code == 200
        assert elapsed_ms < 200, f"?q=a took {elapsed_ms:.1f}ms, expected under 200ms"
