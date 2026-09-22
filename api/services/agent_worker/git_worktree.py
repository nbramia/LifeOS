"""Git worktree + branch provisioning for CLI-routed agent worker sessions.

A board-dispatched Claude Code or Codex session must never run directly
inside the primary checkout — the working tree the production API server
runs from. Before such a session starts, :func:`ensure_worktree` gives it
an isolated worktree on a fresh branch, off a freshly-fetched
``origin/<default-branch>`` — or, for a coding child of a project with a
recorded integration branch, off that branch instead, lazily creating it
on origin first if it doesn't exist yet — so the session can commit, push,
and open a pull request the way every other change in this project is
made.

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

Every git/gh command — provisioning's and completion's alike — runs
through an injectable ``Runner`` — the plain local subprocess by default,
or (when the caller names a registered remote host) an ssh-wrapped one
using the same host registry and ssh invocation shape the executors use
for a remote CLI spawn. Provisioning and finalization against an
unregistered host both fail closed before any command runs.

:func:`finalize_worktree_session` is the completion-time counterpart: it
commits any changes the session itself left uncommitted (a safety net),
pushes the branch, and — when the caller is finalizing a fully-complete
session — opens a pull request (or reuses one that already exists for the
branch), running every step through the same resolved runner a remote-host
session was provisioned with. Both functions no-op on a directory that
isn't a linked git worktree, so a non-git-repo task or an operator-spawned
session without a worktree is unaffected.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from api.services.secret_redaction import scrub_secrets


DEFAULT_TIMEOUT = 60          # seconds, per git/gh subprocess call (status, rev-parse, fetch, ...)
COMMIT_PUSH_TIMEOUT = 300     # seconds — commit/push may run pre-commit/pre-push hooks

SAFETY_NET_COMMIT_MESSAGE = "chore: worker safety-net commit for uncommitted session changes"

# The project's branch-naming convention (AGENTS.md § Development Workflow)
# allows exactly these types. `agent` is not one of them.
ALLOWED_BRANCH_TYPES = ("feat", "fix", "docs", "test", "refactor", "chore")

MAX_PR_BODY_CHARS = 4000
WORKER_MARKER = "lifeos-agent-worktree.json"

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
    at all, so behavior matches a caller that never provisions a worktree.
    """

    working_dir: str
    is_git: bool
    branch: Optional[str] = None
    repo_toplevel: Optional[str] = None
    reused: bool = False


@dataclass(frozen=True)
class WorktreeContext:
    """A session's own worktree/branch, detected from its working directory.

    Decides whether the git-discipline prompt block applies to a
    fresh CLI session — derived straight from the filesystem/git state
    rather than threaded through the dispatch payload, so it stays correct
    regardless of how the working directory was set.
    """

    branch: str
    repo_toplevel: str


@dataclass(frozen=True)
class FinalizeResult:
    """Outcome of running the worker's own git discipline at session end.

    ``applicable`` is False only when there was never a worktree to act on
    in the first place — no ``working_dir`` at all, or a directory that
    isn't a linked git worktree. A ``working_dir`` that names a real,
    provisioned worktree always gets ``applicable=True`` from here on,
    even when finalization itself fails (an unresolvable host, a git/`gh`
    failure) — that failure lands in ``error`` instead, never silently
    reported as "nothing to do".
    """

    applicable: bool
    branch: Optional[str] = None
    pushed: bool = False
    safety_net_committed: bool = False
    pr_url: Optional[str] = None
    pr_opened: bool = False
    nothing_to_push: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of an ownership-checked worktree cleanup attempt."""

    removed: bool
    applicable: bool = True
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Command execution — local by default, ssh-wrapped for a registered remote
# host. Every public function below runs its git/test/mkdir commands
# through this seam so provisioning works identically regardless of which
# machine actually holds the worktree.
# ---------------------------------------------------------------------------

def _local_runner(
    cmd: list[str], *, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, input=input,
    )


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
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        remote_command = shlex.join(cmd)
        if cwd:
            remote_command = f"cd {shlex.quote(cwd)} && {remote_command}"
        argv = [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={resolved_connect_timeout}",
            target, "--", remote_command,
        ]
        # ssh forwards its own stdin to the remote command's stdin by
        # default, so `input` (the gh pr body, delivered via `--body-file
        # -`) reaches the remote `gh` process exactly like a local one.
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, input=input)

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
    runner: Optional[Runner] = None, input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run one command through ``runner`` (default: the local subprocess),
    uniformly translating a hung or unreachable command into an ordinary
    non-zero :class:`subprocess.CompletedProcess` rather than letting
    ``TimeoutExpired``/``OSError`` escape — every caller's existing
    ``returncode != 0`` handling then reports it the same way it reports
    any other git failure, and provisioning can't crash the dispatch path
    on a slow hook or an unreachable host.

    ``input`` is only forwarded when given (most commands need no stdin);
    the two built-in runners and any test double that doesn't declare an
    ``input`` parameter stay unaffected for every call that omits it.
    """
    active = runner or _local_runner
    kwargs: dict = {"cwd": cwd, "timeout": timeout}
    if input is not None:
        kwargs["input"] = input
    try:
        return active(cmd, **kwargs)
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


def is_linked_worktree(working_dir: str, *, runner: Optional[Runner] = None, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """True when ``working_dir`` is a linked git worktree — its own
    ``.git`` is a file pointing at the shared repo, never a directory the
    way the primary checkout's is.

    ``runner`` defaults to the local filesystem (a plain ``os.path.isfile``
    check — cheaper than a subprocess for the common local case); callers
    that need this checked on a remote host pass a resolved remote
    ``runner``, which routes the check through ``test -f`` instead.
    """
    if runner is None:
        return os.path.isfile(os.path.join(working_dir, ".git"))
    return _path_exists(f"{working_dir.rstrip('/')}/.git", kind="f", runner=runner, timeout=timeout)


def current_branch(working_dir: str, *, runner: Optional[Runner] = None, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """The branch checked out in ``working_dir``, or None on any git
    failure (detached HEAD, not a repo, timeout)."""
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=working_dir, runner=runner, timeout=timeout)
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


def _remote_branch_exists(toplevel: str, branch: str, *, runner: Optional[Runner], timeout: int) -> bool:
    """True when ``origin/<branch>`` is present in this checkout's own
    remote-tracking refs (checked after a fetch, so this reflects origin's
    current state, not a stale local view)."""
    result = _run(
        ["git", "rev-parse", "--verify", "-q", f"origin/{branch}"],
        cwd=toplevel, runner=runner, timeout=timeout,
    )
    return result.returncode == 0


def _ensure_remote_branch(
    toplevel: str, branch: str, default_branch: str, *, runner: Optional[Runner], timeout: int,
) -> None:
    """Make sure ``origin/<branch>`` exists — a project's integration
    branch — creating it off the current ``origin/<default_branch>`` when
    it doesn't. Two callers racing to create the same branch at nearly the
    same moment both succeed: the push itself is allowed to fail (the ref
    already exists, or a concurrent push wins the race) — only "the ref is
    still missing after a re-fetch" is treated as a real failure, since
    that's the only outcome a genuine push-rights or connectivity problem
    produces.
    """
    if _remote_branch_exists(toplevel, branch, runner=runner, timeout=timeout):
        return
    _run(
        ["git", "push", "origin", f"origin/{default_branch}:refs/heads/{branch}"],
        cwd=toplevel, runner=runner, timeout=timeout,
    )
    fetch = _run(["git", "fetch", "origin"], cwd=toplevel, runner=runner, timeout=timeout)
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {fetch.stderr.strip()}")
    if not _remote_branch_exists(toplevel, branch, runner=runner, timeout=timeout):
        raise WorktreeError(f"could not create or find integration branch {branch!r} on origin")


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


def _git_dir(working_dir: str, *, runner: Optional[Runner], timeout: int) -> Optional[str]:
    result = _run(["git", "rev-parse", "--absolute-git-dir"], cwd=working_dir, runner=runner, timeout=timeout)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def _write_worker_marker(
    working_dir: str, marker: dict, *, runner: Optional[Runner], timeout: int,
) -> None:
    git_dir = _git_dir(working_dir, runner=runner, timeout=timeout)
    if not git_dir:
        raise WorktreeError("could not resolve linked worktree git directory")
    payload = json.dumps(marker, sort_keys=True) + "\n"
    result = _run(
        [
            "python3", "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
            f"{git_dir.rstrip('/')}/{WORKER_MARKER}", payload,
        ],
        runner=runner, timeout=timeout,
    )
    if result.returncode != 0:
        raise WorktreeError(f"could not write worktree ownership marker: {result.stderr.strip()}")


def _read_worker_marker(
    working_dir: str, *, runner: Optional[Runner], timeout: int,
) -> Optional[dict]:
    git_dir = _git_dir(working_dir, runner=runner, timeout=timeout)
    if not git_dir:
        return None
    result = _run(
        ["cat", f"{git_dir.rstrip('/')}/{WORKER_MARKER}"], runner=runner, timeout=timeout,
    )
    if result.returncode != 0:
        return None
    try:
        marker = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def ensure_worktree(
    working_dir: str,
    task_id: str,
    title: str,
    *,
    host: Optional[str] = None,
    base_branch: Optional[str] = None,
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

    ``base_branch``, when given (a project's recorded integration branch),
    is used as the worktree's base instead of the repository's detected
    default branch: on fresh provisioning, ``origin/<base_branch>`` is
    created off the current default branch first if origin doesn't already
    have it — concurrent callers racing to create it never fail, since only
    a ref still missing after a re-fetch counts as a real error — and the
    branch is recorded in the worktree's ownership marker so a later
    finalize (and any reuse of this same worktree) can recover it. None
    (the default) preserves today's behavior exactly: the worktree is based
    on the detected default branch and nothing is recorded.

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
        marker = _read_worker_marker(worktree_dir, runner=runner, timeout=timeout)
        if not marker or marker.get("task_id") != task_id:
            raise WorktreeError(
                f"refusing to reuse unowned worktree at {worktree_dir!r}"
            )
        return WorktreeResult(
            working_dir=worktree_dir, is_git=True, branch=reused_branch,
            repo_toplevel=toplevel, reused=True,
        )

    fetch = _run(["git", "fetch", "origin"], cwd=toplevel, runner=runner, timeout=timeout)
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {fetch.stderr.strip()}")

    default_branch = _detect_default_branch(toplevel, runner=runner, timeout=timeout)

    if base_branch:
        _ensure_remote_branch(toplevel, base_branch, default_branch, runner=runner, timeout=timeout)
    effective_base = base_branch or default_branch

    _run(["mkdir", "-p", str(Path(worktree_dir).parent)], runner=runner, timeout=timeout)

    if _local_branch_exists(toplevel, branch, runner=runner, timeout=timeout):
        add_cmd = ["git", "worktree", "add", worktree_dir, branch]
    else:
        add_cmd = ["git", "worktree", "add", "-b", branch, worktree_dir, f"origin/{effective_base}"]
    add = _run(add_cmd, cwd=toplevel, runner=runner, timeout=timeout)
    if add.returncode != 0:
        # Race tolerance: re-read the registry before giving up — see
        # `_registered_worktree_branch`'s docstring.
        raced_branch = _registered_worktree_branch(toplevel, worktree_dir, runner=runner, timeout=timeout)
        if raced_branch:
            marker = _read_worker_marker(worktree_dir, runner=runner, timeout=timeout)
            if not marker or marker.get("task_id") != task_id:
                raise WorktreeError(
                    f"refusing to reuse unowned worktree at {worktree_dir!r}"
                )
            return WorktreeResult(
                working_dir=worktree_dir, is_git=True, branch=raced_branch,
                repo_toplevel=toplevel, reused=True,
            )
        raise WorktreeError(f"git worktree add failed: {add.stderr.strip()}")

    _write_worker_marker(
        worktree_dir,
        {
            "version": 1,
            "task_id": task_id,
            "repo_toplevel": toplevel,
            "worktree_dir": worktree_dir,
            "branch": branch,
            "host": host,
            "base_branch": base_branch,
            "state": "ready",
            "created_at": int(time.time()),
        },
        runner=runner,
        timeout=timeout,
    )

    return WorktreeResult(
        working_dir=worktree_dir, is_git=True, branch=branch,
        repo_toplevel=toplevel, reused=False,
    )


# ---------------------------------------------------------------------------
# Completion — safety-net commit, push, pull request. Runs through the same
# resolved runner provisioning used, so a remote-host session's worktree is
# finalized on the host that actually holds it, not silently skipped.
# ---------------------------------------------------------------------------

def _has_uncommitted_changes(working_dir: str, *, runner: Optional[Runner], timeout: int) -> bool:
    """True when `git status --porcelain` reports anything. Raises
    :class:`WorktreeError` when the status check itself fails or times out
    — a failed check must never read as "the tree is clean"; a caller that
    can't determine whether there's uncommitted work must not proceed as
    though there isn't any."""
    result = _run(["git", "status", "--porcelain"], cwd=working_dir, runner=runner, timeout=timeout)
    if result.returncode != 0:
        raise WorktreeError(f"git status failed: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def _commits_ahead(
    working_dir: str, base_ref: str, branch: str, *, runner: Optional[Runner], timeout: int,
) -> Optional[int]:
    result = _run(
        ["git", "rev-list", "--count", f"{base_ref}..{branch}"], cwd=working_dir, runner=runner, timeout=timeout,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _commit_log(working_dir: str, base_ref: str, branch: str, *, runner: Optional[Runner], timeout: int) -> str:
    result = _run(
        ["git", "log", "--oneline", f"{base_ref}..{branch}"], cwd=working_dir, runner=runner, timeout=timeout,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _bounded(text: str, *, max_chars: int = MAX_PR_BODY_CHARS) -> str:
    """``text`` capped at ``max_chars``, with a truncation note appended
    when it was cut — never silently drops the fact that content is
    missing."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n\n…(truncated — {len(text)} chars total)"


# Redacts obvious secret shapes from a PR body before it's published
# publicly — defense in depth alongside the git-discipline prompt's own
# instruction that the body must carry no personal data or secrets. This
# is a fixed set of widely-recognized credential *shapes*, not a personal-
# data scanner — no NLP, no attempt at names/addresses/etc. Order matters:
# a specific prefix (bot token, `sk-`, `ghp_`, ...) is redacted whole
# before the generic long-hex/base64 catch-alls run, so a matched prefix's
# own body isn't then reported twice under a vaguer label.
def _build_pr_body(
    *, card_title: str, summary: str, working_dir: str, base: str, branch: str,
    runner: Optional[Runner], timeout: int,
) -> str:
    """The pull request description: the card title, a bounded summary of
    the session's own completion message, and the branch's commit list —
    in that order, so the reader sees what the card asked for before the
    session's own account of what it did. The summary is untrusted model
    output: scrubbed for obvious secret shapes and length-bounded here —
    the git-discipline prompt is what tells the session not to put
    personal data in it in the first place, which this can't detect."""
    sections = [f"## {card_title}" if card_title else "## Agent task"]
    if summary:
        sections.append(summary.strip())
    commit_log = _commit_log(working_dir, base, branch, runner=runner, timeout=timeout)
    if commit_log:
        sections.append("### Commits\n```\n" + commit_log[:1500] + "\n```")
    return _bounded(scrub_secrets("\n\n".join(sections)))


def _find_existing_pr(branch: str, *, cwd: str, runner: Runner, timeout: int) -> Optional[str]:
    result = _run(["gh", "pr", "view", branch, "--json", "url"], cwd=cwd, runner=runner, timeout=timeout)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


def _create_pr(
    branch: str, base: str, title: str, body: str, *, cwd: str, runner: Runner, timeout: int,
) -> str:
    """``gh pr create``, with the body delivered over stdin (``--body-file
    -``) rather than a temp file or argv — works identically whether
    ``runner`` is local or an ssh-wrapped remote one (ssh forwards local
    stdin to the remote command), and never leaves a body file to clean up
    on either side."""
    cmd = [
        "gh", "pr", "create", "--base", base, "--head", branch,
        "--title", title or branch, "--body-file", "-",
    ]
    result = _run(cmd, cwd=cwd, runner=runner, timeout=timeout, input=body or "")
    if result.returncode != 0:
        raise WorktreeError(f"gh pr create failed: {(result.stderr or '').strip()}")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise WorktreeError("gh pr create returned no URL")
    return lines[-1]


def finalize_worktree_session(
    working_dir: Optional[str],
    *,
    open_pr: bool,
    pr_title: str = "",
    pr_body: str = "",
    base_branch: Optional[str] = None,
    host: Optional[str] = None,
    gh_runner: Optional[Runner] = None,
    runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT,
    commit_push_timeout: int = COMMIT_PUSH_TIMEOUT,
) -> FinalizeResult:
    """Run the worker's own git discipline at one session's completion.

    ``host`` is the same board-facing host name provisioning used
    (``resolve_runner_for_host``) — every git/`gh` operation below runs
    through that same resolved runner, so a remote-host session's worktree
    is finalized on the host that actually holds it. An unresolvable host
    (a session pinned to a host that isn't registered) returns
    ``applicable=True`` with ``error`` set — never a silent
    ``applicable=False``, which would read as "there was nothing to
    finalize" when there really was.

    Always (once a linked worktree is confirmed): commit any changes the
    session itself left uncommitted (the safety net — ``git add -A``
    honors ``.gitignore``, never ``--no-verify``), then push the branch.
    Both run with ``commit_push_timeout`` (default 5 minutes) rather than
    ``timeout`` — a pre-commit/pre-push hook can legitimately take longer
    than a plain status/rev-parse call. A failed *status* check itself
    (not just add/commit/push) is also reported as an error rather than
    treated as "nothing to commit".

    When ``open_pr`` is True and the push succeeded: open a pull request
    against a base branch — ``base_branch`` when the caller passes one,
    else the base branch recorded in the worktree's own ownership marker
    (set by `ensure_worktree`'s `base_branch`, e.g. a project's integration
    branch) when there is one, else the repository's detected default
    branch — whose body leads with the card title, a secret-scrubbed and
    bounded copy of ``pr_body``
    (the session's own completion summary), and the branch's commit list —
    reusing one that already exists for the branch, or reporting
    ``nothing_to_push`` when the branch carries no commits beyond its base
    (no empty PR). ``gh_runner`` overrides the resolved runner for `gh`
    calls specifically (a test seam); production leaves it unset so `gh`
    runs through the same runner as everything else.

    No-ops (``applicable=False``) only when there's no ``working_dir`` at
    all or it isn't a linked git worktree — a non-git-repo task or a
    worktree-less operator spawn is unaffected.
    """
    if not working_dir:
        return FinalizeResult(applicable=False)

    try:
        runner = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return FinalizeResult(applicable=True, error=str(exc))

    if not is_linked_worktree(working_dir, runner=runner, timeout=timeout):
        return FinalizeResult(applicable=False)

    branch = current_branch(working_dir, runner=runner, timeout=timeout)
    if not branch:
        return FinalizeResult(applicable=False)

    try:
        dirty = _has_uncommitted_changes(working_dir, runner=runner, timeout=timeout)
    except WorktreeError as exc:
        return FinalizeResult(applicable=True, branch=branch, error=str(exc))

    safety_net = False
    if dirty:
        add = _run(["git", "add", "-A"], cwd=working_dir, runner=runner, timeout=commit_push_timeout)
        if add.returncode != 0:
            return FinalizeResult(applicable=True, branch=branch, error=f"git add -A failed: {add.stderr.strip()}")
        commit = _run(
            ["git", "commit", "-m", SAFETY_NET_COMMIT_MESSAGE],
            cwd=working_dir, runner=runner, timeout=commit_push_timeout,
        )
        if commit.returncode != 0:
            return FinalizeResult(applicable=True, branch=branch, error=f"safety-net commit failed: {commit.stderr.strip()}")
        safety_net = True

    push = _run(["git", "push", "-u", "origin", branch], cwd=working_dir, runner=runner, timeout=commit_push_timeout)
    if push.returncode != 0:
        return FinalizeResult(
            applicable=True, branch=branch, safety_net_committed=safety_net,
            error=f"git push failed: {push.stderr.strip()}",
        )

    result = FinalizeResult(applicable=True, branch=branch, pushed=True, safety_net_committed=safety_net)
    if not open_pr:
        return result

    toplevel = repo_toplevel(working_dir, runner=runner, timeout=timeout)
    marker = _read_worker_marker(working_dir, runner=runner, timeout=timeout)
    recorded_base = marker.get("base_branch") if marker else None
    try:
        base = (
            base_branch
            or recorded_base
            or (_detect_default_branch(toplevel, runner=runner, timeout=timeout) if toplevel else None)
        )
    except WorktreeError as exc:
        return dataclasses.replace(result, error=str(exc))
    if not base:
        return dataclasses.replace(result, error="could not determine the base branch for the pull request")

    ahead = _commits_ahead(working_dir, f"origin/{base}", branch, runner=runner, timeout=timeout)
    if ahead == 0:
        return dataclasses.replace(result, nothing_to_push=True)

    effective_gh_runner = gh_runner or runner
    existing = _find_existing_pr(branch, cwd=working_dir, runner=effective_gh_runner, timeout=timeout)
    if existing:
        return dataclasses.replace(result, pr_url=existing, pr_opened=False)

    body = _build_pr_body(
        card_title=pr_title, summary=pr_body, working_dir=working_dir,
        base=f"origin/{base}", branch=branch, runner=runner, timeout=timeout,
    )
    try:
        pr_url = _create_pr(branch, base, pr_title, body, cwd=working_dir, runner=effective_gh_runner, timeout=timeout)
    except WorktreeError as exc:
        return dataclasses.replace(result, error=str(exc))
    return dataclasses.replace(result, pr_url=pr_url, pr_opened=True)


def pull_request_state(
    working_dir: str, *, host: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
    runner: Optional[Runner] = None,
) -> Optional[str]:
    """Return the branch PR state (OPEN/MERGED/CLOSED), or None on a miss/failure."""
    active = runner or resolve_runner_for_host(host)
    branch = current_branch(working_dir, runner=active, timeout=timeout)
    if not branch:
        return None
    result = _run(
        ["gh", "pr", "view", branch, "--json", "state", "--jq", ".state"],
        cwd=working_dir, runner=active, timeout=timeout,
    )
    state = result.stdout.strip().upper() if result.returncode == 0 else ""
    return state if state in {"OPEN", "MERGED", "CLOSED"} else None


_PR_URL_RE = re.compile(r"^https?://[^/]+/([^/]+/[^/]+)/pull/\d+/?$")


def repo_slug_from_pr_url(pr_url: str) -> Optional[str]:
    """``"owner/repo"`` parsed out of a full GitHub pull request URL, or
    None when it doesn't look like one. The only way the functions below
    learn which repository to call `gh api` against without a local git
    checkout to read a remote from — a project owner's own session (the
    caller of `merge_pull_request`/`repo_compare_ahead_by`) has no
    worktree of its own, so the repository comes from a child's already-
    recorded pull request URL instead."""
    match = _PR_URL_RE.match((pr_url or "").strip())
    return match.group(1) if match else None


def pr_base_and_state(
    pr_url: str, *, host: Optional[str] = None, runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Optional[dict], Optional[str]]:
    """``{"baseRefName": ..., "state": ...}`` for one pull request,
    identified by its full URL rather than a branch name — works with no
    local checkout at all, since `gh pr view <url>` resolves the
    repository from the URL itself. Returns ``(None, error)`` on any
    failure: an unresolvable host, a missing `gh` binary (surfaced by
    `_run` as a non-zero return carrying the `OSError` text), or `gh`
    itself refusing the lookup."""
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return None, str(exc)
    result = _run(
        ["gh", "pr", "view", pr_url, "--json", "baseRefName,state"],
        runner=active, timeout=timeout,
    )
    if result.returncode != 0:
        return None, (result.stderr or f"gh pr view failed (exit {result.returncode})").strip()
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None, "gh pr view returned invalid JSON"
    if not isinstance(data, dict):
        return None, "gh pr view returned an unexpected response shape"
    return data, None


def merge_pull_request(
    pr_url: str, *, host: Optional[str] = None, runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, Optional[str]]:
    """``gh pr merge <url> --merge``, by full URL so no local checkout is
    needed. ``(True, None)`` on success; ``(False, error)`` otherwise —
    an unresolvable host, a missing `gh` binary, or `gh` itself refusing
    the merge (conflicts, a required check still red, branch protection).
    Never falls back to any other merge method."""
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return False, str(exc)
    result = _run(["gh", "pr", "merge", pr_url, "--merge"], runner=active, timeout=timeout)
    if result.returncode != 0:
        return False, (result.stderr or f"gh pr merge failed (exit {result.returncode})").strip()
    return True, None


def repo_default_branch(
    repo_slug: str, *, host: Optional[str] = None, runner: Optional[Runner] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[str]]:
    """The repository's default branch name, via `gh api` rather than a
    local `origin/HEAD` read (`_detect_default_branch`) — there is no
    local checkout for the completion-time "is the integration branch
    merged" check this feeds. ``(None, error)`` on any failure."""
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return None, str(exc)
    result = _run(
        ["gh", "api", f"repos/{repo_slug}", "--jq", ".default_branch"],
        runner=active, timeout=timeout,
    )
    if result.returncode != 0:
        return None, (result.stderr or f"gh api repos/{repo_slug} failed (exit {result.returncode})").strip()
    branch = result.stdout.strip()
    if not branch:
        return None, f"gh api repos/{repo_slug} returned an empty default branch"
    return branch, None


def repo_compare_ahead_by(
    repo_slug: str, base: str, head: str, *, host: Optional[str] = None,
    runner: Optional[Runner] = None, timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Optional[int], Optional[str]]:
    """How many commits ``head`` is ahead of ``base`` in the remote
    repository (`gh api repos/{slug}/compare/{base}...{head}`'s own
    ``ahead_by``) — a generic, repository-agnostic "is this branch fully
    merged" check with no PR of its own involved on either side.
    ``(None, error)`` on any failure, including a missing `gh` binary."""
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return None, str(exc)
    result = _run(
        ["gh", "api", f"repos/{repo_slug}/compare/{base}...{head}", "--jq", ".ahead_by"],
        runner=active, timeout=timeout,
    )
    if result.returncode != 0:
        return None, (result.stderr or f"gh api compare failed (exit {result.returncode})").strip()
    try:
        return int(result.stdout.strip()), None
    except ValueError:
        return None, "gh api compare returned a non-numeric ahead_by"


def list_worker_worktrees(
    repo: str, *, host: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
    runner: Optional[Runner] = None,
) -> list[str]:
    """List marker-owned worker worktrees registered to one repository."""
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError:
        return []
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo, runner=active, timeout=timeout)
    if result.returncode != 0:
        return []
    blocks = result.stdout.split("\n\n")
    first_repo_line = blocks[0].splitlines()[:1] if blocks else []
    if not first_repo_line or not first_repo_line[0].startswith("worktree "):
        return []
    primary = first_repo_line[0].removeprefix("worktree ")
    owned: list[str] = []
    for block in blocks:
        first = block.splitlines()[:1]
        if not first or not first[0].startswith("worktree "):
            continue
        path = first[0].removeprefix("worktree ")
        if "-wt-agent-" not in Path(path).name:
            continue
        marker = _read_worker_marker(path, runner=active, timeout=timeout)
        if (
            marker
            and os.path.normpath(str(marker.get("worktree_dir", ""))) == os.path.normpath(path)
            and os.path.normpath(str(marker.get("repo_toplevel", ""))) == os.path.normpath(primary)
        ):
            owned.append(path)
    return owned


def remove_worker_worktree(
    working_dir: str,
    *,
    host: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    commit_push_timeout: int = COMMIT_PUSH_TIMEOUT,
    runner: Optional[Runner] = None,
) -> CleanupResult:
    """Safely remove one worker-owned worktree without deleting its branch.

    Ownership requires both the deterministic path pattern and a valid marker
    stored in the linked worktree's git directory. The marker is changed to
    ``cleaning`` before finalization, making an interrupted attempt explicit
    and retryable. Finalization must commit and push successfully before git
    is allowed to remove the worktree.
    """
    try:
        active = runner or resolve_runner_for_host(host)
    except WorktreeError as exc:
        return CleanupResult(removed=False, error=str(exc))
    if "-wt-agent-" not in Path(working_dir).name:
        return CleanupResult(removed=False, applicable=False, error="path is not worker-managed")
    if not is_linked_worktree(working_dir, runner=active, timeout=timeout):
        return CleanupResult(removed=False, applicable=False, error="not a linked worktree")
    marker = _read_worker_marker(working_dir, runner=active, timeout=timeout)
    if not marker or os.path.normpath(str(marker.get("worktree_dir", ""))) != os.path.normpath(working_dir):
        return CleanupResult(removed=False, applicable=False, error="worker ownership marker missing or invalid")
    repo = str(marker.get("repo_toplevel") or "")
    if not repo or os.path.normpath(repo) == os.path.normpath(working_dir):
        return CleanupResult(removed=False, applicable=False, error="refusing primary checkout")
    marker["state"] = "cleaning"
    marker["cleanup_started_at"] = int(time.time())
    try:
        _write_worker_marker(working_dir, marker, runner=active, timeout=timeout)
    except WorktreeError as exc:
        return CleanupResult(removed=False, error=str(exc))
    finalized = finalize_worktree_session(
        working_dir, open_pr=False, host=host, timeout=timeout,
        commit_push_timeout=commit_push_timeout, runner=active,
    )
    if not finalized.applicable or finalized.error or not finalized.pushed:
        return CleanupResult(removed=False, error=finalized.error or "worktree finalization did not push")
    removed = _run(
        ["git", "worktree", "remove", "--force", working_dir], cwd=repo,
        runner=active, timeout=commit_push_timeout,
    )
    if removed.returncode != 0:
        return CleanupResult(removed=False, error=f"git worktree remove failed: {removed.stderr.strip()}")
    pruned = _run(["git", "worktree", "prune"], cwd=repo, runner=active, timeout=timeout)
    if pruned.returncode != 0:
        return CleanupResult(removed=True, error=f"git worktree prune failed: {pruned.stderr.strip()}")
    return CleanupResult(removed=True)


__all__ = [
    "DEFAULT_TIMEOUT",
    "COMMIT_PUSH_TIMEOUT",
    "SAFETY_NET_COMMIT_MESSAGE",
    "ALLOWED_BRANCH_TYPES",
    "MAX_PR_BODY_CHARS",
    "Runner",
    "WorktreeError",
    "WorktreeResult",
    "WorktreeContext",
    "FinalizeResult",
    "CleanupResult",
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
    "finalize_worktree_session",
    "pull_request_state",
    "repo_slug_from_pr_url",
    "pr_base_and_state",
    "merge_pull_request",
    "repo_default_branch",
    "repo_compare_ahead_by",
    "list_worker_worktrees",
    "remove_worker_worktree",
]
