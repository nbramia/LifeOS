"""
Tests for the `scripts/pre-push` gate's skip/run decision, its per-run log
paths (#908), and the round-1 review fixes on top of that (#913): a graceful
fallback when the configured log directory is unusable, real runtime
observation of the console output (not a source grep), and pruning coverage.

The hook decides three things before running anything: skip a deletion-only
push, skip a docs-only push, or run the suites. That decision is pure, so we
exercise it without running any real pytest by invoking the hook in plan-only
mode with an injected file list:

    LIFEOS_PREPUSH_PLAN_ONLY=1 LIFEOS_PREPUSH_CHANGED_FILES="<files>" scripts/pre-push

The hook prints `prepush-plan: <decision>` and exits.

Every invocation below sets `TMPDIR` to the test's own `tmp_path`. The log-dir
setup (mkdir + prune) runs unconditionally, even in plan-only mode, so without
this every test in this file would create and prune directories inside the
real, shared `/tmp/lifeos-prepush/` that other agents' live gates write to.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "pre-push"

# A dummy 40-hex SHA. It's never resolved as a real commit — every `git diff`/
# `git merge-base` call downstream of the stdin loop tolerates a bogus SHA via
# `|| true` fallbacks — it just needs to look like a SHA so the loop treats
# the line as "carries commits" rather than a deletion (all-zeros) push.
_FAKE_SHA = "1234567890abcdef1234567890abcdef12345678"


def _decision(tmp_path, changed_files: str, have_content: str = "1") -> str:
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": changed_files,
        "LIFEOS_PREPUSH_HAVE_CONTENT": have_content,
        "TMPDIR": str(tmp_path),
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


def _run_hook(tmp_path, local_ref: str, changed_files: str = "api/main.py"):
    """Run the hook in plan-only mode with a real stdin ref line.

    Feeding a genuine `local_ref local_sha remote_ref remote_sha` line (as git
    itself would) exercises the actual branch-derivation code path, rather
    than a test-only override. `changed_files` still goes through the existing
    override so the run/skip decision doesn't require a real git diff.

    The log-dir setup (mkdir + prune) runs even in plan-only mode, which is
    what makes it possible to test pruning (AC4) and directory pinning (#4)
    without a real pytest invocation.
    """
    stdin = f"{local_ref} {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n"
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": changed_files,
        "TMPDIR": str(tmp_path),
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


# --- Real (non-plan-only) hook execution, via a stubbed `python` ----------
#
# Plan-only mode never writes `$LOG` or runs pytest, so it cannot prove the
# console announces the REAL log path, that a failure names an existing log,
# or that an unusable log directory doesn't block a real run. For that we run
# the actual, unmodified hook to completion with a fake `python` standing in
# for both the playwright-detection probe and the two pytest invocations —
# this takes well under a second and collects zero real tests.


@pytest.fixture
def stub_python(tmp_path):
    """A PATH + HOME pair that lets the real hook run to completion in a
    fraction of a second: a fake `python` that answers the playwright
    detection probe and both pytest invocations, and an empty `HOME` so the
    hook's venv-activation branch never fires (which would otherwise put a
    real python ahead of the stub on PATH).

    Each pytest-invocation stage's simulated exit code/summary line is
    controllable via env vars (STUB_UNIT_EXIT/STUB_UNIT_SUMMARY,
    STUB_BROWSER_EXIT/STUB_BROWSER_SUMMARY), read by the stub at runtime.
    """
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    home_dir = tmp_path / "empty_home"
    home_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text(
        "#!/bin/bash\n"
        "# Detection probe: `python -c \"import playwright\"`.\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "    exit \"${STUB_DETECT_EXIT:-0}\"\n"
        "fi\n"
        "# Otherwise this is one of the two `python -m pytest -m \"...\"` calls;\n"
        "# tell them apart by the marker expression, which always names its stage.\n"
        "args=\"$*\"\n"
        "if [[ \"$args\" == *browser* ]]; then\n"
        "    echo \"${STUB_BROWSER_SUMMARY:-1 passed in 0.01s}\"\n"
        "    exit \"${STUB_BROWSER_EXIT:-0}\"\n"
        "fi\n"
        "echo \"${STUB_UNIT_SUMMARY:-1 passed in 0.01s}\"\n"
        "exit \"${STUB_UNIT_EXIT:-0}\"\n"
    )
    stub.chmod(0o755)
    return {"bin_dir": bin_dir, "home_dir": home_dir}


def _run_real_hook(stub_python, tmp_path, local_ref="refs/heads/feat/real-run",
                    changed_files="api/main.py", extra_env=None, tmpdir=None):
    """Run the REAL, unmodified hook (not plan-only) to completion."""
    stdin = f"{local_ref} {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n"
    env = {
        **os.environ,
        "PATH": f"{stub_python['bin_dir']}:{os.environ['PATH']}",
        "HOME": str(stub_python["home_dir"]),
        "TMPDIR": str(tmpdir if tmpdir is not None else tmp_path),
        "LIFEOS_PREPUSH_CHANGED_FILES": changed_files,
        "LIFEOS_PREPUSH_HAVE_CONTENT": "1",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        input=stdin,
    )


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
def test_prepush_decision(tmp_path, changed, expected):
    assert _decision(tmp_path, changed) == expected


@pytest.mark.unit
def test_deletion_only_push_skips(tmp_path):
    """Deleting a branch sends no commits, so there is nothing to gate.

    This previously ran the full suite: the zero-SHA ref was skipped, leaving
    an empty file list, which falls through to the safe "unknown -> run
    everything" default.
    """
    assert _decision(tmp_path, "", have_content="0") == "skip-deletion"


@pytest.mark.unit
def test_unknown_changes_still_run_everything(tmp_path):
    """An empty file list on a push that *does* carry commits must not skip."""
    assert _decision(tmp_path, "", have_content="1") == "run"


@pytest.mark.unit
def test_hook_agrees_with_test_sh_on_docs_classification(tmp_path):
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
        hook_skips = _decision(tmp_path, changed) == "skip-docs"
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
def test_log_paths_differ_by_branch(tmp_path):
    """Two different branches must resolve to two different log paths."""
    log_a, browser_a = _log_paths(_run_hook(tmp_path, "refs/heads/feat/alpha"))
    log_b, browser_b = _log_paths(_run_hook(tmp_path, "refs/heads/feat/beta"))
    assert log_a != log_b
    assert browser_a != browser_b


@pytest.mark.unit
def test_unit_and_browser_log_paths_differ(tmp_path):
    """A browser-test failure must not clobber the unit-run's output."""
    log, browser_log = _log_paths(_run_hook(tmp_path, "refs/heads/feat/gamma"))
    assert log != browser_log


@pytest.mark.unit
def test_same_branch_concurrent_runs_do_not_collide(tmp_path):
    """Two invocations for the SAME branch (different processes = different
    PIDs, as in two concurrent worktrees on the same branch name) must still
    get distinct log paths, so neither run's live output overwrites the other's.
    """
    log1, browser1 = _log_paths(_run_hook(tmp_path, "refs/heads/shared-branch-name"))
    log2, browser2 = _log_paths(_run_hook(tmp_path, "refs/heads/shared-branch-name"))
    assert log1 != log2, "same-branch runs collided on the unit log path"
    assert browser1 != browser2, "same-branch runs collided on the browser log path"


@pytest.mark.unit
def test_log_path_derivable_from_branch_name(tmp_path):
    """The branch name (sanitised) must appear in the resolved log path, so
    a person can find the right log without guessing."""
    log, browser_log = _log_paths(_run_hook(tmp_path, "refs/heads/feat/prepush-log-check"))
    assert "feat_prepush-log-check" in log
    assert "feat_prepush-log-check" in browser_log


@pytest.mark.unit
def test_log_dir_is_pinned_to_lifeos_prepush(tmp_path):
    """#4: a mutant that widens LOG_DIR to bare `${TMPDIR:-/tmp}` would
    scatter logs straight into /tmp — exactly the mess this feature exists to
    avoid, and it would contradict both AGENTS.md and testing-standards.md,
    which both promise a `lifeos-prepush/` subdirectory."""
    log, browser_log = _log_paths(_run_hook(tmp_path, "refs/heads/feat/pin-check"))
    assert Path(log).parent == tmp_path / "lifeos-prepush"
    assert Path(browser_log).parent == tmp_path / "lifeos-prepush"


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
def test_log_path_stays_inside_log_dir(tmp_path, local_ref):
    """A branch name containing slashes or path-traversal characters must not
    let the resolved log path escape the intended log directory, and the
    80-char slug cap must actually bound the resulting filename length."""
    log, browser_log = _log_paths(_run_hook(tmp_path, local_ref))
    log_dir = Path(log).parent
    browser_dir = Path(browser_log).parent
    # The branch-derived component must land as a single filename inside the
    # log directory, never as extra path segments that walk out of it.
    assert log_dir == browser_dir
    assert ".." not in log_dir.parts, f"traversal in log dir itself: {log_dir}"
    assert Path(log).name not in ("..", ".")
    assert Path(browser_log).name not in ("..", ".")
    # #6: the 80-char slug cap (plus a small "-<pid>-<stage>.log" suffix)
    # bounds the filename length regardless of how long the raw branch name
    # was. A mutant removing `slug="${slug:0:80}"` produces a 300+ char name
    # for the very_long case; this margin is generous enough to never flake
    # on a long PID while still catching that.
    assert len(Path(log).name) <= 120, f"log filename exceeds slug cap: {Path(log).name}"
    assert len(Path(browser_log).name) <= 125, (
        f"browser log filename exceeds slug cap: {Path(browser_log).name}")


@pytest.mark.unit
def test_console_output_names_log_path(stub_python, tmp_path):
    """AC2, proven by running the REAL hook rather than grepping its source.

    A prior version of this test asserted `'echo "Log: $LOG"' in code` —
    disabling the echo at runtime while leaving that exact substring in the
    file left it passing. Running the actual hook end-to-end (with pytest
    itself stubbed out) closes that gap: it asserts on the literal printed
    line, matched against the log files the run actually produced.
    """
    result = _run_real_hook(stub_python, tmp_path, local_ref="refs/heads/feat/ac2-check")
    assert result.returncode == 0, result.stdout + result.stderr

    log_dir = tmp_path / "lifeos-prepush"
    unit_logs = list(log_dir.glob("*-unit.log"))
    browser_logs = list(log_dir.glob("*-browser.log"))
    assert len(unit_logs) == 1, f"expected exactly one unit log, found {unit_logs}"
    assert len(browser_logs) == 1, f"expected exactly one browser log, found {browser_logs}"

    assert f"Log: {unit_logs[0]}" in result.stdout
    assert f"Log: {browser_logs[0]}" in result.stdout
    # The logs must contain the stubbed pytest's actual output, not just exist.
    assert unit_logs[0].read_text().strip() == "1 passed in 0.01s"
    assert browser_logs[0].read_text().strip() == "1 passed in 0.01s"


@pytest.mark.unit
def test_real_unit_failure_names_existing_log_with_failure_text(stub_python, tmp_path):
    """A real failing unit run must fail the push, name a log that actually
    exists, and stop before the browser stage ever runs."""
    result = _run_real_hook(
        stub_python, tmp_path, local_ref="refs/heads/feat/unit-fail-check",
        extra_env={"STUB_UNIT_EXIT": "1", "STUB_UNIT_SUMMARY": "1 failed in 0.01s"},
    )
    assert result.returncode == 1

    log_dir = tmp_path / "lifeos-prepush"
    unit_logs = list(log_dir.glob("*-unit.log"))
    assert len(unit_logs) == 1
    assert unit_logs[0].exists()
    assert "FAILED (1 failed in 0.01s)" in result.stdout
    assert f"Full output: {unit_logs[0]}" in result.stdout
    # The browser stage must never start once the unit stage has failed.
    assert not list(log_dir.glob("*-browser.log"))


@pytest.mark.unit
def test_real_browser_failure_does_not_clobber_unit_log(stub_python, tmp_path):
    """A browser-stage failure must name the BROWSER log, and the unit log
    (from the same run, which passed) must survive untouched."""
    result = _run_real_hook(
        stub_python, tmp_path, local_ref="refs/heads/feat/browser-fail-check",
        extra_env={"STUB_BROWSER_EXIT": "1", "STUB_BROWSER_SUMMARY": "1 failed in 0.02s"},
    )
    assert result.returncode == 1

    log_dir = tmp_path / "lifeos-prepush"
    unit_logs = list(log_dir.glob("*-unit.log"))
    browser_logs = list(log_dir.glob("*-browser.log"))
    assert len(unit_logs) == 1
    assert unit_logs[0].read_text().strip() == "1 passed in 0.01s", (
        "a browser failure must not clobber the unit log")
    assert len(browser_logs) == 1
    assert "FAILED (1 failed in 0.02s)" in result.stdout
    assert f"Full output: {browser_logs[0]}" in result.stdout


@pytest.mark.unit
def test_fallback_branch_when_no_ref_on_stdin(tmp_path):
    """No local_ref on stdin (e.g. empty push) must still resolve to a usable
    log path rather than failing — it falls back to the checked-out branch."""
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "LIFEOS_PREPUSH_HAVE_CONTENT": "1",
        "TMPDIR": str(tmp_path),
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    log, browser_log = _log_paths(result.stdout)
    assert log and browser_log


# --- Round-1 review fixes (#913) -------------------------------------------
#
# Finding A: an unwritable/occupied configured log directory must degrade to
# a working fallback location rather than blocking the push. Finding C: the
# pruning behaviour (AC4) had no test at all. Finding G: the prune must be
# scoped to this hook's own *.log files.


def _make_occupied_by_file(hostile_tmpdir: Path) -> None:
    """$TMPDIR/lifeos-prepush already exists as a regular file."""
    hostile_tmpdir.mkdir(exist_ok=True)
    (hostile_tmpdir / "lifeos-prepush").write_text("occupied")


def _make_unwritable_log_dir(hostile_tmpdir: Path) -> None:
    """$TMPDIR/lifeos-prepush exists but is not writable (mode 500)."""
    hostile_tmpdir.mkdir(exist_ok=True)
    log_dir = hostile_tmpdir / "lifeos-prepush"
    log_dir.mkdir()
    log_dir.chmod(0o500)


def _make_readonly_tmpdir(hostile_tmpdir: Path) -> None:
    """$TMPDIR itself is read-only, so lifeos-prepush can never be created."""
    hostile_tmpdir.mkdir(exist_ok=True)
    hostile_tmpdir.chmod(0o500)


def _restore_writable(hostile_tmpdir: Path) -> None:
    """Undo the hostile permissions so pytest's tmp_path cleanup can proceed."""
    if not hostile_tmpdir.exists():
        return
    hostile_tmpdir.chmod(0o700)
    log_dir = hostile_tmpdir / "lifeos-prepush"
    if log_dir.is_dir():
        log_dir.chmod(0o700)


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_hostile",
    [_make_occupied_by_file, _make_unwritable_log_dir, _make_readonly_tmpdir],
    ids=["occupied_by_file", "unwritable_log_dir", "readonly_tmpdir"],
)
def test_unwritable_log_dir_push_still_completes(tmp_path, stub_python, make_hostile):
    """Finding A/#1: each of the three real-world triggers reported in
    review (LOG_DIR occupied by a file, LOG_DIR existing but unwritable, and
    a read-only TMPDIR) must still let a real (stubbed) test run complete and
    pass, degrading to a fallback directory instead of reporting a fake
    `FAILED ()` for an unrelated filesystem problem.

    Uses LIFEOS_PREPUSH_TEST_FALLBACK_DIR (a test-only override, unset in
    production) to point the fallback at a scratch directory rather than the
    real, shared /tmp/lifeos-prepush.
    """
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    make_hostile(hostile_tmpdir)
    fallback_dir = tmp_path / "fallback"
    try:
        result = _run_real_hook(
            stub_python, tmp_path, tmpdir=hostile_tmpdir,
            local_ref="refs/heads/feat/hostile-e2e",
            extra_env={"LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(fallback_dir)},
        )
        assert result.returncode == 0, (
            f"push blocked by an unusable log directory: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "is unusable; using" in result.stdout
        unit_logs = list(fallback_dir.glob("*-unit.log"))
        browser_logs = list(fallback_dir.glob("*-browser.log"))
        assert len(unit_logs) == 1, f"expected one unit log in fallback dir, found {unit_logs}"
        assert len(browser_logs) == 1, (
            f"expected one browser log in fallback dir, found {browser_logs}")
        assert "passed" in result.stdout
    finally:
        _restore_writable(hostile_tmpdir)


@pytest.mark.unit
def test_unwritable_log_dir_plan_only_resolves_into_fallback(tmp_path):
    """The same fallback, exercised in plan-only mode (fast, no stub needed):
    the resolved prepush-log path must land inside the fallback directory,
    not the unusable configured one."""
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    _make_occupied_by_file(hostile_tmpdir)
    fallback_dir = tmp_path / "fallback"
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "TMPDIR": str(hostile_tmpdir),
        "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(fallback_dir),
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=str(REPO),
        input=f"refs/heads/hostile-plan-only {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n",
    )
    assert result.returncode == 0
    log, browser_log = _log_paths(result.stdout)
    assert Path(log).parent == fallback_dir
    assert Path(browser_log).parent == fallback_dir


@pytest.mark.unit
def test_prune_removes_old_logs_but_keeps_recent(tmp_path):
    """AC4: logs past the retention window are pruned; a fresh log (as a live
    run's own output always is) must survive."""
    log_dir = tmp_path / "lifeos-prepush"
    log_dir.mkdir()
    old_log = log_dir / "old-run-42-unit.log"
    old_log.write_text("stale")
    recent_log = log_dir / "recent-run-43-unit.log"
    recent_log.write_text("fresh")
    old_time = time.time() - (5 * 86400)  # 5 days old, clear of the +3 window
    os.utime(old_log, (old_time, old_time))
    # recent_log keeps its natural just-created mtime.

    _run_hook(tmp_path, "refs/heads/feat/prune-check")

    assert not old_log.exists(), "prune did not remove a 5-day-old log"
    assert recent_log.exists(), "prune removed a fresh log"


@pytest.mark.unit
def test_prune_only_touches_its_own_log_files(tmp_path):
    """Finding #9/G: the prune must be scoped to this hook's own *.log files,
    not sweep every old file that happens to live in the directory."""
    log_dir = tmp_path / "lifeos-prepush"
    log_dir.mkdir()
    old_log = log_dir / "old-run-99-unit.log"
    old_other = log_dir / "old-unrelated.txt"
    old_log.write_text("stale log")
    old_other.write_text("not a log")
    old_time = time.time() - (5 * 86400)
    os.utime(old_log, (old_time, old_time))
    os.utime(old_other, (old_time, old_time))

    _run_hook(tmp_path, "refs/heads/feat/prune-scope-check")

    assert not old_log.exists(), "prune did not remove a 5-day-old log"
    assert old_other.exists(), "prune deleted a non-.log file it must not touch"
