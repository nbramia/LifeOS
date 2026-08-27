"""Tests for scripts/sync_imessage_interactions.py's direct-export step
(issue #698, the imessage-shaped sibling of #687's clean-skip pattern).

STEP 1 of sync_imessage_interactions() tries to export straight from the
local Messages.app database. On a host that isn't configured for that (not
macOS, no Messages.app database, or no Full Disk Access granted yet) this
must be a silent no-op — steps 2/3 (linking + syncing whatever's already in
data/imessage.db, which on Linux arrives via apple_data_import.py) must
still run. A genuine failure of a working export must still propagate so
the run fails loud.
"""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _build_imessage_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            rowid INTEGER PRIMARY KEY,
            text TEXT,
            timestamp TEXT NOT NULL,
            is_from_me INTEGER NOT NULL,
            handle TEXT NOT NULL,
            handle_normalized TEXT,
            service TEXT NOT NULL,
            person_entity_id TEXT
        );
        CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.commit()
    conn.close()


def _build_interactions_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE interactions (id TEXT, person_id TEXT, timestamp TEXT, "
        "source_type TEXT, title TEXT, snippet TEXT, source_link TEXT, "
        "source_id TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()


class TestImessageExportStepCleanSkip:
    def _run(
        self,
        tmp_path,
        monkeypatch,
        platform,
        source_db_exists,
        export_side_effect=None,
    ):
        import scripts.sync_imessage_interactions as mod

        imessage_db = tmp_path / "imessage.db"
        _build_imessage_db(imessage_db)
        interactions_db = tmp_path / "interactions.db"
        _build_interactions_db(interactions_db)

        monkeypatch.setattr(mod.sys, "platform", platform)
        monkeypatch.setattr(mod, "get_imessage_db_path", lambda: str(imessage_db))
        monkeypatch.setattr(mod, "get_interaction_db_path", lambda: str(interactions_db))
        monkeypatch.setattr(mod, "get_person_entity_store", lambda: MagicMock())
        monkeypatch.setattr(mod, "get_source_entity_store", lambda: MagicMock())

        fake_store = MagicMock()
        fake_store.SOURCE_DB_PATH = tmp_path / "chat.db"
        if source_db_exists:
            fake_store.SOURCE_DB_PATH.touch()
        if export_side_effect is not None:
            fake_store.export_from_source.side_effect = export_side_effect
        else:
            fake_store.export_from_source.return_value = {
                "messages_exported": 0,
                "messages_skipped": 0,
            }

        with patch("api.services.imessage.get_imessage_store", return_value=fake_store), \
             patch("api.services.imessage.join_imessages_to_entities", return_value={"messages_updated": 0}):
            result = mod.sync_imessage_interactions(dry_run=True)

        return result, fake_store

    def test_non_macos_skips_export_without_attempting_it(self, tmp_path, monkeypatch):
        """The maintainer's configured Linux host: step 1 never applies
        there (messages arrive via apple_data_import.py instead) — must not
        even attempt the direct export, and must not error."""
        result, fake_store = self._run(
            tmp_path, monkeypatch, platform="linux", source_db_exists=True,
        )
        fake_store.export_from_source.assert_not_called()
        assert result["errors"] == 0

    def test_macos_no_messages_db_skips_export(self, tmp_path, monkeypatch):
        """A Mac that has never used Messages.app — no chat.db at all."""
        result, fake_store = self._run(
            tmp_path, monkeypatch, platform="darwin", source_db_exists=False,
        )
        fake_store.export_from_source.assert_not_called()
        assert result["errors"] == 0

    def test_macos_no_full_disk_access_skips_cleanly(self, tmp_path, monkeypatch):
        """No FDA granted yet — the documented not-configured state for a
        fresh macOS install. Must not fail the run."""
        result, fake_store = self._run(
            tmp_path, monkeypatch, platform="darwin", source_db_exists=True,
            export_side_effect=PermissionError(
                "Cannot read the iMessage database ... Grant Full Disk Access ..."
            ),
        )
        fake_store.export_from_source.assert_called_once()
        assert result["errors"] == 0

    def test_macos_genuine_export_failure_propagates(self, tmp_path, monkeypatch):
        """FDA-granted-but-genuinely-broken (e.g. a locked/corrupt db) must
        still fail loud, not be swallowed as a routine skip."""
        with pytest.raises(sqlite3.OperationalError):
            self._run(
                tmp_path, monkeypatch, platform="darwin", source_db_exists=True,
                export_side_effect=sqlite3.OperationalError("database is locked"),
            )
