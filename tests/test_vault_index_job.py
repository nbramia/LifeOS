"""The 15-minute vault index job and the indexer behaviour it relies on.

Embeddings, ChromaDB and the GPU reading are stubbed. BM25 runs against the
per-test database from conftest's ``_isolate_vault_indexer_stores``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
LONG_BODY = "Synthetic note body about a made-up project. " * 5


def _write_note(vault: Path, relative: str, mtime: float, body: str = LONG_BODY) -> Path:
    note = vault / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"# {note.stem}\n\n{body}\n")
    os.utime(note, (mtime, mtime))
    return note


@pytest.fixture
def vault(tmp_path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def summary_queue(tmp_path, monkeypatch) -> Path:
    from api.services import summarizer

    queue = tmp_path / "vault_summary_failures.json"
    monkeypatch.setattr(summarizer, "SUMMARY_FAILURES_FILE", str(queue))
    monkeypatch.setattr(summarizer.settings, "vault_path", tmp_path / "vault")
    return queue


@pytest.fixture
def vector_store(monkeypatch) -> MagicMock:
    """Replaces the vector store class; ``.return_value`` is the store in use."""
    import api.services.indexer as indexer_mod

    store_class = MagicMock(name="VectorStore")
    monkeypatch.setattr(indexer_mod, "VectorStore", store_class)
    return store_class


@pytest.fixture
def stats_refresh(monkeypatch) -> MagicMock:
    refresh = MagicMock(name="refresh_person_stats")
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", refresh)
    return refresh


@pytest.fixture
def indexer(vault, summary_queue, vector_store, stats_refresh):
    from api.services.indexer import IndexerService

    resolver = MagicMock()
    resolver.resolve.return_value = None
    return IndexerService(
        vault_path=str(vault),
        interaction_store=MagicMock(),
        source_entity_store=MagicMock(),
        entity_resolver=resolver,
        tag_store=MagicMock(get=MagicMock(return_value=None)),
    )


@pytest.fixture
def job(tmp_path, vault, summary_queue, vector_store, stats_refresh, monkeypatch):
    """The job script with every path it locks or reads under ``tmp_path``."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import vault_index_job
    sys.path.remove(str(REPO_ROOT / "scripts"))
    from api.services import gpu_memory, vault_index_lock
    from config.settings import settings

    monkeypatch.setattr(settings, "vault_path", vault)
    monkeypatch.setattr(vault_index_job, "SYNC_LOCK_PATH", tmp_path / "locks" / "sync.lock")
    monkeypatch.setattr(vault_index_lock, "VAULT_INDEX_LOCK_PATH", tmp_path / "locks" / "vault_index.lock")
    monkeypatch.setattr(gpu_memory, "get_available_gpu_memory_mb", lambda: 60_000)
    monkeypatch.setattr(gpu_memory, "get_available_system_ram_mb", lambda: 60_000)
    yield vault_index_job
    # The job enters maintenance mode on the shared per-process registry.
    from api.services.service_health import exit_maintenance
    exit_maintenance()


@pytest.fixture
def nightly(tmp_path, monkeypatch):
    """The nightly script's entry point, with the reindex itself stubbed."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import sync_vault_reindex
    sys.path.remove(str(REPO_ROOT / "scripts"))
    from api.services import vault_index_lock

    monkeypatch.setattr(vault_index_lock, "VAULT_INDEX_LOCK_PATH", tmp_path / "locks" / "vault_index.lock")
    reindex = MagicMock(return_value={"status": "success", "files_indexed": 0})
    monkeypatch.setattr(sync_vault_reindex, "sync_vault_reindex", reindex)
    return sync_vault_reindex, reindex


def _indexed_paths(store_class: MagicMock) -> list[str]:
    return [call.args[1]["file_path"] for call in store_class.return_value.update_document.call_args_list]


def _queued(queue: Path) -> list[str]:
    return [entry["file_path"] for entry in json.loads(queue.read_text())["files"]] if queue.exists() else []


def _state_file() -> Path:
    from api.services.indexer import IndexerService

    return Path(IndexerService.INDEX_STATE_FILE)


class TestCapAndOrder:
    def test_cap_indexes_exactly_the_newest_and_leaves_the_rest(self, indexer, vault, vector_store):
        ages = {"old.md": 1000, "newest.md": 4000, "middle.md": 2000, "newer.md": 3000}
        notes = {name: _write_note(vault, name, mtime) for name, mtime in ages.items()}

        assert indexer.index_all(max_files=2, newest_first=True, light_pass=True) == 2
        assert _indexed_paths(vector_store) == [
            str(notes["newest.md"].resolve()), str(notes["newer.md"].resolve())
        ]

        vector_store.return_value.update_document.reset_mock()
        assert indexer.index_all(max_files=2, newest_first=True, light_pass=True) == 2
        assert _indexed_paths(vector_store) == [
            str(notes["middle.md"].resolve()), str(notes["old.md"].resolve())
        ]

    def test_capped_run_still_refreshes_affected_people(self, indexer, vault, stats_refresh, monkeypatch):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)
        monkeypatch.setattr(indexer, "index_file", MagicMock(return_value={"person-a"}))

        indexer.index_all(max_files=1, newest_first=True, light_pass=True)

        stats_refresh.assert_called_once_with(["person-a"])

    def test_no_arguments_keep_walk_order_and_full_behaviour(self, indexer, vault, monkeypatch):
        """Walk order, no cap, summaries generated rather than queued."""
        from api.services import summarizer

        for i, name in enumerate(["c.md", "a.md", "d.md", "b.md"]):
            _write_note(vault, name, 4000 - i)
        generate = MagicMock(return_value=("A synthetic generated summary of this note.", True))
        monkeypatch.setattr(summarizer, "generate_summary", generate)
        walk_order = [str(f) for f in vault.rglob("*.md")]
        seen = []
        real_index_file = indexer.index_file

        def _spy(file_path, **kwargs):
            seen.append(file_path)
            return real_index_file(file_path, **kwargs)

        monkeypatch.setattr(indexer, "index_file", _spy)

        assert indexer.index_all() == 4

        assert seen == walk_order
        assert generate.call_count == 4


class TestStateAndFailures:
    def test_indexing_reaches_both_stores_and_is_recorded(self, indexer, vault, vector_store):
        note = _write_note(vault, "meeting.md", 1000, body=LONG_BODY + " zebrafishkeyword")

        assert indexer.index_all(light_pass=True) == 1

        assert _indexed_paths(vector_store) == [str(note.resolve())]
        assert indexer.bm25_index.search("zebrafishkeyword")
        assert json.loads(_state_file().read_text())[str(note)] == 1000

        vector_store.return_value.update_document.reset_mock()
        assert indexer.index_all(light_pass=True) == 0
        vector_store.return_value.update_document.assert_not_called()

    def test_edit_is_indexed_again(self, indexer, vault, vector_store):
        note = _write_note(vault, "note.md", 1000)
        indexer.index_all(light_pass=True)

        _write_note(vault, "note.md", 2000, body=LONG_BODY + " editedkeyword")

        assert indexer.index_all(light_pass=True) == 1
        assert indexer.bm25_index.search("editedkeyword")
        assert json.loads(_state_file().read_text())[str(note)] == 2000

    def test_store_failure_is_not_recorded_and_is_retried(self, indexer, vault, vector_store):
        note = _write_note(vault, "note.md", 1000)
        vector_store.return_value.update_document.side_effect = RuntimeError("synthetic failure")

        assert indexer.index_all(light_pass=True) == 0
        assert str(note) not in json.loads(_state_file().read_text())

        vector_store.return_value.update_document.side_effect = None
        assert indexer.index_all(light_pass=True) == 1
        assert str(note) in json.loads(_state_file().read_text())

    def test_unreadable_file_is_not_recorded_by_a_light_pass(self, indexer, vault):
        note = vault / "binary.md"
        note.write_bytes(b"\xff\xfe not utf-8")

        assert indexer.index_all(light_pass=True) == 0

        assert str(note) not in json.loads(_state_file().read_text())

    def test_failed_queue_write_is_not_recorded(self, indexer, vault, monkeypatch):
        note = _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(
            "api.services.summarizer.add_summary_failure", MagicMock(side_effect=OSError("synthetic disk full"))
        )

        assert indexer.index_all(light_pass=True) == 0

        assert str(note) not in json.loads(_state_file().read_text())

    def test_interrupted_state_save_keeps_the_previous_state(self, indexer, monkeypatch):
        indexer._save_index_state({"first": 1})
        monkeypatch.setattr(os, "replace", MagicMock(side_effect=OSError("synthetic crash")))

        with pytest.raises(OSError):
            indexer._save_index_state({"second": 2})

        assert json.loads(_state_file().read_text()) == {"first": 1}

    def test_deleted_note_is_removed_from_both_stores_and_state(self, indexer, vault, vector_store):
        note = _write_note(vault, "gone.md", 1000, body=LONG_BODY + " goneskeyword")
        indexer.index_all(light_pass=True)
        assert indexer.bm25_index.search("goneskeyword")
        note.unlink()

        indexer.index_all(max_files=5, newest_first=True, light_pass=True)

        vector_store.return_value.delete_document.assert_called_once_with(str(note.resolve()))
        assert not indexer.bm25_index.search("goneskeyword")
        assert str(note) not in json.loads(_state_file().read_text())


class TestSummaryQueue:
    def test_light_pass_queues_instead_of_generating(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "Work/note.md", 1000)
        generate = MagicMock()
        retry = MagicMock()
        monkeypatch.setattr(summarizer, "generate_summary", generate)
        monkeypatch.setattr(summarizer, "retry_summary", retry)

        indexer.index_all(light_pass=True)

        generate.assert_not_called()
        retry.assert_not_called()
        assert _queued(summary_queue) == [str(note)]

    def test_skip_tier_note_is_not_queued(self, indexer, vault, summary_queue):
        _write_note(vault, "zArchive/old.md", 1000)
        _write_note(vault, "Wiki/Vault Map/page.md", 1000)

        assert indexer.index_all(light_pass=True) == 2

        assert _queued(summary_queue) == []

    def test_note_too_short_to_summarize_is_not_queued(self, indexer, vault, summary_queue):
        _write_note(vault, "short.md", 1000, body="Brief.")

        assert indexer.index_all(light_pass=True) == 1

        assert _queued(summary_queue) == []

    def test_light_pass_leaves_the_task_index_to_the_api_server(self, indexer, vault, monkeypatch):
        _write_note(vault, "LifeOS/Tasks/Work.md", 1000)
        get_manager = MagicMock()
        monkeypatch.setattr("api.services.task_manager.get_task_manager", get_manager)

        assert indexer.index_all(light_pass=True) == 1

        get_manager.assert_not_called()

class TestWithMainIndexerFeatures:
    def test_underscore_folder_is_not_listed_and_drops_out_through_deletions(self, indexer, vault, vector_store):
        note = _write_note(vault, "Work/note.md", 1000)
        indexer.index_all(light_pass=True)
        moved = _write_note(vault, "_output/note.md", 1000)
        state = json.loads(_state_file().read_text())
        state[str(moved)] = 1000
        _state_file().write_text(json.dumps(state))

        changes = indexer.scan_changes()

        assert changes["to_index"] == []
        assert changes["deleted"] == [str(moved)]

        indexer.index_all(light_pass=True)

        assert str(moved) not in json.loads(_state_file().read_text())
        assert str(note) in json.loads(_state_file().read_text())

    def test_time_budget_keeps_its_own_order_under_a_cap(self, indexer, vault, monkeypatch):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)
        ordered = MagicMock(side_effect=lambda files: sorted(files, key=lambda item: item[1]))
        monkeypatch.setattr(indexer, "_ordered_for_budget", ordered)
        seen = []
        monkeypatch.setattr(indexer, "index_file", lambda path, **kwargs: seen.append(Path(path).name) or set())

        indexer.index_all(time_budget_seconds=60, max_files=2, newest_first=True)

        assert seen == ["0.md", "1.md"]

    def test_cap_reports_what_it_left(self, indexer, vault):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)

        indexer.index_all(max_files=1, newest_first=True, light_pass=True)

        assert indexer.last_run["files_remaining"] == 2


class TestSummaryQueueAcrossProcesses:
    def test_concurrent_adds_and_removes_lose_nothing(self, tmp_path, summary_queue):
        """Adders and a remover in separate processes, started together."""
        from api.services import summarizer

        doomed = [f"/synthetic/doomed-{i}.md" for i in range(40)]
        for path in doomed:
            summarizer.add_summary_failure(path, "doomed.md")

        worker = (
            "import sys\n"
            "from api.services import summarizer\n"
            "summarizer.SUMMARY_FAILURES_FILE = sys.argv[1]\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "if sys.argv[2] == 'remove':\n"
            "    for i in range(40):\n"
            "        assert summarizer.finish_queued_summary(f'/synthetic/doomed-{i}.md', 0)\n"
            "else:\n"
            "    for i in range(40):\n"
            "        summarizer.add_summary_failure(f'/synthetic/{sys.argv[2]}-{i}.md', 'note.md')\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(summary_queue), role],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=env,
            )
            for role in ("a", "b", "c", "remove")
        ]
        for process in workers:
            assert process.stdout.readline().strip() == "ready"
        for process in workers:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in workers:
            process.stdin.close()
            assert process.wait(timeout=60) == 0
            process.stdout.close()

        expected = {f"/synthetic/{role}-{i}.md" for role in "abc" for i in range(40)}
        assert set(_queued(summary_queue)) == expected


class TestMassDeletions:
    def test_more_deletions_than_fit_run_over_several_runs(self, indexer, vault, vector_store):
        keep = _write_note(vault, "keep.md", 1000)
        gone = [_write_note(vault, f"gone{i}.md", 1000) for i in range(5)]
        indexer.index_all(light_pass=True)
        for note in gone:
            note.unlink()
        store = vector_store.return_value

        def _remaining():
            state = json.loads(_state_file().read_text())
            return sum(str(note) in state for note in gone)

        indexer.index_all(light_pass=True, max_deletions=2)
        assert store.delete_document.call_count == 2
        assert _remaining() == 3

        indexer.index_all(light_pass=True, max_deletions=2)
        assert store.delete_document.call_count == 4
        assert _remaining() == 1

        indexer.index_all(light_pass=True, max_deletions=2)
        assert store.delete_document.call_count == 5
        assert _remaining() == 0

        # Each note removed exactly once: progress carried between runs.
        removed = [call.args[0] for call in store.delete_document.call_args_list]
        assert sorted(removed) == sorted(str(note.resolve()) for note in gone)
        assert str(keep) in json.loads(_state_file().read_text())

    def test_deletions_are_saved_before_indexing_starts(self, indexer, vault, monkeypatch):
        """A run killed while indexing must not repeat the deletions next tick."""
        note = _write_note(vault, "gone.md", 1000)
        indexer.index_all(light_pass=True)
        note.unlink()
        _write_note(vault, "new.md", 2000)
        monkeypatch.setattr(indexer, "index_file", MagicMock(side_effect=KeyboardInterrupt))

        with pytest.raises(KeyboardInterrupt):
            indexer.index_all(light_pass=True)

        assert str(note) not in json.loads(_state_file().read_text())

    def test_job_applies_its_deletion_cap(self, job, vault, vector_store, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_max_deletions", 2)
        _write_note(vault, "keep.md", 1000)
        gone = [_write_note(vault, f"gone{i}.md", 1000) for i in range(3)]
        job.run_job(dry_run=False)
        for note in gone:
            note.unlink()

        job.run_job(dry_run=False)

        assert vector_store.return_value.delete_document.call_count == 2


class TestDryRunAgainstRealTagStore:
    """The real store, opened read-only as the dry run opens it, with no stubs."""

    @staticmethod
    def _tag(vault: Path, note: Path, topic: str) -> None:
        from api.services.vault_tag_store import TagRecord, VaultTagStore

        VaultTagStore().upsert(TagRecord(
            file_path=note.relative_to(vault).as_posix(), content_sha256="synthetic", vocab_version="v",
            doc_type="meeting", doc_type_conf=0.9, topic=topic, topic_conf=0.9,
        ))

    def test_read_only_store_returns_what_was_written(self, vault):
        from api.services.vault_tag_store import VaultTagStore

        note = _write_note(vault, "tagged.md", 1000)
        self._tag(vault, note, "work/alpha")

        record = VaultTagStore(read_only=True).get("tagged.md")

        assert record is not None
        assert record.topic == "work/alpha"

    def test_tagged_notes_already_indexed_are_not_reported_as_changed(self, job, vault, caplog):
        note = _write_note(vault, "tagged.md", 1000)
        self._tag(vault, note, "work/alpha")
        assert job.run_job(dry_run=False)["files_indexed"] == 1

        assert job.run_job(dry_run=True)["changed"] == 0

        self._tag(vault, note, "work/beta")
        assert job.run_job(dry_run=True)["changed"] == 1

    def test_missing_tag_database_counts_by_mtime_and_says_so(self, job, vault, caplog):
        from api.services.vault_tag_store import get_vault_tags_db_path

        note = _write_note(vault, "tagged.md", 1000)
        self._tag(vault, note, "work/alpha")
        job.run_job(dry_run=False)
        db = Path(get_vault_tags_db_path())
        db.unlink()

        with caplog.at_level(logging.INFO):
            assert job.main(["--dry-run"]) == 0

        assert "0 changed" in caplog.text
        assert "Tag store unreadable" in caplog.text
        assert not db.exists()

    def test_read_only_store_rejects_an_unusable_database(self, tmp_path):
        import sqlite3

        from api.services.vault_tag_store import VaultTagStore

        with pytest.raises(sqlite3.Error):
            VaultTagStore(str(tmp_path / "absent.db"), read_only=True)

        old = tmp_path / "old.db"
        connection = sqlite3.connect(old)
        connection.execute("CREATE TABLE vault_tags (file_path TEXT)")
        connection.close()
        with pytest.raises(sqlite3.Error):
            VaultTagStore(str(old), read_only=True)

    def test_failing_tag_lookup_is_logged_once(self, indexer, vault, caplog):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000)
        indexer._tag_store = MagicMock(get=MagicMock(side_effect=RuntimeError("synthetic")))

        with caplog.at_level(logging.WARNING):
            indexer.scan_changes()

        assert caplog.text.count("Tag lookup failed") == 1


class TestJobGates:
    def test_runs_and_reports(self, job, vault, vector_store):
        note = _write_note(vault, "note.md", 1000)

        result = job.run_job(dry_run=False)

        assert result["status"] == "indexed"
        assert result["files_indexed"] == 1
        assert _indexed_paths(vector_store) == [str(note.resolve())]

    def test_applies_the_configured_cap(self, job, vault, vector_store, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_max_files", 2)
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)

        assert job.run_job(dry_run=False)["files_indexed"] == 2

    def test_nightly_holding_the_lock_makes_the_job_exit_quietly(self, job, vault, vector_store, caplog):
        from api.services import vault_index_lock

        _write_note(vault, "note.md", 1000)
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        with caplog.at_level(logging.INFO):
            exit_code = job.main(["--execute"])

        assert exit_code == 0
        vector_store.assert_not_called()
        assert "holds the vault index lock" in caplog.text

    def test_deploy_holding_the_sync_lock_makes_the_job_exit_quietly(self, job, vault, vector_store, caplog):
        from api.services import vault_index_lock

        _write_note(vault, "note.md", 1000)
        held = vault_index_lock.try_flock(job.SYNC_LOCK_PATH)
        assert held is not None

        with caplog.at_level(logging.INFO):
            exit_code = job.main(["--execute"])

        assert exit_code == 0
        vector_store.assert_not_called()
        assert "deploy restart" in caplog.text

    def test_job_holds_the_sync_lock_shared(self, job, vault, monkeypatch):
        """A second sync can overlap it; a deploy's exclusive lock cannot."""
        from api.services import vault_index_lock

        _write_note(vault, "note.md", 1000)
        seen = {}
        import api.services.indexer as indexer_mod
        real_index_all = indexer_mod.IndexerService.index_all

        def _probe(self, **kwargs):
            shared = vault_index_lock.try_flock(job.SYNC_LOCK_PATH, shared=True)
            seen["shared_ok"] = shared is not None
            seen["exclusive_ok"] = vault_index_lock.try_flock(job.SYNC_LOCK_PATH) is not None
            del shared
            return real_index_all(self, **kwargs)

        monkeypatch.setattr(indexer_mod.IndexerService, "index_all", _probe)

        job.run_job(dry_run=False)

        assert seen == {"shared_ok": True, "exclusive_ok": False}

    def test_low_gpu_memory_stands_aside_and_leaves_the_llm_alone(self, job, vault, vector_store, caplog, monkeypatch):
        from api.services import gpu_memory

        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(gpu_memory, "get_available_gpu_memory_mb", lambda: gpu_memory.EMBEDDING_MEMORY_THRESHOLD_MB - 1)
        run = MagicMock()
        monkeypatch.setattr(subprocess, "run", run)

        with caplog.at_level(logging.INFO):
            exit_code = job.main(["--execute"])

        assert exit_code == 0
        vector_store.assert_not_called()
        run.assert_not_called()
        assert "LLM left running" in caplog.text

    def test_low_system_ram_stands_aside(self, job, vault, vector_store, monkeypatch):
        from api.services import gpu_memory

        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(gpu_memory, "get_available_system_ram_mb", lambda: gpu_memory.EMBEDDING_RAM_THRESHOLD_MB - 1)

        assert job.run_job(dry_run=False)["reason"] == "insufficient_memory"
        vector_store.assert_not_called()

    def test_no_gpu_reading_does_not_hold_the_job_back(self, job, vault, monkeypatch):
        from api.services import gpu_memory

        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(gpu_memory, "get_available_gpu_memory_mb", lambda: None)

        assert job.run_job(dry_run=False)["files_indexed"] == 1

    def test_empty_run_builds_no_stores_and_no_embedding_model(self, job, vault, vector_store, monkeypatch):
        import api.services.indexer as indexer_mod

        bm25 = MagicMock()
        monkeypatch.setattr(indexer_mod, "BM25Index", bm25)
        embedding_service = MagicMock()
        monkeypatch.setattr("api.services.embeddings.get_embedding_service", embedding_service)

        assert job.run_job(dry_run=False)["status"] == "nothing_to_do"

        vector_store.assert_not_called()
        bm25.assert_not_called()
        embedding_service.assert_not_called()

    def test_second_run_after_indexing_is_empty(self, job, vault, vector_store):
        _write_note(vault, "note.md", 1000)
        job.run_job(dry_run=False)
        vector_store.reset_mock()

        assert job.run_job(dry_run=False)["status"] == "nothing_to_do"

        vector_store.assert_not_called()

    def test_deleted_note_alone_is_processed_without_a_memory_check(self, job, vault, vector_store, monkeypatch):
        from api.services import gpu_memory

        note = _write_note(vault, "gone.md", 1000)
        keep = _write_note(vault, "keep.md", 1000)
        job.run_job(dry_run=False)
        note.unlink()
        monkeypatch.setattr(gpu_memory, "get_available_gpu_memory_mb", lambda: 0)

        assert job.run_job(dry_run=False)["status"] == "indexed"

        vector_store.return_value.delete_document.assert_called_once_with(str(note.resolve()))
        assert str(keep) in json.loads(_state_file().read_text())

    def test_emptied_vault_removes_nothing(self, job, vault, vector_store):
        note = _write_note(vault, "note.md", 1000)
        job.run_job(dry_run=False)
        note.unlink()
        vector_store.reset_mock()

        assert job.run_job(dry_run=False)["reason"] == "vault_empty"

        vector_store.assert_not_called()
        assert str(note) in json.loads(_state_file().read_text())

    def test_failure_exits_nonzero_with_no_alert_and_no_sync_record(self, job, vault, vector_store, monkeypatch):
        from api.services import sync_health

        _write_note(vault, "note.md", 1000)
        vector_store.side_effect = ConnectionError("synthetic: ChromaDB is restarting")
        send_alert = MagicMock()
        monkeypatch.setattr("api.services.notifications.send_alert", send_alert)
        telegram = MagicMock()
        monkeypatch.setattr("api.services.telegram.send_message", telegram)
        recorders = {name: MagicMock() for name in ("record_sync_start", "record_sync_complete", "record_sync_error")}
        for name, recorder in recorders.items():
            monkeypatch.setattr(sync_health, name, recorder)

        assert job.main(["--execute"]) == 1

        send_alert.assert_not_called()
        telegram.assert_not_called()
        for recorder in recorders.values():
            recorder.assert_not_called()

    def test_critical_failure_inside_a_run_sends_no_alert(self, job, vault, vector_store, monkeypatch):
        """The chromadb failure VectorStore reports would otherwise pass every
        alert gate once its startup grace and failure streak have run out."""
        from api.services import service_health

        _write_note(vault, "note.md", 1000)
        send_alert = MagicMock()
        monkeypatch.setattr("api.services.notifications.send_alert", send_alert)
        monkeypatch.setattr(service_health, "STARTUP_GRACE_PERIOD_SECONDS", -1)
        monkeypatch.setattr(service_health, "MIN_CONSECUTIVE_FAILURES_FOR_ALERT", 1)
        monkeypatch.setattr(service_health, "FAILURE_DURATION_THRESHOLD_SECONDS", -1)
        registry = service_health.get_service_health()
        monkeypatch.setattr(registry, "_alert_cooldowns", {})

        def _failing_store(*args, **kwargs):
            service_health.mark_service_failed("chromadb", "synthetic", service_health.Severity.CRITICAL)
            raise ConnectionError("synthetic")

        vector_store.side_effect = _failing_store

        assert job.main(["--execute"]) == 1

        send_alert.assert_not_called()

    def test_cpu_fallback_is_logged_with_the_run(self, job, vault, caplog, monkeypatch):
        _write_note(vault, "note.md", 1000)
        service = MagicMock(using_cpu_fallback=True)
        monkeypatch.setattr("api.services.embeddings.get_embedding_service", lambda: service)

        with caplog.at_level(logging.INFO):
            assert job.main(["--execute"]) == 0

        assert "embedded on the CPU" in caplog.text

    def test_dry_run_reports_and_writes_nothing(self, job, tmp_path, vault, vector_store, caplog):
        def _snapshot():
            return sorted(
                (str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()
            )

        _write_note(vault, "older.md", 1000)
        _write_note(vault, "newer.md", 2000)
        before = _snapshot()

        with caplog.at_level(logging.INFO):
            exit_code = job.main(["--dry-run", "--max-files", "1"])

        assert exit_code == 0
        assert _snapshot() == before
        vector_store.assert_not_called()
        assert "a run would index 1 (cap 1)" in caplog.text
        assert "2 changed" in caplog.text
        assert "would index: " + str(vault / "newer.md") in caplog.text

    def test_no_mode_flag_means_dry_run(self, job, vault, vector_store):
        _write_note(vault, "note.md", 1000)

        assert job.main([]) == 0

        vector_store.assert_not_called()


BIG_BODY = "Synthetic archive filler sentence. " * 200
SIZE_LIMIT = 2000


def _service_timeout() -> int:
    import re

    service = (REPO_ROOT / "config" / "systemd" / "lifeos-vault-index.service").read_text()
    return int(re.search(r"TimeoutStartSec=(\d+)", service).group(1))


@pytest.fixture
def size_limit(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "vault_index_max_note_bytes", SIZE_LIMIT)
    return SIZE_LIMIT


@pytest.fixture
def index_all_calls(monkeypatch) -> list[dict]:
    """Every ``index_all`` call's keyword arguments; the call itself still runs."""
    from api.services.indexer import IndexerService

    calls: list[dict] = []
    real = IndexerService.index_all

    def spy(self, *args, **kwargs):
        calls.append(kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(IndexerService, "index_all", spy)
    return calls


class TestTimeBudget:
    def test_default_budget_leaves_a_margin_below_the_unit_timeout(self):
        from config.settings import Settings

        budget = Settings.model_fields["vault_index_time_budget_seconds"].default

        assert 0 < budget <= _service_timeout() - 300

    def test_job_passes_the_budget_to_the_indexer(self, job, vault, index_all_calls, monkeypatch):
        from config.settings import settings

        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(settings, "vault_index_time_budget_seconds", 321)

        job.run_job(dry_run=False)

        assert index_all_calls[0]["time_budget_seconds"] == 321

    def test_default_run_hands_the_indexer_a_budget_below_the_unit_timeout(self, job, vault, index_all_calls):
        _write_note(vault, "note.md", 1000)

        job.run_job(dry_run=False)

        assert 0 < index_all_calls[0]["time_budget_seconds"] < _service_timeout()

    def test_zero_turns_the_budget_off(self, job, vault, index_all_calls, monkeypatch):
        from config.settings import settings

        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(settings, "vault_index_time_budget_seconds", 0)

        job.run_job(dry_run=False)

        assert index_all_calls[0]["time_budget_seconds"] is None

    def test_spent_budget_ends_the_run_normally_between_notes(self, job, vault, vector_store, monkeypatch):
        from api.services.indexer import IndexerService
        from config.settings import settings

        for i in range(5):
            _write_note(vault, f"{i}.md", 1000 + i)
        monkeypatch.setattr(settings, "vault_index_time_budget_seconds", 150)
        clock = {"now": 0.0}
        monkeypatch.setattr(IndexerService, "_clock", staticmethod(lambda: clock["now"]))
        real_index_file = IndexerService.index_file

        def slow_index_file(self, *args, **kwargs):
            clock["now"] += 100
            return real_index_file(self, *args, **kwargs)

        monkeypatch.setattr(IndexerService, "index_file", slow_index_file)

        result = job.run_job(dry_run=False)

        assert result["status"] == "indexed"
        assert result["files_indexed"] == 2
        assert len(_indexed_paths(vector_store)) == 2


class TestLargeNotes:
    def test_default_limit_keeps_the_note_in_progress_inside_the_margin(self):
        """Measured on the host: 1.5 to 5 KB of note per chunk, 2 to 4 chunks per second."""
        from config.settings import Settings

        fields = Settings.model_fields
        smallest_bytes_per_chunk, slowest_chunks_per_second = 1500, 2
        worst_seconds = fields["vault_index_max_note_bytes"].default / smallest_bytes_per_chunk / slowest_chunks_per_second

        assert worst_seconds <= _service_timeout() - fields["vault_index_time_budget_seconds"].default

    def test_job_skips_a_large_note_and_the_nightly_path_indexes_it(self, job, vault, vector_store, size_limit):
        small = _write_note(vault, "small.md", 1000)
        large = _write_note(vault, "Archive/large.md", 2000, body=BIG_BODY)
        assert large.stat().st_size > size_limit

        result = job.run_job(dry_run=False)

        assert result["files_indexed"] == 1
        assert _indexed_paths(vector_store) == [str(small.resolve())]
        assert str(large) not in json.loads(_state_file().read_text())

        from api.services.indexer import IndexerService

        vector_store.return_value.update_document.reset_mock()
        nightly_indexer = IndexerService(vault_path=str(vault), tag_store=MagicMock(get=MagicMock(return_value=None)))
        assert nightly_indexer.index_all(skip_summaries=True, time_budget_seconds=1800) == 1
        assert _indexed_paths(vector_store) == [str(large.resolve())]
        assert str(large) in json.loads(_state_file().read_text())

    def test_large_notes_take_no_slot_from_smaller_ones_behind_them(self, job, vault, vector_store, size_limit, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_max_files", 2)
        _write_note(vault, "large-newest.md", 4000, body=BIG_BODY)
        _write_note(vault, "large-newer.md", 3000, body=BIG_BODY)
        first = _write_note(vault, "first.md", 2000)
        second = _write_note(vault, "second.md", 1000)

        result = job.run_job(dry_run=False)

        assert result["files_indexed"] == 2
        assert result["changed"] == 2
        assert sorted(_indexed_paths(vector_store)) == sorted([str(first.resolve()), str(second.resolve())])

    def test_run_with_only_large_notes_exits_cleanly_without_loading_anything(
        self, job, vault, vector_store, size_limit, monkeypatch
    ):
        import api.services.indexer as indexer_mod

        _write_note(vault, "a.md", 1000, body=BIG_BODY)
        _write_note(vault, "b.md", 2000, body=BIG_BODY)
        bm25 = MagicMock()
        monkeypatch.setattr(indexer_mod, "BM25Index", bm25)
        embedding_service = MagicMock()
        monkeypatch.setattr("api.services.embeddings.get_embedding_service", embedding_service)

        result = job.run_job(dry_run=False)

        assert result["status"] == "nothing_to_do"
        assert result["skipped_large"] == 2
        vector_store.assert_not_called()
        bm25.assert_not_called()
        embedding_service.assert_not_called()

    def test_only_large_notes_do_not_trip_the_memory_check(self, job, vault, size_limit, monkeypatch):
        from api.services import gpu_memory

        _write_note(vault, "a.md", 1000, body=BIG_BODY)
        monkeypatch.setattr(gpu_memory, "get_available_gpu_memory_mb", lambda: 1)

        assert job.run_job(dry_run=False)["status"] == "nothing_to_do"

    def test_skip_count_is_logged_with_the_limit(self, job, vault, size_limit, caplog):
        _write_note(vault, "small.md", 500)
        _write_note(vault, "a.md", 1000, body=BIG_BODY)
        _write_note(vault, "b.md", 2000, body=BIG_BODY)

        with caplog.at_level(logging.INFO):
            result = job.run_job(dry_run=False)

        assert result["skipped_large"] == 2
        assert f"Left 2 changed notes over {SIZE_LIMIT} bytes for the nightly reindex" in caplog.text

    def test_nothing_logged_when_no_note_is_large(self, job, vault, caplog):
        _write_note(vault, "small.md", 500)

        with caplog.at_level(logging.INFO):
            job.run_job(dry_run=False)

        assert "for the nightly reindex" not in caplog.text

    def test_dry_run_reports_the_skipped_notes(self, job, vault, size_limit, caplog):
        _write_note(vault, "small.md", 500)
        _write_note(vault, "a.md", 1000, body=BIG_BODY)

        with caplog.at_level(logging.INFO):
            result = job.run_job(dry_run=True)

        assert result["changed"] == 1
        assert result["skipped_large"] == 1
        assert "Left 1 changed notes" in caplog.text

    def test_zero_turns_the_limit_off(self, job, vault, vector_store, size_limit, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_max_note_bytes", 0)
        _write_note(vault, "large.md", 1000, body=BIG_BODY)

        assert job.run_job(dry_run=False)["files_indexed"] == 1

    def test_indexer_reports_what_it_left_apart_from_the_remaining_count(self, indexer, vault):
        _write_note(vault, "large.md", 2000, body=BIG_BODY)
        _write_note(vault, "small.md", 1000)

        indexer.index_all(light_pass=True, max_file_bytes=SIZE_LIMIT)

        assert indexer.last_run["files_skipped_large"] == 1
        assert indexer.last_run["files_remaining"] == 0
        assert indexer.last_run["stopped_early"] is False

    def test_unmeasurable_note_is_not_treated_as_large(self, indexer, vault, monkeypatch):
        _write_note(vault, "note.md", 1000)
        monkeypatch.setattr(os.path, "getsize", MagicMock(side_effect=OSError))

        assert indexer.scan_changes(max_file_bytes=SIZE_LIMIT)["skipped_large"] == 0

    def test_nightly_sets_no_size_limit(self, vault, monkeypatch):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import sync_vault_reindex
        sys.path.remove(str(REPO_ROOT / "scripts"))
        import api.services.indexer as indexer_mod
        from config.settings import settings

        indexer_class = MagicMock(name="IndexerService")
        indexer_class.return_value.index_all.return_value = 0
        indexer_class.return_value.last_run = {"files_pruned": 0, "files_remaining": 0, "stopped_early": False}
        monkeypatch.setattr(indexer_mod, "IndexerService", indexer_class)
        monkeypatch.setattr("api.services.vault_index_pages.write_index_pages", MagicMock(side_effect=RuntimeError))
        monkeypatch.setattr(settings, "vault_path", vault)

        sync_vault_reindex.sync_vault_reindex(dry_run=False, budget_minutes=30)

        kwargs = indexer_class.return_value.index_all.call_args.kwargs
        assert kwargs["time_budget_seconds"] == 1800
        assert not kwargs.get("max_file_bytes")

    def test_nightly_budget_does_not_starve_a_large_note_that_starts_inside_it(self, indexer, vault, vector_store, monkeypatch):
        from api.services.indexer import IndexerService

        large = _write_note(vault, "large.md", 2000, body=BIG_BODY)
        later = _write_note(vault, "later.md", 1000)
        clock = {"now": 0.0}
        monkeypatch.setattr(IndexerService, "_clock", staticmethod(lambda: clock["now"]))
        real_index_file = IndexerService.index_file

        def hour_long_index_file(self, *args, **kwargs):
            clock["now"] += 3600
            return real_index_file(self, *args, **kwargs)

        monkeypatch.setattr(IndexerService, "index_file", hour_long_index_file)

        assert indexer.index_all(skip_summaries=True, time_budget_seconds=1800) == 1

        assert _indexed_paths(vector_store) == [str(large.resolve())]
        assert str(large) in json.loads(_state_file().read_text())
        assert str(later) not in json.loads(_state_file().read_text())
        assert indexer.last_run["files_remaining"] == 1


class TestLightPassSaves:
    def _kill_on_third_note(self, indexer, monkeypatch):
        real_index_file = indexer.index_file
        started = []

        def index_file(path, **kwargs):
            if len(started) == 2:
                raise KeyboardInterrupt  # not an Exception: skips every handler, as a kill does
            started.append(path)
            return real_index_file(path, **kwargs)

        monkeypatch.setattr(indexer, "index_file", index_file)

    def test_light_pass_records_each_note_before_the_next_starts(self, indexer, vault, monkeypatch):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)
        self._kill_on_third_note(indexer, monkeypatch)

        with pytest.raises(KeyboardInterrupt):
            indexer.index_all(newest_first=True, light_pass=True)

        saved = json.loads(_state_file().read_text())
        assert sorted(Path(p).name for p in saved if p.endswith(".md")) == ["1.md", "2.md"]

    def test_full_pass_keeps_its_ten_file_interval(self, indexer, vault, monkeypatch):
        for i in range(3):
            _write_note(vault, f"{i}.md", 1000 + i)
        self._kill_on_third_note(indexer, monkeypatch)

        with pytest.raises(KeyboardInterrupt):
            indexer.index_all(newest_first=True, skip_summaries=True)

        assert not _state_file().exists() or not any(p.endswith(".md") for p in json.loads(_state_file().read_text()))


class TestNightlyWaits:
    def test_nightly_waits_for_the_job_then_runs(self, nightly):
        from api.services import vault_index_lock

        script, reindex = nightly
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=script.main(["--execute"])))
        thread.start()
        try:
            time.sleep(0.3)
            assert thread.is_alive()
            reindex.assert_not_called()
        finally:
            held.close()
        thread.join(timeout=10)

        assert result == {"code": 0}
        reindex.assert_called_once()

    def test_nightly_gives_up_at_its_timeout_without_indexing(self, nightly, monkeypatch, caplog):
        from api.services import vault_index_lock
        from config.settings import settings

        script, reindex = nightly
        monkeypatch.setattr(settings, "vault_index_lock_wait_seconds", 0)
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        assert script.main(["--execute"]) == 1

        reindex.assert_not_called()
        assert "still held" in caplog.text

    def test_nightly_holds_the_lock_while_it_runs(self, nightly):
        from api.services import vault_index_lock

        script, reindex = nightly
        seen = []

        def _probe(**kwargs):
            seen.append(vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH))
            return {"status": "success", "files_indexed": 0}

        reindex.side_effect = _probe

        assert script.main(["--execute"]) == 0

        assert seen == [None]

    def test_nightly_arguments_still_reach_the_time_budget(self, nightly):
        """The orchestrator passes a bare --budget-minutes, meaning the setting."""
        from config.settings import settings

        script, reindex = nightly

        assert script.main(["--execute", "--budget-minutes"]) == 0

        assert reindex.call_args.kwargs["budget_minutes"] == settings.vault_index_budget_minutes

    def test_nightly_dry_run_needs_no_lock(self, nightly):
        from api.services import vault_index_lock

        script, reindex = nightly
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        assert script.main([]) == 0

        reindex.assert_called_once()

    def test_lock_is_released_when_its_holder_is_killed(self, tmp_path):
        from api.services import vault_index_lock

        lock_path = tmp_path / "vault_index.lock"
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a')\n"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n",
                str(lock_path),
            ],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            assert vault_index_lock.try_flock(lock_path) is None
        finally:
            holder.kill()
            holder.wait()

        assert vault_index_lock.try_flock(lock_path) is not None


def _summary_docs(indexer, word: str) -> list:
    return [hit for hit in indexer.bm25_index.search(word) if hit["doc_id"].endswith("::summary")]


class TestDrain:
    """The queue drain, against a real BM25 index and queue file."""

    @staticmethod
    def _answers(monkeypatch, *answers):
        """Stub the LLM with one (summary, success) per call, or a callable."""
        from api.services import summarizer

        calls = iter(answers)

        def _generate(content, file_name, **kwargs):
            answer = next(calls)
            return answer() if callable(answer) else answer

        monkeypatch.setattr(summarizer, "generate_summary", _generate)

    @staticmethod
    def _drain(indexer):
        from api.services.summarizer import drain_summary_queue

        return drain_summary_queue(indexer.bm25_index)

    def test_job_then_drain_produces_the_summary(self, indexer, vault, summary_queue, monkeypatch):
        note = _write_note(vault, "note.md", 1000)
        indexer.index_all(light_pass=True)
        assert _queued(summary_queue) == [str(note)]
        self._answers(monkeypatch, ("A synthetic quokka summary of this note.", True))

        stats = self._drain(indexer)

        assert stats["generated"] == 1
        assert _summary_docs(indexer, "quokka")
        assert _queued(summary_queue) == []

    def test_index_all_leaves_the_queue_to_the_drain(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        indexer.index_all(light_pass=True)
        retry = MagicMock()
        monkeypatch.setattr(summarizer, "retry_summary", retry)

        assert indexer.index_all() == 0

        retry.assert_not_called()
        assert _queued(summary_queue) == [str(note)]

    def test_llm_stall_keeps_entries_and_stops_the_pass(self, indexer, vault, summary_queue, monkeypatch):
        """The first answers, then it stalls: nothing after that gets a fallback."""
        from api.services import summarizer

        notes = [_write_note(vault, f"n{i}.md", 2000 - i) for i in range(6)]
        for note in notes:
            summarizer.add_summary_failure(str(note), note.name)
        failed = (None, False)
        self._answers(monkeypatch, ("A synthetic walrus summary of this note.", True), failed, failed, failed)

        stats = self._drain(indexer)

        assert stats == {"generated": 1, "fallback": 0, "failed": 3, "changed_during_pass": 0, "stopped_on_failures": 1, "remaining": 5}
        assert _queued(summary_queue) == [str(n) for n in notes[1:]]
        docs = [hit for hit in indexer.bm25_index.search("Document") if hit["doc_id"].endswith("::summary")]
        assert len(docs) == 1
        assert "walrus" in docs[0]["content"]

    def test_a_good_answer_resets_the_failure_streak(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        notes = [_write_note(vault, f"n{i}.md", 2000 - i) for i in range(5)]
        for note in notes:
            summarizer.add_summary_failure(str(note), note.name)
        failed = (None, False)
        good = ("A synthetic summary of this note.", True)
        self._answers(monkeypatch, failed, failed, good, failed, good)

        stats = self._drain(indexer)

        assert stats["generated"] == 2
        assert stats["failed"] == 3

    def test_too_short_note_keeps_its_fallback(self, indexer, vault, summary_queue, monkeypatch):
        """Too short is an answer, not a failure: the note leaves the queue."""
        from api.services import summarizer

        note = _write_note(vault, "short.md", 1000, body="A short synthetic line of words.")
        summarizer.add_summary_failure(str(note), note.name)
        self._answers(monkeypatch, (None, True))

        stats = self._drain(indexer)

        assert stats["generated"] == 1
        assert _queued(summary_queue) == []
        assert _summary_docs(indexer, "short")

    def test_note_that_keeps_failing_gets_a_fallback_eventually(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "stubborn.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        failed = (None, False)
        self._answers(monkeypatch, failed, failed, failed)

        for _ in range(summarizer.MAX_SUMMARY_ATTEMPTS - 1):
            assert self._drain(indexer)["fallback"] == 0
            assert _queued(summary_queue) == [str(note)]
        assert self._drain(indexer)["fallback"] == 1

        assert _queued(summary_queue) == []
        assert _summary_docs(indexer, "stubborn")

    def test_notes_that_keep_failing_do_not_block_the_rest(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        stubborn = [_write_note(vault, f"s{i}.md", 9000 + i) for i in range(3)]
        older = _write_note(vault, "older.md", 1000)
        for note in [*stubborn, older]:
            summarizer.add_summary_failure(str(note), note.name)
        failed = (None, False)
        otter = ("A synthetic otter summary of this note.", True)
        self._answers(monkeypatch, failed, failed, failed, otter, failed, failed, failed)

        self._drain(indexer)
        assert not _summary_docs(indexer, "otter")
        self._drain(indexer)

        assert [hit["doc_id"] for hit in _summary_docs(indexer, "otter")] == [f"{older.resolve()}::summary"]

    def test_budget_takes_newest_first_and_keeps_the_rest(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer
        from config.settings import settings

        # Queued oldest first, so only newest-first order reaches the last two.
        notes = [_write_note(vault, f"{i}.md", 1000 + i) for i in range(5)]
        for note in notes:
            summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(settings, "summary_retry_budget_seconds", 100)
        clock = iter(range(0, 10_000, 40))
        monkeypatch.setattr("api.services.summarizer.time.monotonic", lambda: next(clock))
        good = ("A synthetic summary of this note.", True)
        self._answers(monkeypatch, good, good)

        assert self._drain(indexer)["generated"] == 2

        assert _queued(summary_queue) == [str(n) for n in notes[:3]]

    def test_note_rewritten_during_its_summary_stays_correct(self, indexer, vault, summary_queue, monkeypatch):
        """The job reindexes the note while the LLM works on its old text. The old
        summary is refused, the entry stays, and the next pass gets the new text."""
        note = _write_note(vault, "note.md", 1000, body=LONG_BODY + " oldtextmarker")
        indexer.index_all(light_pass=True)

        def _rewrite_midway():
            _write_note(vault, "note.md", 2000, body=LONG_BODY + " newtextmarker")
            assert indexer.index_all(light_pass=True) == 1
            return "A synthetic staleanswer summary of the old text.", True

        self._answers(monkeypatch, _rewrite_midway, ("A synthetic freshanswer summary of the new text.", True))

        first = self._drain(indexer)

        assert first["changed_during_pass"] == 1
        assert not _summary_docs(indexer, "staleanswer")
        assert _queued(summary_queue) == [str(note)]

        self._drain(indexer)

        assert _summary_docs(indexer, "freshanswer")
        assert not _summary_docs(indexer, "staleanswer")
        assert _queued(summary_queue) == []

    def test_full_reindex_during_a_summary_refuses_it_too(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)

        def _reindex_midway():
            indexer.index_file(str(note), skip_stats_refresh=True, skip_summaries=True)
            return "A synthetic staleanswer summary of the old text.", True

        self._answers(monkeypatch, _reindex_midway)

        assert self._drain(indexer)["changed_during_pass"] == 1
        assert not _summary_docs(indexer, "staleanswer")

    def test_note_removed_during_a_summary_leaves_none_behind(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)

        def _remove_midway():
            note.unlink()
            indexer.delete_file(str(note))
            return "A synthetic staleanswer summary of the old text.", True

        self._answers(monkeypatch, _remove_midway)

        self._drain(indexer)

        assert not _summary_docs(indexer, "staleanswer")

    def test_queue_entry_is_updated_before_the_old_summary_is_deleted(self, indexer, vault, summary_queue, monkeypatch):
        """That order closes the gap in which a finished drain could leave a summary
        of the old text: the delete that follows removes it."""
        note = _write_note(vault, "note.md", 1000)
        indexer.index_all(light_pass=True)
        _write_note(vault, "note.md", 2000, body=LONG_BODY + " edited")
        generations = []
        real_delete = indexer.bm25_index.delete_by_path

        def _spy(path):
            generations.append(json.loads(summary_queue.read_text())["files"][0].get("generation"))
            return real_delete(path)

        monkeypatch.setattr(indexer.bm25_index, "delete_by_path", _spy)

        indexer.index_all(light_pass=True)

        assert generations == [1]
        assert _queued(summary_queue) == [str(note)]

    def test_vanished_note_leaves_the_queue(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        summarizer.add_summary_failure(str(vault / "gone.md"), "gone.md")

        self._drain(indexer)

        assert _queued(summary_queue) == []


class TestDrainScript:
    @pytest.fixture
    def script(self, summary_queue, monkeypatch):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import sync_vault_summaries
        sys.path.remove(str(REPO_ROOT / "scripts"))
        return sync_vault_summaries

    def test_reports_generated_summaries(self, script, vault, summary_queue, monkeypatch, capsys):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        monkeypatch.setattr(summarizer, "generate_summary", lambda *a, **k: ("A synthetic summary of this note.", True))

        assert script.main(["--execute"]) == 0

        line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("SYNC_STATS:"))
        assert json.loads(line[len("SYNC_STATS:"):])["processed"] == 1
        assert _queued(summary_queue) == []

    def test_unreachable_llm_skips_and_keeps_the_queue(self, script, vault, summary_queue, monkeypatch, capsys):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: False)
        monkeypatch.setattr(summarizer, "LLM_READY_WAIT_SECONDS", 0)
        generate = MagicMock()
        monkeypatch.setattr(summarizer, "generate_summary", generate)

        assert script.main(["--execute"]) == 0

        assert "SYNC_SKIPPED" in capsys.readouterr().out
        generate.assert_not_called()
        assert _queued(summary_queue) == [str(note)]

    def test_slow_llm_start_gets_drained_rather_than_skipped(self, script, vault, summary_queue, monkeypatch, capsys):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        answers = iter([False, False, False, True])
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: next(answers))
        monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(summarizer, "generate_summary", lambda *a, **k: ("A synthetic summary of this note.", True))

        assert script.main(["--execute"]) == 0

        assert "SYNC_SKIPPED" not in capsys.readouterr().out
        assert _queued(summary_queue) == []

    def test_llm_that_never_answers_skips_at_the_end_of_the_wait(self, script, vault, summary_queue, monkeypatch, capsys):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: False)
        clock = iter(range(0, 10_000, 200))
        monkeypatch.setattr(script.time, "monotonic", lambda: next(clock))
        slept = []
        monkeypatch.setattr(script.time, "sleep", slept.append)

        assert script.main(["--execute"]) == 0

        assert "SYNC_SKIPPED" in capsys.readouterr().out
        assert len(slept) == 2
        assert _queued(summary_queue) == [str(note)]

    def test_healthy_llm_that_keeps_failing_reports_a_failure(self, script, vault, summary_queue, monkeypatch, capsys):
        from api.services import summarizer

        notes = [_write_note(vault, f"n{i}.md", 1000 + i) for i in range(4)]
        for note in notes:
            summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        monkeypatch.setattr(summarizer, "generate_summary", lambda *a, **k: (None, False))

        assert script.main(["--execute"]) == 1

        captured = capsys.readouterr()
        assert "Summary pass stopped" in captured.err
        assert "SYNC_SKIPPED" not in captured.out
        line = next(row for row in captured.out.splitlines() if row.startswith("SYNC_STATS:"))
        assert json.loads(line[len("SYNC_STATS:"):])["errors"] == 3
        assert len(_queued(summary_queue)) == 4

    def test_failures_short_of_a_stop_stay_a_success(self, script, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        monkeypatch.setattr(summarizer, "generate_summary", lambda *a, **k: (None, False))

        assert script.main(["--execute"]) == 0

    def test_runs_while_the_vault_index_lock_is_held(self, script, vault, summary_queue, monkeypatch):
        from api.services import summarizer, vault_index_lock

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        monkeypatch.setattr(summarizer, "generate_summary", lambda *a, **k: ("A synthetic summary of this note.", True))
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        assert script.main(["--execute"]) == 0

        assert _queued(summary_queue) == []

    def test_empty_queue_never_asks_the_llm(self, script, summary_queue, monkeypatch):
        from api.services import summarizer

        available = MagicMock()
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", available)

        assert script.main(["--execute"]) == 0

        available.assert_not_called()

    def test_without_execute_changes_nothing(self, script, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        generate = MagicMock()
        monkeypatch.setattr(summarizer, "generate_summary", generate)

        assert script.main([]) == 0

        generate.assert_not_called()


class TestDrainIsItsOwnSource:
    def test_runs_last_with_its_own_timeout_and_health_row(self):
        from api.services.summarizer import LLM_READY_WAIT_SECONDS
        from api.services.sync_health import SYNC_SOURCES
        from config.settings import settings
        from scripts import run_all_syncs

        assert run_all_syncs.SYNC_ORDER[-1] == "vault_summaries"
        assert run_all_syncs.SYNC_SCRIPTS["vault_summaries"] == ("scripts/sync_vault_summaries.py", ["--execute"])
        assert run_all_syncs.SYNC_TIMEOUTS["vault_summaries"] > (
            LLM_READY_WAIT_SECONDS + settings.summary_retry_budget_seconds + settings.ollama_retry_timeout
        )
        assert SYNC_SOURCES["vault_summaries"]["idle_is_normal"] is True

    def test_short_run_is_not_a_duration_collapse_but_others_still_are(self, monkeypatch):
        from scripts import run_all_syncs

        monkeypatch.setattr(run_all_syncs, "get_typical_duration_seconds", lambda source: 3000.0)

        assert run_all_syncs._detect_duration_collapse("vault_summaries", 1.0) is None
        assert run_all_syncs._detect_duration_collapse("vault_reindex", 1.0) is not None

    def test_nothing_waits_on_it_and_it_waits_on_nothing(self):
        """A failure stays its own, and a failed reindex doesn't hold it back."""
        from api.services.sync_health import SYNC_SOURCES

        assert SYNC_SOURCES["vault_summaries"]["depends_on"] == []
        assert not [
            name for name, source in SYNC_SOURCES.items()
            if "vault_summaries" in source.get("depends_on", [])
        ]


class TestTagLookupFailure:
    """Real tag store, indexed once with tags, then made unreadable."""

    @staticmethod
    def _tagged(vault: Path, name: str):
        from api.services.vault_tag_store import TagRecord, VaultTagStore

        note = _write_note(vault, name, 1000)
        VaultTagStore().upsert(TagRecord(
            file_path=name, content_sha256="synthetic", vocab_version="v",
            doc_type="meeting", doc_type_conf=0.9, topic="work/alpha", topic_conf=0.9,
        ))
        return note

    @pytest.fixture
    def tagged_index(self, vault, summary_queue, vector_store, stats_refresh):
        from api.services.indexer import IndexerService

        self._tagged(vault, "tagged.md")
        _write_note(vault, "untagged.md", 1000)
        resolver = MagicMock()
        resolver.resolve.return_value = None

        def _new(**kwargs):
            return IndexerService(
                vault_path=str(vault), interaction_store=MagicMock(), source_entity_store=MagicMock(),
                entity_resolver=resolver, **kwargs,
            )

        assert _new().index_all(light_pass=True) == 2
        return _new

    @staticmethod
    def _broken_store():
        return MagicMock(get=MagicMock(side_effect=RuntimeError("synthetic: database is locked")))

    def test_failed_lookups_reindex_nothing_and_keep_recorded_keys(self, tagged_index, vector_store, caplog):
        recorded = json.loads(_state_file().read_text())["__tag_phrase_keys__"]
        assert recorded
        vector_store.return_value.update_document.reset_mock()
        indexer = tagged_index(tag_store=self._broken_store())

        with caplog.at_level(logging.WARNING):
            assert indexer.scan_changes()["to_index"] == []
            assert indexer.index_all() == 0

        vector_store.return_value.update_document.assert_not_called()
        assert json.loads(_state_file().read_text())["__tag_phrase_keys__"] == recorded
        assert caplog.text.count("Tag lookup failed") == 1

    def test_store_that_cannot_open_reindexes_nothing(self, tagged_index, vector_store, monkeypatch):
        recorded = json.loads(_state_file().read_text())["__tag_phrase_keys__"]
        vector_store.return_value.update_document.reset_mock()
        monkeypatch.setattr(
            "api.services.vault_tag_store.VaultTagStore", MagicMock(side_effect=OSError("synthetic: unreadable"))
        )

        assert tagged_index().index_all(light_pass=True) == 0

        vector_store.return_value.update_document.assert_not_called()
        assert json.loads(_state_file().read_text())["__tag_phrase_keys__"] == recorded

    def test_real_change_is_noticed_once_lookups_work_again(self, tagged_index, vault):
        from api.services.vault_tag_store import TagRecord, VaultTagStore

        tagged_index(tag_store=self._broken_store()).index_all()
        VaultTagStore().upsert(TagRecord(
            file_path="tagged.md", content_sha256="synthetic", vocab_version="v",
            doc_type="meeting", doc_type_conf=0.9, topic="work/beta", topic_conf=0.9,
        ))

        assert tagged_index().scan_changes()["to_index"] == [str(vault / "tagged.md")]

    def test_note_edited_during_an_outage_is_indexed_once_more_after_it(self, tagged_index, vault):
        """Indexed without its phrase, so it records no key and gets the phrase back."""
        _write_note(vault, "tagged.md", 2000, body=LONG_BODY + " edited")

        assert tagged_index(tag_store=self._broken_store()).index_all() == 1
        assert tagged_index().index_all() == 1
        assert tagged_index().index_all() == 0

    def test_a_note_with_no_row_stays_unchanged_without_any_warning(self, tagged_index, caplog):
        with caplog.at_level(logging.WARNING):
            assert tagged_index().scan_changes()["to_index"] == []

        assert "Tag lookup failed" not in caplog.text


class TestApiReindexTakesTheLock:
    def test_background_job_waits_for_the_lock_and_holds_it(self, vector_store, monkeypatch):
        from api.services import job_queue, vault_index_lock

        seen = []
        indexer_class = MagicMock()
        indexer_class.return_value.index_all.side_effect = lambda: (
            seen.append(vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)) or 3
        )
        monkeypatch.setattr("api.services.indexer.IndexerService", indexer_class)

        assert job_queue._handle_reindex_vault({}) == {"files_indexed": 3}

        assert seen == [None]
        released = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert released is not None

    def test_background_job_gives_up_without_indexing(self, monkeypatch):
        from api.services import job_queue, vault_index_lock
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_lock_wait_seconds", 0)
        indexer_class = MagicMock()
        monkeypatch.setattr("api.services.indexer.IndexerService", indexer_class)
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        with pytest.raises(RuntimeError, match="still held"):
            job_queue._handle_reindex_vault({})

        indexer_class.return_value.index_all.assert_not_called()

    def test_sync_route_reports_busy_without_indexing(self, monkeypatch):
        import asyncio

        from api.routes import admin
        from api.services import vault_index_lock

        indexer_class = MagicMock()
        monkeypatch.setattr(admin, "IndexerService", indexer_class)
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        response = asyncio.run(admin.reindex_sync())

        assert response.status == "busy"
        indexer_class.assert_not_called()

    def test_sync_route_holds_and_releases_the_lock(self, monkeypatch):
        import asyncio

        from api.routes import admin
        from api.services import vault_index_lock

        seen = []
        indexer_class = MagicMock()
        indexer_class.return_value.index_all.side_effect = lambda: (
            seen.append(vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)) or 4
        )
        monkeypatch.setattr(admin, "IndexerService", indexer_class)

        assert asyncio.run(admin.reindex_sync()).files_indexed == 4

        assert seen == [None]
        assert vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH) is not None


class TestWatcherRemoved:
    def test_vault_watcher_is_gone(self):
        import api.services.indexer as indexer_mod

        assert not hasattr(indexer_mod, "VaultEventHandler")
        assert not hasattr(indexer_mod.IndexerService, "start_watching")
        assert not hasattr(indexer_mod.IndexerService, "is_watching")
        assert "watchdog" not in (REPO_ROOT / "api" / "services" / "indexer.py").read_text()

    def test_task_and_scheduler_watchers_remain(self):
        for module in ("task_watcher", "scheduler_watcher"):
            assert "from watchdog.observers import Observer" in (
                REPO_ROOT / "api" / "services" / f"{module}.py"
            ).read_text()


class TestInstallation:
    def test_timer_runs_the_job_every_15_minutes(self):
        timer = (REPO_ROOT / "config" / "systemd" / "lifeos-vault-index.timer").read_text()
        service = (REPO_ROOT / "config" / "systemd" / "lifeos-vault-index.service").read_text()
        assert "OnUnitActiveSec=15min" in timer
        assert "Type=oneshot" in service
        assert "scripts/vault_index_job.py --execute" in service

    def test_setup_enables_the_timer(self):
        setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
        assert "systemctl enable --now lifeos-vault-index.timer" in setup

    def test_job_timeout_is_shorter_than_the_nightly_wait(self):
        import re

        from config.settings import Settings

        service = (REPO_ROOT / "config" / "systemd" / "lifeos-vault-index.service").read_text()
        timeout = int(re.search(r"TimeoutStartSec=(\d+)", service).group(1))
        assert timeout + 90 < Settings.model_fields["vault_index_lock_wait_seconds"].default


def _announce_deploy(job, age_seconds: float = 0) -> Path:
    """Leave the marker auto-deploy.sh leaves when it finds the sync lock held."""
    marker = job.SYNC_LOCK_PATH.with_name(job.DEPLOY_PENDING_NAME)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    stamp = time.time() - age_seconds
    os.utime(marker, (stamp, stamp))
    return marker


class TestDeployPending:
    def test_fresh_marker_starts_no_run_and_takes_no_lock(self, job, vault, vector_store):
        from api.services import vault_index_lock

        _write_note(vault, "note.md", 1000)
        _announce_deploy(job)

        result = job.run_job(dry_run=False)

        assert result == {"status": "skipped", "reason": "deploy_pending"}
        vector_store.assert_not_called()
        assert vault_index_lock.try_flock(job.SYNC_LOCK_PATH) is not None, "the job took the sync lock"
        assert vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH) is not None

    def test_marker_is_honoured_until_it_is_old_and_then_ignored(self, job, vault, vector_store):
        _write_note(vault, "note.md", 1000)
        marker = _announce_deploy(job, age_seconds=job.DEPLOY_PENDING_MAX_AGE_SECONDS - 60)
        assert job.run_job(dry_run=False)["reason"] == "deploy_pending"

        stamp = time.time() - job.DEPLOY_PENDING_MAX_AGE_SECONDS - 60
        os.utime(marker, (stamp, stamp))
        result = job.run_job(dry_run=False)

        assert result["status"] == "indexed"
        assert len(_indexed_paths(vector_store)) == 1

    def test_marker_stays_fresh_across_the_gap_between_deploy_ticks(self):
        """Every deferral refreshes the marker, so it must outlast several ticks of the deploy timer."""
        import re

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import vault_index_job
        sys.path.remove(str(REPO_ROOT / "scripts"))

        timer = (REPO_ROOT / "config" / "systemd" / "lifeos-autodeploy.timer").read_text()
        interval_seconds = int(re.search(r"OnUnitActiveSec=(\d+)min", timer).group(1)) * 60

        assert vault_index_job.DEPLOY_PENDING_MAX_AGE_SECONDS >= 2 * interval_seconds

    def test_run_ends_between_notes_when_a_deploy_announces_itself(self, job, vault, vector_store, monkeypatch):
        from api.services.indexer import IndexerService

        for i in range(6):
            _write_note(vault, f"{i}.md", 1000 + i)
        real_index_file = IndexerService.index_file
        calls = []

        def index_file(self, *args, **kwargs):
            calls.append(args[0])
            outcome = real_index_file(self, *args, **kwargs)
            if len(calls) == 2:
                _announce_deploy(job)
            return outcome

        monkeypatch.setattr(IndexerService, "index_file", index_file)

        result = job.run_job(dry_run=False)

        assert result["status"] == "indexed"
        assert result["files_indexed"] == 2
        assert len(calls) == 2
        assert len([p for p in json.loads(_state_file().read_text()) if p.endswith(".md")]) == 2

    def test_stopped_run_loses_no_work(self, job, vault, vector_store, monkeypatch):
        from api.services.indexer import IndexerService

        notes = {str(_write_note(vault, f"{i}.md", 1000 + i).resolve()) for i in range(6)}
        real_index_file = IndexerService.index_file
        seen = []

        def index_file(self, *args, **kwargs):
            outcome = real_index_file(self, *args, **kwargs)
            seen.append(args[0])
            if len(seen) == 3:
                _announce_deploy(job)
            return outcome

        monkeypatch.setattr(IndexerService, "index_file", index_file)
        job.run_job(dry_run=False)
        assert len(_indexed_paths(vector_store)) == 3
        job.SYNC_LOCK_PATH.with_name(job.DEPLOY_PENDING_NAME).unlink()
        while job.run_job(dry_run=False)["status"] == "indexed":
            pass

        indexed = _indexed_paths(vector_store)
        assert sorted(indexed) == sorted(notes)
        assert len(indexed) == len(set(indexed)), "a note was indexed twice"

    def test_indexer_stops_when_asked_and_a_later_run_finishes(self, indexer, vault, vector_store):
        for i in range(5):
            _write_note(vault, f"{i}.md", 1000 + i)
        asked = []

        def should_stop():
            asked.append(1)
            return len(asked) > 2

        assert indexer.index_all(newest_first=True, light_pass=True, should_stop=should_stop) == 2
        assert indexer.last_run["stopped_early"] is True
        assert indexer.last_run["files_remaining"] == 3

        assert indexer.index_all(newest_first=True, light_pass=True) == 3

    def test_indexer_asked_to_stop_removes_no_deleted_notes_and_a_later_run_does(self, indexer, vault, vector_store):
        notes = [_write_note(vault, f"{i}.md", 1000 + i) for i in range(3)]
        indexer.index_all(light_pass=True)
        for note in notes:
            note.unlink()
        _write_note(vault, "keep.md", 5000)

        indexer.index_all(light_pass=True, should_stop=lambda: True)
        assert indexer.last_run["files_pruned"] == 0
        assert indexer.last_run["files_remaining"] == 4

        indexer.index_all(light_pass=True)
        assert indexer.last_run["files_pruned"] == 3

    def test_a_deploy_gets_through_runs_that_follow_one_another(self, job, vault, vector_store, tmp_path, monkeypatch):
        """The job's runs follow one another with no gap and its backlog never
        empties. A deploy tick that finds the lock held leaves its marker; the
        next tick must get the lock, and the job loses nothing."""
        from api.services.indexer import IndexerService
        from tests.test_deploy_drift import _restarts, _run_main, _stale_api_repo

        repo, state, venv = _stale_api_repo(tmp_path / "deploy")
        monkeypatch.setattr(job, "SYNC_LOCK_PATH", repo / "home" / ".lifeos" / "sync.lock")
        for i in range(30):
            _write_note(vault, f"seed-{i:03}.md", 1000 + i)
        real_index_file = IndexerService.index_file
        in_run = threading.Event()
        growing = {"on": True, "n": 0}

        def index_file(self, *args, **kwargs):
            in_run.set()
            time.sleep(0.02)
            if growing["on"]:
                growing["n"] += 1
                _write_note(vault, f"new-{growing['n']:04}.md", 5000 + growing["n"])
            return real_index_file(self, *args, **kwargs)

        monkeypatch.setattr(IndexerService, "index_file", index_file)
        keep_running = threading.Event()
        keep_running.set()

        def timer():
            while keep_running.is_set():
                job.run_job(dry_run=False, max_files=30)

        runner = threading.Thread(target=timer, daemon=True)
        runner.start()
        try:
            assert in_run.wait(15), "the job never started indexing"
            attempts = 0
            while "restart lifeos-api" not in _restarts(state):
                attempts += 1
                assert attempts <= 3, "the deploy was starved by back-to-back runs"
                assert _run_main(repo, state, venv).returncode == 0
                time.sleep(0.3)  # the gap between deploy ticks
            assert attempts == 2
        finally:
            keep_running.clear()
            runner.join(30)
        assert not runner.is_alive()
        assert not job.SYNC_LOCK_PATH.with_name(job.DEPLOY_PENDING_NAME).exists()

        growing["on"] = False
        while job.run_job(dry_run=False, max_files=30)["status"] == "indexed":
            pass
        indexed = _indexed_paths(vector_store)
        assert len(indexed) == len(set(indexed)), "a note was indexed twice"
        assert {Path(p).name for p in indexed} == {p.name for p in vault.glob("*.md")}
