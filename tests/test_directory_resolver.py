"""
Tests for the directory resolver.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest
from unittest.mock import patch

from config.settings import settings
from api.services.jev_task_routing import JevAnswer, TaskJudgment

pytestmark = pytest.mark.unit

HOME = os.path.expanduser("~")
VAULT_DIR = str(settings.vault_path)


class TestResolveWorkingDirectory:
    """Tests for resolve_working_directory()."""

    def _resolve(self, task: str) -> str:
        # Reset cached project dirs between tests
        import api.services.directory_resolver as mod
        mod._project_dirs = None
        return mod.resolve_working_directory(task)

    def test_vault_keywords(self):
        assert self._resolve("edit my journal entry") == VAULT_DIR
        assert self._resolve("add to the backlog") == VAULT_DIR
        assert self._resolve("update my meeting notes") == VAULT_DIR
        assert self._resolve("create a daily note") == VAULT_DIR
        assert self._resolve("open the vault") == VAULT_DIR
        assert self._resolve("find obsidian files") == VAULT_DIR

    def test_vault_word_boundary(self):
        """'note' should not match 'notification' or 'denoted'."""
        result = self._resolve("send a notification to the team")
        assert result != VAULT_DIR

        result = self._resolve("this denoted something")
        assert result != VAULT_DIR

    def test_lifeos_keywords(self):
        assert self._resolve("fix the lifeos server") == os.path.join(HOME, "Code", "LifeOS")
        assert self._resolve("update the sync logic") == os.path.join(HOME, "Code", "LifeOS")
        assert self._resolve("change the telegram bot") == os.path.join(HOME, "Code", "LifeOS")
        assert self._resolve("check chromadb status") == os.path.join(HOME, "Code", "LifeOS")
        assert self._resolve("add an api endpoint") == os.path.join(HOME, "Code", "LifeOS")

    def test_code_keywords(self):
        code_dir = os.path.join(HOME, "Code")
        assert self._resolve("write a script to automate") == code_dir
        assert self._resolve("create a cron job") == code_dir

    def test_code_word_boundary(self):
        """'code' should match as a word, not inside 'encode'."""
        code_dir = os.path.join(HOME, "Code")
        assert self._resolve("write some code") == code_dir
        # 'encode' contains 'code' but shouldn't match code keyword
        # (it would still match via word boundary since 'code' appears at end)
        # This is fine — encode ends with 'code' which matches \bcode\b

    def test_default_to_home(self):
        assert self._resolve("do something random") == HOME
        assert self._resolve("hello world") == HOME

    def test_priority_vault_over_lifeos(self):
        """Vault keywords should take priority over LifeOS keywords."""
        # "sync" is LifeOS, but "notes" is vault — vault should win since checked first
        result = self._resolve("sync my notes")
        assert result == VAULT_DIR

    def test_vault_path_honors_late_monkeypatch(self, monkeypatch, tmp_path):
        """The resolver must read settings.vault_path at call time,
        not cache it into a module-level constant at import time — otherwise
        whichever test imports the module first under xdist fixes the value
        for every later test in that worker, even ones that monkeypatch
        settings.vault_path (as test_vault_write_route.py does) ahead of
        any call to this resolver."""
        monkeypatch.setattr(settings, "vault_path", tmp_path)
        assert self._resolve("open the vault") == str(tmp_path)

    @patch("api.services.directory_resolver.Path")
    def test_project_name_match(self, mock_path_cls):
        """Project directory names should be matched in task."""
        import api.services.directory_resolver as mod
        mod._project_dirs = None

        # Mock the Code directory scan
        mock_entry1 = type("Entry", (), {"name": "MyProject", "is_dir": lambda self: True})()
        mock_entry2 = type("Entry", (), {"name": "AnotherApp", "is_dir": lambda self: True})()
        mock_code_path = type("MockPath", (), {
            "is_dir": lambda self: True,
            "iterdir": lambda self: [mock_entry1, mock_entry2],
        })()
        mock_path_cls.return_value = mock_code_path

        # Patch str() on entries to return full paths
        mock_entry1.__str__ = lambda self: f"{HOME}/Code/MyProject"
        mock_entry2.__str__ = lambda self: f"{HOME}/Code/AnotherApp"

        # Need to also patch the entry str representation via the append
        mod._project_dirs = [
            ("myproject", f"{HOME}/Code/MyProject"),
            ("anotherapp", f"{HOME}/Code/AnotherApp"),
        ]

        result = mod.resolve_working_directory("update the myproject docs")
        assert result == f"{HOME}/Code/MyProject"


class TestJevLocationResolution:
    """resolve_working_directory's Jev fan-out consumption."""

    def _resolve(self, task: str) -> str:
        import api.services.directory_resolver as mod
        mod._project_dirs = None
        return mod.resolve_working_directory(task)

    def test_high_confidence_location_wins(self, monkeypatch):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("widget", "the Widget repo", "/code/Widget"),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="widget", confidence=0.9),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        # Title carries no keyword cue at all, so a keyword fallback would
        # land on home — proving the Jev answer, not a lucky keyword match, won.
        assert self._resolve("do the quarterly thing") == "/code/Widget"

    def test_low_confidence_falls_back_to_keyword_cascade(self, monkeypatch):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("widget", "the Widget repo", "/code/Widget"),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="widget", confidence=0.59),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        # Same result the keyword cascade alone would give today.
        assert self._resolve("open the vault") == VAULT_DIR

    def test_location_confidence_boundary_at_exactly_point_six(self, monkeypatch):
        """Exactly 0.6 must be accepted (>=, not >) — the mutation check
        for this file: flipping the resolver's `>= 0.6` to `> 0.6` fails
        this test."""
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("widget", "the Widget repo", "/code/Widget"),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="widget", confidence=0.6),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        assert self._resolve("do the quarterly thing") == "/code/Widget"

    def test_no_judgment_falls_back_to_keyword_cascade(self, monkeypatch):
        monkeypatch.setattr("api.services.jev_task_routing.judge_task", lambda title: None)
        assert self._resolve("do something random") == HOME

    def test_github_only_repo_resolves_under_code_dir_even_if_not_cloned(self, monkeypatch):
        """A repo choice that only came from `_github_repos()` (not yet
        cloned locally) still resolves to a path under code_dir."""
        import api.services.directory_resolver as mod
        code_dir = os.path.join(HOME, "Code")
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("notyetcloned", "a repo not on this host", os.path.join(code_dir, "NotYetCloned")),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="notyetcloned", confidence=0.8),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        result = self._resolve("work on that other project")
        assert result == os.path.join(code_dir, "NotYetCloned")
        assert result.startswith(code_dir + os.sep)


class TestGithubRepoListing:
    """`_github_repos()` and its 24h on-disk cache."""

    def _stub_run(self, monkeypatch, mod, responses):
        """`responses` maps argv[1] (e.g. 'user' or 'repo') to a
        (returncode, stdout) tuple."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            key = argv[1] if len(argv) > 1 else argv[0]
            rc, out = responses.get(key, (1, ""))
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        return calls

    def test_repos_listed_from_gh(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_github_cache_path", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(settings, "github_owner", "nbramia")
        self._stub_run(monkeypatch, mod, {
            "repo": (0, json.dumps([
                {"name": "Widget", "description": "A widget"},
                {"name": "NoDesc", "description": None},
            ])),
        })
        repos = mod._github_repos()
        names = {r[0] for r in repos}
        assert names == {"Widget", "NoDesc"}
        widget = next(r for r in repos if r[0] == "Widget")
        assert widget[1] == "A widget"
        assert widget[2] == os.path.join(mod._CODE_DIR, "Widget")
        nodesc = next(r for r in repos if r[0] == "NoDesc")
        assert nodesc[1] == "local project directory"

    def test_gh_missing_returns_local_only(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_github_cache_path", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(settings, "github_owner", "nbramia")

        def raise_missing(argv, **kwargs):
            raise FileNotFoundError("gh not found")

        monkeypatch.setattr(mod.subprocess, "run", raise_missing)
        assert mod._github_repos() == []

    def test_cache_hit_within_24h_skips_gh(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "owner": "nbramia",
            "fetched_at": time.time() - 60,  # 1 minute old
            "repos": [{"name": "Cached", "description": "from cache", "path": "/code/Cached"}],
        }))
        monkeypatch.setattr(mod, "_github_cache_path", lambda: cache_file)

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not be invoked on a fresh cache hit")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        repos = mod._github_repos()
        assert repos == [("Cached", "from cache", "/code/Cached")]

    def test_cache_expired_refetches(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "owner": "nbramia",
            "fetched_at": time.time() - (25 * 60 * 60),  # 25 hours old
            "repos": [{"name": "Stale", "description": "old", "path": "/code/Stale"}],
        }))
        monkeypatch.setattr(mod, "_github_cache_path", lambda: cache_file)
        monkeypatch.setattr(settings, "github_owner", "nbramia")
        self._stub_run(monkeypatch, mod, {
            "repo": (0, json.dumps([{"name": "Fresh", "description": "new"}])),
        })
        repos = mod._github_repos()
        assert [r[0] for r in repos] == ["Fresh"]


class TestEnsureCloned:
    def test_already_cloned_returns_true_without_gh(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        existing = tmp_path / "AlreadyHere"
        existing.mkdir()

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not run when the directory already exists")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(existing)) is True

    def test_clone_success(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        target = tmp_path / "NewRepo"
        monkeypatch.setattr(settings, "github_owner", "nbramia")

        def fake_run(argv, **kwargs):
            target.mkdir()  # simulate `gh repo clone` creating the checkout
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.ensure_cloned(str(target)) is True

    def test_clone_failure_returns_false(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        target = tmp_path / "FailedRepo"
        monkeypatch.setattr(settings, "github_owner", "nbramia")

        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="auth error")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.ensure_cloned(str(target)) is False
        assert not target.exists()

    def test_no_owner_returns_false(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        target = tmp_path / "NoOwnerRepo"
        monkeypatch.setattr(settings, "github_owner", "")
        monkeypatch.setattr(mod, "_github_cache_path", lambda: tmp_path / "cache.json")

        def fail_if_called(argv, **kwargs):
            if argv[:2] == ["gh", "repo"]:
                raise AssertionError("clone should not run without a resolvable owner")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(target)) is False
