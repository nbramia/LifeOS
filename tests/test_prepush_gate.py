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


# A dummy 40-hex SHA. It's never resolved as a real commit — every `git diff`/
# `git merge-base` call downstream of the stdin loop tolerates a bogus SHA via
# `|| true` fallbacks — it just needs to look like a SHA so the loop treats
# the line as "carries commits" rather than a deletion (all-zeros) push.
_FAKE_SHA = "1234567890abcdef1234567890abcdef12345678"


def _run_hook(local_ref: str, changed_files: str = "api/main.py"):
    """Run the hook in plan-only mode with a real stdin ref line.

    Feeding a genuine `local_ref local_sha remote_ref remote_sha` line (as git
    itself would) exercises the actual branch-derivation code path, rather
    than a test-only override. `changed_files` still goes through the existing
    override so the run/skip decision doesn't require a real git diff.
    """
    stdin = f"{local_ref} {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n"
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": changed_files,
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        input=stdin,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return result.stdout


def _log_paths(stdout: str) -> tuple[str, str]:
    log = browser_log = None
    for line in stdout.splitlines():
        if line.startswith("prepush-log:"):
            log = line.split("prepush-log:", 1)[1].strip()
        elif line.startswith("prepush-browser-log:"):
            browser_log = line.split("prepush-browser-log:", 1)[1].strip()
    assert log and browser_log, f"missing log lines in output: {stdout!r}"
    return log, browser_log


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


# --- Per-run log paths (#908) ---------------------------------------------
#
# Fixed shared /tmp paths meant two pushes from different worktrees/branches
# clobbered each other's output, and a result could be misattributed to the
# wrong branch. The hook now derives a log path from the pushed branch (and
# the process id), and prints it in plan-only mode as `prepush-log:` /
# `prepush-browser-log:` so path resolution is testable without running pytest.


@pytest.mark.unit
def test_log_paths_differ_by_branch():
    """Two different branches must resolve to two different log paths."""
    log_a, browser_a = _log_paths(_run_hook("refs/heads/feat/alpha"))
    log_b, browser_b = _log_paths(_run_hook("refs/heads/feat/beta"))
    assert log_a != log_b
    assert browser_a != browser_b


@pytest.mark.unit
def test_unit_and_browser_log_paths_differ():
    """A browser-test failure must not clobber the unit-run's output."""
    log, browser_log = _log_paths(_run_hook("refs/heads/feat/gamma"))
    assert log != browser_log


@pytest.mark.unit
def test_same_branch_concurrent_runs_do_not_collide():
    """Two invocations for the SAME branch (different processes = different
    PIDs, as in two concurrent worktrees on the same branch name) must still
    get distinct log paths, so neither run's live output overwrites the other's.
    """
    log1, browser1 = _log_paths(_run_hook("refs/heads/shared-branch-name"))
    log2, browser2 = _log_paths(_run_hook("refs/heads/shared-branch-name"))
    assert log1 != log2, "same-branch runs collided on the unit log path"
    assert browser1 != browser2, "same-branch runs collided on the browser log path"


@pytest.mark.unit
def test_log_path_derivable_from_branch_name():
    """The branch name (sanitised) must appear in the resolved log path, so
    a person can find the right log without guessing."""
    log, browser_log = _log_paths(_run_hook("refs/heads/feat/prepush-log-check"))
    assert "feat_prepush-log-check" in log
    assert "feat_prepush-log-check" in browser_log


@pytest.mark.unit
@pytest.mark.parametrize(
    "local_ref",
    [
        "refs/heads/feat/some-thing",  # slash in branch name
        "refs/heads/../../etc/passwd",  # path traversal attempt
        "refs/heads/" + ("x" * 300),  # very long branch name
    ],
    ids=["slash", "traversal", "very_long"],
)
def test_log_path_stays_inside_log_dir(local_ref):
    """A branch name containing slashes or path-traversal characters must not
    let the resolved log path escape the intended log directory."""
    log, browser_log = _log_paths(_run_hook(local_ref))
    log_dir = Path(log).parent
    browser_dir = Path(browser_log).parent
    # The branch-derived component must land as a single filename inside the
    # log directory, never as extra path segments that walk out of it.
    assert log_dir == browser_dir
    assert ".." not in log_dir.parts, f"traversal in log dir itself: {log_dir}"
    assert Path(log).name not in ("..", ".")
    assert Path(browser_log).name not in ("..", ".")
    # No path separator survives into the filename (it would otherwise create
    # subdirectories under log_dir instead of a single file).
    assert "/" not in Path(log).name
    assert "/" not in Path(browser_log).name


@pytest.mark.unit
def test_console_output_names_log_path():
    """The hook's own source must announce the log path it writes (not just
    on failure) so a run can be traced to its log without a failure occurring.
    Source-inspection is used here (like the --ff/--lf check above) because
    exercising this at runtime would require a full pytest invocation, which
    the test suite for the gate itself must not spawn.
    """
    code = HOOK.read_text(encoding="utf-8")
    assert 'echo "Log: $LOG"' in code, (
        "pre-push must announce the resolved log path before running each suite"
    )
    # And the failure path must still point at the correct (possibly
    # reassigned, for the browser suite) log.
    assert 'Full output: $LOG' in code


@pytest.mark.unit
def test_fallback_branch_when_no_ref_on_stdin():
    """No local_ref on stdin (e.g. empty push) must still resolve to a usable
    log path rather than failing — it falls back to the checked-out branch."""
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "LIFEOS_PREPUSH_HAVE_CONTENT": "1",
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    log, browser_log = _log_paths(result.stdout)
    assert log and browser_log
