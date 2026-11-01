"""Tests for how relationship strength shapes the people index."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.services import person_indexer
from api.services.person_entity import PersonEntity
from api.services.person_indexer import generate_person_document, sync_people_to_vectorstore

pytestmark = pytest.mark.unit


def _summary(strength: float, interactions: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        relationship_strength=strength,
        total_interactions_90d=interactions,
        channels=[],
        active_channels=[],
        primary_channel=None,
        contact_on_record=False,
        days_since_contact=0,
        has_facts=False,
    )


class TestStrengthWording:
    @pytest.mark.parametrize(
        ("strength", "wording"),
        [
            (100, "Very strong relationship"),
            (65, "Very strong relationship"),
            (64.9, "Strong relationship"),
            (40, "Strong relationship"),
            (39.9, "Moderate relationship"),
            (18, "Moderate relationship"),
            (17.9, "Light relationship"),
            (11, "Light relationship"),
            (10.9, "Distant relationship"),
            (0, "Distant relationship"),
        ],
    )
    def test_bands(self, strength, wording):
        person = PersonEntity(id="p1", canonical_name="Robin Example")
        doc = generate_person_document(person, _summary(strength))
        assert f"{wording} (strength: {strength}/100)" in doc


class TestEligibility:
    @pytest.fixture
    def run_sync(self, monkeypatch):
        def run(strength, interactions):
            person = PersonEntity(id="p1", canonical_name="Robin Example")
            index = MagicMock(return_value=True)
            monkeypatch.setattr(person_indexer, "VectorStore", MagicMock())
            monkeypatch.setattr(
                person_indexer, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: [person])
            )
            monkeypatch.setattr(
                person_indexer, "get_relationship_summary", lambda person_id: _summary(strength, interactions)
            )
            monkeypatch.setattr(person_indexer, "index_person_to_vectorstore", index)
            sync_people_to_vectorstore()
            return index.called

        return run

    def test_at_the_strength_cutoff(self, run_sync):
        assert run_sync(9, 0) is True

    def test_below_the_strength_cutoff(self, run_sync):
        assert run_sync(8.9, 2) is False

    def test_recent_activity_qualifies_on_its_own(self, run_sync):
        assert run_sync(0, 3) is True


class _FakeIndex:
    """Holds file paths the way the people collection does, without a server."""

    def __init__(self, paths=()):
        self.paths = set(paths)
        self.deleted = []
        self.fail_delete_of = set()
        self.fail_listing = False

    def get_all_file_paths(self):
        if self.fail_listing:
            raise ConnectionError("chroma down")
        return set(self.paths)

    def delete_document(self, file_path):
        if file_path in self.fail_delete_of:
            raise ConnectionError("chroma down")
        self.deleted.append(file_path)
        self.paths.discard(file_path)

    def add_document(self, chunks, metadata):
        self.paths.add(metadata["file_path"])


def _people_of(*ids, hidden=()):
    return [PersonEntity(id=i, canonical_name=f"Robin {i}", hidden=i in hidden) for i in ids]


def _paths(*ids):
    return {f"person:{i}" for i in ids}


@pytest.fixture
def run_sync(monkeypatch):
    """Sync a set of people against a fake index. Strength 9 is eligible, 0 is not."""

    def run(index, people, eligible=None, **kwargs):
        eligible = {p.id for p in people} if eligible is None else set(eligible)
        monkeypatch.setattr(person_indexer, "VectorStore", lambda collection_name: index)
        monkeypatch.setattr(
            person_indexer, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: people)
        )
        monkeypatch.setattr(
            person_indexer, "get_relationship_summary",
            lambda person_id: _summary(9 if person_id in eligible else 0),
        )
        monkeypatch.setattr(person_indexer, "generate_person_document", lambda person, summary: "doc")
        return sync_people_to_vectorstore(**kwargs)

    return run


class TestConvergence:
    def test_index_ends_up_holding_exactly_the_eligible_people(self, run_sync):
        # kept: already there and eligible. new: eligible, not yet indexed.
        # below: under the cutoffs. hidden: hidden. gone: absent from the store.
        index = _FakeIndex(_paths("kept1", "kept2", "kept3", "below", "hidden", "gone"))
        people = _people_of("kept1", "kept2", "kept3", "new", "below", "hidden", hidden={"hidden"})

        result = run_sync(index, people, eligible={"kept1", "kept2", "kept3", "new", "hidden"})

        assert index.paths == _paths("kept1", "kept2", "kept3", "new")
        assert result["stale"] == 3
        assert result["removed"] == 3
        assert result["removal_skipped_reason"] is None

    def test_a_second_sync_finds_nothing_left_to_remove(self, run_sync):
        index = _FakeIndex(_paths("a", "b", "c", "stale"))
        people = _people_of("a", "b", "c")

        run_sync(index, people)
        result = run_sync(index, people)

        assert result["stale"] == 0
        assert result["removed"] == 0

    def test_only_person_entries_are_touched_or_counted(self, run_sync):
        index = _FakeIndex(_paths("a", "b", "stale") | {"note.md"})

        result = run_sync(index, _people_of("a", "b"))

        assert "note.md" in index.paths
        assert "person:stale" not in index.paths
        assert result["stale"] == 1
        assert result["removed"] == 1

    def test_only_person_entries_count_toward_the_limit(self, run_sync):
        index = _FakeIndex(_paths("a", "s1", "s2") | {"one.md", "two.md", "three.md", "four.md"})

        result = run_sync(index, _people_of("a"))

        assert "2 of 3" in result["removal_skipped_reason"]
        assert _paths("s1", "s2") <= index.paths

    def test_someone_eligible_whose_indexing_failed_is_not_removed(self, run_sync, monkeypatch):
        index = _FakeIndex(_paths("a", "b", "c"))
        monkeypatch.setattr(person_indexer, "index_person_to_vectorstore", lambda *a, **k: False)

        result = run_sync(index, _people_of("a", "b", "c"))

        assert result["errors"] == 3
        assert result["stale"] == 0
        assert index.deleted == []

    def test_a_failed_removal_is_counted_and_the_rest_still_go(self, run_sync):
        index = _FakeIndex(_paths("a", "b", "c", "d", "stale1", "stale2"))
        index.fail_delete_of = {"person:stale1"}

        result = run_sync(index, _people_of("a", "b", "c", "d"))

        assert result["removal_errors"] == 1
        assert result["removed"] == 1
        assert "person:stale2" in index.deleted


class TestRemovalRails:
    def test_over_half_the_index_is_not_removed(self, run_sync, caplog):
        index = _FakeIndex(_paths("a", "s1", "s2"))

        result = run_sync(index, _people_of("a"))

        assert set(index.deleted) <= {"person:a"}
        assert _paths("s1", "s2") <= index.paths
        assert result["stale"] == 2
        assert result["removed"] == 0
        assert "2 of 3" in result["removal_skipped_reason"]
        assert any(r.levelname == "ERROR" and "removal skipped" in r.getMessage() for r in caplog.records)

    def test_exactly_half_the_index_is_removed(self, run_sync):
        index = _FakeIndex(_paths("a", "b", "s1", "s2"))

        result = run_sync(index, _people_of("a", "b"))

        assert result["removal_skipped_reason"] is None
        assert result["removed"] == 2

    def test_the_limit_measures_the_index_as_it_stood(self, run_sync):
        # Indexing ten newcomers must not dilute 2 of 3 stale entries below the limit.
        index = _FakeIndex(_paths("a", "s1", "s2"))
        people = _people_of("a", *[f"new{n}" for n in range(10)])

        result = run_sync(index, people)

        assert result["removal_skipped_reason"] is not None
        assert _paths("s1", "s2") <= index.paths

    def test_allow_large_removal_lifts_the_limit(self, run_sync):
        index = _FakeIndex(_paths("a", "s1", "s2"))

        result = run_sync(index, _people_of("a"), allow_large_removal=True)

        assert result["removal_skipped_reason"] is None
        assert index.paths == _paths("a")

    def test_no_one_eligible_removes_no_one(self, run_sync):
        index = _FakeIndex(_paths("a", "b"))

        result = run_sync(index, _people_of("a", "b"), eligible=set())

        assert index.deleted == []
        assert result["removal_skipped_reason"] == "no one is eligible"

    def test_no_one_eligible_is_refused_even_when_large_removal_is_allowed(self, run_sync):
        index = _FakeIndex(_paths("a", "b"))

        result = run_sync(index, [], eligible=set(), allow_large_removal=True)

        assert index.deleted == []
        assert result["removal_skipped_reason"] == "no one is eligible"

    def test_an_empty_store_removes_no_one(self, run_sync):
        index = _FakeIndex(_paths("a", "b"))

        result = run_sync(index, [])

        assert index.paths == _paths("a", "b")
        assert result["removal_skipped_reason"] == "no one is eligible"

    def test_a_store_that_fails_to_load_removes_no_one(self, monkeypatch):
        index = _FakeIndex(_paths("a", "b"))

        def boom():
            raise RuntimeError("database unreadable")

        monkeypatch.setattr(person_indexer, "VectorStore", lambda collection_name: index)
        monkeypatch.setattr(person_indexer, "get_person_entity_store", lambda: SimpleNamespace(get_all=boom))

        with pytest.raises(RuntimeError):
            sync_people_to_vectorstore()

        assert index.deleted == []

    def test_summaries_that_fail_to_load_remove_no_one(self, monkeypatch):
        index = _FakeIndex(_paths("a", "b"))

        def boom(person_id):
            raise RuntimeError("database unreadable")

        monkeypatch.setattr(person_indexer, "VectorStore", lambda collection_name: index)
        monkeypatch.setattr(
            person_indexer, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: _people_of("a"))
        )
        monkeypatch.setattr(person_indexer, "get_relationship_summary", boom)

        with pytest.raises(RuntimeError):
            sync_people_to_vectorstore()

        assert index.deleted == []

    def test_an_index_that_cannot_be_listed_is_left_alone_but_still_indexed(self, run_sync, caplog):
        index = _FakeIndex(_paths("a", "stale"))
        index.fail_listing = True

        result = run_sync(index, _people_of("a"))

        assert result["indexed"] == 1
        assert result["removal_skipped_reason"] == "the index could not be listed"
        assert "person:stale" in index.paths

    def test_nothing_stale_in_an_empty_index_is_not_a_failure(self, run_sync, caplog):
        result = run_sync(_FakeIndex(), [])

        assert result["removal_skipped_reason"] is None
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_rail_logs_carry_no_person_ids(self, run_sync, caplog):
        index = _FakeIndex(_paths("a", "secretid1", "secretid2"))

        run_sync(index, _people_of("a"))

        assert "secretid" not in caplog.text


def _load_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "sync_crm_to_vectorstore.py"
    spec = importlib.util.spec_from_file_location("sync_crm_to_vectorstore_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSyncStatsContract:
    def _run_main(self, monkeypatch, capsys, result):
        import sys

        script = _load_script()
        recorded = MagicMock()
        monkeypatch.setattr(script, "sync_people_to_vectorstore", lambda **kwargs: result)
        monkeypatch.setattr("api.services.sync_health.record_sync_error", recorded)
        monkeypatch.setattr(sys, "argv", ["sync_crm_to_vectorstore.py", "--execute"])
        script.main()
        return capsys.readouterr().out, recorded

    _BASE = {
        "indexed": 5, "skipped": 1, "errors": 0, "total_checked": 6,
        "stale": 3, "removed": 3, "removal_errors": 0, "removal_skipped_reason": None,
    }

    def test_removals_reach_the_orchestrator(self, monkeypatch, capsys):
        from scripts.run_all_syncs import _parse_sync_output

        output, recorded = self._run_main(monkeypatch, capsys, dict(self._BASE))
        stats = _parse_sync_output(output)

        assert stats["people_removed"] == 3
        assert stats["people_stale"] == 3
        assert stats["removal_skipped"] == 0
        assert stats["people_updated"] == 5
        assert stats["errors"] == 0
        recorded.assert_not_called()

    def test_a_skipped_removal_is_reported_as_an_error(self, monkeypatch, capsys):
        from scripts.run_all_syncs import _parse_sync_output

        result = {**self._BASE, "removed": 0, "removal_skipped_reason": "no one is eligible"}
        output, recorded = self._run_main(monkeypatch, capsys, result)
        stats = _parse_sync_output(output)

        assert stats["people_removed"] == 0
        assert stats["removal_skipped"] == 1
        assert stats["errors"] == 1
        assert "REMOVAL SKIPPED" in output
        recorded.assert_called_once()
        assert recorded.call_args.kwargs["error_type"] == "removal_skipped"

    def test_removal_errors_count_as_errors(self, monkeypatch, capsys):
        from scripts.run_all_syncs import _parse_sync_output

        result = {**self._BASE, "errors": 2, "removed": 2, "removal_errors": 1}
        output, _ = self._run_main(monkeypatch, capsys, result)

        assert _parse_sync_output(output)["errors"] == 3

    def test_telegram_summary_calls_out_a_skipped_removal(self):
        from unittest.mock import patch

        from scripts.run_all_syncs import send_sync_summary_telegram

        result = {
            "failed": 0, "succeeded": 1, "sources_run": 1, "failed_sources": [], "duration_seconds": 1,
            "results": {"crm_vectorstore": {"success": True, "removal_skipped": 1, "people_stale": 900}},
        }
        with patch("api.services.telegram.send_message", return_value=True) as send:
            send_sync_summary_telegram(result, trigger="test")

        message = send.call_args[0][0]
        assert "People index not pruned" in message
        assert "900" in message
        assert "✅" not in message

    def test_telegram_summary_is_quiet_when_removal_ran(self):
        from unittest.mock import patch

        from scripts.run_all_syncs import send_sync_summary_telegram

        result = {
            "failed": 0, "succeeded": 1, "sources_run": 1, "failed_sources": [], "duration_seconds": 1,
            "results": {"crm_vectorstore": {"success": True, "removal_skipped": 0, "people_removed": 3}},
        }
        with patch("api.services.telegram.send_message", return_value=True) as send:
            send_sync_summary_telegram(result, trigger="test")

        assert "People index not pruned" not in send.call_args[0][0]
        assert "✅" in send.call_args[0][0]


class TestDryRun:
    def test_reports_the_plan_and_writes_nothing(self, monkeypatch):
        script = _load_script()
        index = _FakeIndex(_paths("a", "b", "stale"))
        monkeypatch.setattr(script, "VectorStore", lambda collection_name: index)
        monkeypatch.setattr(
            script, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: _people_of("a", "b"))
        )
        monkeypatch.setattr(
            script, "get_relationship_summary",
            lambda person_id: SimpleNamespace(
                relationship_strength=9, total_interactions_90d=0, active_channels=[],
            ),
        )

        result = script.dry_run(9, 3)

        assert result["stale"] == 1
        assert result["removal_skipped_reason"] is None
        assert index.deleted == []

    def test_an_unreachable_index_does_not_stop_the_report(self, monkeypatch):
        script = _load_script()

        def unreachable(collection_name):
            raise ConnectionError("chroma down")

        monkeypatch.setattr(script, "VectorStore", unreachable)
        monkeypatch.setattr(
            script, "get_person_entity_store", lambda: SimpleNamespace(get_all=lambda: _people_of("a"))
        )
        monkeypatch.setattr(
            script, "get_relationship_summary",
            lambda person_id: SimpleNamespace(
                relationship_strength=9, total_interactions_90d=0, active_channels=[],
            ),
        )

        result = script.dry_run(9, 3)

        assert len(result["eligible"]) == 1
        assert "could not be listed" in result["removal_skipped_reason"]
