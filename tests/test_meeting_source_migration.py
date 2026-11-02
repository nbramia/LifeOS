"""Tests for the meeting source type, its provider field, and the store migration."""
import json
import logging
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services import meeting_source as ms
from api.services.interaction_store import Interaction, InteractionStore, create_vault_interaction
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.source_entity import SourceEntity, SourceEntityStore

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent

INTERACTIONS_SCHEMA = """
CREATE TABLE interactions (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    snippet TEXT,
    source_link TEXT,
    source_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_account TEXT,
    attendee_count INTEGER
);
CREATE UNIQUE INDEX idx_interactions_source_unique ON interactions(source_type, source_id);
CREATE INDEX idx_interactions_source ON interactions(source_type, source_id);
"""

SOURCE_ENTITIES_SCHEMA = """
CREATE TABLE source_entities (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT,
    observed_name TEXT,
    observed_email TEXT,
    observed_phone TEXT,
    metadata TEXT,
    canonical_person_id TEXT,
    link_confidence REAL DEFAULT 0.0,
    link_status TEXT DEFAULT 'auto',
    linked_at TIMESTAMP,
    observed_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    match_attempted_at TIMESTAMP,
    match_attempt_count INTEGER DEFAULT 0,
    link_method TEXT,
    UNIQUE(source_type, source_id)
);
CREATE INDEX idx_source_entities_source_type ON source_entities(source_type);
"""

TS = "2026-03-04T10:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any test here that tries to reach a server, such as a live Chroma or API."""

    def refuse(self, address):
        raise AssertionError(f"this test tried to connect to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def _write_note(path: Path, frontmatter: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\nSynthetic body.\n")
    return str(path)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "Vault"
    root.mkdir()
    _write_note(root / "Other" / "unrelated.md", "title: unrelated")
    return root


def _interactions_db(tmp_path, rows=()):
    db = str(tmp_path / "interactions.db")
    conn = sqlite3.connect(db)
    conn.executescript(INTERACTIONS_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    for row_id, person, source_type, source_id in rows:
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_link, "
            "source_id, created_at) VALUES (?, ?, ?, ?, 'Synthetic', 'obsidian://x', ?, ?)",
            (row_id, person, TS, source_type, source_id, TS),
        )
    conn.commit()
    conn.close()
    return db


def _crm_db(tmp_path, rows=(), people=()):
    db = str(tmp_path / "crm.db")
    # Creates the person tables at their real shape, then seeds the older tables.
    PersonEntityStore(db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(f"DROP TABLE IF EXISTS {ms.PERSON_SOURCES_MARKER_TABLE}")
    conn.executescript(SOURCE_ENTITIES_SCHEMA)
    for row_id, person, source_type, source_id, *status in rows:
        conn.execute(
            "INSERT INTO source_entities (id, source_type, source_id, observed_name, metadata, "
            "canonical_person_id, link_status, observed_at, created_at) "
            "VALUES (?, ?, ?, 'Alex Example', '{\"is_granola\": true}', ?, ?, ?, ?)",
            (row_id, source_type, source_id, person, status[0] if status else "auto", TS, TS),
        )
    for person_id, sources in people:
        conn.execute(
            "INSERT INTO person_entities (id, canonical_name, display_name, sources, company) "
            "VALUES (?, 'alex example', 'Alex Example', ?, 'Synthetic Co')",
            (person_id, json.dumps(sources)),
        )
    conn.commit()
    conn.close()
    return db


def _dump(db, table):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {row["id"]: dict(row) for row in conn.execute(f"SELECT * FROM {table}")}
    finally:
        conn.close()


def _traced_statements(monkeypatch):
    """Record every statement the migration module sends to SQLite."""
    statements = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(ms.sqlite3, "connect", connect)
    return statements


class TestProviderDerivation:
    def test_wispr_id_gives_wispr(self):
        assert ms.meeting_provider({"wispr_id": "w-1"}) == "wispr"

    def test_granola_id_gives_granola(self):
        assert ms.meeting_provider({"granola_id": "g-1"}) == "granola"

    def test_neither_gives_none(self):
        assert ms.meeting_provider({"title": "x"}) is None

    def test_wispr_wins_when_both_are_present(self):
        assert ms.meeting_provider({"granola_id": "g", "wispr_id": "w"}) == "wispr"

    def test_frontmatter_keys_survive_malformed_yaml(self, tmp_path):
        note = Path(_write_note(tmp_path / "n.md", "title: a: b: [unclosed\nwispr_id: w-1"))
        assert "wispr_id" in ms._frontmatter_keys(note)

    def test_frontmatter_keys_ignore_the_body(self, tmp_path):
        note = tmp_path / "n.md"
        note.write_text("---\ntitle: x\n---\nwispr_id: in the body\n")
        assert "wispr_id" not in ms._frontmatter_keys(note)

    @pytest.mark.parametrize(
        "source_id,expected",
        [
            ("/v/a.md", "/v/a.md"),
            ("/v/a.md:Alex Example", "/v/a.md"),
            ("/v/a: b.md:Alex Example", "/v/a: b.md"),
            ("/v/a.md#dup-row1", "/v/a.md"),
            ("/v/a.md:Alex Example#dup-row1", "/v/a.md"),
            (None, None),
        ],
    )
    def test_note_path_from_source_id(self, source_id, expected):
        assert ms.note_path_from_source_id(source_id) == expected


class TestRowMigration:
    def _seed(self, tmp_path, vault):
        wispr = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        granola = _write_note(vault / "Work" / "g.md", "granola_id: g-1")
        neither = _write_note(vault / "Granola" / "n.md", "title: x")
        gone = str(vault / "Work" / "deleted.md")
        return wispr, granola, neither, gone, _interactions_db(
            tmp_path,
            [
                ("i1", "p1", "granola", wispr),
                ("i2", "p2", "granola", granola),
                ("i3", "p3", "granola", neither),
                ("i4", "p4", "granola", gone),
                ("i5", "p5", "granola", None),
                ("v1", "p1", "vault", "/synthetic/vault.md"),
                ("m1", "p1", "gmail", "msg-1"),
            ],
        )

    def test_no_granola_rows_remain_and_counts_hold(self, tmp_path, vault):
        *_, db = self._seed(tmp_path, vault)
        before = _dump(db, "interactions")
        ms.migrate_interactions_db(db, vault)
        after = _dump(db, "interactions")
        assert len(after) == len(before)
        assert sum(r["source_type"] == "granola" for r in after.values()) == 0
        assert sum(r["source_type"] == "meeting" for r in after.values()) == 5

    def test_ids_links_timestamps_and_source_ids_are_stable(self, tmp_path, vault):
        *_, db = self._seed(tmp_path, vault)
        before = _dump(db, "interactions")
        ms.migrate_interactions_db(db, vault)
        after = _dump(db, "interactions")
        assert set(after) == set(before)
        for row_id, row in before.items():
            for column in ("person_id", "timestamp", "created_at", "source_id", "title", "source_link"):
                assert after[row_id][column] == row[column]

    def test_unrelated_rows_are_untouched(self, tmp_path, vault):
        *_, db = self._seed(tmp_path, vault)
        ms.migrate_interactions_db(db, vault)
        after = _dump(db, "interactions")
        assert after["v1"]["source_type"] == "vault" and after["v1"]["provider"] is None
        assert after["m1"]["source_type"] == "gmail" and after["m1"]["provider"] is None

    def test_providers_come_from_frontmatter_with_granola_as_fallback(self, tmp_path, vault):
        *_, db = self._seed(tmp_path, vault)
        ms.migrate_interactions_db(db, vault)
        after = _dump(db, "interactions")
        assert after["i1"]["provider"] == "wispr"
        assert after["i2"]["provider"] == "granola"
        assert after["i3"]["provider"] == "granola"  # note exists, names neither
        assert after["i4"]["provider"] == "granola"  # note is missing
        assert after["i5"]["provider"] == "granola"  # no source_id at all

    def test_path_from_another_machine_is_found_under_this_vault(self, tmp_path, vault):
        _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        foreign = f"/Users/someone/{vault.name}/Work/w.md"
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", foreign)])
        ms.migrate_interactions_db(db, vault)
        row = _dump(db, "interactions")["i1"]
        assert row["provider"] == "wispr"
        assert row["source_id"] == foreign

    def test_vault_name_repeated_in_the_path_still_finds_the_note(self, tmp_path, vault):
        _write_note(vault / vault.name / "w.md", "wispr_id: w-1")
        db = _interactions_db(
            tmp_path, [("i1", "p1", "granola", f"/Users/someone/{vault.name}/{vault.name}/w.md")]
        )
        ms.migrate_interactions_db(db, vault)
        assert _dump(db, "interactions")["i1"]["provider"] == "wispr"

    def test_provider_set_by_indexing_before_migration_stands(self, tmp_path, vault):
        gone = str(vault / "Work" / "gone.md")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", gone)])
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE interactions ADD COLUMN provider TEXT")
        conn.execute("UPDATE interactions SET provider = 'wispr'")
        conn.commit()
        conn.close()
        ms.migrate_interactions_db(db, vault)
        row = _dump(db, "interactions")["i1"]
        assert (row["source_type"], row["provider"]) == ("meeting", "wispr")

    def test_each_distinct_note_is_read_once(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _crm_db(
            tmp_path,
            [
                ("s1", "p1", "granola", f"{note}:Alex"),
                ("s2", "p2", "granola", f"{note}:Blair"),
                ("s3", "p3", "granola", f"{note}:Casey"),
            ],
        )
        real = ms._frontmatter_keys
        with patch.object(ms, "_frontmatter_keys", side_effect=real) as reader:
            ms.migrate_crm_db(db, vault)
        assert reader.call_count == 1
        assert {r["provider"] for r in _dump(db, "source_entities").values()} == {"wispr"}

    def test_files_are_read_with_no_write_lock_held(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        real = ms._frontmatter_keys

        def read_while_another_process_writes(path):
            other = sqlite3.connect(db, timeout=0)
            other.execute("UPDATE interactions SET title = 'Concurrent' WHERE id = 'i1'")
            other.commit()
            other.close()
            return real(path)

        with patch.object(ms, "_frontmatter_keys", side_effect=read_while_another_process_writes):
            ms.migrate_interactions_db(db, vault)
        row = _dump(db, "interactions")["i1"]
        assert row["title"] == "Concurrent"
        assert row["source_type"] == "meeting"

    def test_row_written_after_the_file_read_defers_then_completes(self, tmp_path, vault):
        first = _write_note(vault / "Work" / "a.md", "granola_id: g-1")
        late = _write_note(vault / "Work" / "b.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", first)])
        real = ms._read_providers

        def read_then_old_process_writes(paths, root):
            providers = real(paths, root)
            other = sqlite3.connect(db)
            other.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id) "
                "VALUES ('i2', 'p2', ?, 'granola', 'Late', ?)",
                (TS, late),
            )
            other.commit()
            other.close()
            return providers

        with patch.object(ms, "_read_providers", side_effect=read_then_old_process_writes):
            ms.migrate_interactions_db(db, vault)
        row = _dump(db, "interactions")["i2"]
        assert (row["source_type"], row["provider"]) == ("meeting", None)

        ms.migrate_interactions_db(db, vault)
        assert _dump(db, "interactions")["i2"]["provider"] == "wispr"


class TestIdempotence:
    def test_second_run_changes_nothing(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        assert ms.migrate_interactions_db(db, vault).did_work is True
        first = _dump(db, "interactions")
        assert ms.migrate_interactions_db(db, vault).did_work is False
        assert _dump(db, "interactions") == first

    def test_later_startups_do_no_scan_rewrite_or_file_read(self, tmp_path, vault, monkeypatch):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        crm = _crm_db(tmp_path, [("s1", "p1", "granola", f"{note}:Alex")], [("p1", ["granola"])])
        ms.migrate_interactions_db(interactions, vault)
        ms.migrate_crm_db(crm, vault)

        statements = _traced_statements(monkeypatch)
        with patch.object(ms, "_read_providers") as reader:
            ms.migrate_interactions_db(interactions, vault)
            ms.migrate_crm_db(crm, vault)
        reader.assert_not_called()
        joined = " ".join(statements).upper()
        for forbidden in ("BEGIN", "UPDATE", "INSERT", "DELETE", "CREATE", "ALTER", "LIKE"):
            assert forbidden not in joined, statements
        # The only row-table reads are the indexed existence checks.
        assert all("LIMIT 1" in s for s in statements if " FROM interactions" in s or " FROM source_entities" in s)

    def test_later_granola_row_from_an_unrestarted_process_is_picked_up(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        ms.migrate_interactions_db(db, vault)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id) "
            "VALUES ('i2', 'p2', ?, 'granola', 'Later', '/synthetic/later.md')",
            (TS,),
        )
        conn.commit()
        conn.close()
        ms.migrate_interactions_db(db, vault)
        assert _dump(db, "interactions")["i2"]["source_type"] == "meeting"


class TestCollisions:
    def test_same_person_merges_keeping_the_migrated_row(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "title: x")
        db = _interactions_db(
            tmp_path, [("old", "p1", "granola", note), ("new", "p1", "meeting", note)]
        )
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE interactions ADD COLUMN provider TEXT")
        conn.execute("UPDATE interactions SET provider = 'wispr' WHERE id = 'new'")
        conn.commit()
        conn.close()

        result = ms.migrate_interactions_db(db, vault)

        rows = _dump(db, "interactions")
        assert set(rows) == {"old"}
        assert rows["old"]["source_type"] == "meeting"
        assert rows["old"]["person_id"] == "p1"
        assert rows["old"]["provider"] == "wispr"
        assert result.collisions_merged == 1

    def test_different_people_both_keep_their_rows_and_links(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        db = _interactions_db(
            tmp_path, [("old", "p1", "granola", note), ("new", "p2", "meeting", note)]
        )

        result = ms.migrate_interactions_db(db, vault)

        rows = _dump(db, "interactions")
        assert rows["old"]["person_id"] == "p1" and rows["old"]["source_id"] == note
        assert rows["new"]["person_id"] == "p2"
        assert rows["new"]["source_id"] == f"{note}{ms.COLLISION_SUFFIX}new"
        assert {r["source_type"] for r in rows.values()} == {"meeting"}
        assert result.collisions_kept_apart == 1

    def test_source_entities_with_differing_links_survive(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        source_id = f"{note}:Alex"
        db = _crm_db(
            tmp_path,
            [("old", "p1", "granola", source_id), ("new", None, "meeting", source_id)],
        )
        ms.migrate_crm_db(db, vault)
        rows = _dump(db, "source_entities")
        assert rows["old"]["canonical_person_id"] == "p1"
        assert rows["old"]["source_id"] == source_id
        assert rows["new"]["canonical_person_id"] is None
        assert rows["new"]["source_id"] != source_id

    def test_a_confirmed_link_is_not_merged_away(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        source_id = f"{note}:Alex"
        db = _crm_db(
            tmp_path,
            [
                ("old", "p1", "granola", source_id, "auto"),
                ("new", "p1", "meeting", source_id, "confirmed"),
            ],
        )
        ms.migrate_crm_db(db, vault)
        rows = _dump(db, "source_entities")
        assert rows["new"]["link_status"] == "confirmed"
        assert rows["new"]["source_id"] == source_id
        assert rows["old"]["source_id"] == f"{source_id}{ms.COLLISION_SUFFIX}old"
        assert {r["source_type"] for r in rows.values()} == {"meeting"}
        assert rows["old"]["provider"] == "granola"

    def test_a_confirmed_legacy_link_keeps_the_canonical_source_id(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        source_id = f"{note}:Alex"
        db = _crm_db(
            tmp_path,
            [
                ("old", "p1", "granola", source_id, "confirmed"),
                ("new", "p2", "meeting", source_id, "auto"),
            ],
        )
        ms.migrate_crm_db(db, vault)
        rows = _dump(db, "source_entities")
        assert rows["old"]["source_id"] == source_id
        assert rows["new"]["source_id"] == f"{source_id}{ms.COLLISION_SUFFIX}new"

    def test_a_rejected_meeting_row_is_not_displaced_by_an_automatic_one(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        source_id = f"{note}:Alex"
        db = _crm_db(
            tmp_path,
            [
                ("old", "p1", "granola", source_id, "auto"),
                ("new", "p1", "meeting", source_id, "rejected"),
            ],
        )
        ms.migrate_crm_db(db, vault)
        rows = _dump(db, "source_entities")
        assert rows["new"]["source_id"] == source_id
        assert rows["old"]["source_id"] == f"{source_id}{ms.COLLISION_SUFFIX}old"

    def test_confirmed_beats_rejected_and_two_confirmed_keep_the_legacy_row(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        confirmed_over_rejected = f"{note}:Alex"
        both_confirmed = f"{note}:Blair"
        db = _crm_db(
            tmp_path,
            [
                ("o1", "p1", "granola", confirmed_over_rejected, "rejected"),
                ("n1", "p2", "meeting", confirmed_over_rejected, "confirmed"),
                ("o2", "p1", "granola", both_confirmed, "confirmed"),
                ("n2", "p2", "meeting", both_confirmed, "confirmed"),
            ],
        )
        ms.migrate_crm_db(db, vault)
        rows = _dump(db, "source_entities")
        assert rows["n1"]["source_id"] == confirmed_over_rejected
        assert rows["o2"]["source_id"] == both_confirmed

    def test_second_run_after_a_collision_changes_nothing(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        db = _interactions_db(
            tmp_path, [("old", "p1", "granola", note), ("new", "p2", "meeting", note)]
        )
        ms.migrate_interactions_db(db, vault)
        first = _dump(db, "interactions")
        ms.migrate_interactions_db(db, vault)
        assert _dump(db, "interactions") == first


class TestVaultUnavailable:
    def _db(self, tmp_path, note):
        return _interactions_db(tmp_path, [("i1", "p1", "granola", note)])

    def test_missing_vault_renames_and_defers_the_provider(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = self._db(tmp_path, note)
        result = ms.migrate_interactions_db(db, tmp_path / "not-mounted")
        row = _dump(db, "interactions")["i1"]
        assert (row["source_type"], row["provider"]) == ("meeting", None)
        assert result.providers_pending == 1

    def test_empty_vault_directory_counts_as_unavailable(self, tmp_path, vault):
        empty = tmp_path / "empty"
        empty.mkdir()
        db = self._db(tmp_path, "/synthetic/w.md")
        ms.migrate_interactions_db(db, empty)
        assert _dump(db, "interactions")["i1"]["provider"] is None

    def test_many_notes_with_none_found_counts_as_unavailable(self, tmp_path, vault):
        rows = [
            (f"i{n}", "p1", "granola", f"/elsewhere/{n}.md")
            for n in range(ms.UNTRUSTED_VAULT_MIN_NOTES)
        ]
        db = _interactions_db(tmp_path, rows)
        ms.migrate_interactions_db(db, vault)
        assert {r["provider"] for r in _dump(db, "interactions").values()} == {None}

    def test_backfill_finishes_once_the_vault_returns(self, tmp_path, vault):
        wispr = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        gone = str(vault / "Work" / "gone.md")
        db = _interactions_db(
            tmp_path, [("i1", "p1", "granola", wispr), ("i2", "p2", "granola", gone)]
        )
        ms.migrate_interactions_db(db, tmp_path / "not-mounted")

        # A note indexed in the meantime that names neither tool stays NULL.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id) "
            "VALUES ('i3', 'p3', ?, 'meeting', 'New', '/synthetic/new.md')",
            (TS,),
        )
        conn.commit()
        conn.close()

        ms.migrate_interactions_db(db, vault)

        rows = _dump(db, "interactions")
        assert rows["i1"]["provider"] == "wispr"
        assert rows["i2"]["provider"] == "granola"
        assert rows["i3"]["provider"] is None
        conn = sqlite3.connect(db)
        assert not ms._table_exists(conn, ms.BACKFILL_TABLE)
        conn.close()

    def test_deferred_row_displaced_by_a_collision_still_gets_its_notes_provider(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("first", "p1", "granola", note)])
        ms.migrate_interactions_db(db, tmp_path / "not-mounted")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id) "
            "VALUES ('later', 'p2', ?, 'granola', 'Later', ?)",
            (TS, note),
        )
        conn.commit()
        conn.close()

        ms.migrate_interactions_db(db, vault)

        rows = _dump(db, "interactions")
        assert rows["first"]["source_id"] == f"{note}{ms.COLLISION_SUFFIX}first"
        assert rows["first"]["provider"] == "wispr"
        assert rows["later"]["provider"] == "wispr"

    def test_partial_vault_labels_only_the_missing_notes_granola(self, tmp_path, vault):
        present = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        absent = str(vault / "Work" / "not-yet-synced.md")
        db = _interactions_db(
            tmp_path, [("i1", "p1", "granola", present), ("i2", "p2", "granola", absent)]
        )
        ms.migrate_interactions_db(db, vault)
        rows = _dump(db, "interactions")
        assert rows["i1"]["provider"] == "wispr"
        assert rows["i2"]["provider"] == "granola"

    def test_deferred_retry_takes_no_write_lock(self, tmp_path, vault, monkeypatch):
        db = self._db(tmp_path, "/synthetic/w.md")
        ms.migrate_interactions_db(db, tmp_path / "not-mounted")
        statements = _traced_statements(monkeypatch)
        result = ms.migrate_interactions_db(db, tmp_path / "not-mounted")
        assert result.providers_pending == 1
        assert not any("BEGIN" in s.upper() for s in statements)


class TestFailClosed:
    def test_failure_rolls_the_whole_database_back(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _crm_db(
            tmp_path, [("s1", "p1", "granola", f"{note}:Alex")], [("p1", ["granola", "vault"])]
        )
        rows_before = _dump(db, "source_entities")
        people_before = _dump(db, "person_entities")
        with patch.object(ms, "_migrate_person_sources", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                ms.migrate_crm_db(db, vault)
        rows_after = _dump(db, "source_entities")
        assert rows_after["s1"]["source_type"] == rows_before["s1"]["source_type"] == "granola"
        assert rows_after["s1"]["provider"] is None
        assert _dump(db, "person_entities") == people_before

    def test_locked_database_raises_and_leaves_data_unchanged(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE interactions ADD COLUMN provider TEXT")
        conn.commit()
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError):
                ms.migrate_interactions_db(db, vault, busy_timeout=0.05)
            assert _dump(db, "interactions")["i1"]["source_type"] == "granola"
        finally:
            holder.execute("ROLLBACK")
            holder.close()
            conn.close()
        ms.migrate_interactions_db(db, vault)
        assert _dump(db, "interactions")["i1"]["source_type"] == "meeting"

    def test_store_init_survives_failure_and_logs_loudly(self, tmp_path, vault, caplog):
        db = _interactions_db(tmp_path, [("i1", "p1", "granola", "/synthetic/w.md")])
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")), \
                patch.object(ms, "_default_vault_root", return_value=vault), \
                caplog.at_level(logging.ERROR):
            store = InteractionStore(db_path=db, strict=False)
        assert "MEETING SOURCE MIGRATION FAILED" in caplog.text
        assert store.get_by_id("i1").source_type == "meeting"


class TestFailureAlert:
    @pytest.fixture
    def failing_db(self, tmp_path):
        return _interactions_db(tmp_path, [("i1", "p1", "granola", "/synthetic/w.md")])

    def _fail(self, db, vault):
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")), \
                patch.object(ms, "_default_vault_root", return_value=vault):
            ms.run_at_store_init(ms.migrate_interactions_db, db)

    def test_failure_sends_one_alert_across_processes_and_restarts(self, failing_db, vault):
        with patch("api.services.telegram.send_message", return_value=True) as send:
            for _ in range(5):
                self._fail(failing_db, vault)
        assert send.call_count == 1
        message = send.call_args.args[0]
        assert "MEETING SOURCE MIGRATION FAILED" in message
        assert "synthetic" not in message

    def test_each_database_has_its_own_window(self, tmp_path, failing_db, vault):
        other = tmp_path / "other"
        other.mkdir()
        second = _interactions_db(other, [("i1", "p1", "granola", "/synthetic/w.md")])
        with patch("api.services.telegram.send_message", return_value=True) as send:
            self._fail(failing_db, vault)
            self._fail(second, vault)
        assert send.call_count == 2

    def test_alert_returns_after_the_window(self, failing_db, vault):
        with patch("api.services.telegram.send_message", return_value=True) as send:
            self._fail(failing_db, vault)
            marker = f"{failing_db}{ms.ALERT_MARKER_SUFFIX}"
            expired = Path(marker).stat().st_mtime - ms.ALERT_WINDOW_SECONDS - 1
            import os

            os.utime(marker, (expired, expired))
            self._fail(failing_db, vault)
        assert send.call_count == 2

    @pytest.mark.parametrize(
        "behavior", [{"side_effect": RuntimeError("telegram down")}, {"return_value": False}]
    )
    def test_a_failed_send_never_raises_and_gives_the_window_back(self, failing_db, vault, behavior):
        with patch("api.services.telegram.send_message", **behavior) as send:
            self._fail(failing_db, vault)
            self._fail(failing_db, vault)
        assert send.call_count == 2
        assert not Path(f"{failing_db}{ms.ALERT_MARKER_SUFFIX}").exists()

    def test_an_unwritable_marker_never_raises(self, failing_db, vault):
        with patch.object(ms.os, "open", side_effect=PermissionError("read-only")), \
                patch("api.services.telegram.send_message") as send:
            self._fail(failing_db, vault)
        send.assert_not_called()

    def test_a_hung_send_does_not_hold_up_startup(self, failing_db, vault, monkeypatch):
        import threading
        import time as time_module

        release = threading.Event()
        monkeypatch.setattr(ms, "ALERT_SEND_WAIT_SECONDS", 0.05)
        with patch("api.services.telegram.send_message", side_effect=lambda _: release.wait(5)):
            started = time_module.monotonic()
            self._fail(failing_db, vault)
            elapsed = time_module.monotonic() - started
            release.set()
        assert elapsed < 2

    def test_a_deferred_provider_does_not_alert(self, tmp_path, failing_db):
        with patch("api.services.telegram.send_message") as send, \
                patch.object(ms, "_default_vault_root", return_value=tmp_path / "not-mounted"):
            ms.run_at_store_init(ms.migrate_interactions_db, failing_db)
            ms.run_at_store_init(ms.migrate_interactions_db, failing_db)
        send.assert_not_called()
        assert _dump(failing_db, "interactions")["i1"]["source_type"] == "meeting"

    def test_success_does_not_alert(self, failing_db, vault):
        with patch("api.services.telegram.send_message") as send, \
                patch.object(ms, "_default_vault_root", return_value=vault):
            ms.run_at_store_init(ms.migrate_interactions_db, failing_db)
        send.assert_not_called()


class TestMigrationStatus:
    """Raw counts must tell migrated from failed, which folded reads can't."""

    def test_a_failed_migration_shows_legacy_rows(self, tmp_path, vault):
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", "/synthetic/w.md")])
        crm = _crm_db(tmp_path, [("s1", "p1", "granola", "/synthetic/w.md:Alex")], [("p1", ["granola"])])
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")):
            InteractionStore(db_path=interactions, strict=False)
        status = ms.migration_status(interactions, crm)
        assert status["interactions"]["state"] == "not_migrated"
        assert status["interactions"]["legacy_rows"] == 1
        assert status["source_entities"]["legacy_rows"] == 1
        assert status["source_entities"]["person_sources_legacy"] == 1

    def test_a_deferred_provider_is_its_own_state(self, tmp_path, vault):
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", "/synthetic/w.md")])
        crm = _crm_db(tmp_path)
        ms.migrate_interactions_db(interactions, tmp_path / "not-mounted")
        status = ms.migration_status(interactions, crm)["interactions"]
        assert status == {
            "state": "provider_deferred",
            "legacy_rows": 0,
            "meeting_rows": 1,
            "null_provider": 1,
            "provider_backfill_pending": 1,
        }

    def test_a_complete_migration(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        crm = _crm_db(tmp_path, [("s1", "p1", "granola", f"{note}:Alex")], [("p1", ["granola"])])
        ms.migrate_interactions_db(interactions, vault)
        ms.migrate_crm_db(crm, vault)
        status = ms.migration_status(interactions, crm)
        assert status["interactions"]["state"] == "complete"
        assert status["interactions"]["null_provider"] == 0
        assert status["source_entities"]["state"] == "complete"
        assert status["source_entities"]["person_sources_legacy"] == 0

    def test_a_store_not_yet_created_or_without_the_column_is_reported(self, tmp_path):
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", "/n.md")])
        status = ms.migration_status(interactions, str(tmp_path / "absent.db"))
        assert status["source_entities"] == {"state": "unavailable"}
        assert not (tmp_path / "absent.db").exists()
        assert status["interactions"]["null_provider"] is None


class TestConcurrentInitializers:
    def test_parallel_processes_converge(self, tmp_path, vault):
        rows = []
        for n in range(200):
            note = _write_note(vault / "Work" / f"{n}.md", "wispr_id: w" if n % 2 else "granola_id: g")
            rows.append((f"s{n}", "p1", "granola", f"{note}:Alex"))
        db = _crm_db(tmp_path, rows, [("p1", ["granola", "gmail"]), ("p2", ["meeting", "granola"])])
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ms', {str(REPO_ROOT / 'api/services/meeting_source.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['ms'] = m\n"
            "spec.loader.exec_module(m)\n"
            f"m.migrate_crm_db({db!r}, __import__('pathlib').Path({str(vault)!r}))\n"
        )
        processes = [
            subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE) for _ in range(6)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=120)
            assert process.returncode == 0, stderr.decode()

        after = _dump(db, "source_entities")
        assert len(after) == 200
        assert {r["source_type"] for r in after.values()} == {"meeting"}
        assert sum(r["provider"] == "wispr" for r in after.values()) == 100
        people = _dump(db, "person_entities")
        assert json.loads(people["p1"]["sources"]) == ["meeting", "gmail"]
        assert json.loads(people["p2"]["sources"]) == ["meeting"]

    def test_add_column_race_is_tolerated(self, tmp_path):
        db = _interactions_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE interactions ADD COLUMN provider TEXT")
        conn.commit()

        class StaleInfo:
            """Reports the column as absent, as a racing process would have seen it."""

            def execute(self, sql, *args):
                return [] if sql.startswith("PRAGMA table_info") else conn.execute(sql, *args)

        ms.ensure_provider_column(StaleInfo(), "interactions")
        conn.close()


class TestPersonSources:
    def test_granola_becomes_meeting_without_duplicates(self, tmp_path, vault):
        db = _crm_db(
            tmp_path,
            people=[
                ("p1", ["gmail", "granola", "vault"]),
                ("p2", ["granola", "meeting"]),
                ("p3", ["meeting", "granola", "slack"]),
                ("p4", ["gmail"]),
            ],
        )
        ms.migrate_crm_db(db, vault)
        people = _dump(db, "person_entities")
        assert json.loads(people["p1"]["sources"]) == ["gmail", "meeting", "vault"]
        assert json.loads(people["p2"]["sources"]) == ["meeting"]
        assert json.loads(people["p3"]["sources"]) == ["meeting", "slack"]
        assert json.loads(people["p4"]["sources"]) == ["gmail"]

    def test_only_the_sources_column_changes(self, tmp_path, vault):
        db = _crm_db(tmp_path, people=[("p1", ["granola"])])
        before = _dump(db, "person_entities")["p1"]
        ms.migrate_crm_db(db, vault)
        after = _dump(db, "person_entities")["p1"]
        changed = {column for column in before if before[column] != after[column]}
        assert changed == {"sources"}

    def test_either_crm_store_migrates_people(self, tmp_path, vault):
        db = _crm_db(tmp_path, [("s1", "p1", "granola", "/synthetic/a.md:Alex")], [("p1", ["granola"])])
        with patch.object(ms, "_default_vault_root", return_value=vault):
            PersonEntityStore(db_path=db)
        assert json.loads(_dump(db, "person_entities")["p1"]["sources"]) == ["meeting"]
        assert _dump(db, "source_entities")["s1"]["source_type"] == "meeting"

    def test_a_store_holding_a_stale_entity_cannot_restore_the_old_name(self, tmp_path, vault):
        db = _crm_db(tmp_path, people=[("p1", ["granola", "gmail"])])
        store = PersonEntityStore(db_path=db)
        stale = store.get_by_id("p1")
        stale.sources = ["granola", "gmail", "slack"]  # as a process loaded before the migration
        ms.migrate_crm_db(db, vault)
        store.update(stale)
        assert json.loads(_dump(db, "person_entities")["p1"]["sources"]) == ["meeting", "gmail", "slack"]

    def test_a_concurrent_update_to_the_person_is_kept(self, tmp_path, vault):
        db = _crm_db(tmp_path, [("s1", "p1", "granola", "/synthetic/a.md:Alex")], [("p1", ["granola"])])
        real = ms._read_providers

        def another_process_updates_the_person(paths, root):
            other = sqlite3.connect(db)
            other.execute(
                "UPDATE person_entities SET sources = ?, company = 'Updated Co' WHERE id = 'p1'",
                (json.dumps(["granola", "photos"]),),
            )
            other.commit()
            other.close()
            return real(paths, root)

        with patch.object(ms, "_read_providers", side_effect=another_process_updates_the_person):
            ms.migrate_crm_db(db, vault)
        person = _dump(db, "person_entities")["p1"]
        assert json.loads(person["sources"]) == ["meeting", "photos"]
        assert person["company"] == "Updated Co"

    def test_unparseable_sources_are_left_alone_without_failing(self, tmp_path, vault):
        db = _crm_db(tmp_path, [("s1", "p1", "granola", "/synthetic/a.md:Alex")])
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO person_entities (id, canonical_name, display_name, sources) "
            """VALUES ('bad', 'x', 'x', '["granola"')"""
        )
        conn.commit()
        conn.close()
        ms.migrate_crm_db(db, vault)
        assert _dump(db, "source_entities")["s1"]["source_type"] == "meeting"
        assert _dump(db, "person_entities")["bad"]["sources"] == '["granola"'


class TestRollback:
    def test_rollback_restores_labels_and_a_later_migration_reapplies(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        interactions = _interactions_db(tmp_path, [("i1", "p1", "granola", note)])
        crm = _crm_db(tmp_path, [("s1", "p1", "granola", f"{note}:Alex")], [("p1", ["granola", "gmail"])])
        ms.migrate_interactions_db(interactions, vault)
        ms.migrate_crm_db(crm, vault)

        assert ms.rollback_interactions_db(interactions) == 1
        assert ms.rollback_crm_db(crm) == 1
        assert _dump(interactions, "interactions")["i1"]["source_type"] == "granola"
        assert _dump(crm, "source_entities")["s1"]["source_type"] == "granola"
        assert json.loads(_dump(crm, "person_entities")["p1"]["sources"]) == ["granola", "gmail"]

        ms.migrate_crm_db(crm, vault)
        assert json.loads(_dump(crm, "person_entities")["p1"]["sources"]) == ["meeting", "gmail"]

    def test_rollback_conflict_changes_nothing(self, tmp_path):
        db = _interactions_db(
            tmp_path, [("a", "p1", "meeting", "/n.md"), ("b", "p1", "granola", "/n.md")]
        )
        with pytest.raises(sqlite3.IntegrityError):
            ms.rollback_interactions_db(db)
        assert _dump(db, "interactions")["a"]["source_type"] == "meeting"


class TestCommandLine:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "migrate_meeting_source_type.py"), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_migrates_named_databases_then_rolls_back(self, tmp_path, vault):
        note = _write_note(vault / "Work" / "w.md", "wispr_id: w-1")
        interactions = _interactions_db(
            tmp_path, [("i1", "p1", "granola", note), ("v1", "p1", "vault", "/n.md")]
        )
        crm = _crm_db(tmp_path, [("s1", "p1", "granola", f"{note}:Alex")], [("p1", ["granola"])])
        conn = sqlite3.connect(interactions)
        conn.execute("ALTER TABLE interactions ADD COLUMN provider TEXT")
        conn.commit()
        conn.close()

        done = self._run("--interactions-db", interactions, "--crm-db", crm, "--vault", str(vault))
        assert done.returncode == 0, done.stderr
        assert "people whose meeting-row count changed: 0" in done.stdout
        assert "Alex" not in done.stdout and str(vault) not in done.stdout
        assert _dump(interactions, "interactions")["i1"]["provider"] == "wispr"

        undone = self._run("--interactions-db", interactions, "--crm-db", crm, "--rollback")
        assert undone.returncode == 0, undone.stderr
        assert _dump(crm, "source_entities")["s1"]["source_type"] == "granola"

    def test_requires_explicit_paths(self):
        assert self._run().returncode != 0

    def test_refuses_a_missing_database(self, tmp_path, vault):
        done = self._run(
            "--interactions-db", str(tmp_path / "no.db"), "--crm-db", str(tmp_path / "no2.db"),
            "--vault", str(vault),
        )
        assert done.returncode != 0
        assert not (tmp_path / "no.db").exists()


def _offline_indexer(tmp_path, vault_root, **stores):
    """A real IndexerService with its Chroma store stubbed, so no server is contacted."""
    from api.services.indexer import IndexerService

    with patch("api.services.indexer.VectorStore", MagicMock()):
        return IndexerService(
            vault_path=str(vault_root), db_path=str(tmp_path / "chroma"), **stores
        )


def _person_store_stub():
    stub = MagicMock()
    stub.get_canonical_id.side_effect = lambda person_id: person_id
    stub.get_by_id.return_value = MagicMock()
    return stub


class TestReadsAcrossAFailedMigration:
    """Stores still holding granola rows, as after a failed or unreached migration."""

    @pytest.fixture
    def stuck_store(self, tmp_path):
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        db = _interactions_db(tmp_path)
        conn = sqlite3.connect(db)
        for row_id, source_type, source_id in (
            ("g1", "granola", "/synthetic/g.md"),
            ("m1", "meeting", "/synthetic/m.md"),
            ("v1", "vault", "/synthetic/v.md"),
            ("e1", "gmail", "msg-1"),
        ):
            conn.execute(
                "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id, "
                "created_at) VALUES (?, 'p1', ?, ?, 'Synthetic', ?, ?)",
                (row_id, recent, source_type, source_id, recent),
            )
        conn.commit()
        conn.close()
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")):
            return InteractionStore(db_path=db, strict=False)

    def test_rows_read_as_meeting(self, stuck_store):
        assert stuck_store.get_by_id("g1").source_type == "meeting"

    @pytest.mark.parametrize("name", ["meeting", "granola", "vault,granola", "meeting,gmail"])
    def test_filters_see_both_labels(self, stuck_store, name):
        ids = {i.id for i in stuck_store.get_for_person("p1", source_type=name)}
        assert {"g1", "m1"} <= ids

    def test_counts_fold_into_meeting(self, stuck_store):
        counts = stuck_store.get_interaction_counts("p1")
        assert counts["meeting"] == 2
        assert "granola" not in counts

    def test_statistics_and_daily_counts_fold(self, stuck_store):
        assert stuck_store.get_statistics()["by_source"]["meeting"] == 2
        start = datetime.now(timezone.utc) - timedelta(days=30)
        end = datetime.now(timezone.utc)
        daily = stuck_store.get_daily_source_counts(start, end)
        assert sum(c for _, s, c in daily if s == "meeting") == 2
        assert not [s for _, s, _ in daily if s == "granola"]
        by_person = stuck_store.get_daily_person_source_counts(start, end)
        assert sum(c for _, _, s, c in by_person if s == "meeting") == 2

    def test_strength_inputs_and_recency_fold(self, stuck_store):
        buckets = stuck_store.get_strength_inputs("p1", days_back=30)["p1"].buckets
        assert {b.source_type for b in buckets} == {"meeting", "vault", "gmail"}
        assert set(stuck_store.get_last_interaction_by_source("p1")) == {"meeting", "vault", "gmail"}
        batched = stuck_store.get_last_interaction_by_source_batch(["p1"])["p1"]
        assert "granola" not in batched

    def test_weight_detail_sums_both_labels(self, stuck_store):
        detail = stuck_store.get_interaction_counts_with_subtypes("p1", days_back=30)
        meetings = [d for d in detail if d["source_type"] == "meeting"]
        assert [d["count"] for d in meetings] == [2]

    def test_reindexing_does_not_duplicate_a_granola_row(self, stuck_store):
        interaction = create_vault_interaction(
            person_id="p1",
            file_path="/synthetic/g.md",
            title="Synthetic",
            timestamp=datetime.now(timezone.utc),
            is_meeting=True,
            provider="wispr",
        )
        with patch(
            "api.services.person_entity.get_person_entity_store", return_value=_person_store_stub()
        ):
            existing, added = stuck_store.add_if_not_exists(interaction)
        assert added is False
        assert existing.id == "g1"
        assert existing.provider == "wispr"

    def test_replace_and_delete_reach_both_labels(self, stuck_store):
        with patch(
            "api.services.person_entity.get_person_entity_store", return_value=_person_store_stub()
        ):
            result = stuck_store.atomic_replace("meeting", [])
        assert result["deleted"] == 2
        assert stuck_store.get_by_id("g1") is None


class TestSourceEntityStore:
    @pytest.fixture
    def store(self, tmp_path, vault):
        db = _crm_db(
            tmp_path,
            [
                ("s1", "p1", "granola", "/synthetic/g.md:Alex"),
                ("s2", "p1", "gmail", "msg-1"),
                ("s3", None, "granola", "/synthetic/h.md:Blair"),
            ],
        )
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")):
            return SourceEntityStore(db_path=db)

    @pytest.mark.parametrize("name", ["meeting", "granola"])
    def test_filters_see_legacy_rows(self, store, name):
        assert [e.id for e in store.get_for_person("p1", source_type=name)] == ["s1"]
        assert [e.id for e in store.get_for_person("p1", source_type=name, limit=5)] == ["s1"]
        assert [e.id for e in store.get_unlinked(source_type=name)] == ["s3"]
        assert store.get_by_source(name, "/synthetic/g.md:Alex").id == "s1"

    def test_rows_read_as_meeting(self, store):
        assert store.get_by_id("s1").source_type == "meeting"
        assert store.get_statistics()["by_source"]["meeting"] == 2

    def test_updating_a_legacy_row_leaves_it_for_the_migration(self, store, vault):
        entity = store.get_by_id("s1")
        entity.observed_name = "Alex Updated"
        store.update(entity)
        assert _dump(store.db_path, "source_entities")["s1"]["source_type"] == "granola"
        assert store.get_by_id("s1").observed_name == "Alex Updated"

        ms.migrate_crm_db(store.db_path, vault)
        row = _dump(store.db_path, "source_entities")["s1"]
        assert (row["source_type"], row["provider"]) == ("meeting", "granola")

    def test_update_never_clears_a_stored_provider(self, tmp_path, vault):
        db = str(tmp_path / "se.db")
        store = SourceEntityStore(db_path=db)
        store.add(
            SourceEntity(
                id="s1", source_type="meeting", source_id="/n.md:Alex", provider="granola",
                observed_at=datetime.now(timezone.utc),
            ),
            validate_person=False,
        )
        store.add_or_update(
            SourceEntity(
                source_type="meeting", source_id="/n.md:Alex", observed_at=datetime.now(timezone.utc)
            ),
            validate_person=False,
        )
        assert store.get_by_id("s1").provider == "granola"


class TestFilterAliasAfterMigration:
    @pytest.fixture
    def store(self, tmp_path, vault):
        db = str(tmp_path / "fresh.db")
        store = InteractionStore(db_path=db, strict=False)
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        with patch(
            "api.services.person_entity.get_person_entity_store", return_value=_person_store_stub()
        ):
            store.add(Interaction(id="m1", person_id="p1", timestamp=recent, source_type="meeting",
                                  title="Synthetic", source_id="/m.md", provider="wispr"))
            store.add(Interaction(id="v1", person_id="p1", timestamp=recent, source_type="vault",
                                  title="Synthetic", source_id="/v.md"))
        return store

    def test_every_filter_gives_the_same_results(self, store):
        start = datetime.now(timezone.utc) - timedelta(days=30)
        end = datetime.now(timezone.utc)
        for name in ("granola", "meeting"):
            assert [i.id for i in store.get_for_person("p1", source_type=name)] == ["m1"]
            assert [i.id for i in store.get_for_person_in_range("p1", start, end, source_type=name)] == ["m1"]
            assert [i.id for i in store.get_all_in_range(start, end, source_type=name)] == ["m1"]
            assert sum(store.get_monthly_interaction_counts_in_range("p1", start, end, source_type=name).values()) == 1
            assert sum(c for _, _, c in store.get_daily_source_counts(start, end, source_types=[name])) == 1

    def test_writing_the_legacy_type_is_rejected(self):
        interaction = Interaction(
            id="x", person_id="p", timestamp=datetime.now(timezone.utc),
            source_type="granola", title="Synthetic",
        )
        with pytest.raises(ValueError):
            interaction.validate()

    def test_provider_round_trips(self, store):
        assert store.get_by_id("m1").provider == "wispr"
        start = datetime.now(timezone.utc) - timedelta(days=30)
        found = store.get_all_in_range(start, datetime.now(timezone.utc), source_type="meeting")
        assert found[0].provider == "wispr"


class TestIndexing:
    """A note indexed through the real stores gets the right type and provider."""

    @pytest.fixture
    def indexed(self, tmp_path):
        vault_root = tmp_path / "Vault"
        vault_root.mkdir()
        interactions = InteractionStore(db_path=str(tmp_path / "i.db"), strict=False)
        entities = SourceEntityStore(db_path=str(tmp_path / "c.db"))
        resolver = MagicMock()
        person = PersonEntity(id="p1", canonical_name="Alex Example", display_name="Alex Example")
        resolver.resolve.return_value = MagicMock(entity=person, confidence=1.0, match_type="exact")
        indexer = _offline_indexer(
            tmp_path,
            vault_root,
            entity_resolver=resolver,
            interaction_store=interactions,
            source_entity_store=entities,
        )

        def index(relative, frontmatter):
            path = vault_root / relative
            _write_note(path, f"{frontmatter}\npeople: [Alex Example]")
            indexer._sync_people_to_v2(
                path,
                ["Alex Example"],
                datetime(2026, 3, 4, tzinfo=timezone.utc),
                *_classify(path, frontmatter),
            )
            return (
                interactions.get_by_source("meeting", str(path.resolve())),
                entities.get_by_source("meeting", f"{path.resolve()}:Alex Example"),
                person,
            )

        def _classify(path, frontmatter):
            keys = {line.split(":")[0] for line in frontmatter.splitlines()}
            provider = ms.meeting_provider(keys)
            return provider is not None or "Granola" in str(path), provider

        with patch(
            "api.services.person_entity.get_person_entity_store", return_value=_person_store_stub()
        ), patch("api.services.interaction_store.TEMP_PREFIXES", ()):
            yield index

    @pytest.mark.parametrize(
        "relative,frontmatter,provider",
        [
            ("Work/w.md", "wispr_id: w-1", "wispr"),
            ("Work/g.md", "granola_id: g-1", "granola"),
            ("Granola/n.md", "title: x", None),
        ],
    )
    def test_type_and_provider(self, indexed, relative, frontmatter, provider):
        interaction, entity, person = indexed(relative, frontmatter)
        assert interaction.source_type == "meeting"
        assert interaction.provider == provider
        assert entity.source_type == "meeting"
        assert entity.provider == provider
        assert "meeting" in person.sources
        assert "granola" not in person.sources


class TestIndexFileClassification:
    @pytest.mark.parametrize(
        "relative,frontmatter,is_meeting,provider",
        [
            ("Work/w.md", "wispr_id: w-1", True, "wispr"),
            ("Work/g.md", "granola_id: g-1", True, "granola"),
            ("Granola/n.md", "title: x", True, None),
            ("Work/plain.md", "title: x", False, None),
        ],
    )
    def test_index_file_passes_classification_to_people_sync(
        self, tmp_path, relative, frontmatter, is_meeting, provider
    ):
        vault_root = tmp_path / "Vault"
        indexer = _offline_indexer(
            tmp_path,
            _mkdir(vault_root),
            entity_resolver=MagicMock(),
            interaction_store=MagicMock(),
            source_entity_store=MagicMock(),
        )
        note = vault_root / relative
        _write_note(note, f"{frontmatter}\npeople: [Alex Example]")
        with patch.object(indexer, "_sync_people_to_v2", return_value=set()) as sync:
            indexer.index_file(str(note))
        assert sync.call_args.args[3] is is_meeting
        assert sync.call_args.args[4] == provider


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestEquivalentBehavior:
    """Consumers treat meeting exactly as they treated the granola label."""

    def test_mention_count_includes_meeting(self):
        from api.services.person_stats import _apply_counts_to_entity

        entity = PersonEntity(canonical_name="Alex Example")
        _apply_counts_to_entity(entity, {"vault": 3, "meeting": 2})
        assert entity.mention_count == 5

    def test_mention_count_folds_a_legacy_count_and_keeps_sources_clean(self):
        from api.services.person_stats import _apply_counts_to_entity

        entity = PersonEntity(canonical_name="Alex Example")
        _apply_counts_to_entity(entity, {"vault": 3, "meeting": 2, "granola": 4})
        assert entity.mention_count == 9
        assert "granola" not in entity.sources

    def test_relationship_weight(self):
        from config.relationship_weights import get_interaction_weight

        assert get_interaction_weight("meeting") == 0.8

    def test_fact_extraction_priority(self):
        from api.services.person_facts import PersonFactExtractor

        assert "meeting" in PersonFactExtractor.PRIORITY_SOURCES

    def test_interaction_history_counts_meetings_as_notes(self, tmp_path):
        store = InteractionStore(db_path=str(tmp_path / "i.db"), strict=False)
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        with patch(
            "api.services.person_entity.get_person_entity_store", return_value=_person_store_stub()
        ):
            store.add(Interaction(id="m1", person_id="p1", timestamp=recent, source_type="meeting",
                                  title="Sync", source_link="obsidian://x", source_id="/m.md"))
        history = store.format_interaction_history("p1")
        assert "1 notes" in history
        assert "[[Sync]]" in history

    def test_link_override_matches_across_the_rename(self):
        from api.services.link_override import LinkOverride

        override = LinkOverride(
            id="o1", name_pattern="Alex", source_type="granola", context_pattern=None,
            preferred_person_id="p1", rejected_person_id=None, reason="", created_at=None,
        )
        assert override.matches("Alex", "meeting")

    def test_split_overrides_keep_vault_folder_context_for_meetings(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import split_person

        store = MagicMock()
        with patch.object(split_person, "get_link_override_store", return_value=store):
            created = split_person.create_link_overrides(
                "from-person",
                "to-person",
                [{
                    "observed_name": "Alex",
                    "source_type": "meeting",
                    "source_id": "/synthetic/Personal/n.md:Alex",
                }],
                dry_run=False,
            )
        assert created == 1
        override = store.add.call_args.args[0]
        assert override.source_type == "meeting"
        assert override.context_pattern == "Personal/"

    def test_consistency_check_leaves_a_kept_apart_row_alone(self, tmp_path, vault):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import sync_consistency_verify as verify

        note = _write_note(vault / "Work" / "w.md", "granola_id: g-1")
        gone = str(vault / "Work" / "gone.md")
        db = _interactions_db(
            tmp_path,
            [
                ("old", "p1", "meeting", note),
                ("new", "p2", "meeting", f"{note}{ms.COLLISION_SUFFIX}new"),
                ("stale", "p3", "meeting", gone),
            ],
        )
        settings = MagicMock(vault_path=str(vault))
        with patch("api.services.interaction_store.get_interaction_db_path", return_value=db), \
                patch("config.settings.settings", settings):
            buckets = verify._classify_missing_vault_files()
        assert [path for path, _ in buckets["gone"]] == [gone]
        assert not buckets["duplicate"] and not buckets["repoint"]

    def test_kept_apart_row_stays_even_after_its_note_is_deleted(self, tmp_path, vault):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import sync_consistency_verify as verify

        gone = str(vault / "Work" / "gone.md")
        db = _interactions_db(tmp_path, [("new", "p2", "meeting", f"{gone}{ms.COLLISION_SUFFIX}new")])
        settings = MagicMock(vault_path=str(vault))
        with patch("api.services.interaction_store.get_interaction_db_path", return_value=db), \
                patch("config.settings.settings", settings):
            buckets = verify._classify_missing_vault_files()
        assert not any(buckets.values())


class TestMentionHandling:
    """Note mentions stay out of the evidence that only real contact may supply."""

    @pytest.mark.parametrize("source_type", ["vault", "meeting", "granola"])
    def test_a_note_is_not_contact_evidence(self, tmp_path, vault, source_type):
        from datetime import date

        from api.services.mention_resolver import MentionResolver

        day = "2026-03-04T10:00:00+00:00"
        db = _interactions_db(tmp_path, [("n1", "p1", source_type, "/synthetic/n.md")])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE interactions SET timestamp = ?", (day,))
        conn.commit()
        conn.close()
        resolver = MentionResolver(
            [], crm_db=_crm_db(tmp_path), interaction_db=db, vault_root=vault
        )
        assert resolver._closest_contact(["p1"], date(2026, 3, 4), 7) == {}

    @pytest.mark.parametrize("name", ["meeting", "granola"])
    def test_confirmed_note_link_is_found_under_either_label(self, tmp_path, name):
        db = _crm_db(
            tmp_path,
            [
                ("s1", "p1", "granola", "/synthetic/a.md:Alex", "confirmed"),
                ("s2", "p2", "meeting", "/synthetic/b.md:Blair", "confirmed"),
            ],
        )
        with patch.object(ms, "_rename_rows", side_effect=RuntimeError("boom")):
            store = SourceEntityStore(db_path=db)
        assert store.has_confirmed_note_link(name, "/synthetic/a.md", "p1") is True
        assert store.has_confirmed_note_link(name, "/synthetic/b.md", "p2") is True

    def test_unlinked_meeting_mention_keeps_type_and_provider(self, tmp_path):
        vault_root = _mkdir(tmp_path / "Vault")
        note = vault_root / "Work" / "w.md"
        _write_note(note, "wispr_id: w-1")
        entities = SourceEntityStore(db_path=str(tmp_path / "c.db"))
        indexer = _offline_indexer(
            tmp_path,
            vault_root,
            entity_resolver=MagicMock(),
            interaction_store=MagicMock(),
            source_entity_store=entities,
        )
        person = indexer._record_mention(
            note, "Alex", None, 0.0, None, datetime(2026, 3, 4, tzinfo=timezone.utc),
            True, "wispr", MagicMock(), MagicMock(), entities, set(),
        )
        assert person is None
        entity = entities.get_by_source("meeting", f"{note.resolve()}:Alex")
        assert (entity.source_type, entity.provider) == ("meeting", "wispr")


class TestNetworkGuard:
    def test_the_indexer_factory_makes_no_connection(self, tmp_path):
        indexer = _offline_indexer(
            tmp_path,
            _mkdir(tmp_path / "Vault"),
            entity_resolver=MagicMock(),
            interaction_store=MagicMock(),
            source_entity_store=MagicMock(),
        )
        assert isinstance(indexer.vector_store, MagicMock)

    def test_a_connection_attempt_fails_the_test(self):
        with pytest.raises(AssertionError):
            socket.create_connection(("127.0.0.1", 9), timeout=1)


class TestWebUi:
    """The pages read `meeting`; a leftover `granola` key would silently undercount."""

    @pytest.mark.parametrize("page", ["crm.html", "index.html"])
    def test_no_granola_source_keys_remain(self, page):
        assert "granola" not in (REPO_ROOT / "web" / page).read_text().lower()

    def test_crm_note_counts_and_filters_read_meeting(self):
        html = (REPO_ROOT / "web" / "crm.html").read_text()
        assert html.count("(sourceTotals['vault'] || 0) + (sourceTotals['meeting'] || 0)") == 3
        assert "(counts['vault'] || 0) + (counts['meeting'] || 0)" in html
        assert "'notes': 'vault,meeting'" in html
        assert ".channel-meeting" in html and ".bar-meeting" in html
