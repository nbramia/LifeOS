"""A time budget on index_all: stop early, lose nothing, carry on next run."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services.indexer import IndexerService
from scripts import sync_vault_reindex as reindex_script

pytestmark = pytest.mark.unit


class Harness:
    """An indexer over a real vault folder and state file. Indexing a file ticks a
    fake clock forward, so a budget is measured in files."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.state_path = tmp_path / "state.json"
        monkeypatch.setattr(IndexerService, "INDEX_STATE_FILE", str(self.state_path))
        self.now = 0.0
        self.indexed: list[str] = []
        self.deleted: list[str] = []
        self.fail_on: set[str] = set()
        self.affected = {}

    def make(self):
        indexer = IndexerService.__new__(IndexerService)
        indexer.vault_path = self.vault
        indexer._tag_store = MagicMock()
        indexer._tag_store.get.return_value = None
        indexer._tag_store_failed = False
        indexer._clock = lambda: self.now
        indexer.index_file = self._index_file
        indexer.delete_file = self._delete_file
        return indexer

    def _index_file(self, path, **kwargs):
        name = Path(path).name
        self.now += 1.0
        if name in self.fail_on:
            raise RuntimeError("synthetic failure")
        self.indexed.append(name)
        return self.affected.get(name, set())

    def _delete_file(self, path):
        self.now += 1.0
        self.deleted.append(path)

    def notes(self, count):
        for i in range(count):
            (self.vault / f"note{i}.md").write_text("body")

    def state(self):
        return json.loads(self.state_path.read_text())


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


class TestStoppingEarly:
    def test_stops_when_the_budget_is_spent_and_saves_its_place(self, harness):
        harness.notes(5)
        indexer = harness.make()

        count = indexer.index_all(time_budget_seconds=2.0)

        assert count == 2
        assert len(harness.indexed) == 2
        assert indexer.last_run == {"files_pruned": 0, "files_remaining": 3, "files_skipped_large": 0, "stopped_early": True, "files_failed": 0, "failed_paths": []}
        saved = [k for k in harness.state() if k.endswith(".md")]
        assert len(saved) == 2
        assert {Path(k).name for k in saved} == set(harness.indexed)

    def test_next_run_continues_with_nothing_lost_or_repeated(self, harness):
        harness.notes(5)
        harness.make().index_all(time_budget_seconds=2.0)

        indexer = harness.make()
        harness.now = 0.0
        indexer.index_all(time_budget_seconds=2.0)
        harness.now = 0.0
        last = harness.make()
        count = last.index_all(time_budget_seconds=2.0)

        assert sorted(harness.indexed) == sorted(f"note{i}.md" for i in range(5))
        assert len(harness.indexed) == 5
        assert count == 1
        assert last.last_run["files_remaining"] == 0
        assert last.last_run["stopped_early"] is False

    def test_a_finished_vault_has_nothing_to_redo(self, harness):
        harness.notes(2)
        harness.make().index_all(time_budget_seconds=10.0)
        harness.indexed.clear()

        assert harness.make().index_all(time_budget_seconds=10.0) == 0
        assert harness.indexed == []

    def test_a_failed_file_is_retried_next_run_and_not_counted_as_remaining(self, harness):
        harness.notes(2)
        harness.fail_on = {"note0.md", "note1.md"}
        indexer = harness.make()
        indexer.index_all(time_budget_seconds=100.0)
        assert indexer.last_run["files_remaining"] == 0
        assert indexer.last_run["stopped_early"] is False

        harness.fail_on = set()
        assert harness.make().index_all(time_budget_seconds=100.0) == 2

    def test_a_failed_note_is_reported_apart_from_indexed_and_remaining(self, harness, caplog):
        harness.notes(3)
        harness.fail_on = {"note1.md"}
        indexer = harness.make()

        with caplog.at_level("ERROR"):
            count = indexer.index_all(time_budget_seconds=100.0)

        assert count == 2
        run = indexer.last_run
        assert run["files_failed"] == 1
        assert [Path(p).name for p in run["failed_paths"]] == ["note1.md"]
        assert run["files_remaining"] == 0
        assert {Path(k).name for k in harness.state() if k.endswith(".md")} == {"note0.md", "note2.md"}
        assert any("note1.md" in r.getMessage() and "synthetic failure" in r.getMessage() for r in caplog.records)

    def test_failed_paths_are_bounded_but_the_count_is_not(self, harness):
        harness.notes(25)
        harness.fail_on = {f"note{i}.md" for i in range(25)}
        indexer = harness.make()

        indexer.index_all(time_budget_seconds=100.0)

        assert indexer.last_run["files_failed"] == 25
        assert len(indexer.last_run["failed_paths"]) == 20

    def test_a_clean_run_reports_no_failures(self, harness):
        harness.notes(2)
        indexer = harness.make()
        indexer.index_all(time_budget_seconds=100.0)
        assert indexer.last_run["files_failed"] == 0
        assert indexer.last_run["failed_paths"] == []

    def test_stats_refresh_for_people_touched_before_the_stop(self, harness):
        harness.notes(4)
        harness.affected = {f"note{i}.md": {f"person{i}"} for i in range(4)}
        with patch("api.services.person_stats.refresh_person_stats") as refresh:
            harness.make().index_all(time_budget_seconds=2.0)

        assert refresh.call_count == 1
        assert len(refresh.call_args.args[0]) == 2

    def test_stopped_run_logs_what_remains_without_a_warning(self, harness, caplog):
        harness.notes(3)
        with caplog.at_level("INFO"):
            harness.make().index_all(time_budget_seconds=1.0)
        assert "2 files remain for the next run" in caplog.text
        assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


class TestPruningUnderABudget:
    def _with_deleted(self, harness, count):
        state = {str(harness.vault / f"gone{i}.md"): 1.0 for i in range(count)}
        harness.state_path.write_text(json.dumps(state))

    def test_pending_removals_stay_in_state_for_the_next_run(self, harness):
        self._with_deleted(harness, 5)
        indexer = harness.make()

        indexer.index_all(time_budget_seconds=2.0)

        assert len(harness.deleted) == 2
        assert indexer.last_run == {"files_pruned": 2, "files_remaining": 3, "files_skipped_large": 0, "stopped_early": True, "files_failed": 0, "failed_paths": []}
        assert len(harness.state()) - ("__tag_phrase_keys__" in harness.state()) == 3

        harness.now = 0.0
        harness.make().index_all(time_budget_seconds=100.0)
        assert len(harness.deleted) == 5
        assert len(set(harness.deleted)) == 5
        assert [k for k in harness.state() if k.endswith(".md")] == []

    def test_files_on_disk_are_never_pruned_by_a_stop(self, harness):
        harness.notes(4)
        harness.make().index_all(time_budget_seconds=1.0)
        harness.now = 0.0
        harness.make().index_all(time_budget_seconds=1.0)
        assert harness.deleted == []

    def test_budget_spent_pruning_leaves_indexing_for_the_next_run(self, harness):
        self._with_deleted(harness, 2)
        harness.notes(2)
        indexer = harness.make()

        count = indexer.index_all(time_budget_seconds=2.0)

        assert count == 0
        assert indexer.last_run["files_remaining"] == 2
        assert indexer.last_run["files_pruned"] == 2

    def test_a_state_entry_is_only_dropped_when_its_removal_succeeds(self, harness):
        self._with_deleted(harness, 1)
        indexer = harness.make()

        def fail(path):
            raise RuntimeError("synthetic failure")

        indexer.delete_file = fail
        indexer.index_all(time_budget_seconds=100.0)

        assert [k for k in harness.state() if k.endswith(".md")]


class TestOrdering:
    def test_ordinary_notes_go_before_generated_ones_newest_first(self, harness):
        import os

        for name, content, mtime in [
            ("older.md", "body", 1000),
            ("newer.md", "body", 2000),
            ("generated_new.md", "---\npeople: false\n---\nbody", 3000),
            ("generated_old.md", "---\ngenerated: 2026-01-01\n---\nbody", 500),
        ]:
            path = harness.vault / name
            path.write_text(content)
            os.utime(path, (mtime, mtime))

        harness.make().index_all(time_budget_seconds=100.0)

        assert harness.indexed == ["newer.md", "older.md", "generated_new.md", "generated_old.md"]

    def test_notes_in_a_bulk_folder_go_after_ordinary_ones(self, harness, monkeypatch):
        import os

        from config.settings import settings

        monkeypatch.setattr(settings, "vault_no_people_paths_raw", "Bulk")
        (harness.vault / "Bulk").mkdir()
        bulk = harness.vault / "Bulk" / "new.md"
        bulk.write_text("body")
        os.utime(bulk, (3000, 3000))
        plain = harness.vault / "old.md"
        plain.write_text("---\nsource: llm\n---\nbody")
        os.utime(plain, (1000, 1000))

        harness.make().index_all(time_budget_seconds=100.0)

        assert harness.indexed == ["old.md", "new.md"]


class TestUnbudgeted:
    def test_no_budget_indexes_everything(self, harness):
        harness.notes(5)
        indexer = harness.make()

        assert indexer.index_all() == 5
        assert indexer.last_run == {"files_pruned": 0, "files_remaining": 0, "files_skipped_large": 0, "stopped_early": False, "files_failed": 0, "failed_paths": []}

    def test_force_is_unbudgeted_by_default_however_slow(self, harness):
        harness.notes(5)
        harness.now = 10_000.0
        assert harness.make().index_all(force=True) == 5

    def test_explicit_budget_applies_to_force_too(self, harness):
        harness.notes(5)
        assert harness.make().index_all(force=True, time_budget_seconds=2.0) == 2

    def test_force_resumes_where_a_budgeted_force_stopped(self, harness):
        harness.notes(5)
        harness.make().index_all(force=True, time_budget_seconds=2.0)
        harness.now = 0.0
        harness.make().index_all(force=True)
        assert len(harness.indexed) == 5
        assert len(set(harness.indexed)) == 5

    def test_an_ordinary_night_inside_the_budget_is_unaffected(self, harness):
        harness.notes(3)
        indexer = harness.make()
        assert indexer.index_all(time_budget_seconds=1800.0) == 3
        assert indexer.last_run["stopped_early"] is False


class TestScriptAndOrchestrator:
    def _run(self, monkeypatch, tmp_path, **kwargs):
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)
        indexer = MagicMock()
        indexer.index_all.return_value = 4
        indexer.last_run = {"files_pruned": 1, "files_remaining": 9, "stopped_early": True, "files_failed": 0, "failed_paths": []}
        monkeypatch.setattr("config.settings.settings.vault_path", str(vault))
        with (
            patch("api.services.indexer.IndexerService", return_value=indexer),
            patch("api.services.vault_index_pages.write_index_pages", side_effect=RuntimeError("skip")),
            patch.object(reindex_script, "backfill_search_keys", return_value=0),
        ):
            result = reindex_script.sync_vault_reindex(dry_run=False, **kwargs)
        return indexer, result

    def test_manual_run_is_unbudgeted(self, monkeypatch, tmp_path):
        indexer, _ = self._run(monkeypatch, tmp_path)
        assert indexer.index_all.call_args.kwargs["time_budget_seconds"] is None

    def test_budget_reaches_the_indexer_in_seconds(self, monkeypatch, tmp_path):
        indexer, _ = self._run(monkeypatch, tmp_path, budget_minutes=30)
        assert indexer.index_all.call_args.kwargs["time_budget_seconds"] == 1800

    def test_partial_run_is_success_with_remaining_reported(self, monkeypatch, tmp_path):
        _, result = self._run(monkeypatch, tmp_path, budget_minutes=30)
        assert result["status"] == "success"
        assert result["files_remaining"] == 9
        assert result["stopped_early"] is True

    def test_stats_report_remaining_and_count_pruned_as_work(self):
        stats = reindex_script.sync_stats({"files_indexed": 4, "files_pruned": 1, "files_remaining": 9})
        assert stats["processed"] == 5
        assert stats["files_remaining"] == 9

    def test_stats_carry_failed_notes_as_errors(self):
        assert reindex_script.sync_stats({"files_indexed": 4, "files_failed": 2})["errors"] == 2
        assert reindex_script.sync_stats({"files_indexed": 4})["errors"] == 0

    def test_a_run_that_only_pruned_is_not_a_zero_yield_run(self):
        from scripts.run_all_syncs import _detect_yield_collapse, _run_yield

        stats = reindex_script.sync_stats({"files_indexed": 0, "files_pruned": 3, "files_remaining": 100})
        assert _run_yield(stats) == 3
        assert _detect_yield_collapse("vault_reindex", stats) is None

    def test_orchestrator_parses_files_remaining(self):
        from scripts.run_all_syncs import _parse_sync_output

        parsed = _parse_sync_output('SYNC_STATS:{"processed": 5, "files_remaining": 9}')
        assert parsed["files_remaining"] == 9
        assert parsed["processed"] == 5

    @pytest.mark.parametrize("flag,default,expected", [
        (None, 30, None),
        (reindex_script.BUDGET_FROM_SETTING, 30, 30),
        (reindex_script.BUDGET_FROM_SETTING, 0, None),
        (45.0, 30, 45.0),
        (0.0, 30, None),
    ])
    def test_resolve_budget(self, flag, default, expected):
        assert reindex_script.resolve_budget(flag, default) == expected

    def test_the_nightly_sync_asks_for_the_budget(self):
        from scripts.run_all_syncs import SYNC_SCRIPTS

        assert "--budget-minutes" in SYNC_SCRIPTS["vault_reindex"][1]

    def test_setting_defaults_to_half_an_hour(self):
        from config.settings import Settings

        assert Settings.model_fields["vault_index_budget_minutes"].default == 30
