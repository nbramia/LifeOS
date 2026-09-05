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


# ---------------------------------------------------------------------------
# "Resume here" — explicit target_host override
# ---------------------------------------------------------------------------


def test_target_host_registry_host_overrides_sessions_recorded_host(client, remote_cc_session, monkeypatch):
    """The session is registered on "laptop"; posting a DIFFERENT registry
    host as target_host must launch there instead — proving target_host
    overrides the recorded host rather than merely confirming it."""
    from config.settings import settings
    monkeypatch.setattr(
        settings, "agent_hosts",
        {"laptop": "user@laptop.example", "studio": "user@studio.example"},
        raising=False,
    )
    proc = MagicMock()
    proc.communicate.return_value = (b"42\n", b"")
    proc.returncode = 0
    proc.pid = 9999
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/resume", json={"target_host": "studio"})
    assert resp.status_code == 200

    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "user@studio.example" in argv
    assert "user@laptop.example" not in argv


def test_target_host_api_host_launches_locally(client, tmp_path, monkeypatch):
    """A session with NO cli_sessions row (purely local transcript) posted
    with target_host equal to this API's own name behaves exactly like the
    existing local launch path — proving the "this machine" entry in the
    resume-host list is a real, working option."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    # Encoded-cwd dirs replace EVERY "-" with "/" on decode — a hyphenated
    # segment name would decode wrong ("local-proj" -> "local/proj"), so
    # this fixture's project segment is deliberately hyphen-free.
    projects_dir = tmp_path / "claude_code_projects"
    proj = projects_dir / "-home-user-Code-localproj"
    proj.mkdir(parents=True)
    (proj / "local-sess-1.jsonl").write_text('{"type": "user", "message": {"role": "user", "content": "hi"}}\n')
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(projects_dir), raising=False)

    proc = MagicMock()
    proc.communicate.return_value = (b"5\n", b"")
    proc.returncode = 0
    proc.pid = 4242
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cc:local-sess-1/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/Code/localproj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] != "ssh"  # launched locally, not wrapped in ssh


def test_target_host_unknown_host_400s_with_copyable_command(client, remote_cc_session, monkeypatch):
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        f"/api/agents/sessions/{remote_cc_session}/resume",
        json={"target_host": "nonexistent-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "nonexistent-host" in detail["error"]
    assert "claude --resume cc-uuid-on-laptop" in detail["command"]
    assert "/home/user/Code/project" in detail["command"]
    popen_mock.assert_not_called()  # never launched anywhere


def test_target_host_unknown_host_400s_with_empty_command_when_no_cwd_resolves(
    client, tmp_path, monkeypatch,
):
    """A session that resolves to NO cwd anywhere (no
    local transcript, no mirrored transcript, no `cli_sessions` row) must
    render an EMPTY `command`, not a bare inner command missing its `cd
    <cwd> &&` prefix — that would offer a command that resumes wherever
    the operator's terminal happens to be, not the session's actual
    project. Contrast with `test_target_host_unknown_host_400s_with_
    copyable_command` above, whose session DOES have a cwd and keeps its
    non-empty, `cd`-prefixed command."""
    from config.settings import settings
    from api.services import agent_transcript_mirror

    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --dangerously-skip-permissions --resume {session_id}")
    monkeypatch.setattr(
        settings, "claude_code_projects_dir", str(tmp_path / "empty-local-projects"), raising=False,
    )
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: tmp_path / "empty-mirror-root")
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cc:00000000-0000-0000-0000-000000000000/resume",
        json={"target_host": "nonexistent-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["command"] == ""
    popen_mock.assert_not_called()


def test_absent_target_host_preserves_existing_409(client, remote_cc_session, monkeypatch):
    """No target_host in the body at all — an unregistered recorded host
    still 409s."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)  # "laptop" isn't registered
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/sessions/{remote_cc_session}/resume", json={})
    assert resp.status_code == 409
    popen_mock.assert_not_called()


def test_target_host_resume_falls_back_to_mirrored_transcript_cwd_with_no_cli_row(
    client, tmp_path, monkeypatch,
):
    """A session known ONLY from its mirrored transcript (no cli_sessions
    row at all — the hook's registration expired or never fired) must still
    resolve a cwd for a "resume here" targeted at a registered host, via
    _lookup_cc_session_meta's mirrored-dir fallback rather than the
    cli_sessions row this scenario doesn't have."""
    from config.settings import settings
    from api.services import agent_transcript_mirror

    monkeypatch.setattr(settings, "agent_hosts", {"laptop": "user@laptop.example"}, raising=False)
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    mirror_root = tmp_path / "mirror-root"
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: mirror_root)
    cc_dir = mirror_root / "laptop" / "claude_code" / "-home-user-mirroredproj"
    cc_dir.mkdir(parents=True)
    (cc_dir / "mirror-only-sess.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"9\n", b"")
    proc.returncode = 0
    proc.pid = 5151
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cc:mirror-only-sess/resume",
        json={"target_host": "laptop"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/mirroredproj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "user@laptop.example" in argv


def test_target_host_api_host_resume_falls_back_to_cli_session_cwd_with_no_transcript_at_all(
    client, tmp_path, monkeypatch,
):
    """A session known ONLY from its `cli_sessions` row —
    no local transcript AND no mirrored transcript for it at all (e.g. the
    hook registered it but neither transcript scan has caught up yet) —
    must still resolve a cwd for "resume here" targeted at THIS API host,
    via the local branch's FINAL `cli_sessions`-cwd fallback
    (`api/routes/agents.py`'s `_resume_claude_code_launcher`, the `cli =
    _get_session_store().get_cli_session(session_id)` sub-branch inside
    the local `else`). Mutation-proved by deleting the sub-branch (leaving
    `target = None` unconditionally): every other test in this file stays
    green, so only this test binds the fallback."""
    from config.settings import settings
    from api.services import agent_transcript_mirror
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)
    # No local transcript for this session id anywhere.
    monkeypatch.setattr(
        settings, "claude_code_projects_dir", str(tmp_path / "empty-local-projects"), raising=False,
    )
    # No mirrored transcript either — an empty (nonexistent) mirror root.
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: tmp_path / "empty-mirror-root")

    store = agents_route._get_session_store()
    store.record_cli_session_event(
        engine="claude_code", event="session_start", session_id="cli-only-sess",
        host="laptop", cwd="/home/user/cli-only-proj", branch="main",
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"9\n", b"")
    proc.returncode = 0
    proc.pid = 7171
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cc:cli-only-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/cli-only-proj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] != "ssh"  # launched locally, not wrapped in ssh


def test_target_host_api_host_codex_resume_falls_back_to_cli_session_cwd_with_no_transcript_at_all(
    client, tmp_path, monkeypatch,
):
    """Codex sibling of the test above — the same unbound
    `cli_sessions`-cwd fallback sub-branch in `_resume_codex_session`'s
    local `else`."""
    from config.settings import settings
    from api.services import agent_transcript_mirror
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "codex_resume_enabled", True)
    monkeypatch.setattr(settings, "codex_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "codex_resume_inner_cmd", "codex resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(
        settings, "codex_sessions_dir", str(tmp_path / "empty-local-codex-sessions"), raising=False,
    )
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: tmp_path / "empty-mirror-root")

    store = agents_route._get_session_store()
    store.record_cli_session_event(
        engine="codex", event="session_start", session_id="cli-only-cx-sess",
        host="laptop", cwd="/home/user/cli-only-cxproj", branch="main",
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"9\n", b"")
    proc.returncode = 0
    proc.pid = 7272
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cx:cli-only-cx-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/cli-only-cxproj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] != "ssh"  # launched locally, not wrapped in ssh


def test_target_host_api_host_resume_falls_back_to_mirrored_cc_transcript_cwd(
    client, tmp_path, monkeypatch,
):
    """"Resume here" targeted at the API HOST ITSELF (not a registered
    remote) for a Claude Code session known only from its mirrored
    transcript must still resolve a cwd: the launcher's local `else`
    branch falls through to `_lookup_cc_session_meta`'s mirror-aware
    lookup, the same way the `remote_ssh_target` branch above does,
    rather than relying solely on the local `claude_code_projects_dir`
    scan."""
    from config.settings import settings
    from api.services import agent_transcript_mirror
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)
    # No session lives under the local projects dir — force the local
    # discover_sessions scan to miss so the fallback chain is exercised.
    monkeypatch.setattr(
        settings, "claude_code_projects_dir", str(tmp_path / "empty-local-projects"), raising=False,
    )

    mirror_root = tmp_path / "mirror-root"
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: mirror_root)
    cc_dir = mirror_root / "laptop" / "claude_code" / "-home-user-mirroredproj"
    cc_dir.mkdir(parents=True)
    (cc_dir / "mirror-only-sess-api.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )

    proc = MagicMock()
    proc.communicate.return_value = (b"9\n", b"")
    proc.returncode = 0
    proc.pid = 6161
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cc:mirror-only-sess-api/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/mirroredproj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] != "ssh"  # launched locally, not wrapped in ssh


def test_target_host_api_host_resume_falls_back_to_mirrored_codex_transcript_cwd(
    client, tmp_path, monkeypatch,
):
    """Codex sibling of the test above — the same local-branch fallback gap
    (round-1 finding #1) applied to `_resume_codex_session`'s `else`
    branch."""
    from config.settings import settings
    from api.services import agent_transcript_mirror
    from api.routes import agents as agents_route
    from tests.test_codex_ingest import _write_rollout, _session_meta

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "codex_resume_enabled", True)
    monkeypatch.setattr(settings, "codex_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "codex_resume_inner_cmd", "codex resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)
    # No local codex sessions dir has this rollout — force the local
    # discover_sessions scan to miss.
    monkeypatch.setattr(
        settings, "codex_sessions_dir", str(tmp_path / "empty-local-codex-sessions"), raising=False,
    )

    mirror_root = tmp_path / "mirror-root"
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: mirror_root)
    cx_dir = mirror_root / "laptop" / "codex"
    _write_rollout(cx_dir, "mirror-only-cx-api", [_session_meta(cwd="/home/user/mirroredcxproj")])

    proc = MagicMock()
    proc.communicate.return_value = (b"9\n", b"")
    proc.returncode = 0
    proc.pid = 6262
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        "/api/agents/sessions/cx:mirror-only-cx-api/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == "/home/user/mirroredcxproj"

    argv = popen_mock.call_args.args[0]
    assert argv[0] != "ssh"  # launched locally, not wrapped in ssh


def test_target_host_api_host_local_launch_with_missing_cwd_400s_with_command(
    client, tmp_path, monkeypatch,
):
    """A mirrored session's cwd is the REMOTE machine's path. "Resume
    here" targeted at THIS API host (local branch) for such a session,
    when the cwd doesn't exist, must 400 with `{error, command}` naming
    the cwd — not a bare-string 500 that blames the resume binary and
    loses the copyable-command fallback the drawer otherwise renders."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    projects_dir = tmp_path / "claude_code_projects"
    proj = projects_dir / "-home-user-Code-probeproj"
    proj.mkdir(parents=True)
    (proj / "missing-cwd-sess.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(projects_dir), raising=False)

    def _raise(argv, cwd=None, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cwd)
    monkeypatch.setattr("subprocess.Popen", _raise)

    resp = client.post(
        "/api/agents/sessions/cc:missing-cwd-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "/home/user/Code/probeproj" in detail["error"]
    assert "resume binary" not in detail["error"]
    assert "claude --resume missing-cwd-sess" in detail["command"]
    assert "cd /home/user/Code/probeproj" in detail["command"]


def test_target_host_api_host_local_launch_missing_binary_still_500s(
    client, tmp_path, monkeypatch,
):
    """Sibling contrast: when `Popen` raises `FileNotFoundError` for the
    EXECUTABLE (not the cwd) on the same local-launch path, the existing
    500 "resume binary not found" behavior must be unchanged — the new
    cwd-vs-binary distinction must not misfire on a real missing-binary
    case (mutation-proved: swapping the `exc.filename == popen_cwd` check
    to always take the 400 branch turns this into a 400)."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "no-such-binary --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    projects_dir = tmp_path / "claude_code_projects"
    proj = projects_dir / "-home-user-Code-probeproj2"
    proj.mkdir(parents=True)
    (proj / "missing-binary-sess.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(projects_dir), raising=False)

    def _raise(argv, cwd=None, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "no-such-binary")
    monkeypatch.setattr("subprocess.Popen", _raise)

    resp = client.post(
        "/api/agents/sessions/cc:missing-binary-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 500
    assert "resume binary not found" in resp.json()["detail"].lower()


def test_target_host_api_host_local_launch_with_cwd_that_is_a_file_400s_with_command(
    client, tmp_path, monkeypatch,
):
    """A mirrored cwd that exists on this host but is a regular file, not
    a directory, raises `NotADirectoryError` (ENOTDIR) — a distinct
    `OSError` subclass from a missing path. The merged `except OSError`
    handler must catch this too, not just `FileNotFoundError`, or it
    500s with a bare-string detail that loses the copyable-command
    fallback. Mutation-proved: narrowing the merged `except OSError` back
    to `except FileNotFoundError` makes this 500 instead of 400 (see the
    mutation-proof note below)."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    projects_dir = tmp_path / "claude_code_projects"
    proj = projects_dir / "-home-user-Code-notdirproj"
    proj.mkdir(parents=True)
    (proj / "notdir-sess.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(projects_dir), raising=False)

    def _raise(argv, cwd=None, **kwargs):
        raise NotADirectoryError(20, "Not a directory", cwd)
    monkeypatch.setattr("subprocess.Popen", _raise)

    resp = client.post(
        "/api/agents/sessions/cc:notdir-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "/home/user/Code/notdirproj" in detail["error"]
    assert "resume binary" not in detail["error"]
    assert "claude --resume notdir-sess" in detail["command"]
    assert "cd /home/user/Code/notdirproj" in detail["command"]


def test_target_host_api_host_local_launch_with_inaccessible_cwd_400s_with_command(
    client, tmp_path, monkeypatch,
):
    """Same as the ENOTDIR case above, for `PermissionError` (EACCES) —
    genuinely reachable via a
    Linux-to-Linux mirror pair where the mirrored cwd's parent directory
    exists on this host but is mode 700, owned by someone else (e.g.
    `/home/otheruser` when the API host also runs another operator's
    sessions). Mutation-proved the same way: narrowing the merged `except
    OSError` back to `except FileNotFoundError` turns this into a 500."""
    from config.settings import settings
    from api.routes import agents as agents_route

    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "claude --resume {session_id}")
    monkeypatch.setattr("shutil.which", lambda name: None)

    projects_dir = tmp_path / "claude_code_projects"
    proj = projects_dir / "-home-otheruser-Code-eaccesproj"
    proj.mkdir(parents=True)
    (proj / "eacces-sess.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    )
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(projects_dir), raising=False)

    def _raise(argv, cwd=None, **kwargs):
        raise PermissionError(13, "Permission denied", cwd)
    monkeypatch.setattr("subprocess.Popen", _raise)

    resp = client.post(
        "/api/agents/sessions/cc:eacces-sess/resume",
        json={"target_host": "this-api-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "/home/otheruser/Code/eaccesproj" in detail["error"]
    assert "resume binary" not in detail["error"]
    assert "claude --resume eacces-sess" in detail["command"]
    assert "cd /home/otheruser/Code/eaccesproj" in detail["command"]


def test_target_host_unknown_host_400s_on_focus_too(client, remote_cc_session, monkeypatch):
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(
        f"/api/agents/sessions/{remote_cc_session}/focus",
        json={"target_host": "nonexistent-host"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "command" in detail
