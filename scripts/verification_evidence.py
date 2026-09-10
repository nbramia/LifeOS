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


SCHEMA_VERSION = 2
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
    def execution_fingerprint_dict(self) -> dict[str, str]:
        """The execution-affecting subset alone: identical values here mean an
        identical tree was already run through an identical lane selection,
        runner, dependency set and environment -- regardless of which commit,
        base, or lane subset a given caller happens to attribute it to.

        ``base_identity``/``merge_identity``/``scope_identity`` are
        publication provenance, not execution identity: a dirty pre-commit
        run (no base, no merge SHA yet), the same tree committed and pushed
        (a concrete SHA and base), and a later re-push against a moved base
        are all the *same tested execution* and must share this key so the
        proof is not silently re-run for each attribution. They are recorded
        per attempt instead (see ``EvidenceStore.record``) so no receipt is
        relabeled and a real base change can still be detected and enforced
        at reuse time.
        """
        return {
            "content_fingerprint": self.content_fingerprint,
            "lane_inventory_fingerprint": self.lane_inventory_fingerprint,
            "runner_fingerprint": self.runner_fingerprint,
            "dependency_fingerprint": self.dependency_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
        }

    @property
    def key(self) -> str:
        return _digest(self.execution_fingerprint_dict)


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
        if receipt.get("execution_inputs") != inputs.execution_fingerprint_dict:
            return None, "inputs_changed"
        attempts = receipt.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return None, "no_attempts"
        latest = attempts[-1]
        if latest.get("result") != "success":
            return None, f"prior_{latest.get('result', 'invalid')}"
        # A receipt this store itself wrote always has a well-formed
        # provenance object (record() never omits it). Missing or malformed
        # provenance -- e.g. a hand-edited or corrupted file -- is never
        # treated as an implicit base_identity=None: that would let a
        # base-independent request slip past a base check that should have
        # applied. Fail closed instead of guessing.
        provenance = latest.get("provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != {"base_identity", "merge_identity", "scope_identity"}:
            return None, "malformed_attempt"
        recorded_base = provenance["base_identity"]
        if recorded_base is not None and (not isinstance(recorded_base, str) or len(recorded_base) > 128):
            return None, "malformed_attempt"
        # Base is a publication-authorization concern, not an execution one,
        # but a mismatch must still fail closed: a base recorded as None
        # matches only another None request (genuinely base-independent
        # execution -- e.g. a checkout with no upstream at all). Any concrete
        # base must match exactly; a moved or absent base is never silently
        # treated as compatible, or a stale local receipt could follow a
        # moved main without a fresh check.
        if inputs.base_identity != recorded_base:
            return None, "base_changed"
        outcomes = latest.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            return None, "malformed_attempt"
        by_lane: dict[str, set[str]] = {}
        for outcome in outcomes:
            lane, nodeids = outcome.get("lane"), outcome.get("nodeids")
            if (
                not isinstance(lane, str) or not lane or lane in by_lane
                or not isinstance(nodeids, list) or not nodeids
                or not all(isinstance(nodeid, str) for nodeid in nodeids)
            ):
                return None, "malformed_attempt"
            # A "success" attempt must be internally consistent: every stored
            # outcome -- not just the lanes this call happens to want -- must
            # itself report a clean pass. A tampered or partially-written
            # receipt that claims overall success while one outcome disagrees
            # must never become a cache hit for anything.
            if outcome.get("result") != "success" or outcome.get("exit_status") != 0:
                return None, "malformed_attempt"
            by_lane[lane] = set(nodeids)
        # A prior success proves every *lane* it actually covered -- a request
        # for a subset of those lanes (e.g. only "fast-unit" out of a
        # six-lane success) is served by the same attempt without demanding
        # every original lane back. Within a requested lane, though, the
        # node-ID inventory must match exactly: an explicit partial-node
        # selection is never opportunistically satisfied by a broader
        # recorded run, matching the CLI's existing exact-selection-or-reject
        # contract for --nodeids/--paths.
        for lane, wanted_nodeids in expected.items():
            entry = by_lane.get(lane)
            if entry is None or set(wanted_nodeids) != entry:
                return None, "scope_incomplete"
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
                # Provenance is per attempt, never collapsed into the shared
                # receipt: each attempt keeps the exact base/merge/scope
                # attribution it was actually run or reused under, so an
                # earlier attempt is never relabeled by a later one.
                "provenance": {
                    "base_identity": inputs.base_identity,
                    "merge_identity": inputs.merge_identity,
                    "scope_identity": inputs.scope_identity,
                },
                "outcomes": [outcome.to_dict() for outcome in outcomes],
            }
            if diagnostics:
                attempt["diagnostics"] = list(diagnostics)
            payload = {
                "schema_version": SCHEMA_VERSION, "key": key,
                "execution_inputs": inputs.execution_fingerprint_dict,
                "attempts": [*attempts, attempt],
            }
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
