"""Verify one isolated candidate and atomically retain reusable local evidence.

The runner integration supplies ``execute_lane``.  This module owns the
identity, receipt, mutation, capacity, and outcome invariants so consumers
cannot accidentally reuse a current-working-directory result for another
candidate.
"""
from __future__ import annotations

import dataclasses
import argparse
import atexit
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
import signal
import uuid
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence

# Direct hook invocation executes this file by path, which otherwise places
# ``scripts/`` (not the checkout root) on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.candidate_snapshot import SnapshotResult, build_snapshot, verify_snapshot_unmodified
from scripts.development_metrics import MetricsRecorder
from scripts.test_capacity import CapacityError, CapacityManager
from scripts.test_instance import TestInstance, run_supervised_command
from scripts.verification_evidence import (
    EvidenceError, EvidenceStore, LaneOutcome, PRIVACY_AUDIT_NODEID,
    PRIVACY_AUDIT_NOT_APPLICABLE_REASON, VerificationInputs,
    fingerprint_named_files, safe_environment_fingerprint,
)
from scripts.test_lane_registry import BY_NAME, EXECUTION_ORDER, marker


RUNNER_INPUTS = ("scripts/test.sh", "scripts/test-lanes.sh", "scripts/test_lane_registry.py", "scripts/test_lane_plugin.py", "scripts/verify_candidate.py", "scripts/verification_evidence.py", "pyproject.toml")
DEPENDENCY_INPUTS = ("requirements.txt",)
_HERMETIC_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "LIFEOS_TEST_PARALLEL_WORKERS": "1",
    "LIFEOS_PARALLEL_BROWSER_FREE": "0",
}
_REDIRECT_EXEC = (
    "import os,sys; "
    "fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600); "
    "os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd); "
    "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)"
)


class CandidateVerificationError(RuntimeError):
    """The candidate cannot be safely verified or reused."""


_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9_.-]+)")


@dataclasses.dataclass(frozen=True)
class VerificationResult:
    candidate_id: str
    evidence_key: str
    reused: bool
    reason: str
    outcomes: tuple[LaneOutcome, ...]


def _snapshot_modes_ok(snapshot: SnapshotResult) -> tuple[bool, list[str]]:
    """Use candidate_snapshot's shared byte/mode/runtime mismatch policy."""
    ok, mismatches = verify_snapshot_unmodified(snapshot)
    return ok, [str(mismatch) for mismatch in mismatches]


def _bounded_snapshot_diagnostics(mismatches: Sequence[str]) -> list[str]:
    """Fit strict snapshot receipts without replacing an integrity failure."""
    if len(mismatches) > 20:
        details = [*mismatches[:19], f"... {len(mismatches) - 19} additional snapshot mismatches omitted"]
    else:
        details = list(mismatches)
    return [detail[:512] for detail in details]


def _resolve_playwright_browsers_path() -> str:
    """Mirrors the installed Playwright driver's own resolution algorithm
    (``registry/index.js``'s ``registryDirectory``) against the *caller's*
    real environment/HOME/cwd -- before HOME gets replaced for the sandboxed
    subprocess below, which would otherwise make Playwright's own in-process
    resolution (it also reads ``os.homedir()``/cwd, just *inside* the
    sandbox) silently pick an empty synthetic directory instead of the real,
    already-installed cache."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit == "0":
        # Package-local `.local-browsers`, resolved by the driver itself
        # relative to its own install location -- unaffected by HOME/cwd,
        # so passed through untouched rather than re-resolved here.
        return explicit
    if explicit:
        result = explicit
    elif sys.platform == "darwin":
        result = str(Path.home() / "Library" / "Caches" / "ms-playwright")
    elif sys.platform == "win32":
        cache_directory = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        result = str(Path(cache_directory) / "ms-playwright")
    else:
        cache_directory = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        result = str(Path(cache_directory) / "ms-playwright")
    # Matches the driver's own trailing step: any relative result (an
    # explicit relative override, or a relative XDG_CACHE_HOME/LOCALAPPDATA)
    # resolves against the original invocation directory, not whatever cwd
    # the sandboxed subprocess ends up running from.
    if not os.path.isabs(result):
        base = os.environ.get("INIT_CWD") or os.getcwd()
        result = os.path.normpath(os.path.join(base, result))
    return result


def _required_chromium_executable() -> Path:
    """The exact binary a bare ``chromium.launch()`` (headless, the only
    mode this suite's ``--browser chromium`` runs use, no channel override)
    resolves to -- per the installed driver's own ``getExecutableName``:
    ``options.headless ? "chromium-headless-shell" : "chromium"`` with no
    channel set -- so ``chromium-headless-shell``, not the full browser.

    Queried by invoking the driver's own bundled Node runtime against its
    own registry module directly (``registry.findExecutable(name)
    .executablePath()``), never a hand-copied revision/platform/executable-
    name table, so the path comes from the installed driver and includes
    cases like a --only-shell-only install. Playwright 1.58 bundles
    that registry in ``coreBundle.js`` while older releases keep the
    standalone module. Never launches a browser."""
    from playwright._impl._driver import compute_driver_executable

    node, cli_path = compute_driver_executable()
    driver_root = Path(cli_path).parent
    registry_module = driver_root / "lib" / "server" / "registry" / "index.js"
    if registry_module.is_file():
        script = f"process.stdout.write(require({str(registry_module)!r}).registry.findExecutable('chromium-headless-shell').executablePath())"
    else:
        core_bundle = driver_root / "lib" / "coreBundle.js"
        script = f"process.stdout.write(require({str(core_bundle)!r}).registry.registry.findExecutable('chromium-headless-shell').executablePath())"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    executable = result.stdout.strip()
    if result.returncode != 0 or not executable or not os.path.isabs(executable):
        raise CandidateVerificationError("playwright driver did not resolve a headless-shell executable path")
    return Path(executable)


def _installed_chromium_prerequisite() -> tuple[str, bool]:
    """Bounded browser-install identity: an opaque digest of the resolved
    executable path (changes if the browsers path or revision changes)
    plus one cheap check that the exact executable exists and is
    executable -- never a directory-presence proxy (a stray file or a
    partial/interrupted download would pass that), and never hashes or
    walks the (multi-GB) cache contents, or records the raw path itself."""
    try:
        executable = _required_chromium_executable()
        installed = executable.is_file() and os.access(executable, os.X_OK)
    except Exception:
        return ("unknown", False)
    identity = hashlib.sha256(str(executable).encode()).hexdigest()[:16]
    return (identity, installed)


def make_hermetic_environment(runtime_root: Path, *, workers: int, parallel_browser_free: bool = False) -> dict[str, str]:
    """The complete, small environment used for both collection and execution."""
    home = runtime_root / "home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LIFEOS_TEST_PARALLEL_WORKERS": str(workers),
        # Explicit, default-false opt-in: browser-free tests never touch a
        # live server (excluded: requires_server in the lane registry), so
        # independent xdist workers are safe there, but this must stay off
        # by default -- the benchmark this flag exists for is what decides
        # whether the *default* ever changes, not this flag itself.
        "LIFEOS_PARALLEL_BROWSER_FREE": "1" if parallel_browser_free else "0",
        "PLAYWRIGHT_BROWSERS_PATH": _resolve_playwright_browsers_path(),
        # Pytest collection imports api.routes.agents, whose agent-viz modules
        # initialize SQLite at Path(settings.chroma_path).parent; the default
        # (./data/chromadb) is relative to cwd, which for lane execution is
        # the immutable snapshot -- writing there would fail the strict
        # snapshot-unmodified check on every browser-marked collection/run.
        "LIFEOS_CHROMA_PATH": str(runtime_root / "chromadb"),
    }


def collect_lane_inventory(snapshot_root: Path, receipt_dir: Path, environment: Mapping[str, str]) -> dict:
    """Use the shared plugin once; JSON, not pytest console scraping, is the API."""
    receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="lane-inventory-", suffix=".json", dir=receipt_dir)
    os.close(fd)
    os.unlink(name)  # plugin requires a new path and writes it atomically.
    # Collection imports application modules and plugins, some of which have
    # legitimate relative runtime state. Keep that state outside the immutable
    # candidate while loading the exact candidate tests/config through an
    # explicit PYTHONPATH and absolute test root.
    collection_cwd = Path(environment["HOME"]).parent
    collection_environment = dict(environment)
    collection_environment["PYTHONPATH"] = str(snapshot_root)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(snapshot_root / "tests"), "--collect-only", "-qq", "-p", "no:cacheprovider", "-p", "scripts.test_lane_plugin", "--lifeos-lane-inventory", name],
        cwd=collection_cwd,
        text=True,
        capture_output=True,
        env=collection_environment,
    )
    def output_tail() -> str:
        # Pytest's normal collection output can contain every node ID, which
        # is not useful (or bounded) in a hosted job log. Collection tracebacks
        # normally go to stdout, while launcher errors go to stderr; retain a
        # short tail of each rather than either whole stream.
        detail = ""
        stderr_tail = result.stderr.strip()[-4000:]
        stdout_tail = result.stdout.strip()[-4000:]
        if stderr_tail:
            detail += f"; stderr tail: {stderr_tail}"
        if stdout_tail:
            detail += f"; stdout tail: {stdout_tail}"
        return detail
    try:
        if not os.path.exists(name):
            raise CandidateVerificationError(
                f"lane collection did not produce an inventory (exit {result.returncode}){output_tail()}"
            )
        inventory = json.loads(Path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateVerificationError("lane collection receipt is invalid") from exc
    finally:
        Path(name).unlink(missing_ok=True)
    if result.returncode != 0 or inventory.get("status") != "ok":
        raise CandidateVerificationError(
            f"lane collection is not complete: {inventory.get('status', 'missing')}"
            f" (exit {result.returncode}){output_tail()}"
        )
    if inventory.get("unassigned") or inventory.get("assigned_count") != inventory.get("collected_count"):
        raise CandidateVerificationError("lane collection does not partition every test")
    return inventory


def _expected(inventory: Mapping, required_lanes: Sequence[str] | None = None, nodeid_paths: Sequence[str] | None = None, nodeids: Sequence[str] | None = None) -> dict[str, tuple[str, ...]]:
    lanes = inventory.get("lanes")
    if not isinstance(lanes, Mapping):
        raise CandidateVerificationError("lane inventory lacks lanes")
    if set(lanes) != set(BY_NAME):
        raise CandidateVerificationError("lane inventory has unknown or missing lanes")
    selected: dict[str, tuple[str, ...]] = {}
    wanted = set(required_lanes) if required_lanes is not None else set(lanes)
    if not wanted or wanted - set(lanes):
        raise CandidateVerificationError("requested lane is absent from inventory")
    requested_nodeids = set(nodeids) if nodeids is not None else None
    eligible_nodeids: set[str] = set()
    for name in EXECUTION_ORDER:
        if name not in lanes:
            raise CandidateVerificationError("lane inventory lacks a registered lane")
        lane = lanes[name]
        lane_nodeids = lane.get("nodeids") if isinstance(lane, Mapping) else None
        if not isinstance(name, str) or not isinstance(lane_nodeids, list) or any(not isinstance(nodeid, str) for nodeid in lane_nodeids):
            raise CandidateVerificationError("lane inventory is malformed")
        if name in wanted and lane_nodeids:
            chosen = tuple(lane_nodeids)
            if nodeid_paths is not None:
                chosen = tuple(nodeid for nodeid in chosen if nodeid.split("::", 1)[0] in nodeid_paths)
            eligible_nodeids.update(chosen)
            if requested_nodeids is not None:
                chosen = tuple(nodeid for nodeid in chosen if nodeid in requested_nodeids)
            if chosen:
                selected[name] = chosen
    if requested_nodeids is not None:
        missing = requested_nodeids - eligible_nodeids
        if missing:
            preview = ", ".join(sorted(missing)[:3])
            raise CandidateVerificationError(
                "requested node ID is absent from the selected lanes or paths: " + preview
            )
    if not selected:
        raise CandidateVerificationError("unexpected zero-test inventory")
    return selected


def _parse_optional_exact_csv(value: str | None, option: str) -> tuple[str, ...] | None:
    """Keep omitted selection distinct from an invalid explicit selection."""
    if value is None:
        return None
    values = tuple(value.split(","))
    if not all(values):
        raise CandidateVerificationError(
            f"{option} must contain one or more nonempty comma-separated values"
        )
    return values


def _record_metric(metrics: MetricsRecorder | None, **values: object) -> None:
    """Metrics are observational and must never alter verification status."""
    if metrics is None:
        return
    try:
        metrics.record(**values)
    except Exception:
        pass


# A LaneOutcome's own result vocabulary (verification_evidence.py) is a
# superset of development_metrics.py's RESULTS enum ("incomplete" has no
# direct equivalent there); map it to the closest honest label rather than
# widening the shared metrics schema for one caller's internal state.
_LANE_RESULT_TO_METRIC = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "incomplete": "unknown",
    "infrastructure_failure": "infrastructure_failure",
}


def _lane_failure_exit_status(outcome: "LaneOutcome") -> int | None:
    """A non-success metric record can never carry exit_status 0 (the schema
    forbids it as misleading); a lane that reports 0 despite a non-success
    result is recorded without a fabricated numeric code instead."""
    return outcome.exit_status if outcome.exit_status not in (0, None) else None


def build_inputs(
    snapshot: SnapshotResult,
    inventory: Mapping,
    *,
    base_identity: str | None = None,
    merge_identity: str | None = None,
    environment: Mapping[str, str] = {},
    scope_identity: str | None = None,
) -> VerificationInputs:
    root = Path(snapshot.dest_root)
    return VerificationInputs(
        content_fingerprint=snapshot.fingerprint,
        lane_inventory_fingerprint=__import__("hashlib").sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        runner_fingerprint=fingerprint_named_files(root, RUNNER_INPUTS),
        dependency_fingerprint=_installed_dependency_fingerprint(root),
        environment_fingerprint=safe_environment_fingerprint(environment),
        base_identity=base_identity,
        merge_identity=merge_identity,
        scope_identity=scope_identity,
    )


def _installed_dependency_fingerprint(root: Path) -> str:
    """Bind the manifest to the interpreter's installed package identities.

    This deliberately records only a digest of package names/versions plus
    Python implementation/version — never an executable path, wheel cache,
    environment dump, or package metadata payload. Collection and execution
    both use this module's ``sys.executable`` under the same hermetic env.
    """
    manifest = root / "requirements.txt"
    if manifest.is_symlink() or not manifest.is_file():
        raise EvidenceError("required verification input is missing: requirements.txt")
    identities: list[tuple[str, str]] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", ".", "/")):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match is None:
            continue
        name = match.group(1).lower().replace("_", "-")
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = "missing"
        identities.append((name, version))
    chromium_revision, chromium_installed = _installed_chromium_prerequisite()
    payload = {
        "manifest": fingerprint_named_files(root, DEPENDENCY_INPUTS),
        "python": (sys.implementation.name, sys.version_info[:3]),
        "packages": sorted(set(identities)),
        # The playwright *package* version alone doesn't prove its browser
        # binary is actually present -- that's a separate download step
        # (`playwright install`). Without this, evidence from a run made
        # before/after that install step (same package version either way)
        # would wrongly look identical and could be reused across a real
        # pass/fail change. Bounded to a revision string + one bool, never
        # the cache path or contents.
        "chromium_prerequisite": (chromium_revision, chromium_installed),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _new_private_nodeid_file(runtime_root: Path, nodeids: tuple[str, ...]) -> Path:
    """Persist exact collected IDs outside the candidate without argv limits."""
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="lane-nodeids-", suffix=".txt", dir=runtime_root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(nodeids))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def pytest_lane_executor(
    environment: Mapping[str, str], *, workers: int, lane_log_dir: Path | None = None,
    process_started: Callable[[int, float], None] | None = None, parallel_browser_free: bool = False,
) -> Callable[[Path, str, tuple[str, ...]], LaneOutcome]:
    """Real isolated pytest subprocess adapter; node IDs are the execution API."""
    bound_snapshot: SnapshotResult | None = None

    def bind_snapshot(snapshot: SnapshotResult) -> None:
        nonlocal bound_snapshot
        bound_snapshot = snapshot

    def execute(snapshot_root: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        if lane not in BY_NAME:
            raise CandidateVerificationError(f"unknown lane: {lane}")
        runtime_root = Path(environment["HOME"]).parent
        nodeid_file = _new_private_nodeid_file(runtime_root, nodeids)
        if lane_log_dir is not None:
            lane_log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt = lane_log_dir / f"{lane}.json"
            if receipt.exists():
                receipt.unlink()
            output_log = lane_log_dir / f"{lane}.log"
        else:
            # Keep pytest's human-readable output out of the verifier's
            # stdout. Callers commonly redirect stdout to the authoritative
            # top-level receipt, which must remain one JSON document rather
            # than a pytest transcript followed by JSON. This private log is
            # removed with the runtime root after the lane completes.
            execution_id = uuid.uuid4().hex
            receipt = runtime_root / f"execution-{execution_id}.json"
            output_log = runtime_root / f"execution-{execution_id}.log"
        command = [sys.executable, "-m", "pytest", "tests", "-q", "--tb=short", "--ignore=tests/archive", "-p", "no:cacheprovider", "-p", "scripts.test_lane_plugin", "--lifeos-lane-nodeids", str(nodeid_file), "--lifeos-lane-execution", str(receipt), "-m", marker(BY_NAME[lane])]
        if lane == "browser-free" and parallel_browser_free and workers > 1:
            # Explicit opt-in only -- default false, so an ordinary caller's
            # workers>1 request never silently changes browser-free from
            # its serial default. Fails loudly rather than silently
            # collapsing back to a serial run that would misreport as
            # "parallel" evidence.
            if importlib.util.find_spec("xdist") is None:
                raise CandidateVerificationError(
                    "explicit browser-free parallel opt-in requires pytest-xdist, which is not installed"
                )
            command.extend(["-n", str(workers), "--dist", "loadscope"])
        elif lane in {"fast-unit", "slow"} and workers > 1 and importlib.util.find_spec("xdist") is not None:
            command.extend(["-n", str(workers), "--dist", "loadscope"])
        if "playwright" in BY_NAME[lane]["prerequisites"]:
            command.extend(["--browser", "chromium"])
        runner_environment = dict(environment)
        instance: TestInstance | None = None
        if "server" in BY_NAME[lane]["prerequisites"]:
            if bound_snapshot is None:
                raise CandidateVerificationError("server lane lacks its bound candidate snapshot")
            instance = TestInstance(
                snapshot_root, existing_snapshot=bound_snapshot, process_started=process_started,
            )
            instance.start()
            runner_environment = instance.command_environment()
            runner_environment.update({
                "PYTHONHASHSEED": environment["PYTHONHASHSEED"],
                "LIFEOS_TEST_PARALLEL_WORKERS": environment["LIFEOS_TEST_PARALLEL_WORKERS"],
                # Nested test-instance calls enter server.sh under this
                # lane's synthetic HOME; keep its interpreter bound to the
                # verifier's canonical environment rather than that empty
                # private home directory.
                "LIFEOS_VENV_PYTHON": sys.executable,
                # The embedding lock's production default is relative to the
                # checkout.  A candidate's test process must keep that
                # runtime artifact beside its owned data, not mutate its
                # immutable source snapshot.
                "LIFEOS_EMBEDDING_GPU_LOCK_PATH": str(
                    Path(instance.manifest.data_dir) / "gpu_embed.lock"
                ),
            })
            if "PLAYWRIGHT_BROWSERS_PATH" in environment:
                # TestInstance intentionally replaces HOME with a private
                # directory. Preserve the caller-resolved browser cache so
                # browser+server lanes do not make Playwright look under that
                # empty synthetic HOME.
                runner_environment["PLAYWRIGHT_BROWSERS_PATH"] = environment["PLAYWRIGHT_BROWSERS_PATH"]
        try:
            supervised_command = command if output_log is None else [
                sys.executable, "-c", _REDIRECT_EXEC, str(output_log), *command,
            ]
            result = run_supervised_command(
                supervised_command, cwd=str(snapshot_root), env=runner_environment,
                process_started=process_started,
            )
        finally:
            if instance is not None:
                instance.stop()
            nodeid_file.unlink(missing_ok=True)
        try:
            execution = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return LaneOutcome(lane, nodeids, result.returncode or 1, "incomplete")
        finally:
            if lane_log_dir is None:
                receipt.unlink(missing_ok=True)
        reports = execution.get("reports")
        candidates = execution.get("not_applicable_candidates", {})
        not_applicable: tuple[tuple[str, str], ...] = ()
        if not isinstance(candidates, dict):
            return LaneOutcome(lane, nodeids, result.returncode, "failure")
        expected_candidate = {PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON}
        if candidates:
            if (
                candidates != expected_candidate
                or not isinstance(reports, dict)
                or PRIVACY_AUDIT_NODEID not in nodeids
                or reports.get(PRIVACY_AUDIT_NODEID) != "skipped"
            ):
                return LaneOutcome(lane, nodeids, result.returncode, "failure")
            not_applicable = ((PRIVACY_AUDIT_NODEID, PRIVACY_AUDIT_NOT_APPLICABLE_REASON),)
        passed_count = sum(1 for nodeid in nodeids if isinstance(reports, dict) and reports.get(nodeid) == "passed")
        complete = (
            execution.get("status") == "success"
            and isinstance(reports, dict)
            and set(reports) == set(nodeids)
            and passed_count > 0
            and all(
                reports[nodeid] == "passed"
                or (nodeid == PRIVACY_AUDIT_NODEID and bool(not_applicable))
                for nodeid in nodeids
            )
        )
        outcome_result = "success" if complete and result.returncode == 0 else "failure"
        return LaneOutcome(
            lane, nodeids, result.returncode,
            outcome_result,
            not_applicable=not_applicable if outcome_result == "success" else (),
        )
    setattr(execute, "bind_snapshot", bind_snapshot)
    return execute


def verify_candidate(
    source_root: Path,
    snapshot_root: Path,
    evidence_root: Path,
    execute_lane: Callable[[Path, str, tuple[str, ...]], LaneOutcome],
    *,
    base_identity: str | None = None,
    merge_identity: str | None = None,
    environment: Mapping[str, str] = {},
    inventory_receipt_root: Path | None = None,
    capacity: CapacityManager | None = None,
    metrics: MetricsRecorder | None = None,
    workers: int = 1,
    retry_reason: str | None = None,
    hermetic_environment: bool = False,
    required_lanes: Sequence[str] | None = None,
    runtime_root: Path | None = None,
    nodeid_paths: Sequence[str] | None = None,
    nodeids: Sequence[str] | None = None,
    capacity_held_externally: bool = False,
    external_capacity_wait_seconds: float | None = None,
    metrics_run_id: str | None = None,
) -> VerificationResult:
    """Snapshot, collect, execute every selected lane, then atomically record.

    A caller must provide a fresh ``snapshot_root``.  Reuse stays content based:
    the snapshot's git head is deliberately not part of ``VerificationInputs``.

    ``metrics_run_id``, when given, is used only for a metrics recorder this
    call constructs itself (``metrics is None``); a caller-supplied
    ``metrics`` object already carries its own run identity. This is what
    lets an external correlator (e.g. a remote wrapper that dispatched this
    exact run over SSH) tie this run's real queue/execution records back to
    its own opaque run identity without this function needing to know
    anything about that caller.
    """
    snapshot = build_snapshot(source_root, snapshot_root)
    binder = getattr(execute_lane, "bind_snapshot", None)
    if binder is not None:
        binder(snapshot)
    store = EvidenceStore(evidence_root)
    # Concrete runner entry points are hermetic. They acquire the shared
    # capacity namespace once here, after the candidate identity exists, so
    # collection and every supervised lane share one outer ownership edge.
    if hermetic_environment and capacity is None and not capacity_held_externally:
        capacity = CapacityManager()
    if hermetic_environment and metrics is None:
        try:
            metrics = MetricsRecorder(evidence_root / "metrics.jsonl", run_id=metrics_run_id, candidate_id=snapshot.candidate_id)
        except Exception:
            # Metrics are observational; an unavailable private receipt never
            # turns a passing or failing candidate into another result.
            metrics = None
    actual_runtime_root = runtime_root or (inventory_receipt_root or evidence_root) / f"runtime-{snapshot.candidate_id}"
    execution_environment = make_hermetic_environment(actual_runtime_root, workers=workers)
    if environment:
        if set(environment) - {"LIFEOS_TEST_PARALLEL_WORKERS", "PYTHONHASHSEED", "LIFEOS_PARALLEL_BROWSER_FREE"}:
            raise CandidateVerificationError("caller environment is not a safe execution control")
        execution_environment.update(environment)
    inventory = collect_lane_inventory(Path(snapshot.dest_root), inventory_receipt_root or evidence_root, execution_environment)
    expected = _expected(inventory, required_lanes, nodeid_paths, nodeids)
    scope = ",".join(sorted(expected))
    inputs = build_inputs(snapshot, inventory, base_identity=base_identity, merge_identity=merge_identity, environment={name: execution_environment[name] for name in _HERMETIC_ENVIRONMENT}, scope_identity=scope)
    reusable, reason = store.reusable(inputs, expected)
    # An adapter that inherits arbitrary ambient environment cannot prove that
    # an omitted secret/config value did not affect tests.  Keep its receipt
    # for diagnostics, but never reuse it; only an adapter that establishes a
    # clean environment may opt into reuse with the explicit safe allowlist.
    if not hermetic_environment:
        reusable, reason = None, "ambient_environment_unproven"
    if reusable is not None:
        outcomes = tuple(LaneOutcome(item["lane"], tuple(item["nodeids"]), item["exit_status"], item["result"]) for item in reusable["outcomes"])
        _record_metric(metrics, phase="verification-cache", phase_kind="execution", elapsed_seconds=0, result="success", cache_hit=True, suite=scope, worker_count=workers, evidence_ref=inputs.key[:24])
        return VerificationResult(snapshot.candidate_id, inputs.key, True, reason, outcomes)
    if reason == "prior_infrastructure_failure" and not retry_reason:
        raise CandidateVerificationError("infrastructure retry requires a recorded reason")

    valid, mismatches = _snapshot_modes_ok(snapshot)
    if not valid:
        raise CandidateVerificationError("snapshot changed before execution: " + "; ".join(mismatches[:3]))
    # A durable outer owner may already hold this exact manager (including a
    # synthetic test manager rather than the canonical host root). Never
    # reacquire it here: that would deadlock a one-slot manager.
    lease = capacity.acquire(workers) if capacity is not None and not capacity_held_externally else None
    outcomes: list[LaneOutcome] = []
    started = time.monotonic()
    recorded = False
    try:
        if lease is not None:
            _record_metric(metrics, phase="verification-queue", phase_kind="waiting", elapsed_seconds=lease.waited_seconds, result="success", suite=scope, worker_count=workers, evidence_ref=inputs.key[:24])
        elif capacity_held_externally and external_capacity_wait_seconds is not None:
            # The durable-owner path (verify_git_ref / CLI `local`) acquires
            # capacity before calling this function. The caller measures
            # that wait and supplies it once the candidate identity for
            # this record is known, preserving the measurement.
            _record_metric(metrics, phase="verification-queue", phase_kind="waiting", elapsed_seconds=external_capacity_wait_seconds, result="success", suite=scope, worker_count=workers, evidence_ref=inputs.key[:24])
        for lane, nodeids in expected.items():
            outcome = execute_lane(Path(snapshot.dest_root), lane, nodeids)
            if outcome.lane != lane or tuple(outcome.nodeids) != nodeids:
                raise CandidateVerificationError("lane executor did not report exactly the selected node IDs")
            outcomes.append(outcome)
            if outcome.result != "success" or outcome.exit_status != 0:
                lane_result = outcome.result if outcome.result != "success" else "failure"
                store.record(inputs, outcomes, result=lane_result, retry_reason=retry_reason)
                recorded = True
                _record_metric(
                    metrics, phase="verification-execution", phase_kind="execution",
                    elapsed_seconds=time.monotonic() - started,
                    result=_LANE_RESULT_TO_METRIC.get(lane_result, "failure"),
                    exit_status=_lane_failure_exit_status(outcome),
                    suite=scope, worker_count=workers, evidence_ref=inputs.key[:24],
                )
                return VerificationResult(snapshot.candidate_id, inputs.key, False, "executed_failure", tuple(outcomes))
        valid, mismatches = _snapshot_modes_ok(snapshot)
        if not valid:
            store.record(
                inputs, outcomes, result="incomplete", retry_reason=retry_reason,
                diagnostics=_bounded_snapshot_diagnostics(mismatches),
            )
            recorded = True
            raise CandidateVerificationError("snapshot changed during execution: " + "; ".join(mismatches[:3]))
        store.record(inputs, outcomes, result="success", retry_reason=retry_reason)
        recorded = True
        _record_metric(metrics, phase="verification-execution", phase_kind="execution", elapsed_seconds=time.monotonic() - started, result="success", suite=scope, worker_count=workers, evidence_ref=inputs.key[:24])
        return VerificationResult(snapshot.candidate_id, inputs.key, False, reason, tuple(outcomes))
    except BaseException as exc:
        # A launcher crash/cancellation is never a reusable absence of data.
        # Preserve any already-observed lane outcomes for an explicit later
        # infrastructure retry, while retaining the caller's original error:
        # this handler only ever records an observational metric and always
        # re-raises the exact original exception unchanged (`raise` with no
        # arguments), never substituting a different one or swallowing it.
        if not recorded:
            store.record(inputs, outcomes, result="infrastructure_failure", retry_reason=retry_reason)
        metric_result = "interrupted" if isinstance(exc, KeyboardInterrupt) else "infrastructure_failure"
        _record_metric(
            metrics, phase="verification-execution", phase_kind="execution",
            elapsed_seconds=time.monotonic() - started, result=metric_result,
            suite=scope, worker_count=workers, evidence_ref=inputs.key[:24],
        )
        raise
    finally:
        if lease is not None:
            lease.release()


def verify_pytest_candidate(
    source_root: Path,
    snapshot_root: Path,
    evidence_root: Path,
    *,
    required_lanes: Sequence[str],
    base_identity: str | None = None,
    merge_identity: str | None = None,
    workers: int = 1,
    capacity: CapacityManager | None = None,
    metrics: MetricsRecorder | None = None,
    lane_log_dir: Path | None = None,
    nodeid_paths: Sequence[str] | None = None,
    nodeids: Sequence[str] | None = None,
    capacity_held_externally: bool = False,
    process_started: Callable[[int, float], None] | None = None,
    external_capacity_wait_seconds: float | None = None,
    metrics_run_id: str | None = None,
    parallel_browser_free: bool = False,
) -> VerificationResult:
    """Concrete hermetic pytest entry point used by local runner adapters."""
    runtime_root = snapshot_root.parent / f"{snapshot_root.name}-runtime"
    environment = make_hermetic_environment(runtime_root, workers=workers, parallel_browser_free=parallel_browser_free)
    return verify_candidate(
        source_root, snapshot_root, evidence_root,
        pytest_lane_executor(
            environment, workers=workers, lane_log_dir=lane_log_dir,
            process_started=process_started, parallel_browser_free=parallel_browser_free,
        ),
        base_identity=base_identity, merge_identity=merge_identity,
        environment={
            "LIFEOS_TEST_PARALLEL_WORKERS": str(workers),
            "LIFEOS_PARALLEL_BROWSER_FREE": "1" if parallel_browser_free else "0",
        },
        capacity=capacity, metrics=metrics, workers=workers,
        hermetic_environment=True, required_lanes=required_lanes,
        runtime_root=runtime_root, nodeid_paths=nodeid_paths, nodeids=nodeids,
        capacity_held_externally=capacity_held_externally,
        external_capacity_wait_seconds=external_capacity_wait_seconds,
        metrics_run_id=metrics_run_id,
    )


def _start_candidate_owner(
    repository: Path,
    parent: Path,
    *,
    worktree: Path | None,
    workers: int,
    capacity: CapacityManager | None = None,
) -> tuple[int, int, float]:
    """Start the detached owner and return liveness/process-registry fds plus
    the real wall time this call blocked waiting for it to acquire capacity —
    the only true measurement of that wait, since it happens entirely inside
    the detached subprocess before any candidate identity exists here."""
    owner_read, owner_write = os.pipe()
    ready_read, ready_write = os.pipe()
    process_read, process_write = os.pipe()
    handed_off = False
    manager = capacity or CapacityManager()
    wait_started = time.monotonic()
    try:
        command = [
            sys.executable, str(Path(__file__).with_name("_candidate_owner_bootstrap.py")),
            "--owner-fd", str(owner_read), "--ready-fd", str(ready_write),
            "--process-fd", str(process_read),
            "--repository", str(repository), "--parent", str(parent),
            "--workers", str(workers),
            "--capacity-root", str(manager.state_root),
            "--capacity-total", str(manager.total_workers),
            "--capacity-max-run", str(manager.max_run_workers),
        ]
        if worktree is not None:
            command.extend(("--worktree", str(worktree)))
        subprocess.Popen(
            command, pass_fds=(owner_read, ready_write, process_read), start_new_session=True,
        )
        os.close(owner_read)
        owner_read = -1
        os.close(ready_write)
        ready_write = -1
        os.close(process_read)
        process_read = -1
        if os.read(ready_read, 1) != b"1":
            raise CandidateVerificationError("candidate owner did not acquire capacity")
        waited_seconds = time.monotonic() - wait_started
        handed_off = True
        return owner_write, process_write, waited_seconds
    finally:
        descriptors = (owner_read, ready_read, ready_write, process_read)
        if not handed_off:
            descriptors += (owner_write, process_write)
        for fd in descriptors:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def verify_git_ref(
    repository: Path,
    source_sha: str,
    evidence_root: Path,
    *,
    base_identity: str | None,
    required_lanes: Sequence[str],
    workers: int = 1,
    capacity: CapacityManager | None = None,
    metrics: MetricsRecorder | None = None,
    lane_log_dir: Path | None = None,
) -> VerificationResult:
    """Verify a pushed commit in a temporary detached worktree, never cwd.

    A local-only ref is rejected before creating anything.  The owned parent
    directory and worktree path are fixed by this function, so cleanup cannot
    target an operator checkout.
    """
    repository = repository.resolve()
    check = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", f"{source_sha}^{{commit}}"], capture_output=True, text=True)
    if check.returncode != 0:
        raise CandidateVerificationError("pushed source SHA is not a local commit")
    resolved_sha = check.stdout.strip()
    parent = Path(tempfile.mkdtemp(prefix="lifeos-pushed-candidate-"))
    worktree = parent / "source"
    snapshot = parent / "snapshot"
    cleaned = False
    owner_write = process_write = -1

    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        if worktree.exists():
            subprocess.run(["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)], capture_output=True, text=True)
        shutil.rmtree(parent, ignore_errors=True)

    # The detached owner covers SIGKILL. This process retains local cleanup so
    # ordinary errors return promptly without waiting for owner-pipe EOF.
    atexit.register(cleanup)
    try:
        subprocess.run(["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree), resolved_sha], check=True, capture_output=True, text=True)
        owner_write, process_write, capacity_wait_seconds = _start_candidate_owner(
            repository, parent, worktree=worktree, workers=workers, capacity=capacity,
        )
        def register_group(pid: int, started: float) -> None:
            try:
                os.write(process_write, f"{pid} {started:.9f}\n".encode("ascii"))
            except OSError as exc:
                raise CandidateVerificationError("candidate owner lost process registry") from exc
        return verify_pytest_candidate(
            worktree, snapshot, evidence_root, required_lanes=required_lanes,
            base_identity=base_identity, merge_identity=resolved_sha, workers=workers,
            capacity=capacity, metrics=metrics, lane_log_dir=lane_log_dir,
            capacity_held_externally=True,
            process_started=register_group,
            external_capacity_wait_seconds=capacity_wait_seconds,
        )
    finally:
        for fd in (owner_write, process_write):
            try:
                os.close(fd)
            except OSError:
                pass
        cleanup()
        atexit.unregister(cleanup)


def _main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pushed = sub.add_parser("pushed-ref", help="verify an exact local pushed commit in an isolated worktree")
    pushed.add_argument("--repository", required=True, type=Path)
    pushed.add_argument("--sha", required=True)
    pushed.add_argument("--base", default=None)
    pushed.add_argument("--evidence-root", required=True, type=Path)
    pushed.add_argument("--lanes", default="fast-unit,browser-free")
    pushed.add_argument("--workers", default=1, type=int)
    pushed.add_argument(
        "--capacity-max-run-workers", type=int,
        help="explicit per-run override within the configured canonical host total",
    )
    pushed.add_argument("--lane-log-dir", type=Path)
    local = sub.add_parser("local", help="verify this exact dirty working tree in an isolated snapshot")
    local.add_argument("--source", default=Path.cwd(), type=Path)
    local.add_argument("--evidence-root", required=True, type=Path)
    local.add_argument("--lanes", default="fast-unit,browser-free")
    local.add_argument("--workers", default=1, type=int)
    local.add_argument(
        "--capacity-max-run-workers", type=int,
        help="explicit per-run override within the configured canonical host total",
    )
    local.add_argument("--lane-log-dir", type=Path)
    local.add_argument("--paths", default=None, help="comma-separated exact test-file paths from one collected inventory")
    local.add_argument("--nodeids", default=None, help="comma-separated exact collected node IDs")
    local.add_argument(
        "--run-id", default=os.environ.get("LIFEOS_DEV_METRICS_RUN_ID"),
        help="reuse an external opaque run identity (e.g. LIFEOS_DEV_METRICS_RUN_ID from a "
        "remote dispatcher) for this run's own metrics records, instead of generating a new one",
    )
    local.add_argument(
        "--parallel-browser-free", action="store_true",
        help="explicit opt-in (default off) to run the browser-free lane under xdist with "
        "--workers as its worker count; browser-free never touches a live server, so this is "
        "safe, but the default stays serial until measurements justify changing it",
    )
    args = parser.parse_args(argv)
    if args.command == "pushed-ref":
        lanes = tuple(filter(None, args.lanes.split(",")))
        try:
            capacity = CapacityManager(max_run_workers=args.capacity_max_run_workers)
            result = verify_git_ref(args.repository, args.sha, args.evidence_root, base_identity=args.base, required_lanes=lanes, workers=args.workers, capacity=capacity, lane_log_dir=args.lane_log_dir)
        except (CapacityError, CandidateVerificationError, EvidenceError, subprocess.CalledProcessError) as exc:
            print(f"candidate verification failed: {exc}", file=sys.stderr)
            return 1
    elif args.command == "local":
        lanes = tuple(filter(None, args.lanes.split(",")))
        try:
            nodeid_paths = _parse_optional_exact_csv(args.paths, "--paths")
            nodeids = _parse_optional_exact_csv(args.nodeids, "--nodeids")
        except CandidateVerificationError as exc:
            print(f"candidate verification failed: {exc}", file=sys.stderr)
            return 1
        source = args.source.resolve()
        parent = Path(tempfile.mkdtemp(prefix="lifeos-local-candidate-"))
        snapshot = parent / "snapshot"
        owner_write = process_write = -1
        try:
            capacity = CapacityManager(max_run_workers=args.capacity_max_run_workers)
            owner_write, process_write, capacity_wait_seconds = _start_candidate_owner(
                source, parent, worktree=None, workers=args.workers, capacity=capacity,
            )
            def register_group(pid: int, started: float) -> None:
                try:
                    os.write(process_write, f"{pid} {started:.9f}\n".encode("ascii"))
                except OSError as exc:
                    raise CandidateVerificationError("candidate owner lost process registry") from exc
            result = verify_pytest_candidate(
                source, snapshot, args.evidence_root, required_lanes=lanes,
                workers=args.workers, lane_log_dir=args.lane_log_dir,
                capacity=capacity,
                nodeid_paths=nodeid_paths,
                nodeids=nodeids,
                capacity_held_externally=True,
                process_started=register_group,
                external_capacity_wait_seconds=capacity_wait_seconds,
                metrics_run_id=args.run_id,
                parallel_browser_free=args.parallel_browser_free,
            )
        except (CapacityError, CandidateVerificationError, EvidenceError, subprocess.CalledProcessError) as exc:
            print(f"candidate verification failed: {exc}", file=sys.stderr)
            return 1
        finally:
            for fd in (owner_write, process_write):
                try:
                    os.close(fd)
                except OSError:
                    pass
            shutil.rmtree(snapshot.parent, ignore_errors=True)
    else:
        return 2
    # ``reason`` describes cache selection (and is ``missing`` on the first
    # successful execution); it is not the verification result.  Keep it for
    # diagnostics, but publish an authoritative top-level result so consumers
    # cannot mistake a stale/missing receipt reason for the candidate outcome.
    # ``evidence_key`` links this summary to the lane-attributable receipt
    # under ``--evidence-root``.
    outcome_result = (
        "success"
        if result.outcomes and all(
            outcome.result == "success" and outcome.exit_status == 0
            for outcome in result.outcomes
        )
        else "failure"
    )
    payload = {
        "candidate_id": result.candidate_id,
        "evidence_key": result.evidence_key,
        "reused": result.reused,
        "result": outcome_result,
        "reason": result.reason,
        "lanes": [outcome.lane for outcome in result.outcomes],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if all(outcome.result == "success" and outcome.exit_status == 0 for outcome in result.outcomes) else 1


def _supervised_main(argv: Sequence[str]) -> int:
    """Turn normal terminal termination into Python unwinding for cleanup.

    SIGKILL remains uncatchable here, but the detached candidate owner retains
    capacity, reaps its descendant group, and removes only its registered
    snapshot/worktree after its liveness pipe closes.
    """
    received: list[int] = []

    def interrupt(signum, _frame):
        received.append(signum)
        raise KeyboardInterrupt

    previous = {signum: signal.signal(signum, interrupt) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 128 + (received[-1] if received else signal.SIGINT)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


__all__ = ["CandidateVerificationError", "VerificationResult", "build_inputs", "collect_lane_inventory", "make_hermetic_environment", "pytest_lane_executor", "verify_candidate", "verify_pytest_candidate", "verify_git_ref"]


if __name__ == "__main__":
    raise SystemExit(_supervised_main(sys.argv[1:]))
