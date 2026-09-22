"""Tests for the `gh`-by-URL/repo-slug helpers `git_worktree.py` adds for
the project owner's merge-on-accept and completion gate (see
`test_agent_project_owner_review.py`, which exercises them through
`lifeos_agent_project_owner`): `repo_slug_from_pr_url`, `pr_base_and_state`,
`merge_pull_request`, `repo_default_branch`, and `repo_compare_ahead_by`.

None of these need a local git checkout — every `gh` call is either given a
full pull request URL (which `gh` resolves the repository from itself) or an
explicit `owner/repo` slug, so tests exercise a fake runner directly with no
real repository or network access.
"""
from __future__ import annotations

import subprocess

import pytest

from api.services.agent_worker.git_worktree import (
    merge_pull_request,
    pr_base_and_state,
    repo_compare_ahead_by,
    repo_default_branch,
    repo_slug_from_pr_url,
)

pytestmark = pytest.mark.unit


class _FakeRunner:
    """Records every invocation; returns a scripted `CompletedProcess`, or
    raises `FileNotFoundError` (the real failure mode when the `gh` binary
    itself is missing — `_run` turns that into a returncode-127 result,
    which is what "gh missing" looks like to every caller)."""

    def __init__(self, *, result=None, raise_missing=False):
        self.calls: list[list[str]] = []
        self._result = result
        self._raise_missing = raise_missing

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self._raise_missing:
            raise FileNotFoundError("gh: command not found")
        if self._result is not None:
            return self._result
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


def test_repo_slug_from_pr_url_parses_owner_repo():
    assert repo_slug_from_pr_url("https://github.com/acme/widgets/pull/42") == "acme/widgets"


def test_repo_slug_from_pr_url_rejects_non_pr_urls():
    assert repo_slug_from_pr_url("https://github.com/acme/widgets") is None
    assert repo_slug_from_pr_url("not a url") is None
    assert repo_slug_from_pr_url("") is None


def test_pr_base_and_state_parses_gh_json():
    runner = _FakeRunner(result=subprocess.CompletedProcess(
        [], returncode=0, stdout='{"baseRefName": "feat/integration-abc123", "state": "OPEN"}', stderr="",
    ))
    data, error = pr_base_and_state("https://github.com/acme/widgets/pull/9", runner=runner)
    assert error is None
    assert data == {"baseRefName": "feat/integration-abc123", "state": "OPEN"}
    assert runner.calls[0][:3] == ["gh", "pr", "view"]
    assert runner.calls[0][3] == "https://github.com/acme/widgets/pull/9"


def test_pr_base_and_state_reports_gh_failure():
    runner = _FakeRunner(result=subprocess.CompletedProcess(
        [], returncode=1, stdout="", stderr="pull request not found",
    ))
    data, error = pr_base_and_state("https://github.com/acme/widgets/pull/9", runner=runner)
    assert data is None
    assert "pull request not found" in error


def test_pr_base_and_state_missing_gh_fails_closed_with_clear_error():
    runner = _FakeRunner(raise_missing=True)
    data, error = pr_base_and_state("https://github.com/acme/widgets/pull/9", runner=runner)
    assert data is None
    assert error  # a clear, specific error -- never silently "not found"


def test_merge_pull_request_success():
    runner = _FakeRunner()
    merged, error = merge_pull_request("https://github.com/acme/widgets/pull/9", runner=runner)
    assert merged is True
    assert error is None
    assert runner.calls[0] == ["gh", "pr", "merge", "https://github.com/acme/widgets/pull/9", "--merge"]


def test_merge_pull_request_failure_surfaces_gh_stderr():
    runner = _FakeRunner(result=subprocess.CompletedProcess(
        [], returncode=1, stdout="", stderr="merge conflict",
    ))
    merged, error = merge_pull_request("https://github.com/acme/widgets/pull/9", runner=runner)
    assert merged is False
    assert "merge conflict" in error


def test_merge_pull_request_missing_gh_fails_closed():
    runner = _FakeRunner(raise_missing=True)
    merged, error = merge_pull_request("https://github.com/acme/widgets/pull/9", runner=runner)
    assert merged is False
    assert error


def test_repo_default_branch_parses_gh_api_output():
    runner = _FakeRunner(result=subprocess.CompletedProcess([], returncode=0, stdout="main\n", stderr=""))
    branch, error = repo_default_branch("acme/widgets", runner=runner)
    assert branch == "main"
    assert error is None
    assert runner.calls[0][:2] == ["gh", "api"]
    assert runner.calls[0][2] == "repos/acme/widgets"


def test_repo_default_branch_empty_output_is_an_error():
    runner = _FakeRunner(result=subprocess.CompletedProcess([], returncode=0, stdout="\n", stderr=""))
    branch, error = repo_default_branch("acme/widgets", runner=runner)
    assert branch is None
    assert error


def test_repo_compare_ahead_by_parses_int():
    runner = _FakeRunner(result=subprocess.CompletedProcess([], returncode=0, stdout="3\n", stderr=""))
    ahead_by, error = repo_compare_ahead_by("acme/widgets", "main", "feat/integration-abc123", runner=runner)
    assert ahead_by == 3
    assert error is None
    assert runner.calls[0][2] == "repos/acme/widgets/compare/main...feat/integration-abc123"


def test_repo_compare_ahead_by_zero_when_merged():
    runner = _FakeRunner(result=subprocess.CompletedProcess([], returncode=0, stdout="0\n", stderr=""))
    ahead_by, error = repo_compare_ahead_by("acme/widgets", "main", "feat/integration-abc123", runner=runner)
    assert ahead_by == 0
    assert error is None


def test_repo_compare_ahead_by_missing_gh_fails_closed():
    runner = _FakeRunner(raise_missing=True)
    ahead_by, error = repo_compare_ahead_by("acme/widgets", "main", "feat/integration-abc123", runner=runner)
    assert ahead_by is None
    assert error


def test_unresolvable_host_fails_closed_before_any_command_runs():
    """`resolve_runner_for_host` itself raising `WorktreeError` for an
    unregistered host must reach every one of these helpers as an
    ordinary `(None/False, error)` -- never propagate, and never fall
    back to a local runner that would operate on the wrong machine."""
    merged, error = merge_pull_request("https://github.com/acme/widgets/pull/9", host="nonexistent-host")
    assert merged is False
    assert error
