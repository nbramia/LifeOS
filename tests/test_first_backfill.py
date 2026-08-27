"""Tests for scripts/first_backfill.py, the one-time deep backfill entry
point for full-history-capable sync sources (issue #778).

The nightly sync (run_all_syncs.py) deliberately narrows Gmail/Calendar to
a 30-day window; a fresh install never gets its older history filled in on
its own. These tests pin: which sources the backfill covers and in what
order (mirroring the nightly SYNC_ORDER's Phases 1-4), that it widens only
the sources whose nightly args actually narrow a window, that an
unconfigured source is treated as a clean skip rather than a failure, that
a failed source doesn't abort the rest of the run, and that re-running the
orchestrator issues the exact same commands both times (the property that
makes it safe to re-run — actual row-level idempotence lives in the
underlying per-source scripts, reused unmodified here).
"""
import sqlite3
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


class TestBackfillOrder:
    def test_excludes_sources_with_no_full_history_concept(self):
        import scripts.first_backfill as fb

        for excluded in (
            "push_birthdays", "google_docs", "google_sheets", "monarch_money",
            "entity_cleanup", "consistency_verify",
        ):
            assert excluded not in fb.BACKFILL_ORDER

    def test_preserves_nightly_relative_order(self):
        """Every source the backfill does cover must appear in exactly the
        same relative order as the nightly SYNC_ORDER — the acceptance
        criteria's "same dependency order as the nightly pipeline's
        phases"."""
        import scripts.first_backfill as fb

        nightly_positions = {s: i for i, s in enumerate(fb.SYNC_ORDER)}
        backfill_positions = [nightly_positions[s] for s in fb.BACKFILL_ORDER]
        assert backfill_positions == sorted(backfill_positions)

    def test_covers_the_full_history_capable_sources(self):
        import scripts.first_backfill as fb

        for expected in (
            "gmail_personal", "gmail_work", "gmail_work2",
            "calendar_personal", "calendar_work", "calendar_work2",
        ):
            assert expected in fb.BACKFILL_ORDER


class TestFullDepthArgs:
    def test_strips_nightly_days_override(self):
        import scripts.first_backfill as fb

        nightly_args = ["--execute", "--gmail-only", "--account", "personal", "--days", "30"]
        assert fb._full_depth_args(nightly_args) == [
            "--execute", "--gmail-only", "--account", "personal",
        ]

    def test_leaves_sources_without_days_flag_unchanged(self):
        import scripts.first_backfill as fb

        nightly_args = ["--execute"]
        assert fb._full_depth_args(nightly_args) == ["--execute"]

    def test_does_not_mutate_the_input_list(self):
        """SYNC_SCRIPTS is shared, module-level state — run_all_syncs.py's
        nightly job reads the same list objects. Stripping --days must
        never mutate them in place (#778's regression guard: "the nightly
        sync job's existing per-source arguments shall be unchanged")."""
        import scripts.first_backfill as fb

        original = ["--execute", "--gmail-only", "--account", "personal", "--days", "30"]
        snapshot = list(original)
        fb._full_depth_args(original)
        assert original == snapshot

    def test_nightly_sync_scripts_definitions_are_unchanged(self):
        """Importing this module must not alter run_all_syncs.py's own
        SYNC_SCRIPTS — the nightly job's args are a separate, untouched
        concern."""
        import scripts.first_backfill as fb

        assert fb.SYNC_SCRIPTS["gmail_personal"][1] == [
            "--execute", "--gmail-only", "--account", "personal", "--days", "30",
        ]
        assert fb.SYNC_SCRIPTS["calendar_work2"][1] == [
            "--execute", "--calendar-only", "--account", "work2", "--days", "30",
        ]


class TestRunBackfillSource:
    def test_dry_run_does_not_invoke_subprocess(self, monkeypatch):
        import scripts.first_backfill as fb

        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(1))

        result = fb.run_backfill_source("gmail_personal", dry_run=True)

        assert result["success"] is True
        assert result["dry_run"] is True
        assert called == []

    def test_dry_run_command_omits_the_nightly_days_window(self, monkeypatch):
        import scripts.first_backfill as fb

        result = fb.run_backfill_source("gmail_personal", dry_run=True)
        assert "--days" not in result["cmd"]

    def test_unknown_source_errors_without_subprocess_call(self, monkeypatch):
        import scripts.first_backfill as fb

        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(1))

        result = fb.run_backfill_source("not-a-real-source", dry_run=False)

        assert result["success"] is False
        assert called == []

    def test_successful_run_is_reported_as_such(self, monkeypatch):
        import scripts.first_backfill as fb

        fake = MagicMockResult(returncode=0, stdout='SYNC_STATS:{"processed": 5}\n', stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)

        result = fb.run_backfill_source("gmail_personal", dry_run=False)

        assert result["success"] is True
        assert result["skipped"] is False
        assert result["stats"]["processed"] == 5

    def test_command_actually_run_omits_the_nightly_days_window(self, monkeypatch):
        """The regression guard for the whole issue: full-depth sources
        must actually be invoked without the nightly's narrow window."""
        import scripts.first_backfill as fb

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMockResult(returncode=0, stdout="SYNC_STATS:{}\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        fb.run_backfill_source("calendar_work", dry_run=False)

        assert "--days" not in captured["cmd"]
        assert "--calendar-only" in captured["cmd"]
        assert "work" in captured["cmd"]

    def test_unconfigured_source_is_a_clean_skip_not_a_failure(self, monkeypatch):
        """Consistent with #687: a source with nothing configured must be
        reported as skipped, never as a failure, so one missing
        integration doesn't fail the whole backfill."""
        import scripts.first_backfill as fb

        fake = MagicMockResult(
            returncode=0,
            stdout="SYNC_SKIPPED: gmail not configured for work2\n",
            stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)

        result = fb.run_backfill_source("gmail_work2", dry_run=False)

        assert result["success"] is True
        assert result["skipped"] is True

    def test_nonzero_exit_is_a_real_failure(self, monkeypatch):
        fake = MagicMockResult(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)

        import scripts.first_backfill as fb
        result = fb.run_backfill_source("gmail_personal", dry_run=False)

        assert result["success"] is False
        assert result["skipped"] is False
        assert "boom" in result["error"]

    def test_timeout_is_a_failure_not_a_crash(self, monkeypatch):
        import scripts.first_backfill as fb

        def fake_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = fb.run_backfill_source("gmail_personal", dry_run=False)
        assert result["success"] is False


class TestCoverageReport:
    def test_empty_database_reports_nothing(self, tmp_path, monkeypatch):
        import scripts.first_backfill as fb

        db_path = tmp_path / "interactions.db"  # never created — no table
        monkeypatch.setattr(fb, "get_interaction_db_path", lambda: str(db_path))

        assert fb.coverage_report() == {}

    def test_reports_count_and_date_range_per_source(self, tmp_path, monkeypatch):
        import scripts.first_backfill as fb

        db_path = tmp_path / "interactions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE interactions (id TEXT, timestamp TEXT, source_type TEXT)"
        )
        conn.executemany(
            "INSERT INTO interactions VALUES (?, ?, ?)",
            [
                ("1", "2020-01-01T00:00:00+00:00", "gmail"),
                ("2", "2024-06-15T00:00:00+00:00", "gmail"),
                ("3", "2023-03-01T00:00:00+00:00", "calendar"),
            ],
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(fb, "get_interaction_db_path", lambda: str(db_path))

        report = fb.coverage_report()

        assert report["gmail"]["count"] == 2
        assert report["gmail"]["earliest"] == "2020-01-01T00:00:00+00:00"
        assert report["gmail"]["latest"] == "2024-06-15T00:00:00+00:00"
        assert report["calendar"]["count"] == 1
        assert "slack" not in report  # no rows for this source_type


class MagicMockResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestMainOrchestration:
    def test_runs_sources_in_backfill_order(self, monkeypatch):
        import scripts.first_backfill as fb

        call_order = []

        def fake_run_source(source, dry_run=False):
            call_order.append(source)
            return {"success": True, "skipped": False, "stats": {}}

        monkeypatch.setattr(fb, "run_backfill_source", fake_run_source)
        monkeypatch.setattr(fb, "coverage_report", lambda: {})
        monkeypatch.setattr(sys, "argv", ["first_backfill.py", "--execute"])

        fb.main()

        assert call_order == fb.BACKFILL_ORDER

    def test_a_failed_source_does_not_abort_the_rest(self, monkeypatch):
        import scripts.first_backfill as fb

        call_order = []

        def fake_run_source(source, dry_run=False):
            call_order.append(source)
            if source == "gmail_work":
                return {"success": False, "skipped": False, "error": "boom"}
            return {"success": True, "skipped": False, "stats": {}}

        monkeypatch.setattr(fb, "run_backfill_source", fake_run_source)
        monkeypatch.setattr(fb, "coverage_report", lambda: {})
        monkeypatch.setattr(sys, "argv", ["first_backfill.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            fb.main()

        assert call_order == fb.BACKFILL_ORDER  # every source still ran
        assert exc_info.value.code == 1  # but the run is reported as failed

    def test_all_succeeding_exits_zero(self, monkeypatch):
        import scripts.first_backfill as fb

        monkeypatch.setattr(
            fb, "run_backfill_source",
            lambda source, dry_run=False: {"success": True, "skipped": False, "stats": {}},
        )
        monkeypatch.setattr(fb, "coverage_report", lambda: {})
        monkeypatch.setattr(sys, "argv", ["first_backfill.py", "--execute"])

        fb.main()  # must not raise SystemExit

    def test_rerun_issues_identical_commands(self, monkeypatch):
        """The orchestration-level half of #778's idempotence criterion:
        this script keeps no local state between runs, so re-running it
        against an install that already has full history issues the exact
        same commands both times. Row-level dedup is the underlying
        scripts' own, already-established responsibility."""
        import scripts.first_backfill as fb

        commands_by_run = [[], []]

        def make_fake_run(bucket):
            def fake_run(cmd, **kwargs):
                bucket.append(cmd)
                return MagicMockResult(returncode=0, stdout="SYNC_STATS:{}\n", stderr="")
            return fake_run

        monkeypatch.setattr(subprocess, "run", make_fake_run(commands_by_run[0]))
        for source in fb.BACKFILL_ORDER:
            fb.run_backfill_source(source, dry_run=False)

        monkeypatch.setattr(subprocess, "run", make_fake_run(commands_by_run[1]))
        for source in fb.BACKFILL_ORDER:
            fb.run_backfill_source(source, dry_run=False)

        assert commands_by_run[0] == commands_by_run[1]

    def test_no_execute_or_dry_run_flag_exits_with_usage_message(self, monkeypatch, capsys):
        import scripts.first_backfill as fb

        monkeypatch.setattr(sys, "argv", ["first_backfill.py"])

        with pytest.raises(SystemExit) as exc_info:
            fb.main()

        assert exc_info.value.code == 1
        assert "--execute" in capsys.readouterr().out
