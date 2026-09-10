"""Synthetic runtime-evidence coverage through producer and CLI paths."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import pytest

from api.services.runtime_identity import (
    IDENTITY_PROVENANCE,
    RuntimeIdentity,
    _launchd_unit_is_active,
    capture_runtime_identity,
    evaluate_runtime_evidence,
    verify_runtime_evidence,
)

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SH = REPO_ROOT / "scripts" / "server.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Synthetic Evidence")
    _git(root, "config", "user.email", "evidence@example.invalid")
    (root / "candidate.txt").write_text("initial\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    previous = _git(root, "rev-parse", "HEAD")
    (root / "candidate.txt").write_text("candidate\n")
    _git(root, "commit", "-qam", "candidate")
    return root, previous, _git(root, "rev-parse", "HEAD")


def _identity(root: Path, service: str, revision: str, *, startup: str | None = None, heartbeat: bool = True) -> RuntimeIdentity:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return RuntimeIdentity(
        service=service,
        revision=revision,
        clean=True,
        process_id=os.getpid(),
        startup_id=startup or f"startup-{service}-{revision[:8]}-{id(service)}",
        source_root=str(root.resolve()),
        started_at_utc=now,
        provenance=IDENTITY_PROVENANCE,
        last_heartbeat_at_utc=now if heartbeat else None,
        process_start_time=psutil.Process(os.getpid()).create_time(),
    )


def _write_identity(directory: Path, identity: RuntimeIdentity) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identity.service}-{identity.process_id}-{identity.startup_id}.json"
    path.write_text(json.dumps(identity.as_dict(), allow_nan=False), encoding="utf-8")
    return path


def _restart(path: Path, *, targets: tuple[str, ...], after: dict[str, RuntimeIdentity], before: dict[str, RuntimeIdentity], health: dict[str, str], result: str = "success") -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "services": list(targets),
        "result": result,
        "identities": {key: value.as_dict() for key, value in after.items()},
        "previous_identities": {key: value.as_dict() for key, value in before.items()},
        "health": {key: {"status": value, "observed": True} for key, value in health.items()},
        "source": "lifeos.runtime_identity:restart-wrapper",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8")


class _HealthHandler(BaseHTTPRequestHandler):
    payload: dict[str, object] = {}

    def do_GET(self):  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@contextmanager
def _health_server(payload: dict[str, object]):
    _HealthHandler.payload = payload
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_startup_revision_does_not_follow_checkout_changes(tmp_path):
    root, _previous, revision = _repo(tmp_path)
    identity = capture_runtime_identity("synthetic-api", source_root=root)
    (root / "candidate.txt").write_text("changed after startup\n")
    assert identity.revision == revision
    assert identity.clean is True


def test_api_health_exposes_startup_bound_identity_without_source_root():
    import asyncio
    from api.main import API_RUNTIME_IDENTITY, health_check

    payload = asyncio.run(health_check())
    assert payload["runtime_identity"]["startup_id"] == API_RUNTIME_IDENTITY.startup_id
    assert "source_root" not in payload["runtime_identity"]


def test_launchd_activity_probe_uses_label_and_pid(monkeypatch):
    class Result:
        returncode = 0
        stdout = "PID\tStatus\tLabel\n123\t0\tcom.lifeos.agent-worker\n"

    monkeypatch.setattr("api.services.runtime_identity.subprocess.run", lambda *args, **kwargs: Result())
    assert _launchd_unit_is_active("com.lifeos.agent-worker") is True
    assert _launchd_unit_is_active("com.lifeos.missing") is False


def test_evaluate_rejects_bad_types_and_unknown_service_without_raising():
    expected = "a" * 40
    bad = RuntimeIdentity(
        service="lifeos-api", revision=expected, clean=True, process_id=0,
        startup_id="bad", source_root="/synthetic", started_at_utc="not-a-time",
        process_start_time=float("nan"),
    )
    evidence = evaluate_runtime_evidence(
        expected_revision=expected, target_services=("lifeos-api", "made-up"),
        restart_result="success", observations={"lifeos-api": bad}, health_ok=True,
        revert_ref="b" * 40,
    )
    assert not evidence.accepted
    assert "lifeos-api_process_identity_missing" in evidence.reasons
    assert "made-up_service_unknown" in evidence.reasons


def test_valid_api_evidence_uses_local_producer_and_cli(tmp_path):
    root, previous_revision, expected = _repo(tmp_path)
    (root / "scripts").mkdir()
    (root / "logs").mkdir()
    wrapper = root / "scripts" / "server.sh"
    wrapper.write_text(SERVER_SH.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    identity_dir = tmp_path / "identities"
    api = _identity(root, "lifeos-api", expected, startup="api-after")
    previous = _identity(root, "lifeos-api", previous_revision, startup="api-before")
    api_path = _write_identity(identity_dir, api)
    previous_path = _write_identity(identity_dir, previous)
    restart_path = tmp_path / "restart.json"
    subprocess.run([
        sys.executable, "-m", "api.services.runtime_identity", "record-restart",
        "--service", "lifeos-api", "--result", "success", "--identity", str(api_path),
        "--previous-identity", str(previous_path), "--health-status", "healthy",
        "--output", str(restart_path),
    ], check=True, cwd=REPO_ROOT)
    with _health_server({"status": "healthy", "runtime_identity": api.as_public_dict()}) as url:
        result = subprocess.run([
            str(wrapper), "verify-runtime-evidence", "--restart-evidence", str(restart_path),
            "--identity-dir", str(identity_dir), "--api-url", url,
        ], cwd=root, env={
            **os.environ, "LIFEOS_VENV_PYTHON": sys.executable,
            "PYTHONPATH": str(REPO_ROOT),
        }, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["accepted"] is True
    assert "source_root" not in output["observed"]["lifeos-api"]


def test_server_wrapper_rejects_bound_flag_overrides(tmp_path):
    root, _previous_revision, expected = _repo(tmp_path)
    (root / "scripts").mkdir()
    (root / "logs").mkdir()
    wrapper = root / "scripts" / "server.sh"
    wrapper.write_text(SERVER_SH.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    common = [str(wrapper), "verify-runtime-evidence", "--restart-evidence", str(tmp_path / "missing.json")]
    env = {**os.environ, "LIFEOS_VENV_PYTHON": sys.executable, "PYTHONPATH": str(REPO_ROOT)}
    for duplicate in (
        ["--source-root", str(tmp_path / "other")],
        ["--expected-revision", expected],
    ):
        result = subprocess.run(common + duplicate, cwd=root, env=env, capture_output=True, text=True)
        assert result.returncode == 2
        assert "wrapper-bound" in result.stdout


def test_same_revision_restart_is_valid_and_prior_revision_is_rollback_ref(tmp_path):
    root, previous_revision, expected = _repo(tmp_path)
    identity_dir = tmp_path / "identities"
    after = _identity(root, "lifeos-api", expected, startup="after")
    before = _identity(root, "lifeos-api", expected, startup="before")
    _write_identity(identity_dir, after)
    _write_identity(identity_dir, before)
    record = tmp_path / "restart.json"
    _restart(record, targets=("lifeos-api",), after={"lifeos-api": after}, before={"lifeos-api": before}, health={"lifeos-api": "healthy"})
    with _health_server({"status": "healthy", "runtime_identity": after.as_public_dict()}) as url:
        evidence = verify_runtime_evidence(
            expected_revision=expected, target_services=("lifeos-api",), restart_evidence=record,
            api_url=url, identity_dir=identity_dir, source_root=root,
        )
    assert evidence.accepted
    assert evidence.revert_ref == expected


def test_worker_only_and_combined_targets_require_real_scoped_records(tmp_path):
    root, previous_revision, expected = _repo(tmp_path)
    identity_dir = tmp_path / "identities"
    worker = _identity(root, "lifeos-agent-worker", expected, startup="worker-after")
    worker_before = _identity(root, "lifeos-agent-worker", previous_revision, startup="worker-before")
    _write_identity(identity_dir, worker)
    record = tmp_path / "worker-restart.json"
    _restart(record, targets=("lifeos-agent-worker",), after={"lifeos-agent-worker": worker}, before={"lifeos-agent-worker": worker_before}, health={"lifeos-agent-worker": "active"})
    evidence = verify_runtime_evidence(
        expected_revision=expected, target_services=("lifeos-agent-worker",), restart_evidence=record,
        api_url="http://127.0.0.1:1", identity_dir=identity_dir, source_root=root,
    )
    assert evidence.accepted

    combined = tmp_path / "combined.json"
    api = _identity(root, "lifeos-api", expected, startup="api-after")
    api_before = _identity(root, "lifeos-api", previous_revision, startup="api-before")
    _write_identity(identity_dir, api)
    _restart(combined, targets=("lifeos-api", "lifeos-agent-worker"), after={"lifeos-api": api, "lifeos-agent-worker": worker}, before={"lifeos-api": api_before, "lifeos-agent-worker": worker_before}, health={"lifeos-api": "healthy", "lifeos-agent-worker": "active"})
    with _health_server({"status": "healthy", "runtime_identity": api.as_public_dict()}) as url:
        evidence = verify_runtime_evidence(
            expected_revision=expected, target_services=("lifeos-api", "lifeos-agent-worker"), restart_evidence=combined,
            api_url=url, identity_dir=identity_dir, source_root=root,
        )
    assert evidence.accepted


@pytest.mark.parametrize("mutation,reason", [
    (lambda payload: payload.update({"result": 1}), "restart_not_successful"),
    (lambda payload: payload.update({"recorded_at_utc": "NaN"}), "restart_recorded_at_invalid"),
    (lambda payload: payload["identities"]["lifeos-api"].update({"process_start_time": "NaN"}), "lifeos-api_restart_after_identity_missing"),
])
def test_malformed_restart_json_fails_closed_through_cli(tmp_path, mutation, reason):
    root, previous_revision, expected = _repo(tmp_path)
    identity_dir = tmp_path / "identities"
    api = _identity(root, "lifeos-api", expected, startup="api-after")
    before = _identity(root, "lifeos-api", previous_revision, startup="api-before")
    _write_identity(identity_dir, api)
    _write_identity(identity_dir, before)
    record = tmp_path / "restart.json"
    _restart(record, targets=("lifeos-api",), after={"lifeos-api": api}, before={"lifeos-api": before}, health={"lifeos-api": "healthy"})
    payload = json.loads(record.read_text())
    mutation(payload)
    record.write_text(json.dumps(payload), encoding="utf-8")
    with _health_server({"status": "healthy", "runtime_identity": api.as_public_dict()}) as url:
        result = subprocess.run([
            sys.executable, "-m", "api.services.runtime_identity", "verify", "--expected-revision", expected,
            "--restart-evidence", str(record), "--identity-dir", str(identity_dir), "--source-root", str(root), "--api-url", url,
        ], cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert reason in json.loads(result.stdout)["reasons"]


def test_wrong_checkout_dead_process_and_missing_health_fail_closed(tmp_path):
    root, previous_revision, expected = _repo(tmp_path)
    identity_dir = tmp_path / "identities"
    api = _identity(root, "lifeos-api", expected, startup="api-after")
    before = _identity(root, "lifeos-api", previous_revision, startup="api-before")
    _write_identity(identity_dir, api)
    record = tmp_path / "restart.json"
    _restart(record, targets=("lifeos-api",), after={"lifeos-api": api}, before={"lifeos-api": before}, health={})
    with _health_server({"status": "healthy", "runtime_identity": api.as_public_dict()}) as url:
        stale = replace(api, source_root=str(tmp_path / "wrong-checkout"), process_start_time=api.process_start_time + 100)
        (identity_dir / f"{api.service}-{api.process_id}-{api.startup_id}.json").write_text(json.dumps(stale.as_dict()), encoding="utf-8")
        evidence = verify_runtime_evidence(
            expected_revision=expected, target_services=("lifeos-api",), restart_evidence=record,
            api_url=url, identity_dir=identity_dir, source_root=root,
        )
    assert not evidence.accepted
    assert "lifeos-api_scoped_health_missing" in evidence.reasons
    assert any(token in reason for reason in evidence.reasons for token in ("source_root", "process_identity_not_live", "health_identity_not_local"))


def test_restart_worker_wrapper_starts_machine_evidence_watcher(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "logs").mkdir()
    server_copy = project / "scripts" / "server.sh"
    server_copy.write_text(SERVER_SH.read_text(encoding="utf-8"), encoding="utf-8")
    server_copy.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    order = tmp_path / "order.log"
    fake_python = bindir / "fake-python"
    fake_python.write_text(
        "#!/bin/bash\n"
        f"for a in \"$@\"; do [ \"$a\" = \"--mark-self-restart\" ] && echo mark >> '{order}'; done\n"
        f"echo python:$* >> '{order}'\nexit 0\n", encoding="utf-8"
    )
    fake_python.chmod(0o755)
    for name, body in {
        "systemctl": f"echo restart >> '{order}'\nexit 0\n",
        "sudo": 'exec "$@"\n',
        "systemd-run": 'while [[ "$1" == --* ]]; do shift; done\nexec "$@"\n',
        "setsid": 'exec "$@"\n',
        "nohup": 'exec "$@"\n',
    }.items():
        path = bindir / name
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
    venv = tmp_path / ".venvs" / "lifeos" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(fake_python)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{bindir}:{os.environ['PATH']}", "LIFEOS_RUNTIME_IDENTITY_DIR": str(tmp_path / "identities")}
    result = subprocess.run([str(server_copy), "restart-worker-detached", "--session", "sid-1", "--notify", "Shipped"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for _ in range(20):
        if "wait-and-record" in order.read_text():
            break
        time.sleep(0.05)
    lines = order.read_text().splitlines()
    assert any(line == "mark" for line in lines)
    assert any(line == "restart" for line in lines)
    assert any("wait-and-record" in line for line in lines)
    assert lines.index("mark") < lines.index("restart")
