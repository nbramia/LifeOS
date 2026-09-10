"""Focused synthetic-repository coverage for local verification evidence."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from scripts.verify_candidate import (
    CandidateVerificationError, _installed_chromium_prerequisite, _installed_dependency_fingerprint,
    collect_lane_inventory, make_hermetic_environment, pytest_lane_executor, verify_candidate, verify_git_ref,
    verify_pytest_candidate,
)
from scripts.development_metrics import MetricsRecorder, read_records
from scripts.test_capacity import CapacityManager
from scripts.verification_evidence import EvidenceStore, LaneOutcome, VerificationInputs, safe_environment_fingerprint
from scripts.verification_evidence import PRIVACY_AUDIT_NODEID, PRIVACY_AUDIT_NOT_APPLICABLE_REASON


REPO = Path(__file__).resolve().parent.parent


def _pids_in_directory(parent: Path) -> list[int]:
    target = os.path.realpath(parent)
    found = []
    for process in psutil.process_iter(["pid", "cwd"]):
        try:
            cwd = process.info["cwd"]
            if cwd and os.path.commonpath((target, os.path.realpath(cwd))) == target:
                found.append(process.info["pid"])
        except (psutil.Error, ValueError):
            pass
    return found


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _source_repo(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    for name in (
        "test.sh", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py",
        "verify_candidate.py", "verification_evidence.py", "candidate_snapshot.py",
        "test_instance.py", "_supervised_exec_bootstrap.py",
    ):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    (root / "requirements.txt").write_text("synthetic-dependency==1\n")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit', 'integration']\n")
    (root / "app.py").write_text("VALUE = 'initial'\n")
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_synthetic(): assert True\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Synthetic Verifier")
    _git(root, "config", "user.email", "verifier@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "synthetic baseline")
    return root


def _executor(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
    """A real subprocess body, with its exact selected IDs reported back."""
    result = subprocess.run(
        [sys.executable, "-c", "assert True"],
        cwd=snapshot,
    )
    return LaneOutcome(lane, nodeids, result.returncode, "success" if result.returncode == 0 else "failure")


@pytest.mark.unit
def test_collection_failure_reports_bounded_real_import_error(tmp_path):
    """A real pytest import failure reaches the hosted diagnostic without full output."""
    snapshot = tmp_path / "snapshot"
    (snapshot / "scripts").mkdir(parents=True)
    (snapshot / "tests").mkdir()
    shutil.copy2(REPO / "scripts" / "test_lane_plugin.py", snapshot / "scripts" / "test_lane_plugin.py")
    shutil.copy2(REPO / "scripts" / "test_lane_registry.py", snapshot / "scripts" / "test_lane_registry.py")
    shutil.copy2(REPO / "scripts" / "verification_evidence.py", snapshot / "scripts" / "verification_evidence.py")
    (snapshot / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    (snapshot / "tests" / "test_broken_import.py").write_text("raise ImportError('missing synthetic dependency')\n")
    with pytest.raises(CandidateVerificationError, match=r"(?s)exit 2.*ImportError: missing synthetic dependency") as exc:
        collect_lane_inventory(snapshot, tmp_path / "receipts", {"HOME": str(tmp_path / "home")})
    assert "stdout tail" in str(exc.value)
    assert len(str(exc.value)) <= 4300


def _verify(root: Path, tmp_path: Path, *, executor=_executor, base: str | None = "base-a", environment=None):
    return verify_candidate(
        root,
        tmp_path / f"snapshot-{len(list(tmp_path.glob('snapshot-*')))}",
        tmp_path / "evidence",
        executor,
        base_identity=base,
        environment={} if environment is None else environment,
        hermetic_environment=True,
    )


@pytest.mark.unit
def test_dirty_precommit_content_reuses_after_commit_without_git_head_key(tmp_path):
    root = _source_repo(tmp_path)
    (root / "app.py").write_text("VALUE = 'dirty but tested'\n")
    first = _verify(root, tmp_path)
    assert not first.reused
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "commit exact tested tree")
    second = _verify(root, tmp_path)
    assert second.reused
    assert second.candidate_id == first.candidate_id


@pytest.mark.unit
def test_unproven_ambient_environment_records_but_never_reuses(tmp_path):
    root = _source_repo(tmp_path)
    first = verify_candidate(root, tmp_path / "snapshot-one", tmp_path / "evidence", _executor)
    second = verify_candidate(root, tmp_path / "snapshot-two", tmp_path / "evidence", _executor)
    assert not first.reused and not second.reused
    assert second.reason == "ambient_environment_unproven"


@pytest.mark.unit
def test_snapshot_mutation_during_execution_records_incomplete_not_success(tmp_path):
    root = _source_repo(tmp_path)

    def mutating(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        (snapshot / "app.py").write_text("mutated during test\n")
        return LaneOutcome(lane, nodeids, 0, "success")

    with pytest.raises(CandidateVerificationError, match="changed during execution"):
        _verify(root, tmp_path, executor=mutating)
    receipts = list((tmp_path / "evidence").glob("*.json"))
    assert len(receipts) == 1
    attempt = json.loads(receipts[0].read_text())["attempts"][-1]
    assert attempt["result"] == "incomplete"
    assert attempt["diagnostics"] == ["app.py (content changed)"]


@pytest.mark.unit
def test_runtime_data_created_during_execution_is_not_source_mutation(tmp_path):
    root = _source_repo(tmp_path)

    written: list[Path] = []

    def writing_runtime_data(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        # The verifier resolves the candidate's whole ``data`` directory to
        # runtime storage before the lane runs, so a store writing to its
        # repo-root-anchored default lands outside the snapshot.
        data = snapshot / "data"
        assert data.is_symlink()
        for name in ("task_index.json", "imessage.db", "gpu_embed.lock"):
            (data / name).write_text("isolated runtime state\n")
            written.append(data.resolve() / name)
        return LaneOutcome(lane, nodeids, 0, "success")

    result = _verify(root, tmp_path, executor=writing_runtime_data)

    assert not result.reused
    assert result.outcomes[0].result == "success"
    snapshot_root = (tmp_path / "snapshot-0").resolve()
    for path in written:
        assert path.is_file()
        assert not path.is_relative_to(snapshot_root)


@pytest.mark.unit
def test_new_source_file_created_during_execution_remains_source_mutation(tmp_path):
    root = _source_repo(tmp_path)

    def writing_source(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        (snapshot / "unexpected.py").write_text("VALUE = True\n")
        return LaneOutcome(lane, nodeids, 0, "success")

    with pytest.raises(CandidateVerificationError, match="unexpected.py.*unexpected new file"):
        _verify(root, tmp_path, executor=writing_source)


@pytest.mark.unit
def test_snapshot_mutation_with_more_than_twenty_paths_retains_incomplete_receipt(tmp_path):
    root = _source_repo(tmp_path)

    def mutating(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        for index in range(21):
            (snapshot / f"changed-{index:02}.py").write_text("mutated during test\n")
        return LaneOutcome(lane, nodeids, 0, "success")

    with pytest.raises(CandidateVerificationError, match="changed during execution"):
        _verify(root, tmp_path, executor=mutating)
    attempt = json.loads(next((tmp_path / "evidence").glob("*.json")).read_text())["attempts"][-1]
    assert attempt["result"] == "incomplete"
    assert len(attempt["diagnostics"]) == 20
    assert attempt["diagnostics"][-1] == "... 2 additional snapshot mismatches omitted"


@pytest.mark.unit
def test_untracked_content_and_executable_mode_change_evidence_identity(tmp_path):
    root = _source_repo(tmp_path)
    (root / "added.py").write_text("VALUE = 'untracked one'\n")
    first = _verify(root, tmp_path)
    (root / "added.py").write_text("VALUE = 'untracked two'\n")
    second = _verify(root, tmp_path)
    assert not second.reused
    (root / "added.py").chmod(0o755)
    third = _verify(root, tmp_path)
    assert not third.reused
    assert len({first.evidence_key, second.evidence_key, third.evidence_key}) == 3


@pytest.mark.unit
def test_base_dependency_config_and_safe_environment_change_invalidate(tmp_path):
    root = _source_repo(tmp_path)
    first = _verify(root, tmp_path, base="base-a", environment={"LIFEOS_TEST_PARALLEL_WORKERS": "2"})
    assert not _verify(root, tmp_path, base="base-b", environment={"LIFEOS_TEST_PARALLEL_WORKERS": "2"}).reused
    (root / "requirements.txt").write_text("synthetic-dependency==2\n")
    assert not _verify(root, tmp_path, base="base-a", environment={"LIFEOS_TEST_PARALLEL_WORKERS": "2"}).reused
    (root / "requirements.txt").write_text("synthetic-dependency==1\n")
    assert not _verify(root, tmp_path, base="base-a", environment={"LIFEOS_TEST_PARALLEL_WORKERS": "1"}).reused
    assert first.candidate_id
    with pytest.raises(Exception):
        safe_environment_fingerprint({"SECRET_TOKEN": "never-hash"})


@pytest.mark.unit
def test_changed_runner_script_invalidates_reuse(tmp_path):
    """A runner-affecting file (not just app content) is part of execution
    identity: editing it must invalidate reuse just like changed content."""
    root = _source_repo(tmp_path)
    first = _verify(root, tmp_path, base=None)
    (root / "scripts" / "test.sh").write_text("#!/bin/bash\necho changed runner\n")
    second = _verify(root, tmp_path, base=None)
    assert not second.reused
    assert first.evidence_key != second.evidence_key


@pytest.mark.unit
def test_reuse_never_invokes_the_lane_executor_a_second_time(tmp_path):
    """Mechanism-level proof: once a candidate is proven at a given base, a
    later request under the same base/content/runner/dependency/environment
    never calls the lane executor again -- counted directly, not inferred
    from timing."""
    root = _source_repo(tmp_path)
    (root / "app.py").write_text("VALUE = 'reused end to end'\n")
    calls: list[str] = []

    def counting_executor(snapshot, lane, nodeids):
        calls.append(lane)
        return _executor(snapshot, lane, nodeids)

    precommit = _verify(root, tmp_path, executor=counting_executor, base="main-tip")
    assert not precommit.reused
    assert calls  # the precommit capture really executed at least one lane

    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "commit exact tested tree")

    calls.clear()
    pushed = verify_candidate(
        root, tmp_path / "snapshot-pushed", tmp_path / "evidence", counting_executor,
        base_identity="main-tip", merge_identity="pushed-sha-differs-from-none", environment={},
        hermetic_environment=True,
    )
    assert pushed.reused
    assert pushed.candidate_id == precommit.candidate_id
    assert calls == []  # zero repeated lane commands


@pytest.mark.unit
def test_local_cli_commit_and_pushed_ref_cli_reuse_matching_real_hook_semantics(tmp_path):
    """The real ``local`` CLI (as ``test.sh`` invokes it,
    inferring its base from the upstream tip) captures a dirty tree; once
    committed, the real ``pushed-ref`` CLI (as pre-push invokes it, with the
    base pre-push actually computes for an existing-branch push -- the
    remote's current tip, see scripts/pre-push lines 84-86) must reuse that
    capture. Both calls use identical worker/environment settings -- reuse
    must come from genuine input equality, not a loosened comparison -- and
    the reused call must still leave the exact per-lane receipt files
    pre-push's file-existence gate requires, honestly labeled as reused
    rather than a fabricated execution."""
    root = _source_repo(tmp_path)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "-u", "origin", "HEAD")

    (root / "app.py").write_text("VALUE = 'hook-matching reuse'\n")
    evidence = tmp_path / "evidence"
    local_logs = tmp_path / "local-lanes"
    local = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1", "--lane-log-dir", str(local_logs),
        ],
        text=True, capture_output=True,
    )
    assert local.returncode == 0, local.stdout + local.stderr
    assert json.loads(local.stdout.splitlines()[-1])["reused"] is False
    assert json.loads((local_logs / "fast-unit.json").read_text())["status"] == "success"

    # Nothing advanced the remote in between: an existing-branch push's real
    # base (scripts/pre-push's $remote_sha) is exactly this tracked tip.
    remote_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "@{upstream}"], text=True).strip()

    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "hook-matching reuse")
    pushed_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    pushed_logs = tmp_path / "pushed-lanes"
    pushed = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref",
            "--repository", str(root), "--sha", pushed_sha, "--base", remote_sha,
            "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1", "--lane-log-dir", str(pushed_logs),
        ],
        text=True, capture_output=True,
    )
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    payload = json.loads(pushed.stdout.splitlines()[-1])
    assert payload["reused"] is True
    # Pre-push's own gate: it requires exactly this file to exist to accept
    # the candidate (scripts/pre-push, run_pushed_candidate). Its content
    # must say "reused", never a fabricated fresh "success" execution.
    pushed_receipt = json.loads((pushed_logs / "fast-unit.json").read_text())
    assert pushed_receipt["status"] == "reused"
    assert pushed_receipt["evidence_key"]
    assert "reused" in (pushed_logs / "fast-unit.log").read_text()


@pytest.mark.unit
def test_missing_or_malformed_provenance_never_becomes_a_base_independent_cache_hit(tmp_path):
    """A receipt whose attempt is missing (or has malformed) provenance must
    never be treated as an implicit base_identity=None -- that would let a
    base-independent request slip past a base check that should apply."""
    inputs = VerificationInputs(*("d" * 64 for _ in range(5)))
    store = EvidenceStore(tmp_path / "evidence")
    outcome = LaneOutcome("fast-unit", ("tests/test_x.py::test_x",), 0, "success")
    store.record(inputs, (outcome,), result="success")
    receipt_path = tmp_path / "evidence" / f"{inputs.key}.json"
    payload = json.loads(receipt_path.read_text())
    del payload["attempts"][-1]["provenance"]
    receipt_path.write_text(json.dumps(payload))

    receipt, reason = store.reusable(inputs, {"fast-unit": outcome.nodeids})
    assert receipt is None
    assert reason == "malformed_attempt"


@pytest.mark.unit
def test_one_bad_outcome_in_a_success_attempt_blocks_reuse_of_every_lane(tmp_path):
    """Every stored outcome in a "success" attempt must itself be a clean
    pass, not just the lanes a given call happens to want -- a tampered
    receipt (top-level success, one outcome disagreeing) must never become
    a cache hit for any lane, including ones that look fine in isolation."""
    inputs = VerificationInputs(*("e" * 64 for _ in range(5)))
    store = EvidenceStore(tmp_path / "evidence")
    fine = LaneOutcome("fast-unit", ("tests/test_x.py::test_x",), 0, "success")
    store.record(inputs, (fine,), result="success")
    receipt_path = tmp_path / "evidence" / f"{inputs.key}.json"
    payload = json.loads(receipt_path.read_text())
    payload["attempts"][-1]["outcomes"].append(
        {"lane": "integration", "nodeids": ["tests/test_y.py::test_y"], "exit_status": 1, "result": "failure"}
    )
    receipt_path.write_text(json.dumps(payload))

    receipt, reason = store.reusable(inputs, {"fast-unit": fine.nodeids})
    assert receipt is None
    assert reason == "malformed_attempt"


@pytest.mark.unit
def test_local_capture_on_base_a_does_not_authorize_pushed_ref_once_base_moves_to_b(tmp_path):
    """Identical tested content captured against base A must
    never authorize a pushed-ref check once the real target has moved to
    base B -- missing/changed provenance fails closed rather than silently
    following the move."""
    root = _source_repo(tmp_path)
    (root / "app.py").write_text("VALUE = 'identical candidate content'\n")
    local_capture = _verify(root, tmp_path, base="main-at-a")
    assert not local_capture.reused

    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "identical candidate content")
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    stale = verify_git_ref(
        root, sha, tmp_path / "evidence", base_identity="main-at-b",
        required_lanes=("fast-unit",), workers=1,
        capacity=CapacityManager.for_test(tmp_path / "capacity", total_workers=1),
    )
    assert not stale.reused
    assert stale.candidate_id == local_capture.candidate_id  # same content, correctly rejected on base alone


@pytest.mark.unit
def test_full_lane_success_serves_a_requested_lane_subset_without_re_execution(tmp_path):
    """A success covering every lane can serve a later request for a
    strict subset of those lanes without re-running anything -- but the
    subset is only ever whole lanes, never opportunistically promoted from
    the wrong lane."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.unit\ndef test_synthetic(): assert True\n\n"
        "@pytest.mark.integration\ndef test_integration(): assert True\n"
    )
    first_logs = tmp_path / "first-logs"
    first = verify_pytest_candidate(
        root, tmp_path / "snapshot-one", tmp_path / "evidence",
        required_lanes=("fast-unit", "integration"), workers=1, lane_log_dir=first_logs,
    )
    assert not first.reused
    assert {path.stem for path in first_logs.glob("*.log")} == {"fast-unit", "integration"}

    second_logs = tmp_path / "second-logs"
    second = verify_pytest_candidate(
        root, tmp_path / "snapshot-two", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1, lane_log_dir=second_logs,
    )
    assert second.reused
    assert [outcome.lane for outcome in second.outcomes] == ["fast-unit"]
    # A consumer's file-existence gate (pre-push) must still see exactly the
    # requested lane's receipt -- honestly labeled reused, never "integration"
    # (never requested this time) and never a fabricated fresh execution.
    assert {path.stem for path in second_logs.glob("*.json")} == {"fast-unit"}
    assert json.loads((second_logs / "fast-unit.json").read_text())["status"] == "reused"


@pytest.mark.unit
def test_explicit_partial_nodeid_selection_is_not_served_by_a_broader_recorded_success(tmp_path):
    """Exact node-ID inventory per requested lane must be preserved --
    a narrower explicit --nodeids selection is never opportunistically
    satisfied by a broader lane success that happens to include those IDs."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.unit\ndef test_one(): assert True\n"
        "@pytest.mark.unit\ndef test_two(): assert True\n"
    )
    evidence = tmp_path / "evidence"
    full = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert full.returncode == 0, full.stdout + full.stderr
    assert json.loads(full.stdout.splitlines()[-1])["reused"] is False

    partial = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--nodeids", "tests/test_synthetic.py::test_one",
            "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert partial.returncode == 0, partial.stdout + partial.stderr
    assert json.loads(partial.stdout.splitlines()[-1])["reused"] is False


@pytest.mark.unit
def test_local_cli_infers_upstream_base_and_pushed_ref_reuses_it(tmp_path):
    """The ``local`` CLI defaults to a real inferred base (this
    checkout's upstream tip) rather than leaving it unattributed, so a
    pushed-ref check naming that same tip can actually reuse the capture --
    and an explicit ``--base`` still overrides the inferred one."""
    root = _source_repo(tmp_path)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "-u", "origin", "HEAD")
    upstream_tip = subprocess.check_output(["git", "-C", str(root), "rev-parse", "@{upstream}"], text=True).strip()

    (root / "app.py").write_text("VALUE = 'inferred base end to end'\n")
    evidence = tmp_path / "evidence"
    local = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert local.returncode == 0, local.stdout + local.stderr
    assert json.loads(local.stdout.splitlines()[-1])["reused"] is False

    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "inferred base end to end")
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    pushed = verify_git_ref(
        root, sha, evidence, base_identity=upstream_tip,
        required_lanes=("fast-unit",), workers=1,
        capacity=CapacityManager.for_test(tmp_path / "capacity", total_workers=1),
    )
    assert pushed.reused


@pytest.mark.unit
def test_local_cli_infers_merge_base_on_a_fresh_branch_first_push(tmp_path):
    """The common pre-commit -> first-push path is a feature
    branch with no upstream tracking yet. ``local`` must fall back to the
    resolved merge-base with origin/main (matching scripts/pre-push's own
    new-branch ``MERGE_BASE`` provenance, lines 71-83) rather than leaving
    the capture unattributed, so a first-push ``pushed-ref`` check naming
    that same merge-base actually reuses it."""
    root = _source_repo(tmp_path)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "origin", "HEAD:main")  # no -u: no upstream tracking configured
    _git(root, "checkout", "-qb", "feature")
    assert subprocess.run(
        ["git", "-C", str(root), "rev-parse", "@{upstream}"], capture_output=True,
    ).returncode != 0  # sanity: this branch genuinely has no upstream yet

    (root / "app.py").write_text("VALUE = 'fresh branch first push'\n")
    evidence = tmp_path / "evidence"
    local_logs = tmp_path / "local-lanes"
    local = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1", "--lane-log-dir", str(local_logs),
        ],
        text=True, capture_output=True,
    )
    assert local.returncode == 0, local.stdout + local.stderr
    assert json.loads(local.stdout.splitlines()[-1])["reused"] is False

    # What pre-push itself would compute for this exact new-branch first push.
    merge_base = subprocess.check_output(
        ["git", "-C", str(root), "merge-base", "HEAD", "origin/main"], text=True,
    ).strip()

    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "fresh branch first push")
    pushed_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    pushed_logs = tmp_path / "pushed-lanes"
    pushed = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref",
            "--repository", str(root), "--sha", pushed_sha, "--base", merge_base,
            "--evidence-root", str(evidence),
            "--lanes", "fast-unit", "--workers", "1", "--lane-log-dir", str(pushed_logs),
        ],
        text=True, capture_output=True,
    )
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    assert json.loads(pushed.stdout.splitlines()[-1])["reused"] is True
    assert json.loads((pushed_logs / "fast-unit.json").read_text())["status"] == "reused"


@pytest.mark.unit
def test_failed_attempt_is_retained_and_infrastructure_retry_is_reason_recorded(tmp_path):
    inputs = VerificationInputs(*( "a" * 64 for _ in range(5)))
    store = EvidenceStore(tmp_path / "evidence")
    failed = LaneOutcome("fast-unit", ("tests/test_x.py::test_x",), 1, "infrastructure_failure")
    store.record(inputs, (failed,), result="infrastructure_failure")
    assert store.reusable(inputs, {"fast-unit": failed.nodeids})[1] == "prior_infrastructure_failure"
    passed = LaneOutcome("fast-unit", failed.nodeids, 0, "success")
    store.record(inputs, (passed,), result="success", retry_reason="synthetic runner repaired")
    receipt, reason = store.reusable(inputs, {"fast-unit": passed.nodeids})
    assert reason == "reused"
    assert [attempt["result"] for attempt in json.loads((tmp_path / "evidence" / f"{inputs.key}.json").read_text())["attempts"]] == ["infrastructure_failure", "success"]
    assert receipt is not None


@pytest.mark.unit
def test_evidence_repeated_sequential_round_trips_remain_complete(tmp_path):
    """Replace publication preserves a complete receipt on each round trip."""
    inputs = VerificationInputs(*( "b" * 64 for _ in range(5)))
    store = EvidenceStore(tmp_path / "evidence")
    outcome = LaneOutcome("fast-unit", ("tests/test_x.py::test_x",), 0, "success")
    for _ in range(8):
        store.record(inputs, (outcome,), result="success")
        receipt, reason = store.reusable(inputs, {"fast-unit": outcome.nodeids})
        assert receipt is not None and reason == "reused"


@pytest.mark.unit
def test_evidence_rejects_symlink_root_and_nonprivate_receipt(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "evidence-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(Exception):
        EvidenceStore(link)
    inputs = VerificationInputs(*( "c" * 64 for _ in range(5)))
    store = EvidenceStore(tmp_path / "evidence")
    outcome = LaneOutcome("fast-unit", ("tests/test_x.py::test_x",), 0, "success")
    store.record(inputs, (outcome,), result="success")
    (tmp_path / "evidence" / f"{inputs.key}.json").chmod(0o644)
    with pytest.raises(Exception):
        store.reusable(inputs, {"fast-unit": outcome.nodeids})


@pytest.mark.unit
def test_outer_verifier_records_queue_execution_and_cache_with_one_capacity_lease(tmp_path):
    root = _source_repo(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = MetricsRecorder(metrics_path, candidate_id="synthetic-candidate")
    capacity = CapacityManager.for_test(tmp_path / "capacity", total_workers=2)
    first = verify_candidate(
        root, tmp_path / "snapshot-first", tmp_path / "evidence", _executor,
        capacity=capacity, workers=1, metrics=metrics,
        hermetic_environment=True,
    )
    assert not first.reused
    second = verify_candidate(
        root, tmp_path / "snapshot-second", tmp_path / "evidence", _executor,
        capacity=capacity, workers=1, metrics=metrics,
        hermetic_environment=True,
    )
    assert second.reused
    assert [record.phase for record in read_records(metrics_path)] == [
        "verification-queue", "verification-execution", "verification-cache",
    ]


@pytest.mark.unit
def test_failed_execution_records_a_distinguishable_metric_not_silence(tmp_path):
    """A failed execution records a failure metric."""
    root = _source_repo(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = MetricsRecorder(metrics_path, candidate_id="synthetic-candidate")
    capacity = CapacityManager.for_test(tmp_path / "capacity", total_workers=2)

    def failing(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        return LaneOutcome(lane, nodeids, 1, "failure")

    result = verify_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence", failing,
        metrics=metrics, workers=2, capacity=capacity, hermetic_environment=True,
    )
    assert result.reason == "executed_failure"
    records = read_records(metrics_path)
    # A real (test-isolated) capacity lease is acquired first, exactly as in
    # the success path — only the execution record is new coverage here.
    assert [r.phase for r in records] == ["verification-queue", "verification-execution"]
    failed = records[-1]
    assert failed.result == "failure"
    assert failed.exit_status == 1
    assert failed.worker_count == 2
    assert failed.suite  # non-empty lane scope, never a raw path/env dump
    assert failed.candidate_id == metrics.candidate_id


@pytest.mark.unit
def test_cancellation_metric_never_replaces_or_swallows_original_exception(tmp_path):
    """A signal/BaseException path records cancellation without replacing
    or swallowing the original exception."""
    root = _source_repo(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    metrics = MetricsRecorder(metrics_path, candidate_id="synthetic-candidate")
    capacity = CapacityManager.for_test(tmp_path / "capacity", total_workers=2)

    def interrupted(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        verify_candidate(
            root, tmp_path / "snapshot", tmp_path / "evidence", interrupted,
            metrics=metrics, capacity=capacity, hermetic_environment=True,
        )
    records = read_records(metrics_path)
    assert [r.phase for r in records] == ["verification-queue", "verification-execution"]
    assert records[-1].result == "interrupted"
    assert records[-1].exit_status is None

    # A distinct, non-signal crash inside the lane executor is a different,
    # equally distinguishable category — never mislabeled as "interrupted".
    def crashing(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        raise RuntimeError("synthetic infrastructure crash")

    with pytest.raises(RuntimeError, match="synthetic infrastructure crash"):
        verify_candidate(
            root, tmp_path / "snapshot-2", tmp_path / "evidence", crashing,
            metrics=metrics, capacity=capacity, hermetic_environment=True,
            retry_reason="synthetic retry after the interrupted attempt above",
        )
    second_records = read_records(metrics_path)[2:]
    assert [r.phase for r in second_records] == ["verification-queue", "verification-execution"]
    assert second_records[-1].result == "infrastructure_failure"


@pytest.mark.unit
def test_metrics_sink_failure_never_changes_verification_outcome(tmp_path, monkeypatch):
    """A broken/unwritable metrics receipt must never turn a real pass into a
    failure, a real failure into a pass, or raise out of verify_candidate."""
    root = _source_repo(tmp_path)
    metrics = MetricsRecorder(tmp_path / "metrics.jsonl", candidate_id="synthetic-candidate")
    capacity = CapacityManager.for_test(tmp_path / "capacity", total_workers=2)

    def always_broken(record) -> None:
        raise OSError("synthetic metrics sink failure")

    monkeypatch.setattr(metrics, "append", always_broken)

    passing = verify_candidate(
        root, tmp_path / "snapshot-pass", tmp_path / "evidence", _executor,
        metrics=metrics, capacity=capacity, hermetic_environment=True,
    )
    assert passing.reason != "executed_failure"
    assert not (tmp_path / "metrics.jsonl").exists()  # append() never actually wrote

    # A genuinely different candidate (distinct source content) so the
    # second call cannot short-circuit through the reuse cache and skip
    # actually invoking the executor — the point is a real, fresh failure.
    (root / "app.py").write_text("VALUE = 'mutated for the sink-failure test'\n")

    def failing(snapshot: Path, lane: str, nodeids: tuple[str, ...]) -> LaneOutcome:
        return LaneOutcome(lane, nodeids, 1, "failure")

    failing_result = verify_candidate(
        root, tmp_path / "snapshot-fail", tmp_path / "evidence", failing,
        metrics=metrics, capacity=capacity, hermetic_environment=True,
    )
    assert failing_result.reason == "executed_failure"
    assert not (tmp_path / "metrics.jsonl").exists()


@pytest.mark.unit
def test_durable_owner_capacity_wait_is_recorded_not_lost(tmp_path):
    """The externally-held capacity path (verify_git_ref / CLI `local`)
    records its measured wait after the candidate identity is available."""
    root = _source_repo(tmp_path)
    result = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "evidence"),
            "--lanes", "fast-unit", "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    records = read_records(tmp_path / "evidence" / "metrics.jsonl")
    phases = [r.phase for r in records]
    assert "verification-queue" in phases
    queue_record = next(r for r in records if r.phase == "verification-queue")
    assert queue_record.result == "success"
    assert queue_record.elapsed_seconds is not None and queue_record.elapsed_seconds >= 0
    assert queue_record.suite == "fast-unit"
    assert queue_record.worker_count == 1


@pytest.mark.unit
def test_concurrent_worktrees_remain_attributable_by_candidate_id(tmp_path):
    """Two real, concurrently-running `local` CLI candidates sharing one
    metrics receipt must never blend their records under a shared identity —
    each record group's candidate_id must trace back to its own worktree."""
    root_a = _source_repo(tmp_path / "repo-a")
    (root_a / "app.py").write_text("VALUE = 'candidate-a'\n")
    _git(root_a, "add", "app.py")
    _git(root_a, "commit", "-qm", "candidate a content")
    root_b = _source_repo(tmp_path / "repo-b")
    (root_b / "app.py").write_text("VALUE = 'candidate-b'\n")
    _git(root_b, "add", "app.py")
    _git(root_b, "commit", "-qm", "candidate b content")

    evidence_root = tmp_path / "shared-evidence"

    def _spawn(root: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
                "--source", str(root), "--evidence-root", str(evidence_root),
                "--lanes", "fast-unit", "--workers", "1",
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    proc_a = _spawn(root_a)
    proc_b = _spawn(root_b)  # started while A may still be acquiring capacity
    out_a, err_a = proc_a.communicate(timeout=120)
    out_b, err_b = proc_b.communicate(timeout=120)
    assert proc_a.returncode == 0, err_a
    assert proc_b.returncode == 0, err_b

    candidate_a = json.loads(out_a.splitlines()[-1])["candidate_id"]
    candidate_b = json.loads(out_b.splitlines()[-1])["candidate_id"]
    assert candidate_a != candidate_b

    records = read_records(evidence_root / "metrics.jsonl")
    ids_seen = {record.candidate_id for record in records}
    assert ids_seen == {candidate_a, candidate_b}
    for candidate_id in (candidate_a, candidate_b):
        own_phases = {r.phase for r in records if r.candidate_id == candidate_id}
        assert "verification-queue" in own_phases
        assert "verification-execution" in own_phases


@pytest.mark.unit
def test_real_pytest_lane_adapter_executes_snapshot_not_callback(tmp_path):
    """The integration adapter runs pytest by collected node ID in the snapshot."""
    root = _source_repo(tmp_path)
    result = verify_pytest_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1,
    )
    assert not result.reused
    assert result.outcomes[0].lane == "fast-unit"
    assert result.outcomes[0].exit_status == 0


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["", "   ", " leading", "trailing ", "line\nbreak", "x" * 161])
def test_pytest_adapter_rejects_malformed_retry_reason(tmp_path, reason):
    root = _source_repo(tmp_path)
    with pytest.raises(CandidateVerificationError, match="invalid retry reason"):
        verify_pytest_candidate(
            root, tmp_path / "snapshot", tmp_path / "evidence",
            required_lanes=("fast-unit",), workers=1, retry_reason=reason,
        )


@pytest.mark.unit
def test_supplied_retry_reason_is_not_recorded_for_first_attempt(tmp_path):
    root = _source_repo(tmp_path)
    verify_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence", _executor,
        capacity=CapacityManager.for_test(tmp_path / "capacity", total_workers=1),
        hermetic_environment=True, retry_reason="available only if this is a retry",
    )
    receipt = json.loads(next((tmp_path / "evidence").glob("*.json")).read_text())
    assert receipt["attempts"][-1]["retry_reason"] is None


@pytest.mark.unit
def test_real_adapter_writes_lane_log_and_receipt_without_nodeids_on_argv(tmp_path):
    """One supervised lane process receives an external exact-ID file."""
    root = _source_repo(tmp_path)
    lane_logs = tmp_path / "lane-logs"
    result = verify_pytest_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1, lane_log_dir=lane_logs,
    )
    assert result.outcomes[0].result == "success"
    assert "1 passed" in (lane_logs / "fast-unit.log").read_text()
    receipt = json.loads((lane_logs / "fast-unit.json").read_text())
    assert receipt["reports"] == {"tests/test_synthetic.py::test_synthetic": "passed"}


@pytest.mark.unit
def test_real_pytest_failure_is_retained_not_a_success_receipt(tmp_path):
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_synthetic(): assert False\n"
    )
    result = verify_pytest_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1,
    )
    assert result.reason == "executed_failure"
    assert result.outcomes[0].result == "failure"
    receipt = json.loads(next((tmp_path / "evidence").glob("*.json")).read_text())
    assert receipt["attempts"][-1]["result"] == "failure"


@pytest.mark.unit
def test_local_cli_preserves_dispatched_lane_failure_status(tmp_path):
    """The concrete runner, not a callback, returns a failed candidate body."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_synthetic(): assert False\n"
    )
    result = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "evidence"),
            "--lanes", "fast-unit", "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["result"] == "failure"
    assert payload["reason"] == "executed_failure"
    evidence = json.loads(
        (tmp_path / "evidence" / f"{payload['evidence_key']}.json").read_text()
    )
    assert evidence["key"] == payload["evidence_key"]
    assert evidence["attempts"][-1]["result"] == "failure"


@pytest.mark.unit
def test_local_cli_executes_only_requested_nodeids_across_lanes(tmp_path):
    """Exact CLI selection excludes unrelated failures and retains exact receipts."""
    root = _source_repo(tmp_path)
    test_file = root / "tests" / "test_synthetic.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.unit\ndef test_fast_selected(): assert True\n"
        "@pytest.mark.unit\ndef test_fast_unrelated(): assert False\n"
        "@pytest.mark.integration\ndef test_integration_selected(): assert True\n"
        "@pytest.mark.integration\ndef test_integration_unrelated(): assert False\n"
    )
    selected = (
        "tests/test_synthetic.py::test_fast_selected",
        "tests/test_synthetic.py::test_integration_selected",
    )
    lane_logs = tmp_path / "lane-logs"
    result = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "evidence"),
            "--lanes", "fast-unit,integration", "--nodeids", ",".join(selected),
            "--workers", "1", "--lane-log-dir", str(lane_logs),
        ],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["lanes"] == ["fast-unit", "integration"]
    assert json.loads((lane_logs / "fast-unit.json").read_text())["reports"] == {
        selected[0]: "passed"
    }
    assert json.loads((lane_logs / "integration.json").read_text())["reports"] == {
        selected[1]: "passed"
    }


@pytest.mark.unit
def test_local_cli_rejects_partly_missing_or_wrong_lane_exact_selection(tmp_path):
    """Exact requests cannot silently narrow to an available subset."""
    root = _source_repo(tmp_path)
    selected = "tests/test_synthetic.py::test_synthetic"
    missing = "tests/test_synthetic.py::test_missing"
    result = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "evidence"),
            "--lanes", "fast-unit", "--nodeids", f"{selected},{missing}",
            "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "requested node ID is absent from the selected lanes or paths" in result.stderr

    wrong_lane = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "wrong-lane-evidence"),
            "--lanes", "integration", "--nodeids", selected, "--workers", "1",
        ],
        text=True, capture_output=True,
    )
    assert wrong_lane.returncode == 1
    assert "requested node ID is absent from the selected lanes or paths" in wrong_lane.stderr


@pytest.mark.unit
def test_local_cli_rejects_explicit_empty_exact_filters_without_dispatch(tmp_path):
    """Empty CLI filters cannot broaden into an otherwise failing test body."""
    root = _source_repo(tmp_path)
    sentinel = tmp_path / "test-body-ran"
    (root / "tests" / "test_synthetic.py").write_text(
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.mark.unit\ndef test_unrelated():\n"
        f"    Path({str(sentinel)!r}).write_text('unexpected execution')\n"
    )
    for option, value in (("--nodeids", ""), ("--nodeids", ",,"), ("--paths", ""), ("--paths", ",,")):
        evidence = tmp_path / f"evidence-{option[2:]}-{len(value)}"
        result = subprocess.run(
            [
                sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
                "--source", str(root), "--evidence-root", str(evidence),
                "--lanes", "fast-unit", option, value, "--workers", "1",
            ],
            text=True, capture_output=True,
        )
        assert result.returncode == 1
        assert f"{option} must contain one or more nonempty comma-separated values" in result.stderr
        assert not sentinel.exists()
        assert not evidence.exists()


@pytest.mark.unit
def test_local_cli_reconciles_plugin_deselection_with_exact_execution(tmp_path):
    """The real CLI log reports its two excluded IDs as deselected, not selected."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.unit\ndef test_selected(): assert True\n"
        "@pytest.mark.unit\ndef test_unrelated_one(): assert False\n"
        "@pytest.mark.unit\ndef test_unrelated_two(): assert False\n"
    )
    selected = "tests/test_synthetic.py::test_selected"
    lane_logs = tmp_path / "lane-logs"
    result = subprocess.run(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
            "--source", str(root), "--evidence-root", str(tmp_path / "evidence"),
            "--lanes", "fast-unit", "--nodeids", selected, "--workers", "1",
            "--lane-log-dir", str(lane_logs),
        ],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = (lane_logs / "fast-unit.log").read_text()
    assert "1 passed, 2 deselected" in log
    receipt = json.loads((lane_logs / "fast-unit.json").read_text())
    assert receipt["collected_count"] == len(receipt["reports"]) == 1
    assert receipt["reports"] == {selected: "passed"}


@pytest.mark.unit
def test_real_all_skipped_lane_cannot_become_success_evidence(tmp_path):
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_synthetic(): pytest.skip('synthetic skip')\n"
    )
    result = verify_pytest_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1,
    )
    assert result.reason == "executed_failure"
    assert result.outcomes[0].result == "failure"


@pytest.mark.unit
def test_real_mixed_setup_skip_and_pass_lane_cannot_become_success_evidence(tmp_path):
    """A complete setup-skip receipt remains partial coverage, not success."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest\n\n"
        "@pytest.fixture\ndef skip_in_setup():\n"
        "    pytest.skip('synthetic setup skip')\n\n"
        "@pytest.mark.unit\ndef test_setup_skip(skip_in_setup): pass\n\n"
        "@pytest.mark.unit\ndef test_passes(): assert True\n"
    )
    lane_logs = tmp_path / "lane-logs"
    result = verify_pytest_candidate(
        root, tmp_path / "snapshot", tmp_path / "evidence",
        required_lanes=("fast-unit",), workers=1, lane_log_dir=lane_logs,
    )
    assert result.reason == "executed_failure"
    assert result.outcomes[0].result == "failure"
    assert json.loads((lane_logs / "fast-unit.json").read_text())["reports"] == {
        "tests/test_synthetic.py::test_passes": "passed",
        "tests/test_synthetic.py::test_setup_skip": "skipped",
    }


@pytest.mark.unit
def test_pytest_lane_adapter_fails_closed_for_missing_malformed_and_bad_reports(monkeypatch, tmp_path):
    """Only complete all-passed receipts can be accepted."""
    import scripts.verify_candidate as verifier

    for receipt_body, expected_result in (
        (None, "incomplete"),
        ("not json", "incomplete"),
        ('{"status": "success", "reports": {}}', "failure"),
        ('{"status": "success", "reports": {"tests/test_synthetic.py::test_ok": "skipped"}}', "failure"),
        ('{"status": "success", "reports": {"tests/test_synthetic.py::test_ok": "failed"}}', "failure"),
        ('{"status": "success", "reports": {"tests/test_synthetic.py::test_ok": "error"}}', "failure"),
    ):
        def fake_run(command, **_kwargs):
            receipt = Path(command[command.index("--lifeos-lane-execution") + 1])
            if receipt_body is not None:
                receipt.write_text(receipt_body)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(verifier, "run_supervised_command", fake_run)
        environment = make_hermetic_environment(tmp_path / expected_result, workers=1)
        execute = pytest_lane_executor(environment, workers=1)
        outcome = execute(
            tmp_path,
            "fast-unit",
            ("tests/test_synthetic.py::test_ok",),
        )
        assert outcome.result == expected_result
        assert outcome.result != "success"


@pytest.mark.unit
def test_pytest_lane_adapter_allows_only_the_named_privacy_audit_not_applicable(monkeypatch, tmp_path):
    import scripts.verify_candidate as verifier

    mandatory = "tests/test_synthetic.py::test_mandatory"

    def execute(receipt_body):
        def fake_run(command, **_kwargs):
            receipt = Path(command[command.index("--lifeos-lane-execution") + 1])
            receipt.write_text(json.dumps(receipt_body))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(verifier, "run_supervised_command", fake_run)
        environment = make_hermetic_environment(tmp_path / str(len(str(receipt_body))), workers=1)
        return pytest_lane_executor(environment, workers=1)(
            tmp_path, "fast-unit", (PRIVACY_AUDIT_NODEID, mandatory),
        )

    accepted = execute({
        "status": "success",
        "reports": {PRIVACY_AUDIT_NODEID: "skipped", mandatory: "passed"},
        "not_applicable_candidates": {PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON},
    })
    assert accepted.result == "success"
    assert accepted.not_applicable == ((PRIVACY_AUDIT_NODEID, PRIVACY_AUDIT_NOT_APPLICABLE_REASON),)

    for receipt_body in (
        {"status": "success", "reports": {PRIVACY_AUDIT_NODEID: "skipped", mandatory: "passed"}},
        {"status": "success", "reports": {PRIVACY_AUDIT_NODEID: "skipped", mandatory: "passed"}, "not_applicable_candidates": {PRIVACY_AUDIT_NODEID: "wrong reason"}},
        {"status": "success", "reports": {PRIVACY_AUDIT_NODEID: "failed", mandatory: "passed"}, "not_applicable_candidates": {PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON}},
        {"status": "success", "reports": {mandatory: "passed"}, "not_applicable_candidates": {PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON}},
        {"status": "success", "reports": {PRIVACY_AUDIT_NODEID: "skipped", mandatory: "skipped"}, "not_applicable_candidates": {PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON}},
    ):
        assert execute(receipt_body).result == "failure"


@pytest.mark.unit
def test_pushed_ref_protocol_executes_detached_candidate_not_dirty_cwd(tmp_path):
    root = _source_repo(tmp_path)
    test_file = root / "tests" / "test_synthetic.py"
    test_file.write_text("import pytest\nfrom app import VALUE\n\n@pytest.mark.unit\ndef test_synthetic(): assert VALUE == 'candidate'\n")
    (root / "app.py").write_text("VALUE = 'candidate'\n")
    _git(root, "add", "app.py", "tests/test_synthetic.py")
    _git(root, "commit", "-qm", "candidate one")
    first_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    (root / "app.py").write_text("VALUE = 'wrong cwd'\n")
    cli = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref", "--repository", str(root), "--sha", first_sha, "--base", "remote-base", "--evidence-root", str(tmp_path / "evidence"), "--lanes", "fast-unit", "--workers", "1"],
        capture_output=True, text=True,
    )
    assert cli.returncode == 0, cli.stderr
    # stdout is the authoritative top-level receipt; parsing the complete
    # document rejects a stale mixed pytest-transcript-plus-JSON result.
    first_payload = json.loads(cli.stdout)
    assert first_payload["reused"] is False
    assert first_payload["result"] == "success"
    evidence = json.loads(
        (tmp_path / "evidence" / f"{first_payload['evidence_key']}.json").read_text()
    )
    assert evidence["key"] == first_payload["evidence_key"]
    assert evidence["attempts"][-1]["result"] == "success"
    (root / "app.py").write_text("VALUE = 'candidate'\n")
    (root / "extra.py").write_text("VALUE = 'second pushed ref'\n")
    _git(root, "add", "app.py", "extra.py")
    _git(root, "commit", "-qm", "candidate two")
    second_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    (root / "app.py").write_text("VALUE = 'wrong cwd again'\n")
    second = verify_git_ref(
        root, second_sha, tmp_path / "evidence", base_identity="remote-base",
        required_lanes=("fast-unit",), workers=1,
        capacity=CapacityManager.for_test(tmp_path / "capacity", total_workers=1),
    )
    assert not second.reused
    assert first_payload["candidate_id"] != second.candidate_id


@pytest.mark.unit
def test_pushed_ref_cli_retries_interrupted_exact_candidate_with_reason(tmp_path):
    root = _source_repo(tmp_path)
    sentinel = tmp_path / "allow-completion"
    # The lane process announces that it reached the candidate's own test
    # before it parks, so the interrupt below lands mid-execution rather
    # than at whatever point a fixed sleep happens to catch on a busy host.
    started = tmp_path / "lane-started"
    (root / "tests" / "test_synthetic.py").write_text(
        "import pathlib, pytest, time\n\n"
        "@pytest.mark.unit\n"
        f"def test_synthetic():\n"
        f"    pathlib.Path({str(started)!r}).touch()\n"
        f"    if not pathlib.Path({str(sentinel)!r}).exists(): time.sleep(20)\n"
    )
    _git(root, "add", "tests/test_synthetic.py")
    _git(root, "commit", "-qm", "interruptible candidate")
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    evidence = tmp_path / "evidence"
    command = [
        sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref",
        "--repository", str(root), "--sha", sha, "--base", "base", "--workers", "1",
        "--lanes", "fast-unit", "--evidence-root", str(evidence),
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 120
    while not started.exists() and time.monotonic() < deadline:
        assert process.poll() is None, "candidate exited before its lane began"
        time.sleep(0.05)
    assert started.exists(), "candidate never reached its own test"
    process.terminate()
    assert process.wait(timeout=10) == 128 + signal.SIGTERM
    assert json.loads(next(evidence.glob("*.json")).read_text())["attempts"][-1]["result"] == "infrastructure_failure"

    sentinel.touch()
    without_reason = subprocess.run(command, capture_output=True, text=True)
    assert without_reason.returncode == 1
    assert "infrastructure retry requires a recorded reason" in without_reason.stderr
    empty_reason = subprocess.run([*command, "--retry-reason", ""], capture_output=True, text=True)
    assert empty_reason.returncode == 1
    assert "invalid retry reason" in empty_reason.stderr

    reason = "pre-push retry after synthetic interrupted exact candidate"
    retried = subprocess.run([*command, "--retry-reason", reason], capture_output=True, text=True)
    assert retried.returncode == 0, retried.stderr
    receipt = json.loads(next(evidence.glob("*.json")).read_text())
    assert receipt["attempts"][-1]["result"] == "success"
    assert receipt["attempts"][-1]["retry_reason"] == reason


@pytest.mark.unit
def test_pushed_ref_sigterm_reaps_owned_worktree_and_supervised_test(tmp_path):
    """Normal terminal termination leaves neither a registered worktree nor temp tree."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest, time\n\n@pytest.mark.unit\ndef test_synthetic(): time.sleep(20)\n"
    )
    _git(root, "add", "tests/test_synthetic.py")
    _git(root, "commit", "-qm", "slow candidate")
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    process = subprocess.Popen(
        [
            sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref",
            "--repository", str(root), "--sha", sha, "--base", "base", "--workers", "1",
            "--lanes", "fast-unit", "--evidence-root", str(tmp_path / "evidence"),
        ],
        env={**os.environ, "TMPDIR": str(tmp_path)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    process.terminate()
    assert process.wait(timeout=10) == 128 + 15
    worktrees = subprocess.check_output(["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True)
    assert "lifeos-pushed-candidate-" not in worktrees
    assert not list(tmp_path.glob("lifeos-pushed-candidate-*"))


@pytest.mark.unit
def test_pushed_ref_sigkill_guardian_reaps_worktree_descendant_and_capacity(tmp_path):
    """The surviving owner, not verifier atexit, cleans an abruptly killed run."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest, time\n\n@pytest.mark.unit\ndef test_synthetic(): time.sleep(20)\n"
    )
    _git(root, "add", "tests/test_synthetic.py")
    _git(root, "commit", "-qm", "kill candidate")
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    process = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "pushed-ref",
         "--repository", str(root), "--sha", sha, "--base", "base", "--workers", "1",
         "--lanes", "fast-unit", "--evidence-root", str(tmp_path / "evidence")],
        env={**os.environ, "TMPDIR": str(tmp_path)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    candidate_pids: list[int] = []
    while time.monotonic() < deadline:
        parents = list(tmp_path.glob("lifeos-pushed-candidate-*"))
        candidate_pids = _pids_in_directory(parents[0]) if parents else []
        if candidate_pids:
            break
        time.sleep(0.05)
    assert candidate_pids
    # This process has the old cwd-based cleanup's only heuristic, but was
    # not launched by the verifier and therefore must never be signalled.
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], cwd=parents[0])
    try:
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        deadline = time.monotonic() + 12
        while list(tmp_path.glob("lifeos-pushed-candidate-*")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not list(tmp_path.glob("lifeos-pushed-candidate-*"))
        assert "lifeos-pushed-candidate-" not in subprocess.check_output(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True,
        )
        child_deadline = time.monotonic() + 5
        while any(psutil.pid_exists(pid) for pid in candidate_pids) and time.monotonic() < child_deadline:
            time.sleep(0.05)
        assert all(not psutil.pid_exists(pid) for pid in candidate_pids), candidate_pids
        assert unrelated.poll() is None
        # A fresh acquisition proves the guardian released only after cleanup.
        lease = CapacityManager().acquire(1)
        lease.release()
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


@pytest.mark.unit
def test_local_sigkill_guardian_removes_snapshot_and_releases_capacity(tmp_path):
    """The local candidate path has the same SIGKILL ownership boundary."""
    root = _source_repo(tmp_path)
    (root / "tests" / "test_synthetic.py").write_text(
        "import pytest, time\n\n@pytest.mark.unit\ndef test_synthetic(): time.sleep(20)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "verify_candidate.py"), "local",
         "--source", str(root), "--workers", "1", "--lanes", "fast-unit",
         "--evidence-root", str(tmp_path / "evidence")],
        env={**os.environ, "TMPDIR": str(tmp_path)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    candidate_pids: list[int] = []
    while time.monotonic() < deadline:
        parents = list(tmp_path.glob("lifeos-local-candidate-*"))
        candidate_pids = _pids_in_directory(parents[0]) if parents else []
        if candidate_pids:
            break
        time.sleep(0.05)
    assert candidate_pids
    process.kill()
    assert process.wait(timeout=5) == -signal.SIGKILL
    deadline = time.monotonic() + 12
    while list(tmp_path.glob("lifeos-local-candidate-*")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not list(tmp_path.glob("lifeos-local-candidate-*"))
    child_deadline = time.monotonic() + 5
    while any(psutil.pid_exists(pid) for pid in candidate_pids) and time.monotonic() < child_deadline:
        time.sleep(0.05)
    assert all(not psutil.pid_exists(pid) for pid in candidate_pids), candidate_pids
    lease = CapacityManager().acquire(1)
    lease.release()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "linux", reason="asserts the Linux branch of the platform-default resolver")
def test_hermetic_environment_exposes_ambient_playwright_cache_by_default(tmp_path, monkeypatch):
    """The synthetic HOME must not hide an already-installed browser cache:
    with no explicit override and no XDG_CACHE_HOME, the hermetic env should
    point at the real HOME's platform-default Playwright cache (~/.cache),
    not the throwaway runtime HOME."""
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["HOME"] == str(tmp_path / "runtime" / "home")
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(real_home / ".cache" / "ms-playwright")


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "linux", reason="XDG_CACHE_HOME only takes precedence on Linux")
def test_hermetic_environment_honors_xdg_cache_home_on_linux(tmp_path, monkeypatch):
    """Playwright's own resolver prefers XDG_CACHE_HOME over ~/.cache on
    Linux; the hermetic env must match, not always assume ~/.cache."""
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    xdg_cache = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(xdg_cache / "ms-playwright")


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "linux", reason="asserts the Linux XDG_CACHE_HOME branch")
def test_hermetic_environment_normalizes_relative_xdg_cache_home_against_init_cwd(tmp_path, monkeypatch):
    """A relative XDG_CACHE_HOME (unusual but valid) needs the same
    original-cwd normalization as an explicit relative override -- the
    platform-default branch isn't exempt from Playwright's own final
    relative-path resolution step."""
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "relative-xdg-cache")
    monkeypatch.setenv("INIT_CWD", str(tmp_path / "original-cwd"))

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(
        tmp_path / "original-cwd" / "relative-xdg-cache" / "ms-playwright"
    )


@pytest.mark.unit
def test_hermetic_environment_respects_explicit_playwright_browsers_path(tmp_path, monkeypatch):
    """An explicit absolute PLAYWRIGHT_BROWSERS_PATH from the caller must be
    honored verbatim, never overridden by the platform-default guess."""
    explicit = str(tmp_path / "custom-playwright-cache")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", explicit)

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == explicit


@pytest.mark.unit
def test_hermetic_environment_preserves_literal_zero_playwright_browsers_path(monkeypatch, tmp_path):
    """PLAYWRIGHT_BROWSERS_PATH="0" has special package-local meaning inside
    Playwright's own driver (resolved relative to its own install location,
    not HOME/cwd) -- it must pass through untouched, never be treated as a
    relative path segment."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "0"


@pytest.mark.unit
def test_hermetic_environment_resolves_relative_playwright_browsers_path_against_init_cwd(monkeypatch, tmp_path):
    """A relative override resolves against INIT_CWD (Playwright's own
    preferred source of the original invocation directory) rather than
    whatever cwd the sandboxed subprocess ends up running from."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "relative-cache")
    monkeypatch.setenv("INIT_CWD", str(tmp_path / "original-cwd"))

    environment = make_hermetic_environment(tmp_path / "runtime", workers=1)

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "original-cwd" / "relative-cache")


@pytest.mark.unit
def test_hermetic_environment_redirects_chroma_path_under_the_runtime_root(tmp_path):
    """Collection imports api.routes.agents, whose agent-viz modules
    initialize SQLite at Path(settings.chroma_path).parent; the default
    (./data/chromadb) is relative to cwd, which for lane execution is the
    immutable snapshot -- writing there fails the strict snapshot-unmodified
    check on every browser-marked collection/run. Must point outside the
    snapshot, under the private runtime root, never the snapshot itself."""
    runtime_root = tmp_path / "runtime"

    environment = make_hermetic_environment(runtime_root, workers=1)

    chroma_path = Path(environment["LIFEOS_CHROMA_PATH"])
    assert chroma_path.is_relative_to(runtime_root)
    assert not chroma_path.is_relative_to(runtime_root / "home")


@pytest.mark.unit
def test_installed_chromium_prerequisite_identity_is_stable_for_the_same_resolved_path(monkeypatch, tmp_path):
    """Against a deterministic temporary browsers path (never an assumption
    about what happens to be installed on this specific host), the real
    driver's resolution -- and therefore the identity digest -- must be
    stable across repeated calls, not incidental/random per invocation."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "stable-browsers-path"))

    first_identity, first_installed = _installed_chromium_prerequisite()
    second_identity, second_installed = _installed_chromium_prerequisite()

    assert first_identity != "unknown"
    assert first_identity == second_identity
    assert first_installed == second_installed is False


@pytest.mark.unit
def test_installed_chromium_prerequisite_reports_absent_install(monkeypatch, tmp_path):
    """Against the real driver (not a mocked resolver) pointed at an empty
    browsers path, nothing is installed -- must report installed=False, not
    silently reuse a stale identity from wherever it last resolved."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty-browsers-path"))

    identity, installed = _installed_chromium_prerequisite()

    assert installed is False
    assert identity != "unknown"


@pytest.mark.unit
def test_installed_chromium_prerequisite_rejects_nonempty_directory_without_executable(monkeypatch, tmp_path):
    """A directory that exists and is non-empty (a stray README, a partial/
    interrupted download missing the actual binary) must still report
    installed=False -- directory presence alone is not proof the exact
    required executable is there."""
    import scripts.verify_candidate as verify_candidate_module

    browsers_path = tmp_path / "partial-browsers-path"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    real_executable = verify_candidate_module._required_chromium_executable()
    real_executable.parent.mkdir(parents=True)
    (real_executable.parent / "README.txt").write_text("partial download\n")

    identity, installed = _installed_chromium_prerequisite()

    assert installed is False
    assert identity != "unknown"


@pytest.mark.unit
def test_installed_chromium_prerequisite_accepts_headless_shell_only_install(monkeypatch, tmp_path):
    """A --only-shell install (no full Chromium binary, only the headless
    shell) must be recognized as installed once the exact executable the
    real driver resolves is present and executable -- not rejected for
    lacking a `chromium-<rev>` directory that only a full install would
    have had. Uses the driver's own resolution to learn the exact path
    (never a hand-copied revision/platform table) before populating it, so
    this proves real behavior rather than a mocked stand-in."""
    import scripts.verify_candidate as verify_candidate_module

    browsers_path = tmp_path / "only-shell-browsers-path"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    real_executable = verify_candidate_module._required_chromium_executable()
    real_executable.parent.mkdir(parents=True)
    real_executable.write_text("#!/bin/sh\n")
    real_executable.chmod(0o755)

    identity, installed = _installed_chromium_prerequisite()

    assert installed is True
    assert identity != "unknown"


@pytest.mark.unit
def test_dependency_fingerprint_changes_when_chromium_install_state_changes(tmp_path, monkeypatch):
    """A stale evidence entry must not be reusable purely because the
    ``playwright`` pip package version didn't change: installing/removing
    the browser binary itself (a separate step) has to also change the
    dependency identity, or a prior failure caused by a missing browser
    could wrongly look identical to -- and get reused instead of -- a later
    run made after the browser was installed."""
    root = _source_repo(tmp_path)
    import scripts.verify_candidate as verify_candidate_module

    monkeypatch.setattr(verify_candidate_module, "_installed_chromium_prerequisite", lambda: ("1208", False))
    without_browser = _installed_dependency_fingerprint(root)

    monkeypatch.setattr(verify_candidate_module, "_installed_chromium_prerequisite", lambda: ("1208", True))
    with_browser = _installed_dependency_fingerprint(root)

    assert without_browser != with_browser


class _FakeTestInstance:
    """Minimal stand-in for TestInstance's interface used by
    pytest_lane_executor -- exercising the real per-lane command-building
    logic without paying for a real server startup."""

    class _Manifest:
        def __init__(self, data_dir: Path) -> None:
            self.data_dir = str(data_dir)

    def __init__(self, snapshot_root, *, existing_snapshot, process_started=None):
        self.manifest = self._Manifest(snapshot_root)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def command_environment(self) -> dict:
        return {}


def _lane_executor_fixture(monkeypatch, tmp_path, *, workers: int, parallel_browser_free: bool):
    """Shared harness: a real pytest_lane_executor with run_supervised_command
    and TestInstance faked out, capturing the exact constructed argv without
    paying for a real subprocess or server."""
    import scripts.verify_candidate as verify_candidate_module

    captured: dict = {}

    def fake_run_supervised_command(command, *, cwd, env, process_started=None):
        captured["command"] = command
        captured["env"] = env
        receipt = Path(command[command.index("--lifeos-lane-execution") + 1])
        receipt.write_text(json.dumps({"status": "success", "reports": {"tests/test_synthetic.py::test_ok": "passed"}}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(verify_candidate_module, "run_supervised_command", fake_run_supervised_command)
    monkeypatch.setattr(verify_candidate_module, "TestInstance", _FakeTestInstance)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir(exist_ok=True)
    execute = pytest_lane_executor(
        {
            "HOME": str(home),
            "PYTHONHASHSEED": "0",
            "LIFEOS_TEST_PARALLEL_WORKERS": str(workers),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "browser-cache"),
        },
        workers=workers, parallel_browser_free=parallel_browser_free,
    )
    return execute, snapshot_root, captured


_BROWSER_FREE_NODEIDS = ("tests/test_synthetic.py::test_ok",)


@pytest.mark.unit
def test_browser_free_stays_serial_by_default_even_with_workers_requested(monkeypatch, tmp_path):
    """An ordinary workers>1 request (parallel_browser_free defaults False)
    must never silently change browser-free's serial default."""
    execute, snapshot_root, captured = _lane_executor_fixture(monkeypatch, tmp_path, workers=2, parallel_browser_free=False)
    execute(snapshot_root, "browser-free", _BROWSER_FREE_NODEIDS)
    assert "-n" not in captured["command"]


@pytest.mark.unit
@pytest.mark.parametrize("workers", [2, 4])
def test_browser_free_parallel_opt_in_uses_exact_worker_count(monkeypatch, tmp_path, workers):
    """Explicit opt-in must produce the exact requested -n/--dist loadscope,
    at both a 2- and a 4-worker count."""
    execute, snapshot_root, captured = _lane_executor_fixture(monkeypatch, tmp_path, workers=workers, parallel_browser_free=True)
    execute(snapshot_root, "browser-free", _BROWSER_FREE_NODEIDS)
    command = captured["command"]
    assert command[command.index("-n") + 1] == str(workers)
    assert command[command.index("--dist") + 1] == "loadscope"


@pytest.mark.unit
def test_browser_server_stays_serial_even_with_parallel_opt_in(monkeypatch, tmp_path):
    """browser-server requires a shared live server -- the parallel opt-in
    only ever names browser-free, so it must have no effect here regardless
    of workers or the opt-in flag."""
    execute, snapshot_root, captured = _lane_executor_fixture(monkeypatch, tmp_path, workers=2, parallel_browser_free=True)
    execute.bind_snapshot(object())
    execute(snapshot_root, "browser-server", _BROWSER_FREE_NODEIDS)
    assert "-n" not in captured["command"]
    assert "--browser" in captured["command"]
    assert captured["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "browser-cache")
    assert captured["env"]["LIFEOS_VENV_PYTHON"] == sys.executable


@pytest.mark.unit
def test_browser_free_parallel_opt_in_fails_loudly_without_xdist(monkeypatch, tmp_path):
    """A missing xdist under explicit opt-in must raise, not silently
    collapse back to a serial run that would misreport as parallel
    evidence."""
    import scripts.verify_candidate as verify_candidate_module

    real_find_spec = verify_candidate_module.importlib.util.find_spec
    monkeypatch.setattr(
        verify_candidate_module.importlib.util, "find_spec",
        lambda name: None if name == "xdist" else real_find_spec(name),
    )
    execute, snapshot_root, _captured = _lane_executor_fixture(monkeypatch, tmp_path, workers=2, parallel_browser_free=True)
    with pytest.raises(CandidateVerificationError, match="xdist"):
        execute(snapshot_root, "browser-free", _BROWSER_FREE_NODEIDS)


@pytest.mark.unit
def test_parallel_browser_free_flag_is_part_of_environment_identity(tmp_path):
    """Same workers value, different parallel_browser_free -- must produce a
    different hermetic environment and a different safe-environment
    fingerprint, or a serial evidence entry could be wrongly reused for what
    is actually a parallel execution (or vice versa)."""
    from scripts.verification_evidence import safe_environment_fingerprint

    serial_env = make_hermetic_environment(tmp_path / "serial-runtime", workers=2, parallel_browser_free=False)
    parallel_env = make_hermetic_environment(tmp_path / "parallel-runtime", workers=2, parallel_browser_free=True)
    assert serial_env["LIFEOS_PARALLEL_BROWSER_FREE"] == "0"
    assert parallel_env["LIFEOS_PARALLEL_BROWSER_FREE"] == "1"

    serial_fingerprint = safe_environment_fingerprint({"LIFEOS_PARALLEL_BROWSER_FREE": serial_env["LIFEOS_PARALLEL_BROWSER_FREE"]})
    parallel_fingerprint = safe_environment_fingerprint({"LIFEOS_PARALLEL_BROWSER_FREE": parallel_env["LIFEOS_PARALLEL_BROWSER_FREE"]})
    assert serial_fingerprint != parallel_fingerprint
