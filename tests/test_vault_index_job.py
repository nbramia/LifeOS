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
        """Walk order, no cap, summaries generated rather than queued, retry run."""
        from api.services import summarizer

        for i, name in enumerate(["c.md", "a.md", "d.md", "b.md"]):
            _write_note(vault, name, 4000 - i)
        generate = MagicMock(return_value=("A synthetic generated summary of this note.", True))
        monkeypatch.setattr(summarizer, "generate_summary", generate)
        monkeypatch.setattr(indexer, "_retry_failed_summaries", MagicMock(return_value=0))
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
        indexer._retry_failed_summaries.assert_called_once()


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
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)

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

    def test_nightly_run_generates_the_queued_summary(self, indexer, vault, summary_queue, monkeypatch):
        """Job indexes and queues; the following no-argument run finds nothing
        changed and still produces the summary."""
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        indexer.index_all(light_pass=True)
        assert _queued(summary_queue) == [str(note)]

        generate = MagicMock(return_value=("A synthetic quokka summary of this note.", True))
        monkeypatch.setattr(summarizer, "generate_summary", generate)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)

        assert indexer.index_all() == 0

        generate.assert_called_once()
        assert any(hit["doc_id"].endswith("::summary") for hit in indexer.bm25_index.search("quokka"))
        assert _queued(summary_queue) == []

    def test_retry_stops_at_its_budget_and_keeps_the_rest(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer
        from config.settings import settings

        # Queued oldest first, so only newest-first order reaches the last two.
        notes = [_write_note(vault, f"{i}.md", 1000 + i) for i in range(5)]
        for note in notes:
            summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        monkeypatch.setattr(settings, "summary_retry_budget_seconds", 100)
        clock = iter(range(0, 10_000, 40))
        monkeypatch.setattr("api.services.indexer.time.monotonic", lambda: next(clock))
        retry = MagicMock(return_value="A synthetic summary of this note.")
        monkeypatch.setattr(summarizer, "retry_summary", retry)

        assert indexer._retry_failed_summaries() == 2

        assert _queued(summary_queue) == [str(n) for n in notes[:3]]

    def test_unreachable_llm_keeps_the_queue_without_fallback_summaries(
        self, indexer, vault, summary_queue, monkeypatch
    ):
        from api.services import summarizer

        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: False)
        retry = MagicMock()
        monkeypatch.setattr(summarizer, "retry_summary", retry)

        assert indexer._retry_failed_summaries() == 0

        retry.assert_not_called()
        assert _queued(summary_queue) == [str(note)]

    def test_entry_added_during_retry_survives(self, indexer, vault, summary_queue, monkeypatch):
        from api.services import summarizer

        first = _write_note(vault, "first.md", 1000)
        late = _write_note(vault, "late.md", 1000)
        summarizer.add_summary_failure(str(first), first.name)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)

        def _retry(body, file_name):
            summarizer.add_summary_failure(str(late), late.name)
            return "A synthetic summary of this note."

        monkeypatch.setattr(summarizer, "retry_summary", _retry)

        indexer._retry_failed_summaries()

        assert _queued(summary_queue) == [str(late)]


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
            "        summarizer.remove_summary_failures({f'/synthetic/doomed-{i}.md'})\n"
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


class TestDrainSummaries:
    @pytest.fixture
    def drain(self, vault, summary_queue, vector_store, nightly, monkeypatch):
        from api.services import summarizer
        from config.settings import settings

        script, _reindex = nightly
        monkeypatch.setattr(settings, "vault_path", vault)
        monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
        note = _write_note(vault, "note.md", 1000)
        summarizer.add_summary_failure(str(note), note.name)
        monkeypatch.setattr(summarizer, "retry_summary", MagicMock(return_value="A synthetic summary of this note."))
        return script

    def test_waits_for_a_restarted_llm_then_drains(self, drain, summary_queue, monkeypatch):
        from api.services import summarizer

        answers = iter([False, False, True, True])
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: next(answers))

        assert drain.main(["--drain-summaries"]) == 0

        assert _queued(summary_queue) == []
        summarizer.retry_summary.assert_called_once()

    def test_llm_that_never_returns_leaves_the_queue(self, drain, summary_queue, monkeypatch):
        from api.services import summarizer

        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: False)
        monkeypatch.setattr(drain, "LLM_READY_WAIT_SECONDS", 0)

        assert drain.main(["--drain-summaries"]) == 0

        assert len(_queued(summary_queue)) == 1
        summarizer.retry_summary.assert_not_called()

    def test_takes_the_vault_index_lock(self, drain, summary_queue, monkeypatch):
        from api.services import summarizer, vault_index_lock
        from config.settings import settings

        monkeypatch.setattr(settings, "vault_index_lock_wait_seconds", 0)
        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)
        held = vault_index_lock.try_flock(vault_index_lock.VAULT_INDEX_LOCK_PATH)
        assert held is not None

        assert drain.main(["--drain-summaries"]) == 1

        assert len(_queued(summary_queue)) == 1
        summarizer.retry_summary.assert_not_called()

    def test_does_not_reindex(self, drain, nightly, monkeypatch):
        from api.services import summarizer

        monkeypatch.setattr(summarizer, "is_summarizer_llm_available", lambda: True)

        drain.main(["--drain-summaries"])

        nightly[1].assert_not_called()


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
