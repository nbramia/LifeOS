"""Tests for scripts/backfill_whatsapp_group_size.py."""
import json
import logging
import os
import sqlite3
import stat
import sys

import pytest

from scripts import backfill_whatsapp_group_size as backfill_script
from scripts.backfill_whatsapp_group_size import backfill, plan_updates

pytestmark = pytest.mark.unit

GROUP = "120363001@g.us"
UNLISTED_GROUP = "120363002@g.us"
PEER = "15550000001@s.whatsapp.net"

EXPORT = {
    "messages": [
        {"msg_id": "direct-in", "chat_jid": PEER, "from_me": 0, "text": "synthetic secret text"},
        {"msg_id": "direct-out", "chat_jid": PEER, "from_me": 1, "text": "synthetic secret text"},
        {"msg_id": "group-in", "chat_jid": GROUP, "from_me": 0, "text": "synthetic secret text"},
        {"msg_id": "group-out", "chat_jid": GROUP, "from_me": 1, "text": "synthetic secret text"},
        {"msg_id": "unlisted-in", "chat_jid": UNLISTED_GROUP, "from_me": 0, "text": "synthetic secret text"},
    ],
    "group_participants": [
        {"group_jid": GROUP, "user_jid": PEER},
        {"group_jid": GROUP, "user_jid": "15550000002@s.whatsapp.net"},
        {"group_jid": GROUP, "user_jid": "15550000003@s.whatsapp.net"},
    ],
}

ROWS = [
    ("1", "whatsapp_direct-in"),
    ("2", "whatsapp_direct-out"),
    ("3", "whatsapp_group-in"),
    ("4", f"whatsapp_group-out:{PEER}"),
    ("5", "whatsapp_group-out:15550000002@s.whatsapp.net"),
    ("6", "whatsapp_unlisted-in"),
    ("7", "whatsapp_not-in-export"),
    ("8", f"whatsapp_not-in-export-either:{PEER}"),
]


@pytest.fixture
def paths(tmp_path):
    db = str(tmp_path / "interactions.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE interactions (id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, attendee_count INTEGER)"
    )
    conn.executemany(
        "INSERT INTO interactions (id, source_type, source_id) VALUES (?, 'whatsapp', ?)", ROWS
    )
    conn.execute(
        "INSERT INTO interactions VALUES ('cal', 'calendar', ?, 4)", (f"whatsapp_group-in:{PEER}",)
    )
    conn.commit()
    conn.close()
    export = tmp_path / "whatsapp.json"
    export.write_text(json.dumps(EXPORT))
    return db, str(export)


def _values(db: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT id, attendee_count FROM interactions"))
    finally:
        conn.close()


def _backups(db: str) -> list[str]:
    folder = os.path.dirname(db)
    return [f for f in os.listdir(folder) if ".bak-whatsapp-group-size-" in f]


class TestDryRun:
    def test_reports_without_writing(self, paths):
        db, export = paths
        before = _values(db)

        stats = backfill(db, export, dry_run=True)

        assert _values(db) == before
        assert _backups(db) == []
        assert stats["to_update"] == 4
        assert stats["updated"] == 0
        assert stats["backup"] is None


class TestExecute:
    def test_sets_group_sizes_and_leaves_the_rest_null(self, paths):
        db, export = paths

        stats = backfill(db, export, dry_run=False)

        assert stats["updated"] == 4
        assert _values(db) == {
            "1": None,
            "2": None,
            "3": 3,
            "4": 3,
            "5": 3,
            "6": 0,
            "7": None,
            "8": None,
            "cal": 4,
        }

    def test_reports_each_kind_of_row(self, paths):
        db, export = paths

        stats = backfill(db, export, dry_run=False)

        assert stats["rows"] == 8
        assert stats["direct"] == 2
        assert stats["group_received"] == 2
        assert stats["group_sent"] == 2
        assert stats["unknown_membership"] == 1
        assert stats["unmatched"] == 2
        assert stats["unmatched_bare"] == 1
        assert stats["unmatched_fanout"] == 1

    def test_backs_up_first(self, paths):
        db, export = paths

        stats = backfill(db, export, dry_run=False)

        assert stat.S_IMODE(os.stat(stats["backup"]).st_mode) == 0o600
        conn = sqlite3.connect(stats["backup"])
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE source_type = 'whatsapp' AND attendee_count IS NOT NULL"
            ).fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone() == (9,)
        finally:
            conn.close()

    def test_is_idempotent(self, paths):
        db, export = paths
        backfill(db, export, dry_run=False)
        once = _values(db)

        again = backfill(db, export, dry_run=False)

        assert _values(db) == once
        assert again["updated"] == 0
        assert again["to_update"] == 0
        assert again["already_set"] == 4
        assert again["backup"] is None
        assert len(_backups(db)) == 1

    def test_a_newer_export_does_not_rewrite_a_stored_size(self, paths, tmp_path):
        db, export = paths
        backfill(db, export, dry_run=False)
        once = _values(db)

        grown = dict(EXPORT)
        grown["group_participants"] = EXPORT["group_participants"] + [
            {"group_jid": GROUP, "user_jid": "15550000004@s.whatsapp.net"}
        ]
        newer = tmp_path / "newer.json"
        newer.write_text(json.dumps(grown))

        backfill(db, str(newer), dry_run=False)

        assert _values(db) == once

    def test_a_value_set_after_planning_is_kept(self, paths, monkeypatch):
        """A nightly import can store a size between reading rows and writing."""
        db, export = paths
        monkeypatch.setattr(backfill_script, "plan_updates", lambda rows, data: ([(99, "cal")], {"to_update": 1}))

        stats = backfill(db, export, dry_run=False)

        assert stats["updated"] == 0
        assert _values(db)["cal"] == 4

    def test_never_touches_other_source_types(self, paths):
        """A calendar row with a matching-looking source_id keeps its attendee count."""
        db, export = paths
        backfill(db, export, dry_run=False)
        assert _values(db)["cal"] == 4


class TestPlanUpdates:
    def test_flags_a_sent_group_row_without_the_fanout_form(self):
        _, stats = plan_updates([("1", "whatsapp_group-out", None)], EXPORT)
        assert stats["group_from_me_without_fanout"] == 1

    def test_flags_a_fanout_row_in_a_direct_chat(self):
        updates, stats = plan_updates([("1", f"whatsapp_direct-out:{PEER}", None)], EXPORT)
        assert updates == []
        assert stats["fanout_in_direct_chat"] == 1

    def test_expected_data_raises_no_flags(self, paths):
        db, export = paths
        stats = backfill(db, export, dry_run=True)
        assert "group_from_me_without_fanout" not in stats
        assert "fanout_in_direct_chat" not in stats


class TestMain:
    def _run(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["backfill_whatsapp_group_size.py", *args])
        return backfill_script.main()

    def test_dry_run_is_the_default(self, paths, monkeypatch):
        db, export = paths
        before = _values(db)
        assert self._run(monkeypatch, "--interactions-db", db, "--export-json", export) == 0
        assert _values(db) == before

    def test_execute_writes(self, paths, monkeypatch):
        db, export = paths
        assert self._run(monkeypatch, "--interactions-db", db, "--export-json", export, "--execute") == 0
        assert _values(db)["3"] == 3

    def test_missing_export_fails(self, paths, monkeypatch, tmp_path):
        db, _ = paths
        assert self._run(monkeypatch, "--interactions-db", db, "--export-json", str(tmp_path / "absent.json")) == 1

    def test_output_is_counts_only(self, paths, monkeypatch, caplog):
        db, export = paths
        with caplog.at_level(logging.INFO):
            self._run(monkeypatch, "--interactions-db", db, "--export-json", export)
        assert "synthetic secret text" not in caplog.text
        assert GROUP not in caplog.text
        assert PEER not in caplog.text
        assert "group-in" not in caplog.text
