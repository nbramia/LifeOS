"""Git worktree + branch provisioning for CLI-routed agent worker sessions.

A board-dispatched Claude Code or Codex session must never run directly
inside the primary checkout — the working tree the production API server
runs from. Before such a session starts, :func:`ensure_worktree` gives it
an isolated worktree on a fresh branch, off a freshly-fetched
``origin/<default-branch>``, so the session can commit, push, and open a
pull request the way every other change in this project is made.

Provisioning is deterministic on (repository, task id): both the worktree
path and the branch name are pure functions of the two. Re-provisioning
for the same task — an operator follow-up, a worker restart mid-run, two
dispatch ticks racing the same task — asks git's own worktree registry
(``git worktree list --porcelain``) for a match on that exact path before
creating anything, and again if ``git worktree add`` itself fails, so a
race resolves to reuse rather than a spurious failure. A resumed session
reuses its worktree for a simpler reason still: the working directory
persists on the session's own execution spec, so a resume never calls
this module at all.

Every git command runs through an injectable ``Runner`` — the plain local
subprocess by default, or (when the caller names a registered remote
host) an ssh-wrapped one using the same host registry and ssh invocation
shape the executors use for a remote CLI spawn. Provisioning against an
unregistered host fails closed before any command runs.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


DEFAULT_TIMEOUT = 60  # seconds, per git subprocess call (status, rev-parse, fetch, ...)

# The project's branch-naming convention (AGENTS.md § Development Workflow)
# allows exactly these types. `agent` is not one of them.
ALLOWED_BRANCH_TYPES = ("feat", "fix", "docs", "test", "refactor", "chore")

Runner = Callable[..., subprocess.CompletedProcess]


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


# ---------------------------------------------------------------------------
# Command execution — local by default, ssh-wrapped for a registered remote
# host. Every public function below runs its git/test/mkdir commands
# through this seam so provisioning works identically regardless of which
# machine actually holds the worktree.
# ---------------------------------------------------------------------------

def _local_runner(
    cmd: list[str], *, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def make_ssh_runner(target: str, *, connect_timeout: Optional[int] = None) -> Runner:
    """A :data:`Runner` that executes every command over ssh on ``target``,
    using the same invocation shape the executors use for a remote CLI
    spawn (``remote_spawn.build_remote_launcher_argv``: ``BatchMode=yes``,
    a bounded connect timeout, no interactive prompt possible). ``cwd``
    becomes a ``cd`` prefix on the remote command — ssh has no cwd concept
    of its own.
    """
    from config.settings import settings as _settings

    resolved_connect_timeout = (
        connect_timeout if connect_timeout is not None else _settings.agent_ssh_connect_timeout
    )

    def _runner(
        cmd: list[str], *, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess:
        remote_command = shlex.join(cmd)
        if cwd:
            remote_command = f"cd {shlex.quote(cwd)} && {remote_command}"
        argv = [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={resolved_connect_timeout}",
            target, "--", remote_command,
        ]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)

    return _runner


def resolve_runner_for_host(host: Optional[str]) -> Runner:
    """The :data:`Runner` provisioning should use for ``host`` — the same
    board-facing host name (``[host:: ...]``, ``assignment.host``)
    resolves through everywhere else
    (``remote_spawn.resolve_host_target``). None/local returns the plain
    local runner. A registered remote host returns an ssh-wrapped one using
    that same resolution, so git commands run on the machine that will
    actually run the session rather than standing in for it with this
    worker's own filesystem. An unregistered host raises
    :class:`WorktreeError` before any command runs — callers must fail
    closed rather than silently falling back to a local runner that would
    operate on the wrong machine.
    """
    from api.services.agent_worker.remote_spawn import (
        HostResolutionError,
        api_host_name,
        resolve_host_target,
    )

    try:
        target = resolve_host_target(host, api_host_name())
    except HostResolutionError as exc:
        raise WorktreeError(f"cannot resolve host {host!r} for worktree provisioning: {exc}") from exc
    if target is None:
        return _local_runner
    return make_ssh_runner(target)


def _run(
    cmd: list[str], *, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
    runner: Optional[Runner] = None,
) -> subprocess.CompletedProcess:
    """Run one command through ``runner`` (default: the local subprocess),
    uniformly translating a hung or unreachable command into an ordinary
    non-zero :class:`subprocess.CompletedProcess` rather than letting
    ``TimeoutExpired``/``OSError`` escape — every caller's existing
    ``returncode != 0`` handling then reports it the same way it reports
    any other git failure, and provisioning can't crash the dispatch path
    on a slow hook or an unreachable host.
    """
    active = runner or _local_runner
    try:
        return active(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=f"timed out after {timeout}s: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(exc))


def _path_exists(path: str, *, kind: str, runner: Optional[Runner], timeout: int) -> bool:
    """True when ``path`` exists as a file (``kind='f'``) or directory
    (``kind='d'``) — checked through ``runner`` so this works identically
    for a local filesystem check and a remote one over ssh."""
    result = _run(["test", f"-{kind}", path], runner=runner, timeout=timeout)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Detection — local-only. Consulted by the CLI executors' prompt assembly,
# which only ever runs against this worker's own filesystem (the session's
# working directory as seen from wherever the executor's subprocess
# actually reads/writes it), so no runner injection is needed here.
# ---------------------------------------------------------------------------

def repo_toplevel(path: str, *, runner: Optional[Runner] = None, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """The git working-tree root containing ``path``, or None when
    ``path`` doesn't exist or isn't inside a git repository. For a linked
    worktree this is that worktree's own root, not the primary checkout's
    — each worktree is its own working-tree root even though they share
    one underlying repository."""
    if not _path_exists(path, kind="d", runner=runner, timeout=timeout):
        return None
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, runner=runner, timeout=timeout)
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
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=working_dir, timeout=timeout)
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
# routes make the same commitment to the session either way, including the
# `[CLARIFY]` convention Codex has no convention of its own for.
GIT_DISCIPLINE_INSTRUCTIONS = (
    "You are working in your own git worktree, isolated from the primary "
    "checkout, on branch `{branch}`. Commit your work as you go — don't "
    "leave it uncommitted. When you consider the task fully complete with "
    "nothing further needed from you, make sure everything is committed: "
    "the worker pushes your branch and opens a pull request once you "
    "finish, so an uncommitted result is never treated as a finished one. "
    "The pull request description is built from your final message and is "
    "publicly visible in this repository, so it must contain no personal "
    "data, secrets, or credentials — a plain technical summary of the "
    "change. If you need to ask a question before the task is done, end "
    "your final message with `[CLARIFY] <question>` and nothing after it; "
    "the worker treats that as a paused task awaiting the operator's "
    "answer, not a finished one, and commits/pushes what you have so far "
    "without opening a pull request."
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

_TYPE_PREFIX_RE = re.compile(r"^(feat|fix|docs|test|refactor|chore)[:/\s-]+(.+)$", re.IGNORECASE)

# A leading keyword that implies an allowed type when the title carries no
# explicit `type: ...` / `type/...` prefix. Matched against the title's
# first word only — a light heuristic, not a scan of the whole title, so
# it doesn't misfire on a word that merely appears later ("...then test
# it"). Anything unmatched (most ordinary titles) defaults to `feat`.
_LEADING_KEYWORD_TYPE = {
    "fix": "fix", "fixes": "fix", "fixed": "fix", "bug": "fix", "bugfix": "fix",
    "repair": "fix", "resolve": "fix", "resolves": "fix",
    "doc": "docs", "docs": "docs", "document": "docs", "documentation": "docs",
    "test": "test", "tests": "test", "testing": "test",
    "refactor": "refactor", "cleanup": "refactor", "clean": "refactor",
    "reorganize": "refactor", "simplify": "refactor",
    "chore": "chore", "bump": "chore", "upgrade": "chore", "update": "chore",
}


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].strip("-") or "task"


def derive_branch_name(title: str, task_id: str) -> str:
    """``<type>/<slug>-<suffix>`` per the project's branch-naming
    convention (AGENTS.md § Development Workflow) — ``type`` is always one
    of :data:`ALLOWED_BRANCH_TYPES`, never a made-up value.

    ``type`` is read off a leading conventional prefix in the title
    (``fix: ...``, ``feat/...``) when present; otherwise a leading keyword
    in the title (``"Fix the printer"``, ``"Document the API"``) maps to
    an allowed type via :data:`_LEADING_KEYWORD_TYPE`; failing both, it
    defaults to ``feat``. The task id's own tail is appended so two
    similarly-titled tasks never collide on the same branch.
    """
    title = (title or "").strip()
    match = _TYPE_PREFIX_RE.match(title)
    if match:
        branch_type, rest = match.group(1).lower(), match.group(2)
    else:
        first_word = re.match(r"^([A-Za-z]+)", title)
        branch_type = _LEADING_KEYWORD_TYPE.get(first_word.group(1).lower(), "feat") if first_word else "feat"
        rest = title
    suffix = re.sub(r"[^a-z0-9]", "", task_id.lower())[-8:] or "task"
    return f"{branch_type}/{_slugify(rest)}-{suffix}"


def worktree_dir_for(repo_toplevel_path: str, task_id: str) -> str:
    """Deterministic sibling worktree directory for one task.

    Deterministic on (repo, task_id) so re-provisioning for the same task
    — an operator follow-up, a worker restart mid-provisioning, a racing
    duplicate dispatch tick — finds the same directory instead of creating
    a new one.
    """
    top = Path(repo_toplevel_path)
    suffix = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)
    return str(top.parent / f"{top.name}-wt-agent-{suffix}")


def _detect_default_branch(toplevel: str, *, runner: Optional[Runner], timeout: int) -> str:
    result = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=toplevel, runner=runner, timeout=timeout)
    if result.returncode == 0:
        name = result.stdout.strip().rsplit("/", 1)[-1]
        if name:
            return name
    for candidate in ("main", "master"):
        result = _run(
            ["git", "rev-parse", "--verify", "-q", f"origin/{candidate}"],
            cwd=toplevel, runner=runner, timeout=timeout,
        )
        if result.returncode == 0:
            return candidate
    raise WorktreeError(f"could not determine the default branch for {toplevel!r}")


def _local_branch_exists(toplevel: str, branch: str, *, runner: Optional[Runner], timeout: int) -> bool:
    result = _run(
        ["git", "show-ref", "--verify", "-q", f"refs/heads/{branch}"],
        cwd=toplevel, runner=runner, timeout=timeout,
    )
    return result.returncode == 0


def _registered_worktree_branch(
    toplevel: str, worktree_dir: str, *, runner: Optional[Runner], timeout: int,
) -> Optional[str]:
    """The branch git itself has registered for ``worktree_dir`` under
    ``toplevel``'s ``git worktree list --porcelain``, or None when no
    worktree is registered at that exact path (or it's detached, or the
    listing itself failed).

    Ground truth is git's own registry, not a bare directory-exists check
    — a directory that merely looks like a worktree (a stale leftover, an
    unrelated repo) must never be reused. Called both as the primary reuse
    check and, after a `git worktree add` failure, as a race-tolerance
    probe: two dispatch ticks racing the same task can both miss the
    initial check and then have one `add` fail on the ref the other just
    created — re-reading the registry catches that instead of failing a
    task that already has a valid worktree.
    """
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=toplevel, runner=runner, timeout=timeout)
    if result.returncode != 0:
        return None
    target = os.path.normpath(worktree_dir)
    for block in result.stdout.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        if os.path.normpath(lines[0][len("worktree "):]) != target:
            continue
        for line in lines[1:]:
            if line.startswith("branch "):
                ref = line[len("branch "):].strip()
                return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
        return None  # registered but detached — nothing to reuse as a branch
    return None


def ensure_worktree(
    working_dir: str,
    task_id: str,
    title: str,
    *,
    host: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> WorktreeResult:
    """Provision (or reuse) an isolated worktree+branch for a CLI-routed
    board task whose resolved working directory is ``working_dir``.

    ``host`` is the board-facing host name (``[host:: ...]``,
    ``assignment.host``) the session will actually run on. None/local runs
    every git command through the plain local subprocess; a registered
    remote host runs them over ssh instead, so provisioning always happens
    on the machine that will actually run the session — never this
    worker's own filesystem standing in for a remote one. An unregistered
    host raises :class:`WorktreeError` before any command runs.

    Returns unchanged behavior (``is_git=False``) when ``working_dir``
    isn't inside a git repository at all. Raises :class:`WorktreeError` on
    any other failure — callers must fail the task closed rather than fall
    back to running inside ``working_dir`` (which may be the primary
    checkout).
    """
    runner = resolve_runner_for_host(host)

    toplevel = repo_toplevel(working_dir, runner=runner, timeout=timeout)
    if toplevel is None:
        return WorktreeResult(working_dir=working_dir, is_git=False)

    worktree_dir = worktree_dir_for(toplevel, task_id)
    branch = derive_branch_name(title, task_id)

    reused_branch = _registered_worktree_branch(toplevel, worktree_dir, runner=runner, timeout=timeout)
    if reused_branch:
        return WorktreeResult(
            working_dir=worktree_dir, is_git=True, branch=reused_branch,
            repo_toplevel=toplevel, reused=True,
        )

    fetch = _run(["git", "fetch", "origin"], cwd=toplevel, runner=runner, timeout=timeout)
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {fetch.stderr.strip()}")

    default_branch = _detect_default_branch(toplevel, runner=runner, timeout=timeout)

    _run(["mkdir", "-p", str(Path(worktree_dir).parent)], runner=runner, timeout=timeout)

    if _local_branch_exists(toplevel, branch, runner=runner, timeout=timeout):
        add_cmd = ["git", "worktree", "add", worktree_dir, branch]
    else:
        add_cmd = ["git", "worktree", "add", "-b", branch, worktree_dir, f"origin/{default_branch}"]
    add = _run(add_cmd, cwd=toplevel, runner=runner, timeout=timeout)
    if add.returncode != 0:
        # Race tolerance: re-read the registry before giving up — see
        # `_registered_worktree_branch`'s docstring.
        raced_branch = _registered_worktree_branch(toplevel, worktree_dir, runner=runner, timeout=timeout)
        if raced_branch:
            return WorktreeResult(
                working_dir=worktree_dir, is_git=True, branch=raced_branch,
                repo_toplevel=toplevel, reused=True,
            )
        raise WorktreeError(f"git worktree add failed: {add.stderr.strip()}")

    return WorktreeResult(
        working_dir=worktree_dir, is_git=True, branch=branch,
        repo_toplevel=toplevel, reused=False,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "ALLOWED_BRANCH_TYPES",
    "Runner",
    "WorktreeError",
    "WorktreeResult",
    "WorktreeContext",
    "GIT_DISCIPLINE_INSTRUCTIONS",
    "git_discipline_text",
    "make_ssh_runner",
    "resolve_runner_for_host",
    "repo_toplevel",
    "is_linked_worktree",
    "current_branch",
    "describe_worktree",
    "derive_branch_name",
    "worktree_dir_for",
    "ensure_worktree",
]
