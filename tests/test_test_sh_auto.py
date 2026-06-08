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
    # frontend -> unit + critical browser test
    ("api/routes/chat.py", "unit browser", "front_routes"),
    ("static/app.js", "unit browser", "front_js"),
    ("api/templates/index.html", "unit browser", "front_html"),
    # sync / index -> unit + slow
    ("scripts/run_all_syncs.py", "unit slow", "sync_script"),
    ("api/services/slack_sync.py", "unit slow", "sync_service"),
    ("api/services/embeddings.py", "unit slow", "sync_embeddings"),
    ("api/services/vectorstore.py", "unit slow", "sync_vectorstore"),
    # mixed categories -> additive (covers everything)
    ("api/routes/chat.py\napi/services/embeddings.py", "unit browser slow", "mixed_front_sync"),
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
