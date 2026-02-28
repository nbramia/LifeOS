"""
Tests for merge crash safety (intent log, single CRM transaction, recovery).

Tests use temp SQLite databases and temp files — no production data.
"""
import json
import sqlite3
import uuid
import pytest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers to create temp databases matching production schemas
# ---------------------------------------------------------------------------

def _create_crm_db(db_path: str):
    """Create a crm.db with person_entities, source_entities, person_facts, relationships."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS person_entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            emails TEXT NOT NULL DEFAULT '[]',
            company TEXT,
            position TEXT,
            linkedin_url TEXT,
            category TEXT NOT NULL DEFAULT 'unknown',
            vault_contexts TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '[]',
            first_seen TEXT,
            last_seen TEXT,
            meeting_count INTEGER NOT NULL DEFAULT 0,
            email_count INTEGER NOT NULL DEFAULT 0,
            mention_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            slack_message_count INTEGER NOT NULL DEFAULT 0,
            related_notes TEXT NOT NULL DEFAULT '[]',
            aliases TEXT NOT NULL DEFAULT '[]',
            phone_numbers TEXT NOT NULL DEFAULT '[]',
            phone_primary TEXT,
            confidence_score REAL NOT NULL DEFAULT 1.0,
            tags TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            source_entity_count INTEGER NOT NULL DEFAULT 0,
            birthday TEXT,
            hidden INTEGER NOT NULL DEFAULT 0,
            hidden_at TEXT,
            hidden_reason TEXT NOT NULL DEFAULT '',
            relationship_strength REAL,
            is_peripheral_contact INTEGER NOT NULL DEFAULT 0,
            dunbar_circle INTEGER
        );

        CREATE TABLE IF NOT EXISTS person_emails (
            email TEXT PRIMARY KEY,
            person_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS person_phones (
            phone TEXT PRIMARY KEY,
            person_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS person_names (
            name TEXT PRIMARY KEY,
            person_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_entities (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT,
            observed_name TEXT,
            observed_email TEXT,
            observed_phone TEXT,
            metadata TEXT,
            canonical_person_id TEXT,
            link_confidence REAL DEFAULT 0.0,
            link_status TEXT DEFAULT 'auto'
        );

        CREATE TABLE IF NOT EXISTS person_facts (
            id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            source_interaction_id TEXT,
            source_quote TEXT
        );

        CREATE TABLE IF NOT EXISTS relationships (
            id TEXT PRIMARY KEY,
            person_a_id TEXT NOT NULL,
            person_b_id TEXT NOT NULL,
            relationship_type TEXT,
            shared_contexts TEXT,
            shared_events_count INTEGER DEFAULT 0,
            shared_threads_count INTEGER DEFAULT 0,
            first_seen_together TIMESTAMP,
            last_seen_together TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            shared_messages_count INTEGER DEFAULT 0,
            shared_whatsapp_count INTEGER DEFAULT 0,
            shared_slack_count INTEGER DEFAULT 0,
            is_linkedin_connection INTEGER DEFAULT 0,
            shared_phone_calls_count INTEGER DEFAULT 0,
            shared_photos_count INTEGER DEFAULT 0,
            UNIQUE(person_a_id, person_b_id)
        );
    """)
    conn.commit()
    conn.close()


def _create_interactions_db(db_path: str):
    """Create an interactions.db with the interactions table."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            snippet TEXT,
            source_link TEXT,
            source_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_source_id
        ON interactions(source_id) WHERE source_id IS NOT NULL
    """)
    conn.commit()
    conn.close()


def _insert_person(crm_path: str, person_id: str, name: str,
                    emails=None, phones=None, aliases=None, category="unknown"):
    """Insert a person entity into the CRM database."""
    conn = sqlite3.connect(crm_path)
    conn.execute("""
        INSERT INTO person_entities
        (id, canonical_name, display_name, emails, phone_numbers, aliases, category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        person_id, name, name,
        json.dumps(emails or []),
        json.dumps(phones or []),
        json.dumps(aliases or []),
        category,
    ))
    # Lookup tables
    for email in (emails or []):
        conn.execute("INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
                     (email.lower(), person_id))
    for phone in (phones or []):
        conn.execute("INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
                     (phone, person_id))
    conn.execute("INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                 (name.lower(), person_id))
    for alias in (aliases or []):
        conn.execute("INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                     (alias.lower(), person_id))
    conn.commit()
    conn.close()


def _insert_interaction(int_path: str, person_id: str, title: str = "Test interaction"):
    """Insert a test interaction."""
    conn = sqlite3.connect(int_path)
    iid = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (iid, person_id, datetime.now(timezone.utc).isoformat(), "test", title, iid))
    conn.commit()
    conn.close()
    return iid


def _insert_source_entity(crm_path: str, person_id: str):
    """Insert a test source entity linked to a person."""
    conn = sqlite3.connect(crm_path)
    se_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO source_entities (id, source_type, canonical_person_id)
        VALUES (?, ?, ?)
    """, (se_id, "test", person_id))
    conn.commit()
    conn.close()
    return se_id


def _insert_fact(crm_path: str, person_id: str):
    """Insert a test person fact."""
    conn = sqlite3.connect(crm_path)
    fact_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO person_facts (id, person_id, category, key, value)
        VALUES (?, ?, ?, ?, ?)
    """, (fact_id, person_id, "test", "test_key", "test_value"))
    conn.commit()
    conn.close()
    return fact_id


def _insert_relationship(crm_path: str, person_a_id: str, person_b_id: str,
                          events=0, messages=0):
    """Insert a test relationship (normalizes IDs)."""
    conn = sqlite3.connect(crm_path)
    rel_id = str(uuid.uuid4())
    norm_a, norm_b = (person_a_id, person_b_id) if person_a_id < person_b_id else (person_b_id, person_a_id)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO relationships
        (id, person_a_id, person_b_id, relationship_type, shared_contexts,
         shared_events_count, shared_threads_count, first_seen_together,
         last_seen_together, created_at, updated_at,
         shared_messages_count, shared_whatsapp_count, shared_slack_count,
         is_linkedin_connection, shared_phone_calls_count, shared_photos_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rel_id, norm_a, norm_b, "inferred", "[]",
          events, 0, now, now, now, now, messages, 0, 0, 0, 0, 0))
    conn.commit()
    conn.close()
    return rel_id


# ---------------------------------------------------------------------------
# Test: Intent Log Functions
# ---------------------------------------------------------------------------

class TestIntentLog:
    """Tests for merge intent log write/read/update/clear."""

    @pytest.fixture
    def log_file(self, tmp_path):
        return tmp_path / "merge_log.json"

    def test_write_and_load_intent_log(self, log_file):
        """Log write/read roundtrip."""
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import write_merge_intent, load_merge_log
            write_merge_intent("primary-1", "secondary-1")
            log = load_merge_log()
            assert log is not None
            assert log["operation"] == "merge"
            assert log["primary_id"] == "primary-1"
            assert log["secondary_id"] == "secondary-1"
            assert log["phase"] == "pending"
            assert "started_at" in log

    def test_update_phase(self, log_file):
        """Phase updates atomically."""
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import write_merge_intent, update_merge_phase, load_merge_log
            write_merge_intent("p", "s")
            update_merge_phase("ids_written")
            log = load_merge_log()
            assert log["phase"] == "ids_written"
            # Update again
            update_merge_phase("crm_done")
            log = load_merge_log()
            assert log["phase"] == "crm_done"

    def test_clear_log(self, log_file):
        """Log file removed after clear."""
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import write_merge_intent, clear_merge_log, load_merge_log
            write_merge_intent("p", "s")
            assert log_file.exists()
            clear_merge_log()
            assert not log_file.exists()
            assert load_merge_log() is None

    def test_load_merge_log_absent(self, log_file):
        """Returns None when no log file exists."""
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import load_merge_log
            assert load_merge_log() is None


# ---------------------------------------------------------------------------
# Test: CRM Single Transaction
# ---------------------------------------------------------------------------

class TestCrmSingleTransaction:
    """Tests that the CRM transaction commits atomically or rolls back."""

    @pytest.fixture
    def db_paths(self, tmp_path):
        crm_path = str(tmp_path / "crm.db")
        int_path = str(tmp_path / "interactions.db")
        _create_crm_db(crm_path)
        _create_interactions_db(int_path)
        return crm_path, int_path

    def test_crm_single_transaction_commits_together(self, db_paths):
        """All CRM changes (source_entities, facts, relationships, person delete) visible after commit."""
        crm_path, int_path = db_paths

        primary_id = "person-primary"
        secondary_id = "person-secondary"
        other_id = "person-other"

        _insert_person(crm_path, primary_id, "Alice Smith", emails=["alice@test.com"])
        _insert_person(crm_path, secondary_id, "Alice S", emails=["alices@test.com"])
        _insert_person(crm_path, other_id, "Bob Jones")
        _insert_source_entity(crm_path, secondary_id)
        _insert_fact(crm_path, secondary_id)
        _insert_fact(crm_path, primary_id)
        _insert_relationship(crm_path, secondary_id, other_id, events=3)
        _insert_interaction(int_path, secondary_id)

        # Run the CRM transaction directly
        conn = sqlite3.connect(crm_path)
        conn.execute("BEGIN IMMEDIATE")

        # Source entities
        conn.execute(
            "UPDATE source_entities SET canonical_person_id = ? WHERE canonical_person_id = ?",
            (primary_id, secondary_id))

        # Facts
        conn.execute(
            "DELETE FROM person_facts WHERE person_id IN (?, ?)",
            (primary_id, secondary_id))

        # Relationships: transfer secondary->other to primary->other
        norm_a, norm_b = (primary_id, other_id) if primary_id < other_id else (other_id, primary_id)
        rows = conn.execute(
            "SELECT id FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
            (secondary_id, secondary_id)).fetchall()
        for (rel_id,) in rows:
            conn.execute("DELETE FROM relationships WHERE id = ?", (rel_id,))
        conn.execute("""
            INSERT INTO relationships
            (id, person_a_id, person_b_id, relationship_type, shared_contexts,
             shared_events_count, shared_threads_count, created_at, updated_at,
             shared_messages_count, shared_whatsapp_count, shared_slack_count,
             is_linkedin_connection, shared_phone_calls_count, shared_photos_count)
            VALUES (?, ?, ?, 'inferred', '[]', 3, 0, datetime('now'), datetime('now'),
                    0, 0, 0, 0, 0, 0)
        """, (str(uuid.uuid4()), norm_a, norm_b))

        # Delete secondary
        conn.execute("DELETE FROM person_emails WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_phones WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_names WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_entities WHERE id = ?", (secondary_id,))

        conn.commit()
        conn.close()

        # Verify all changes visible
        conn = sqlite3.connect(crm_path)
        # Source entities re-pointed
        assert conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (primary_id,)).fetchone()[0] == 1
        # Facts cleared
        assert conn.execute("SELECT COUNT(*) FROM person_facts").fetchone()[0] == 0
        # Relationships transferred
        assert conn.execute(
            "SELECT COUNT(*) FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
            (secondary_id, secondary_id)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
            (primary_id, primary_id)).fetchone()[0] == 1
        # Secondary deleted
        assert conn.execute(
            "SELECT COUNT(*) FROM person_entities WHERE id = ?",
            (secondary_id,)).fetchone()[0] == 0
        conn.close()

    def test_crm_transaction_rolls_back_on_error(self, db_paths):
        """No partial state when exception occurs mid-transaction."""
        crm_path, _ = db_paths

        primary_id = "person-primary"
        secondary_id = "person-secondary"
        _insert_person(crm_path, primary_id, "Alice Smith")
        _insert_person(crm_path, secondary_id, "Alice S")
        _insert_source_entity(crm_path, secondary_id)
        _insert_fact(crm_path, secondary_id)

        conn = sqlite3.connect(crm_path)
        conn.execute("BEGIN IMMEDIATE")

        # Do some operations
        conn.execute(
            "UPDATE source_entities SET canonical_person_id = ? WHERE canonical_person_id = ?",
            (primary_id, secondary_id))
        conn.execute(
            "DELETE FROM person_facts WHERE person_id IN (?, ?)",
            (primary_id, secondary_id))

        # Simulate crash: rollback instead of commit
        conn.rollback()
        conn.close()

        # Verify NO changes persisted
        conn = sqlite3.connect(crm_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (secondary_id,)).fetchone()[0] == 1  # Still points to secondary
        assert conn.execute(
            "SELECT COUNT(*) FROM person_facts WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] == 1  # Facts still exist
        conn.close()


# ---------------------------------------------------------------------------
# Test: Interactions Update Idempotency
# ---------------------------------------------------------------------------

class TestInteractionsIdempotent:
    """Tests that running interaction UPDATE twice produces the same result."""

    def test_interactions_update_idempotent(self, tmp_path):
        """Running UPDATE interactions twice = same result."""
        int_path = str(tmp_path / "interactions.db")
        _create_interactions_db(int_path)

        primary_id = "person-primary"
        secondary_id = "person-secondary"

        # Insert 3 interactions for secondary
        for i in range(3):
            _insert_interaction(int_path, secondary_id, f"Interaction {i}")

        # First update
        conn = sqlite3.connect(int_path)
        conn.execute(
            "UPDATE interactions SET person_id = ? WHERE person_id = ?",
            (primary_id, secondary_id))
        conn.commit()
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE person_id = ?",
            (primary_id,)).fetchone()[0]
        conn.close()

        assert count_after_first == 3

        # Second update (idempotent — no rows match secondary_id anymore)
        conn = sqlite3.connect(int_path)
        conn.execute(
            "UPDATE interactions SET person_id = ? WHERE person_id = ?",
            (primary_id, secondary_id))
        conn.commit()
        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE person_id = ?",
            (primary_id,)).fetchone()[0]
        conn.close()

        assert count_after_second == 3  # Same result


# ---------------------------------------------------------------------------
# Test: Recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    """Tests for recover_incomplete_merge()."""

    def test_no_recovery_when_no_log(self, tmp_path):
        """No-op when merge_log.json absent."""
        log_file = tmp_path / "merge_log.json"
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import recover_incomplete_merge
            assert recover_incomplete_merge() is False

    def test_no_recovery_when_complete(self, tmp_path):
        """Clears log and returns False when phase is 'complete'."""
        log_file = tmp_path / "merge_log.json"
        log_file.write_text(json.dumps({
            "operation": "merge",
            "primary_id": "p",
            "secondary_id": "s",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "complete",
        }))
        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file):
            from scripts.merge_people import recover_incomplete_merge
            assert recover_incomplete_merge() is False
            assert not log_file.exists()

    def test_recovery_clears_log_when_primary_missing(self, tmp_path):
        """If primary doesn't exist, clears log and returns False."""
        log_file = tmp_path / "merge_log.json"
        log_file.write_text(json.dumps({
            "operation": "merge",
            "primary_id": "nonexistent",
            "secondary_id": "also-nonexistent",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "pending",
        }))

        mock_store = _make_mock_store(people={})

        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file), \
             patch('scripts.merge_people.get_person_entity_store', return_value=mock_store):
            from scripts.merge_people import recover_incomplete_merge
            assert recover_incomplete_merge() is False
            assert not log_file.exists()

    def test_recovery_runs_cleanup_when_secondary_already_deleted(self, tmp_path):
        """If secondary is already deleted, runs cleanup and clears log."""
        log_file = tmp_path / "merge_log.json"
        primary_id = "person-primary"
        log_file.write_text(json.dumps({
            "operation": "merge",
            "primary_id": primary_id,
            "secondary_id": "person-secondary",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "crm_done",
        }))

        mock_primary = _make_mock_person(primary_id, "Alice Smith")
        mock_store = _make_mock_store(people={primary_id: mock_primary})

        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file), \
             patch('scripts.merge_people.get_person_entity_store', return_value=mock_store), \
             patch('api.services.person_stats.refresh_person_stats'), \
             patch('api.services.relationship_metrics.update_strength_for_person', return_value=0.5):
            from scripts.merge_people import recover_incomplete_merge
            assert recover_incomplete_merge() is True
            assert not log_file.exists()

    def test_recovery_completes_interrupted_merge(self, tmp_path):
        """Crash at 'ids_written' phase → recovery re-runs merge → correct final state."""
        crm_path = str(tmp_path / "crm.db")
        int_path = str(tmp_path / "interactions.db")
        log_file = tmp_path / "merge_log.json"
        merged_ids_file = tmp_path / "merged_person_ids.json"
        _create_crm_db(crm_path)
        _create_interactions_db(int_path)

        primary_id = "person-aaa"
        secondary_id = "person-bbb"

        _insert_person(crm_path, primary_id, "Alice Smith", emails=["alice@test.com"])
        _insert_person(crm_path, secondary_id, "Alice S", emails=["alices@test.com"])
        _insert_interaction(int_path, secondary_id, "Test email")
        _insert_source_entity(crm_path, secondary_id)

        # Simulate crash after ids_written phase
        log_file.write_text(json.dumps({
            "operation": "merge",
            "primary_id": primary_id,
            "secondary_id": secondary_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "ids_written",
        }))
        # The merged_ids was already written (since phase > pending)
        merged_ids_file.write_text(json.dumps({secondary_id: primary_id}))

        # Build a PersonEntityStore that uses our temp db
        from api.services.person_entity import PersonEntityStore
        store = PersonEntityStore(db_path=crm_path)

        with patch('scripts.merge_people.MERGE_LOG_FILE', log_file), \
             patch('scripts.merge_people.MERGED_IDS_FILE', merged_ids_file), \
             patch('scripts.merge_people.get_person_entity_store', return_value=store), \
             patch('scripts.merge_people.get_interaction_db_path', return_value=int_path), \
             patch('scripts.merge_people.get_crm_db_path', return_value=crm_path), \
             patch('api.services.person_stats.refresh_person_stats'), \
             patch('api.services.relationship_metrics.update_strength_for_person', return_value=0.5):
            from scripts.merge_people import recover_incomplete_merge
            result = recover_incomplete_merge()
            assert result is True

        # Verify final state
        assert not log_file.exists()

        # Interactions re-pointed
        conn = sqlite3.connect(int_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE person_id = ?",
            (primary_id,)).fetchone()[0] == 1
        conn.close()

        # Secondary deleted from CRM
        conn = sqlite3.connect(crm_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM person_entities WHERE id = ?",
            (secondary_id,)).fetchone()[0] == 0
        # Primary still exists with merged emails
        row = conn.execute(
            "SELECT emails FROM person_entities WHERE id = ?",
            (primary_id,)).fetchone()
        assert row is not None
        emails = json.loads(row[0])
        assert "alice@test.com" in emails
        assert "alices@test.com" in emails
        # Source entities re-pointed
        assert conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        conn.close()


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_mock_person(person_id, name, emails=None, phones=None, aliases=None):
    """Create a mock PersonEntity-like object."""
    from unittest.mock import MagicMock
    person = MagicMock()
    person.id = person_id
    person.canonical_name = name
    person.display_name = name
    person.emails = emails or []
    person.phone_numbers = phones or []
    person.aliases = aliases or []
    person.sources = []
    person.category = "unknown"
    person.tags = []
    person.notes = ""
    person.first_seen = None
    person.last_seen = None
    person.relationship_strength = None
    person.email_count = 0
    person.message_count = 0
    person.meeting_count = 0
    return person


def _make_mock_store(people: dict):
    """Create a mock PersonEntityStore."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_by_id.side_effect = lambda pid: people.get(pid)
    return store
