"""Tests for soft delete on merge, purge, and undo."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from api.services.person_entity import PersonEntityStore

pytestmark = pytest.mark.unit


def _insert_person(path: str, person_id: str, name: str,
                   emails=None, phones=None, hidden=False,
                   hidden_at=None, hidden_reason=""):
    """Insert a person record using raw SQL for test setup."""
    conn = sqlite3.connect(path)
    emails = emails or []
    phones = phones or []
    conn.execute("""
        INSERT INTO person_entities (id, canonical_name, emails, phone_numbers, hidden, hidden_at, hidden_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (person_id, name, json.dumps(emails), json.dumps(phones),
          1 if hidden else 0, hidden_at, hidden_reason))

    for email in emails:
        conn.execute("INSERT OR REPLACE INTO person_emails (email, person_id) VALUES (?, ?)",
                     (email.lower(), person_id))
    for phone in phones:
        conn.execute("INSERT OR REPLACE INTO person_phones (phone, person_id) VALUES (?, ?)",
                     (phone, person_id))
    conn.execute("INSERT OR REPLACE INTO person_names (name, person_id) VALUES (?, ?)",
                 (name.lower(), person_id))
    conn.commit()
    conn.close()


@pytest.fixture
def crm_db(tmp_path):
    """Create a temp CRM database using PersonEntityStore (creates full schema)."""
    path = str(tmp_path / "crm.db")
    PersonEntityStore(db_path=path)  # Creates person_entities tables via _init_db()
    # Also create source_entities table (used by undo_merge)
    from api.services.source_entity import SourceEntityStore
    SourceEntityStore(db_path=path)  # Creates source_entities table via _init_db()
    return path


@pytest.fixture
def store(crm_db):
    """PersonEntityStore backed by temp db."""
    return PersonEntityStore(db_path=crm_db)


class TestPurgeHidden:
    """Tests for PersonEntityStore.purge_hidden()."""

    def test_purges_old_hidden_entity(self, crm_db, store):
        """Entity hidden >90 days ago is purged."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        _insert_person(crm_db, "person-old", "Old Person",
                       hidden=True, hidden_at=old_date,
                       hidden_reason="merged_into:person-primary")

        count = store.purge_hidden(older_than_days=90)
        assert count == 1

        conn = sqlite3.connect(crm_db)
        assert conn.execute(
            "SELECT COUNT(*) FROM person_entities WHERE id = 'person-old'"
        ).fetchone()[0] == 0
        conn.close()

    def test_preserves_recent_hidden_entity(self, crm_db, store):
        """Entity hidden <90 days ago is NOT purged."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _insert_person(crm_db, "person-recent", "Recent Person",
                       hidden=True, hidden_at=recent_date,
                       hidden_reason="merged_into:person-primary")

        count = store.purge_hidden(older_than_days=90)
        assert count == 0

        conn = sqlite3.connect(crm_db)
        assert conn.execute(
            "SELECT COUNT(*) FROM person_entities WHERE id = 'person-recent'"
        ).fetchone()[0] == 1
        conn.close()

    def test_ignores_non_hidden_entities(self, crm_db, store):
        """Non-hidden entities are never purged."""
        _insert_person(crm_db, "person-visible", "Visible Person")

        count = store.purge_hidden(older_than_days=0)
        assert count == 0

    def test_returns_correct_count(self, crm_db, store):
        """Returns the number of purged entities."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        for i in range(3):
            _insert_person(crm_db, f"person-old-{i}", f"Old Person {i}",
                           hidden=True, hidden_at=old_date,
                           hidden_reason="merged_into:person-primary")

        count = store.purge_hidden(older_than_days=90)
        assert count == 3

    def test_cleans_up_lookup_tables(self, crm_db, store):
        """Purge removes lookup table entries for the entity."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        _insert_person(crm_db, "person-old", "Old Person",
                       emails=["old@test.com"], phones=["+15551234567"],
                       hidden=True, hidden_at=old_date,
                       hidden_reason="merged_into:person-primary")

        store.purge_hidden(older_than_days=90)

        conn = sqlite3.connect(crm_db)
        assert conn.execute(
            "SELECT COUNT(*) FROM person_emails WHERE person_id = 'person-old'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM person_phones WHERE person_id = 'person-old'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM person_names WHERE person_id = 'person-old'"
        ).fetchone()[0] == 0
        conn.close()


class TestMergeSoftDelete:
    """Tests for soft delete behavior in merge_people.py."""

    def test_merge_soft_deletes_secondary(self, crm_db):
        """After merge, secondary is hidden with correct reason, not hard-deleted."""
        primary_id = "person-primary"
        secondary_id = "person-secondary"
        _insert_person(crm_db, primary_id, "Alice Smith", emails=["alice@test.com"])
        _insert_person(crm_db, secondary_id, "Alice S", emails=["alices@test.com"])

        conn = sqlite3.connect(crm_db)
        # Simulate the soft-delete step of merge
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE person_entities SET hidden = 1, hidden_at = ?, hidden_reason = ? WHERE id = ?",
            (now_iso, f"merged_into:{primary_id}", secondary_id),
        )
        conn.execute("DELETE FROM person_emails WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_phones WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_names WHERE person_id = ?", (secondary_id,))
        conn.commit()

        # Verify row still exists but is hidden
        row = conn.execute(
            "SELECT hidden, hidden_at, hidden_reason, canonical_name FROM person_entities WHERE id = ?",
            (secondary_id,),
        ).fetchone()
        assert row is not None, "Secondary row should still exist"
        assert row[0] == 1, "Should be hidden"
        assert row[1] is not None, "hidden_at should be set"
        assert row[2] == f"merged_into:{primary_id}"
        assert row[3] == "Alice S", "Name should be preserved"
        conn.close()

    def test_merge_preserves_secondary_data(self, crm_db):
        """Soft-deleted secondary retains its original emails/phones/name."""
        secondary_id = "person-secondary"
        _insert_person(crm_db, secondary_id, "Bob Jones",
                       emails=["bob@test.com", "bobby@test.com"],
                       phones=["+15559876543"])

        conn = sqlite3.connect(crm_db)
        conn.execute(
            "UPDATE person_entities SET hidden = 1, hidden_at = datetime('now'), hidden_reason = 'merged_into:p1' WHERE id = ?",
            (secondary_id,),
        )
        conn.commit()

        row = conn.execute(
            "SELECT emails, phone_numbers, canonical_name FROM person_entities WHERE id = ?",
            (secondary_id,),
        ).fetchone()
        emails = json.loads(row[0])
        phones = json.loads(row[1])
        assert "bob@test.com" in emails
        assert "bobby@test.com" in emails
        assert "+15559876543" in phones
        assert row[2] == "Bob Jones"
        conn.close()

    def test_merge_clears_lookup_tables(self, crm_db):
        """Merge removes lookup table entries for the secondary."""
        secondary_id = "person-secondary"
        _insert_person(crm_db, secondary_id, "Bob Jones",
                       emails=["bob@test.com"], phones=["+15559876543"])

        conn = sqlite3.connect(crm_db)
        # Simulate merge soft-delete
        conn.execute(
            "UPDATE person_entities SET hidden = 1 WHERE id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_emails WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_phones WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_names WHERE person_id = ?", (secondary_id,))
        conn.commit()

        assert conn.execute(
            "SELECT COUNT(*) FROM person_emails WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM person_phones WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM person_names WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] == 0
        conn.close()

    def test_merge_does_not_blocklist_secondary(self, crm_db):
        """Merge soft-delete does NOT add secondary's identifiers to blocklist."""
        secondary_id = "person-secondary"
        _insert_person(crm_db, secondary_id, "Bob Jones",
                       emails=["bob@test.com"])

        conn = sqlite3.connect(crm_db)
        # Simulate merge: only soft-delete, no blocklist
        conn.execute(
            "UPDATE person_entities SET hidden = 1 WHERE id = ?", (secondary_id,))
        conn.commit()

        assert conn.execute(
            "SELECT COUNT(*) FROM person_blocklist WHERE identifier = 'bob@test.com'"
        ).fetchone()[0] == 0
        conn.close()


class TestUndoMerge:
    """Tests for the undo_merge script."""

    def test_undo_restores_secondary(self, crm_db, tmp_path):
        """Undo merge restores the secondary entity to visible state."""
        primary_id = "person-primary"
        secondary_id = "person-secondary"
        _insert_person(crm_db, primary_id, "Alice Smith",
                       emails=["alice@test.com", "alices@test.com"])
        _insert_person(crm_db, secondary_id, "Alice S",
                       emails=["alices@test.com"],
                       hidden=True, hidden_at=datetime.now(timezone.utc).isoformat(),
                       hidden_reason=f"merged_into:{primary_id}")

        # Remove secondary's lookup tables (as merge does)
        conn = sqlite3.connect(crm_db)
        conn.execute("DELETE FROM person_emails WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_phones WHERE person_id = ?", (secondary_id,))
        conn.execute("DELETE FROM person_names WHERE person_id = ?", (secondary_id,))
        conn.commit()
        conn.close()

        # Create minimal interactions db
        int_path = str(tmp_path / "interactions.db")
        int_conn = sqlite3.connect(int_path)
        int_conn.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, person_id TEXT, source_id TEXT, source_type TEXT, title TEXT, timestamp TEXT)")
        int_conn.commit()
        int_conn.close()

        merged_ids_file = tmp_path / "merged_person_ids.json"
        merged_ids_file.write_text(json.dumps({secondary_id: primary_id}))

        with patch("scripts.undo_merge.get_crm_db_path", return_value=crm_db), \
             patch("scripts.undo_merge.get_interaction_db_path", return_value=int_path), \
             patch("scripts.undo_merge.MERGED_IDS_FILE", merged_ids_file), \
             patch("scripts.undo_merge.get_person_entity_store") as mock_store_fn, \
             patch("api.services.person_stats.refresh_person_stats"):

            mock_store = PersonEntityStore(db_path=crm_db)
            mock_store_fn.return_value = mock_store

            from scripts.undo_merge import undo_merge
            undo_merge(secondary_id, dry_run=False)

        # Verify secondary is restored
        conn = sqlite3.connect(crm_db)
        row = conn.execute(
            "SELECT hidden, hidden_reason FROM person_entities WHERE id = ?",
            (secondary_id,)).fetchone()
        assert row[0] == 0, "Secondary should no longer be hidden"
        assert row[1] == ""

        # Verify lookup tables rebuilt
        assert conn.execute(
            "SELECT COUNT(*) FROM person_names WHERE person_id = ?",
            (secondary_id,)).fetchone()[0] > 0
        conn.close()

        # Verify merge chain entry removed
        remaining = json.loads(merged_ids_file.read_text())
        assert secondary_id not in remaining

    def test_undo_dry_run_makes_no_changes(self, crm_db, tmp_path):
        """Dry run reports but doesn't modify anything."""
        primary_id = "person-primary"
        secondary_id = "person-secondary"
        _insert_person(crm_db, primary_id, "Alice Smith",
                       emails=["alice@test.com"])
        _insert_person(crm_db, secondary_id, "Alice S",
                       emails=["alices@test.com"],
                       hidden=True, hidden_at=datetime.now(timezone.utc).isoformat(),
                       hidden_reason=f"merged_into:{primary_id}")

        int_path = str(tmp_path / "interactions.db")
        int_conn = sqlite3.connect(int_path)
        int_conn.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, person_id TEXT, source_id TEXT, source_type TEXT, title TEXT, timestamp TEXT)")
        int_conn.commit()
        int_conn.close()

        with patch("scripts.undo_merge.get_crm_db_path", return_value=crm_db), \
             patch("scripts.undo_merge.get_interaction_db_path", return_value=int_path), \
             patch("scripts.undo_merge.get_person_entity_store") as mock_store_fn:

            mock_store = PersonEntityStore(db_path=crm_db)
            mock_store_fn.return_value = mock_store

            from scripts.undo_merge import undo_merge
            undo_merge(secondary_id, dry_run=True)

        # Verify no changes made
        conn = sqlite3.connect(crm_db)
        row = conn.execute(
            "SELECT hidden FROM person_entities WHERE id = ?",
            (secondary_id,)).fetchone()
        assert row[0] == 1, "Secondary should still be hidden after dry run"
        conn.close()

    def test_undo_fails_on_purged_entity(self, crm_db, tmp_path):
        """Graceful error when secondary was already hard-deleted."""
        int_path = str(tmp_path / "interactions.db")
        int_conn = sqlite3.connect(int_path)
        int_conn.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, person_id TEXT, source_id TEXT)")
        int_conn.commit()
        int_conn.close()

        with patch("scripts.undo_merge.get_crm_db_path", return_value=crm_db), \
             patch("scripts.undo_merge.get_interaction_db_path", return_value=int_path):

            from scripts.undo_merge import undo_merge
            stats = undo_merge("nonexistent-id", dry_run=False)

        assert stats["source_entities_moved"] == 0
        assert stats["interactions_moved"] == 0
