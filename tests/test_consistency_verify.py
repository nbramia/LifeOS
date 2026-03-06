"""Tests for post-sync consistency verification (Phase 7)."""
import json
import sqlite3
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from api.services.person_entity import PersonEntity


def _create_interaction_db(db_path: str, interactions: list[tuple]) -> None:
    """Create a test interactions database with given rows."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            person_id TEXT,
            timestamp TEXT,
            source_type TEXT,
            title TEXT,
            snippet TEXT,
            source_link TEXT,
            source_id TEXT,
            created_at TEXT
        )
    """)
    conn.executemany("""
        INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, interactions)
    conn.commit()
    conn.close()


def _create_crm_db(db_path: str, *, relationships=None, facts=None, overrides=None, source_entities=None) -> None:
    """Create a test CRM database with given rows."""
    conn = sqlite3.connect(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            id TEXT PRIMARY KEY,
            person_a_id TEXT NOT NULL,
            person_b_id TEXT NOT NULL,
            relationship_type TEXT,
            shared_events_count INTEGER DEFAULT 0,
            shared_threads_count INTEGER DEFAULT 0,
            shared_messages_count INTEGER DEFAULT 0,
            shared_whatsapp_count INTEGER DEFAULT 0,
            shared_slack_count INTEGER DEFAULT 0,
            shared_phone_calls_count INTEGER DEFAULT 0,
            shared_photos_count INTEGER DEFAULT 0,
            UNIQUE(person_a_id, person_b_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS person_facts (
            id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_overrides (
            id TEXT PRIMARY KEY,
            name_pattern TEXT NOT NULL,
            source_type TEXT,
            context_pattern TEXT,
            preferred_person_id TEXT NOT NULL,
            rejected_person_id TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_entities (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT,
            canonical_person_id TEXT,
            link_status TEXT DEFAULT 'auto'
        )
    """)

    if relationships:
        conn.executemany(
            "INSERT INTO relationships (id, person_a_id, person_b_id, relationship_type) VALUES (?, ?, ?, ?)",
            relationships
        )
    if facts:
        conn.executemany(
            "INSERT INTO person_facts (id, person_id, category, key, value) VALUES (?, ?, ?, ?, ?)",
            facts
        )
    if overrides:
        conn.executemany(
            "INSERT INTO link_overrides (id, name_pattern, preferred_person_id) VALUES (?, ?, ?)",
            overrides
        )
    if source_entities:
        conn.executemany(
            "INSERT INTO source_entities (id, source_type, canonical_person_id, link_status) VALUES (?, ?, ?, ?)",
            source_entities
        )

    conn.commit()
    conn.close()


def _make_person(id: str, name: str) -> PersonEntity:
    """Create a minimal PersonEntity."""
    return PersonEntity(
        id=id,
        canonical_name=name,
        emails=[],
    )


def _mock_store(people: list[PersonEntity], merged_ids: dict = None):
    """Create a mock PersonEntityStore."""
    store = MagicMock()
    store.get_all.return_value = people
    merged = merged_ids or {}
    store.get_canonical_id.side_effect = lambda pid: merged.get(pid, pid)
    return store


class TestNoIssuesCleanState:
    """When data is consistent, all checks return zeros."""

    def test_no_issues_clean_state(self, tmp_path):
        people = [_make_person("p1", "Alice"), _make_person("p2", "Bob")]
        valid_ids = {"p1", "p2"}
        mock_store = _mock_store(people)

        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Email", "", "", "src1", "2024-01-01"),
            ("i2", "p2", "2024-01-01", "calendar", "Meeting", "", "", "src2", "2024-01-01"),
        ])

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db,
            relationships=[("r1", "p1", "p2", "colleague")],
            facts=[("f1", "p1", "work", "role", "Engineer")],
            source_entities=[("se1", "gmail", "p1", "confirmed")],
        )

        with patch("scripts.sync_consistency_verify._get_valid_person_ids", return_value=(valid_ids, set(), mock_store)), \
             patch("scripts.sync_consistency_verify._check_person_stats", return_value={"count": 0, "fixed": 0}), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db), \
             patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import (
                _check_orphaned_interactions,
                _check_stale_merged_ids,
                _check_orphaned_crm_records,
            )

            result_oi = _check_orphaned_interactions(valid_ids, dry_run=True, fix_threshold=10)
            result_sm = _check_stale_merged_ids(valid_ids, mock_store, dry_run=True, fix_threshold=10)
            result_crm = _check_orphaned_crm_records(valid_ids, dry_run=True, fix_threshold=10)

            assert result_oi["count"] == 0
            assert result_sm["count"] == 0
            assert result_crm["count"] == 0


class TestDetectsOrphanedInteractions:
    """Finds interactions with person_id not in PersonEntity store."""

    def test_detects_orphaned_interactions(self, tmp_path):
        valid_ids = {"p1"}
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_gone", "2024-01-01", "gmail", "Orphan1", "", "", "src2", "2024-01-01"),
            ("i3", "p_gone2", "2024-01-01", "calendar", "Orphan2", "", "", "src3", "2024-01-01"),
        ])

        mock_store = _mock_store([_make_person("p1", "Alice")])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_orphaned_interactions
            result = _check_orphaned_interactions(valid_ids, dry_run=True, fix_threshold=10)

        assert result["count"] == 2
        assert result["fixed"] == 0


class TestDetectsStaleMergedIds:
    """Finds interactions pointing to merged person_ids that can be re-pointed."""

    def test_detects_stale_merged_ids(self, tmp_path):
        valid_ids = {"p1", "p2"}
        merged_ids = {"p_old": "p1"}  # p_old was merged into p1
        mock_store = _mock_store(
            [_make_person("p1", "Alice"), _make_person("p2", "Bob")],
            merged_ids=merged_ids,
        )

        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_old", "2024-01-01", "gmail", "Stale", "", "", "src2", "2024-01-01"),
            ("i3", "p_old", "2024-01-01", "calendar", "Stale2", "", "", "src3", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_stale_merged_ids
            result = _check_stale_merged_ids(valid_ids, mock_store, dry_run=True, fix_threshold=10)

        assert result["count"] == 2
        assert result["fixed"] == 0


class TestDetectsOrphanedRelationships:
    """Finds CRM records with invalid person_ids."""

    def test_detects_orphaned_relationships(self, tmp_path):
        valid_ids = {"p1", "p2"}
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db,
            relationships=[
                ("r1", "p1", "p2", "colleague"),       # valid
                ("r2", "p1", "p_gone", "friend"),       # orphaned
                ("r3", "p_gone2", "p2", "family"),      # orphaned
            ],
            facts=[
                ("f1", "p1", "work", "role", "Engineer"),  # valid
                ("f2", "p_gone", "work", "role", "PM"),     # orphaned
            ],
        )

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_orphaned_crm_records
            result = _check_orphaned_crm_records(valid_ids, dry_run=True, fix_threshold=10)

        # r2: p1 valid, p_gone invalid → orphaned
        # r3: p_gone2 invalid, p2 valid → orphaned
        # f2: p_gone invalid → orphaned
        # Total: 2 relationships + 1 fact = 3
        assert result["count"] == 3
        assert result["fixed"] == 0


class TestAutoFixesBelowThreshold:
    """Fixes applied when count <= threshold."""

    def test_auto_fixes_below_threshold(self, tmp_path):
        valid_ids = {"p1"}
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_gone", "2024-01-01", "gmail", "Orphan", "", "", "src2", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_orphaned_interactions
            result = _check_orphaned_interactions(valid_ids, dry_run=False, fix_threshold=10)

        assert result["count"] == 1
        assert result["fixed"] == 1

        # Verify the orphan was actually deleted
        conn = sqlite3.connect(interaction_db)
        count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        conn.close()
        assert count == 1  # only the valid one remains


class TestSkipsFixAboveThreshold:
    """No fixes when count > threshold."""

    def test_skips_fix_above_threshold(self, tmp_path):
        valid_ids = {"p1"}
        interaction_db = str(tmp_path / "interactions.db")

        # Create more orphans than the threshold
        interactions = [("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01")]
        for i in range(5):
            interactions.append(
                (f"orphan_{i}", f"p_gone_{i}", "2024-01-01", "gmail", "Orphan", "", "", f"src_{i}", "2024-01-01")
            )
        _create_interaction_db(interaction_db, interactions)

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_orphaned_interactions
            result = _check_orphaned_interactions(valid_ids, dry_run=False, fix_threshold=3)

        assert result["count"] == 5
        assert result["fixed"] == 0  # skipped because above threshold

        # Verify nothing was deleted
        conn = sqlite3.connect(interaction_db)
        count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        conn.close()
        assert count == 6  # all still there


class TestDryRunNoFixes:
    """No modifications in dry_run mode."""

    def test_dry_run_no_fixes(self, tmp_path):
        valid_ids = {"p1"}
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_gone", "2024-01-01", "gmail", "Orphan", "", "", "src2", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_orphaned_interactions
            result = _check_orphaned_interactions(valid_ids, dry_run=True, fix_threshold=10)

        assert result["count"] == 1
        assert result["fixed"] == 0

        # Verify nothing was deleted
        conn = sqlite3.connect(interaction_db)
        count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        conn.close()
        assert count == 2


class TestPersonStatsMismatch:
    """Cached counts differ from computed."""

    def test_person_stats_mismatch_detected(self):
        mock_discrepancies = {
            "p1": {
                "name": "Alice",
                "cached": {"email": 5, "meeting": 2, "mention": 0, "message": 0},
                "computed": {"email": 3, "meeting": 2, "mention": 0, "message": 0},
            }
        }

        with patch("api.services.person_stats.verify_person_stats", return_value=mock_discrepancies) as mock_verify:
            from scripts.sync_consistency_verify import _check_person_stats
            result = _check_person_stats(dry_run=True)

        assert result["count"] == 1
        mock_verify.assert_called_once_with(fix=False)

    def test_person_stats_fix_applied(self):
        mock_discrepancies = {
            "p1": {
                "name": "Alice",
                "cached": {"email": 5, "meeting": 2, "mention": 0, "message": 0},
                "computed": {"email": 3, "meeting": 2, "mention": 0, "message": 0},
            }
        }

        with patch("api.services.person_stats.verify_person_stats", return_value=mock_discrepancies):
            from scripts.sync_consistency_verify import _check_person_stats
            result = _check_person_stats(dry_run=False)

        assert result["count"] == 1
        assert result["fixed"] == 1


class TestVerifyConsistencyIntegration:
    """Integration test for the full verify_consistency function."""

    def test_full_run_no_issues(self, tmp_path):
        people = [_make_person("p1", "Alice"), _make_person("p2", "Bob")]
        valid_ids = {"p1", "p2"}
        mock_store = _mock_store(people)

        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Email", "", "", "src1", "2024-01-01"),
        ])

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db,
            relationships=[("r1", "p1", "p2", "colleague")],
        )

        with patch("scripts.sync_consistency_verify._get_valid_person_ids", return_value=(valid_ids, set(), mock_store)), \
             patch("api.services.person_stats.verify_person_stats", return_value={}), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db), \
             patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import verify_consistency
            result = verify_consistency(dry_run=True)

        assert result["total_issues"] == 0
        assert result["total_fixed"] == 0
        assert result["auto_fix_skipped"] is False


class TestAutoFixStaleMergedIds:
    """Auto-fix re-points stale merged IDs when below threshold."""

    def test_auto_fixes_stale_merged_ids(self, tmp_path):
        valid_ids = {"p1", "p2"}
        merged_ids = {"p_old": "p1"}
        mock_store = _mock_store(
            [_make_person("p1", "Alice"), _make_person("p2", "Bob")],
            merged_ids=merged_ids,
        )

        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_old", "2024-01-01", "gmail", "Stale", "", "", "src2", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_stale_merged_ids
            result = _check_stale_merged_ids(valid_ids, mock_store, dry_run=False, fix_threshold=10)

        assert result["count"] == 1
        assert result["fixed"] == 1

        # Verify the interaction was re-pointed
        conn = sqlite3.connect(interaction_db)
        row = conn.execute("SELECT person_id FROM interactions WHERE id = 'i2'").fetchone()
        conn.close()
        assert row[0] == "p1"  # re-pointed from p_old to p1


class TestCheckHiddenInteractions:
    """Tests for _check_hidden_interactions."""

    def test_detects_hidden_interactions_dry_run(self, tmp_path):
        hidden_ids = {"p_hidden"}
        valid_ids = {"p1", "p_hidden"}
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_hidden", "2024-01-01", "gmail", "Hidden", "", "", "src2", "2024-01-01"),
            ("i3", "p_hidden", "2024-01-01", "calendar", "Hidden2", "", "", "src3", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_hidden_interactions
            result = _check_hidden_interactions(hidden_ids, valid_ids, dry_run=True)

        assert result["count"] == 2
        assert result["fixed"] == 0

        # Verify nothing deleted
        conn = sqlite3.connect(interaction_db)
        assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 3
        conn.close()

    def test_deletes_hidden_interactions(self, tmp_path):
        hidden_ids = {"p_hidden"}
        valid_ids = {"p1", "p_hidden"}
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Valid", "", "", "src1", "2024-01-01"),
            ("i2", "p_hidden", "2024-01-01", "gmail", "Hidden", "", "", "src2", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_hidden_interactions
            result = _check_hidden_interactions(hidden_ids, valid_ids, dry_run=False)

        assert result["count"] == 1
        assert result["fixed"] == 1

        conn = sqlite3.connect(interaction_db)
        assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1
        conn.close()

    def test_empty_hidden_ids_noop(self, tmp_path):
        from scripts.sync_consistency_verify import _check_hidden_interactions
        result = _check_hidden_interactions(set(), {"p1"}, dry_run=False)
        assert result == {"count": 0, "fixed": 0}

    def test_safety_guard_skips_when_majority_hidden(self, tmp_path):
        """If >50% of people are hidden, skip cleanup as a safety guard."""
        hidden_ids = {"p1", "p2", "p3"}
        valid_ids = {"p1", "p2", "p3", "p4"}  # 3/4 = 75% hidden
        interaction_db = str(tmp_path / "interactions.db")
        _create_interaction_db(interaction_db, [
            ("i1", "p1", "2024-01-01", "gmail", "Hidden", "", "", "src1", "2024-01-01"),
        ])

        with patch("api.services.interaction_store.get_interaction_db_path", return_value=interaction_db):
            from scripts.sync_consistency_verify import _check_hidden_interactions
            result = _check_hidden_interactions(hidden_ids, valid_ids, dry_run=False)

        assert result["count"] == 0  # skipped entirely
        assert result["fixed"] == 0


class TestCheckStaleMergedRelationships:
    """Tests for _check_stale_merged_relationships."""

    def test_detects_stale_merged_relationships_dry_run(self, tmp_path):
        valid_ids = {"p1", "p2", "p3"}
        merged_ids = {"p_old": "p1"}
        mock_store = _mock_store(
            [_make_person("p1", "Alice"), _make_person("p2", "Bob"), _make_person("p3", "Carol")],
            merged_ids=merged_ids,
        )

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p_old", "p2", "colleague"),  # stale: p_old → p1
            ("r2", "p2", "p3", "friend"),          # valid
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_stale_merged_relationships
            result = _check_stale_merged_relationships(valid_ids, mock_store, dry_run=True)

        assert result["count"] == 1
        assert result["fixed"] == 0

    def test_repoints_stale_merged_relationships(self, tmp_path):
        valid_ids = {"p1", "p2", "p3"}
        merged_ids = {"p_old": "p1"}
        mock_store = _mock_store(
            [_make_person("p1", "Alice"), _make_person("p2", "Bob"), _make_person("p3", "Carol")],
            merged_ids=merged_ids,
        )

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p_old", "p2", "colleague"),  # stale
            ("r2", "p2", "p3", "friend"),          # valid
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_stale_merged_relationships
            result = _check_stale_merged_relationships(valid_ids, mock_store, dry_run=False)

        assert result["repointed"] == 1

        # Verify re-pointed
        conn = sqlite3.connect(crm_db)
        row = conn.execute("SELECT person_a_id, person_b_id FROM relationships WHERE id = 'r1'").fetchone()
        conn.close()
        assert row[0] == "p1"
        assert row[1] == "p2"

    def test_unique_constraint_handled(self, tmp_path):
        """When re-pointing would create a duplicate pair, the stale row is deleted."""
        valid_ids = {"p1", "p2"}
        merged_ids = {"p_old": "p1"}
        mock_store = _mock_store(
            [_make_person("p1", "Alice"), _make_person("p2", "Bob")],
            merged_ids=merged_ids,
        )

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p2", "colleague"),   # canonical pair already exists
            ("r2", "p_old", "p2", "friend"),    # stale — re-pointing would duplicate (p1, p2)
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_stale_merged_relationships
            # Should NOT crash with IntegrityError
            result = _check_stale_merged_relationships(valid_ids, mock_store, dry_run=False)

        assert result["count"] == 1

        # Only the canonical row should survive
        conn = sqlite3.connect(crm_db)
        rows = conn.execute("SELECT id, person_a_id, person_b_id FROM relationships").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "r1"

    def test_no_stale_ids_noop(self, tmp_path):
        valid_ids = {"p1", "p2"}
        mock_store = _mock_store([_make_person("p1", "Alice"), _make_person("p2", "Bob")])

        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[("r1", "p1", "p2", "colleague")])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_stale_merged_relationships
            result = _check_stale_merged_relationships(valid_ids, mock_store, dry_run=False)

        assert result["count"] == 0


class TestCheckRelationshipHygiene:
    """Tests for _check_relationship_hygiene."""

    def test_detects_self_loops_dry_run(self, tmp_path):
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p2", "colleague"),
            ("r2", "p1", "p1", "self-loop"),
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_relationship_hygiene
            result = _check_relationship_hygiene(set(), {"p1", "p2"}, dry_run=True)

        assert result["self_loops"] == 1
        assert result["hidden"] == 0
        assert result["count"] == 1
        assert result["fixed"] == 0

    def test_deletes_self_loops(self, tmp_path):
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p2", "colleague"),
            ("r2", "p1", "p1", "self-loop"),
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_relationship_hygiene
            result = _check_relationship_hygiene(set(), {"p1", "p2"}, dry_run=False)

        assert result["fixed"] == 1

        conn = sqlite3.connect(crm_db)
        assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1
        conn.close()

    def test_detects_hidden_person_relationships(self, tmp_path):
        hidden_ids = {"p_hidden"}
        valid_ids = {"p1", "p2", "p_hidden"}
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p2", "colleague"),
            ("r2", "p1", "p_hidden", "friend"),
            ("r3", "p_hidden", "p2", "coworker"),
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_relationship_hygiene
            result = _check_relationship_hygiene(hidden_ids, valid_ids, dry_run=False)

        assert result["hidden"] == 2
        assert result["fixed"] == 2

        conn = sqlite3.connect(crm_db)
        assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1
        conn.close()

    def test_no_double_counting_self_loop_hidden(self, tmp_path):
        """A self-loop where the person is also hidden should count once, not twice."""
        hidden_ids = {"p_hidden"}
        valid_ids = {"p1", "p_hidden"}
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p_hidden", "friend"),        # hidden only
            ("r2", "p_hidden", "p_hidden", "self"),     # self-loop AND hidden
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_relationship_hygiene
            result = _check_relationship_hygiene(hidden_ids, valid_ids, dry_run=True)

        # r2 counted as self_loop only (excluded from hidden count)
        assert result["self_loops"] == 1
        assert result["hidden"] == 1  # r1 only
        assert result["count"] == 2   # not 3

    def test_safety_guard_skips_when_majority_hidden(self, tmp_path):
        """If >50% of people are hidden, skip hidden cleanup (self-loops still cleaned)."""
        hidden_ids = {"p1", "p2", "p3"}
        valid_ids = {"p1", "p2", "p3", "p4"}  # 75% hidden
        crm_db = str(tmp_path / "crm.db")
        _create_crm_db(crm_db, relationships=[
            ("r1", "p1", "p4", "friend"),
        ])

        with patch("config.settings.settings") as mock_settings:
            mock_settings.chroma_path = str(tmp_path / "chroma")
            Path(mock_settings.chroma_path).mkdir()

            from scripts.sync_consistency_verify import _check_relationship_hygiene
            result = _check_relationship_hygiene(hidden_ids, valid_ids, dry_run=False)

        assert result["hidden"] == 0  # hidden cleanup skipped
        assert result["fixed"] == 0
