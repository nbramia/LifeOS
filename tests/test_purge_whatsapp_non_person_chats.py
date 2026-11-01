"""Tests for scripts/purge_whatsapp_non_person_chats.py."""
import json
import logging
import os
import sqlite3
import stat

import pytest

from scripts.purge_whatsapp_non_person_chats import plan_deletions, purge

pytestmark = pytest.mark.unit

PEER = "15550000001@s.whatsapp.net"
BOT = "15550000002@bot"
GROUP = "120363001@g.us"

EXPORT = {
    "messages": [
        {"msg_id": "direct", "chat_jid": PEER, "text": "synthetic secret text"},
        {"msg_id": "bot-out", "chat_jid": BOT, "text": "synthetic secret text"},
        {"msg_id": "bot-in", "chat_jid": BOT, "text": "synthetic secret text"},
        {"msg_id": "bcast", "chat_jid": "status@broadcast", "text": "synthetic secret text"},
        {"msg_id": "group", "chat_jid": GROUP, "text": "synthetic secret text"},
    ],
}

# (id, source_id, person_id)
ROWS = [
    ("1", "whatsapp_direct", "p1"),
    ("2", "whatsapp_bot-out", "p2"),
    ("3", "whatsapp_bot-in", "p2"),
    ("4", "whatsapp_bcast", "p1"),
    ("5", f"whatsapp_group:{PEER}", "p1"),
    ("6", "whatsapp_not-in-export", "p3"),
]


@pytest.fixture
def paths(tmp_path):
    db = str(tmp_path / "interactions.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE interactions (id TEXT PRIMARY KEY, person_id TEXT, source_type TEXT, source_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO interactions (id, source_type, source_id, person_id) VALUES (?, 'whatsapp', ?, ?)",
        ROWS,
    )
    conn.execute("INSERT INTO interactions VALUES ('mail', 'p2', 'gmail', 'whatsapp_bot-out')")
    conn.commit()
    conn.close()
    export = tmp_path / "whatsapp.json"
    export.write_text(json.dumps(EXPORT))
    return db, str(export)


def _ids(db: str) -> set:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM interactions")}
    finally:
        conn.close()


def _backups(db: str) -> list[str]:
    return [f for f in os.listdir(os.path.dirname(db)) if ".bak-whatsapp-non-person-" in f]


class TestPlan:
    def test_selects_only_bot_and_broadcast_rows(self):
        doomed, stats = plan_deletions(ROWS, EXPORT)

        assert doomed == ["2", "3", "4"]
        assert stats["bot"] == 2
        assert stats["broadcast"] == 1
        assert stats["affected_people"] == 2
        assert stats["unmatched"] == 1
        assert stats["rows"] == 6


class TestDryRun:
    def test_reports_without_writing(self, paths):
        db, export = paths
        before = _ids(db)

        stats = purge(db, export, dry_run=True)

        assert _ids(db) == before
        assert _backups(db) == []
        assert stats["to_delete"] == 3
        assert stats["deleted"] == 0
        assert stats["backup"] is None


class TestExecute:
    def test_deletes_only_the_matching_whatsapp_rows(self, paths):
        db, export = paths

        stats = purge(db, export, dry_run=False)

        assert stats["deleted"] == 3
        # The direct, group, unmatched, and non-WhatsApp rows are untouched.
        assert _ids(db) == {"1", "5", "6", "mail"}

    def test_backs_up_first(self, paths):
        db, export = paths

        stats = purge(db, export, dry_run=False)

        assert stat.S_IMODE(os.stat(stats["backup"]).st_mode) == 0o600
        conn = sqlite3.connect(stats["backup"])
        try:
            assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone() == (7,)
        finally:
            conn.close()

    def test_second_run_changes_nothing_and_takes_no_backup(self, paths):
        db, export = paths
        purge(db, export, dry_run=False)
        remaining = _ids(db)
        backups = _backups(db)

        stats = purge(db, export, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["backup"] is None
        assert _ids(db) == remaining
        assert _backups(db) == backups


class TestOutput:
    def test_logs_aggregate_counts_only(self, paths, caplog, monkeypatch):
        from scripts import purge_whatsapp_non_person_chats as script

        db, export = paths
        monkeypatch.setattr("sys.argv", ["purge", "--interactions-db", db, "--export-json", export])
        with caplog.at_level(logging.INFO):
            assert script.main() == 0

        assert "DRY RUN" in caplog.text
        assert "to_delete" in caplog.text
        for secret in ("synthetic secret text", BOT, PEER, "status@broadcast"):
            assert secret not in caplog.text

    def test_missing_input_fails(self, tmp_path, monkeypatch):
        from scripts import purge_whatsapp_non_person_chats as script

        monkeypatch.setattr(
            "sys.argv",
            ["purge", "--interactions-db", str(tmp_path / "nope.db"), "--export-json", str(tmp_path / "nope.json")],
        )
        assert script.main() == 1
