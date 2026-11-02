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
from api.services.vault_listing import in_underscore_dir, no_people_reason, vault_relative_path
from config.settings import settings
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

    def get_by_source(self, source_type, source_id):
        if source_id not in self.added:
            return None
        return MagicMock(id=source_id, person_id=self.added[source_id])

    def reassign(self, interaction_id, person_id):
        self.added[interaction_id] = person_id
        return True

    def delete(self, interaction_id):
        return self.added.pop(interaction_id, None) is not None


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


class TestNoPeopleRule:
    BULK = ["Work/Knowledge Base"]

    @pytest.mark.parametrize("rel,frontmatter,expected", [
        ("Work/Knowledge Base/page.md", {}, "folder"),
        ("Work/Knowledge Base/deep/page.md", {"source": "gdrive"}, "folder"),
        ("work/knowledge base/page.md", {}, "folder"),
        ("Work/Knowledge Base 2/page.md", {}, None),
        ("Work/Knowledge Base.md", {}, None),
        ("Elsewhere/Work/Knowledge Base/page.md", {}, None),
        ("Work/note.md", {"people": False}, "people_false"),
        ("Work/note.md", {"people": "false"}, "people_false"),
        ("Work/note.md", {"people": "False"}, "people_false"),
        ("Work/note.md", {"people": "NO"}, "people_false"),
        ("Work/note.md", {"people": None}, None),
        ("Work/note.md", {"people": ""}, None),
        ("Work/note.md", {"people": "  "}, None),
        ("Work/note.md", {"people": 0}, None),
        ("Work/note.md", {"people": []}, None),
        ("Work/note.md", {"people": ["Quillon Example"]}, None),
        ("Work/note.md", {"generated": "2026-11-01"}, "generated"),
        ("Work/note.md", {"generated": date(2026, 11, 1)}, "generated"),
        ("Work/note.md", {"generated": True}, "generated"),
        ("Work/note.md", {"generated": False}, None),
        ("Work/note.md", {"generated": "false"}, None),
        ("Work/note.md", {"generated": ""}, None),
        ("Work/note.md", {"source": "llm"}, None),
        ("Work/note.md", {"source": "llm", "type": "synthesis"}, None),
        ("Work/note.md", {"type": "reference"}, None),
        ("Work/note.md", {"generated_at": "2026-01-01"}, None),
        ("Work/note.md", {}, None),
        (None, {"generated": "2026-11-01"}, "generated"),
        (None, {}, None),
    ])
    def test_rule(self, rel, frontmatter, expected):
        assert no_people_reason(rel, frontmatter, self.BULK) == expected

    def test_no_folders_configured_by_default(self):
        assert no_people_reason("Work/Knowledge Base/page.md", {}) is None

    def test_setting_parses_a_comma_separated_list(self, monkeypatch):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", " Work/One , Work/Two/,, ")
        assert settings.vault_no_people_paths == ["Work/One", "Work/Two/"]

    def test_setting_is_empty_by_default(self):
        from config.settings import Settings

        assert Settings.model_fields["vault_no_people_paths_raw"].default == ""


class TestBulkContent:
    CONTENT = "---\npeople: [Quillon Example]\n---\n**Robin** with Mirel\n"

    def _assert_nothing_recorded(self, world, path):
        assert world.people.count() == 0
        assert world.interactions.added == {}
        assert world.source_entity(path, "Quillon Example") is None
        assert world.source_entity(path, "Robin") is None

    def test_configured_folder_creates_nothing(self, world, monkeypatch):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "Work/Knowledge Base")
        path = world.note("Work/Knowledge Base/page.md", self.CONTENT)
        world.index(path)
        self._assert_nothing_recorded(world, path)

    def test_people_false_creates_nothing(self, world):
        path = world.note("Work/Pages/page.md", "---\npeople: false\n---\n**Robin** with Mirel\n")
        world.index(path)
        assert world.people.count() == 0
        assert world.interactions.added == {}
        assert world.source_entity(path, "Robin") is None

    def test_blank_people_property_still_produces_people(self, world):
        path = world.note("Work/Pages/page.md", "---\npeople:\n---\nQuillon Example opened. **Quillon** closed.\n")
        real = world.people.add(PersonEntity(canonical_name="Quillon Example"))
        world.index(path)
        assert world.source_entity(path, "Quillon").canonical_person_id == real.id
        assert world.interactions.added[str(path.resolve())] == real.id

    def test_generated_key_creates_nothing(self, world):
        path = world.note(
            "Work/Pages/page.md", "---\ngenerated: 2026-11-01\npeople: [Quillon Example]\n---\n**Robin**\n"
        )
        world.index(path)
        self._assert_nothing_recorded(world, path)

    def test_stays_searchable(self, world, monkeypatch):
        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "Work/Knowledge Base")
        path = world.note("Work/Knowledge Base/page.md", self.CONTENT)
        world.index(path)
        chunks, metadata = world.indexer.vector_store.update_document.call_args.args
        assert chunks
        assert metadata["file_path"] == str(path.resolve())
        assert "Quillon Example" in metadata["people"]
        assert world.indexer.bm25_index.add_document.called

    def test_source_llm_alone_keeps_producing_people(self, world):
        path = world.note(
            "Work/Daily/2026-11-01.md", "---\nsource: llm\ntype: synthesis\npeople: [Quillon Example]\n---\nbody\n"
        )
        world.index(path)
        created = world.people.get_by_name("Quillon Example")
        assert created
        assert world.interactions.added[str(path.resolve())] == created.id

    def test_ordinary_note_creates_as_usual(self, world):
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


class TestNoteInteractionFollowsTheNote:
    def _reindex(self, world, path, content):
        path.write_text(content)
        world.indexer._mention_resolver = None
        world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)
        return world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)

    def _setup(self, world):
        x = world.people.add(PersonEntity(canonical_name="Quillon Example"))
        y = world.people.add(PersonEntity(canonical_name="Quillon Sample"))
        path = world.note("Other/note.md", "Quillon Example opened. **Quillon** closed.\n")
        world.index(path)
        assert world.interactions.added[str(path.resolve())] == x.id
        return x, y, path

    def test_flip_moves_it_to_the_person_resolved_now(self, world):
        x, y, path = self._setup(world)

        self._reindex(world, path, "Quillon Sample opened. **Quillon** closed.\n")

        assert world.interactions.added[str(path.resolve())] == y.id
        assert world.source_entity(path, "Quillon").canonical_person_id == y.id
        assert str(path.resolve()) in world.people.get_by_id(y.id).related_notes

    def test_stats_refresh_reaches_both_sides_of_a_flip(self, world):
        x, y, path = self._setup(world)
        path.write_text("Quillon Sample opened. **Quillon** closed.\n")
        world.indexer._mention_resolver = None

        affected = world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)

        assert {x.id, y.id} <= affected

    def test_flip_to_no_one_removes_it(self, world):
        x, y, path = self._setup(world)

        self._reindex(world, path, "**Quillon** closed.\n")

        assert world.interactions.added == {}
        assert world.source_entity(path, "Quillon").canonical_person_id is None

    def test_note_that_stops_naming_anyone_removes_it(self, world):
        x, y, path = self._setup(world)

        self._reindex(world, path, "Nothing here.\n")

        assert world.interactions.added == {}

    def test_full_names_come_first(self, world):
        x, y, path = self._setup(world)
        path.write_text("---\npeople: [Mirel Example]\n---\nQuillon Sample opened. **Quillon** closed.\n")
        world.indexer._mention_resolver = None

        world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)

        assert world.interactions.added[str(path.resolve())] == world.people.get_by_name("Mirel Example").id

    def test_holder_who_still_resolves_keeps_it(self, world):
        x, y, path = self._setup(world)

        self._reindex(world, path, "---\npeople: [Mirel Example]\n---\nQuillon Example opened. **Quillon** closed.\n")

        assert world.interactions.added[str(path.resolve())] == x.id

    def test_holder_with_a_human_confirmed_link_keeps_it(self, world):
        x, y, path = self._setup(world)
        world.sources.add(SourceEntity(
            source_type="vault", source_id=f"{path.resolve()}:Old Name", observed_name="Old Name",
            canonical_person_id=x.id, link_status=LINK_STATUS_CONFIRMED,
        ), validate_person=False)

        self._reindex(world, path, "Quillon Sample opened.\n")

        assert world.interactions.added[str(path.resolve())] == x.id

    def _one_word_holder(self, world, name, content):
        holder = world.people.add(PersonEntity(canonical_name=name))
        path = world.note("Other/family.md", content)
        world.interactions.added[str(path.resolve())] = holder.id
        return holder, path

    def test_visible_one_word_holder_the_note_names_keeps_it(self, world):
        holder, path = self._one_word_holder(world, "Mirel", "**Mirel** called.\n")

        self._reindex(world, path, "**Mirel** called again.\n")

        assert world.interactions.added[str(path.resolve())] == holder.id

    def test_one_word_holder_the_note_stops_naming_gives_it_up(self, world):
        holder, path = self._one_word_holder(world, "Mirel", "**Mirel** called.\n")

        self._reindex(world, path, "Nothing here.\n")

        assert world.interactions.added == {}

    def test_hidden_one_word_holder_gives_it_up(self, world):
        holder, path = self._one_word_holder(world, "Mirel", "**Mirel** called.\n")
        holder.hidden = True
        world.people.update(holder)

        self._reindex(world, path, "**Mirel** called again.\n")

        assert world.interactions.added == {}

    def test_one_word_holder_with_a_phone_the_note_names_keeps_it(self, world):
        holder, path = self._one_word_holder(world, "Mirel", "**Mirel** called.\n")
        holder.phone_numbers = ["+12125550199"]
        world.people.update(holder)

        self._reindex(world, path, "**Mirel** called again.\n")

        assert world.interactions.added[str(path.resolve())] == holder.id

    def test_a_confirmed_link_in_another_note_does_not_count(self, world):
        x, y, path = self._setup(world)
        world.sources.add(SourceEntity(
            source_type="vault", source_id=f"{path.resolve()}.other.md:Old Name", observed_name="Old Name",
            canonical_person_id=x.id, link_status=LINK_STATUS_CONFIRMED,
        ), validate_person=False)

        self._reindex(world, path, "Nothing here.\n")

        assert world.interactions.added == {}

    def test_a_failed_mention_leaves_it_alone(self, world):
        x, y, path = self._setup(world)
        path.write_text("---\npeople: [Mirel Example]\n---\nbody\n")
        world.indexer._mention_resolver = None

        with patch.object(world.indexer._entity_resolver, "resolve", side_effect=RuntimeError("synthetic")):
            world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)

        assert world.interactions.added[str(path.resolve())] == x.id

    def test_an_unavailable_mention_resolver_leaves_it_alone(self, world):
        x, y, path = self._setup(world)
        path.write_text("**Quillon** closed.\n")
        world.indexer._mention_resolver = None

        with patch.object(indexer_module, "MentionResolver", side_effect=RuntimeError("boom")):
            world.indexer.index_file(str(path), skip_stats_refresh=True, skip_summaries=True)

        assert world.interactions.added[str(path.resolve())] == x.id

    def test_bulk_note_is_left_to_the_repair(self, world):
        x, y, path = self._setup(world)

        self._reindex(world, path, "---\ngenerated: 2026-01-01\n---\nNothing here.\n")

        assert world.interactions.added[str(path.resolve())] == x.id

    def test_store_lookup_only_reaches_this_notes_interaction(self, tmp_path):
        from api.services.interaction_store import Interaction, InteractionStore

        store = InteractionStore(str(tmp_path / "i.db"), strict=False)
        email = store.add(Interaction(
            id="e", person_id="x", timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source_type="gmail", title="t", source_id="/not/under/tmp/note.md",
        ))
        assert store.get_by_source("vault", email.source_id) is None
        assert store.reassign("e", "y") is True
        assert store.get_by_id("e").person_id == "y"
        assert store.reassign("missing", "y") is False


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
