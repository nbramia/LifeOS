"""Tests for `api/services/agent_worker/remote_spawn.py` (#851): host
resolution, ssh argv construction, and the injectable remote kill runner."""
from __future__ import annotations

import pytest

from api.services.agent_worker.remote_spawn import (
    HostResolutionError,
    build_remote_argv,
    env_names_matching_prefixes,
    kill_remote_process_group,
    read_remote_pgid_line,
    resolve_host_target,
)


pytestmark = pytest.mark.unit


def test_resolve_host_target_empty_host_is_local():
    assert resolve_host_target("", "studio") is None
    assert resolve_host_target(None, "studio") is None


def test_resolve_host_target_matching_api_host_is_local():
    assert resolve_host_target("studio", "studio") is None


def test_resolve_host_target_known_host_returns_ssh_target(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)
    assert resolve_host_target("laptop", "studio") == "user@laptop.example"


def test_resolve_host_target_unknown_host_raises(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)
    with pytest.raises(HostResolutionError):
        resolve_host_target("nonexistent", "studio")


def test_build_remote_argv_shape(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_ssh_connect_timeout", 7, raising=False)
    argv = build_remote_argv(
        ["claude", "-p", "hello there"],
        target="user@laptop.example",
        unset_env_names=["ANTHROPIC_API_KEY", "CLAUDECODE"],
    )
    assert argv[0] == "ssh"
    assert "-o" in argv and "BatchMode=yes" in argv
    assert "ConnectTimeout=7" in argv
    assert "user@laptop.example" in argv
    assert argv[-2] == "--"
    remote_command = argv[-1]
    assert "env -u ANTHROPIC_API_KEY -u CLAUDECODE" in remote_command
    assert "'hello there'" in remote_command  # shlex-quoted, spaces preserved
    assert remote_command.startswith("setsid bash -c")


def test_read_remote_pgid_line():
    assert read_remote_pgid_line("PGID:12345\n") == 12345
    assert read_remote_pgid_line("PGID:12345") == 12345
    assert read_remote_pgid_line("not a pgid line\n") is None
    assert read_remote_pgid_line("") is None


def test_kill_remote_process_group_uses_injected_runner_never_touches_network():
    calls = []

    class _FakeResult:
        returncode = 0

    def _fake_runner(argv):
        calls.append(argv)
        return _FakeResult()

    ok = kill_remote_process_group(target="user@laptop.example", pgid=999, runner=_fake_runner)
    assert ok is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "ssh"
    assert "user@laptop.example" in argv
    assert argv[-3:] == ["kill", "--", "-999"]


def test_kill_remote_process_group_nonzero_exit_returns_false():
    class _FakeResult:
        returncode = 1

    ok = kill_remote_process_group(
        target="user@laptop.example", pgid=999, runner=lambda argv: _FakeResult(),
    )
    assert ok is False


def test_kill_remote_process_group_runner_exception_returns_false():
    def _raising_runner(argv):
        raise OSError("network unreachable")

    ok = kill_remote_process_group(
        target="user@laptop.example", pgid=999, runner=_raising_runner,
    )
    assert ok is False


def test_env_names_matching_prefixes(monkeypatch):
    monkeypatch.setenv("CLAUDE_TEST_VAR_851", "x")
    monkeypatch.setenv("ANTHROPIC_TEST_VAR_851", "y")
    monkeypatch.setenv("UNRELATED_VAR_851", "z")
    names = env_names_matching_prefixes(("ANTHROPIC_", "CLAUDE"))
    assert "CLAUDE_TEST_VAR_851" in names
    assert "ANTHROPIC_TEST_VAR_851" in names
    assert "UNRELATED_VAR_851" not in names


def test_env_names_matching_prefixes_respects_keep(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/home/x/.codex")
    monkeypatch.setenv("CODEX_TEST_851", "y")
    names = env_names_matching_prefixes(("CODEX_",), keep=frozenset({"CODEX_HOME"}))
    assert "CODEX_HOME" not in names
    assert "CODEX_TEST_851" in names
