"""Single resolver for CLI executor binaries (`claude`, `codex`).

Readiness checks and spawn paths must agree on whether a configured command
resolves to a real executable — a bare command name that isn't on the
worker service's own (minimal) PATH can still exist in a well-known
per-user or package-manager install directory that an interactive shell's
PATH would include. This module is the one place that search happens, so
every caller sees the same answer.

Kept free of imports from `worker.py`, `model_catalog.py`, or either
executor module so none of them need to import each other to share this
logic.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

# Common per-user and package-manager install directories searched, in
# order, when a bare command name isn't on PATH. Applied uniformly to every
# CLI-backed engine (claude, codex).
_SEARCH_DIRS = (
    "~/.local/bin",
    "/usr/local/bin",
    "~/.npm/bin",
    "/opt/homebrew/bin",
)


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _nvm_candidates(command: str) -> list[str]:
    """``~/.nvm/versions/node/v*/bin/<command>``, newest version first.

    npm-global installs (codex is typically ``npm i -g``-installed) land
    under the active node version's bin directory.
    """
    nvm_root = os.path.expanduser("~/.nvm/versions/node")
    if not os.path.isdir(nvm_root):
        return []
    return [
        os.path.join(nvm_root, version, "bin", command)
        for version in sorted(os.listdir(nvm_root), reverse=True)
    ]


@dataclass(frozen=True)
class BinaryResolution:
    """Outcome of resolving a configured command to a concrete executable.

    ``resolved`` is the absolute path found, or ``None`` if nothing
    matched. ``candidates`` lists every location that was tried, in
    order, for diagnostics. ``override`` is set when ``configured`` was
    an absolute path — an explicit operator choice that is honoured
    exactly rather than repaired by falling through to the search.
    """

    configured: str
    resolved: str | None
    candidates: tuple[str, ...] = ()
    override: bool = False

    @property
    def ready(self) -> bool:
        return self.resolved is not None


def resolve_binary(configured: str) -> BinaryResolution:
    """Resolve ``configured`` (a bare command name or an absolute path).

    Resolution order:
      1. An absolute ``configured`` value is an explicit operator
         override. It is honoured exactly: resolved when it names an
         existing executable file, unresolved when it does not. This
         never falls through to the search below — a wrong override
         must be visible, not silently repaired.
      2. ``shutil.which(configured)`` — the calling process's own PATH.
      3. The common install directories in ``_SEARCH_DIRS``, then any
         ``~/.nvm/versions/node/*/bin`` directories (newest version
         first).
    """
    expanded = os.path.expanduser(configured)
    if os.path.isabs(expanded):
        resolved = expanded if _is_executable_file(expanded) else None
        return BinaryResolution(configured=configured, resolved=resolved, candidates=(expanded,), override=True)

    candidates: list[str] = [f"$PATH ({configured})"]
    which = shutil.which(configured)
    if which:
        return BinaryResolution(configured=configured, resolved=which, candidates=tuple(candidates))

    for directory in _SEARCH_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), configured)
        candidates.append(candidate)
        if _is_executable_file(candidate):
            return BinaryResolution(configured=configured, resolved=candidate, candidates=tuple(candidates))

    for candidate in _nvm_candidates(configured):
        candidates.append(candidate)
        if _is_executable_file(candidate):
            return BinaryResolution(configured=configured, resolved=candidate, candidates=tuple(candidates))

    return BinaryResolution(configured=configured, resolved=None, candidates=tuple(candidates))


def resolve_for_spawn(configured: str) -> str:
    """Resolve for launch: the resolved path, or ``configured`` itself on
    total failure so the caller still surfaces ``FileNotFoundError`` on
    spawn."""
    resolution = resolve_binary(configured)
    return resolution.resolved if resolution.resolved else configured


def describe_candidates(resolution: BinaryResolution) -> str:
    """Human-readable list of the locations that were searched, for
    diagnostics shown to the operator when nothing resolved."""
    return ", ".join(resolution.candidates)
