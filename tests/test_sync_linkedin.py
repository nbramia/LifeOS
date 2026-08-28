"""Tests for scripts/sync_linkedin.py's clean-skip path (issue #780).

Before this fix, a missing data/LinkedInConnections.csv already returned a
{"status": "skipped", "reason": "csv_not_found"} dict internally, but this
script runs as a subprocess (run_all_syncs.py's SYNC_SOURCES entry), so only
what lands on stdout/stderr is ever parsed downstream — the returned dict
was discarded, and the SYNC_SKIPPED marker _parse_sync_output actually looks
for was never printed. The result: an absent CSV recorded as a normal,
zero-stat "success", indistinguishable from a healthy but quiet source.
These tests pin that the marker is now printed, and that a present CSV's
behavior is unchanged.
"""
import pytest

pytestmark = pytest.mark.unit


class TestSyncLinkedinSkip:
    def test_missing_csv_prints_marker_and_returns_skip(self, tmp_path, capsys):
        from scripts.sync_linkedin import sync_linkedin

        missing_csv = tmp_path / "does-not-exist.csv"

        result = sync_linkedin(csv_path=str(missing_csv), dry_run=False)

        assert result == {"status": "skipped", "reason": "csv_not_found"}
        captured = capsys.readouterr()
        assert "SYNC_SKIPPED:" in captured.out
        assert str(missing_csv) in captured.out

    def test_missing_csv_dry_run_also_returns_skip_without_marker_requirement(
        self, tmp_path
    ):
        """Dry run isn't parsed downstream (run_all_syncs.py always invokes
        with --execute) — behavior here is unchanged: still reports skipped,
        marker presence doesn't matter for this path."""
        from scripts.sync_linkedin import sync_linkedin

        missing_csv = tmp_path / "does-not-exist.csv"
        result = sync_linkedin(csv_path=str(missing_csv), dry_run=True)
        assert result == {"status": "skipped", "reason": "csv_not_found"}

    def test_present_csv_configured_run_is_unaffected(self, tmp_path, monkeypatch, capsys):
        """Regression guard: a present, valid CSV must produce byte-identical
        stats and behavior to before this fix — no SYNC_SKIPPED marker, same
        result shape."""
        from scripts.sync_linkedin import sync_linkedin

        csv_path = tmp_path / "Connections.csv"
        csv_path.write_text("First Name,Last Name\nJane,Doe\n")

        fake_results = {
            "connections_processed": 1,
            "entities_created": 1,
            "entities_updated": 0,
            "connections_skipped": 0,
        }
        monkeypatch.setattr(
            "api.services.people_aggregator.sync_linkedin_to_v2",
            lambda csv_path, entity_resolver: fake_results,
        )
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver", lambda: object()
        )

        result = sync_linkedin(csv_path=str(csv_path), dry_run=False)

        assert result == fake_results
        captured = capsys.readouterr()
        assert "SYNC_SKIPPED:" not in captured.out
        assert "SYNC_STATS:" in captured.out
