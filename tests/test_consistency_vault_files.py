"""
Vault interactions whose backing file is gone.

Moving a note in Obsidian strands its interactions at the path it moved from, and the next
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

    def test_ambiguous_but_all_candidates_covered_is_a_duplicate(self, vault_env):
        """
        The move target is unknown, but every candidate already carries its own
        interactions — so the history survives whichever one it was, and the
        stale row is a duplicate either way. Decidable without guessing.
        """
        vault, add, _ = vault_env
        (vault / "Work" / "Notes.md").write_text("one")
        (vault / "Personal" / "Notes.md").write_text("two")
        add(str(vault / "Notes.md"))
        add(str(vault / "Work" / "Notes.md"))
        add(str(vault / "Personal" / "Notes.md"))

        buckets = _classify_missing_vault_files()

        assert len(buckets["duplicate"]) == 1
        assert buckets["ambiguous"] == []

    def test_partial_candidate_coverage_stays_ambiguous(self, vault_env):
        """One uncovered candidate means the row might be its only history."""
        vault, add, _ = vault_env
        (vault / "Work" / "Notes.md").write_text("one")
        (vault / "Personal" / "Notes.md").write_text("two")
        add(str(vault / "Notes.md"))
        add(str(vault / "Work" / "Notes.md"))  # Personal/ left uncovered

        buckets = _classify_missing_vault_files()

        assert len(buckets["ambiguous"]) == 1
        assert buckets["duplicate"] == []

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


class TestPeopleCoverage:
    """A moved note is only a duplicate when the new path names the same people."""

    def test_new_path_linking_other_people_is_relinked_not_duplicate(self, vault_env):
        vault, add, _ = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"), person_id="p_old")
        add(str(vault / "Work" / "Standup.md"), person_id="p_new")

        buckets = _classify_missing_vault_files()

        assert buckets["duplicate"] == []
        assert buckets["relinked"] == [
            (str(vault / "Personal" / "Standup.md"), str(vault / "Work" / "Standup.md"))
        ]

    def test_relinked_rows_are_kept_and_flagged_for_review(self, vault_env):
        vault, add, source_ids = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"), person_id="p_old")
        add(str(vault / "Work" / "Standup.md"), person_id="p_new")

        result = _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert str(vault / "Personal" / "Standup.md") in source_ids()
        assert result["needs_review"] is True
        assert "relinked=1" in result["details"]

    def test_ambiguous_candidates_need_the_same_people_to_be_a_duplicate(self, vault_env):
        vault, add, _ = vault_env
        for folder in ("Work", "Personal"):
            (vault / folder / "Notes.md").write_text(folder)
            add(str(vault / folder / "Notes.md"), person_id="p_other")
        add(str(vault / "Notes.md"), person_id="p_old")

        buckets = _classify_missing_vault_files()

        assert buckets["duplicate"] == []
        assert len(buckets["ambiguous"]) == 1


class TestThresholdGatesDeletionOnly:
    def test_repoints_apply_above_the_threshold(self, vault_env):
        """Re-pointing keeps every row, so the mass-deletion guard does not apply."""
        vault, add, source_ids = vault_env
        for i in range(5):
            (vault / "Work" / f"Moved{i}.md").write_text("moved")
            add(str(vault / "Personal" / f"Moved{i}.md"))
        for i in range(5):
            add(str(vault / "Work" / f"Gone{i}.md"))

        result = _check_missing_vault_files(dry_run=False, fix_threshold=2)

        assert result["fixed"] == 5
        assert {str(vault / "Work" / f"Moved{i}.md") for i in range(5)} <= source_ids()
        assert {str(vault / "Work" / f"Gone{i}.md") for i in range(5)} <= source_ids()
        assert result["needs_review"] is True

    def test_clean_fix_needs_no_review(self, vault_env):
        vault, add, _ = vault_env
        (vault / "Work" / "Standup.md").write_text("moved here")
        add(str(vault / "Personal" / "Standup.md"))

        result = _check_missing_vault_files(dry_run=False, fix_threshold=100)

        assert result["needs_review"] is False


class TestSyncSummary:
    def test_data_issues_needing_review_turn_the_header_and_name_the_check(self):
        from unittest.mock import patch

        from scripts.run_all_syncs import _parse_sync_output, send_sync_summary_telegram

        output = (
            'CONSISTENCY_SUMMARY:{"total_issues": 12, "total_fixed": 2, "auto_fix_skipped": true, '
            '"review": ["moved or deleted vault notes: duplicate=0, repoint=2, relinked=10, gone=0, ambiguous=0"]}\n'
        )
        consistency = {"success": True, **_parse_sync_output(output)}
        result = {
            "failed": 0, "succeeded": 1, "sources_run": 1, "failed_sources": [], "duration_seconds": 1,
            "results": {"consistency_verify": consistency},
        }
        with patch("api.services.telegram.send_message", return_value=True) as send:
            send_sync_summary_telegram(result, trigger="test")
        message = send.call_args[0][0]
        assert message.startswith("⚠️")
        assert "relinked=10" in message
