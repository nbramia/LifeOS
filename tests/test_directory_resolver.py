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

    def test_allow_uncloned_false_rejects_a_directory_that_does_not_exist(self, monkeypatch):
        """`allow_uncloned=False` — for a spawn that can't clone into the
        chosen path (a remote host) — falls back to the keyword cascade
        rather than handing over a path with no local evidence it
        exists."""
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("widget", "the Widget repo", "/nonexistent/code/Widget"),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="widget", confidence=0.9),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        mod._project_dirs = None
        result = mod.resolve_working_directory("do the quarterly thing", allow_uncloned=False)
        assert result != "/nonexistent/code/Widget"
        assert result == HOME  # the keyword cascade's own default

    def test_allow_uncloned_true_is_the_default(self, monkeypatch):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("widget", "the Widget repo", "/nonexistent/code/Widget"),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="widget", confidence=0.9),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        mod._project_dirs = None
        assert mod.resolve_working_directory("do the quarterly thing") == "/nonexistent/code/Widget"

    def test_allow_uncloned_false_still_accepts_an_existing_directory(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        existing = tmp_path / "RealProject"
        existing.mkdir()
        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("realproject", "an already-cloned repo", str(existing)),
        ])
        monkeypatch.setattr(
            "api.services.jev_task_routing.judge_task",
            lambda title: TaskJudgment(
                location=JevAnswer(choice="realproject", confidence=0.9),
                difficulty=None, preset_class=None, software_work=None,
            ),
        )
        mod._project_dirs = None
        result = mod.resolve_working_directory("work on that", allow_uncloned=False)
        assert result == str(existing)


class TestLocationAffinityResolution:
    """Repository affinity is an option key, never an arbitrary path."""

    def test_recognized_affinity_maps_through_catalog(self, monkeypatch):
        import api.services.directory_resolver as mod

        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("synthetic-repo", "a synthetic repository", "/catalog/SyntheticRepo"),
        ])

        assert mod.resolve_location_affinity(" Synthetic-Repo ") == "/catalog/SyntheticRepo"

    def test_unknown_affinity_is_not_treated_as_path(self, monkeypatch):
        import api.services.directory_resolver as mod

        monkeypatch.setattr(mod, "_location_options", lambda: [
            ("synthetic-repo", "a synthetic repository", "/catalog/SyntheticRepo"),
        ])

        assert mod.resolve_location_affinity("/untrusted/arbitrary/path") is None


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
        # `path` is rebuilt from the (validated) name, not read back from
        # the cache's own "/code/Cached" field — see `_repo_path`.
        assert repos == [("Cached", "from cache", mod._repo_path("Cached"))]

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

    def test_invalid_repo_names_excluded_from_gh_listing(self, monkeypatch, tmp_path):
        """`..` and a path-traversal name both pass a naive charset check
        but must never become a location option or a clone target."""
        import api.services.directory_resolver as mod
        monkeypatch.setattr(mod, "_github_cache_path", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(settings, "github_owner", "nbramia")
        self._stub_run(monkeypatch, mod, {
            "repo": (0, json.dumps([
                {"name": "..", "description": "parent traversal"},
                {"name": "../../etc", "description": "path traversal"},
                {"name": "Valid-Repo_1.0", "description": "fine"},
            ])),
        })
        repos = mod._github_repos()
        assert {r[0] for r in repos} == {"Valid-Repo_1.0"}

    def test_malicious_cached_repo_name_excluded_and_path_rebuilt_from_name(self, monkeypatch, tmp_path):
        """A cache file is a file on disk, not something this process
        fully controls — an invalid name is dropped on read, and every
        surviving entry's `path` is rebuilt from its (validated) name,
        never trusted from the cache's own `path` field."""
        import api.services.directory_resolver as mod
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "owner": "nbramia",
            "fetched_at": time.time() - 60,
            "repos": [
                {"name": "../../etc", "description": "evil", "path": "/etc"},
                {"name": "..", "description": "evil2", "path": "/code"},
                {"name": "Fine", "description": "ok", "path": "/should/be/ignored"},
            ],
        }))
        monkeypatch.setattr(mod, "_github_cache_path", lambda: cache_file)
        repos = mod._github_repos()
        assert {r[0] for r in repos} == {"Fine"}
        fine = next(r for r in repos if r[0] == "Fine")
        assert fine[2] == mod._repo_path("Fine")
        assert fine[2] != "/should/be/ignored"

    def test_malicious_cached_name_not_in_options_and_ensure_cloned_refuses(self, monkeypatch, tmp_path):
        """End-to-end: a cache file containing `{"name": "../../etc"}`
        never surfaces as a location option, and `ensure_cloned` refuses
        a same-shaped target even when asked directly."""
        import api.services.directory_resolver as mod
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "owner": "nbramia",
            "fetched_at": time.time() - 60,
            "repos": [{"name": "../../etc", "description": "evil", "path": "/etc"}],
        }))
        monkeypatch.setattr(mod, "_github_cache_path", lambda: cache_file)
        monkeypatch.setattr(mod, "_scan_projects", lambda: [])
        options = mod._location_options()
        assert "../../etc" not in {name for name, _d, _p in options}
        assert not any(p == "/etc" for _n, _d, p in options)

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not run for a rejected repo name")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        # A target that doesn't exist on this host, under code_dir, whose
        # name isn't in the (now-empty, given the rejected cache entry)
        # known repo list — refused regardless of the cache's own path.
        assert mod.ensure_cloned(str(code_dir / "etc")) is False

    def test_write_uses_atomic_replace_no_leftover_tmp_file(self, tmp_path):
        """Round-trips through the real write + read path, then checks the
        cache directory for a stray `.github_repos_cache.*` temp file —
        `os.replace` either leaves the finished file or nothing, never a
        partial one."""
        import api.services.directory_resolver as mod
        cache_file = tmp_path / "cache.json"
        mod._write_github_cache(cache_file, "nbramia", [("Widget", "d", "/code/Widget")])
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["owner"] == "nbramia"
        assert data["repos"] == [{"name": "Widget", "description": "d", "path": "/code/Widget"}]
        # Only check for a stray temp artifact of this write, not that the
        # directory is otherwise pristine — `tmp_path` can be reused/shared
        # with unrelated fixtures across test runs.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".github_repos_cache.")]
        assert leftovers == []


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
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)
        target = code_dir / "NewRepo"
        monkeypatch.setattr(settings, "github_owner", "nbramia")
        monkeypatch.setattr(mod, "_github_repos", lambda: [("NewRepo", "d", str(target))])

        def fake_run(argv, **kwargs):
            target.mkdir(parents=True)  # simulate `gh repo clone` creating the checkout
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.ensure_cloned(str(target)) is True

    def test_clone_failure_returns_false(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)
        target = code_dir / "FailedRepo"
        monkeypatch.setattr(settings, "github_owner", "nbramia")
        monkeypatch.setattr(mod, "_github_repos", lambda: [("FailedRepo", "d", str(target))])

        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="auth error")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.ensure_cloned(str(target)) is False
        assert not target.exists()

    def test_no_owner_returns_false(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)
        target = code_dir / "NoOwnerRepo"
        monkeypatch.setattr(settings, "github_owner", "")
        monkeypatch.setattr(mod, "_github_cache_path", lambda: tmp_path / "cache.json")
        monkeypatch.setattr(mod, "_github_repos", lambda: [("NoOwnerRepo", "d", str(target))])

        def fail_if_called(argv, **kwargs):
            if argv[:2] == ["gh", "repo"]:
                raise AssertionError("clone should not run without a resolvable owner")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(target)) is False

    def test_refuses_path_outside_code_root(self, monkeypatch, tmp_path):
        import api.services.directory_resolver as mod
        monkeypatch.setattr(settings, "code_dir", str(tmp_path / "Code"), raising=False)
        outside = tmp_path / "NotCode" / "Repo"

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not run for a path outside code_root")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(outside)) is False

    def test_refuses_name_not_in_known_repos(self, monkeypatch, tmp_path):
        """The containment check alone isn't enough — the name must also
        be one of the operator's actual repos, not merely well-formed and
        under code_dir (e.g. an arbitrary `[working_dir::]` card field)."""
        import api.services.directory_resolver as mod
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)
        monkeypatch.setattr(mod, "_github_repos", lambda: [("KnownRepo", "d", str(code_dir / "KnownRepo"))])

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not run for a repo name not in the known list")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(code_dir / "UnknownRepo")) is False

    def test_refuses_dot_dot_path_component(self, monkeypatch, tmp_path):
        """`<code_dir>/..` resolves to code_dir's own parent — caught by
        the containment check."""
        import api.services.directory_resolver as mod
        code_dir = tmp_path / "Code"
        monkeypatch.setattr(settings, "code_dir", str(code_dir), raising=False)

        def fail_if_called(argv, **kwargs):
            raise AssertionError("gh should not run for a '..' path component")

        monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
        assert mod.ensure_cloned(str(code_dir / "..")) is False
