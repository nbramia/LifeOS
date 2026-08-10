"""
Tests for the `scripts/pre-push` gate's skip/run decision.

The hook decides three things before running anything: skip a deletion-only
push, skip a docs-only push, or run the suites. That decision is pure, so we
exercise it without running any real pytest by invoking the hook in plan-only
mode with an injected file list:

    LIFEOS_PREPUSH_PLAN_ONLY=1 LIFEOS_PREPUSH_CHANGED_FILES="<files>" scripts/pre-push

The hook prints `prepush-plan: <decision>` and exits.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "pre-push"


def _decision(changed_files: str, have_content: str = "1") -> str:
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": changed_files,
        "LIFEOS_PREPUSH_HAVE_CONTENT": have_content,
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    for line in result.stdout.splitlines():
        if line.startswith("prepush-plan:"):
            return line.split("prepush-plan:", 1)[1].strip()
    raise AssertionError(f"no prepush-plan line in output: {result.stdout!r}")


_CASES = [
    # docs-only -> skip
    ("README.md", "skip-docs", "docs_readme"),
    ("docs/guides/scheduler.md\ndocs/AGENTS.md", "skip-docs", "docs_multiple"),
    # Dependency manifests are code-affecting, NOT docs. The hook used to carry
    # its own `\.(md|txt|rst)$` regex, which matched requirements.txt and
    # skipped the entire suite on a dep bump — while test.sh's decide_plan
    # deliberately excluded them. These pin the two back together.
    ("requirements.txt", "run", "requirements"),
    ("requirements-dev.txt", "run", "requirements_dev"),
    ("constraints.txt", "run", "constraints"),
    ("requirements.txt\nREADME.md", "run", "requirements_plus_docs"),
    # code -> run
    ("api/main.py", "run", "code_api"),
    ("web/chat/voice.js", "run", "code_web_js"),
    ("docs/x.md\napi/services/llm_client.py", "run", "mixed_docs_code"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed,expected",
    [(c, e) for c, e, _ in _CASES],
    ids=[i for _, _, i in _CASES],
)
def test_prepush_decision(changed, expected):
    assert _decision(changed) == expected


@pytest.mark.unit
def test_deletion_only_push_skips():
    """Deleting a branch sends no commits, so there is nothing to gate.

    This previously ran the full suite: the zero-SHA ref was skipped, leaving
    an empty file list, which falls through to the safe "unknown -> run
    everything" default.
    """
    assert _decision("", have_content="0") == "skip-deletion"


@pytest.mark.unit
def test_unknown_changes_still_run_everything():
    """An empty file list on a push that *does* carry commits must not skip."""
    assert _decision("", have_content="1") == "run"


@pytest.mark.unit
def test_hook_agrees_with_test_sh_on_docs_classification():
    """The hook must not reintroduce its own copy of the docs rule.

    It delegates to test.sh's decide_plan; this pins that the two agree on the
    input that made them drift, so a future edit to either can't silently
    reopen the gap.
    """
    for changed in ("requirements.txt", "README.md", "api/main.py"):
        plan = subprocess.run(
            ["bash", str(REPO / "scripts" / "test.sh"), "auto"],
            capture_output=True, text=True, cwd=str(REPO),
            env={**os.environ, "LIFEOS_TEST_PLAN_ONLY": "1",
                 "LIFEOS_TEST_CHANGED_FILES": changed},
        ).stdout
        test_sh_skips = "auto-plan: skip" in plan
        hook_skips = _decision(changed) == "skip-docs"
        assert hook_skips == test_sh_skips, (
            f"{changed}: hook skip={hook_skips} but test.sh skip={test_sh_skips}")


@pytest.mark.unit
def test_gate_is_not_narrowed_by_lastfailed():
    """`--lf` must not come back.

    It restricted the run to only previously-failed tests whenever a
    lastfailed cache existed, so a fix-then-push ran a handful of tests out of
    ~2400 and passed. `--ff` (ordering only) is the intended behaviour.
    """
    # Comments are stripped: the hook explains at length *why* --lf is gone,
    # and that prose must not trip this check.
    code = "\n".join(
        line for line in HOOK.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--lf" not in code, "pre-push must not deselect tests via --lf"
    assert "--ff" in code, "pre-push should still order previously-failed tests first"
