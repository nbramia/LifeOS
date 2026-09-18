"""Git worktree + branch provisioning for CLI-routed agent worker sessions.

A board-dispatched Claude Code or Codex session must never run directly
inside the primary checkout — the working tree the production API server
runs from. Before such a session starts, :func:`ensure_worktree` gives it
an isolated worktree on a fresh branch, off a freshly-fetched
``origin/<default-branch>``, so the session can commit, push, and open a
pull request the way every other change in this project is made.

Provisioning is deterministic and idempotent: both the worktree path and
the branch name are derived from the repository's toplevel and the task
id, so re-provisioning for the same task — a worker restart mid-run, a
racing duplicate dispatch tick — finds and reuses the same worktree
instead of creating a second one. A resumed session reuses its worktree
for a simpler reason: the working directory persists on the session's own
execution spec, so a resume never calls this module at all.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_TIMEOUT = 30  # seconds, per git subprocess call


class WorktreeError(Exception):
    """Raised when worktree/branch provisioning cannot proceed safely.

    Callers must fail the task closed on this — never fall back to running
    the session in the caller-supplied directory.
    """


@dataclass(frozen=True)
class WorktreeResult:
    """Where a CLI session should run, and on which branch.

    ``working_dir`` is the freshly-provisioned (or reused) worktree path
    when ``is_git`` is True. When ``is_git`` is False, ``working_dir`` is
    the original directory unchanged — it wasn't inside a git repository
    at all, so behavior is identical to before this module existed.
    """

    working_dir: str
    is_git: bool
    branch: Optional[str] = None
    repo_toplevel: Optional[str] = None
    reused: bool = False


@dataclass(frozen=True)
class WorktreeContext:
    """A session's own worktree/branch, detected from its working directory.

    Used to decide whether the git-discipline prompt block applies to a
    fresh CLI session — derived straight from the filesystem/git state
    rather than threaded through the dispatch payload, so it stays correct
    regardless of how the working directory was set.
    """

    branch: str
    repo_toplevel: str


def _run(cmd: list[str], *, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )


# ---------------------------------------------------------------------------
# Detection — shared by provisioning and the prompt-assembly seam.
# ---------------------------------------------------------------------------

def repo_toplevel(path: str, *, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """The git working-tree root containing ``path``, or None when
    ``path`` doesn't exist or isn't inside a git repository. For a linked
    worktree this is that worktree's own root, not the primary checkout's
    — each worktree is its own working-tree root even though they share
    one underlying repository."""
    if not os.path.isdir(path):
        return None
    try:
        result = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def is_linked_worktree(working_dir: str) -> bool:
    """True when ``working_dir`` is a linked git worktree — its own
    ``.git`` is a file pointing at the shared repo, never a directory the
    way the primary checkout's is."""
    return os.path.isfile(os.path.join(working_dir, ".git"))


def current_branch(working_dir: str, *, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """The branch checked out in ``working_dir``, or None on any git
    failure (detached HEAD, not a repo, timeout)."""
    try:
        result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=working_dir, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


def describe_worktree(working_dir: Optional[str], *, timeout: int = DEFAULT_TIMEOUT) -> Optional[WorktreeContext]:
    """The branch/repo for ``working_dir`` when it's a linked git worktree
    (never the primary checkout), else None.

    Consulted by the CLI executors' prompt assembly to decide whether the
    git-discipline instructions apply to a fresh session — no dispatch
    payload plumbing required.
    """
    if not working_dir or not is_linked_worktree(working_dir):
        return None
    branch = current_branch(working_dir, timeout=timeout)
    if not branch:
        return None
    toplevel = repo_toplevel(working_dir, timeout=timeout)
    if not toplevel:
        return None
    return WorktreeContext(branch=branch, repo_toplevel=toplevel)


# Shared instructional text for a fresh CLI session running inside a
# worker-provisioned worktree. Each executor's prompt assembly wraps this
# in its own presentation (a system-prompt section for Claude Code, a
# prepended header for Codex) — the content stays identical so the two
# routes make the same commitment to the session either way.
GIT_DISCIPLINE_INSTRUCTIONS = (
    "You are working in your own git worktree, isolated from the primary "
    "checkout, on branch `{branch}`. Commit your work as you go — don't "
    "leave it uncommitted. When you consider the task fully complete with "
    "nothing further needed from you, make sure everything is committed: "
    "the worker pushes your branch and opens a pull request once you "
    "finish, so an uncommitted result is never treated as a finished one. "
    "If you pause to ask a question before the task is done, commit "
    "whatever you have first."
)


def git_discipline_text(working_dir: Optional[str], *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """The git-discipline instructions for a fresh CLI session in
    ``working_dir``, or an empty string when it isn't a worker-provisioned
    worktree (a vault/home-directory task, or an operator spawn with no
    worktree at all)."""
    context = describe_worktree(working_dir, timeout=timeout)
    if context is None:
        return ""
    return GIT_DISCIPLINE_INSTRUCTIONS.format(branch=context.branch)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

_TYPE_PREFIX_RE = re.compile(r"^(feat|fix|docs|test|refactor|perf|chore)[:/\s-]+(.+)$", re.IGNORECASE)


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].strip("-") or "task"


def derive_branch_name(title: str, task_id: str) -> str:
    """``<type>/<slug>-<suffix>`` per the project's branch-naming convention.

    ``type`` is read off a leading conventional prefix in the title
    (``fix: ...``, ``feat/...``) when present, else defaults to ``agent``.
    The task id's own tail is appended so two similarly-titled tasks never
    collide on the same branch.
    """
    title = (title or "").strip()
    match = _TYPE_PREFIX_RE.match(title)
    if match:
        branch_type, rest = match.group(1).lower(), match.group(2)
    else:
        branch_type, rest = "agent", title
    suffix = re.sub(r"[^a-z0-9]", "", task_id.lower())[-8:] or "task"
    return f"{branch_type}/{_slugify(rest)}-{suffix}"


def worktree_dir_for(repo_toplevel_path: str, task_id: str) -> str:
    """Deterministic sibling worktree directory for one task.

    Deterministic on (repo, task_id) so re-provisioning for the same task
    — a worker restart mid-provisioning, a racing duplicate dispatch tick
    — finds the same directory instead of creating a new one.
    """
    top = Path(repo_toplevel_path)
    suffix = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)
    return str(top.parent / f"{top.name}-wt-agent-{suffix}")


def _detect_default_branch(toplevel: str, *, timeout: int) -> str:
    try:
        result = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=toplevel, timeout=timeout)
        if result.returncode == 0:
            name = result.stdout.strip().rsplit("/", 1)[-1]
            if name:
                return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in ("main", "master"):
        try:
            result = _run(
                ["git", "rev-parse", "--verify", "-q", f"origin/{candidate}"],
                cwd=toplevel, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate
    raise WorktreeError(f"could not determine the default branch for {toplevel!r}")


def _local_branch_exists(toplevel: str, branch: str, *, timeout: int) -> bool:
    result = _run(["git", "show-ref", "--verify", "-q", f"refs/heads/{branch}"], cwd=toplevel, timeout=timeout)
    return result.returncode == 0


def ensure_worktree(
    working_dir: str,
    task_id: str,
    title: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> WorktreeResult:
    """Provision (or reuse) an isolated worktree+branch for a CLI-routed
    board task whose resolved working directory is ``working_dir``.

    Returns unchanged behavior (``is_git=False``) when ``working_dir``
    isn't inside a git repository at all. Raises :class:`WorktreeError` on
    any git failure — callers must fail the task closed rather than fall
    back to running inside ``working_dir`` (which may be the primary
    checkout).
    """
    toplevel = repo_toplevel(working_dir, timeout=timeout)
    if toplevel is None:
        return WorktreeResult(working_dir=working_dir, is_git=False)

    worktree_dir = worktree_dir_for(toplevel, task_id)

    reused_branch = current_branch(worktree_dir, timeout=timeout) if (
        os.path.isdir(worktree_dir) and is_linked_worktree(worktree_dir)
    ) else None
    if reused_branch:
        return WorktreeResult(
            working_dir=worktree_dir, is_git=True, branch=reused_branch,
            repo_toplevel=toplevel, reused=True,
        )

    if os.path.isdir(worktree_dir):
        # A previous attempt left a directory behind that isn't a registered
        # worktree of this repo (the reuse check above ruled that out) —
        # never provision into an existing, unrecognized path.
        raise WorktreeError(f"worktree directory already exists and is not a worktree: {worktree_dir}")

    try:
        fetch = _run(["git", "fetch", "origin"], cwd=toplevel, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git fetch origin failed: {exc}") from exc
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {fetch.stderr.strip()}")

    default_branch = _detect_default_branch(toplevel, timeout=timeout)
    branch = derive_branch_name(title, task_id)

    os.makedirs(Path(worktree_dir).parent, exist_ok=True)

    if _local_branch_exists(toplevel, branch, timeout=timeout):
        add_cmd = ["git", "worktree", "add", worktree_dir, branch]
    else:
        add_cmd = ["git", "worktree", "add", "-b", branch, worktree_dir, f"origin/{default_branch}"]
    try:
        add = _run(add_cmd, cwd=toplevel, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git worktree add failed: {exc}") from exc
    if add.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {add.stderr.strip()}")

    return WorktreeResult(
        working_dir=worktree_dir, is_git=True, branch=branch,
        repo_toplevel=toplevel, reused=False,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "WorktreeError",
    "WorktreeResult",
    "WorktreeContext",
    "GIT_DISCIPLINE_INSTRUCTIONS",
    "git_discipline_text",
    "repo_toplevel",
    "is_linked_worktree",
    "current_branch",
    "describe_worktree",
    "derive_branch_name",
    "worktree_dir_for",
    "ensure_worktree",
]
