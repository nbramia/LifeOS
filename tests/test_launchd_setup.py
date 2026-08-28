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
import re
import shutil
import stat
import subprocess
import xml.dom.minidom
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
def test_xml_escape_escapes_ampersand_lt_gt(tmp_path: Path):
    """#830: values that flow into a plist <string> element (llama.cpp dir,
    model-source args) need real XML escaping, not just sed-safety — a raw
    `&`, `<`, or `>` is invalid XML outside an entity reference."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    result = _run_sourced(repo, r'''_xml_escape 'R&D <models> here' ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "R&amp;D &lt;models&gt; here"


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
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    plist = repo / "p.plist"
    plist.write_text(
        f"<dict>\n    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "{venv}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.unit
def test_check_paths_exist_flags_missing_program_binary(tmp_path: Path):
    """The venv exists and has a python interpreter, but the SPECIFIC binary
    ProgramArguments launches (e.g. uvicorn) was never installed into it —
    a real gap a directory-only or interpreter-only check would miss."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    # No uvicorn in this venv.
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n"
        f"    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{repo}/scripts/launchd-env-wrapper.sh</string>\n"
        f"        <string>{repo}</string>\n"
        f"        <string>{venv}/bin/uvicorn</string>\n"
        "    </array>\n"
        "</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "{venv}"')
    assert result.returncode == 0, result.stderr
    assert f"{venv}/bin/uvicorn" in result.stdout


@pytest.mark.unit
def test_check_paths_exist_flags_missing_or_non_executable_wrapper(tmp_path: Path):
    """Found on review: a missing/non-executable launchd-env-wrapper.sh
    (item 1 — every template routes through it) went completely unchecked;
    the plist would install and load, then fail with no earlier signal."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    (venv / "bin" / "uvicorn").write_text("#!/bin/sh\n")
    (venv / "bin" / "uvicorn").chmod(0o755)
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{repo}/scripts/launchd-env-wrapper.sh</string>\n"  # doesn't exist
        f"        <string>{repo}</string>\n"
        f"        <string>{venv}/bin/uvicorn</string>\n"
        "    </array>\n"
        "</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "{venv}"')
    assert result.returncode == 0, result.stderr
    assert f"{repo}/scripts/launchd-env-wrapper.sh" in result.stdout
    assert f"{venv}/bin/uvicorn" not in result.stdout  # this one IS present+executable


@pytest.mark.unit
def test_check_paths_exist_flags_missing_interpreted_script(tmp_path: Path):
    """crm-sync's shape: item 3 is an interpreter (`/bin/bash`), which
    trivially exists on every machine — the thing that can actually be
    missing is item 4, the script it runs. Found on review: this was never
    checked at all, so a missing sync wrapper script passed validation."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    wrapper = repo / "scripts" / "launchd-env-wrapper.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/bash\n")
    wrapper.chmod(0o755)
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{wrapper}</string>\n"
        f"        <string>{repo}</string>\n"
        "        <string>/bin/bash</string>\n"
        f"        <string>{repo}/scripts/run_sync_wrapper.sh</string>\n"  # doesn't exist
        "    </array>\n"
        "</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'check_paths_exist "{plist}" "{venv}"')
    assert result.returncode == 0, result.stderr
    assert f"interpreted script: {repo}/scripts/run_sync_wrapper.sh" in result.stdout
    assert "program binary" not in result.stdout  # /bin/bash itself is fine


@pytest.mark.unit
def test_plist_program_argument_extracts_the_nth_program_argument(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    plist = repo / "p.plist"
    plist.write_text(
        "<dict>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "        <string>/proj/scripts/launchd-env-wrapper.sh</string>\n"
        "        <string>/proj</string>\n"
        "        <string>/proj/.venvs/lifeos/bin/uvicorn</string>\n"
        "        <string>api.main:app</string>\n"
        "    </array>\n"
        "</dict>",
        encoding="utf-8",
    )
    result = _run_sourced(repo, f'_plist_program_argument "{plist}" 1')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/proj/scripts/launchd-env-wrapper.sh"

    result = _run_sourced(repo, f'_plist_program_argument "{plist}" 3')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/proj/.venvs/lifeos/bin/uvicorn"

    result = _run_sourced(repo, f'_plist_program_argument "{plist}" 4')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "api.main:app"


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
def test_validate_plist_rejects_venv_dir_with_no_python_binary(tmp_path: Path):
    """A venv directory that exists but was never populated (e.g.
    `python3 -m venv` ran but `pip install -r requirements.txt` never did)
    must fail validation — the directory existing isn't enough."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    empty_venv = tmp_path / "empty-venv"
    empty_venv.mkdir()  # exists, but no bin/python inside
    plist = repo / "p.plist"
    plist.write_text(
        f"<dict>\n    <key>WorkingDirectory</key>\n    <string>{workdir}</string>\n</dict>",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = _run_sourced(repo, f'validate_plist "{plist}" "{empty_venv}"; echo "rc=$?"', env)
    assert "rc=1" in result.stdout, (result.stdout, result.stderr)
    assert "bin/python" in result.stdout


@pytest.mark.unit
def test_validate_plist_accepts_well_formed_plist(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
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


@pytest.mark.unit
def test_install_plist_backs_up_and_warns_before_replacing_a_different_file(tmp_path: Path):
    """Found on review: overwriting a different existing plist with no more
    signal than the run's one blanket 'Continue?' prompt (skipped entirely
    under --yes) counts as silently replacing a working unit. A backup and
    an explicit WARNING must appear even when nothing prompts."""
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
    assert "WARNING" in result.stdout, result.stdout
    assert "differs from what's currently installed" in result.stdout

    # Backed up OUTSIDE ~/Library/LaunchAgents (found on review: launchd
    # itself scans that directory, and the directory-form `launchctl load`
    # this script's own next-steps print would load a .bak'd plist too).
    backups = list((repo / "config" / "launchd" / "backups").glob("com.lifeos.api.plist.*"))
    assert len(backups) == 1, backups
    assert backups[0].read_text() == "content-v1", "backup must hold the PRE-replacement content"
    assert dst.read_text() == "content-v2"
    assert list(dst_dir.iterdir()) == [dst], "no backup file must land inside LaunchAgents"


@pytest.mark.unit
def test_install_plist_refuses_to_overwrite_when_backup_fails(tmp_path: Path):
    """`set -e` is active for the whole script — a failed backup
    (permission problem, disk full) must not silently abort the entire run
    mid-loop with no report. install_plist refuses to overwrite THIS one
    plist and reports it, letting the caller track the failure and
    continue with the rest."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    src = repo / "com.lifeos.api.plist"
    src.write_text("content-v2", encoding="utf-8")
    dst_dir = tmp_path / "LaunchAgents"
    dst_dir.mkdir()
    dst = dst_dir / "com.lifeos.api.plist"
    dst.write_text("content-v1", encoding="utf-8")
    backups_dir = repo / "config" / "launchd" / "backups"
    backups_dir.mkdir(parents=True)
    backups_dir.chmod(0o000)
    try:
        result = _run_sourced(repo, f'install_plist "{src}" "{dst_dir}"; echo "rc=$?"')
        assert "rc=1" in result.stdout, result.stdout
        assert "ERROR" in result.stdout, result.stdout
        assert dst.read_text() == "content-v1", "must not overwrite when the backup failed"
    finally:
        backups_dir.chmod(0o755)


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
    (venv / "bin").mkdir(parents=True)
    for exe in ("python", "uvicorn"):
        (venv / "bin" / exe).write_text("#!/bin/sh\n")
        (venv / "bin" / exe).chmod(0o755)
    (repo / "config" / "launchd" / "com.lifeos.api.plist.template").write_text(
        template_body, encoding="utf-8"
    )
    # _GOOD_TEMPLATE's ProgramArguments routes through this wrapper (#776) —
    # check_paths_exist validates it exists and is executable.
    wrapper = repo / "scripts" / "launchd-env-wrapper.sh"
    wrapper.write_text("#!/bin/bash\n")
    wrapper.chmod(0o755)
    return repo, vault, fake_home, venv


_GOOD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lifeos.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>__LIFEOS_PATH__/scripts/launchd-env-wrapper.sh</string>
        <string>__LIFEOS_PATH__</string>
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
def test_main_with_yes_and_no_vault_arg_fails_fast_instead_of_blocking(tmp_path: Path):
    """Found on review: --yes is meant for unattended automation, but with
    no vault argument and no LIFEOS_VAULT_PATH in .env, this used to block
    on an interactive `read` anyway — the opposite of what --yes asks for.
    A 5s timeout stands in for "would have hung forever": if the fix
    regresses, this test itself times out rather than merely failing an
    assertion, making the failure mode obvious."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, _vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "LIFEOS_VAULT_PATH" in result.stdout


@pytest.mark.unit
def test_main_with_yes_and_no_vault_arg_falls_back_to_env_file(tmp_path: Path):
    """The other half of the same fix: LIFEOS_VAULT_PATH in .env must still
    work as the vault source under --yes, not just as a failure reason."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    (repo / ".env").write_text(f"LIFEOS_VAULT_PATH={vault}\n", encoding="utf-8")
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert installed.exists(), (result.stdout, result.stderr)


# chromadb's real template doesn't follow the launchd-env-wrapper.sh
# convention the others do (documented as unreliable under launchd; never
# installed — see the real config/launchd/com.lifeos.chromadb.plist.template),
# so its ProgramArguments' third item is never a real executable path.
_CHROMADB_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lifeos.chromadb</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HOME__/.venvs/lifeos/bin/chroma</string>
        <string>run</string>
        <string>--host</string>
        <string>localhost</string>
    </array>
    <key>WorkingDirectory</key>
    <string>__LIFEOS_PATH__</string>
</dict>
</plist>
"""


@pytest.mark.unit
def test_main_does_not_abort_over_the_never_installed_chromadb_template(tmp_path: Path):
    """Found on review: chromadb's plist is generated and was validated
    against the same launchd-env-wrapper.sh convention every other
    template follows, even though it's never installed (skipped later,
    same as ever) — a broken/non-wrapper-shaped chromadb template could
    abort setup for api/crm-sync too. It must be skipped at validation the
    same way it's skipped at install."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    (repo / "config" / "launchd" / "com.lifeos.chromadb.plist.template").write_text(
        _CHROMADB_TEMPLATE, encoding="utf-8"
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Skipped: com.lifeos.chromadb.plist" in result.stdout
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.api.plist"
    assert installed.exists(), (result.stdout, result.stderr)
    assert not (fake_home / "Library" / "LaunchAgents" / "com.lifeos.chromadb.plist").exists()


# ---------------------------------------------------------------------------
# #774 — conditionally-installed agent-worker and mcp-http services
# ---------------------------------------------------------------------------
_AGENT_WORKER_TEMPLATE = (
    REPO_ROOT / "config" / "launchd" / "com.lifeos.agent-worker.plist.template"
)
_MCP_HTTP_TEMPLATE = REPO_ROOT / "config" / "launchd" / "com.lifeos.mcp-http.plist.template"


def _add_real_template(repo: Path, template_path: Path) -> None:
    """Copy an actual repo template (not a synthetic fixture) into the
    sandbox's config/launchd/, so #774's tests exercise the real templates
    this change ships, not a stand-in."""
    dest = repo / "config" / "launchd" / template_path.name
    dest.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.mark.unit
def test_main_skips_agent_worker_and_mcp_http_by_default(tmp_path: Path):
    """Neither opt-in is set — both must be completely absent, not just
    unloaded: no plist installed, matching 'a fresh install stays exactly
    as inert as it is today.'"""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not (_AGENT_WORKER_TEMPLATE.exists() and _MCP_HTTP_TEMPLATE.exists()):
        pytest.skip("agent-worker/mcp-http plist templates not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _AGENT_WORKER_TEMPLATE)
    _add_real_template(repo, _MCP_HTTP_TEMPLATE)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Agent Worker: disabled" in result.stdout
    assert "MCP HTTP:     disabled" in result.stdout
    assert "Skipped: com.lifeos.agent-worker.plist" in result.stdout
    assert "Skipped: com.lifeos.mcp-http.plist" in result.stdout
    agents = fake_home / "Library" / "LaunchAgents"
    assert not (agents / "com.lifeos.agent-worker.plist").exists()
    assert not (agents / "com.lifeos.mcp-http.plist").exists()
    assert (agents / "com.lifeos.api.plist").exists()  # unaffected


@pytest.mark.unit
def test_main_installs_agent_worker_when_autostart_enabled(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _AGENT_WORKER_TEMPLATE.exists():
        pytest.skip("agent-worker plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _AGENT_WORKER_TEMPLATE)
    (repo / ".env").write_text("LIFEOS_AGENT_WORKER_AUTOSTART=true\n", encoding="utf-8")
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Agent Worker: enabled" in result.stdout
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.agent-worker.plist"
    assert installed.exists(), (result.stdout, result.stderr)
    assert "__LIFEOS_PATH__" not in installed.read_text()
    assert "launchctl load ~/Library/LaunchAgents/com.lifeos.agent-worker.plist" in result.stdout


@pytest.mark.unit
def test_main_installs_mcp_http_when_bearer_token_set(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _MCP_HTTP_TEMPLATE.exists():
        pytest.skip("mcp-http plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _MCP_HTTP_TEMPLATE)
    (repo / "mcp_server.py").write_text("#!/usr/bin/env python3\n")
    (repo / "mcp_server.py").chmod(0o755)
    (repo / ".env").write_text("LIFEOS_MCP_BEARER_TOKEN=test-token-123\n", encoding="utf-8")
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "MCP HTTP:     enabled" in result.stdout
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.mcp-http.plist"
    assert installed.exists(), (result.stdout, result.stderr)
    assert "__LIFEOS_PATH__" not in installed.read_text()
    assert "launchctl load ~/Library/LaunchAgents/com.lifeos.mcp-http.plist" in result.stdout


@pytest.mark.unit
def test_main_rerun_with_autostart_still_enabled_leaves_agent_worker_untouched(tmp_path: Path):
    """Idempotency for the new conditional services too: re-running with
    the same opt-in state and unchanged templates must not replace an
    already-installed, unchanged plist."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _AGENT_WORKER_TEMPLATE.exists():
        pytest.skip("agent-worker plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _AGENT_WORKER_TEMPLATE)
    (repo / ".env").write_text("LIFEOS_AGENT_WORKER_AUTOSTART=true\n", encoding="utf-8")
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
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.agent-worker.plist"
    before_mtime = installed.stat().st_mtime

    second = run()
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "Unchanged: com.lifeos.agent-worker.plist" in second.stdout
    assert installed.stat().st_mtime == before_mtime


# ---------------------------------------------------------------------------
# #830 — conditionally-installed local LLM (llama-server) service
# ---------------------------------------------------------------------------
_LLM_TEMPLATE = REPO_ROOT / "config" / "launchd" / "com.lifeos.llm.plist.template"


def _add_llama_server_binary(fake_home: Path) -> Path:
    """Stub a llama.cpp checkout at the default __LLAMA_CPP_DIR__ location
    (fake_home/llama.cpp, matching LLAMA_DIR="${LIFEOS_LLAMA_DIR:-$HOME/llama.cpp}"
    in setup-launchd.sh) so check_paths_exist's WorkingDirectory/binary
    checks pass. Returns the llama.cpp dir."""
    llama_dir = fake_home / "llama.cpp"
    binary = llama_dir / "build" / "bin" / "llama-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return llama_dir


@pytest.mark.unit
def test_generate_plist_substitutes_llama_cpp_dir_placeholder(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    template = repo / "t.plist.template"
    template.write_text(
        "<dict><key>WorkingDirectory</key><string>__LLAMA_CPP_DIR__</string></dict>",
        encoding="utf-8",
    )
    output = repo / "t.plist"
    result = _run_sourced(
        repo,
        f'generate_plist "{template}" "{output}" "/home/op" "/proj" "/vault" "/opt/llama.cpp"',
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text()
    assert "__LLAMA_CPP_DIR__" not in text
    assert "/opt/llama.cpp" in text


@pytest.mark.unit
def test_inject_llm_source_args_replaces_marker_with_one_string_per_token(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    repo = _make_sandbox(tmp_path)
    plist = repo / "p.plist"
    plist.write_text(
        "<array>\n"
        "        <string>/bin/llama-server</string>\n"
        "        __LLM_SOURCE_ARGS_LINES__\n"
        "        <string>-ngl</string>\n"
        "</array>\n",
        encoding="utf-8",
    )
    result = _run_sourced(
        repo,
        f'LLM_SOURCE_ARGS_TOKENS=("-hf" "some/repo"); inject_llm_source_args "{plist}"',
    )
    assert result.returncode == 0, result.stderr
    text = plist.read_text()
    assert "__LLM_SOURCE_ARGS_LINES__" not in text
    assert "<string>-hf</string>" in text
    assert "<string>some/repo</string>" in text
    # Order preserved and no stray duplication of the surrounding lines.
    assert text.count("<string>-ngl</string>") == 1
    assert text.index("<string>-hf</string>") < text.index("<string>some/repo</string>")


@pytest.mark.unit
def test_main_skips_llm_by_default(tmp_path: Path):
    """Same 'fresh install stays exactly as inert as it is today' contract
    as agent-worker/mcp-http: with no opt-in set, nothing is installed."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Local LLM:    disabled" in result.stdout
    assert "Skipped: com.lifeos.llm.plist" in result.stdout
    agents = fake_home / "Library" / "LaunchAgents"
    assert not (agents / "com.lifeos.llm.plist").exists()
    assert (agents / "com.lifeos.api.plist").exists()  # unaffected


@pytest.mark.unit
def test_main_installs_llm_when_autostart_enabled(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    (repo / ".env").write_text("LIFEOS_LOCAL_LLM_AUTOSTART=true\n", encoding="utf-8")
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Local LLM:    enabled" in result.stdout
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.llm.plist"
    assert installed.exists(), (result.stdout, result.stderr)
    text = installed.read_text()
    assert not re.search(r"__[A-Z_]+__", text), text  # no leftover placeholders
    assert "<string>-hf</string>" in text
    assert "<string>unsloth/gemma-4-26B-A4B-it-GGUF</string>" in text
    assert "launchctl load ~/Library/LaunchAgents/com.lifeos.llm.plist" in result.stdout


@pytest.mark.unit
def test_main_installs_llm_with_model_path_override(tmp_path: Path):
    """LIFEOS_LLM_MODEL_PATH/_MMPROJ_PATH switch the args to `-m`/`--mmproj`
    — same override scripts/setup-systemd.sh honors for a stale HF cache."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    model_path = tmp_path / "model.gguf"
    model_path.write_text("fake gguf")
    mmproj_path = tmp_path / "mmproj.gguf"
    mmproj_path.write_text("fake mmproj")
    (repo / ".env").write_text(
        "LIFEOS_LOCAL_LLM_AUTOSTART=true\n"
        f"LIFEOS_LLM_MODEL_PATH={model_path}\n"
        f"LIFEOS_LLM_MMPROJ_PATH={mmproj_path}\n",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "WARNING" not in result.stdout, result.stdout
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.llm.plist"
    text = installed.read_text()
    assert f"<string>-m</string>\n        <string>{model_path}</string>" in text
    assert f"<string>--mmproj</string>\n        <string>{mmproj_path}</string>" in text
    assert "<string>-hf</string>" not in text


@pytest.mark.unit
def test_main_installs_llm_with_ampersand_in_model_path(tmp_path: Path):
    """Found on review: a real-world directory name like `R&D` in
    LIFEOS_LLM_MODEL_PATH produced invalid plist XML (a bare `&` is not
    legal outside an entity reference) — the stubbed plutil in other tests
    doesn't catch this since it always exits 0, so this test parses the
    installed plist as real XML instead."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    model_dir = tmp_path / "R&D"
    model_dir.mkdir()
    model_path = model_dir / "model.gguf"
    model_path.write_text("fake gguf")
    (repo / ".env").write_text(
        f"LIFEOS_LOCAL_LLM_AUTOSTART=true\nLIFEOS_LLM_MODEL_PATH={model_path}\n",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.llm.plist"
    text = installed.read_text()
    assert "R&amp;D" in text
    assert "R&D" not in text  # the raw, unescaped form must not appear
    xml.dom.minidom.parseString(text)  # raises ExpatError if not well-formed


@pytest.mark.unit
def test_main_warns_when_model_path_configured_but_missing(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    (repo / ".env").write_text(
        "LIFEOS_LOCAL_LLM_AUTOSTART=true\nLIFEOS_LLM_MODEL_PATH=/no/such/model.gguf\n",
        encoding="utf-8",
    )
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "WARNING: LIFEOS_LLM_MODEL_PATH=/no/such/model.gguf does not exist" in result.stdout


@pytest.mark.unit
def test_main_aborts_install_when_llama_server_binary_missing(tmp_path: Path):
    """check_paths_exist validates the actual llama-server binary
    (ProgramArguments item 3) and WorkingDirectory the same way it already
    does for uvicorn/mcp_server.py — an operator who enables the local LLM
    before building llama.cpp gets a named error, not a broken install.
    A single failing plist aborts the whole run (existing behavior), so
    even com.lifeos.api.plist must not be installed."""
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    # Deliberately no _add_llama_server_binary(fake_home) call.
    (repo / ".env").write_text("LIFEOS_LOCAL_LLM_AUTOSTART=true\n", encoding="utf-8")
    bindir = _stub_plutil_ok(repo)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        ["bash", "scripts/setup-launchd.sh", str(vault), "--yes"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "llama-server" in result.stdout
    agents = fake_home / "Library" / "LaunchAgents"
    assert not (agents / "com.lifeos.llm.plist").exists()
    assert not (agents / "com.lifeos.api.plist").exists()


@pytest.mark.unit
def test_main_rerun_with_llm_autostart_still_enabled_leaves_it_untouched(tmp_path: Path):
    if not SETUP_LAUNCHD.exists():
        pytest.skip("scripts/setup-launchd.sh not present")
    if not _LLM_TEMPLATE.exists():
        pytest.skip("llm plist template not present")
    repo, vault, fake_home, _venv = _make_main_sandbox(tmp_path, _GOOD_TEMPLATE)
    _add_real_template(repo, _LLM_TEMPLATE)
    _add_llama_server_binary(fake_home)
    (repo / ".env").write_text("LIFEOS_LOCAL_LLM_AUTOSTART=true\n", encoding="utf-8")
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
    installed = fake_home / "Library" / "LaunchAgents" / "com.lifeos.llm.plist"
    before_mtime = installed.stat().st_mtime

    second = run()
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "Unchanged: com.lifeos.llm.plist" in second.stdout
    assert installed.stat().st_mtime == before_mtime


@pytest.mark.unit
def test_main_names_the_gpu_and_network_watchdog_gap_on_macos(tmp_path: Path):
    """No macOS equivalent exists for these two — the run must say so
    explicitly rather than leave the gap silent (the exact ambiguity #774
    was filed over: an operator couldn't tell 'not applicable here' from
    'just never built')."""
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
    assert "lifeos-gpu-watchdog" in result.stdout
    assert "lifeos-network-watchdog" in result.stdout
    assert "not applicable" in result.stdout.lower()


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
    shutil.rmtree(venv.parent)  # simulate a plist referencing a venv that isn't there
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
