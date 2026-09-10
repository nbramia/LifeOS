"""Private, atomic receipts for an exact candidate verification.

This deliberately stores only fingerprints and outcome summaries.  It is a
local reuse optimization, not a CI/security attestation.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
TERMINAL_FAILURES = frozenset({"failure", "cancelled", "incomplete", "infrastructure_failure"})
PRIVACY_AUDIT_NODEID = "tests/test_fixtures_no_personal_data.py::test_no_fixture_contains_a_real_sensitive_value"
PRIVACY_AUDIT_NOT_APPLICABLE_REASON = (
    "No real .env reachable from this checkout -- nothing to check "
    "fixtures against (expected on a fresh clone or CI)."
)
SAFE_ENVIRONMENT_NAMES = frozenset({
    "LIFEOS_TEST_PARALLEL_WORKERS", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "LIFEOS_PARALLEL_BROWSER_FREE",
})


class EvidenceError(RuntimeError):
    """Evidence is malformed, unsafe, or insufficient for reuse."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EvidenceError("evidence directory must be private and owned by this user")


@dataclasses.dataclass(frozen=True)
class VerificationInputs:
    """Every execution-affecting identity input, with no source path or secret."""

    content_fingerprint: str
    lane_inventory_fingerprint: str
    runner_fingerprint: str
    dependency_fingerprint: str
    environment_fingerprint: str
    base_identity: str | None = None
    merge_identity: str | None = None
    scope_identity: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.content_fingerprint, self.lane_inventory_fingerprint,
            self.runner_fingerprint, self.dependency_fingerprint,
            self.environment_fingerprint,
        ):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise EvidenceError("fingerprints must be sha256 hex")
        for value in (self.base_identity, self.merge_identity, self.scope_identity):
            if value is not None and (not isinstance(value, str) or len(value) > 128 or "\x00" in value):
                raise EvidenceError("invalid provenance identity")

    @property
    def key(self) -> str:
        return _digest(dataclasses.asdict(self))

    def public_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class LaneOutcome:
    lane: str
    nodeids: tuple[str, ...]
    exit_status: int
    result: str
    not_applicable: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.lane or not self.nodeids or self.exit_status is None:
            raise EvidenceError("lane outcome must name executed tests and an exit status")
        if self.result not in {"success", "failure", "cancelled", "incomplete", "infrastructure_failure"}:
            raise EvidenceError("invalid lane result")
        if self.not_applicable not in ((), ((PRIVACY_AUDIT_NODEID, PRIVACY_AUDIT_NOT_APPLICABLE_REASON),)):
            raise EvidenceError("invalid not-applicable outcome")
        if self.not_applicable and self.result != "success":
            raise EvidenceError("not-applicable outcome cannot mask a lane failure")
        if self.not_applicable and self.not_applicable[0][0] not in self.nodeids:
            raise EvidenceError("not-applicable node was not selected")

    def to_dict(self) -> dict[str, Any]:
        payload = {"lane": self.lane, "nodeids": list(self.nodeids), "exit_status": self.exit_status, "result": self.result}
        if self.not_applicable:
            nodeid, reason = self.not_applicable[0]
            payload["not_applicable"] = {"nodeid": nodeid, "reason": reason}
        return payload


def safe_environment_fingerprint(values: Mapping[str, str]) -> str:
    """Fingerprint only explicitly allowed non-secret execution controls."""
    if set(values) - SAFE_ENVIRONMENT_NAMES:
        raise EvidenceError("environment input is not in the public-safe allowlist")
    normalized: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(value, str) or not value.isascii() or len(value) > 32:
            raise EvidenceError("invalid safe environment value")
        if name == "LIFEOS_TEST_PARALLEL_WORKERS" and not value.isdecimal():
            raise EvidenceError("invalid worker count")
        if name == "PYTHONHASHSEED" and value != "random" and (not value.isdecimal() or int(value) >= 2**32):
            raise EvidenceError("invalid PYTHONHASHSEED")
        if name == "PYTHONDONTWRITEBYTECODE" and value != "1":
            raise EvidenceError("invalid bytecode policy")
        if name == "LIFEOS_PARALLEL_BROWSER_FREE" and value not in ("0", "1"):
            raise EvidenceError("invalid parallel-browser-free flag")
        normalized[name] = value
    return _digest(normalized)


def fingerprint_named_files(root: Path, names: Sequence[str]) -> str:
    """Hash a fixed, caller-approved relative file allowlist and executable mode."""
    rows = []
    for name in sorted(set(names)):
        candidate = root / name
        if not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise EvidenceError("fingerprint path must be a relative allowlist entry")
        if candidate.is_symlink() or not candidate.is_file():
            raise EvidenceError(f"required verification input is missing: {name}")
        rows.append((name, bool(candidate.stat().st_mode & 0o100), hashlib.sha256(candidate.read_bytes()).hexdigest()))
    return _digest(rows)


class EvidenceStore:
    """Per-key receipts with flock serialization and replace-only publication."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        _private_directory(self.root)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def _lock(self, key: str):
        lock = self.root / f"{key}.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise EvidenceError("evidence receipt is not a private regular file")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError("evidence receipt is unreadable") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("key") != key:
            raise EvidenceError("evidence receipt schema mismatch")
        return data

    def reusable(self, inputs: VerificationInputs, expected: Mapping[str, Sequence[str]]) -> tuple[dict[str, Any] | None, str]:
        receipt = self._read(inputs.key)
        if receipt is None:
            return None, "missing"
        if receipt.get("inputs") != inputs.public_dict():
            return None, "inputs_changed"
        attempts = receipt.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return None, "no_attempts"
        latest = attempts[-1]
        if latest.get("result") != "success":
            return None, f"prior_{latest.get('result', 'invalid')}"
        actual = {outcome.get("lane"): sorted(outcome.get("nodeids", [])) for outcome in latest.get("outcomes", [])}
        wanted = {lane: sorted(nodeids) for lane, nodeids in expected.items()}
        if actual != wanted or any(outcome.get("exit_status") != 0 for outcome in latest.get("outcomes", [])):
            return None, "outcomes_incomplete"
        return latest, "reused"

    def record(
        self,
        inputs: VerificationInputs,
        outcomes: Sequence[LaneOutcome],
        *,
        result: str,
        retry_reason: str | None = None,
        diagnostics: Sequence[str] = (),
    ) -> dict[str, Any]:
        if result not in {"success", *TERMINAL_FAILURES}:
            raise EvidenceError("invalid verification result")
        if retry_reason is not None and (not retry_reason or len(retry_reason) > 160):
            raise EvidenceError("invalid retry reason")
        if result == "success" and diagnostics:
            raise EvidenceError("successful verification cannot carry diagnostics")
        if len(diagnostics) > 20 or any(
            not isinstance(detail, str) or not detail or len(detail) > 512 or "\x00" in detail
            for detail in diagnostics
        ):
            raise EvidenceError("invalid verification diagnostics")
        key = inputs.key
        fd = self._lock(key)
        try:
            current = self._read(key)
            attempts = [] if current is None else list(current["attempts"])
            attempt = {
                "attempt_id": uuid.uuid4().hex,
                "result": result,
                "retry_reason": retry_reason,
                "outcomes": [outcome.to_dict() for outcome in outcomes],
            }
            if diagnostics:
                attempt["diagnostics"] = list(diagnostics)
            payload = {"schema_version": SCHEMA_VERSION, "key": key, "inputs": inputs.public_dict(), "attempts": [*attempts, attempt]}
            temp_fd, temp_name = tempfile.mkstemp(prefix="evidence-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self._path(key))
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return attempt
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = ["EvidenceError", "EvidenceStore", "LaneOutcome", "PRIVACY_AUDIT_NODEID", "PRIVACY_AUDIT_NOT_APPLICABLE_REASON", "VerificationInputs", "fingerprint_named_files", "safe_environment_fingerprint"]
