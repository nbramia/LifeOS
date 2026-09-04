"""
Tests for the `./scripts/test.sh auto` diff-aware scope mapping.

`auto` inspects the git diff and picks which tests to run. The mapping
(decide_plan in test.sh) is pure, so we exercise it without running any real
pytest by invoking the script in plan-only mode with an injected file list:

    LIFEOS_TEST_PLAN_ONLY=1 LIFEOS_TEST_CHANGED_FILES="<files>" ./scripts/test.sh auto

The script prints `auto-plan: <plan>` and exits. We assert the plan string.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "test.sh"


def _plan(changed_files: str) -> str:
    """Run test.sh auto in plan-only mode and return the selected plan."""
    env = {
        **os.environ,
        "LIFEOS_TEST_PLAN_ONLY": "1",
        "LIFEOS_TEST_CHANGED_FILES": changed_files,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "auto"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    for line in result.stdout.splitlines():
        if line.startswith("auto-plan:"):
            return line.split("auto-plan:", 1)[1].strip()
    raise AssertionError(f"no auto-plan line in output: {result.stdout!r}")


# (changed_files, expected_plan, id). IDs deliberately avoid the substrings
# "browser"/"playwright"/"integration"/"real_" — conftest's
# pytest_collection_modifyitems auto-marks tests whose *name* contains them,
# which would otherwise deselect these cases from the unit run.
_CASES = [
    # docs-only -> skip
    ("README.md", "skip", "docs_readme"),
    ("docs/guides/scripts.md\ndocs/AGENTS.md", "skip", "docs_multiple"),
    ("CHANGELOG.txt", "skip", "docs_txt"),
    # tests-only -> run just the changed test files
    ("tests/test_settings.py", "files tests/test_settings.py", "tests_one"),
    (
        "tests/test_settings.py\ntests/test_slack_sync.py",
        "files tests/test_settings.py tests/test_slack_sync.py",
        "tests_two",
    ),
    # conftest/helpers affect every test -> full unit suite
    ("tests/conftest.py", "unit", "tests_conftest"),
    ("tests/test_settings.py\ntests/conftest.py", "unit", "tests_plus_conftest"),
    ("tests/fixtures/production_test_data.py", "unit", "tests_fixture"),
    # plain service/config code -> unit
    ("api/services/llm_client.py", "unit", "service_plain"),
    ("config/settings.py", "unit", "config"),
    # frontend -> unit + critical browser test. These paths must be ones that
    # really exist: the mapping previously matched on `static/` and
    # `/templates/`, neither of which is in this repo, and these cases asserted
    # the same fiction — so a web/-only JS change silently skipped the browser
    # scope while the suite stayed green (#518).
    ("api/routes/chat.py", "unit browser", "front_routes"),
    ("web/chat/voice.js", "unit browser", "front_js"),
    ("web/index.html", "unit browser", "front_html"),
    # sync / index -> unit + slow
    ("scripts/run_all_syncs.py", "unit slow", "sync_script"),
    ("api/services/slack_sync.py", "unit slow", "sync_service"),
    ("api/services/embeddings.py", "unit slow", "sync_embeddings"),
    ("api/services/vectorstore.py", "unit slow", "sync_vectorstore"),
    # mixed categories -> additive (covers everything)
    ("api/routes/chat.py\napi/services/embeddings.py", "unit browser slow", "mixed_front_sync"),
    # dependency manifests are code-affecting, not docs -> must run tests
    ("requirements.txt", "unit", "requirements"),
    ("requirements-dev.txt", "unit", "requirements_dev"),
    ("constraints.txt", "unit", "constraints"),
    ("requirements.txt\nREADME.md", "unit", "requirements_plus_docs"),
    # docs mixed with code -> not docs-only, classify by the code
    ("docs/x.md\napi/services/llm_client.py", "unit", "mixed_docs_code"),
    # no changes detected -> safe default
    ("", "unit", "empty"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed,expected",
    [(c, e) for c, e, _ in _CASES],
    ids=[i for _, _, i in _CASES],
)
def test_auto_scope_mapping(changed, expected):
    assert _plan(changed) == expected


# The frontend cases are the ones that broke: they asserted `static/app.js` and
# `api/templates/index.html`, paths this repo has never had, so the mapping and
# its tests agreed on a layout that didn't exist. Pin them to real files so a
# future move of web/ fails here instead of silently narrowing the scope.
@pytest.mark.unit
@pytest.mark.parametrize("path", ["api/routes/chat.py", "web/chat/voice.js", "web/index.html"])
def test_frontend_fixture_paths_exist(path):
    assert (REPO / path).exists(), (
        f"{path} no longer exists — decide_plan's frontend pattern and this "
        f"test's fixtures are out of sync with the repo layout")


# #919: test.sh used to read/write/rm a fixed, shared /tmp/lifeos_test_server.pid
# from a stop_test_server() function — the same cross-worktree collision class
# as #908 (fixed for scripts/pre-push in #913): two concurrent test.sh runs on
# this box would clobber or kill each other's server via that shared path.
# Investigation found the actual defect was narrower than that: nothing in the
# repo ever wrote that file, and stop_test_server() was never called from
# anywhere, so the fix is removal, not a per-run path (see the comment above
# start_server_background() in scripts/test.sh). These two guards cover both
# ways that bug could come back: the fixed shared path itself, and a
# PID-tracking function reappearing without server.sh's port-based lifecycle
# management (get_server_pid/lsof) actually needing one.
@pytest.mark.unit
def test_no_fixed_shared_pid_path():
    text = SCRIPT.read_text()
    assert "/tmp/lifeos_test_server" not in text, (
        "scripts/test.sh reintroduced a fixed shared /tmp PID path — this is "
        "the #908/#913 collision class: concurrent test.sh runs from "
        "different worktrees would clobber or kill each other's server "
        "process. If a PID file is genuinely needed again, key it per-run "
        "the way scripts/pre-push does (see PR #913), not on a fixed path."
    )
    assert "stop_test_server" not in text, (
        "scripts/test.sh reintroduced a PID-tracking function — confirm it "
        "is actually called from somewhere (server.sh already owns the test "
        "server's lifecycle and identifies it by port), and if it's still "
        "unreachable, remove it rather than leaving dead code behind."
    )


@pytest.mark.unit
def test_server_owns_lifecycle_by_port():
    """server.sh -- not test.sh -- identifies and manages the test server,
    and it does so by port (lsof), never a PID file. That's why test.sh
    doesn't need to track a PID of its own at all (see #919)."""
    server_sh = (REPO / "scripts" / "server.sh").read_text()
    assert "get_server_pid()" in server_sh
    assert "lsof -ti" in server_sh
