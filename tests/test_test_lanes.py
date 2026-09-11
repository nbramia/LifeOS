"""Real pytest collection coverage for the shared test-lane registry."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from scripts.test_lane_registry import LANES as LANE_DEFINITIONS
from scripts.test_lane_registry import classify, marker


REPO = Path(__file__).resolve().parent.parent
LANES = ("fast-unit", "slow", "browser-free", "browser-server", "server", "integration")


@pytest.mark.unit
@pytest.mark.parametrize(
    "markers",
    [
        {name for bit, name in enumerate(("unit", "slow", "browser", "requires_server", "integration")) if mask & (1 << bit)}
        for mask in range(32)
    ],
)
def test_registry_classification_matches_pytest_marker_expressions(markers):
    """All marker combinations use the exact predicates passed to pytest."""
    matching = [
        lane["name"]
        for lane in LANE_DEFINITIONS
        if Expression.compile(marker(lane)).evaluate(lambda name: name in markers)
    ]
    assert len(matching) <= 1
    assert classify(markers) == (matching[0] if matching else None)


def _collect(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Collect a real temporary test module through the lane plugin."""
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    test_file = cases / "test_lane_cases.py"
    test_file.write_text(source)
    inventory = tmp_path / "lane-inventory.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file), "--collect-only", "-qq",
            "-p", "scripts.test_lane_plugin", "--lifeos-lane-inventory", str(inventory),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    return result, json.loads(inventory.read_text())


@pytest.mark.unit
def test_plugin_partitions_real_mixed_marker_collection_once(tmp_path):
    """Explicit mixed markers follow precedence and expose lane prerequisites."""
    result, inventory = _collect(
        tmp_path,
        """
import pytest

@pytest.mark.unit
def test_fast(): pass

@pytest.mark.slow
def test_slow(): pass

@pytest.mark.integration
def test_integration_without_server(): pass

@pytest.mark.integration
@pytest.mark.requires_server
def test_server_beats_integration(): pass

@pytest.mark.browser
@pytest.mark.integration
def test_browser_beats_integration(): pass

@pytest.mark.browser
@pytest.mark.requires_server
@pytest.mark.integration
def test_browser_server_beats_all(): pass
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert inventory["status"] == "ok"
    assert inventory["collected_count"] == inventory["assigned_count"] == 6
    memberships = {
        lane: inventory["lanes"][lane]["nodeids"]
        for lane in LANES
    }
    test_prefix = "test_lane_cases.py::"
    assert memberships["fast-unit"] == [test_prefix + "test_fast"]
    assert memberships["slow"] == [test_prefix + "test_slow"]
    assert memberships["integration"] == [test_prefix + "test_integration_without_server"]
    assert memberships["server"] == [test_prefix + "test_server_beats_integration"]
    assert memberships["browser-free"] == [test_prefix + "test_browser_beats_integration"]
    assert memberships["browser-server"] == [test_prefix + "test_browser_server_beats_all"]
    assert inventory["lanes"]["slow"]["prerequisites"] == ["server"]
    assert inventory["lanes"]["integration"]["prerequisites"] == []
    assert inventory["lanes"]["server"]["prerequisites"] == ["server"]


@pytest.mark.unit
def test_plugin_records_real_empty_collection(tmp_path):
    """A valid file with no tests produces an explicit empty receipt, never success."""
    result, inventory = _collect(tmp_path, "VALUE = 'no test functions here'\n")
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert inventory["status"] == "empty"
    assert inventory["collected_count"] == inventory["assigned_count"] == 0


@pytest.mark.unit
def test_plugin_marks_real_collection_error_failed_not_interrupted(tmp_path):
    """A syntax error is a failed inventory, not a user cancellation."""
    result, inventory = _collect(tmp_path, "def test_bad(:\n")
    assert result.returncode != 0
    assert inventory["status"] == "failed"


@pytest.mark.unit
def test_plugin_executes_exact_nodeids_from_external_file(tmp_path):
    """The real plugin avoids an argv-sized node-ID list and rejects omissions."""
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    test_file = cases / "test_nodeids.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.mark.unit\n"
        "def test_selected(): assert True\n"
        "@pytest.mark.unit\n"
        "def test_not_selected(): assert False\n"
    )
    nodeids = tmp_path / "exact-nodeids.txt"
    nodeids.write_text(f"{test_file.name}::test_selected\n")
    receipt = tmp_path / "execution.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file), "-q",
            "-p", "scripts.test_lane_plugin", "--lifeos-lane-nodeids", str(nodeids),
            "--lifeos-lane-execution", str(receipt),
        ],
        capture_output=True, text=True, cwd=cases,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    assert json.loads(receipt.read_text())["reports"] == {"test_nodeids.py::test_selected": "passed"}


@pytest.mark.unit
@pytest.mark.parametrize("xdist", [False, True])
def test_plugin_records_selected_setup_skips_once_in_serial_and_xdist(tmp_path, xdist):
    """A setup skip is terminal for that ID even though it has no call report."""
    if xdist:
        pytest.importorskip("xdist")
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    test_file = cases / "test_setup_skip.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.fixture\ndef skip_in_setup():\n"
        "    pytest.skip('synthetic setup skip')\n\n"
        "@pytest.mark.unit\ndef test_setup_skip(skip_in_setup): pass\n\n"
        "@pytest.mark.unit\ndef test_passes(): assert True\n"
    )
    nodeids = tmp_path / "exact-nodeids.txt"
    selected = (
        "test_setup_skip.py::test_setup_skip",
        "test_setup_skip.py::test_passes",
    )
    nodeids.write_text("\n".join(selected) + "\n")
    receipt = tmp_path / "execution.json"
    command = [
        sys.executable, "-m", "pytest", str(test_file), "-q",
        "-p", "scripts.test_lane_plugin", "--lifeos-lane-nodeids", str(nodeids),
        "--lifeos-lane-execution", str(receipt),
    ]
    if xdist:
        command.extend(("-n", "2", "--dist", "loadscope"))
    result = subprocess.run(
        command,
        capture_output=True, text=True, cwd=cases,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "success"
    assert payload["reports"] == {
        selected[0]: "skipped",
        selected[1]: "passed",
    }


@pytest.mark.unit
def test_plugin_records_only_the_named_privacy_audit_skip_reason(tmp_path):
    from scripts.verification_evidence import PRIVACY_AUDIT_NODEID, PRIVACY_AUDIT_NOT_APPLICABLE_REASON

    cases = tmp_path / "cases"
    tests_dir = cases / "tests"
    tests_dir.mkdir(parents=True)
    (cases / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    (tests_dir / "test_fixtures_no_personal_data.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_no_fixture_contains_a_real_sensitive_value():\n"
        f"    pytest.skip({PRIVACY_AUDIT_NOT_APPLICABLE_REASON!r})\n"
    )
    nodeids = tmp_path / "exact-nodeids.txt"
    nodeids.write_text(PRIVACY_AUDIT_NODEID + "\n")
    receipt = tmp_path / "execution.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests", "-q", "-p", "scripts.test_lane_plugin",
            "--lifeos-lane-nodeids", str(nodeids), "--lifeos-lane-execution", str(receipt),
        ],
        capture_output=True, text=True, cwd=cases, env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(receipt.read_text())["not_applicable_candidates"] == {
        PRIVACY_AUDIT_NODEID: PRIVACY_AUDIT_NOT_APPLICABLE_REASON,
    }


@pytest.mark.unit
def test_auto_fails_for_a_real_present_test_file_with_zero_collected_ids(tmp_path):
    """Auto cannot treat a real pytest exit-5 collection as a skipped lane."""
    copied = tmp_path / "checkout"
    (copied / "scripts").mkdir(parents=True)
    (copied / "tests").mkdir()
    for name in (
        "test.sh", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py",
        "verify_candidate.py", "verification_evidence.py", "candidate_snapshot.py",
        "development_metrics.py", "test_capacity.py", "test_instance.py",
        "_supervised_exec_bootstrap.py", "_candidate_owner_bootstrap.py",
    ):
        shutil.copy2(REPO / "scripts" / name, copied / "scripts" / name)
    (copied / "tests" / "test_empty_lane.py").write_text("VALUE = 'no test functions'\n")
    result = subprocess.run(
        ["bash", "scripts/test.sh", "auto"],
        capture_output=True,
        text=True,
        cwd=copied,
        env={
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "LIFEOS_TEST_CHANGED_FILES": "tests/test_empty_lane.py",
            "LIFEOS_TEST_DISPATCH_ONLY": "1",
        },
    )
    assert result.returncode != 0
    assert "Could not collect changed test files" in result.stdout


def _all_dispatch(
    copied: Path, temporary: Path, *, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real ``all`` entrypoint in a self-contained synthetic checkout."""
    home = temporary / "home"
    activate = home / ".venvs" / "lifeos" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(f'export PATH="{Path(sys.executable).parent}:$PATH"\n')
    return subprocess.run(
        ["bash", "scripts/test.sh", "all"],
        capture_output=True,
        text=True,
        cwd=copied,
        env={
            **os.environ,
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "LIFEOS_TEST_PARALLEL_WORKERS": "2",
            **(extra_env or {}),
        },
    )


def _copied_all_checkout(tmp_path: Path, test_source: str) -> tuple[Path, Path]:
    """Create only the runner dependencies and a deliberately fast-only test."""
    copied = tmp_path / "checkout"
    (copied / "scripts").mkdir(parents=True)
    (copied / "tests").mkdir()
    for name in (
        "test.sh", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py",
        "verify_candidate.py", "verification_evidence.py", "candidate_snapshot.py",
        "development_metrics.py", "test_capacity.py", "test_instance.py",
        "_supervised_exec_bootstrap.py", "_candidate_owner_bootstrap.py",
    ):
        shutil.copy2(REPO / "scripts" / name, copied / "scripts" / name)
    (copied / "requirements.txt").write_text("synthetic-dependency==1\n")
    (copied / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit', 'integration', 'requires_server']\n")
    (copied / "tests" / "test_synthetic.py").write_text(test_source)
    subprocess.run(["git", "init", "-q"], cwd=copied, check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic All"], cwd=copied, check=True)
    subprocess.run(["git", "config", "user.email", "all@example.invalid"], cwd=copied, check=True)
    subprocess.run(["git", "add", "."], cwd=copied, check=True)
    subprocess.run(["git", "commit", "-qm", "synthetic all"], cwd=copied, check=True)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    return copied, temporary


@pytest.mark.unit
def test_all_dispatch_runs_nonempty_lane_and_skips_intentionally_empty_lanes(tmp_path):
    """Caller-owned inventory succeeds, then real pytest runs only its populated lane."""
    copied, temporary = _copied_all_checkout(
        tmp_path,
        "import pytest\n\n@pytest.mark.unit\ndef test_fast_only(): assert True\n",
    )
    result = _all_dispatch(copied, temporary)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert payload["lanes"] == ["fast-unit"]
    assert payload["result"] == "success"
    assert not list(temporary.glob("tmp.*"))


@pytest.mark.unit
def test_all_dispatch_preserves_lane_failure_after_cleaning_inventory(tmp_path):
    """A real dispatched pytest failure is returned after the caller inventory is removed."""
    copied, temporary = _copied_all_checkout(
        tmp_path,
        "import pytest\n\n@pytest.mark.unit\ndef test_fast_only(): assert False\n",
    )
    result = _all_dispatch(copied, temporary)
    assert result.returncode != 0
    payload = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert payload["result"] == "failure"
    assert not list(temporary.glob("tmp.*"))


@pytest.mark.unit
def test_all_dispatch_stops_before_test_body_when_server_prerequisite_fails(tmp_path):
    """A lane executed under ``if`` cannot turn a failed prerequisite into green pytest."""
    copied, temporary = _copied_all_checkout(
        tmp_path,
        """import pathlib
import pytest

@pytest.mark.requires_server
def test_server_body_never_runs():
    pathlib.Path('server-body-ran').write_text('unexpected')
""",
    )
    (copied / "scripts" / "server.sh").write_text("#!/bin/sh\nexit 1\n")
    (copied / "scripts" / "server.sh").chmod(0o755)
    fake_bin = temporary / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "curl").chmod(0o755)
    result = _all_dispatch(
        copied,
        temporary,
        extra_env={"PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert result.stderr and "candidate" in result.stderr
    assert not (copied / "server-body-ran").exists()
    assert not list(temporary.glob("tmp.*"))


@pytest.mark.unit
def test_all_dispatch_runs_fast_unit_before_later_nonempty_lanes(tmp_path):
    """Collection precedence does not determine user-facing execution order."""
    copied, temporary = _copied_all_checkout(
        tmp_path,
        """import pytest

@pytest.mark.unit
def test_fast(): assert True

@pytest.mark.integration
def test_integration(): assert True
""",
    )
    result = _all_dispatch(copied, temporary)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.index('"fast-unit"') < result.stdout.index('"integration"')


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["candidate", "unit"])
def test_test_sh_server_free_modes_use_the_isolated_verifier_entrypoint(tmp_path, mode):
    """The local runner delegates a real body to snapshot/capacity evidence."""
    copied = tmp_path / "checkout"
    (copied / "scripts").mkdir(parents=True)
    (copied / "tests").mkdir()
    for name in (
        "test.sh", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py",
        "verify_candidate.py", "verification_evidence.py", "candidate_snapshot.py",
        "development_metrics.py", "test_capacity.py", "test_instance.py",
        "_supervised_exec_bootstrap.py", "_candidate_owner_bootstrap.py",
    ):
        shutil.copy2(REPO / "scripts" / name, copied / "scripts" / name)
    (copied / "requirements.txt").write_text("synthetic-dependency==1\n")
    (copied / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    (copied / "tests" / "test_candidate.py").write_text(
        "import pytest\n@pytest.mark.unit\ndef test_candidate(): assert True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=copied, check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic Candidate"], cwd=copied, check=True)
    subprocess.run(["git", "config", "user.email", "candidate@example.invalid"], cwd=copied, check=True)
    subprocess.run(["git", "add", "."], cwd=copied, check=True)
    subprocess.run(["git", "commit", "-qm", "synthetic candidate"], cwd=copied, check=True)
    activate = tmp_path / "home" / ".venvs" / "lifeos" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(f'export PATH="{Path(sys.executable).parent}:$PATH"\n')
    result = subprocess.run(
        ["bash", "scripts/test.sh", mode], capture_output=True, text=True, cwd=copied,
        env={
            **os.environ, "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "LIFEOS_TEST_PARALLEL_WORKERS": "1",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"reused": false' in result.stdout


@pytest.mark.unit
def test_plugin_refuses_to_overwrite_source_or_existing_receipt(tmp_path):
    """Inventory output is external and create-only, protecting source files."""
    source = "import pytest\n\n@pytest.mark.unit\ndef test_ok(): pass\n"
    cases = tmp_path / "cases"
    cases.mkdir()
    test_file = cases / "test_lane_cases.py"
    test_file.write_text(source)
    existing = tmp_path / "existing.json"
    existing.write_text("keep")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file), "--collect-only", "-qq",
            "-p", "scripts.test_lane_plugin", "--lifeos-lane-inventory", str(existing),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert result.returncode != 0
    assert existing.read_text() == "keep"


@pytest.mark.unit
def test_shell_runner_reads_marker_and_prerequisite_data_from_registry():
    """The shell runner does not carry a second lane table or server rule."""
    assert "mapfile" not in (REPO / "scripts" / "test-lanes.sh").read_text()
    result = subprocess.run(
        [
            "bash", "-c",
            "source scripts/test-lanes.sh; test_lane_marker integration; "
            "test_lane_requires_server integration; printf ' integration-server=%s\\n' $?; "
            "test_lane_requires_server slow; printf ' slow-server=%s\\n' $?; "
            "test_lane_requires_server server; printf ' server-server=%s\\n' $?",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "integration and not browser and not requires_server",
        " integration-server=1",
        " slow-server=0",
        " server-server=0",
    ]


@pytest.mark.unit
def test_hook_dependencies_are_present_in_a_copied_repo_fixture(tmp_path):
    """The tracked hook brings its sourced registry and plugin into a copied checkout."""
    copied = tmp_path / "checkout"
    (copied / "scripts").mkdir(parents=True)
    for name in (
        "pre-push", "test-lanes.sh", "test_lane_registry.py", "test_lane_plugin.py", "test.sh",
    ):
        shutil.copy2(REPO / "scripts" / name, copied / "scripts" / name)
    subprocess.run(["git", "init", "-q"], cwd=copied, check=True)
    result = subprocess.run(
        ["bash", "scripts/pre-push"],
        capture_output=True,
        text=True,
        cwd=copied,
        env={
            **os.environ,
            "LIFEOS_PREPUSH_PLAN_ONLY": "1",
            "LIFEOS_PREPUSH_HAVE_CONTENT": "0",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "prepush-plan: skip-deletion" in result.stdout
