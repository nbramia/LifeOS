"""Tests for #851's remote resume/focus: a session whose `cli_sessions.host`
names a REGISTERED (settings.agent_hosts) machine other than this API host
runs the configured launcher over ssh instead of a local wezterm spawn, and
an unregistered host still 409s. No real ssh/wezterm is invoked — every
test here mocks `subprocess.Popen` globally, the same pattern
test_agent_resume_api.py uses.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import main as api_main


pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def wezterm_store(tmp_path: Path, monkeypatch):
    """Swap the default cc_wezterm store for a tmp-path-backed one so tests
    can introspect what got persisted without touching the real DB — same
    pattern as tests/test_agent_resume_api.py."""
    from api.services import cc_wezterm_store as mod
    store = mod.CCWezTermStore(db_path=tmp_path / "cc_wezterm.db")
    monkeypatch.setattr(mod, "_default_store", store)
    yield store
    store.close()


@pytest.fixture
def remote_cc_session(tmp_path: Path, monkeypatch):
    """Register a `cc:`-prefixed session on a remote host via the same
    `cli_sessions` path `scripts/lifeos-agent-hook.sh` uses (#849), and
    register that host's ssh target in the #851 registry."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(settings, "agent_hook_token", "test-hook-token")
    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr(settings, "codex_resume_enabled", True)
    monkeypatch.setattr(settings, "codex_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "codex_resume_inner_cmd", "codex resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    store = agents_route._get_session_store()
    sid = "cc-uuid-on-laptop"
    cli = store.record_cli_session_event(
        engine="claude_code", event="session_start", session_id=sid,
        host="laptop", cwd="/home/user/Code/project", branch="main",
    )
    return cli.session_id  # "cc:cc-uuid-on-laptop"


def test_remote_resume_wraps_launcher_in_ssh(client, remote_cc_session, monkeypatch):
    proc = MagicMock()
    proc.communicate.return_value = (b"42\n", b"")
    proc.returncode = 0
    proc.pid = 9999
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["spawned"] is True
    assert body["cwd"] == "/home/user/Code/project"

    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "user@laptop.example" in argv
    remote_command = argv[-1]
    assert "wezterm cli spawn" in remote_command
    assert "/home/user/Code/project" in remote_command
    # The LOCAL ssh client must not cwd= into a path that only exists remotely.
    assert popen_mock.call_args.kwargs["cwd"] is None

    # Round 1, finding #7: an ssh round trip routinely exceeds the 1.5s
    # local budget — the remote branch must pass the larger, connect-
    # timeout-derived value to communicate(), not the local one.
    from config.settings import settings
    communicate_timeout = proc.communicate.call_args.kwargs["timeout"]
    assert communicate_timeout == settings.agent_ssh_connect_timeout + 1.5
    assert communicate_timeout != 1.5


def test_remote_codex_resume_wraps_launcher_in_ssh(client, remote_cc_session, monkeypatch):
    from api.routes import agents as agents_route

    store = agents_route._get_session_store()
    cli = store.record_cli_session_event(
        engine="codex", event="session_start", session_id="cx-uuid-on-laptop",
        host="laptop", cwd="/home/user/Code/other-project",
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"7\n", b"")
    proc.returncode = 0
    proc.pid = 8888
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{cli.session_id}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/Code/other-project"

    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "user@laptop.example" in argv

    # Round 1, finding #7 (codex sibling).
    from config.settings import settings
    communicate_timeout = proc.communicate.call_args.kwargs["timeout"]
    assert communicate_timeout == settings.agent_ssh_connect_timeout + 1.5
    assert communicate_timeout != 1.5


def test_remote_resume_does_not_upsert_local_wezterm_store(
    client, remote_cc_session, wezterm_store, monkeypatch,
):
    """Round 1, finding #10: a remote resume's pane and wezterm process
    live on the REMOTE host — upserting the parsed pane id into the LOCAL
    `cc_wezterm_store` (keyed by `wezterm_pid` from THIS host's own
    `_current_wezterm_pid`) would record a host-mismatched mapping that a
    later local `/focus` could act on against the wrong machine."""
    proc = MagicMock()
    proc.communicate.return_value = (b"42\n", b"")  # a parseable pane id
    proc.returncode = 0
    proc.pid = 9999
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/resume")
    assert resp.status_code == 200
    assert resp.json()["pane_id"] == 42  # pane id WAS parsed...

    assert wezterm_store.get(remote_cc_session) is None  # ...but never upserted locally


def test_remote_codex_resume_does_not_upsert_local_wezterm_store(
    client, remote_cc_session, wezterm_store, monkeypatch,
):
    """Round 1, finding #10 (codex sibling)."""
    from api.routes import agents as agents_route

    store = agents_route._get_session_store()
    cli = store.record_cli_session_event(
        engine="codex", event="session_start", session_id="cx-uuid-on-laptop-2",
        host="laptop", cwd="/home/user/Code/other-project",
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"7\n", b"")
    proc.returncode = 0
    proc.pid = 8888
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{cli.session_id}/resume")
    assert resp.status_code == 200
    assert resp.json()["pane_id"] == 7

    assert wezterm_store.get(cli.session_id) is None


def test_unregistered_host_still_409s(client, remote_cc_session, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)  # "laptop" no longer registered

    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/resume")
    assert resp.status_code == 409
    popen_mock.assert_not_called()


def test_remote_focus_falls_back_to_launcher_and_returns_focus_shape(client, remote_cc_session, monkeypatch):
    """No cross-host pane registry exists, so /focus on a remote session
    runs the same launcher as /resume and returns focus's response shape
    (`focused`/`pane_id`/`cwd`), not resume's (`spawned`/`pid`/...)."""
    proc = MagicMock()
    proc.communicate.return_value = (b"13\n", b"")
    proc.returncode = 0
    proc.pid = 7777
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/focus")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"focused": True, "pane_id": 13, "cwd": "/home/user/Code/project"}

    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
