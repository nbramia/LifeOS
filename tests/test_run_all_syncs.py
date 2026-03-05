"""Tests for dependency-skip behavior in run_all_syncs."""

import pytest
from unittest.mock import patch, MagicMock

# Minimal SYNC_SOURCES for tests — only defines metadata (depends_on, frequency, phase).
# The actual sync logic is mocked via run_sync.
TEST_SYNC_SOURCES = {
    "source_a": {"description": "A", "phase": 1, "frequency": "daily"},
    "source_b": {"description": "B", "phase": 1, "frequency": "daily"},
    "source_c": {"description": "C", "phase": 2, "frequency": "daily", "depends_on": ["source_a"]},
    "source_d": {"description": "D", "phase": 3, "frequency": "daily", "depends_on": ["source_c"]},
    "source_e": {"description": "E", "phase": 2, "frequency": "daily", "depends_on": ["source_a", "source_b"]},
    # phone is absent from SYNC_ORDER (like FDA sources)
    "phone": {"description": "Phone", "phase": 1, "frequency": "daily"},
    "source_f": {"description": "F", "phase": 2, "frequency": "daily", "depends_on": ["phone"]},
    # disabled source
    "slack": {"description": "Slack", "phase": 1, "frequency": "daily"},
    "link_slack": {"description": "Link Slack", "phase": 2, "frequency": "daily", "depends_on": ["slack"]},
}

TEST_SYNC_ORDER = [
    "source_a",
    "source_b",
    "source_c",
    "source_d",
    "source_e",
    "source_f",  # depends on phone which is NOT in this list
    "slack",
    "link_slack",
]


def _make_run_sync_side_effect(fail_sources: set):
    """Return a side_effect function for run_sync that fails specified sources."""
    def side_effect(source, dry_run=False):
        if source in fail_sources:
            return False, {"error": "simulated failure"}
        return True, {"processed": 1, "created": 0}
    return side_effect


def _run_with_patches(fail_sources: set | None = None, disabled_sources: set | None = None):
    """Run run_all_syncs(dry_run=True) with test patches and return the result dict.

    dry_run=True skips: subprocess calls, backup, maintenance mode, Telegram, server restart.
    We only need to patch: SYNC_SOURCES, SYNC_ORDER, run_sync, check_sync_health,
    get_disabled_work_sources, and log_sync_summary_to_markdown.
    """
    from scripts.run_all_syncs import run_all_syncs

    fail_sources = fail_sources or set()

    run_sync_mock = MagicMock(side_effect=_make_run_sync_side_effect(fail_sources))

    with (
        patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
        patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
        patch("scripts.run_all_syncs.run_sync", run_sync_mock),
        patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
        patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=disabled_sources or set()),
        patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
    ):
        result = run_all_syncs(dry_run=True)

    return result, run_sync_mock


class TestDependencySkip:
    """Tests for the dependency-skip behavior in run_all_syncs."""

    def test_skip_when_dependency_failed(self):
        """Source C (depends on A) is skipped when A fails."""
        result, run_sync_mock = _run_with_patches(fail_sources={"source_a"})

        assert "source_c" in result["dep_skipped_sources"]
        assert result["results"]["source_c"]["skipped"] is True
        assert result["results"]["source_c"]["reason"] == "dependency_failed"
        assert "source_a" in result["results"]["source_c"]["failed_dependencies"]

        # run_sync should NOT have been called for source_c
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_c" not in called_sources

    def test_cascading_skip(self):
        """A→C→D chain: A fails → C skipped → D skipped."""
        result, run_sync_mock = _run_with_patches(fail_sources={"source_a"})

        assert "source_c" in result["dep_skipped_sources"]
        assert "source_d" in result["dep_skipped_sources"]
        assert result["results"]["source_d"]["reason"] == "dependency_failed"
        assert "source_c" in result["results"]["source_d"]["failed_dependencies"]

        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_c" not in called_sources
        assert "source_d" not in called_sources

    def test_partial_dependency_failure(self):
        """Source E depends on [A, B]. A succeeds, B fails → E skipped."""
        result, _ = _run_with_patches(fail_sources={"source_b"})

        assert "source_e" in result["dep_skipped_sources"]
        assert result["results"]["source_e"]["reason"] == "dependency_failed"
        assert "source_b" in result["results"]["source_e"]["failed_dependencies"]
        # source_a succeeded, so it should NOT be in failed_dependencies
        assert "source_a" not in result["results"]["source_e"]["failed_dependencies"]

    def test_absent_dependency_no_skip(self):
        """Source F depends on 'phone', which is NOT in the run list.
        Absent deps should not trigger a skip."""
        result, run_sync_mock = _run_with_patches(fail_sources=set())

        assert "source_f" not in result["dep_skipped_sources"]
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_f" in called_sources

    def test_disabled_dependency_no_skip(self):
        """link_slack depends on 'slack'. If slack is disabled (not failed),
        link_slack should NOT be dependency-skipped."""
        result, run_sync_mock = _run_with_patches(
            fail_sources=set(),
            disabled_sources={"slack"},
        )

        # slack is skipped as disabled, link_slack should NOT be dep-skipped
        assert "link_slack" not in result["dep_skipped_sources"]
        assert result["results"]["slack"]["reason"] == "work_integration_disabled"
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "link_slack" in called_sources

    def test_dep_skipped_in_result(self):
        """dep_skipped_sources appears in result dict and is sorted."""
        result, _ = _run_with_patches(fail_sources={"source_a"})

        assert "dep_skipped_sources" in result
        assert isinstance(result["dep_skipped_sources"], list)
        # Should be sorted
        assert result["dep_skipped_sources"] == sorted(result["dep_skipped_sources"])
        # source_c and source_d should be there (cascade from source_a)
        assert "source_c" in result["dep_skipped_sources"]
        assert "source_d" in result["dep_skipped_sources"]

    def test_all_succeed_no_skips(self):
        """Clean run: no dependency skips when everything succeeds."""
        result, _ = _run_with_patches(fail_sources=set())

        assert result["dep_skipped_sources"] == []
        # No source should have reason=dependency_failed
        for source, stats in result["results"].items():
            assert stats.get("reason") != "dependency_failed"
