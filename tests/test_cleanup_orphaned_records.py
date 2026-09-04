"""
Tests for scripts/cleanup_orphaned_records.py's tone_analysis_results
handling (#910).

No test file previously existed for this script at all. `cleanup_orphaned_records`
reads/writes `data/people_entities.json`, `data/crm.db`, and `data/interactions.db`
via hardcoded relative paths -- deliberately not something #910 should change
(the script has always worked this way, for every other table it covers) --
so these tests `monkeypatch.chdir` into a temp directory shaped like a real
`data/` tree rather than patching the function's internals.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.link_override import LinkOverrideStore
from api.services.person_facts import PersonFactStore
from api.services.relationship import RelationshipStore
from api.services.source_entity import SourceEntityStore
from api.services.tone_analysis_store import ToneAnalysisStore

pytestmark = pytest.mark.unit


def _sample_result(score: float = 65.0) -> dict:
    return {
        "user_score": score, "partner_score": score, "combined_score": score,
        "user_sample_count": 2, "partner_sample_count": 2,
    }


@pytest.fixture
def orphan_cleanup_env(tmp_path, monkeypatch):
    """A temp cwd shaped like a real data/ tree: people_entities.json plus
    crm.db (real schema, via the real store classes, to avoid hand-authoring
    one that could drift from production) and an empty interactions.db."""
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    crm_db_path = str(data_dir / "crm.db")
    interactions_db_path = str(data_dir / "interactions.db")

    # Instantiating these against the temp crm.db creates their tables via
    # each class's own CREATE TABLE IF NOT EXISTS -- the same schema
    # cleanup_orphaned_records.py expects to find, without duplicating it
    # here by hand.
    RelationshipStore(crm_db_path)
    PersonFactStore(crm_db_path)
    LinkOverrideStore(Path(crm_db_path))
    SourceEntityStore(crm_db_path)

    conn = sqlite3.connect(interactions_db_path)
    conn.execute("""
        CREATE TABLE interactions (
            id TEXT PRIMARY KEY, person_id TEXT, timestamp TEXT,
            source_type TEXT, title TEXT
        )
    """)
    conn.commit()
    conn.close()

    def _write_valid_ids(ids):
        (data_dir / "people_entities.json").write_text(
            json.dumps([{"id": i} for i in ids])
        )

    return {
        "crm_db_path": crm_db_path,
        "interactions_db_path": interactions_db_path,
        "write_valid_ids": _write_valid_ids,
    }


class TestOrphanedToneRows:
    def test_reports_and_removes_rows_for_a_person_that_no_longer_exists(
        self, orphan_cleanup_env,
    ):
        from scripts.cleanup_orphaned_records import cleanup_orphaned_records

        tone_store = ToneAnalysisStore(orphan_cleanup_env["crm_db_path"])
        tone_store.upsert("valid-person", "2026-01", 5, _sample_result(70.0))
        tone_store.upsert("orphaned-person", "2026-01", 5, _sample_result(60.0))
        tone_store.upsert("orphaned-person", "2026-02", 3, _sample_result(55.0))

        orphan_cleanup_env["write_valid_ids"](["valid-person"])

        dry_run_stats = cleanup_orphaned_records(dry_run=True)
        assert dry_run_stats["orphan_tone_rows"] == 2
        # Dry run must not touch anything.
        assert len(tone_store.get_for_person("orphaned-person")) == 2
        assert "deleted_tone_rows" not in dry_run_stats

        execute_stats = cleanup_orphaned_records(dry_run=False)
        assert execute_stats["orphan_tone_rows"] == 2
        assert execute_stats["deleted_tone_rows"] == 2

        assert tone_store.get_for_person("orphaned-person") == []
        # The valid person's own row is untouched.
        assert len(tone_store.get_for_person("valid-person")) == 1
        assert tone_store.get_month("valid-person", "2026-01").result["user_score"] == 70.0

    def test_no_orphaned_rows_reports_zero_and_deletes_nothing(self, orphan_cleanup_env):
        from scripts.cleanup_orphaned_records import cleanup_orphaned_records

        tone_store = ToneAnalysisStore(orphan_cleanup_env["crm_db_path"])
        tone_store.upsert("valid-person", "2026-01", 5, _sample_result())

        orphan_cleanup_env["write_valid_ids"](["valid-person"])

        stats = cleanup_orphaned_records(dry_run=False)
        assert stats["orphan_tone_rows"] == 0
        assert "deleted_tone_rows" not in stats
        assert len(tone_store.get_for_person("valid-person")) == 1

    def test_missing_tone_analysis_results_table_is_handled_gracefully(
        self, orphan_cleanup_env,
    ):
        """An install where tone analysis has never run has no
        tone_analysis_results table at all (it's created lazily) -- the
        cleanup script must not error, and must report zero rather than
        silently omitting the key."""
        from scripts.cleanup_orphaned_records import cleanup_orphaned_records

        # Deliberately never instantiate ToneAnalysisStore against this db.
        orphan_cleanup_env["write_valid_ids"](["valid-person"])

        stats = cleanup_orphaned_records(dry_run=True)
        assert stats["orphan_tone_rows"] == 0

        stats = cleanup_orphaned_records(dry_run=False)
        assert stats["orphan_tone_rows"] == 0
        assert "deleted_tone_rows" not in stats
