"""Focused synthetic tests for the private lifecycle metrics helper."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import subprocess
import sys

import pytest

from scripts.development_metrics import (
    MetricsCorruptionError,
    MetricsError,
    MetricsRecorder,
    MetricRecord,
    append_record,
    read_records,
    read_records_detailed,
    summarize,
)


pytestmark = pytest.mark.unit


def _append_many(path: str, worker: int) -> None:
    for index in range(15):
        append_record(
            path,
            MetricRecord(
                run_id=f"run-{worker}-{index}",
                candidate_id=f"candidate-{worker}",
                phase="unit",
                phase_kind="execution",
                elapsed_seconds=index / 10,
                result="success",
                cache_hit=False,
                exit_status=0,
            ),
        )


def test_context_uses_monotonic_duration_and_preserves_exit_status(tmp_path):
    path = tmp_path / "receipts.jsonl"
    ticks = iter((10.0, 12.25))
    recorder = MetricsRecorder(
        path,
        run_id="run-synthetic",
        candidate_id="candidate-a",
        task_id="task-a",
        clock=lambda: next(ticks),
    )
    with recorder.phase("unit", phase_kind="execution", suite="unit", worker_count=2) as phase:
        phase.finish(result="failure", exit_status=7)
    [record] = read_records(path)
    assert record.elapsed_seconds == 2.25
    assert record.result == "failure"
    assert record.exit_status == 7
    assert record.task_id == "task-a"


def test_exception_and_cancellation_are_distinguishable(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-synthetic", candidate_id="candidate-a")
    with pytest.raises(RuntimeError):
        with recorder.phase("unit", phase_kind="execution"):
            raise RuntimeError("synthetic failure")
    with recorder.phase("transfer", phase_kind="transfer") as phase:
        phase.cancel(exit_status=-15)
    records = read_records(path)
    assert [record.result for record in records] == ["failure", "cancelled"]
    assert records[0].exit_status is None
    assert records[1].exit_status == -15


def test_async_cancellation_is_recorded_and_reraised(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-synthetic", candidate_id="candidate-a")
    with pytest.raises(asyncio.CancelledError):
        with recorder.phase("queue", phase_kind="waiting"):
            raise asyncio.CancelledError
    [record] = read_records(path)
    assert record.result == "cancelled"
    assert record.exit_status is None


def test_concurrent_processes_append_complete_json_lines(tmp_path):
    path = tmp_path / "receipts.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_append_many, args=(str(path), worker)) for worker in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    records = read_records(path)
    assert len(records) == 30
    assert len({record.run_id for record in records}) == 30
    assert all(record.schema_version == 1 for record in records)


def test_summary_reports_missing_unknown_cache_and_exit_status(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-a", candidate_id="candidate-a")
    recorder.record(phase="queue", phase_kind="waiting", elapsed_seconds=1, result="success", cache_hit=False)
    recorder.record(
        phase="unit",
        phase_kind="execution",
        elapsed_seconds=10,
        result="success",
        cache_hit=False,
        exit_status=0,
        review_round=1,
    )
    recorder = MetricsRecorder(path, run_id="run-b", candidate_id="candidate-a")
    recorder.record(
        phase="unit",
        phase_kind="execution",
        elapsed_seconds=20,
        result="success",
        cache_hit=True,
        exit_status=0,
        review_round=2,
    )
    recorder.record(
        phase="transfer",
        phase_kind="transfer",
        elapsed_seconds=3,
        result="infrastructure_failure",
        cache_hit=False,
        exit_status=23,
    )
    summary = summarize(read_records(path), expected_phases=("queue", "unit", "browser"))
    assert summary["sample_count"] == 2
    assert summary["duration_seconds"]["median_seconds"] == 6.5
    assert summary["duration_seconds"]["p95_seconds"] == 18.5
    assert summary["attempts"] == 3
    assert summary["reused_receipts"] == 1
    assert summary["infrastructure_failures"] == 1
    assert summary["phases"]["browser"]["status"] == "unknown"
    assert summary["phases"]["browser"]["missing_runs"] == 2
    assert summary["candidates"]["candidate-a"]["execution_count"] == 2
    assert summary["candidates"]["candidate-a"]["executions_by_suite"] == {"unknown": 2}
    assert summary["candidates"]["candidate-a"]["review_rounds"] == [1, 2]
    assert summary["review_rounds"] == [1, 2]


def test_completed_corrupt_line_is_explicit_and_does_not_expose_payload(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-a", candidate_id="candidate-a")
    recorder.record(phase="unit", phase_kind="execution", elapsed_seconds=4, result="success")
    with path.open("ab") as stream:
        stream.write(b"not-a-real-private-payload\n")
        stream.write(b"{\"broken\": true}\n")
    with pytest.raises(MetricsCorruptionError) as error:
        read_records(path)
    assert error.value.line_number == 2
    assert "not-a-real-private-payload" not in str(error.value)
    assert str(path) not in str(error.value)
    detailed = read_records_detailed(path)
    assert detailed.corruption_count == 2
    assert detailed.first_corrupt_line == 2


def test_unterminated_final_line_is_reported_as_incomplete_only(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-a", candidate_id="candidate-a")
    recorder.record(phase="unit", phase_kind="execution", elapsed_seconds=4, result="success")
    with path.open("ab") as stream:
        stream.write(b"{\"schema_version\":1,\"run_id\":\"run-a\"")
    result = read_records_detailed(path)
    assert len(result.records) == 1
    assert result.incomplete_trailing_lines == 1
    assert result.corruption_count == 0


def test_summary_cli_reports_corruption_without_receipt_payload_or_path(tmp_path):
    path = tmp_path / "receipts.jsonl"
    path.write_bytes(b"{\"corrupt\": true}\n")
    result = subprocess.run(
        [sys.executable, "scripts/development_metrics.py", "summary", "--path", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "corruption" in result.stderr
    assert '{"corrupt"' not in result.stderr
    assert str(path) not in result.stderr


def test_invalid_private_or_payload_like_fields_are_rejected():
    with pytest.raises(MetricsError):
        MetricRecord(
            run_id="/private/path",
            candidate_id="candidate-a",
            phase="unit",
            phase_kind="execution",
            elapsed_seconds=1,
            result="success",
            cache_hit=False,
        )
    with pytest.raises(MetricsError):
        MetricRecord(
            run_id="run-a",
            candidate_id="candidate-a",
            phase="unit",
            phase_kind="execution",
            elapsed_seconds=1,
            result="success",
            cache_hit=False,
            suite="synthetic suite",
        )


def test_existing_shared_directory_and_symlink_target_are_not_mutated(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared_before = shared.stat().st_mode
    record = MetricRecord(
        run_id="run-a",
        candidate_id="candidate-a",
        phase="unit",
        phase_kind="execution",
        elapsed_seconds=1,
        result="success",
        cache_hit=False,
    )
    with pytest.raises(MetricsError):
        append_record(shared / "receipts.jsonl", record)
    assert shared.stat().st_mode == shared_before

    target = tmp_path / "sentinel"
    target.write_text("do-not-change", encoding="utf-8")
    target.chmod(0o640)
    target_before = (target.read_bytes(), target.stat().st_mode)
    link = tmp_path / "receipts-link.jsonl"
    link.symlink_to(target)
    with pytest.raises(MetricsError):
        append_record(link, record)
    assert (target.read_bytes(), target.stat().st_mode) == target_before

    target_dir = tmp_path / "sentinel-dir"
    target_dir.mkdir()
    target_dir_before = set(target_dir.iterdir())
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target_dir, target_is_directory=True)
    with pytest.raises(MetricsError):
        append_record(parent_link / "new" / "receipts.jsonl", record)
    assert set(target_dir.iterdir()) == target_dir_before


def test_context_does_not_mask_wrapped_exception_when_receipt_fails(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    recorder = MetricsRecorder(shared / "receipts.jsonl", run_id="run-a", candidate_id="candidate-a")
    with pytest.raises(RuntimeError, match="synthetic suite failure"):
        with recorder.phase("unit", phase_kind="execution"):
            raise RuntimeError("synthetic suite failure")
    assert isinstance(recorder.last_error, MetricsError)


def test_context_preserves_nonzero_command_status_when_receipt_fails(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    recorder = MetricsRecorder(shared / "receipts.jsonl", run_id="run-a", candidate_id="candidate-a")
    with recorder.phase("unit", phase_kind="execution") as phase:
        record = phase.finish(result="failure", exit_status=23)
    assert record.exit_status == 23
    assert phase.error is not None


def test_success_cannot_claim_nonzero_exit_status():
    with pytest.raises(MetricsError):
        MetricRecord(
            run_id="run-a",
            candidate_id="candidate-a",
            phase="unit",
            phase_kind="execution",
            elapsed_seconds=1,
            result="success",
            cache_hit=False,
            exit_status=23,
        )


def test_cli_summary_is_aggregate_only(tmp_path):
    path = tmp_path / "receipts.jsonl"
    recorder = MetricsRecorder(path, run_id="run-a", candidate_id="candidate-a")
    recorder.record(phase="unit", phase_kind="execution", elapsed_seconds=4, result="success")
    result = subprocess.run(
        [sys.executable, "scripts/development_metrics.py", "summary", "--path", str(path), "--expected-phase", "browser"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)
    assert summary["sample_count"] == 1
    assert summary["phases"]["browser"]["status"] == "unknown"
    assert str(path) not in result.stdout


def test_cli_record_started_monotonic_uses_deterministic_fake_clock_not_wall_time(tmp_path, monkeypatch):
    """A non-Python caller (a shell runner) can time a phase via two monotonic
    readings computed here at record time —
    never by the caller doing its own wall-clock arithmetic. Proven with a
    fake clock and zero real elapsed time, so this cannot pass by coincidence
    of how long the test itself happened to take.
    """
    import scripts.development_metrics as dm

    monkeypatch.setattr(dm, "_current_monotonic", lambda: 1007.5)
    path = tmp_path / "metrics.jsonl"
    exit_code = dm.main([
        "record", "--path", str(path), "--candidate-id", "remote-fake",
        "--phase", "remote-transfer", "--phase-kind", "transfer", "--result", "success",
        "--started-monotonic", "1000.0",
    ])
    assert exit_code == 0
    record = read_records(path)[0]
    assert record.elapsed_seconds == pytest.approx(7.5)


def test_cli_now_prints_a_monotonic_reading_for_a_shell_caller_to_capture(tmp_path):
    """The portable phase-start primitive: no GNU `timeout`, no bash SECONDS."""
    result = subprocess.run(
        [sys.executable, "scripts/development_metrics.py", "now"],
        check=True, capture_output=True, text=True,
    )
    started = float(result.stdout.strip())
    # A real elapsed measurement built from two of these, subtracted in this
    # same process, is non-negative and matches --started-monotonic exactly.
    path = tmp_path / "metrics.jsonl"
    subprocess.run(
        [
            sys.executable, "scripts/development_metrics.py", "record",
            "--path", str(path), "--candidate-id", "c1", "--phase", "p",
            "--phase-kind", "execution", "--result", "success",
            "--started-monotonic", str(started),
        ],
        check=True, capture_output=True, text=True,
    )
    record = read_records(path)[0]
    assert record.elapsed_seconds is not None and record.elapsed_seconds >= 0


def test_cli_import_preserves_a_pre_formed_record_exactly(tmp_path):
    """The correlation primitive: a record fetched from elsewhere (e.g. a
    remote host's own metrics.jsonl for a shared run_id) is validated and
    appended verbatim — its own run_id/candidate_id/phase/result untouched,
    never rewritten as if this process had generated it."""
    path = tmp_path / "metrics.jsonl"
    foreign = MetricRecord(
        run_id="shared-run-abc", candidate_id="remote-candidate-xyz",
        phase="verification-queue", phase_kind="waiting", elapsed_seconds=3.25,
        result="success", cache_hit=False, exit_status=None, suite="fast-unit", worker_count=2,
    )
    result = subprocess.run(
        [sys.executable, "scripts/development_metrics.py", "import", "--path", str(path)],
        input=json.dumps(foreign.to_dict()), check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "shared-run-abc"
    imported = read_records(path)[0]
    assert imported == foreign


def test_cli_import_rejects_invalid_json_without_crashing(tmp_path):
    path = tmp_path / "metrics.jsonl"
    result = subprocess.run(
        [sys.executable, "scripts/development_metrics.py", "import", "--path", str(path)],
        input="not valid json", capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "unable to import" in result.stderr
    assert not path.exists()
