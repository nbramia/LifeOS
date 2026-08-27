"""macOS launchd setup coverage (#776).

A real second-user deployment crash-looped twice from the same root cause:
`scripts/setup-launchd.sh` filled in a plist template with `sed`, then
validated only that the result was well-formed XML (`plutil -lint`) before
copying it straight into `~/Library/LaunchAgents`. Neither check catches a
substitution that silently didn't happen (a leftover `__LIFEOS_PATH__` token
is syntactically valid XML) or a path that was substituted correctly but
points nowhere on this machine.

This file covers the two new checks — `check_placeholders` /
`check_paths_exist`, combined in `validate_plist` — by `source`-ing
scripts/setup-launchd.sh (its operational body is wrapped in `main()` and
guarded, mirroring tests/test_deploy_drift.py's pattern for auto-deploy.sh),
plus an end-to-end run of `main()` against a sandboxed HOME/LaunchAgents dir
and fixture templates, with `plutil` stubbed since it's macOS-only.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_LAUNCHD = REPO_ROOT / "scripts" / "setup-launchd.sh"


def _stub_bin(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


def _make_sandbox(tmp_path: Path) -> Path:
    """A synthetic project tree: scripts/setup-launchd.sh + config/launchd/."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config" / "launchd").mkdir(parents=True)
    (repo / "scripts" / "setup-launchd.sh").write_text(
        SETUP_LAUNCHD.read_text(), encoding="utf-8"
    )
    (repo / "scripts" / "setup-launchd.sh").chmod(0o755)
    return repo


def _run_sourced(repo: Path, call: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source setup-launchd.sh then run `call`. The script sets `-e`; a
    helper under test is often deliberately exercised for its nonzero exit
    status (e.g. to check "rc=$?" after it), so disable errexit right after
    sourcing — sourcing itself must never run main() or fail either way."""
    script = repo / "scripts" / "setup-launchd.sh"
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'source "{script}"; set +e; {call}'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.unit
def test_sourcing_setup_launchd_does_not_run_main(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    result = _run_sourced(repo, "echo did-not-exit")
    assert result.returncode == 0, result.stderr
    assert "did-not-exit" in result.stdout


@pytest.mark.unit
def test_auto_deploy_style_functions_defined(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    syntax = subprocess.run(["bash", "-n", str(SETUP_LAUNCHD)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    repo = _make_sandbox(tmp_path)
    result = _run_sourced(
        repo,
        "type -t generate_plist check_placeholders check_paths_exist "
        "validate_plist install_plist main",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("function") == 6, result.stdout


@pytest.mark.unit
def test_generate_plist_substitutes_all_placeholders(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    template = repo / "t.plist.template"
    template.write_text(
        "<dict><key>WorkingDirectory</key><string>__LIFEOS_PATH__</string>"
        "<key>Vault</key><string>__VAULT_PATH__</string>"
        "<key>Home</key><string>__HOME__</string></dict>",
        encoding="utf-8",
    )
    output = repo / "t.plist"
    result = _run_sourced(
        repo,
        f'generate_plist "{template}" "{output}" "/home/op" "/proj" "/vault"',
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text()
    assert "__HOME__" not in text and "__LIFEOS_PATH__" not in text and "__VAULT_PATH__" not in text
    assert "/home/op" in text and "/proj" in text and "/vault" in text


@pytest.mark.unit
def test_generate_plist_handles_sed_metacharacters_in_values(tmp_path: Path):
    """A path containing `&`, `|`, or `\\` must reach the output literally —
    `&` means "whole match" in a sed replacement, `|` is generate_plist's own
    delimiter, and an unescaped `\\` can corrupt the substitution outright."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    template = repo / "t.plist.template"
    template.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>__LIFEOS_PATH__</string>\n</dict>",
        encoding="utf-8",
    )
    output = repo / "t.plist"
    tricky_path = r"/proj&ects/a|b\c"
    result = _run_sourced(
        repo,
        f'generate_plist "{template}" "{output}" "/home/op" "{tricky_path}" "/vault"',
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == (
        "<dict>\n    <key>WorkingDirectory</key>\n    "
        f"<string>{tricky_path}</string>\n</dict>"
    )


@pytest.mark.unit
def test_sed_escape_replacement_round_trips_special_characters(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    result = _run_sourced(repo, r'''esc=$(_sed_escape_replacement 'a&b\c|d'); printf 'X__T__Y' | sed "s|__T__|$esc|g"''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == r'Xa&b\c|dY'


@pytest.mark.unit
def test_check_placeholders_reports_leftover_token(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    plist = repo / "broken.plist"
    plist.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>__LIFEOS_PATH__</string>\n</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_placeholders "{plist}"')
    assert result.returncode == 0, result.stderr
    assert "__LIFEOS_PATH__" in result.stdout


@pytest.mark.unit
def test_check_placeholders_silent_when_fully_substituted(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    plist = repo / "clean.plist"
    plist.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>/proj</string>\n</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_placeholders "{plist}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.unit
def test_check_paths_exist_flags_missing_workingdirectory_and_venv(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>/no/such/dir</string>\n</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "/no/such/venv"')
    assert result.returncode == 0, result.stderr
    assert "/no/such/dir" in result.stdout
    assert "/no/such/venv" in result.stdout


@pytest.mark.unit
def test_check_paths_exist_silent_when_paths_are_real(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    venv = tmp_path / "venv"
    venv.mkdir()
    plist = repo / "p.plist"
    plist.write_text(
        f"<dict>\n    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "{venv}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def _stub_plutil_ok(repo: Path) -> Path:
    bindir = repo / "stubbin"
    bindir.mkdir(exist_ok=True)
    _stub_bin(bindir, "plutil", "exit 0\n")
    return bindir


@pytest.mark.unit
def test_validate_plist_rejects_leftover_placeholder(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    venv = tmp_path / "venv"
    venv.mkdir()
    plist = repo / "broken.plist"
    plist.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>__LIFEOS_PATH__</string>\n</dict>",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, f'validate_plist "{plist}" "{venv}"; echo "rc=$?"', env)
    assert "rc=1" in result.stdout, (result.stdout, result.stderr)
    assert "unsubstituted placeholder" in result.stdout


@pytest.mark.unit
def test_validate_plist_rejects_missing_workingdirectory(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    venv = tmp_path / "venv"
    venv.mkdir()
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n    <key>WorkingDirectory</key>\n    <string>/does/not/exist</string>\n</dict>",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, f'validate_plist "{plist}" "{venv}"; echo "rc=$?"', env)
    assert "rc=1" in result.stdout, (result.stdout, result.stderr)
    assert "does not exist" in result.stdout


@pytest.mark.unit
def test_validate_plist_rejects_missing_venv(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    plist = repo / "p.plist"
    plist.write_text(
        f"<dict>\n    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n</dict>",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, f'validate_plist "{plist}" "/no/such/venv"; echo "rc=$?"', env)
    assert "rc=1" in result.stdout, (result.stdout, result.stderr)


@pytest.mark.unit
def test_validate_plist_accepts_well_formed_plist(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    venv = tmp_path / "venv"
    venv.mkdir()
    plist = repo / "p.plist"
    plist.write_text(
        f"<dict>\n    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n</dict>",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, f'validate_plist "{plist}" "{venv}"; echo "rc=$?"', env)
    assert "rc=0" in result.stdout, (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# install_plist — idempotent copy (never silently replace an unchanged unit)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_install_plist_writes_a_new_file(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    src = repo / "com.lifeos.api.plist"
    src.write_text("content-v1", encoding="utf-8")
    dst_dir = tmp_path / "LaunchAgents"
    dst_dir.mkdir()
    result = _run_sourced(repo, f'install_plist "{src}" "{dst_dir}"')
    assert result.returncode == 0, result.stderr
    assert "Installed:" in result.stdout
    assert (dst_dir / "com.lifeos.api.plist").read_text() == "content-v1"


@pytest.mark.unit
def test_install_plist_leaves_an_unchanged_file_untouched(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    src = repo / "com.lifeos.api.plist"
    src.write_text("content-v1", encoding="utf-8")
    dst_dir = tmp_path / "LaunchAgents"
    dst_dir.mkdir()
    dst = dst_dir / "com.lifeos.api.plist"
    dst.write_text("content-v1", encoding="utf-8")
    before_mtime = dst.stat().st_mtime

    result = _run_sourced(repo, f'install_plist "{src}" "{dst_dir}"')
    assert result.returncode == 0, result.stderr
    assert "Unchanged:" in result.stdout
    assert "Installed:" not in result.stdout
    assert dst.stat().st_mtime == before_mtime


@pytest.mark.unit
def test_install_plist_overwrites_a_genuinely_different_file(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    src = repo / "com.lifeos.api.plist"
    src.write_text("content-v2", encoding="utf-8")
    dst_dir = tmp_path / "LaunchAgents"
    dst_dir.mkdir()
    dst = dst_dir / "com.lifeos.api.plist"
    dst.write_text("content-v1", encoding="utf-8")

    result = _run_sourced(repo, f'install_plist "{src}" "{dst_dir}"')
    assert result.returncode == 0, result.stderr
    assert "Installed:" in result.stdout
    assert dst.read_text() == "content-v2"


# ---------------------------------------------------------------------------
# End-to-end: main() against a sandboxed HOME/LaunchAgents dir and fixture
# templates — the actual install path, not just the helpers in isolation.
# ---------------------------------------------------------------------------
def _make_main_sandbox(tmp_path: Path, template_body: str) -> tuple[Path, Path, Path, Path]:
    repo = _make_sandbox(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    fake_home = tmp_path / "fakehome"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    venv = fake_home / ".venvs" / "lifeos"
    venv.mkdir(parents=True)
    (repo / "config" / "launchd" / "com.lifeos.api.plist.template").write_text(
        template_body, encoding="utf-8"
    )
    return repo, vault, fake_home, venv


_GOOD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lifeos.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HOME__/.venvs/lifeos/bin/uvicorn</string>
    </array>
    <key>WorkingDirectory</key>
    <string>__LIFEOS_PATH__</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIFEOS_VAULT_PATH</key>
        <string>__VAULT_PATH__</string>
    </dict>
</dict>
</plist>
"""

@pytest.mark.unit
def test_main_installs_a_well_formed_plist(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert installed.exists(), (result.stdout, result.stderr)
    assert "__LIFEOS_PATH__" not in installed.read_text()


@pytest.mark.unit
def test_main_rerun_leaves_an_unchanged_installed_plist_untouched(tmp_path: Path):
    """Re-running setup on a host where the service is already installed
    must not silently replace it if nothing changed (constraint: never
    replace a working unit silently)."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)

    def run():
        return subprocess.run(
            ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=30,
        )

    first = run()
    assert first.returncode == 0, (first.stdout, first.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert installed.exists()
    before_mtime = installed.stat().st_mtime

    second = run()
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "Unchanged: com.lifeos.api.plist" in second.stdout, second.stdout
    assert installed.stat().st_mtime == before_mtime


@pytest.mark.unit
def test_main_refuses_to_install_unsubstituted_plist(tmp_path: Path, monkeypatch):
    """The exact field failure mode: a substitution silently doesn't happen
    (simulated here by a template whose placeholder survives generation
    because it's spelled differently from what sed is told to replace) —
    main() must exit non-zero and must NOT copy anything into LaunchAgents."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    broken_template = _GOOD_TEMPLATE.replace("__LIFEOS_PATH__", "__LIFEOS_PROJECT_PATH__")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, broken_template)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert not installed.exists(), "must not install a plist with a leftover placeholder"


@pytest.mark.unit
def test_main_refuses_to_install_when_venv_missing(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, vault, fake_home, venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    venv.rmdir()  # simulate a plist referencing a venv that isn't there
    (venv.parent).rmdir()
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert not installed.exists()
