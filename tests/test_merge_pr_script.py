"""scripts/merge-pr.sh: runs origin/main's publisher with the gate's fixed identity."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MERGE_PR = REPO_ROOT / "scripts" / "merge-pr.sh"

pytestmark = pytest.mark.unit

PINNED_HEAD = "a" * 40

PUBLISHER_TEMPLATE = """\
import json, os, sys
with open(os.environ["MERGE_TEST_RECORD"], "w") as f:
    json.dump({{"version": "{version}", "argv": sys.argv[1:]}}, f)
sys.exit(int(os.environ.get("MERGE_TEST_PUBLISHER_EXIT", "0")))
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _commit_publisher(checkout: Path, version: str) -> None:
    (checkout / "scripts" / "candidate_publisher.py").write_text(
        PUBLISHER_TEMPLATE.format(version=version)
    )
    _git(checkout, "add", "scripts/candidate_publisher.py")
    _git(checkout, "commit", "-m", f"publisher {version}")


@pytest.fixture
def harness(tmp_path):
    """A clone whose origin/main is stale, plus a stub `gh`, with nothing real reachable."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    _git(checkout, "init", "-b", "main")
    _git(checkout, "remote", "add", "origin", str(origin))
    _commit_publisher(checkout, "old")
    _git(checkout, "push", "origin", "main")
    shutil.copy2(MERGE_PR, checkout / "scripts" / "merge-pr.sh")

    # Advance origin/main from a second clone, leaving the checkout's ref stale.
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(origin), str(other))
    _commit_publisher(other, "from-main")
    _git(other, "push", "origin", "main")

    # The checkout's own working-tree publisher differs from both commits.
    (checkout / "scripts" / "candidate_publisher.py").write_text(
        PUBLISHER_TEMPLATE.format(version="working-tree")
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\n'
        f'echo "$@" >> "{gh_log}"\n'
        '[ -n "$MERGE_TEST_GH_FAIL" ] && exit 1\n'
        'printf "%s\\n" "$MERGE_TEST_GH_HEAD"\n'
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    record = tmp_path / "publisher.json"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "MERGE_TEST_RECORD": str(record),
        "MERGE_TEST_GH_HEAD": PINNED_HEAD,
    }
    return {"checkout": checkout, "env": env, "record": record, "gh_log": gh_log}


def _run(harness, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(harness["checkout"] / "scripts" / "merge-pr.sh"), *args],
        cwd=harness["checkout"], env={**harness["env"], **extra_env},
        capture_output=True, text=True,
    )


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_runs_freshly_fetched_main_publisher_with_gate_identity_and_pinned_head(harness):
    result = _run(harness, "1234")
    assert result.returncode == 0, result.stderr

    recorded = json.loads(harness["record"].read_text())
    assert recorded["version"] == "from-main"
    argv = recorded["argv"]
    assert _option(argv, "--repository") == "nbramia/LifeOS"
    assert _option(argv, "--pr") == "1234"
    assert _option(argv, "--workflow-file") == "candidate-verification.yml"
    assert _option(argv, "--check-name") == "candidate-verification"
    assert _option(argv, "--trusted-app-id") == "4891159"
    assert _option(argv, "--expected-head-sha") == PINNED_HEAD
    assert Path(_option(argv, "--repo-path")).resolve() == harness["checkout"].resolve()


def test_pins_the_head_it_is_given_without_reading_it(harness):
    given = "b" * 40
    result = _run(harness, "1234", given)
    assert result.returncode == 0, result.stderr
    assert _option(json.loads(harness["record"].read_text())["argv"], "--expected-head-sha") == given
    assert not harness["gh_log"].exists()


def test_propagates_a_refused_publish(harness):
    result = _run(harness, "1234", MERGE_TEST_PUBLISHER_EXIT="1")
    assert result.returncode == 1


@pytest.mark.parametrize("args", [
    (), ("abc",), ("12", "34"), ("12; echo hi",), ("12", "B" * 40), ("12", "b" * 39), ("12", "b" * 40, "x"),
])
def test_rejects_bad_arguments_before_contacting_github(harness, args):
    result = _run(harness, *args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not harness["gh_log"].exists()
    assert not harness["record"].exists()


def test_does_not_publish_when_the_head_cannot_be_read(harness):
    result = _run(harness, "1234", MERGE_TEST_GH_FAIL="1")
    assert result.returncode != 0
    assert not harness["record"].exists()


def test_does_not_publish_on_an_empty_head(harness):
    result = _run(harness, "1234", MERGE_TEST_GH_HEAD="")
    assert result.returncode == 1
    assert not harness["record"].exists()
