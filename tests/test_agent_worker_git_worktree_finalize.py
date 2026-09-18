"""Tests for `git_worktree.finalize_worktree_session` — the completion-time
half of git worktree provisioning: safety-net commit, push, and (when
finalizing a fully-complete session) opening a pull request.

Every test runs against a real temporary git repository and worktree — no
network access. `gh` is never actually invoked: a fake runner captures the
argv it would have received, so the pull-request paths are exercised
without any real GitHub interaction.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api.services.agent_worker import git_worktree
from api.services.agent_worker.git_worktree import (
    SAFETY_NET_COMMIT_MESSAGE,
    ensure_worktree,
    finalize_worktree_session,
)


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path, name: str = "repo") -> Path:
    origin = root / f"{name}-origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    assert _git(repo, "push", "-q", "origin", "main").returncode == 0
    return repo


def _provision(root: Path, task_id: str = "task-1", title: str = "fix the thing", name: str = "repo"):
    repo = _init_repo_with_origin(root, name=name)
    result = ensure_worktree(str(repo), task_id, title)
    assert result.is_git
    return repo, Path(result.working_dir), result.branch


class _FakeGh:
    """Records every invocation; scripted responses per subcommand.

    ``--body-file -`` means the body arrives over stdin (the ``input``
    kwarg), the same as a real ``gh`` would read it — never a real file on
    disk, so there's nothing left to clean up on either the local or the
    remote side.
    """

    def __init__(self, *, view_result=None, create_result=None):
        self.calls: list[list[str]] = []
        self.body_files_content: list[str] = []
        self._view_result = view_result  # CompletedProcess or None
        self._create_result = create_result

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "--body-file" in cmd and cmd[cmd.index("--body-file") + 1] == "-":
            self.body_files_content.append(kwargs.get("input") or "")
        if cmd[:2] == ["gh", "pr"] and cmd[2] == "view":
            if self._view_result is not None:
                return self._view_result
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no PR found")
        if cmd[:2] == ["gh", "pr"] and cmd[2] == "create":
            if self._create_result is not None:
                return self._create_result
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="https://github.com/x/y/pull/1\n", stderr="")
        raise AssertionError(f"unexpected gh invocation: {cmd}")


def test_finalize_pushes_and_opens_pr_on_full_completion(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path)
    (worktree / "change.txt").write_text("a real change\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "do the work")

    gh = _FakeGh()
    result = finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing", pr_body="done",
        gh_runner=gh,
    )

    assert result.applicable is True
    assert result.pushed is True
    assert result.pr_url == "https://github.com/x/y/pull/1"
    assert result.pr_opened is True
    assert result.error is None
    # The branch actually landed on origin.
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", branch)
    assert branch in ls_remote.stdout
    # gh was asked to check for an existing PR before creating one.
    assert any(c[:3] == ["gh", "pr", "view"] for c in gh.calls)
    assert any(c[:3] == ["gh", "pr", "create"] for c in gh.calls)


def test_finalize_reuses_existing_pr_for_branch(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-2")
    (worktree / "change.txt").write_text("a change\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    gh = _FakeGh(view_result=subprocess.CompletedProcess(
        ["gh", "pr", "view", branch, "--json", "url"], returncode=0,
        stdout='{"url": "https://github.com/x/y/pull/42"}', stderr="",
    ))
    result = finalize_worktree_session(str(worktree), open_pr=True, gh_runner=gh)

    assert result.pr_url == "https://github.com/x/y/pull/42"
    assert result.pr_opened is False
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh.calls)


def test_finalize_question_pause_pushes_without_opening_a_pr(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-3")
    (worktree / "change.txt").write_text("partial work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "partial")

    gh = _FakeGh()
    result = finalize_worktree_session(str(worktree), open_pr=False, gh_runner=gh)

    assert result.pushed is True
    assert result.pr_url is None
    assert result.pr_opened is False
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", branch)
    assert branch in ls_remote.stdout
    assert gh.calls == []  # gh is never consulted when open_pr is False


def test_finalize_safety_net_commits_uncommitted_changes(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-4")
    (worktree / "leftover.txt").write_text("never committed by the session\n")
    # Deliberately no `git add`/`commit` — the session left this dirty.

    result = finalize_worktree_session(str(worktree), open_pr=False)

    assert result.safety_net_committed is True
    assert result.pushed is True
    status = _git(worktree, "status", "--porcelain")
    assert status.stdout.strip() == ""  # clean after the safety net
    log = _git(worktree, "log", "-1", "--pretty=%s")
    assert log.stdout.strip() == SAFETY_NET_COMMIT_MESSAGE
    # And it actually reached origin.
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", branch)
    assert branch in ls_remote.stdout


def test_finalize_nothing_to_push_opens_no_pr(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-5")
    # No changes at all — branch is identical to its base.

    gh = _FakeGh()
    result = finalize_worktree_session(str(worktree), open_pr=True, gh_runner=gh)

    assert result.pushed is True
    assert result.nothing_to_push is True
    assert result.pr_url is None
    assert gh.calls == []  # never even asked gh — no empty PR


def test_finalize_reports_push_failure_without_pretending_success(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-6")
    (worktree / "change.txt").write_text("change\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")
    # Point origin somewhere unreachable so the push fails.
    _git(worktree, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    result = finalize_worktree_session(str(worktree), open_pr=True)

    assert result.pushed is False
    assert result.error is not None
    assert result.pr_url is None


def test_finalize_never_bypasses_the_pre_push_hook(tmp_path: Path):
    """A repository's pre-push hook must actually run — proves the push
    never carries `--no-verify`."""
    repo, worktree, branch = _provision(tmp_path, task_id="task-7")
    (worktree / "change.txt").write_text("change\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    # Hooks live in the shared git dir — install it via the primary
    # checkout so the linked worktree picks it up too.
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "pre-push"
    hook.write_text("#!/bin/sh\necho 'blocked by policy' >&2\nexit 1\n")
    hook.chmod(0o755)

    result = finalize_worktree_session(str(worktree), open_pr=False)

    assert result.pushed is False
    assert result.error is not None
    assert "blocked by policy" in result.error


def test_finalize_noop_outside_a_worktree(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path, name="repo8")
    (repo / "dirty.txt").write_text("uncommitted in the primary checkout\n")

    result = finalize_worktree_session(str(repo), open_pr=True)

    assert result.applicable is False
    # Nothing was touched — the primary checkout stays dirty exactly as
    # the session left it, never committed by the worker.
    status = _git(repo, "status", "--porcelain")
    assert "dirty.txt" in status.stdout


def test_finalize_noop_for_non_git_directory(tmp_path: Path):
    plain_dir = tmp_path / "vault"
    plain_dir.mkdir()

    result = finalize_worktree_session(str(plain_dir), open_pr=True)

    assert result.applicable is False


def test_finalize_noop_for_none_working_dir():
    result = finalize_worktree_session(None, open_pr=True)
    assert result.applicable is False


# ---------------------------------------------------------------------------
# F6 — every finalize subprocess is timeout/OSError-safe.
# ---------------------------------------------------------------------------

def test_run_converts_a_hang_into_a_reported_error_not_a_crash(monkeypatch):
    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(git_worktree, "_local_runner", raise_timeout)

    result = git_worktree._run(["git", "status"], timeout=1)

    assert result.returncode != 0
    assert "timed out" in result.stderr


def test_run_converts_a_missing_binary_into_a_reported_error_not_a_crash(monkeypatch):
    def raise_oserror(cmd, **kwargs):
        raise OSError("no such file or directory")

    monkeypatch.setattr(git_worktree, "_local_runner", raise_oserror)

    result = git_worktree._run(["git", "status"])

    assert result.returncode != 0
    assert "no such file" in result.stderr


def test_finalize_reports_a_hook_timeout_as_error_without_crashing(tmp_path: Path, monkeypatch):
    """A slow pre-push hook (or a dead network) must surface as
    `FinalizeResult.error`, not an unhandled `TimeoutExpired` that would
    crash the dispatch path."""
    repo, worktree, branch = _provision(tmp_path, task_id="task-timeout")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    real_local_runner = git_worktree._local_runner

    def flaky(cmd, **kwargs):
        if cmd[:2] == ["git", "push"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        return real_local_runner(cmd, **kwargs)

    monkeypatch.setattr(git_worktree, "_local_runner", flaky)

    result = finalize_worktree_session(str(worktree), open_pr=False)

    assert result.pushed is False
    assert result.error is not None
    assert "timed out" in result.error


def test_finalize_uses_a_generous_timeout_for_commit_and_push(tmp_path: Path, monkeypatch):
    """Commit/push may run pre-commit/pre-push hooks and can legitimately
    take longer than a plain status/rev-parse call."""
    repo, worktree, branch = _provision(tmp_path, task_id="task-timeouts")
    (worktree / "change.txt").write_text("work\n")
    # left uncommitted on purpose — exercises the safety-net add+commit too.

    seen: list[tuple[str, int | None]] = []
    real_local_runner = git_worktree._local_runner

    def spying(cmd, **kwargs):
        if cmd[0] == "git" and len(cmd) > 1:
            seen.append((cmd[1], kwargs.get("timeout")))
        return real_local_runner(cmd, **kwargs)

    monkeypatch.setattr(git_worktree, "_local_runner", spying)

    finalize_worktree_session(str(worktree), open_pr=False)

    timeouts = dict(seen)
    assert timeouts["status"] == git_worktree.DEFAULT_TIMEOUT
    assert timeouts["add"] == git_worktree.COMMIT_PUSH_TIMEOUT
    assert timeouts["commit"] == git_worktree.COMMIT_PUSH_TIMEOUT
    assert timeouts["push"] == git_worktree.COMMIT_PUSH_TIMEOUT


# ---------------------------------------------------------------------------
# F7 — bounded PR body, delivered via --body-file, leading with the card
# title and the branch's commit list.
# ---------------------------------------------------------------------------

def test_finalize_pr_body_uses_body_file_not_argv(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-bodyfile")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "did the work")

    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing",
        pr_body="a normal completion summary", gh_runner=gh,
    )

    create_call = next(c for c in gh.calls if c[:3] == ["gh", "pr", "create"])
    assert "--body-file" in create_call
    assert "--body" not in create_call  # never the raw, unbounded flag
    body = gh.body_files_content[0]
    assert "fix the thing" in body
    assert "a normal completion summary" in body


def test_finalize_pr_body_leads_with_title_then_summary_then_commits(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-bodyorder")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "a specific commit message")

    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="Card Title Here",
        pr_body="Session summary text.", gh_runner=gh,
    )

    body = gh.body_files_content[0]
    title_pos = body.index("Card Title Here")
    summary_pos = body.index("Session summary text.")
    commit_pos = body.index("a specific commit message")
    assert title_pos < summary_pos < commit_pos


def test_finalize_pr_body_is_bounded_and_reports_truncation(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-bodybound")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    huge_summary = "word " * 2000  # long, but space-broken so it isn't token-shaped
    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing",
        pr_body=huge_summary, gh_runner=gh,
    )

    body = gh.body_files_content[0]
    assert len(body) <= git_worktree.MAX_PR_BODY_CHARS + 200  # + truncation note
    assert "truncated" in body


# ---------------------------------------------------------------------------
# Remote-host finalization — every filesystem/git/gh operation must go
# through the same resolved runner provisioning used. No real ssh is ever
# invoked: the runner `resolve_runner_for_host` would build is swapped for
# a fake that executes git for real (standing in for the remote host's own
# filesystem) and fakes `gh` (no real GitHub call).
# ---------------------------------------------------------------------------

def test_finalize_remote_host_routes_every_operation_through_the_resolved_runner(tmp_path: Path, monkeypatch):
    repo, worktree, branch = _provision(tmp_path, task_id="task-remote-finalize")
    (worktree / "change.txt").write_text("remote work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "remote work")

    calls: list[list[str]] = []
    body_inputs: list[str] = []

    def fake_runner(cmd, *, cwd=None, timeout=git_worktree.DEFAULT_TIMEOUT, input=None):
        calls.append(cmd)
        if cmd and cmd[0] == "gh":
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no PR found")
            if cmd[:3] == ["gh", "pr", "create"]:
                body_inputs.append(input or "")
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="https://github.com/x/y/pull/77\n", stderr="",
                )
            raise AssertionError(f"unexpected gh invocation: {cmd}")
        kwargs = {"cwd": cwd, "capture_output": True, "text": True, "timeout": timeout}
        if input is not None:
            kwargs["input"] = input
        return subprocess.run(cmd, **kwargs)

    def fake_resolve(host):
        assert host == "studio"
        return fake_runner

    monkeypatch.setattr(git_worktree, "resolve_runner_for_host", fake_resolve)

    result = finalize_worktree_session(
        str(worktree), open_pr=True, host="studio", pr_title="fix the thing", pr_body="done",
    )

    assert result.applicable is True
    assert result.pushed is True
    assert result.pr_url == "https://github.com/x/y/pull/77"
    assert any(c[:2] == ["git", "push"] for c in calls)
    assert any(c[:2] == ["git", "status"] for c in calls)
    assert body_inputs and "done" in body_inputs[0]
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", branch)
    assert branch in ls_remote.stdout


def test_finalize_unresolvable_host_reports_error_not_silently_false(tmp_path: Path, monkeypatch):
    """A provisioned worktree pinned to a host that no longer resolves
    must never read as `applicable=False` ("nothing to finalize") — it's a
    real finalization failure and must be reported as one."""
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    repo, worktree, branch = _provision(tmp_path, task_id="task-badhost")

    result = finalize_worktree_session(str(worktree), open_pr=False, host="no-such-host")

    assert result.applicable is True
    assert result.error is not None
    assert result.pushed is False


# ---------------------------------------------------------------------------
# A failed/timed-out `git status --porcelain` must be reported as an
# error, never silently treated as "the tree is clean" (which would
# happily push whatever was already committed and drop real uncommitted
# work on the floor).
# ---------------------------------------------------------------------------

def test_finalize_status_check_failure_is_reported_not_treated_as_clean(tmp_path: Path, monkeypatch):
    repo, worktree, branch = _provision(tmp_path, task_id="task-statusfail")
    (worktree / "dirty.txt").write_text("uncommitted work\n")  # genuinely dirty

    real_local_runner = git_worktree._local_runner

    def flaky(cmd, **kwargs):
        if cmd[:2] == ["git", "status"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        return real_local_runner(cmd, **kwargs)

    monkeypatch.setattr(git_worktree, "_local_runner", flaky)

    result = finalize_worktree_session(str(worktree), open_pr=False)

    assert result.pushed is False
    assert result.safety_net_committed is False
    assert result.error is not None
    # Nothing destructive attempted — the dirty file is exactly as the
    # session left it, never committed or pushed.
    status = _git(worktree, "status", "--porcelain")
    assert "dirty.txt" in status.stdout


# ---------------------------------------------------------------------------
# PR body secret scrubbing — defense in depth alongside the git-discipline
# prompt's own instruction not to put secrets in the completion summary.
# ---------------------------------------------------------------------------

def test_finalize_pr_body_redacts_a_bot_token(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-secret-bot")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing",
        pr_body="Used bot123456789:AAHrealtokenlookingvalue1234 to send a message.",
        gh_runner=gh,
    )

    body = gh.body_files_content[0]
    assert "AAHrealtokenlookingvalue1234" not in body
    assert "bot<REDACTED>" in body


def test_finalize_pr_body_redacts_common_secret_shapes(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-secret-mix")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    secrets = [
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefgh12345678",
    ]
    summary = "Config values: " + " ".join(secrets)
    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing", pr_body=summary, gh_runner=gh,
    )

    body = gh.body_files_content[0]
    for secret in secrets:
        assert secret not in body
    assert "<REDACTED" in body


def test_finalize_pr_body_leaves_ordinary_text_alone(tmp_path: Path):
    repo, worktree, branch = _provision(tmp_path, task_id="task-secret-clean")
    (worktree / "change.txt").write_text("work\n")
    _git(worktree, "add", "change.txt")
    _git(worktree, "commit", "-q", "-m", "work")

    gh = _FakeGh()
    finalize_worktree_session(
        str(worktree), open_pr=True, pr_title="fix the thing",
        pr_body="Fixed the printer driver bug and added a regression test.",
        gh_runner=gh,
    )

    body = gh.body_files_content[0]
    assert "Fixed the printer driver bug and added a regression test." in body
