"""Coverage for scripts/launchd-env-wrapper.sh (#776).

launchd's `EnvironmentVariables` dict is static at load time, so every
generated plist's ProgramArguments routes through this wrapper first, which
loads the project's .env into the process environment before exec'ing the
real command.

Critically, this must NOT `source` .env — systemd's EnvironmentFile= (the
thing this wrapper stands in for) is a plain KEY=VALUE parser that never
evaluates the value as shell. A `source`-based wrapper would instead run a
value like `FOO=$(whoami)` as code on every service start. This file's most
important test proves that does not happen.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "launchd-env-wrapper.sh"


def _run(project_dir: Path, *command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(WRAPPER), str(project_dir), *command],
        capture_output=True, text=True, timeout=10,
    )


@pytest.mark.unit
def test_wrapper_exports_plain_value(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("FOO=bar\n")
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bar"


@pytest.mark.unit
def test_wrapper_strips_matching_double_quotes(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text('FOO="bar baz"\n')
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bar baz"


@pytest.mark.unit
def test_wrapper_strips_matching_single_quotes(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("FOO='bar baz'\n")
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bar baz"


@pytest.mark.unit
def test_wrapper_does_not_execute_shell_code_in_value(tmp_path: Path):
    """The core safety property: a value containing shell metacharacters
    must reach the child process literally, never evaluated as code."""
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("FOO=$(whoami)\n")
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "$(whoami)"


@pytest.mark.unit
def test_wrapper_does_not_execute_backticks_in_value(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("FOO=`whoami`\n")
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "`whoami`"


@pytest.mark.unit
def test_wrapper_skips_comments_and_blank_lines(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("# a comment\n\nFOO=bar\n")
    result = _run(tmp_path, "bash", "-c", "echo \"$FOO\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bar"


@pytest.mark.unit
def test_wrapper_does_not_override_an_already_inherited_variable(tmp_path: Path):
    """Found on review: exporting every .env key unconditionally let a
    stale/incorrect .env value silently override one launchd already set
    via the plist's own EnvironmentVariables dict (e.g. a validated
    LIFEOS_VAULT_PATH) — the inherited value must win, matching how
    systemd's EnvironmentFile= never overrides a variable already set at
    the [Service] level."""
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("FOO=from_env_file\n")
    result = subprocess.run(
        ["bash", "-c", f'FOO=from_inherited_environment exec bash "{WRAPPER}" "{tmp_path}" bash -c \'echo "$FOO"\''],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "from_inherited_environment"


@pytest.mark.unit
def test_wrapper_still_exports_a_key_with_no_inherited_value(tmp_path: Path):
    """The precedence fix must not become 'never export anything' — a key
    genuinely absent from the inherited environment still comes from .env,
    same as every other test in this file already relies on."""
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    (tmp_path / ".env").write_text("BRAND_NEW_KEY=from_env_file\n")
    result = subprocess.run(
        ["bash", "-c", f'unset BRAND_NEW_KEY; exec bash "{WRAPPER}" "{tmp_path}" bash -c \'echo "$BRAND_NEW_KEY"\''],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "from_env_file"


@pytest.mark.unit
def test_wrapper_execs_normally_when_env_file_absent(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    result = _run(tmp_path, "echo", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hello"


@pytest.mark.unit
def test_wrapper_usage_error_with_fewer_than_two_args(tmp_path: Path):
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    result = subprocess.run(
        ["bash", str(WRAPPER), str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "Usage" in result.stderr


@pytest.mark.unit
def test_wrapper_is_executable():
    """Checks the git INDEX mode, not the working-tree file's stat() bit
    (found on re-review of an equivalent test in test_auto_update_macos.py)
    — a local `chmod +x` on the working-tree copy alone would pass this
    check while a fresh `git clone` on another machine still got whatever
    mode is actually committed."""
    if not WRAPPER.exists():
        pytest.skip("scripts/launchd-env-wrapper.sh not present")
    ls_files = subprocess.run(
        ["git", "ls-files", "-s", "--", str(WRAPPER)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert ls_files.startswith("100755"), f"git index mode is not 100755: {ls_files!r}"
