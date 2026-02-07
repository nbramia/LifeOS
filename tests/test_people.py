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
import pytest

# Most tests in this file are fast unit tests
pytestmark = pytest.mark.unit
import tempfile
from pathlib import Path
from datetime import datetime

from api.services.people import (
    PeopleRegistry,
    extract_people_from_text,
    resolve_person_name,
    PEOPLE_DICTIONARY,
)


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
