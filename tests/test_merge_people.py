"""
Tests for person merge functionality.

Tests cover:
- Merged ID tracking (load, save, follow chain)
- Person search
- Duplicate detection
- Merge operation logic
"""
import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import test subject after mocking path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

pytestmark = pytest.mark.unit


class TestMergedIdsTracking:
    """Tests for merged ID persistence and chain following."""

    @pytest.fixture
    def temp_merged_ids_file(self, tmp_path):
        """Create a temporary merged IDs file."""
        merged_file = tmp_path / "merged_person_ids.json"
        return merged_file

    def test_load_merged_ids_empty(self, temp_merged_ids_file):
        """Load returns empty dict when file doesn't exist."""
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import load_merged_ids
            result = load_merged_ids()
            assert result == {}

    def test_load_merged_ids_with_data(self, temp_merged_ids_file):
        """Load returns existing mappings."""
        temp_merged_ids_file.write_text('{"old-id": "new-id"}')
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import load_merged_ids
            result = load_merged_ids()
            assert result == {"old-id": "new-id"}

    def test_save_merged_ids(self, temp_merged_ids_file):
        """Save writes merged IDs to file."""
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import save_merged_ids
            save_merged_ids({"secondary-1": "primary-1"})
            assert temp_merged_ids_file.exists()
            data = json.loads(temp_merged_ids_file.read_text())
            assert data == {"secondary-1": "primary-1"}

    def test_get_canonical_person_id_not_merged(self, temp_merged_ids_file):
        """Non-merged ID returns itself."""
        temp_merged_ids_file.write_text('{}')
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import get_canonical_person_id
            result = get_canonical_person_id("person-1")
            assert result == "person-1"

    def test_get_canonical_person_id_single_merge(self, temp_merged_ids_file):
        """Single merge returns primary ID."""
        temp_merged_ids_file.write_text('{"secondary": "primary"}')
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import get_canonical_person_id
            result = get_canonical_person_id("secondary")
            assert result == "primary"

    def test_get_canonical_person_id_chain(self, temp_merged_ids_file):
        """Follows merge chain to find ultimate primary."""
        temp_merged_ids_file.write_text('{"c": "b", "b": "a"}')
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import get_canonical_person_id
            result = get_canonical_person_id("c")
            assert result == "a"

    def test_get_canonical_person_id_avoids_cycles(self, temp_merged_ids_file):
        """Handles circular references gracefully."""
        temp_merged_ids_file.write_text('{"a": "b", "b": "a"}')
        with patch('scripts.merge_people.MERGED_IDS_FILE', temp_merged_ids_file):
            from scripts.merge_people import get_canonical_person_id
            # Should not infinite loop - stops when cycle detected
            result = get_canonical_person_id("a")
            assert result in ["a", "b"]


class TestSearchPeople:
    """Tests for person search functionality."""

    @pytest.fixture
    def mock_person_store(self):
        """Create mock person store with test data."""
        from api.services.person_entity import PersonEntity

        people = [
            PersonEntity(
                id="person-1",
                canonical_name="John Smith",
                emails=["john@example.com"],
                phone_numbers=["+15551234567"],
            ),
            PersonEntity(
                id="person-2",
                canonical_name="Jane Doe",
                emails=["jane@company.com"],
                phone_numbers=[],
            ),
            PersonEntity(
                id="person-3",
                canonical_name="John Doe",
                emails=["jdoe@example.com"],
                phone_numbers=["+15559876543"],
            ),
        ]

        store = MagicMock()
        store.get_all.return_value = people
        return store

    def test_search_by_name(self, mock_person_store):
        """Search matches by name."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import search_people
            results = search_people("John")
            assert len(results) == 2
            names = [p.canonical_name for p in results]
            assert "John Smith" in names
            assert "John Doe" in names

    def test_search_by_email(self, mock_person_store):
        """Search matches by email."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import search_people
            results = search_people("jane@company")
            assert len(results) == 1
            assert results[0].canonical_name == "Jane Doe"

    def test_search_by_phone(self, mock_person_store):
        """Search matches by phone number."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import search_people
            results = search_people("5551234567")
            assert len(results) == 1
            assert results[0].canonical_name == "John Smith"

    def test_search_case_insensitive(self, mock_person_store):
        """Search is case-insensitive."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import search_people
            results = search_people("JOHN")
            assert len(results) == 2

    def test_search_no_matches(self, mock_person_store):
        """Search returns empty list when no matches."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import search_people
            results = search_people("xyz123")
            assert len(results) == 0


class TestFindPotentialDuplicates:
    """Tests for duplicate person detection."""

    @pytest.fixture
    def mock_person_store(self):
        """Create mock person store with potential duplicates."""
        from api.services.person_entity import PersonEntity

        people = [
            PersonEntity(id="1", canonical_name="John Smith", emails=["john@a.com"]),
            PersonEntity(id="2", canonical_name="john smith", emails=["johnsmith@b.com"]),
            PersonEntity(id="3", canonical_name="Jane Doe", emails=["jane@example.com"]),
            PersonEntity(id="4", canonical_name="Bob Wilson Jr", emails=["bob@example.com"]),
            PersonEntity(id="5", canonical_name="Bob Wilson", emails=["bobw@example.com"]),
        ]

        store = MagicMock()
        store.get_all.return_value = people
        return store

    def test_finds_name_duplicates(self, mock_person_store):
        """Finds duplicates with same name (case-insensitive)."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import find_potential_duplicates
            results = find_potential_duplicates()

            # Should find "john smith" duplicates
            john_dups = [d for d in results if d['name'] == 'john smith']
            assert len(john_dups) == 1
            assert len(john_dups[0]['people']) == 2

    def test_handles_name_suffixes(self, mock_person_store):
        """Handles Jr/Sr suffixes when matching."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import find_potential_duplicates
            results = find_potential_duplicates()

            # Should find "bob wilson" duplicates (after removing Jr suffix)
            bob_dups = [d for d in results if 'bob wilson' in d['name']]
            assert len(bob_dups) == 1
            assert len(bob_dups[0]['people']) == 2

    def test_unique_names_not_duplicates(self, mock_person_store):
        """Unique names are not flagged as duplicates."""
        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store):
            from scripts.merge_people import find_potential_duplicates
            results = find_potential_duplicates()

            # Jane Doe should not appear (no duplicates)
            jane_dups = [d for d in results if 'jane' in d['name'].lower()]
            assert len(jane_dups) == 0


class TestMergePeople:
    """Tests for the merge_people function.

    Note: These tests verify the merge_people function interface.
    Deep integration testing requires database mocking which is complex.
    """

    @pytest.fixture
    def mock_person_store(self):
        """Create mock person store for merge testing."""
        from api.services.person_entity import PersonEntity

        person_store = MagicMock()
        primary = PersonEntity(
            id="primary-id",
            canonical_name="John Smith",
            emails=["john@a.com"],
            phone_numbers=["+15551111111"],
            sources=["gmail"],
        )
        secondary = PersonEntity(
            id="secondary-id",
            canonical_name="J. Smith",
            emails=["jsmith@b.com"],
            phone_numbers=["+15552222222"],
            sources=["calendar"],
        )
        person_store.get_by_id.side_effect = lambda x: primary if x == "primary-id" else (secondary if x == "secondary-id" else None)
        person_store.update.return_value = None
        person_store.delete.return_value = None
        person_store.save.return_value = None

        return person_store

    def test_merge_requires_valid_primary(self, mock_person_store, tmp_path):
        """Merge raises ValueError if primary person not found."""
        merged_file = tmp_path / "merged.json"
        merged_file.write_text('{}')

        # Return None for invalid ID
        mock_person_store.get_by_id.side_effect = lambda x: None

        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store), \
             patch('scripts.merge_people.MERGED_IDS_FILE', merged_file):

            from scripts.merge_people import merge_people
            with pytest.raises(ValueError, match="Primary person not found"):
                merge_people("invalid-id", "secondary-id", dry_run=True)

    def test_merge_dry_run_no_changes(self, mock_person_store, tmp_path):
        """Merge with dry_run=True doesn't modify data."""
        merged_file = tmp_path / "merged.json"
        merged_file.write_text('{}')

        mock_rel_store = MagicMock()
        mock_rel_store.get_for_person.return_value = []

        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store), \
             patch('scripts.merge_people.get_interaction_db_path', return_value=":memory:"), \
             patch('scripts.merge_people.get_crm_db_path', return_value=":memory:"), \
             patch('api.services.relationship.get_relationship_store', return_value=mock_rel_store), \
             patch('scripts.merge_people.MERGED_IDS_FILE', merged_file), \
             patch('sqlite3.connect') as mock_conn:

            # Setup mock connection
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = [0]  # COUNT(*) returns 0
            mock_conn.return_value.execute.return_value = mock_cursor

            from scripts.merge_people import merge_people
            merge_people("primary-id", "secondary-id", dry_run=True)

            # In dry run, store methods should not be called to persist
            mock_person_store.update.assert_not_called()
            mock_person_store.delete.assert_not_called()
            mock_person_store.save.assert_not_called()

            # Merged IDs file should not be updated
            assert json.loads(merged_file.read_text()) == {}

    def test_merge_result_structure(self, mock_person_store, tmp_path):
        """Merge returns expected result structure."""
        merged_file = tmp_path / "merged.json"
        merged_file.write_text('{}')

        with patch('scripts.merge_people.get_person_entity_store', return_value=mock_person_store), \
             patch('scripts.merge_people.get_interaction_db_path', return_value=":memory:"), \
             patch('scripts.merge_people.get_crm_db_path', return_value=":memory:"), \
             patch('scripts.merge_people.MERGED_IDS_FILE', merged_file), \
             patch('sqlite3.connect') as mock_conn:

            mock_cursor = MagicMock()
            mock_cursor.rowcount = 0
            mock_cursor.fetchone.return_value = (0,)  # Return tuple with count
            mock_cursor.fetchall.return_value = []
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn.return_value)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value.execute.return_value = mock_cursor

            from scripts.merge_people import merge_people
            result = merge_people("primary-id", "secondary-id", dry_run=True)

            # Check result has expected keys
            assert isinstance(result, dict)
            assert 'interactions_updated' in result
            assert 'source_entities_updated' in result


class TestMergeOperationDetails:
    """Tests for specific merge operation behaviors."""

    def test_emails_combined(self):
        """Merged person has emails from both."""
        from api.services.person_entity import PersonEntity

        primary = PersonEntity(id="p1", canonical_name="A", emails=["a@x.com"])
        secondary = PersonEntity(id="p2", canonical_name="A", emails=["a@y.com"])

        # Simulate merge
        combined_emails = list(set(primary.emails + secondary.emails))

        assert "a@x.com" in combined_emails
        assert "a@y.com" in combined_emails

    def test_sources_combined(self):
        """Merged person has sources from both."""
        from api.services.person_entity import PersonEntity

        primary = PersonEntity(id="p1", canonical_name="A", sources=["gmail"])
        secondary = PersonEntity(id="p2", canonical_name="A", sources=["calendar", "slack"])

        # Simulate merge
        combined_sources = list(set(primary.sources + secondary.sources))

        assert "gmail" in combined_sources
        assert "calendar" in combined_sources
        assert "slack" in combined_sources


class TestMergePeopleToneAnalysisResults:
    """Real, end-to-end coverage of the #910 addition: merging a person
    must remove tone_analysis_results rows for the absorbed (secondary)
    person, and must leave the primary's own rows alone (see the code
    comment in scripts/merge_people.py for why: unlike person_facts, a
    stale primary row self-heals via the existing interaction-count
    freshness check the next time tone analysis runs).

    Unlike TestMergePeople above (which mocks sqlite3.connect entirely),
    this exercises the real SQL against real temporary SQLite files, using
    the actual store classes to create schema so it can't drift from
    production. person_entity, relationship, and post-merge stats/strength
    refresh dependencies are stubbed since they're unrelated to what #910
    changed and would otherwise require substantially more fixture work
    to run for real (e.g. person_entities/person_emails/person_phones
    lookup-table upkeep, real interaction-derived stats)."""

    @pytest.fixture
    def merge_env(self, tmp_path, monkeypatch):
        from api.services.person_entity import PersonEntity, PersonEntityStore
        from api.services.relationship import RelationshipStore
        from api.services.source_entity import SourceEntityStore
        from api.services.person_facts import PersonFactStore, PersonFact
        from api.services.tone_analysis_store import ToneAnalysisStore
        from api.services.interaction_store import InteractionStore, Interaction
        from datetime import datetime, timezone

        crm_db_path = str(tmp_path / "crm.db")
        interactions_db_path = str(tmp_path / "interactions.db")

        person_store = PersonEntityStore(crm_db_path)
        RelationshipStore(crm_db_path)  # table only -- no rows needed
        SourceEntityStore(crm_db_path)  # table only -- no rows needed
        fact_store = PersonFactStore(crm_db_path)
        tone_store = ToneAnalysisStore(crm_db_path)
        interaction_store = InteractionStore(interactions_db_path, strict=False)

        primary = PersonEntity(id="primary-person", canonical_name="Primary Person")
        secondary = PersonEntity(id="secondary-person", canonical_name="Secondary Person")
        person_store.add(primary)
        person_store.add(secondary)

        fact_store.add(PersonFact(person_id="primary-person", category="work", key="k1", value="v1"))
        fact_store.add(PersonFact(person_id="secondary-person", category="work", key="k2", value="v2"))

        tone_store.upsert("primary-person", "2026-01", 2, {
            "user_score": 70.0, "partner_score": 70.0, "combined_score": 70.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        tone_store.upsert("secondary-person", "2026-01", 2, {
            "user_score": 40.0, "partner_score": 40.0, "combined_score": 40.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })
        tone_store.upsert("secondary-person", "2026-02", 3, {
            "user_score": 35.0, "partner_score": 35.0, "combined_score": 35.0,
            "user_sample_count": 1, "partner_sample_count": 1,
        })

        interaction_store.add(Interaction(
            id="synthetic-1", person_id="secondary-person",
            timestamp=datetime.now(timezone.utc), source_type="imessage",
            title="synthetic message",
        ))

        monkeypatch.setattr("scripts.merge_people.get_person_entity_store", lambda: person_store)
        monkeypatch.setattr("scripts.merge_people.get_crm_db_path", lambda: crm_db_path)
        monkeypatch.setattr("scripts.merge_people.get_interaction_db_path", lambda: interactions_db_path)
        monkeypatch.setattr("scripts.merge_people.MERGED_IDS_FILE", tmp_path / "merged_ids.json")
        monkeypatch.setattr("scripts.merge_people.MERGE_LOG_FILE", tmp_path / "merge_log.json")
        # Unrelated post-merge side effects (#910 doesn't touch either) --
        # stubbed rather than wired up for real to keep this fixture from
        # growing into a second copy of person_entity/interaction fixtures.
        monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda *a, **k: {})
        monkeypatch.setattr("api.services.relationship_metrics.update_strength_for_person", lambda *a, **k: None)

        return {"tone_store": tone_store, "fact_store": fact_store}

    def test_merge_removes_absorbed_persons_tone_rows_but_keeps_primarys(self, merge_env):
        from scripts.merge_people import merge_people

        stats = merge_people("primary-person", "secondary-person", dry_run=False)

        assert stats["tone_rows_cleared"] == 2

        tone_store = merge_env["tone_store"]
        assert tone_store.get_for_person("secondary-person") == []
        primary_rows = tone_store.get_for_person("primary-person")
        assert len(primary_rows) == 1
        assert primary_rows[0].result["user_score"] == 70.0  # untouched

    def test_dry_run_reports_the_count_without_deleting_anything(self, merge_env):
        from scripts.merge_people import merge_people

        stats = merge_people("primary-person", "secondary-person", dry_run=True)

        assert stats["tone_rows_cleared"] == 2
        tone_store = merge_env["tone_store"]
        assert len(tone_store.get_for_person("secondary-person")) == 2  # untouched by dry run

    def test_merge_still_clears_facts_for_both_ids(self, merge_env):
        """Regression guard: the pre-existing facts-cleared-for-both-ids
        behavior (person_facts, above the new tone_analysis_results step
        in scripts/merge_people.py) must be unaffected by #910's addition
        right after it."""
        from scripts.merge_people import merge_people

        stats = merge_people("primary-person", "secondary-person", dry_run=False)

        assert stats["facts_cleared"] == 2  # one from each id
        fact_store = merge_env["fact_store"]
        assert fact_store.get_for_person("primary-person") == []
        assert fact_store.get_for_person("secondary-person") == []

    @pytest.fixture
    def merge_env_without_tone_table(self, tmp_path, monkeypatch):
        """Same as merge_env, but the tone_analysis_results table is never
        created -- reproducing a crm.db from an install where tone
        analysis has never run (the table is created lazily). Regression
        coverage for a real bug this addition introduced and fixed in the
        same round: tests/test_merge_crash_safety.py's fixture builds its
        own crm.db without this table, and the first version of this
        change 500'd on it with `no such table: tone_analysis_results`."""
        from api.services.person_entity import PersonEntity, PersonEntityStore
        from api.services.relationship import RelationshipStore
        from api.services.source_entity import SourceEntityStore
        from api.services.person_facts import PersonFactStore
        from api.services.interaction_store import InteractionStore

        crm_db_path = str(tmp_path / "crm.db")
        interactions_db_path = str(tmp_path / "interactions.db")

        person_store = PersonEntityStore(crm_db_path)
        RelationshipStore(crm_db_path)
        SourceEntityStore(crm_db_path)
        PersonFactStore(crm_db_path)
        InteractionStore(interactions_db_path, strict=False)
        # Deliberately no ToneAnalysisStore(crm_db_path) call here.

        person_store.add(PersonEntity(id="primary-person", canonical_name="Primary Person"))
        person_store.add(PersonEntity(id="secondary-person", canonical_name="Secondary Person"))

        monkeypatch.setattr("scripts.merge_people.get_person_entity_store", lambda: person_store)
        monkeypatch.setattr("scripts.merge_people.get_crm_db_path", lambda: crm_db_path)
        monkeypatch.setattr("scripts.merge_people.get_interaction_db_path", lambda: interactions_db_path)
        monkeypatch.setattr("scripts.merge_people.MERGED_IDS_FILE", tmp_path / "merged_ids.json")
        monkeypatch.setattr("scripts.merge_people.MERGE_LOG_FILE", tmp_path / "merge_log.json")
        monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda *a, **k: {})
        monkeypatch.setattr("api.services.relationship_metrics.update_strength_for_person", lambda *a, **k: None)

    def test_merge_execute_succeeds_when_tone_analysis_results_table_does_not_exist_yet(
        self, merge_env_without_tone_table,
    ):
        from scripts.merge_people import merge_people

        stats = merge_people("primary-person", "secondary-person", dry_run=False)
        assert stats["tone_rows_cleared"] == 0

    def test_merge_dry_run_succeeds_when_tone_analysis_results_table_does_not_exist_yet(
        self, merge_env_without_tone_table,
    ):
        from scripts.merge_people import merge_people

        stats = merge_people("primary-person", "secondary-person", dry_run=True)
        assert stats["tone_rows_cleared"] == 0
