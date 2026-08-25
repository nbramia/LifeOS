"""Tests for scripts/sync_google_sheets.py's clean-skip path (issue #687).

Before this fix, a missing config/gsheet_sync.yaml (e.g. the gsheet-journal
Google Form -> Sheet pipeline never set up) produced a zero-count "success"
dict with no SYNC_SKIPPED marker and no emitted SYNC_STATS -- indistinguishable
from a real, quiet run. These tests pin that the script now surfaces the skip
through the structured contract run_all_syncs._parse_sync_output reads.
"""
import pytest

pytestmark = pytest.mark.unit


class TestSyncGoogleSheetsSkip:
    def test_missing_config_prints_marker_and_returns_skip(self, monkeypatch, capsys):
        from scripts.sync_google_sheets import sync_google_sheets

        monkeypatch.setattr(
            "api.services.gsheet_sync.sync_gsheets",
            lambda: {"synced": 0, "failed": 0, "skipped": 0, "sheets": [],
                      "status": "skipped", "reason": "gsheet_sync_not_configured"},
        )

        result = sync_google_sheets(dry_run=False)

        assert result["status"] == "skipped"
        captured = capsys.readouterr()
        assert "SYNC_SKIPPED:" in captured.out

    def test_configured_run_does_not_print_marker(self, monkeypatch, capsys):
        from scripts.sync_google_sheets import sync_google_sheets

        monkeypatch.setattr(
            "api.services.gsheet_sync.sync_gsheets",
            lambda: {"synced": 3, "failed": 0, "skipped": 0, "sheets": []},
        )

        result = sync_google_sheets(dry_run=False)

        assert result.get("status") != "skipped"
        captured = capsys.readouterr()
        assert "SYNC_SKIPPED:" not in captured.out

    def test_dry_run_never_touches_gsheets(self, monkeypatch):
        """A plain dry run must behave exactly as before -- it never calls
        sync_gsheets() at all, configured or not."""
        from scripts.sync_google_sheets import sync_google_sheets

        def _boom():
            raise AssertionError("sync_gsheets must not be called on a dry run")

        monkeypatch.setattr("api.services.gsheet_sync.sync_gsheets", _boom)

        result = sync_google_sheets(dry_run=True)
        assert result["status"] == "dry_run"
