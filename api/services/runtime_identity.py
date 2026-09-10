"""Startup-bound process identities and deployment evidence.

Runtime identities are captured by the API and worker at process startup.  A
deployment verifier consumes those producer records plus the restart wrapper's
before/after record; it never treats checkout HEAD or free-form success text
as evidence of a running process.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

try:
    import psutil
except ImportError:  # pragma: no cover - existing project dependency
    psutil = None


REPO_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_PROVENANCE = "lifeos.runtime_identity:process-startup"
RESTART_PROVENANCE = "lifeos.runtime_identity:restart-wrapper"
KNOWN_SERVICES = frozenset(("lifeos-api", "lifeos-agent-worker"))
IDENTITY_DIR = Path(
    os.environ.get(
        "LIFEOS_RUNTIME_IDENTITY_DIR",
        str(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "lifeos" / "runtime-identities"),
    )
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = stamp.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return result if math.isfinite(result) else None


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class RuntimeIdentity:
    service: str
    revision: str | None
    clean: bool
    process_id: int
    startup_id: str
    source_root: str
    started_at_utc: str
    provenance: str = IDENTITY_PROVENANCE
    last_heartbeat_at_utc: str | None = None
    process_start_time: float | None = None

    @property
    def record_path(self) -> Path:
        return IDENTITY_DIR / f"{self.service}-{self.process_id}-{self.startup_id}.json"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def as_public_dict(self) -> dict[str, object]:
        payload = self.as_dict()
        payload.pop("source_root", None)
        return payload


def capture_runtime_identity(service: str, *, source_root: Path | None = None) -> RuntimeIdentity:
    """Capture revision and process identity once, at process startup."""
    root = (source_root or REPO_ROOT).resolve()
    revision = _git_value(root, "rev-parse", "--verify", "HEAD")
    status = _git_value(root, "status", "--porcelain", "--untracked-files=all")
    process_start_time = None
    if psutil is not None:
        try:
            process_start_time = float(psutil.Process(os.getpid()).create_time())
        except (OSError, ValueError, TypeError, psutil.Error):
            process_start_time = None
    return RuntimeIdentity(
        service=service,
        revision=revision,
        clean=bool(revision) and status == "",
        process_id=os.getpid(),
        startup_id=uuid.uuid4().hex,
        source_root=str(root),
        started_at_utc=_now(),
        process_start_time=process_start_time,
    )


def publish_runtime_identity(identity: RuntimeIdentity) -> Path:
    """Atomically publish a startup identity record for local consumers."""
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    path = identity.record_path
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(identity.as_dict(), sort_keys=True, allow_nan=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def heartbeat_runtime_identity(identity: RuntimeIdentity) -> Path:
    """Publish a heartbeat while retaining all startup-bound identity fields."""
    return publish_runtime_identity(replace(identity, last_heartbeat_at_utc=_now()))


def _load(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _validate_identity(
    identity: RuntimeIdentity | None,
    *,
    expected_service: str | None = None,
    source_root: Path | None = None,
) -> tuple[bool, str]:
    if identity is None:
        return False, "identity_missing"
    if not isinstance(identity.service, str) or not identity.service:
        return False, "identity_service_invalid"
    if expected_service is not None and identity.service != expected_service:
        return False, "identity_service_mismatch"
    if identity.revision is not None and not _is_sha(identity.revision):
        return False, "identity_revision_invalid"
    if type(identity.clean) is not bool:
        return False, "identity_clean_invalid"
    if type(identity.process_id) is not int or identity.process_id <= 0:
        return False, "identity_process_id_invalid"
    if not isinstance(identity.startup_id, str) or not identity.startup_id:
        return False, "identity_startup_id_invalid"
    if not isinstance(identity.source_root, str) or not identity.source_root:
        return False, "identity_source_root_invalid"
    try:
        actual_root = Path(identity.source_root).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, "identity_source_root_invalid"
    if source_root is not None:
        try:
            if actual_root != source_root.resolve():
                return False, "identity_source_root_mismatch"
        except (OSError, RuntimeError, TypeError, ValueError):
            return False, "identity_source_root_invalid"
    if not isinstance(identity.started_at_utc, str) or _parse_timestamp(identity.started_at_utc) is None:
        return False, "identity_started_at_invalid"
    if identity.last_heartbeat_at_utc is not None and _parse_timestamp(identity.last_heartbeat_at_utc) is None:
        return False, "identity_heartbeat_invalid"
    if identity.process_start_time is None:
        return False, "identity_process_start_time_missing"
    if isinstance(identity.process_start_time, bool) or not isinstance(identity.process_start_time, (int, float)):
        return False, "identity_process_start_time_invalid"
    if not math.isfinite(float(identity.process_start_time)) or float(identity.process_start_time) <= 0:
        return False, "identity_process_start_time_invalid"
    if identity.provenance != IDENTITY_PROVENANCE:
        return False, "identity_provenance_untrusted"
    return True, ""


def _identity_from_mapping(payload: object) -> RuntimeIdentity | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        identity = RuntimeIdentity(**dict(payload))
    except (TypeError, ValueError):
        return None
    valid, _ = _validate_identity(identity)
    return identity if valid else None


def load_identity(path: Path | None) -> RuntimeIdentity | None:
    return _identity_from_mapping(_load(path))


def _identity_paths(service: str, identity_dir: Path) -> list[Path]:
    try:
        candidates = list(identity_dir.glob(f"{service}-*.json"))
        return sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except (OSError, RuntimeError):
        return []


def latest_identity(service: str, *, identity_dir: Path = IDENTITY_DIR) -> RuntimeIdentity | None:
    for path in _identity_paths(service, identity_dir):
        identity = load_identity(path)
        if identity is not None:
            return identity
    return None


def _identity_by_startup(service: str, startup_id: str, identity_dir: Path, source_root: Path) -> RuntimeIdentity | None:
    for path in _identity_paths(service, identity_dir):
        identity = load_identity(path)
        if identity is not None and identity.startup_id == startup_id:
            valid, _ = _validate_identity(identity, expected_service=service, source_root=source_root)
            if valid:
                return identity
    return None


@dataclass(frozen=True)
class RuntimeEvidence:
    accepted: bool
    expected_revision: str
    target_services: tuple[str, ...]
    restart_result: str
    observed: Mapping[str, Mapping[str, object]]
    health_ok: bool
    revert_ref: str | None
    source: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["target_services"] = list(self.target_services)
        payload["reasons"] = list(self.reasons)
        return payload


def evaluate_runtime_evidence(
    *,
    expected_revision: str,
    target_services: Iterable[str],
    restart_result: str,
    observations: Mapping[str, RuntimeIdentity | None],
    health_ok: bool,
    revert_ref: str | None,
    source: str = "lifeos.runtime_identity:deployment-verifier",
) -> RuntimeEvidence:
    """Evaluate observed values without allowing malformed values out."""
    reasons: list[str] = []
    expected = expected_revision if isinstance(expected_revision, str) else ""
    services = tuple(dict.fromkeys(service for service in target_services if isinstance(service, str) and service))
    if not _is_sha(expected):
        reasons.append("expected_revision_not_full_sha")
    if not services:
        reasons.append("target_services_missing")
    for service in services:
        if service not in KNOWN_SERVICES:
            reasons.append(f"{service}_service_unknown")
    if restart_result != "success":
        reasons.append("restart_not_successful")
    if type(health_ok) is not bool or not health_ok:
        reasons.append("scoped_health_failed")
    if not _is_sha(revert_ref):
        reasons.append("usable_revert_reference_missing")
    for service in services:
        identity = observations.get(service)
        valid, reason = _validate_identity(identity, expected_service=service)
        if not valid:
            if reason == "identity_process_id_invalid":
                reasons.append(f"{service}_process_identity_missing")
                continue
            reasons.append(f"{service}_{reason}")
            continue
        assert identity is not None
        if identity.revision != expected:
            reasons.append(f"{service}_revision_mismatch")
        if not identity.clean:
            reasons.append(f"{service}_identity_not_clean")
    return RuntimeEvidence(
        accepted=not reasons,
        expected_revision=expected,
        target_services=services,
        restart_result=restart_result if isinstance(restart_result, str) else "invalid",
        observed={service: (identity.as_public_dict() if identity is not None else {}) for service, identity in observations.items()},
        health_ok=health_ok if type(health_ok) is bool else False,
        revert_ref=revert_ref if isinstance(revert_ref, str) else None,
        source=source,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _restart_services(payload: Mapping[str, object]) -> tuple[str, ...]:
    services = payload.get("services")
    if services is not None:
        if not isinstance(services, list) or any(type(service) is not str for service in services):
            return ()
        return tuple(dict.fromkeys(services))
    service = payload.get("service")
    return (service,) if type(service) is str and service else ()


def _restart_identity(payload: Mapping[str, object], service: str, *, previous: bool = False) -> RuntimeIdentity | None:
    key = "previous_identities" if previous else "identities"
    identities = payload.get(key)
    if isinstance(identities, Mapping):
        return _identity_from_mapping(identities.get(service))
    if payload.get("service") == service:
        return _identity_from_mapping(payload.get("previous_identity" if previous else "identity"))
    return None


def _health_observed(payload: Mapping[str, object], service: str) -> bool:
    health = payload.get("health")
    if not isinstance(health, Mapping):
        return False
    item = health.get(service)
    if not isinstance(item, Mapping):
        return False
    return item.get("status") in ("healthy", "active") and item.get("observed") is True


def _process_identity_matches(identity: RuntimeIdentity) -> bool:
    if psutil is None:
        return False
    valid, _ = _validate_identity(identity)
    if not valid:
        return False
    try:
        process = psutil.Process(identity.process_id)
        return abs(float(process.create_time()) - float(identity.process_start_time)) <= 1e-6
    except (OSError, ValueError, TypeError, psutil.Error):
        return False


def _heartbeat_is_recent(identity: RuntimeIdentity, *, max_age_seconds: float = 300.0) -> bool:
    if not identity.last_heartbeat_at_utc:
        return False
    stamp = _parse_timestamp(identity.last_heartbeat_at_utc)
    if stamp is None:
        return False
    age = time.time() - stamp
    return math.isfinite(age) and 0 <= age <= max_age_seconds


def _usable_revert_ref(root: Path, revision: str | None) -> str | None:
    if not _is_sha(revision):
        return None
    resolved = _git_value(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return resolved if _is_sha(resolved) else None


def _health_request(api_url: str, timeout_seconds: float) -> tuple[bool, str | None]:
    try:
        health_url = api_url.rstrip("/")
        if not health_url.endswith("/health"):
            health_url += "/health"
        with urllib.request.urlopen(health_url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("status") != "healthy":
            return False, None
        public_identity = payload.get("runtime_identity")
        if not isinstance(public_identity, Mapping) or type(public_identity.get("startup_id")) is not str:
            return False, None
        return True, public_identity["startup_id"]
    except Exception:
        return False, None


def verify_runtime_evidence(
    *,
    expected_revision: str,
    target_services: Iterable[str],
    restart_evidence: Path,
    api_url: str,
    identity_dir: Path = IDENTITY_DIR,
    worker_evidence: Path | None = None,
    source_root: Path = REPO_ROOT,
    timeout_seconds: float = 5.0,
) -> RuntimeEvidence:
    """Verify machine-produced, startup-bound evidence for selected services."""
    targets = tuple(dict.fromkeys(service for service in target_services if isinstance(service, str) and service))
    restart = _load(restart_evidence)
    reasons: list[str] = []
    if restart is None:
        reasons.append("restart_evidence_missing")
    else:
        if restart.get("source") != RESTART_PROVENANCE:
            reasons.append("restart_evidence_provenance_untrusted")
        if _parse_timestamp(restart.get("recorded_at_utc")) is None:
            reasons.append("restart_recorded_at_invalid")
    restart_services = _restart_services(restart or {})
    if set(restart_services) != set(targets):
        reasons.append("restart_services_mismatch")
    restart_result = restart.get("result") if restart is not None else "missing"
    if type(restart_result) is not str:
        restart_result = "invalid"
    observations: dict[str, RuntimeIdentity | None] = {}
    health_ok = True

    if "lifeos-api" in targets:
        api_health_ok, api_startup_id = _health_request(api_url, timeout_seconds)
        health_ok = health_ok and api_health_ok
        if api_startup_id is None:
            reasons.append("lifeos-api_health_identity_missing")
        else:
            local = _identity_by_startup(
                "lifeos-api", api_startup_id, identity_dir, source_root.resolve()
            )
            observations["lifeos-api"] = local
            if local is None:
                reasons.append("lifeos-api_health_identity_not_local_producer")
    if "lifeos-agent-worker" in targets:
        worker_after = _restart_identity(restart or {}, "lifeos-agent-worker")
        local = load_identity(worker_evidence) if worker_evidence else None
        if local is None and worker_after is not None:
            local = _identity_by_startup(
                "lifeos-agent-worker", worker_after.startup_id, identity_dir, source_root.resolve()
            )
        observations["lifeos-agent-worker"] = local
        if not _health_observed(restart or {}, "lifeos-agent-worker"):
            health_ok = False
            reasons.append("lifeos-agent-worker_scoped_health_missing")
        if local is None:
            reasons.append("lifeos-agent-worker_identity_missing")

    for service in targets:
        if service == "lifeos-api" and not _health_observed(restart or {}, service):
            health_ok = False
            reasons.append("lifeos-api_scoped_health_missing")

    previous_revisions: list[str] = []
    try:
        resolved_root = source_root.resolve()
    except Exception:
        resolved_root = source_root
    for service in targets:
        identity = observations.get(service)
        valid, reason = _validate_identity(identity, expected_service=service, source_root=resolved_root)
        if not valid:
            reasons.append(f"{service}_{reason}")
            continue
        assert identity is not None
        if identity.revision != expected_revision:
            reasons.append(f"{service}_revision_mismatch")
        if not identity.clean:
            reasons.append(f"{service}_identity_not_clean")
        if not _process_identity_matches(identity):
            reasons.append(f"{service}_process_identity_not_live")
        if service == "lifeos-agent-worker" and not _heartbeat_is_recent(identity):
            reasons.append("lifeos-agent-worker_heartbeat_stale")
        after = _restart_identity(restart or {}, service)
        before = _restart_identity(restart or {}, service, previous=True)
        valid_after, after_reason = _validate_identity(after, expected_service=service, source_root=resolved_root)
        valid_before, before_reason = _validate_identity(before, expected_service=service, source_root=resolved_root)
        if not valid_after:
            reasons.append(f"{service}_restart_after_{after_reason}")
        elif after.startup_id != identity.startup_id:
            reasons.append(f"{service}_restart_after_observation_mismatch")
        if not valid_before:
            reasons.append(f"{service}_restart_before_{before_reason}")
        elif before.startup_id == identity.startup_id:
            reasons.append(f"{service}_restart_identity_unchanged")
        elif before.revision:
            previous_revisions.append(before.revision)

    revert_ref = _usable_revert_ref(resolved_root, previous_revisions[0] if previous_revisions else None)
    if revert_ref is None:
        reasons.append("usable_revert_reference_missing")
    base = evaluate_runtime_evidence(
        expected_revision=expected_revision,
        target_services=targets,
        restart_result=restart_result,
        observations=observations,
        health_ok=health_ok,
        revert_ref=revert_ref,
    )
    reasons.extend(base.reasons)
    return replace(base, accepted=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def _write_restart_record(
    *,
    service: str,
    result: str,
    output: Path,
    identity: RuntimeIdentity | None,
    previous_identity: RuntimeIdentity | None,
    health_status: str | None,
) -> None:
    health = {}
    if health_status is not None:
        health[service] = {"status": health_status, "observed": True}
    payload: dict[str, object] = {
        "schema_version": 1,
        "service": service,
        "services": [service],
        "result": result,
        "identity": identity.as_dict() if identity else None,
        "previous_identity": previous_identity.as_dict() if previous_identity else None,
        "identities": {service: identity.as_dict() if identity else None},
        "previous_identities": {service: previous_identity.as_dict() if previous_identity else None},
        "health": health,
        "source": RESTART_PROVENANCE,
        "recorded_at_utc": _now(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)


def _launchd_unit_is_active(label: str) -> bool:
    """Return whether a user launchd label has a live PID."""
    try:
        result = subprocess.run(
            ["launchctl", "list"], check=False, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] == label:
            return fields[0].isdigit() and int(fields[0]) > 0
    return False


def _unit_is_active(args: argparse.Namespace) -> bool:
    if args.manager == "launchd":
        return _launchd_unit_is_active(args.launchd_label or args.unit)
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", args.unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def _wait_and_record(args: argparse.Namespace) -> int:
    before = load_identity(args.previous_identity) if args.previous_identity else None
    deadline = time.monotonic() + max(0.1, float(args.timeout_seconds))
    after = None
    active = False
    while time.monotonic() < deadline:
        after = latest_identity(args.service, identity_dir=args.identity_dir)
        changed = after is not None and (before is None or after.startup_id != before.startup_id)
        if changed:
            active = _unit_is_active(args)
            heartbeat_ok = args.service != "lifeos-agent-worker" or _heartbeat_is_recent(after)
            if active and heartbeat_ok:
                break
        time.sleep(0.2)
    result = "success" if after is not None and active and before is not None and after.startup_id != before.startup_id else "failure"
    _write_restart_record(
        service=args.service,
        result=result,
        output=args.output,
        identity=after,
        previous_identity=before,
        health_status="active" if result == "success" else "failed",
    )
    return 0 if result == "success" else 1


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record-restart")
    record.add_argument("--service", required=True)
    record.add_argument("--result", choices=("success", "failure"), required=True)
    record.add_argument("--identity", type=Path)
    record.add_argument("--previous-identity", type=Path)
    record.add_argument("--health-status", choices=("healthy", "active", "failed"))
    record.add_argument("--output", type=Path, required=True)
    waiter = sub.add_parser("wait-and-record")
    waiter.add_argument("--service", required=True, choices=sorted(KNOWN_SERVICES))
    waiter.add_argument("--unit", required=True)
    waiter.add_argument("--identity-dir", type=Path, default=IDENTITY_DIR)
    waiter.add_argument("--previous-identity", type=Path)
    waiter.add_argument("--output", type=Path, required=True)
    waiter.add_argument("--timeout-seconds", type=float, default=60.0)
    waiter.add_argument("--manager", choices=("systemd", "launchd"), default="systemd")
    waiter.add_argument("--launchd-label")
    verify = sub.add_parser("verify")
    verify.add_argument("--expected-revision", required=True)
    verify.add_argument("--services", default="lifeos-api")
    verify.add_argument("--restart-evidence", type=Path, required=True)
    verify.add_argument("--api-url", default="http://127.0.0.1:8000")
    verify.add_argument("--identity-dir", type=Path, default=IDENTITY_DIR)
    verify.add_argument("--worker-evidence", type=Path)
    verify.add_argument("--source-root", type=Path, default=REPO_ROOT)
    try:
        args = parser.parse_args(argv)
        if args.command == "record-restart":
            _write_restart_record(
                service=args.service,
                result=args.result,
                output=args.output,
                identity=load_identity(args.identity) if args.identity else None,
                previous_identity=load_identity(args.previous_identity) if args.previous_identity else None,
                health_status=args.health_status,
            )
            return 0
        if args.command == "wait-and-record":
            return _wait_and_record(args)
        evidence = verify_runtime_evidence(
            expected_revision=args.expected_revision,
            target_services=args.services.split(","),
            restart_evidence=args.restart_evidence,
            api_url=args.api_url,
            identity_dir=args.identity_dir,
            worker_evidence=args.worker_evidence,
            source_root=args.source_root,
        )
        print(json.dumps(evidence.as_dict(), sort_keys=True, allow_nan=False))
        return 0 if evidence.accepted else 1
    except Exception as exc:
        payload = RuntimeEvidence(
            accepted=False,
            expected_revision="",
            target_services=(),
            restart_result="invalid",
            observed={},
            health_ok=False,
            revert_ref=None,
            source="lifeos.runtime_identity:deployment-verifier",
            reasons=(f"verifier_error:{type(exc).__name__}",),
        )
        print(json.dumps(payload.as_dict(), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
