#!/usr/bin/env python3
"""Private, local development-lifecycle measurements.

The module intentionally observes work performed by a caller.  It does not
start tests, inspect the environment, or derive elapsed time from wall-clock
timestamps.  Callers can use :class:`MetricsRecorder` as a context manager
around an already-authorized phase and append one JSON object per phase to a
private JSONL file.

The file is an evidence input for local tooling, not an application database
or public API.  Records contain no timestamps or paths; an evidence reference
is an opaque label supplied by the caller.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fcntl
import json
import os
import re
import stat
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
PHASE_KINDS = frozenset({"waiting", "execution", "transfer"})
RESULTS = frozenset(
    {"success", "failure", "cancelled", "interrupted", "infrastructure_failure", "unknown"}
)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_SUITE = 128


class MetricsError(ValueError):
    """Raised when a record would be invalid or unsafe to persist."""


class MetricsCorruptionError(MetricsError):
    """Raised when a completed receipt line is malformed."""

    def __init__(self, line_number: int):
        self.line_number = line_number
        super().__init__(f"metrics receipt corruption at line {line_number}")


def _token(value: str, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise MetricsError(f"{field} is required")
        return None
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise MetricsError(f"invalid {field}")
    return value


def _suite(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_SUITE:
        raise MetricsError("invalid suite")
    # Suites are labels, never local paths or arbitrary command payloads.
    if any(ch.isspace() or ch in "/\\\x00" for ch in value):
        raise MetricsError("invalid suite")
    return value


def _duration(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise MetricsError("elapsed_seconds must be non-negative")
    result = float(value)
    if result < 0 or result != result or result in (float("inf"), float("-inf")):
        raise MetricsError("elapsed_seconds must be non-negative")
    return round(result, 6)


def _workers(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MetricsError("worker_count must be a positive integer")
    return value


def _exit_status(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetricsError("exit_status must be an integer")
    # Preserve shell-style positive statuses and negative signal statuses.
    if value < -255 or value > 255:
        raise MetricsError("exit_status is outside the supported range")
    return value


@dataclasses.dataclass(frozen=True)
class MetricRecord:
    """One observed lifecycle phase.

    ``elapsed_seconds`` is measured by a monotonic clock in the caller (the
    context-manager API does this automatically).  ``exit_status`` remains
    separate from ``result`` so a non-zero subprocess status is never lost or
    inferred from a friendly result label.
    """

    run_id: str
    candidate_id: str
    phase: str
    phase_kind: str
    elapsed_seconds: float | None
    result: str
    cache_hit: bool
    task_id: str | None = None
    suite: str | None = None
    worker_count: int | None = None
    exit_status: int | None = None
    evidence_ref: str | None = None
    review_round: int | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _token(self.run_id, "run_id")
        _token(self.candidate_id, "candidate_id")
        _token(self.task_id, "task_id", required=False)
        _token(self.phase, "phase")
        _token(self.evidence_ref, "evidence_ref", required=False)
        if not isinstance(self.phase_kind, str) or self.phase_kind not in PHASE_KINDS:
            raise MetricsError("invalid phase_kind")
        if not isinstance(self.result, str) or self.result not in RESULTS:
            raise MetricsError("invalid result")
        if not isinstance(self.cache_hit, bool):
            raise MetricsError("cache_hit must be boolean")
        _duration(self.elapsed_seconds)
        _suite(self.suite)
        _workers(self.worker_count)
        _exit_status(self.exit_status)
        if self.result == "success" and self.exit_status not in (None, 0):
            raise MetricsError("successful records must have exit_status 0 or null")
        if self.result in {"failure", "infrastructure_failure", "cancelled", "interrupted"} and self.exit_status == 0:
            raise MetricsError("non-success records cannot have exit_status 0")
        if self.review_round is not None and (
            isinstance(self.review_round, bool)
            or not isinstance(self.review_round, int)
            or self.review_round < 1
        ):
            raise MetricsError("review_round must be a positive integer")
        if self.schema_version != SCHEMA_VERSION:
            raise MetricsError("unsupported schema_version")

    def to_dict(self) -> dict[str, Any]:
        """Return the stable on-disk representation."""

        return dataclasses.asdict(self)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MetricRecord":
        """Validate and decode one JSON object from a local receipt file."""

        if not isinstance(raw, Mapping):
            raise MetricsError("record must be an object")
        values = dict(raw)
        if values.get("schema_version") != SCHEMA_VERSION:
            raise MetricsError("unsupported schema_version")
        allowed = {field.name for field in dataclasses.fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise MetricsError("record contains unsupported fields")
        try:
            return cls(**values)
        except TypeError as exc:
            raise MetricsError("record has missing or invalid fields") from exc


def _private_parent(path: Path) -> None:
    """Create a private parent, never changing an adopted directory."""

    # Validate each named component before creating any missing child.  This
    # prevents a path such as ``adopted-link/new`` from creating ``new`` in a
    # symlink target before the link is noticed.
    current = Path(path.anchor or ".")
    for component in path.parent.parts:
        if component in (path.anchor, ""):
            continue
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                component_stat = current.lstat()
            except OSError as exc:
                raise MetricsError("unable to prepare private metrics directory") from exc
        except OSError as exc:
            raise MetricsError("unable to validate metrics directory") from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise MetricsError("metrics directory contains a symlink")
        if not stat.S_ISDIR(component_stat.st_mode):
            raise MetricsError("metrics directory is not a real directory")

    try:
        parent_stat = path.parent.lstat()
    except OSError as exc:
        raise MetricsError("unable to validate metrics directory") from exc
    if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise MetricsError("metrics directory must be owner-only")


def _private_file(path: Path) -> None:
    """Create/validate an owner-only regular file without following links."""

    _private_parent(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError as exc:
            raise MetricsError("unable to open private metrics file") from exc
    except OSError as exc:
        raise MetricsError("unable to create private metrics file") from exc
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise MetricsError("metrics file must be an owner-only regular file")
    finally:
        os.close(fd)


def _open_private(path: Path, flags: int) -> int:
    """Open a validated artifact and re-check its identity via its fd."""

    try:
        fd = os.open(path, flags | os.O_NOFOLLOW)
    except OSError as exc:
        raise MetricsError("unable to open private metrics artifact") from exc
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise MetricsError("metrics artifact must be an owner-only regular file")
    except Exception:
        os.close(fd)
        raise
    return fd


def append_record(path: str | os.PathLike[str], record: MetricRecord) -> None:
    """Append one validated record atomically across threads/processes.

    A sibling lock file is used instead of a process-global lock because
    multiple worktrees and independently spawned runners may share a parent
    directory.  ``O_APPEND`` and ``fsync`` keep a completed receipt durable
    and prevent interleaved JSON lines on local filesystems.
    """

    if not isinstance(record, MetricRecord):
        raise TypeError("record must be a MetricRecord")
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    _private_file(target)
    _private_file(lock_path)
    encoded = (json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    lock_fd = _open_private(lock_path, os.O_RDWR)
    with os.fdopen(lock_fd, "r+b", closefd=True) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fd = _open_private(target, os.O_WRONLY | os.O_APPEND)
            try:
                view = memoryview(encoded)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclasses.dataclass(frozen=True)
class ReadResult:
    """Validated records plus explicit trailing-write diagnostics."""

    records: tuple[MetricRecord, ...]
    incomplete_trailing_lines: int = 0
    corruption_count: int = 0
    first_corrupt_line: int | None = None


def read_records_detailed(path: str | os.PathLike[str]) -> ReadResult:
    """Read receipts, allowing only an unterminated final line to be partial.

    Writers always terminate a record with a newline.  Therefore an invalid
    final line without a newline is treated as an interrupted/incomplete
    write, while any malformed completed line raises a safe corruption error.
    No malformed record is silently discarded.
    """

    target = Path(path)
    if not target.exists():
        return ReadResult(())
    records: list[MetricRecord] = []
    with target.open("rb") as stream:
        lines = stream.readlines()
    incomplete = 0
    corruption_count = 0
    first_corrupt_line = None
    for line_number, line in enumerate(lines, start=1):
        is_unterminated_final = line_number == len(lines) and not line.endswith(b"\n")
        try:
            decoded = line.decode("utf-8")
            raw = json.loads(decoded)
            records.append(MetricRecord.from_mapping(raw))
        except (json.JSONDecodeError, MetricsError, UnicodeDecodeError):
            if is_unterminated_final:
                incomplete += 1
                continue
            corruption_count += 1
            if first_corrupt_line is None:
                first_corrupt_line = line_number
    return ReadResult(
        tuple(records),
        incomplete_trailing_lines=incomplete,
        corruption_count=corruption_count,
        first_corrupt_line=first_corrupt_line,
    )


def read_records(path: str | os.PathLike[str]) -> list[MetricRecord]:
    """Read validated records, raising on completed-line corruption."""

    result = read_records_detailed(path)
    if result.corruption_count:
        raise MetricsCorruptionError(result.first_corrupt_line or 0)
    return list(result.records)


def new_run_id() -> str:
    """Return an opaque run identifier suitable for a receipt."""

    return uuid.uuid4().hex


def _current_monotonic() -> float:
    """A thin, separately-monkeypatchable wrapper around ``time.monotonic()``
    — the CLI's ``record --started-monotonic``/``now`` subcommands go through
    this one function so a test can substitute a deterministic fake clock
    without depending on real wall-clock time passing.

    ``LIFEOS_DEV_METRICS_FAKE_MONOTONIC``, when set, overrides the real clock
    for exactly this purpose across a process boundary (a Python-level
    monkeypatch cannot reach a separate CLI subprocess the way an in-process
    test can) — e.g. a shell caller's own end-to-end test proving its timing
    plumbing is a pure function of two monotonic readings, never dependent on
    real elapsed wall time. Never read or set by production code paths.
    """
    override = os.environ.get("LIFEOS_DEV_METRICS_FAKE_MONOTONIC")
    if override is not None:
        return float(override)
    return time.monotonic()


@dataclasses.dataclass
class _Phase:
    recorder: "MetricsRecorder"
    values: dict[str, Any]
    started: float = dataclasses.field(init=False)
    _finished: bool = False
    error: Exception | None = None

    def __post_init__(self) -> None:
        self.started = self.recorder.clock()

    def finish(
        self,
        *,
        result: str = "success",
        exit_status: int | None = 0,
        cache_hit: bool | None = None,
        elapsed_seconds: float | None = None,
        raise_on_error: bool = False,
    ) -> MetricRecord:
        """Persist this phase, preserving an explicit subprocess status."""

        if self._finished:
            raise MetricsError("phase is already finished")
        self._finished = True
        values = dict(self.values)
        values["result"] = result
        values["exit_status"] = exit_status
        if cache_hit is not None:
            values["cache_hit"] = cache_hit
        if elapsed_seconds is None:
            elapsed_seconds = max(0.0, self.recorder.clock() - self.started)
        values["elapsed_seconds"] = elapsed_seconds
        try:
            record = MetricRecord(**values)
            self.recorder.append(record)
        except Exception as exc:
            # Metrics are observational.  Keep the error available for a
            # caller that wants to surface it, but do not turn a successful
            # or failed suite into a different outcome merely because its
            # local receipt path became unavailable.
            self.error = exc
            self.recorder.last_error = exc
            if raise_on_error:
                raise
            if "record" not in locals():
                raise
        return record

    def cancel(self, *, exit_status: int | None = None) -> MetricRecord:
        return self.finish(result="cancelled", exit_status=exit_status)

    def __enter__(self) -> "_Phase":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._finished:
            return False
        try:
            if exc_type is None:
                self.finish()
            elif exc_type is KeyboardInterrupt:
                self.finish(result="interrupted", exit_status=None)
            elif exc_type is asyncio.CancelledError:
                self.finish(result="cancelled", exit_status=None)
            elif exc_type in (GeneratorExit,):
                self.finish(result="cancelled", exit_status=None)
            else:
                self.finish(result="failure", exit_status=None)
        except Exception as metric_error:
            # Even validation/storage errors must not replace the wrapped
            # suite exception.  ``last_error`` makes this fail-open behavior
            # inspectable; direct ``append``/``record`` calls remain strict.
            self.error = metric_error
            self.recorder.last_error = metric_error
        return False


class MetricsRecorder:
    """Small adapter shared by test runners, evidence, and worker callers."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        run_id: str | None = None,
        candidate_id: str,
        task_id: str | None = None,
        clock: Any = time.monotonic,
    ):
        self.path = Path(path)
        self.run_id = run_id or new_run_id()
        _token(self.run_id, "run_id")
        _token(candidate_id, "candidate_id")
        _token(task_id, "task_id", required=False)
        self.candidate_id = candidate_id
        self.task_id = task_id
        self.last_error: Exception | None = None
        # Injectable only for deterministic focused tests; production callers
        # use the monotonic clock captured from the standard library.
        self.clock = clock

    def append(self, record: MetricRecord) -> None:
        append_record(self.path, record)

    def record(
        self,
        *,
        phase: str,
        phase_kind: str,
        elapsed_seconds: float | int | None,
        result: str,
        suite: str | None = None,
        worker_count: int | None = None,
        cache_hit: bool = False,
        exit_status: int | None = None,
        evidence_ref: str | None = None,
        review_round: int | None = None,
    ) -> MetricRecord:
        record = MetricRecord(
            run_id=self.run_id,
            candidate_id=self.candidate_id,
            task_id=self.task_id,
            phase=phase,
            phase_kind=phase_kind,
            elapsed_seconds=elapsed_seconds,
            suite=suite,
            worker_count=worker_count,
            result=result,
            cache_hit=cache_hit,
            exit_status=exit_status,
            evidence_ref=evidence_ref,
            review_round=review_round,
        )
        self.append(record)
        return record

    def phase(
        self,
        phase: str,
        *,
        phase_kind: str,
        suite: str | None = None,
        worker_count: int | None = None,
        cache_hit: bool = False,
        evidence_ref: str | None = None,
        review_round: int | None = None,
    ) -> _Phase:
        """Time an already-running phase using ``time.monotonic()``."""

        values = {
            "run_id": self.run_id,
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "phase": phase,
            "phase_kind": phase_kind,
            "suite": suite,
            "worker_count": worker_count,
            "cache_hit": cache_hit,
            "evidence_ref": evidence_ref,
            "review_round": review_round,
        }
        return _Phase(self, values)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _stats(values: Iterable[float | None]) -> dict[str, float | int | None]:
    observed = [float(value) for value in values if value is not None]
    return {
        "count": len(observed),
        "median_seconds": _percentile(observed, 0.5),
        "p95_seconds": _percentile(observed, 0.95),
    }


def summarize(records: Sequence[MetricRecord], *, expected_phases: Iterable[str] = ()) -> dict[str, Any]:
    """Return privacy-safe aggregate data for human/CI reporting.

    Missing phase measurements are explicit ``unknown`` values; they are not
    treated as zero and are never imputed from commit or wall-clock dates.
    """

    runs = sorted({record.run_id for record in records})
    candidates: dict[str, list[MetricRecord]] = defaultdict(list)
    phases: dict[str, list[MetricRecord]] = defaultdict(list)
    for record in records:
        candidates[record.candidate_id].append(record)
        phases[record.phase].append(record)

    expected = set(expected_phases)
    phase_summary: dict[str, Any] = {}
    for phase in sorted(set(phases) | expected):
        rows = phases.get(phase, [])
        observed = _stats(row.elapsed_seconds for row in rows)
        present_runs = {row.run_id for row in rows}
        missing = len(set(runs) - present_runs)
        status = "unknown" if not rows else ("partial" if missing else "observed")
        phase_summary[phase] = {
            **observed,
            "present_runs": len(present_runs),
            "missing_runs": missing,
            "status": status,
        }

    result_counts = Counter(record.result for record in records)
    review_rounds = sorted({record.review_round for record in records if record.review_round is not None})
    candidate_summary: dict[str, Any] = {}
    for candidate, rows in sorted(candidates.items()):
        execution_rows = [row for row in rows if row.phase_kind == "execution"]
        candidate_summary[candidate] = {
            "sample_count": len({row.run_id for row in rows}),
            "record_count": len(rows),
            "execution_count": len(execution_rows),
            "executions_by_suite": dict(
                sorted(Counter(row.suite or "unknown" for row in execution_rows).items())
            ),
            "attempts": sum(not row.cache_hit for row in rows),
            "reused_receipts": sum(row.cache_hit for row in rows),
            "review_rounds": sorted({row.review_round for row in rows if row.review_round is not None}),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "sample_count": len(runs),
        "record_count": len(records),
        "duration_seconds": _stats(record.elapsed_seconds for record in records),
        "phases": phase_summary,
        "attempts": sum(not record.cache_hit for record in records),
        "reused_receipts": sum(record.cache_hit for record in records),
        "results": dict(sorted(result_counts.items())),
        "infrastructure_failures": result_counts.get("infrastructure_failure", 0),
        "review_rounds": review_rounds,
        "candidates": candidate_summary,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize private LifeOS development metrics")
    subparsers = parser.add_subparsers(dest="command", required=True)
    summary = subparsers.add_parser("summary", help="summarize local JSONL receipts")
    summary.add_argument("--path", required=True, type=Path, help=argparse.SUPPRESS)
    summary.add_argument("--expected-phase", action="append", default=[], dest="expected_phases")
    summary.add_argument("--text", action="store_true", help="print a concise human-readable summary")

    record = subparsers.add_parser(
        "record",
        help="append one validated phase record — the shared adapter for a "
        "non-Python caller (e.g. a shell runner) that must not hand-roll a "
        "second metrics format",
    )
    record.add_argument("--path", required=True, type=Path, help=argparse.SUPPRESS)
    record.add_argument("--candidate-id", required=True)
    record.add_argument("--run-id", default=None, help="reuse a prior call's printed run_id to group related phases")
    record.add_argument("--task-id", default=None)
    record.add_argument("--phase", required=True)
    record.add_argument("--phase-kind", required=True, choices=sorted(PHASE_KINDS))
    record.add_argument("--result", required=True, choices=sorted(RESULTS))
    duration_group = record.add_mutually_exclusive_group()
    duration_group.add_argument("--elapsed-seconds", type=float, default=None)
    duration_group.add_argument(
        "--started-monotonic", type=float, default=None,
        help="a monotonic timestamp (e.g. printed by the 'now' command below) captured at "
        "phase start; elapsed time is computed here, at record time, as this process's own "
        "time.monotonic() minus that value — never in the caller's shell arithmetic, and "
        "never from a wall-clock timestamp",
    )
    record.add_argument("--suite", default=None)
    record.add_argument("--worker-count", type=int, default=None)
    record.add_argument("--exit-status", type=int, default=None)
    record.add_argument("--evidence-ref", default=None)
    record.add_argument("--cache-hit", action="store_true")

    subparsers.add_parser(
        "now", help="print this process's own time.monotonic() reading, for a non-Python "
        "caller to capture a phase-start timestamp portably (no external 'timeout'/date "
        "arithmetic needed)",
    )

    import_record = subparsers.add_parser(
        "import",
        help="validate and append one pre-formed record read as a single JSON line from "
        "stdin, preserving its own run_id/candidate_id/phase/result exactly as given — for "
        "correlating a real receipt fetched from elsewhere (e.g. a remote host's own "
        "metrics.jsonl for a shared run_id), never for constructing a new record",
    )
    import_record.add_argument("--path", required=True, type=Path, help=argparse.SUPPRESS)
    return parser


def _text_summary(summary: Mapping[str, Any]) -> str:
    duration = summary["duration_seconds"]
    lines = [
        f"samples: {summary['sample_count']}",
        f"observed durations: {duration['count']} (median={duration['median_seconds']}, p95={duration['p95_seconds']})",
        f"attempts: {summary['attempts']}; reused receipts: {summary['reused_receipts']}",
        f"infrastructure failures: {summary['infrastructure_failures']}",
    ]
    for phase, values in summary["phases"].items():
        lines.append(
            f"phase {phase}: {values['status']} (count={values['count']}, "
            f"median={values['median_seconds']}, p95={values['p95_seconds']}, "
            f"missing_runs={values['missing_runs']})"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "summary":
        try:
            value = summarize(read_records(args.path), expected_phases=args.expected_phases)
        except MetricsCorruptionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except (MetricsError, OSError):
            print("unable to read private metrics receipts", file=sys.stderr)
            return 2
        print(_text_summary(value) if args.text else json.dumps(value, indent=2, sort_keys=True))
        return 0
    if args.command == "now":
        # A portable phase-start timestamp for a non-Python caller: no
        # external `timeout`/`date` arithmetic, no wall clock — just this
        # process's own monotonic reading, later subtracted (also in Python,
        # at record time) from a second one taken at phase end.
        print(f"{_current_monotonic():.6f}")
        return 0
    if args.command == "record":
        elapsed_seconds = args.elapsed_seconds
        if args.started_monotonic is not None:
            elapsed_seconds = max(0.0, _current_monotonic() - args.started_monotonic)
        try:
            recorder = MetricsRecorder(
                args.path, run_id=args.run_id, candidate_id=args.candidate_id, task_id=args.task_id,
            )
            recorder.record(
                phase=args.phase, phase_kind=args.phase_kind, elapsed_seconds=elapsed_seconds,
                result=args.result, suite=args.suite, worker_count=args.worker_count,
                cache_hit=args.cache_hit, exit_status=args.exit_status, evidence_ref=args.evidence_ref,
            )
        except (MetricsError, OSError) as exc:
            print(f"unable to record metric: {exc}", file=sys.stderr)
            return 2
        # A caller recording several related phases in one invocation (e.g. a
        # shell script's transfer/execution steps) reuses this to keep them
        # grouped under one run_id, whether it supplied one or this call
        # generated it.
        print(recorder.run_id)
        return 0
    if args.command == "import":
        line = sys.stdin.readline()
        try:
            record = MetricRecord.from_mapping(json.loads(line))
            append_record(args.path, record)
        except (MetricsError, json.JSONDecodeError, OSError) as exc:
            print(f"unable to import metric record: {exc}", file=sys.stderr)
            return 2
        print(record.run_id)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
