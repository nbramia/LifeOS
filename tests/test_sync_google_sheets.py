"""Tests for scripts/sync_google_sheets.py's clean-skip path.

A missing config/gsheet_sync.yaml (e.g. the gsheet-journal
Google Form -> Sheet pipeline never set up) must not produce a zero-count
"success" dict with no SYNC_SKIPPED marker and no emitted SYNC_STATS --
that would be indistinguishable from a real, quiet run. These tests pin
that the script surfaces the skip through the structured contract
run_all_syncs._parse_sync_output reads.
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


class TestSyncGoogleSheetsStats:
    """A configured run reports real stats through SYNC_STATS so the
    orchestrator records it instead of a zero-yield success."""

    def _stats(self, capsys):
        import json
        import re

        lines = re.findall(r"^SYNC_STATS:(\{.*\})$", capsys.readouterr().out, re.M)
        assert len(lines) == 1
        return json.loads(lines[0])

    def test_emits_rows_written_and_examined(self, monkeypatch, capsys):
        from scripts.sync_google_sheets import sync_google_sheets

        monkeypatch.setattr(
            "api.services.gsheet_sync.sync_gsheets",
            lambda: {"synced": 3, "failed": 1, "skipped": 40, "sheets": []},
        )

        sync_google_sheets(dry_run=False)

        assert self._stats(capsys) == {"processed": 43, "created": 3, "errors": 1}

    def test_quiet_night_still_reports_rows_examined(self, monkeypatch, capsys):
        """No new responses tonight: rows already synced were still examined,
        so the run is not indistinguishable from a dead source."""
        from scripts.run_all_syncs import _parse_sync_output, _run_yield
        from scripts.sync_google_sheets import sync_google_sheets

        monkeypatch.setattr(
            "api.services.gsheet_sync.sync_gsheets",
            lambda: {"synced": 0, "failed": 0, "skipped": 12, "sheets": []},
        )

        sync_google_sheets(dry_run=False)

        parsed = _parse_sync_output(capsys.readouterr().out)
        assert parsed["created"] == 0
        assert _run_yield(parsed) == 12

    def test_skip_and_dry_run_emit_no_stats(self, monkeypatch, capsys):
        from scripts.sync_google_sheets import sync_google_sheets

        monkeypatch.setattr(
            "api.services.gsheet_sync.sync_gsheets",
            lambda: {"synced": 0, "failed": 0, "skipped": 0, "sheets": [],
                     "status": "skipped", "reason": "gsheet_sync_not_configured"},
        )
        sync_google_sheets(dry_run=False)
        sync_google_sheets(dry_run=True)

        assert "SYNC_STATS:" not in capsys.readouterr().out
