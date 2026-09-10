"""
Build an isolated source snapshot of a candidate LifeOS checkout.

A "candidate" is the exact source tree currently under test: tracked files
(with whatever uncommitted edits are actually on disk, not just the last
commit) plus untracked-but-not-gitignored files. Copying that tree into its
own directory and running the application from there — instead of from the
shared checkout — keeps hardcoded ``Path(__file__).../data``-style stores
from resolving back into the operator's real checkout: every such path is
relative to *this* root instead.

This module is intentionally generic (candidate identity + a safe file
snapshot), not test-runner-specific, so it can be reused anywhere a
candidate-evidence snapshot is needed instead of growing a second
implementation.

Every file lands in the snapshot fully self-contained: an internal symlink
is rebased to a relative link that still resolves correctly *inside the
snapshot*, never one that reaches back into the source checkout. A symlink
(or any ancestor directory in its path) that would resolve outside the
source root is rejected without ever opening or `stat`-following it — only
`lstat`/`readlink`, which read a link's own stored text, determine eligibility.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import stat as stat_module
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class UnsafeSymlinkError(RuntimeError):
    """A tracked/untracked path is, or passes through, a symlink whose target
    (or whose ancestor's target) escapes the source root."""


class SourceMutatedError(RuntimeError):
    """The source tree changed while it was being snapshotted."""


# Directories excluded even if somehow not caught by .gitignore. Matched by
# exact path-segment name anywhere in the relative path.
DEFAULT_EXCLUDE_DIR_NAMES = frozenset({
    "data", "vault", "logs", ".git", "__pycache__", ".pytest_cache",
    "node_modules", ".venv", "venv", ".chroma", "chroma_data",
    ".mypy_cache", ".ruff_cache",
})

# Exact filename patterns excluded even if somehow not caught by
# .gitignore. Deliberately mirrors .gitignore's own literal entries rather
# than broad substring globs (like `*credential*`) — a broad glob would also
# silently drop legitimate tracked source/tests whose name merely contains
# that word (e.g. `credential_service.py`, `test_credential_service.py`),
# which corrupts the candidate's actual code rather than protecting privacy.
DEFAULT_EXCLUDE_FILE_GLOBS: tuple[str, ...] = (
    ".env", ".env.local", ".env-*", "*.env", ".env.bak.*",
    "*.db", "*.sqlite",
    "*.log",
    "config/credentials*.json", "config/token*.json",
    "credentials.json", "credentials-*.json",
    "token.json", "token-*.json",
    "*.pem", "*.key",
)


def _git(args: Sequence[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _is_excluded(rel_path: str, extra_exclude_dir_names: frozenset[str],
                  extra_exclude_globs: tuple[str, ...]) -> bool:
    parts = Path(rel_path).parts
    exclude_dirs = DEFAULT_EXCLUDE_DIR_NAMES | extra_exclude_dir_names
    if any(part in exclude_dirs for part in parts[:-1]):
        return True
    for pattern in (*DEFAULT_EXCLUDE_FILE_GLOBS, *extra_exclude_globs):
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _is_runtime_directory_link(source_root: Path, rel_path: str,
                                extra_exclude_dir_names: frozenset[str]) -> bool:
    """True for a symlink standing in for one of the excluded runtime
    directories (``data``, ``logs``, …).

    Those names hold runtime state wherever they live, never candidate
    source: an operator's checkout may point one at another filesystem, and
    candidate verification points the candidate's own ``data`` at
    verifier-owned storage. ``_is_excluded`` only sees such a name as a
    parent segment, so the link itself has to be excluded here — exactly
    like the directory it names, rather than rejected for resolving outside
    the source root. Decided from the link's own `lstat`, never its target.
    """
    if Path(rel_path).name not in DEFAULT_EXCLUDE_DIR_NAMES | extra_exclude_dir_names:
        return False
    return os.path.islink(source_root / rel_path)


def _candidate_paths(source_root: Path) -> list[str]:
    """Tracked (working-tree content) + untracked-non-ignored relative paths, deduped."""
    cached = _git(["ls-files", "-z", "--cached"], source_root).split("\0")
    others = _git(["ls-files", "-z", "--others", "--exclude-standard"], source_root).split("\0")
    seen: dict[str, None] = {}
    for p in (*cached, *others):
        if p:
            seen[p] = None
    return list(seen.keys())


_MAX_SYMLINK_HOPS = 40


def _lexically_resolve_within_root(source_root: Path, rel_path: str) -> str:
    """Resolve every path component of ``rel_path`` under ``source_root``
    purely lexically — using only `os.path.islink`/`os.readlink` (which read
    a link's own stored text via `lstat`, never the target's content) — and
    return the fully resolved path relative to ``source_root``.

    Raises ``UnsafeSymlinkError`` if any component — including one reached
    only through a symlink TARGET's own intermediate segments, not just the
    symlink itself — would resolve outside ``source_root``.

    This walks exactly one path segment at a time, from a known-safe base,
    and re-validates from scratch whenever a symlink is found — including
    every segment of that symlink's *target*, recursively. That last part is
    what an earlier, buggy version of this function got wrong: given
    ``link -> nested/sentinel.txt`` where ``nested`` is *itself* a symlink to
    somewhere outside the root, checking only the fully-joined candidate
    ``root/nested/sentinel.txt`` against ``root`` as a string prefix passes
    (it lexically starts with ``root``), even though reaching it actually
    requires the kernel to traverse through the escaping ``nested`` symlink.
    Only checking `os.path.islink` on that one joined path never catches
    this, because reaching that final component at all already means the
    escape happened one segment earlier. Walking ``nested`` and
    ``sentinel.txt`` as two separate, individually-validated segments (via
    the recursion below) closes that gap: `nested`'s own escape is caught
    before `sentinel.txt` is ever considered.
    """
    root = os.path.normpath(str(source_root))
    hops = [0]

    def resolve_from(base: str, parts: tuple[str, ...]) -> str:
        current = base
        for part in parts:
            if part in ("", "."):
                continue
            candidate = os.path.normpath(os.path.join(current, part))
            try:
                Path(candidate).relative_to(root)
            except ValueError:
                raise UnsafeSymlinkError(
                    f"{rel_path!r} escapes the source root while resolving "
                    f"{part!r} under {current!r} (resolved to {candidate!r}, "
                    f"outside {root!r})"
                )
            if os.path.islink(candidate):
                hops[0] += 1
                if hops[0] > _MAX_SYMLINK_HOPS:
                    raise UnsafeSymlinkError(
                        f"{rel_path!r} involves more than {_MAX_SYMLINK_HOPS} "
                        f"symlink hops (possible cycle); refusing to resolve further"
                    )
                target = os.readlink(candidate)
                if os.path.isabs(target):
                    # An absolute target must itself land under `root` (a
                    # plain prefix check is enough here — no walking from
                    # "/" down through root's own real, external ancestry,
                    # which isn't part of the candidate tree being
                    # validated). Once confirmed, re-walk only the portion
                    # *under* root one segment at a time, the same as the
                    # relative-target branch below — that's what catches an
                    # absolute target whose own remainder passes through
                    # another escaping symlink.
                    normalized_target = os.path.normpath(target)
                    try:
                        remainder = Path(normalized_target).relative_to(root)
                    except ValueError:
                        raise UnsafeSymlinkError(
                            f"{rel_path!r} resolves (via absolute symlink "
                            f"target {target!r}) outside the source root {root!r}"
                        )
                    current = resolve_from(root, remainder.parts)
                else:
                    link_dir = os.path.dirname(candidate)
                    current = resolve_from(link_dir, Path(target).parts)
            else:
                current = candidate
        return current

    resolved = resolve_from(root, Path(rel_path).parts)
    return os.path.relpath(resolved, root)


def _rebased_symlink_target(source_root: Path, rel_path: str) -> str:
    """Return a *relative* symlink target, safe to write into the snapshot,
    that resolves to the same in-root file the original symlink resolves to
    — whether the original link was relative or absolute. Never copies an
    absolute source-root path verbatim (that would leave the snapshot
    pointing back at the real checkout).
    """
    resolved_rel = _lexically_resolve_within_root(source_root, rel_path)
    link_dir_rel = os.path.dirname(rel_path)
    return os.path.relpath(resolved_rel, link_dir_rel) if link_dir_rel else resolved_rel


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_normalized_mode(path: Path) -> int:
    """Git only ever tracks two regular-file modes: 100644 and 100755 — the
    executable bit is the only one that means anything to git or to running
    the snapshot. Normalizing to exactly 0o644/0o755 (rather than copying
    raw `st_mode`, which can carry incidental group/other/setuid bits from
    the filesystem/umask that have no source meaning) keeps the fingerprint
    stable across machines and snapshot runs of identical content.
    """
    is_exec = bool(os.stat(path).st_mode & stat_module.S_IXUSR)
    return 0o755 if is_exec else 0o644


@dataclass(frozen=True)
class SnapshotFile:
    rel_path: str
    mode: int
    content_hash: str  # sha256 hex for regular files; "symlink:<rebased target>" for symlinks


@dataclass(frozen=True)
class SnapshotResult:
    source_root: str
    dest_root: str
    git_head: str | None  # provenance metadata only — NOT part of candidate_id
    candidate_id: str  # pure content identity (== fingerprint, truncated)
    fingerprint: str
    files: tuple[SnapshotFile, ...]

    def to_dict(self) -> dict:
        return {
            "source_root": self.source_root,
            "dest_root": self.dest_root,
            "git_head": self.git_head,
            "candidate_id": self.candidate_id,
            "fingerprint": self.fingerprint,
            "file_count": len(self.files),
        }


def compute_fingerprint(files: Iterable[SnapshotFile]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f.rel_path):
        h.update(f"{f.rel_path}\0{f.mode:o}\0{f.content_hash}\n".encode())
    return h.hexdigest()


def build_snapshot(
    source_root: Path,
    dest_root: Path,
    *,
    extra_exclude_dir_names: frozenset[str] = frozenset(),
    extra_exclude_globs: tuple[str, ...] = (),
) -> SnapshotResult:
    """Copy the candidate's exact working-tree content into ``dest_root``.

    ``dest_root`` must not already exist (created fresh, mode 0700) — this
    function never merges into or overwrites an existing directory, so a
    stale snapshot can't silently blend with a new one. On any failure
    (including the source tree changing mid-copy), ``dest_root`` is removed
    before the error propagates — never left as a partial snapshot.
    """
    source_root = source_root.resolve()
    dest_root = dest_root.resolve()
    if dest_root.exists():
        raise FileExistsError(f"snapshot destination already exists: {dest_root}")
    dest_root.mkdir(mode=0o700, parents=True)

    try:
        try:
            git_head = _git(["rev-parse", "HEAD"], source_root).strip()
        except subprocess.CalledProcessError:
            git_head = None

        rel_paths_before = sorted(_candidate_paths(source_root))
        files: list[SnapshotFile] = []
        for rel_path in rel_paths_before:
            if _is_excluded(rel_path, extra_exclude_dir_names, extra_exclude_globs):
                continue
            # Necessarily before the lexical validation below: such a link's
            # target is legitimately outside the source root, and this decides
            # on the link's own lstat without dereferencing it.
            if _is_runtime_directory_link(source_root, rel_path, extra_exclude_dir_names):
                continue
            src_path = source_root / rel_path
            if not os.path.lexists(src_path):
                continue  # tracked-but-deleted-in-worktree; nothing to snapshot

            # Validate the FULL lexical chain — including every ancestor
            # directory — before any operation that would resolve/follow it
            # further. `os.path.islink`/`os.path.isfile` both traverse
            # intermediate path components at the OS level regardless of
            # which one is called first, so this must run before either,
            # not just before the operations that read file content.
            _lexically_resolve_within_root(source_root, rel_path)

            dest_path = dest_root / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            if os.path.islink(src_path):
                rebased_target = _rebased_symlink_target(source_root, rel_path)
                os.symlink(rebased_target, dest_path)
                files.append(SnapshotFile(rel_path, 0o120000, f"symlink:{rebased_target}"))
                continue

            if not os.path.isfile(src_path):
                continue  # directories appear implicitly via file paths

            shutil.copy2(src_path, dest_path)
            mode = _git_normalized_mode(dest_path)
            os.chmod(dest_path, mode)
            files.append(SnapshotFile(rel_path, mode, _hash_file(dest_path)))

        # Detect the source changing *during* the copy — including a
        # dirty-to-dirty edit of an already-modified tracked file, or an
        # untracked file changing while still untracked. Neither changes
        # git's status category, so a signature built from `git status`
        # text wouldn't see it; re-reading every included source file
        # directly and comparing against what was actually captured does.
        rel_paths_after = sorted(_candidate_paths(source_root))
        if rel_paths_after != rel_paths_before:
            raise SourceMutatedError(
                "the set of tracked/untracked candidate files changed while "
                "the source tree was being snapshotted"
            )
        for f in files:
            src_path = source_root / f.rel_path
            if not os.path.lexists(src_path):
                raise SourceMutatedError(
                    f"{f.rel_path} disappeared from the source tree while it "
                    f"was being snapshotted"
                )
            # Re-validate the full lexical chain before reading anything —
            # a parent directory swapped to an escaping symlink after the
            # initial copy is itself a mutation, not something to silently
            # read through.
            try:
                _lexically_resolve_within_root(source_root, f.rel_path)
            except UnsafeSymlinkError as e:
                raise SourceMutatedError(
                    f"{f.rel_path} is no longer safely reachable in the "
                    f"source tree ({e})"
                )
            if os.path.islink(src_path):
                current_hash = f"symlink:{_rebased_symlink_target(source_root, f.rel_path)}"
                current_mode = 0o120000
            else:
                current_hash = _hash_file(src_path)
                current_mode = _git_normalized_mode(src_path)
            if current_hash != f.content_hash or current_mode != f.mode:
                raise SourceMutatedError(
                    f"{f.rel_path} changed in the source tree while it was "
                    f"being snapshotted (its content or mode no longer "
                    f"matches what was copied)"
                )
    except Exception:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise

    fingerprint = compute_fingerprint(files)
    candidate_id = fingerprint[:24]

    return SnapshotResult(
        source_root=str(source_root),
        dest_root=str(dest_root),
        git_head=git_head,
        candidate_id=candidate_id,
        fingerprint=fingerprint,
        files=tuple(files),
    )


#: Structured mismatch kinds returned by ``verify_snapshot_unmodified``.
#: "missing"/"content_changed"/"mode_changed"/"type_changed" describe an
#: INCLUDED source file whose contents, mode, or type differ from the captured
#: snapshot — these are never ignorable by any caller, under any
#: policy, because they mean the tested candidate's actual code changed.
#: "unexpected_new_file" describes a path that didn't exist in the original
#: snapshot at all — the only kind a caller may selectively ignore, via
#: ``ignore_new_file_globs``, for its own known-generated runtime additions
#: (e.g. a cache directory a verifier deliberately allows a run to create).
SnapshotMismatchKind = str  # "missing" | "content_changed" | "mode_changed" | "type_changed" | "unexpected_new_file"


@dataclass(frozen=True)
class SnapshotMismatch:
    rel_path: str
    kind: SnapshotMismatchKind
    detail: str

    def __str__(self) -> str:
        return f"{self.rel_path} ({self.detail})"


def verify_snapshot_unmodified(
    result: SnapshotResult,
    *,
    ignore_new_file_globs: tuple[str, ...] = (),
) -> tuple[bool, list[SnapshotMismatch]]:
    """Re-scan ``result.dest_root`` and confirm it still matches its fingerprint.

    Returns ``(True, [])`` when nothing changed, else ``(False, mismatches)``.
    ``ignore_new_file_globs`` lets a caller (e.g. a verifier that allows a
    run to write specific generated artifacts into the snapshot) exempt
    *only* the "an unrecognized file appeared" check — it can
    never suppress a mismatch on a file that was actually part of the
    snapshot's included source, which must always be reported.
    """
    dest_root = Path(result.dest_root)
    mismatches: list[SnapshotMismatch] = []
    expected_by_path = {f.rel_path: f for f in result.files}

    for f in result.files:
        p = dest_root / f.rel_path
        if not os.path.lexists(p):
            mismatches.append(SnapshotMismatch(f.rel_path, "missing", "missing"))
            continue
        # Re-validate the full lexical chain before reading anything — a
        # parent directory swapped to an escaping symlink since the
        # snapshot was built is a mismatch to report, not a path to read
        # through.
        try:
            _lexically_resolve_within_root(dest_root, f.rel_path)
        except UnsafeSymlinkError as e:
            mismatches.append(SnapshotMismatch(f.rel_path, "unsafe_path", str(e)))
            continue
        if os.path.islink(p):
            actual_hash = f"symlink:{os.readlink(p)}"
            actual_mode = 0o120000
        elif os.path.isfile(p):
            actual_hash = _hash_file(p)
            actual_mode = _git_normalized_mode(p)
        else:
            mismatches.append(SnapshotMismatch(
                f.rel_path, "type_changed", "no longer a regular file/symlink",
            ))
            continue
        if actual_hash != f.content_hash:
            mismatches.append(SnapshotMismatch(f.rel_path, "content_changed", "content changed"))
        elif actual_mode != f.mode:
            mismatches.append(SnapshotMismatch(
                f.rel_path, "mode_changed", f"mode changed: {oct(f.mode)} -> {oct(actual_mode)}",
            ))

    # Also catch new files sneaking into the snapshot after the fact —
    # except ones the caller explicitly named as intended generated output.
    for root, _dirs, filenames in os.walk(dest_root):
        for name in filenames:
            full = Path(root) / name
            rel = str(full.relative_to(dest_root))
            if rel in expected_by_path:
                continue
            if any(fnmatch.fnmatch(rel, pattern) for pattern in ignore_new_file_globs):
                continue
            mismatches.append(SnapshotMismatch(rel, "unexpected_new_file", "unexpected new file"))

    return (len(mismatches) == 0, mismatches)
