"""
Vault interactions whose file no longer exists.

Moving a note in Obsidian strands its interactions at the old path, and the next
reindex creates new ones at the new path. A blanket delete would be wrong: on
the corpus that prompted this, 56 of 78 dangling rows were *moved* notes, not
deleted ones, and 4 of those had no replacement rows at all — deleting them
would have destroyed the only record of the interaction.
"""
import sqlite3

import pytest

pytestmark = pytest.mark.unit

from api.services.interaction_store import InteractionStore  # noqa: E402
from scripts.sync_consistency_verify import (  # noqa: E402
    _check_missing_vault_files,
    _classify_missing_vault_files,
)


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """A temp vault plus an interactions DB wired into the checker."""
    vault = tmp_path / "vault"
    (vault / "Work").mkdir(parents=True)
    (vault / "Personal").mkdir(parents=True)

    db_path = str(tmp_path / "interactions.db")
    InteractionStore(db_path=db_path)

    monkeypatch.setattr(
        "api.services.interaction_store.get_interaction_db_path", lambda: db_path
    )
    from config.settings import settings
    monkeypatch.setattr(settings, "vault_path", vault, raising=False)

    def add_interaction(source_id, person_id="p1"):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, "
            "title, source_id) VALUES (?, ?, '2026-01-01T00:00:00+00:00', "
            "'vault', 'Note', ?)",
            (f"i_{source_id}_{person_id}", person_id, source_id),
        )
        conn.commit()
        conn.close()

    def source_ids():
        conn = sqlite3.connect(db_path)
        rows = {r[0] for r in conn.execute("SELECT source_id FROM interactions")}
        conn.close()
        return rows

    return vault, add_interaction, source_ids


class TestClassification:
    def test_moved_note_with_replacement_rows_is_a_duplicate(self, vault_env):
        vault, add, _ = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"))   # stale, file gone
        add(str(vault / "Work" / "Standup.md"))       # already reindexed

        buckets = _classify_missing_vault_files()

        assert len(buckets["duplicate"]) == 1
        assert buckets["repoint"] == []

    def test_moved_note_without_replacement_rows_is_repointable(self, vault_env):
        """Deleting this one would lose the interaction entirely."""
        vault, add, _ = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"))

        buckets = _classify_missing_vault_files()

        assert buckets["repoint"] == [
            (str(vault / "Personal" / "Standup.md"), str(vault / "Work" / "Standup.md"))
        ]

    def test_deleted_note_is_gone(self, vault_env):
        vault, add, _ = vault_env
        add(str(vault / "Work" / "Vanished.md"))

        buckets = _classify_missing_vault_files()

        assert len(buckets["gone"]) == 1

    def test_duplicate_basename_is_ambiguous(self, vault_env):
        """Two candidates means the move target is a guess — never guess."""
        vault, add, _ = vault_env
        (vault / "Work" / "Notes.md").write_text("one")
        (vault / "Personal" / "Notes.md").write_text("two")
        add(str(vault / "Notes.md"))

        buckets = _classify_missing_vault_files()

        assert len(buckets["ambiguous"]) == 1
        assert buckets["duplicate"] == [] and buckets["repoint"] == []

    def test_existing_files_are_not_flagged(self, vault_env):
        vault, add, _ = vault_env
        (vault / "Work" / "Present.md").write_text("here")
        add(str(vault / "Work" / "Present.md"))

        buckets = _classify_missing_vault_files()

        assert all(v == [] for v in buckets.values())


class TestSafety:
    def test_absent_vault_reports_nothing(self, vault_env, monkeypatch, tmp_path):
        """
        If the vault isn't mounted every path looks missing. Reporting them
        would propose deleting the entire interaction history.
        """
        vault, add, _ = vault_env
        add(str(vault / "Work" / "Anything.md"))
        from config.settings import settings
        monkeypatch.setattr(settings, "vault_path", tmp_path / "not-mounted", raising=False)

        buckets = _classify_missing_vault_files()

        assert all(v == [] for v in buckets.values())

    def test_above_threshold_reports_but_does_not_fix(self, vault_env):
        """Matches the script's existing convention for destructive fixes."""
        vault, add, source_ids = vault_env
        for i in range(5):
            add(str(vault / "Work" / f"Gone{i}.md"))

        result = _check_missing_vault_files(dry_run=False, fix_threshold=2)

        assert result["count"] == 5
        assert result["fixed"] == 0
        assert len(source_ids()) == 5

    def test_dry_run_changes_nothing(self, vault_env):
        vault, add, source_ids = vault_env
        add(str(vault / "Work" / "Gone.md"))

        result = _check_missing_vault_files(dry_run=True, fix_threshold=100)

        assert result["count"] == 1 and result["fixed"] == 0
        assert len(source_ids()) == 1


class TestFixing:
    def test_repoints_moved_note_instead_of_deleting_it(self, vault_env):
        vault, add, source_ids = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"))

        _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert source_ids() == {str(vault / "Work" / "Standup.md")}

    def test_deletes_stale_duplicates(self, vault_env):
        vault, add, source_ids = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"))
        add(str(vault / "Work" / "Standup.md"))

        _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert source_ids() == {str(vault / "Work" / "Standup.md")}

    def test_deletes_interactions_for_vanished_notes(self, vault_env):
        vault, add, source_ids = vault_env
        add(str(vault / "Work" / "Vanished.md"))

        _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert source_ids() == set()

    def test_ambiguous_rows_are_left_alone(self, vault_env):
        vault, add, source_ids = vault_env
        (vault / "Work" / "Notes.md").write_text("one")
        (vault / "Personal" / "Notes.md").write_text("two")
        add(str(vault / "Notes.md"))

        _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert source_ids() == {str(vault / "Notes.md")}
