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
import shutil
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

    Two more unit-stage-only modes exercise `fail()`'s other two branches
    (round-1 review finding B), which a normal failing run can't reach
    because bash's `> "$LOG"` redirection always creates the file before the
    stub ever runs, even if the stub prints nothing:

    - STUB_UNIT_NO_OUTPUT=1: exit 1 without printing anything, so `$LOG`
      exists but is empty ("empty log" branch).
    - STUB_UNIT_DELETE_LOG=1: delete the just-opened `$LOG` file (found by
      globbing $TMPDIR/lifeos-prepush for the one `*-unit.log` this run just
      created) before exiting, so `$LOG` doesn't exist at all when `fail()`
      runs ("no log at all" branch). Only meaningful when the run resolves
      to the default, unhostile log directory.
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
        "if [ \"${STUB_UNIT_DELETE_LOG:-0}\" = \"1\" ]; then\n"
        "    find \"${TMPDIR:-/tmp}/lifeos-prepush\" -maxdepth 1 -name '*-unit.log' -delete 2>/dev/null\n"
        "    exit \"${STUB_UNIT_EXIT:-1}\"\n"
        "fi\n"
        "if [ \"${STUB_UNIT_NO_OUTPUT:-0}\" != \"1\" ]; then\n"
        "    echo \"${STUB_UNIT_SUMMARY:-1 passed in 0.01s}\"\n"
        "fi\n"
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
    # Round 2 (#913 finding C): the unconditional `chmod 700` must actually
    # land on the directory this hook owns and normally uses.
    assert log_dir.stat().st_mode & 0o777 == 0o700, (
        "the owned log directory must be tightened to 0700")
    # Round 4 (#908 finding 1): the happy (owned-directory) path must keep
    # naming logs from the branch slug alone — `LOG_FILE_PREFIX` is only for
    # the not-owned last-resort case (test_last_resort_dir_is_never_chmod_or_pruned
    # covers that direction). A mutant that applies the prefix unconditionally,
    # or to only one of the two logs, would otherwise slip through undetected.
    assert not unit_logs[0].name.startswith("lifeos-prepush-"), (
        "the happy-path unit log must not carry the not-owned-dir prefix")
    assert not browser_logs[0].name.startswith("lifeos-prepush-"), (
        "the happy-path browser log must not carry the not-owned-dir prefix")
    assert unit_logs[0].name.startswith("feat_ac2-check-"), (
        "the happy-path unit log should be named from the branch slug")
    assert browser_logs[0].name.startswith("feat_ac2-check-"), (
        "the happy-path browser log should be named from the branch slug")
    # Round 4 (#908 finding 2): the happy path must never print a
    # degraded-directory line, and the console output must be exactly these
    # four lines — no more, no fewer — so an extra line anywhere (e.g. a
    # spuriously injected degraded-path message) is caught.
    for phrase in ("is unusable", "is a symlink", "every fallback failed"):
        assert phrase not in result.stdout, (
            f"happy path must not print a degraded-path message: {phrase!r}")
    assert result.stdout.splitlines() == [
        f"Log: {unit_logs[0]}",
        "Running unit tests... passed (1 passed in 0.01s)",
        f"Log: {browser_logs[0]}",
        "Running server-free browser tests... passed (1 passed in 0.01s)",
    ]


@pytest.mark.unit
def test_preexisting_log_dir_is_tightened_to_0700(stub_python, tmp_path):
    """Finding C: `mkdir -p -m 700` only sets the mode when it CREATES the
    directory — a no-op `mkdir -p` on one that already exists never touches
    its mode. This is exactly why the unconditional `chmod 700` exists
    (comment: "a directory left behind world-writable... would never get
    tightened"), but every other test's log directory is freshly created
    by the hook itself, so `-m 700` alone already satisfies them and a
    `chmod 700 -> true` mutation slips through undetected. Pre-create the
    directory with a permissive mode BEFORE running the hook to force the
    unconditional chmod to be the only thing that can fix it.
    """
    log_dir = tmp_path / "lifeos-prepush"
    log_dir.mkdir(mode=0o777)
    os.chmod(log_dir, 0o777)  # mkdir(mode=...) is affected by umask; force it

    result = _run_real_hook(stub_python, tmp_path, local_ref="refs/heads/feat/preexisting-mode-check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert log_dir.stat().st_mode & 0o777 == 0o700, (
        "a pre-existing, permissive log directory must be tightened to 0700")


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


# Round-2 (#913 finding F): the fallback message must name WHY the
# configured directory was unusable, not just that it was. One expected
# reason string per hostile fixture above.
_REASON_FOR_MAKER = {
    _make_occupied_by_file: "not a directory",
    _make_unwritable_log_dir: "not writable",
    _make_readonly_tmpdir: "could not be created",
}


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
    `FAILED ()` for an unrelated filesystem problem. Round 2 (finding F): the
    printed line must also say WHY the configured directory was unusable.
    Round 2 (finding A/C): the adopted fallback directory is OWNED by this
    hook, so it must be tightened to 0700 (positive half of the
    owned/not-owned pair; see test_last_resort_dir_is_never_chmod_or_pruned
    for the not-owned negative half).

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
        expected_reason = _REASON_FOR_MAKER[make_hostile]
        assert f"is unusable ({expected_reason}); using" in result.stdout, result.stdout
        unit_logs = list(fallback_dir.glob("*-unit.log"))
        browser_logs = list(fallback_dir.glob("*-browser.log"))
        assert len(unit_logs) == 1, f"expected one unit log in fallback dir, found {unit_logs}"
        assert len(browser_logs) == 1, (
            f"expected one browser log in fallback dir, found {browser_logs}")
        assert "passed" in result.stdout
        assert fallback_dir.stat().st_mode & 0o777 == 0o700, (
            "an owned fallback directory must be tightened to 0700")
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


# --- Round-2 review fixes (#913 round 2) -----------------------------------
#
# Finding A: never chmod/prune a directory the hook does not own (the bare
# `/tmp` last resort, and any symlinked $LOG_DIR at any rung). Finding B:
# `fail()`'s empty-log and no-log-at-all branches were untested. Finding C:
# the unconditional `chmod 700` itself was untested, and needs a positive
# (owned dir tightened) AND negative (not-owned dir left alone) pair.
# Finding E: fallback rungs 3/4 and the equality guard were unreachable.
# Finding F: the fallback message must name why, and say if the fixed
# fallback also failed.


@pytest.mark.unit
def test_fail_reports_empty_log(stub_python, tmp_path):
    """Finding B: `fail()`'s "empty log" branch. The redirection `> "$LOG"`
    always creates the file even if the command writes nothing, so this is
    the realistic way a log ends up empty: the stubbed pytest exits 1 having
    produced no output at all. Reverting the whole `fail()` rewrite to the
    pre-round-1 two-liner, or deleting just this branch, must fail this test.
    """
    result = _run_real_hook(
        stub_python, tmp_path, local_ref="refs/heads/feat/empty-log-check",
        extra_env={"STUB_UNIT_EXIT": "1", "STUB_UNIT_NO_OUTPUT": "1"},
    )
    assert result.returncode == 1

    log_dir = tmp_path / "lifeos-prepush"
    unit_logs = list(log_dir.glob("*-unit.log"))
    assert len(unit_logs) == 1
    assert unit_logs[0].exists()
    assert unit_logs[0].stat().st_size == 0
    assert "FAILED (empty log — command likely failed before producing output)" in result.stdout
    assert f"Full output: {unit_logs[0]}" in result.stdout


@pytest.mark.unit
def test_fail_reports_missing_log(stub_python, tmp_path):
    """Finding B: `fail()`'s "no log at all" branch. A previous report
    claimed the readonly_tmpdir hostile case covered this indirectly — false,
    since with the fallback in place that case reaches a usable directory and
    `fail()` is never called. Simulate the log genuinely not existing by
    having the stubbed pytest delete its own just-opened `$LOG` file (the
    shell already created it via `> "$LOG"` redirection; the stub unlinks
    that path before exiting), so by the time `fail()` runs, `[ -e "$LOG" ]`
    is false.
    """
    result = _run_real_hook(
        stub_python, tmp_path, local_ref="refs/heads/feat/missing-log-check",
        extra_env={"STUB_UNIT_DELETE_LOG": "1"},
    )
    assert result.returncode == 1

    log_dir = tmp_path / "lifeos-prepush"
    assert not list(log_dir.glob("*-unit.log")), "the log should have been deleted by the stub"
    assert "FAILED (no log was written to" in result.stdout
    # The "Full output:" line is gated on the log existing — it must not
    # print a path that doesn't exist.
    assert "Full output:" not in result.stdout


@pytest.mark.unit
def test_symlinked_log_dir_is_used_but_not_owned(stub_python, tmp_path):
    """Finding A/#6: a symlinked $LOG_DIR must still be used for writing
    (never block the push) but must never be chmod'd or pruned, since chmod
    follows symlinks and would retarget the tightening onto whatever the
    symlink actually points at. It must also NOT fall through to the fixed
    fallback — that directory is perfectly usable on its own terms, so
    falling through anyway would, in production (no test override), risk
    landing on the real, shared /tmp/lifeos-prepush for no reason.
    """
    real_target = tmp_path / "real_target"
    real_target.mkdir()
    real_target.chmod(0o777)
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    hostile_tmpdir.mkdir()
    (hostile_tmpdir / "lifeos-prepush").symlink_to(real_target)

    bin_dir = tmp_path / "recstubbin"
    bin_dir.mkdir()
    calls_file = tmp_path / "calls.log"
    (bin_dir / "chmod").write_text(f'#!/bin/bash\necho "chmod $*" >> {calls_file}\nexit 0\n')
    (bin_dir / "find").write_text(f'#!/bin/bash\necho "find $*" >> {calls_file}\nexit 0\n')
    (bin_dir / "chmod").chmod(0o755)
    (bin_dir / "find").chmod(0o755)
    not_used = tmp_path / "should_not_be_used"

    result = _run_real_hook(
        stub_python, tmp_path, tmpdir=hostile_tmpdir,
        local_ref="refs/heads/feat/symlink-check",
        extra_env={
            "PATH": f"{bin_dir}:{stub_python['bin_dir']}:{os.environ['PATH']}",
            "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(not_used),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is a symlink; writing there" in result.stdout
    assert not not_used.exists(), (
        "a symlinked (but usable) configured dir must not fall through to the fixed fallback")
    unit_logs = list(real_target.glob("*-unit.log"))
    assert len(unit_logs) == 1, f"expected the log inside the symlink target, found {unit_logs}"
    assert not calls_file.exists(), "a symlinked directory must never be chmod'd or pruned"
    assert real_target.stat().st_mode & 0o777 == 0o777, "the symlink target's mode must be untouched"


@pytest.mark.unit
def test_last_resort_dir_is_never_chmod_or_pruned(tmp_path):
    """Finding A: when every fallback rung fails (configured dir occupied,
    fixed fallback occupied, and `mktemp` itself unavailable), the hook must
    still resolve to a usable location (the literal `/tmp`) without blocking
    the push, but that directory is NOT owned by this hook and must never be
    chmod'd or pruned — the negative half of the owned/not-owned pair (see
    test_unwritable_log_dir_push_still_completes for the positive half).

    Stubs `mktemp` to always fail (forcing the true last-resort literal) and
    no-op stubs `chmod`/`find` (record-only, no real syscall) so this test —
    and its mutation proof, which temporarily breaks the ownership gate — can
    never actually touch a real filesystem location, however the (possibly
    broken) gate resolves.
    """
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    hostile_tmpdir.mkdir()
    (hostile_tmpdir / "lifeos-prepush").write_text("occupied")
    fallback_path = tmp_path / "fallback_occupied"
    fallback_path.write_text("occupied")

    bin_dir = tmp_path / "stubbin_lastresort"
    bin_dir.mkdir()
    (bin_dir / "mktemp").write_text("#!/bin/bash\nexit 1\n")
    (bin_dir / "mktemp").chmod(0o755)
    calls_file = tmp_path / "calls.log"
    (bin_dir / "chmod").write_text(f'#!/bin/bash\necho "chmod $*" >> {calls_file}\nexit 0\n')
    (bin_dir / "find").write_text(f'#!/bin/bash\necho "find $*" >> {calls_file}\nexit 0\n')
    (bin_dir / "chmod").chmod(0o755)
    (bin_dir / "find").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "TMPDIR": str(hostile_tmpdir),
        "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(fallback_path),
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=str(REPO),
        input=f"refs/heads/last-resort-check {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log, browser_log = _log_paths(result.stdout)
    assert Path(log).parent == Path("/tmp"), f"expected the literal /tmp last resort, got {log}"
    assert "every fallback failed" in result.stdout
    assert "not owned by this hook" in result.stdout
    assert not calls_file.exists(), (
        f"chmod/find must never run against the un-owned last resort: "
        f"{calls_file.read_text() if calls_file.exists() else ''}")
    # Round 4 (#908 finding 1): the not-owned last resort is exactly where
    # `LOG_FILE_PREFIX` must apply, so a log dropped in a shared, unswept
    # directory is still attributable as this hook's. The other half of this
    # assertion (the prefix must NOT appear on the owned/happy path) lives in
    # test_console_output_names_log_path.
    assert Path(log).name.startswith("lifeos-prepush-"), (
        "the not-owned last-resort unit log must carry the attribution prefix")
    assert Path(browser_log).name.startswith("lifeos-prepush-"), (
        "the not-owned last-resort browser log must carry the attribution prefix")


@pytest.mark.unit
def test_equality_guard_skips_redundant_fallback_retry(tmp_path):
    """Finding E: `[ "$FALLBACK_DIR" = "$UNUSABLE_LOG_DIR" ]` is otherwise
    unreachable in the suite because every other fallback test points the
    override at a distinct, fresh, writable directory. Set the override to
    the EXACT SAME (already-known-unusable) path as the configured
    directory — the common production shape when TMPDIR is unset, since both
    rungs then default to the same `/tmp/lifeos-prepush` — and check the
    printed reason: with the guard, the retry is skipped and the message
    says so; without it, the code would retry the identical occupied path
    and report its usual "not a directory" reason instead.
    """
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    hostile_tmpdir.mkdir()
    unusable = hostile_tmpdir / "lifeos-prepush"
    unusable.write_text("occupied")

    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "TMPDIR": str(hostile_tmpdir),
        "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(unusable),
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=str(REPO),
        input=f"refs/heads/equality-guard-check {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n",
    )
    assert result.returncode == 0
    assert "(same directory; not retried)" in result.stdout, result.stdout
    log, _ = _log_paths(result.stdout)
    assert Path(log).parent.name.startswith("lifeos-prepush."), (
        "expected the throwaway mktemp rung to be reached")


@pytest.mark.unit
def test_distinct_fixed_fallback_failure_reaches_mktemp_rung(tmp_path):
    """Finding E/#8: rung 3 (`mktemp -d` with the templated name) was
    previously unreachable in the suite because every fallback test pointed
    the override at a fresh, writable directory. Make BOTH the configured
    directory and the (distinct) fixed fallback genuinely unusable, so the
    hook must fall all the way through to the throwaway
    `mktemp -d ".../lifeos-prepush.XXXXXX"` rung, and assert its distinctive
    naming shape and that the message names the fixed fallback's own reason.

    Round 4 (#908 finding 3): the configured directory and the fixed
    fallback must fail for DIFFERENT reasons, so the "the fixed fallback's
    own reason" assertion below is actually exercising the fallback's
    message rather than being satisfied by the configured directory's
    identical-looking one. The configured directory is a plain file ("not a
    directory"); the fixed fallback is a directory that exists but can't be
    written to ("not writable") — dropping the fallback's reason entirely
    would previously leave this test green because both messages read
    "(not a directory)".
    """
    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    hostile_tmpdir.mkdir()
    (hostile_tmpdir / "lifeos-prepush").write_text("occupied")
    fallback_path = tmp_path / "also_occupied_fallback"
    fallback_path.mkdir()
    fallback_path.chmod(0o500)

    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "TMPDIR": str(hostile_tmpdir),
        "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(fallback_path),
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=str(REPO),
        input=f"refs/heads/distinct-fallback-fail {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n",
    )
    assert result.returncode == 0
    assert "the fixed fallback" in result.stdout
    assert f"({fallback_path})" in result.stdout
    assert "(not a directory)" in result.stdout  # the configured dir's own reason
    assert "(not writable)" in result.stdout  # the fixed fallback's own, distinct reason
    log, browser_log = _log_paths(result.stdout)
    resolved_dir = Path(log).parent
    assert resolved_dir == Path(browser_log).parent
    assert resolved_dir.name.startswith("lifeos-prepush."), (
        f"expected the templated mktemp naming shape, got {resolved_dir}")
    assert resolved_dir.parent == hostile_tmpdir
    assert resolved_dir.stat().st_mode & 0o777 == 0o700, "the mktemp rung directory is owned"


@pytest.mark.unit
def test_bare_mktemp_rung_reached_when_templated_mktemp_fails(tmp_path):
    """Finding E/#8: the SECOND, bare `mktemp -d` call (no custom template)
    is a distinct fallback rung from the templated one above, reached only
    if the templated `mktemp -d ".../lifeos-prepush.XXXXXX"` itself fails.
    Stub `mktemp` to fail only for calls carrying that template and delegate
    everything else (the bare `-d` call) to the real binary, so the hook
    must resolve to the bare-mktemp naming shape (`tmp.XXXXXXXXXX`-style,
    not `lifeos-prepush.XXXXXX`) instead.
    """
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp, "this test needs a real `mktemp` on PATH"

    hostile_tmpdir = tmp_path / "hostile_tmpdir"
    hostile_tmpdir.mkdir()
    (hostile_tmpdir / "lifeos-prepush").write_text("occupied")
    fallback_path = tmp_path / "also_occupied_fallback"
    fallback_path.write_text("occupied")

    bin_dir = tmp_path / "stubbin_rung4"
    bin_dir.mkdir()
    (bin_dir / "mktemp").write_text(
        "#!/bin/bash\n"
        "if [[ \"$*\" == *lifeos-prepush* ]]; then\n"
        "    exit 1\n"
        "fi\n"
        f"exec {real_mktemp} \"$@\"\n"
    )
    (bin_dir / "mktemp").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "api/main.py",
        "TMPDIR": str(hostile_tmpdir),
        "LIFEOS_PREPUSH_TEST_FALLBACK_DIR": str(fallback_path),
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=str(REPO),
        input=f"refs/heads/rung4-check {_FAKE_SHA} refs/heads/unused {_FAKE_SHA}\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log, browser_log = _log_paths(result.stdout)
    resolved_dir = Path(log).parent
    assert resolved_dir == Path(browser_log).parent
    assert not resolved_dir.name.startswith("lifeos-prepush"), (
        f"expected the BARE mktemp naming shape (the templated one should "
        f"have failed), got {resolved_dir}")
    assert resolved_dir.parent == hostile_tmpdir, "bare `mktemp -d` should still honour TMPDIR"
    assert resolved_dir.stat().st_mode & 0o777 == 0o700
