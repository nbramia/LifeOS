"""What indexing a note does to people: generated notes, excluded folders, ambiguous mentions."""
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services import indexer as indexer_module
from api.services.entity_resolver import EntityResolver
from api.services.indexer import IndexerService
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.source_entity import (
    LINK_STATUS_CONFIRMED,
    LINK_STATUS_REJECTED,
    SourceEntity,
    SourceEntityStore,
)
from api.services.vault_listing import in_underscore_dir, is_generated_note, vault_relative_path
from api.utils.date_parser import extract_note_date
from config.employment_history import parse_employment_history

pytestmark = pytest.mark.unit


class FakeInteractions:
    """The interaction store's note-facing surface, without its temp-path guard."""

    def __init__(self, db_path):
        import sqlite3

        self.db_path = str(db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE interactions (id TEXT, person_id TEXT, timestamp TEXT, source_type TEXT, "
            "title TEXT, snippet TEXT, source_link TEXT, source_id TEXT, created_at TEXT, "
            "source_account TEXT, attendee_count INTEGER)"
        )
        conn.commit()
        conn.close()
        self.added: dict[str, str] = {}

    def add_if_not_exists(self, interaction):
        if interaction.source_id in self.added:
            return interaction, False
        self.added[interaction.source_id] = interaction.person_id
        return interaction, True


class World:
    def __init__(self, tmp_path: Path):
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.people = PersonEntityStore(str(tmp_path / "crm.db"))
        self.sources = SourceEntityStore(str(tmp_path / "crm.db"))
        self.interactions = FakeInteractions(tmp_path / "interactions.db")
        self.indexer = IndexerService.__new__(IndexerService)
        self.indexer.vault_path = self.vault
        self.indexer._entity_resolver = EntityResolver(self.people)
        self.indexer._interaction_store = self.interactions
        self.indexer._source_entity_store = self.sources
        self.indexer._tag_store = MagicMock()
        self.indexer._tag_store.get.return_value = None
        self.indexer._tag_store_failed = False
        self.indexer.vector_store = MagicMock()
        self.indexer.bm25_index = MagicMock()

    def note(self, rel, content):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def index(self, path):
        self.indexer.index_file(str(path), skip_summaries=True)

    def source_entity(self, path, name, source_type="vault"):
        return self.sources.get_by_source(source_type, f"{path.resolve()}:{name}")


@pytest.fixture
def world(tmp_path):
    with (
        patch("api.services.person_entity.get_person_entity_store") as person_store,
        patch("config.employment_history.get_employment_history", return_value=parse_employment_history({})),
    ):
        built = World(tmp_path)
        person_store.return_value = built.people
        yield built


class TestGeneratedMarkers:
    @pytest.mark.parametrize("frontmatter,expected", [
        ({"source": "llm"}, True),
        ({"source": "LLM"}, True),
        ({"source": " llm "}, True),
        ({"generated": "2026-11-01"}, True),
        ({"generated": date(2026, 11, 1)}, True),
        ({"generated": True}, True),
        ({"generated": False}, False),
        ({"generated": "false"}, False),
        ({"generated": ""}, False),
        ({"source": "granola"}, False),
        ({"source": "manual"}, False),
        ({"type": "synthesis"}, False),
        ({"type": "reference"}, False),
        ({"generated_at": "2026-01-01"}, False),
        ({}, False),
    ])
    def test_marker(self, frontmatter, expected):
        assert is_generated_note(frontmatter) is expected


class TestGeneratedNotes:
    GENERATED = "---\nsource: llm\ntype: synthesis\ngenerated: 2026-11-01\npeople: [Quillon Example]\n---\n**Robin** with Mirel\n"

    def test_creates_no_people_links_interactions_or_source_entities(self, world):
        path = world.note("Work/Pages/page.md", self.GENERATED)
        world.index(path)
        assert world.people.count() == 0
        assert world.interactions.added == {}
        assert world.source_entity(path, "Quillon Example") is None

    def test_still_searchable(self, world):
        path = world.note("Work/Pages/page.md", self.GENERATED)
        world.index(path)
        chunks, metadata = world.indexer.vector_store.update_document.call_args.args
        assert chunks
        assert metadata["file_path"] == str(path.resolve())
        assert "Quillon Example" in metadata["people"]
        assert world.indexer.bm25_index.add_document.called

    def test_same_note_without_the_marker_does_create(self, world):
        path = world.note("Work/Pages/page.md", "---\npeople: [Quillon Example]\n---\nbody\n")
        world.index(path)
        assert world.people.get_by_name("Quillon Example")
        assert world.interactions.added


class TestUnderscoreFolders:
    @pytest.mark.parametrize("rel,expected", [
        ("Work/_build/page.md", True),
        ("_scripts/page.md", True),
        ("Work/Fund/_build/deep/page.md", True),
        ("Work/_Registry.md", False),
        ("_Registry.md", False),
        ("Work/Fund/page.md", False),
        ("Work/not_underscore/page.md", False),
    ])
    def test_in_underscore_dir(self, rel, expected):
        assert in_underscore_dir(rel) is expected

    def test_index_file_skips_them(self, world):
        path = world.note("Work/_build/page.md", "---\npeople: [Quillon Example]\n---\nbody\n")
        world.index(path)
        assert not world.indexer.vector_store.update_document.called
        assert world.people.count() == 0

    def test_folder_above_the_vault_does_not_count(self, tmp_path):
        vault = tmp_path / "_private" / "vault"
        vault.mkdir(parents=True)
        indexer = IndexerService.__new__(IndexerService)
        indexer.vault_path = vault
        assert indexer._is_excluded_from_index(vault / "Work" / "note.md") is False

    def test_index_all_skips_them_and_keeps_underscore_files(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / "Work" / "_build").mkdir(parents=True)
        (vault / "Work" / "_build" / "out.md").write_text("working output")
        (vault / "Work" / "_Registry.md").write_text("a note")
        (vault / "Work" / "note.md").write_text("a note")
        monkeypatch.setattr(IndexerService, "INDEX_STATE_FILE", str(tmp_path / "state.json"))
        indexer = IndexerService.__new__(IndexerService)
        indexer.vault_path = vault
        indexer._tag_store = MagicMock()
        indexer._tag_store.get.return_value = None
        indexer._tag_store_failed = False
        indexed = []
        indexer.index_file = lambda path, **kwargs: indexed.append(Path(path).name) or set()
        indexer._retry_failed_summaries = lambda: 0

        indexer.index_all(skip_summaries=True)

        assert sorted(indexed) == ["_Registry.md", "note.md"]

    def test_index_all_prunes_entries_already_indexed_through_the_deleted_path(self, tmp_path, monkeypatch):
        import json

        vault = tmp_path / "vault"
        (vault / "Work" / "_build").mkdir(parents=True)
        excluded = vault / "Work" / "_build" / "out.md"
        excluded.write_text("working output")
        kept = vault / "Work" / "note.md"
        kept.write_text("a note")
        state = tmp_path / "state.json"
        state.write_text(json.dumps({str(excluded): 1.0, str(kept): kept.stat().st_mtime}))
        monkeypatch.setattr(IndexerService, "INDEX_STATE_FILE", str(state))
        indexer = IndexerService.__new__(IndexerService)
        indexer.vault_path = vault
        indexer._tag_store = MagicMock()
        indexer._tag_store.get.return_value = None
        indexer._tag_store_failed = False
        deleted = []
        indexer.delete_file = deleted.append
        indexer.index_file = MagicMock(return_value=set())
        indexer._retry_failed_summaries = lambda: 0

        indexer.index_all(skip_summaries=True)

        assert deleted == [str(excluded)]
        saved = json.loads(state.read_text())
        assert str(excluded) not in saved
        assert str(kept) in saved
        assert excluded.exists()


class TestAmbiguousMentions:
    def test_unknown_first_name_creates_no_person_and_stays_unlinked(self, world):
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        world.index(path)
        assert world.people.count() == 0
        assert world.interactions.added == {}
        entity = world.source_entity(path, "Quillon")
        assert entity.canonical_person_id is None
        assert entity.link_method is None

    def test_clear_context_links_with_the_mention_method(self, world):
        real = world.people.add(PersonEntity(canonical_name="Quillon Example"))
        path = world.note("Other/note.md", "Quillon Example opened. **Quillon** closed.\n")
        world.index(path)
        entity = world.source_entity(path, "Quillon")
        assert entity.canonical_person_id == real.id
        assert entity.link_method == "mention_context"
        assert world.interactions.added[str(path.resolve())] == real.id

    def test_weak_context_does_not_link_to_the_only_match(self, world):
        world.people.add(PersonEntity(canonical_name="Quillon Example"))
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        world.index(path)
        assert world.source_entity(path, "Quillon").canonical_person_id is None
        assert world.interactions.added == {}

    def test_bare_row_is_not_a_target(self, world):
        world.people.add(PersonEntity(canonical_name="Quillon"))
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        world.index(path)
        assert world.source_entity(path, "Quillon").canonical_person_id is None

    def test_full_name_mention_creates_and_feeds_the_note_interaction_first(self, world):
        path = world.note("Other/note.md", "---\npeople: [Mirel Example]\n---\n**Quillon** spoke.\n")
        world.index(path)
        created = world.people.get_by_name("Mirel Example")
        assert world.interactions.added[str(path.resolve())] == created.id
        assert world.source_entity(path, "Quillon").canonical_person_id is None

    def test_full_name_counts_as_a_co_mention(self, world):
        path = world.note("Other/note.md", "---\npeople: [Mirel Example]\n---\n**Quillon** spoke.\n")
        with patch.object(indexer_module, "build_note_context", wraps=indexer_module.build_note_context) as build:
            world.index(path)
        created = world.people.get_by_name("Mirel Example")
        assert set(build.call_args.kwargs["co_mentioned_ids"]) == {created.id}

    def test_undated_note_passes_no_date(self, world):
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        with patch.object(indexer_module, "build_note_context", wraps=indexer_module.build_note_context) as build:
            world.index(path)
        assert build.call_args.kwargs["note_date"] is None

    def test_dated_note_passes_its_date(self, world):
        path = world.note("Other/2024-06-15 note.md", "**Quillon** spoke.\n")
        with patch.object(indexer_module, "build_note_context", wraps=indexer_module.build_note_context) as build:
            world.index(path)
        assert build.call_args.kwargs["note_date"] == date(2024, 6, 15)

    def test_reindex_drops_a_link_the_context_no_longer_supports(self, world):
        real = world.people.add(PersonEntity(canonical_name="Quillon Example"))
        path = world.note("Other/note.md", "Quillon Example opened. **Quillon** closed.\n")
        world.index(path)
        assert world.source_entity(path, "Quillon").canonical_person_id == real.id
        path.write_text("**Quillon** closed.\n")
        world.indexer._mention_resolver = None
        world.index(path)
        assert world.source_entity(path, "Quillon").canonical_person_id is None

    def test_human_confirmed_link_survives_reindex_and_gets_the_interaction(self, world):
        chosen = world.people.add(PersonEntity(canonical_name="Quillon Example"))
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        world.sources.add(SourceEntity(
            source_type="vault", source_id=f"{path.resolve()}:Quillon", observed_name="Quillon",
            canonical_person_id=chosen.id, link_status=LINK_STATUS_CONFIRMED, link_method="manual",
        ), validate_person=False)
        world.index(path)
        entity = world.source_entity(path, "Quillon")
        assert entity.canonical_person_id == chosen.id
        assert entity.link_status == LINK_STATUS_CONFIRMED
        assert entity.link_method == "manual"
        assert world.interactions.added[str(path.resolve())] == chosen.id

    def test_human_rejected_mention_stays_rejected_and_unlinked(self, world):
        world.people.add(PersonEntity(canonical_name="Quillon Example"))
        path = world.note("Other/note.md", "Quillon Example opened. **Quillon** closed.\n")
        world.sources.add(SourceEntity(
            source_type="vault", source_id=f"{path.resolve()}:Quillon", observed_name="Quillon",
            link_status=LINK_STATUS_REJECTED,
        ), validate_person=False)
        world.index(path)
        entity = world.source_entity(path, "Quillon")
        assert entity.canonical_person_id is None
        assert entity.link_status == LINK_STATUS_REJECTED

    def test_resolver_that_cannot_be_built_leaves_existing_links(self, world):
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        with patch.object(indexer_module, "MentionResolver", side_effect=RuntimeError("boom")):
            world.index(path)
        assert world.source_entity(path, "Quillon") is None
        assert world.people.count() == 0

    def test_resolver_is_reused_then_rebuilt_once_aged(self, world, monkeypatch):
        path = world.note("Other/note.md", "**Quillon** spoke.\n")
        with patch.object(indexer_module, "MentionResolver", wraps=indexer_module.MentionResolver) as built:
            world.index(path)
            world.index(path)
            assert built.call_count == 1
            monkeypatch.setattr(indexer_module, "MENTION_RESOLVER_MAX_AGE_SECONDS", -1)
            world.index(path)
            assert built.call_count == 2


class TestNoteDates:
    def test_yaml_date_in_frontmatter_is_the_note_date(self):
        assert extract_note_date(Path("note.md"), {"date": date(2024, 6, 15)}) == "2024-06-15"

    def test_yaml_datetime_in_frontmatter_is_the_note_date(self):
        moment = datetime(2024, 6, 15, 8, tzinfo=timezone.utc)
        assert extract_note_date(Path("note.md"), {"created": moment}) == "2024-06-15"

    def test_filename_date_outranks_frontmatter(self):
        assert extract_note_date(Path("2023-01-02 note.md"), {"date": date(2024, 6, 15)}) == "2023-01-02"


class TestStoredNotePaths:
    VAULT = Path("/srv/Example Vault")

    @pytest.mark.parametrize("stored,expected", [
        ("/srv/Example Vault/Work/note.md", "Work/note.md"),
        ("/old/home/Example Vault/Work/note.md", "Work/note.md"),
        ("/tmp/elsewhere/note.md", None),
    ])
    def test_vault_relative_path(self, stored, expected):
        assert vault_relative_path(stored, self.VAULT) == expected


class TestRematchingLeavesMentionsAlone:
    def _unlinked(self, sources, source_type, source_id):
        entity = sources.add(SourceEntity(
            source_type=source_type, source_id=source_id, observed_name="Quillon",
        ), validate_person=False)
        conn = sources._get_connection()
        conn.execute("UPDATE source_entities SET match_attempt_count = 5 WHERE id = ?", (entity.id,))
        conn.commit()
        conn.close()

    def test_note_mentions_are_never_eligible(self, tmp_path):
        sources = SourceEntityStore(str(tmp_path / "crm.db"))
        self._unlinked(sources, "vault", "a")
        self._unlinked(sources, "granola", "b")
        self._unlinked(sources, "gmail", "c")
        eligible = sources.get_unlinked_for_rematching(max_capped_per_run=None)
        assert [e.source_type for e in eligible] == ["gmail"]
        assert sources.get_unlinked_for_rematching(source_type="vault", max_capped_per_run=None) == []

    def test_note_mentions_do_not_count_as_a_capped_backlog(self, tmp_path):
        sources = SourceEntityStore(str(tmp_path / "crm.db"))
        self._unlinked(sources, "vault", "a")
        self._unlinked(sources, "gmail", "c")
        assert sources.count_capped_backlog() == 1

    def test_relink_script_leaves_them_too(self, tmp_path):
        from scripts.relink_source_entities import count_unlinked_by_source, get_all_unlinked_entity_ids

        db = str(tmp_path / "crm.db")
        sources = SourceEntityStore(db)
        self._unlinked(sources, "vault", "a")
        self._unlinked(sources, "gmail", "c")
        assert len(get_all_unlinked_entity_ids(db_path=db)) == 1
        assert get_all_unlinked_entity_ids(source_type="vault", db_path=db) == []
        assert set(count_unlinked_by_source(db_path=db)) == {"gmail"}
