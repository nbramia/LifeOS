"""Verifies the conftest-level guard that keeps `directory_resolver.py`'s
real `gh` subprocess calls out of unit tests.

`_resolve_github_owner`/`_github_repos`/`ensure_cloned` all funnel their
real `gh` invocation through the single `_run_gh` seam. The guard installed
in `tests/conftest.py` wraps that seam and raises before any real
subprocess call, unless a test has already neutralized it by patching
`directory_resolver.subprocess.run` itself. This module is the canary: it
proves the guard actually trips, and that a test which patches
`subprocess.run` is left alone.
"""
from __future__ import annotations

import pytest

import api.services.directory_resolver as directory_resolver


pytestmark = pytest.mark.unit

_GUARD_MESSAGE = "real gh subprocess call"


def test_guard_blocks_a_real_gh_call_by_default():
    """Calling the seam directly, with nothing patched, must raise before
    any real subprocess ever starts."""
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        directory_resolver._run_gh(["gh", "api", "user", "-q", ".login"], timeout=5)


def test_guard_covers_every_gh_call_site(monkeypatch, tmp_path):
    """The three real callers of `_run_gh` — owner resolution, repo
    listing, and clone — each degrade to their documented no-gh fallback
    rather than raising, since they all wrap the seam in a broad `except
    Exception`. This proves the guard is actually reached from each call
    site, not just from a direct call to `_run_gh`."""
    monkeypatch.setattr(directory_resolver, "_github_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(directory_resolver.settings, "github_owner", "", raising=False)

    assert directory_resolver._resolve_github_owner(tmp_path / "cache.json") == ""
    assert directory_resolver._github_repos() == []

    code_dir = tmp_path / "Code"
    monkeypatch.setattr(directory_resolver.settings, "code_dir", str(code_dir), raising=False)
    monkeypatch.setattr(directory_resolver, "_github_repos", lambda: [("Widget", "d", str(code_dir / "Widget"))])
    assert directory_resolver.ensure_cloned(str(code_dir / "Widget")) is False


def test_guard_allows_a_test_that_patches_subprocess_run_itself(monkeypatch):
    """A test that neutralizes `directory_resolver.subprocess.run` directly
    (the same escape hatch every `TestGithubRepoListing`/`TestEnsureCloned`
    test in `test_directory_resolver.py` already uses) is left alone — the
    guard only fires when `subprocess.run` is still the real, unpatched
    stdlib function."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="fake-login\n", stderr="")

    monkeypatch.setattr(directory_resolver.subprocess, "run", fake_run)

    result = directory_resolver._run_gh(["gh", "api", "user", "-q", ".login"], timeout=5)

    assert result.stdout == "fake-login\n"
    assert calls == [["gh", "api", "user", "-q", ".login"]]
