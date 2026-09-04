"""
Tests for the `scripts/pre-push` SSH push-URL warning (#886).

The hook is invoked by git as `pre-push <remote name> <remote url>`. GitHub
closes an idle SSH session after ~6 minutes, and the gate's test run takes
~9, so an SSH push URL can fail with "Connection closed by remote host" even
after every test passed. The hook prints one warning line naming the HTTPS
equivalent when `$2` is an SSH URL, and stays silent for an HTTPS one.

Exercised in plan-only mode (mirrors tests/test_prepush_gate.py) with a
deletion-only ref on stdin, so the hook takes its fastest exit path and never
shells out to the real test suite or scripts/test.sh.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "pre-push"


def _run(remote_url: str) -> str:
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "",
        "LIFEOS_PREPUSH_HAVE_CONTENT": "0",
    }
    result = subprocess.run(
        ["bash", str(HOOK), "origin", remote_url],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    "remote_url,expected_https",
    [
        ("git@github.com:nbramia/LifeOS.git", "https://github.com/nbramia/LifeOS.git"),
        ("ssh://git@github.com/nbramia/LifeOS.git", "https://github.com/nbramia/LifeOS.git"),
    ],
    ids=["scp_like", "ssh_url"],
)
def test_ssh_push_url_warns_once_with_https_equivalent(remote_url, expected_https):
    out = _run(remote_url)
    warning_lines = [line for line in out.splitlines() if line.startswith("Warning:")]
    assert len(warning_lines) == 1, f"expected exactly one warning line, got: {out!r}"
    assert remote_url in warning_lines[0]
    assert expected_https in warning_lines[0]


@pytest.mark.unit
def test_https_push_url_is_silent():
    out = _run("https://github.com/nbramia/LifeOS.git")
    assert "Warning:" not in out


@pytest.mark.unit
def test_no_remote_url_is_silent():
    """A bare positional-args call (e.g. some other hook invocation style)
    must not crash on an unset $2."""
    env = {
        **os.environ,
        "LIFEOS_PREPUSH_PLAN_ONLY": "1",
        "LIFEOS_PREPUSH_CHANGED_FILES": "",
        "LIFEOS_PREPUSH_HAVE_CONTENT": "0",
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, env=env, cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Warning:" not in result.stdout
