"""
Tests for the `./scripts/test.sh auto` diff-aware scope mapping.

`auto` inspects the git diff and picks which tests to run. The mapping
(decide_plan in test.sh) is pure, so we exercise it without running any real
pytest by invoking the script in plan-only mode with an injected file list:

    LIFEOS_TEST_PLAN_ONLY=1 LIFEOS_TEST_CHANGED_FILES="<files>" ./scripts/test.sh auto

The script prints `auto-plan: <plan>` and exits. We assert the plan string.
"""
import os
import re
import shutil
import subprocess
import sys
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


def _dispatch(changed_files: str) -> subprocess.CompletedProcess[str]:
    """Run auto's real one-pass collector without executing test bodies."""
    return subprocess.run(
        ["bash", str(SCRIPT), "auto"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={
            **os.environ,
            "PATH": f"{Path(os.sys.executable).parent}:{os.environ['PATH']}",
            "LIFEOS_TEST_CHANGED_FILES": changed_files,
            "LIFEOS_TEST_DISPATCH_ONLY": "1",
        },
    )


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


@pytest.mark.unit
def test_auto_dispatches_fast_file_and_skips_intentionally_empty_lanes():
    """A fast-only file has five empty lanes but dispatches its real fast IDs."""
    result = _dispatch("tests/test_test_sh_auto.py")
    assert result.returncode == 0, result.stdout + result.stderr
    dispatched = [line for line in result.stdout.splitlines() if line.startswith("auto-dispatch:")]
    # The exact count legitimately grows as this focused module grows; the
    # selector contract is one non-empty fast dispatch and no empty lanes.
    assert len(dispatched) == 1
    assert re.fullmatch(r"auto-dispatch: fast-unit \([1-9][0-9]* tests\)", dispatched[0])


@pytest.mark.unit
def test_ordinary_integration_scope_includes_server_lane():
    """Server-marked integration tests are classified as server, not lost."""
    text = SCRIPT.read_text()
    assert 'run_integration_tests() { run_candidate_verification server,integration; }' in text


@pytest.mark.unit
def test_ordinary_playwright_scope_routes_server_lane_through_owned_verification():
    """run_browser_tests must route browser-server through
    run_candidate_verification's owned-TestInstance adapter, not run_lane's
    production-default check_server/start_server_background path. run_lane
    probes localhost:8000 and can start the shared production server.

    Named without the substring "browser" — conftest.py's
    pytest_collection_modifyitems auto-adds the `browser` marker to any
    test whose *name* contains "browser", which would misclassify this
    fast, source-text-only unit test into the browser-free lane too.
    """
    text = SCRIPT.read_text()
    match = re.search(r"run_browser_tests\(\) \{(.*?)\n\}", text, re.DOTALL)
    assert match, "run_browser_tests function not found in test.sh"
    body = match.group(1)
    assert "run_lane" not in body, (
        "run_browser_tests still routes a lane through run_lane, which probes "
        "localhost:8000 and can start the production server"
    )
    assert "run_candidate_verification browser-free" in body
    assert "run_candidate_verification browser-server" in body


@pytest.mark.unit
def test_auto_dispatches_real_self_contained_ui_file_to_ui_lane():
    """A changed browser file is dispatched via its real marker collection."""
    result = _dispatch("tests/test_voice_mic_block_ui_browser.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(line.startswith("auto-dispatch: browser-free (") for line in result.stdout.splitlines())


@pytest.mark.unit
def test_auto_dispatches_conservative_lane_for_deleted_test_path():
    """A deleted test path cannot be collected, so auto falls back to fast-unit."""
    result = _dispatch("tests/test_deleted_synthetic_case.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "auto-dispatch: fast-unit" in result.stdout


@pytest.mark.unit
def test_auto_dispatches_conservative_lane_for_unknown_test_infrastructure():
    """A changed helper outside the normal test-file convention broadens safely."""
    result = _dispatch("tests/helpers/synthetic_helper.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "auto-dispatch: fast-unit" in result.stdout


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


# scripts/test.sh holds no PID for the server it starts -- not in a file, not
# in a variable. scripts/server.sh owns the server's whole lifecycle and
# identifies the process by port (`get_server_pid` via `lsof -ti :$PORT`), so
# there is nothing for test.sh to track. These two guards keep that shape out
# of test.sh: a fixed shared path, or a PID-tracking helper appearing while
# server.sh's port-based lifecycle management still makes it unnecessary.
#
# This is about the shared production-default server server.sh manages on
# its one fixed port (still reachable via check_server/start_server_background,
# e.g. run_inventory_lane's fallback path) -- a run that hits that path
# genuinely does share and contend for it. It is NOT the current default for
# a server-owned lane: run_candidate_verification's pytest_lane_executor
# spins up its own independent TestInstance (its own ephemeral port) per
# verification whenever a lane's prerequisites include "server", so
# run_unit_tests/run_integration_tests/run_browser_tests concurrently do not
# contend for one shared server.
@pytest.mark.unit
def test_no_fixed_shared_pid_path():
    text = SCRIPT.read_text()
    assert "/tmp/lifeos_test_server" not in text, (
        "scripts/test.sh contains a fixed shared /tmp PID path, which is "
        "unnecessary: server.sh identifies and owns the test server by port, "
        "not by PID, so test.sh has nothing to track. (The shared "
        "production-default server server.sh manages on its one fixed port "
        "is still a single shared resource two direct check_server/"
        "start_server_background callers would contend for -- but an owned "
        "TestInstance, which is what run_candidate_verification actually "
        "uses for a server-owned lane today, gets its own independent port "
        "per verification instead.) If a PID file is genuinely needed, key "
        "it per-run the way scripts/pre-push keys its log path, never a "
        "fixed path."
    )
    assert "stop_test_server" not in text, (
        "scripts/test.sh contains a PID-tracking function: confirm it is "
        "actually called from somewhere (server.sh owns the test server's "
        "lifecycle and identifies it by port), and if it is unreachable, "
        "remove it rather than leaving dead code behind."
    )


@pytest.mark.unit
def test_server_owns_lifecycle_by_port():
    """server.sh -- not test.sh -- identifies and manages the test server,
    and it does so by port (lsof), never a PID file. That's why test.sh
    doesn't need to track a PID of its own at all."""
    server_sh = (REPO / "scripts" / "server.sh").read_text()
    assert "get_server_pid()" in server_sh
    assert "lsof -ti" in server_sh


@pytest.mark.unit
def test_test_sh_playwright_mode_invokes_both_owned_lanes_and_propagates_failure(tmp_path):
    """Command-level proof for the browser-command fix (the source-text
    check above only proves what run_browser_tests's body literally says,
    not what actually happens when it runs): a real `./scripts/test.sh
    browser` invocation, with `python`/`python3` shimmed to intercept just
    the verify_candidate.py calls (harmless — the real verifier never runs;
    everything else, including the real test_lane_registry.py this needs
    to even reach the `browser` dispatch, still runs for real), and `curl`
    plus this checkout's own `server.sh` replaced with shims that fail
    loudly if invoked at all.

    Asserts: both owned-verifier lane calls actually happen (browser-free
    then browser-server, not just browser-free short-circuiting the rest);
    the shimmed browser-server lane's failure exit code propagates as
    test.sh's own overall exit code (set -e, not swallowed or masked by the
    prior successful lane); and neither the production health probe (curl)
    nor the production server start path (server.sh) is ever reached.
    """
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "tests").mkdir()
    for name in ("test.sh", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py"):
        shutil.copy2(REPO / "scripts" / name, checkout / "scripts" / name)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim_log = tmp_path / "shim.log"

    python_shim = (
        "#!/bin/bash\nset -eu\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *verify_candidate.py)\n"
        "      lane=\"\"; prev=\"\"\n"
        "      for a in \"$@\"; do\n"
        "        [ \"$prev\" = \"--lanes\" ] && lane=\"$a\"\n"
        "        prev=\"$a\"\n"
        "      done\n"
        "      echo \"verify_candidate_call:$lane\" >> \"$SHIM_LOG\"\n"
        "      if [ \"$lane\" = \"browser-server\" ]; then\n"
        "        echo \"SHIMMED_VERIFY_CANDIDATE_FAILURE for lane $lane\" >&2\n"
        "        exit 7\n"
        "      fi\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        "exec \"$REAL_PYTHON\" \"$@\"\n"
    )
    for name in ("python", "python3"):
        (bin_dir / name).write_text(python_shim)
        (bin_dir / name).chmod(0o755)

    (bin_dir / "curl").write_text(
        "#!/bin/bash\necho 'FORBIDDEN_CURL_INVOKED' \"$@\" >&2\nexit 1\n"
    )
    (bin_dir / "curl").chmod(0o755)

    # server.sh is invoked by absolute path ($SCRIPT_DIR/server.sh), so a
    # PATH shim can't catch it — replace this checkout's own copy instead of
    # the real one, so a real call would be unambiguous and loud.
    (checkout / "scripts" / "server.sh").write_text(
        "#!/bin/bash\necho 'FORBIDDEN_SERVER_SH_INVOKED' \"$@\" >&2\nexit 1\n"
    )
    (checkout / "scripts" / "server.sh").chmod(0o755)

    home = tmp_path / "home"
    activate = home / ".venvs" / "lifeos" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(":\n")  # no-op; PATH below already puts the shims first

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "SHIM_LOG": str(shim_log),
    }

    result = subprocess.run(
        ["bash", "scripts/test.sh", "browser"], cwd=checkout,
        text=True, capture_output=True, env=env,
    )

    assert "FORBIDDEN_CURL_INVOKED" not in result.stdout + result.stderr, (
        "browser mode must never probe the production health endpoint"
    )
    assert "FORBIDDEN_SERVER_SH_INVOKED" not in result.stdout + result.stderr, (
        "browser mode must never start the production server"
    )

    calls = shim_log.read_text().splitlines() if shim_log.exists() else []
    assert calls == ["verify_candidate_call:browser-free", "verify_candidate_call:browser-server"], (
        f"expected both owned-verifier lane calls in order, got {calls}"
    )

    assert result.returncode == 7, (
        "the shimmed browser-server lane's failure exit code must propagate "
        f"as test.sh's own exit code, got {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )
