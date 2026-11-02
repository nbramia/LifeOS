"""One-time repair of note mentions linked to one-word people."""
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api.services import interaction_store as interaction_store_module
from api.services import person_entity as person_entity_module
from api.services.interaction_store import Interaction, InteractionStore
from api.services.link_override import LinkOverride, LinkOverrideStore
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.person_facts import PersonFactStore
from api.services.relationship import Relationship, RelationshipStore
from api.services.source_entity import SourceEntity, SourceEntityStore
from config.employment_history import parse_employment_history
from scripts import repair_bare_person_mentions as repair

pytestmark = pytest.mark.unit

EMPLOYMENT = parse_employment_history({"employers": [{
    "name": "Sample Labs", "email_domains": ["samplelabs.example"], "start": "2023-01", "vault_folders": ["Work/Sample/"],
}]})

NO_PEOPLE_PATHS = ["Work/Knowledge Base"]


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.crm_db = tmp_path / "crm.db"
        self.interactions_db = tmp_path / "interactions.db"
        self.merged = tmp_path / "merged_person_ids.json"
        self.merged.write_text("{}")
        self.vault = tmp_path / "Example Vault"
        self.vault.mkdir()
        self.report = tmp_path / "private" / "report.json"
        self.people = PersonEntityStore(str(self.crm_db))
        self.sources = SourceEntityStore(str(self.crm_db))
        self.relationships = RelationshipStore(str(self.crm_db))
        self.overrides = LinkOverrideStore(self.crm_db)
        self.facts = PersonFactStore(str(self.crm_db))
        self.interactions = InteractionStore(str(self.interactions_db))

    def person(self, name, **fields):
        return self.people.add(PersonEntity(canonical_name=name, **fields))

    def note(self, rel, body="", frontmatter=""):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        head = f"---\n{frontmatter}\n---\n" if frontmatter else ""
        path.write_text(head + body)
        return str(path)

    def mention(self, note_path, person, name=None, source="vault", status="auto"):
        name = name or person.canonical_name
        return self.sources.add(SourceEntity(
            source_type=source,
            source_id=f"{note_path}:{name}",
            observed_name=name,
            canonical_person_id=person.id,
            link_status=status,
        ), validate_person=False)

    def note_interaction(self, note_path, person, source="vault"):
        """Inserted directly: the store refuses note paths under a temp directory."""
        interaction_id = f"n{self.interactions.count()}"
        conn = sqlite3.connect(self.interactions_db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id) "
            "VALUES (?, ?, ?, ?, 'note', ?)",
            (interaction_id, person.id, "2024-06-15T00:00:00+00:00", source, note_path),
        )
        conn.commit()
        conn.close()
        return Interaction(
            id=interaction_id, person_id=person.id, timestamp=datetime(2024, 6, 15, tzinfo=timezone.utc),
            source_type=source, title="note", source_id=note_path,
        )

    def contact(self, person, source="gmail", when=datetime(2024, 6, 15, tzinfo=timezone.utc)):
        return self.interactions.add(Interaction(
            id=f"c{self.interactions.count()}",
            person_id=person.id,
            timestamp=when,
            source_type=source,
            title="contact",
            source_id=f"{source}{self.interactions.count()}",
        ))

    def plan(self):
        return repair.build_plan(
            self.crm_db, self.interactions_db, self.vault, None, self.merged, EMPLOYMENT, NO_PEOPLE_PATHS
        )

    def execute(self, plan=None):
        return repair.execute_plan(plan or self.plan(), self.crm_db, self.interactions_db, self.merged)

    def se(self, entity_id):
        return self.sources.get_by_id(entity_id)

    def row(self, person):
        return self.people.get_by_id(person.id)

    def interaction_rows(self):
        conn = sqlite3.connect(self.interactions_db)
        try:
            return dict(conn.execute("SELECT id, person_id FROM interactions"))
        finally:
            conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """execute_plan points these process-wide handles at its databases."""
    monkeypatch.setattr(person_entity_module, "_entity_store", None)
    monkeypatch.setattr(PersonEntityStore, "MERGED_IDS_PATH", PersonEntityStore.MERGED_IDS_PATH)
    monkeypatch.setattr(
        interaction_store_module, "get_interaction_db_path", interaction_store_module.get_interaction_db_path
    )
    monkeypatch.setattr(repair.settings, "my_person_id", "")
    monkeypatch.setattr(repair.settings, "user_name", "")
    return World(tmp_path)


def strong_setup(world):
    """A bare row whose mention clearly means one real person."""
    bare = world.person("Robin")
    real = world.person("Robin Gamma", emails=["robin@samplelabs.example"])
    path = world.note("Work/Sample/2024-06-15 standup.md", "Robin Gamma spoke. **Robin** agreed.")
    return bare, real, path


class TestMentions:
    def test_strong_match_moves_link_and_interaction(self, world):
        bare, real, path = strong_setup(world)
        link = world.mention(path, bare)
        interaction = world.note_interaction(path, bare)

        world.execute()

        moved = world.se(link.id)
        assert moved.canonical_person_id == real.id
        assert moved.link_method == "mention_context"
        assert moved.link_confidence > 0
        assert world.interaction_rows()[interaction.id] == real.id
        target = world.row(real)
        assert path in target.related_notes
        assert "vault" in target.sources
        assert target.mention_count == 1

    def test_no_strong_match_removes_link_and_interaction(self, world):
        bare = world.person("Robin")
        world.person("Robin Gamma")
        path = world.note("Work/Sample/2024-06-15 standup.md", "**Robin** agreed.")
        link = world.mention(path, bare)
        interaction = world.note_interaction(path, bare)

        world.execute()

        kept = world.se(link.id)
        assert kept.canonical_person_id is None
        assert kept.link_method is None
        assert kept.linked_at is None
        assert interaction.id not in world.interaction_rows()

    def test_decisions_are_per_note_not_per_row(self, world):
        bare = world.person("Robin")
        gamma = world.person("Robin Gamma")
        delta = world.person("Robin Delta")
        gamma_note = world.note("Other/a.md", "robin gamma")
        delta_note = world.note("Other/b.md", "robin delta")
        neither = world.note("Other/c.md", "robin")
        links = {p: world.mention(p, bare) for p in (gamma_note, delta_note, neither)}

        world.execute()

        assert world.se(links[gamma_note].id).canonical_person_id == gamma.id
        assert world.se(links[delta_note].id).canonical_person_id == delta.id
        assert world.se(links[neither].id).canonical_person_id is None

    def test_interaction_on_someone_else_stays_put(self, world):
        bare, real, path = strong_setup(world)
        other = world.person("Quillon Example")
        world.mention(path, bare)
        interaction = world.note_interaction(path, other)

        world.execute()

        assert world.interaction_rows()[interaction.id] == other.id

    def test_interaction_with_no_source_entity_is_still_re_resolved(self, world):
        bare, real, path = strong_setup(world)
        interaction = world.note_interaction(path, bare)

        world.execute()

        assert world.interaction_rows()[interaction.id] == real.id

    def test_note_stored_under_an_earlier_vault_location(self, world):
        bare, real, _ = strong_setup(world)
        old = f"/Elsewhere/older/{world.vault.name}/Work/Sample/2024-06-15 standup.md"
        link = world.mention(old, bare)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id

    def test_note_no_longer_on_disk_unlinks(self, world):
        bare = world.person("Robin")
        world.person("Robin Gamma")
        link = world.mention(str(world.vault / "gone.md"), bare)
        outside = world.mention("/tmp/somewhere/else.md", bare)

        plan = world.plan()
        world.execute(plan)

        assert plan.counts["unlinked:note_not_on_disk"] == 2
        assert world.se(link.id).canonical_person_id is None
        assert world.se(outside.id).canonical_person_id is None

    def test_real_people_mentions_are_left_alone(self, world):
        _, real, path = strong_setup(world)
        link = world.mention(path, real, name="Robin")
        interaction = world.note_interaction(path, real)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id
        assert world.interaction_rows()[interaction.id] == real.id

    def test_a_row_merged_away_is_not_bare(self, world):
        merged_row = world.person("Robin")
        link = world.mention(world.note("Other/a.md", "robin"), merged_row)
        world.merged.write_text(json.dumps({merged_row.id: "elsewhere"}))

        plan = world.plan()

        assert merged_row.id not in plan.hide_ids
        assert link.id not in plan.unlink_ids


class TestBulkContent:
    @pytest.mark.parametrize("rel,frontmatter", [
        ("Work/Pages/page.md", "generated: 2026-01-01"),
        ("Work/Pages/page.md", "people: false"),
        ("Work/Knowledge Base/page.md", ""),
        ("Work/Knowledge Base/deep/page.md", "source: gdrive"),
    ])
    def test_removed_for_real_people_and_bare_rows(self, world, rel, frontmatter):
        real = world.person("Robin Gamma")
        bare = world.person("Quillon")
        path = world.note(rel, "Robin Gamma", frontmatter)
        real_link = world.mention(path, real)
        bare_link = world.mention(path, bare)
        interaction = world.note_interaction(path, real)

        world.execute()

        assert world.se(real_link.id) is None
        assert world.se(bare_link.id) is None
        assert interaction.id not in world.interaction_rows()
        assert world.row(real).mention_count == 0

    def test_source_llm_alone_is_not_bulk_content(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/Daily/page.md", "Robin Gamma", "source: llm\ntype: synthesis")
        link = world.mention(path, real)
        interaction = world.note_interaction(path, real)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id
        assert world.interaction_rows()[interaction.id] == real.id

    def test_mention_on_a_source_llm_note_is_re_resolved(self, world):
        bare = world.person("Robin")
        real = world.person("Robin Gamma")
        path = world.note("Work/Daily/page.md", "Robin Gamma", "source: llm")
        link = world.mention(path, bare)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id

    def test_bulk_folder_note_no_longer_on_disk_is_still_bulk(self, world):
        real = world.person("Robin Gamma")
        link = world.mention(str(world.vault / "Work/Knowledge Base/gone.md"), real)

        world.execute()

        assert world.se(link.id) is None

    def test_sibling_of_a_bulk_folder_is_untouched(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/Knowledge Base 2/page.md", "Robin Gamma")
        link = world.mention(path, real)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id

    def test_no_folders_configured_leaves_them_alone(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/Knowledge Base/page.md", "Robin Gamma")
        link = world.mention(path, real)

        plan = repair.build_plan(world.crm_db, world.interactions_db, world.vault, None, world.merged, EMPLOYMENT, [])

        assert link.id not in plan.delete_link_ids

    def test_setting_supplies_the_folders_by_default(self, world, monkeypatch):
        monkeypatch.setattr(repair.settings, "vault_no_people_paths_raw", "Work/Knowledge Base")
        real = world.person("Robin Gamma")
        link = world.mention(world.note("Work/Knowledge Base/page.md", "Robin Gamma"), real)

        plan = repair.build_plan(world.crm_db, world.interactions_db, world.vault, None, world.merged, EMPLOYMENT)

        assert link.id in plan.delete_link_ids

    def test_counts_say_why(self, world):
        real = world.person("Robin Gamma")
        world.mention(world.note("Work/Knowledge Base/a.md", "x"), real)
        world.mention(world.note("Work/Pages/b.md", "x", "people: false"), real)
        world.mention(world.note("Work/Pages/c.md", "x", "generated: 2026-01-01"), real)
        world.mention(world.note("Work/_build/d.md", "x"), real)

        counts = world.plan().counts

        assert counts["no_people_notes:folder"] == 1
        assert counts["no_people_notes:people_false"] == 1
        assert counts["no_people_notes:generated"] == 1
        assert counts["no_people_notes:underscore_dir"] == 1

    def test_underscore_folder_counts_as_excluded(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/_build/out.md", "Robin Gamma")
        link = world.mention(path, real)
        interaction = world.note_interaction(path, real)

        world.execute()

        assert world.se(link.id) is None
        assert interaction.id not in world.interaction_rows()

    def test_human_confirmed_link_on_a_generated_note_is_kept(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/Pages/page.md", "x", "generated: 2026-01-01")
        link = world.mention(path, real, status="confirmed")

        plan = world.plan()
        world.execute(plan)

        assert world.se(link.id).canonical_person_id == real.id
        assert plan.counts["no_people_note_links_kept_human"] == 1

    def test_protected_row_keeps_links_from_generated_notes(self, world):
        bare = world.person("Robin", tags=["kept"])
        path = world.note("Work/Pages/page.md", "x", "generated: 2026-01-01")
        link = world.mention(path, bare)
        interaction = world.note_interaction(path, bare)

        plan = world.plan()
        world.execute(plan)

        assert world.se(link.id).canonical_person_id == bare.id
        assert interaction.id in world.interaction_rows()
        assert plan.counts["no_people_note_links_kept_protected"] == 1
        assert plan.counts["no_people_note_interactions_kept_protected"] == 1

    def test_ordinary_note_is_not_treated_as_generated(self, world):
        real = world.person("Robin Gamma")
        path = world.note("Work/Pages/page.md", "x", "source: granola")
        link = world.mention(path, real)

        world.execute()

        assert world.se(link.id).canonical_person_id == real.id


class TestHiding:
    def test_empty_row_is_hidden_not_deleted_with_a_reason(self, world):
        bare = world.person("Robin")
        world.mention(world.note("Other/a.md", "robin"), bare)

        world.execute()

        row = world.row(bare)
        assert row.hidden is True
        assert row.hidden_reason == repair.HIDDEN_REASON
        assert row.mention_count == 0

    def test_row_with_nothing_at_all_is_hidden(self, world):
        bare = world.person("Robin")

        world.execute()

        assert world.row(bare).hidden is True

    def test_hidden_row_has_no_interactions_left_to_lose(self, world):
        bare, _, path = strong_setup(world)
        world.note_interaction(path, bare)

        world.execute()

        assert bare.id not in world.interaction_rows().values()
        assert world.row(bare).hidden is True

    def test_hidden_one_word_row_no_longer_answers_to_its_name(self, world):
        from api.services.entity_resolver import EntityResolver

        bare, real, _ = strong_setup(world)
        world.execute()

        store = person_entity_module.get_person_entity_store()
        assert EntityResolver(store).match_name("Robin").result.entity.id == real.id

    def test_real_people_are_never_hidden(self, world):
        real = world.person("Robin Gamma")
        one_word_with_phone = world.person("Quillon", phone_numbers=["+12125550100"])
        one_word_with_email = world.person("Tamsin", emails=["t@example.com"])

        plan = world.plan()

        assert not {real.id, one_word_with_phone.id, one_word_with_email.id} & set(plan.hide_ids)


class TestProtectedRows:
    def _assert_untouched(self, world, bare, expected_reason):
        path = world.note("Other/a.md", "robin gamma")
        world.person("Robin Gamma")
        link = world.mention(path, bare)
        interaction = world.note_interaction(path, bare)

        plan = world.plan()
        assert expected_reason in plan.protected[bare.id]
        assert bare.id not in plan.hide_ids
        world.execute(plan)

        row = world.row(bare)
        assert row.hidden is False
        assert world.se(link.id).canonical_person_id == bare.id
        assert world.interaction_rows()[interaction.id] == bare.id

    def test_link_override_preferred(self, world):
        bare = world.person("Robin")
        world.overrides.add(LinkOverride(
            id="o", name_pattern="Robin", source_type=None, context_pattern=None,
            preferred_person_id=bare.id, rejected_person_id=None, reason=None,
        ))
        self._assert_untouched(world, bare, "link_override")

    def test_link_override_rejected(self, world):
        bare = world.person("Robin")
        world.overrides.add(LinkOverride(
            id="o", name_pattern="Robin", source_type=None, context_pattern=None,
            preferred_person_id="someone", rejected_person_id=bare.id, reason=None,
        ))
        self._assert_untouched(world, bare, "link_override")

    def test_confirmed_link(self, world):
        bare = world.person("Robin")
        world.mention(world.note("Other/b.md", "x"), bare, name="Robin", status="confirmed")
        self._assert_untouched(world, bare, "link_status:confirmed")

    def test_person_fact(self, world):
        bare = world.person("Robin")
        conn = sqlite3.connect(world.crm_db)
        conn.execute(
            "INSERT INTO person_facts (id, person_id, category, key, value, confidence) "
            "VALUES ('f', ?, 'c', 'k', 'v', 1.0)", (bare.id,),
        )
        conn.commit()
        conn.close()
        self._assert_untouched(world, bare, "person_facts")

    def test_photos(self, world):
        self._assert_untouched(world, world.person("Robin", photo_count=2), "photos")

    @pytest.mark.parametrize("source", ["phone", "imessage", "whatsapp", "gmail", "calendar", "slack", "photos"])
    def test_outside_interaction_evidence(self, world, source):
        bare = world.person("Robin")
        world.contact(bare, source=source)
        self._assert_untouched(world, bare, f"interaction:{source}")

    @pytest.mark.parametrize("source", ["gmail", "slack", "contacts", "photos"])
    def test_outside_source_entity_evidence(self, world, source):
        bare = world.person("Robin")
        world.sources.add(SourceEntity(
            source_type=source, source_id=f"{source}-1", canonical_person_id=bare.id,
        ), validate_person=False)
        self._assert_untouched(world, bare, f"source_entity:{source}")

    def test_strength_override(self, world, monkeypatch):
        bare = world.person("Robin")
        monkeypatch.setattr(repair, "STRENGTH_OVERRIDES_BY_ID", {bare.id: 90.0})
        self._assert_untouched(world, bare, "strength_override")

    def test_circle_override(self, world, monkeypatch):
        bare = world.person("Robin")
        monkeypatch.setattr(repair, "CIRCLE_OVERRIDES_BY_ID", {bare.id: 0})
        self._assert_untouched(world, bare, "circle_override")

    def test_tags(self, world):
        self._assert_untouched(world, world.person("Robin", tags=["friend"]), "tags")

    def test_family_category(self, world):
        self._assert_untouched(world, world.person("Robin", category="family"), "family_category")

    @pytest.mark.parametrize("field,value", [
        ("notes", "met at a conference"), ("birthday", "01-02"), ("company", "Sample Labs"),
        ("position", "Lead"), ("linkedin_url", "https://example.com/in/robin"), ("aliases", ["Rob"]),
    ])
    def test_human_fields(self, world, field, value):
        self._assert_untouched(world, world.person("Robin", **{field: value}), field)

    def test_asserted_relationship(self, world):
        bare = world.person("Robin")
        other = world.person("Quillon Example")
        low, high = sorted((bare.id, other.id))
        world.relationships.add(Relationship(person_a_id=low, person_b_id=high, relationship_type="family"))
        self._assert_untouched(world, bare, "relationship:family")

    def test_inferred_relationship_does_not_protect(self, world):
        bare = world.person("Robin")
        other = world.person("Quillon Example")
        low, high = sorted((bare.id, other.id))
        world.relationships.add(Relationship(person_a_id=low, person_b_id=high, relationship_type="inferred"))

        assert bare.id not in world.plan().protected

    def test_review_marked_kept(self, world):
        bare = world.person("Robin")
        conn = sqlite3.connect(world.crm_db)
        conn.execute(
            "CREATE TABLE entity_review_queue (person_a_id TEXT, person_b_id TEXT, status TEXT)"
        )
        conn.execute("INSERT INTO entity_review_queue VALUES (?, 'x', 'kept')", (bare.id,))
        conn.commit()
        conn.close()
        self._assert_untouched(world, bare, "review_kept")

    def test_manual_source(self, world):
        self._assert_untouched(world, world.person("Robin", sources=["manual_fix"]), "manual_source")

    def test_note_only_sources_do_not_protect(self, world):
        bare = world.person("Robin", sources=["vault", "granola"])
        assert bare.id not in world.plan().protected


class TestSafety:
    def _hashes(self, world):
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (world.crm_db, world.interactions_db)
        }

    def _table_rows(self, world):
        out = {}
        for db in (world.crm_db, world.interactions_db):
            conn = sqlite3.connect(db)
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                out[table] = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            conn.close()
        return out

    def test_dry_run_writes_nothing_but_the_report(self, world, capsys):
        bare, real, path = strong_setup(world)
        world.mention(path, bare)
        world.note_interaction(path, bare)
        for db in (world.crm_db, world.interactions_db):
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        before_rows = self._table_rows(world)
        before = self._hashes(world)
        files_before = sorted(p.name for p in world.root.iterdir())

        code = repair.main([
            "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
            "--vault", str(world.vault), "--merged-ids", str(world.merged), "--report", str(world.report),
        ])

        assert code == 0
        assert self._table_rows(world) == before_rows
        assert self._hashes(world) == before
        assert [n for n in sorted(p.name for p in world.root.iterdir()) if n != "private"] == files_before
        assert world.report.exists()

    def test_output_is_aggregate_only(self, world, capsys):
        bare, real, path = strong_setup(world)
        world.mention(path, bare)
        repair.main([
            "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
            "--vault", str(world.vault), "--merged-ids", str(world.merged), "--report", str(world.report),
        ])
        output = capsys.readouterr().out
        assert "Robin" not in output
        assert "standup" not in output

    def test_report_is_private_and_holds_decisions(self, world):
        bare, real, path = strong_setup(world)
        world.mention(path, bare)
        repair.main([
            "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
            "--vault", str(world.vault), "--merged-ids", str(world.merged), "--report", str(world.report),
        ])
        assert stat.S_IMODE(os.stat(world.report).st_mode) == 0o600
        report = json.loads(world.report.read_text())
        sample = report["moved_sample"][0]
        assert sample["chosen_person"] == "Robin Gamma"
        assert sample["signals"]["full_name_in_note"] > 0
        assert report["hidden_rows"] == ["Robin"]

    def test_report_refuses_to_live_in_the_repository(self, world, monkeypatch):
        monkeypatch.setattr(repair, "REPO_ROOT", world.root)
        with pytest.raises(SystemExit):
            repair.write_report(repair.Plan(), world.root / "report.json", {})

    def test_execute_backs_up_first_and_privately(self, world):
        bare, real, path = strong_setup(world)
        link = world.mention(path, bare)

        code = repair.main([
            "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
            "--vault", str(world.vault), "--merged-ids", str(world.merged), "--report", str(world.report),
            "--execute",
        ])

        assert code == 0
        backups = sorted(world.root.glob("*.bak-mention-repair-*"))
        assert len(backups) == 2
        assert all(stat.S_IMODE(os.stat(b).st_mode) == 0o600 for b in backups)
        crm_backup = next(b for b in backups if b.name.startswith("crm.db"))
        conn = sqlite3.connect(crm_backup)
        held = conn.execute("SELECT canonical_person_id FROM source_entities WHERE id = ?", (link.id,)).fetchone()[0]
        conn.close()
        assert held == bare.id
        assert world.se(link.id).canonical_person_id == real.id

    def test_rerun_finds_nothing_to_do(self, world):
        bare, real, path = strong_setup(world)
        other = world.person("Quillon")
        protected = world.person("Tamsin", tags=["kept"])
        world.mention(path, bare)
        world.mention(world.note("Other/b.md", "quillon"), other)
        world.mention(world.note("Other/g.md", "x", "generated: 2026-01-01"), real)
        world.note_interaction(path, bare)
        world.execute()
        after_first = self._table_rows(world)

        second = world.plan()

        assert second.moves == []
        assert second.unlink_ids == []
        assert second.delete_link_ids == []
        assert second.delete_interaction_ids == []
        assert second.hide_ids == []
        assert list(second.protected) == [protected.id]
        world.execute(second)
        assert self._table_rows(world) == after_first

    def test_last_seen_follows_the_interactions_that_remain(self, world):
        bare, real, path = strong_setup(world)
        generated_only = world.person("Quillon Example")
        contacted = datetime(2024, 1, 1, tzinfo=timezone.utc)
        world.contact(generated_only, when=contacted)
        world.note_interaction(world.note("Other/g.md", "x", "generated: 2026-01-01"), generated_only)
        world.note_interaction(path, bare)
        conn = sqlite3.connect(world.crm_db)
        conn.execute(
            "UPDATE person_entities SET last_seen = ? WHERE id = ?",
            ("2024-06-15T00:00:00+00:00", generated_only.id),
        )
        conn.commit()
        conn.close()

        world.execute()

        assert world.row(generated_only).last_seen == contacted
        assert world.row(real).last_seen == datetime(2024, 6, 15, tzinfo=timezone.utc)
        assert world.row(bare).last_seen is None

    def test_stats_recompute_reaches_targets_and_sources(self, world):
        bare, real, path = strong_setup(world)
        world.mention(path, bare)
        world.note_interaction(path, bare)

        world.execute()

        assert world.row(real).mention_count == 1
        assert world.row(real).source_entity_count == 1
        assert world.row(bare).mention_count == 0
        assert world.row(bare).source_entity_count == 0
