"""Tests for the Claude Code resume + focus endpoints (issues #160 + wezterm).

`resume` spawns a wezterm tab via `wezterm cli spawn` and captures the
pane id from stdout. `focus` calls `wezterm cli activate-pane` to revisit
the same tab. Both spawn subprocesses, so every test in this file mocks
the subprocess calls and the cc_wezterm_store; no real wezterm is
required.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import main as api_main


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _write_jsonl(path: Path, line: dict):
    """Write a single synthetic Claude Code jsonl line — enough to surface
    in discovery + recover a decoded_cwd for the resume target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


@pytest.fixture
def synthetic_session(tmp_path: Path, monkeypatch):
    """Build a synthetic ~/.claude/projects/-tmp-x/<uuid>.jsonl and point
    settings + discovery at it. Returns (projects_dir, session_id)."""
    from config.settings import settings

    proj = tmp_path / "-home-syn-Code-A"
    sid = "resume-target-uuid"
    _write_jsonl(proj / f"{sid}.jsonl", {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    })
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(tmp_path))
    monkeypatch.setattr(settings, "claude_code_lookback_days", 365)
    # Reset both ingest caches so the new fixture data is visible.
    from api.services.claude_code import session_ingest as cc
    cc.invalidate_cache()
    cc.invalidate_process_cache()
    # Short-circuit the server-side clipboard helper for the default mock
    # setup — tests that want to assert on it override `shutil.which`
    # themselves. Without this, every test that mocks subprocess.Popen
    # would also have to handle the `wl-copy`/`xclip` subprocess.run.
    monkeypatch.setattr("shutil.which", lambda name: None)
    return tmp_path, sid


@pytest.fixture
def wezterm_store(tmp_path: Path, monkeypatch):
    """Swap the default cc_wezterm store for a tmp-path-backed one so tests
    can introspect what got persisted without touching the real DB."""
    from api.services import cc_wezterm_store as mod
    store = mod.CCWezTermStore(db_path=tmp_path / "cc_wezterm.db")
    monkeypatch.setattr(mod, "_default_store", store)
    yield store
    store.close()


def _make_fake_proc(*, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
                    timeout: bool = False):
    """Build a _FakeProc class whose `communicate` matches the requested
    outcome. Tests instantiate it indirectly via `subprocess.Popen` mock."""

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            captured["env"] = kwargs.get("env") or {}
            self.pid = 4242
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def communicate(self, timeout=None):
            if timeout is True or timeout == "force":
                raise subprocess.TimeoutExpired(cmd=captured["argv"], timeout=timeout)
            self.returncode = returncode
            return stdout, stderr

    if timeout:
        class _TimingOut(_FakeProc):
            def communicate(self, timeout=None):  # noqa: D401
                raise subprocess.TimeoutExpired(cmd=captured["argv"], timeout=timeout)
        return _TimingOut, captured
    return _FakeProc, captured


# ---------------------------------------------------------------------------
# Gating: disabled / non-cc / missing template
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_rejects_non_cc_session(client):
    r = client.post("/api/agents/sessions/some-lifeos-uuid/resume", json={})
    assert r.status_code == 400
    assert "claude code" in r.json()["detail"].lower()


@pytest.mark.unit
def test_resume_disabled_by_default(client, synthetic_session, monkeypatch):
    """LIFEOS_CC_RESUME_ENABLED defaults to False — endpoint refuses."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", False)
    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"].lower()


@pytest.mark.unit
def test_resume_empty_template_400(client, synthetic_session, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "")
    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Template substitution + spawn invocation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_substitutes_session_id_and_spawns_with_cwd(
    client, synthetic_session, wezterm_store, monkeypatch
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "echo claude --resume {session_id} --extra")

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spawned"] is True
    assert body["pid"] == 4242
    # The bare uuid (no cc: prefix) appears in argv.
    assert sid in body["command"]
    assert "{session_id}" not in body["command"]
    # cwd is the decoded project path, not the encoded one.
    assert body["cwd"] == "/home/syn/Code/A"
    # No shell=True anywhere — verify by inspecting the spawn kwargs.
    assert captured["kwargs"].get("shell", False) is False
    # cwd reaches Popen.
    assert captured["kwargs"]["cwd"] == "/home/syn/Code/A"


@pytest.mark.unit
def test_resume_substitutes_inner_command_token(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """The default wezterm template inserts the rendered inner_command via
    `{inner_command}` — its argv tokens should land in the spawn argv."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd",
                        "claude --resume {session_id}")

    Proc, captured = _make_fake_proc(stdout=b"17\n", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    argv = captured["argv"]
    assert argv[0] == "wezterm"
    assert "spawn" in argv
    assert "--" in argv
    # Inner command's individual tokens appear after the `--`.
    dd = argv.index("--")
    inner_tokens = argv[dd + 1:]
    assert inner_tokens[0] == "claude"
    assert sid in inner_tokens


@pytest.mark.unit
def test_resume_subagent_id_resolves_to_parent_cwd(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """Resuming a subagent synthetic id should resume the parent terminal."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    sub_id = f"cc:{sid}:agent:toolu_01abc"
    r = client.post(f"/api/agents/sessions/{sub_id}/resume", json={})
    assert r.status_code == 200, r.text
    # argv has the parent uuid, not the synthetic subagent suffix.
    assert sid in captured["argv"]
    assert "agent:" not in " ".join(captured["argv"])


@pytest.mark.unit
def test_resume_returns_404_when_session_not_discovered(client, monkeypatch, tmp_path):
    """Empty projects dir → 404."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(tmp_path))
    from api.services.claude_code import session_ingest as cc
    cc.invalidate_cache()
    r = client.post("/api/agents/sessions/cc:nonexistent-id/resume", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pane id capture + store integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_captures_pane_id_from_stdout_and_stores_it(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """When the launcher prints a pane id (wezterm cli spawn's contract),
    the endpoint surfaces it AND persists session_id→pane_id."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo 42")

    Proc, _captured = _make_fake_proc(stdout=b"42\n", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pane_id"] == 42

    mapping = wezterm_store.get(f"cc:{sid}")
    assert mapping is not None
    assert mapping.pane_id == 42
    assert mapping.cwd == "/home/syn/Code/A"


@pytest.mark.unit
def test_resume_pane_id_null_when_stdout_has_no_integer(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """If the operator overrides the launcher to a URL dispatcher that
    prints nothing useful, we don't fabricate a pane id."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")

    Proc, _captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pane_id"] is None
    # Nothing should be stored when there's no pane id to remember.
    assert wezterm_store.get(f"cc:{sid}") is None


@pytest.mark.unit
def test_resume_pane_id_null_when_stdout_is_not_an_integer(
    client, synthetic_session, wezterm_store, monkeypatch
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo hello")

    Proc, _captured = _make_fake_proc(stdout=b"not-a-number\n", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["pane_id"] is None


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_500_when_binary_missing(client, synthetic_session, wezterm_store, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "no-such-binary {session_id}")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("no-such-binary")
    monkeypatch.setattr("subprocess.Popen", _raise)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 500
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.unit
def test_resume_500_when_process_exits_nonzero_with_stderr(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """Non-zero exit within the timeout → 500 with stderr preview."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")

    Proc, _captured = _make_fake_proc(stdout=b"", stderr=b"stderr says: bad config\n", returncode=2)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 500
    assert "stderr says" in r.json()["detail"]


@pytest.mark.unit
def test_resume_timeout_returns_spawned_without_pane_id(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """A launcher that BECOMES the terminal (rare; not the default) times
    out the proc.communicate. We treat that as a successful spawn but
    surface pane_id=None so the focus button knows there's nothing tracked."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")

    Proc, _captured = _make_fake_proc(timeout=True)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spawned"] is True
    assert body["pane_id"] is None


# ---------------------------------------------------------------------------
# Clipboard backup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_pipes_inner_command_to_system_clipboard(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """`inner_command` is piped to wl-copy / xclip server-side as a backup
    for operators who override cc_resume_cmd to a launcher that doesn't
    run the inner command itself (e.g. the legacy warp:// flow)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd",
                        "claude --resume {session_id}")

    Proc, _captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    captured_run: dict[str, object] = {}

    class _CompletedProcess:
        returncode = 0
        stderr = b""

    def _fake_run(argv, **kwargs):
        captured_run["argv"] = argv
        captured_run["input"] = kwargs.get("input")
        return _CompletedProcess()

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in ("wl-copy",) else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clipboard_copied"] is True
    assert captured_run["argv"] == ["wl-copy"]
    # Clipboard is cwd-prefixed so it works when pasted into any terminal
    # — `claude --resume <id>` only finds the session in its project cwd.
    assert captured_run["input"].decode("utf-8") == f"cd /home/syn/Code/A && claude --resume {sid}"


@pytest.mark.unit
def test_resume_clipboard_failure_is_non_fatal(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """No clipboard helper available → spawn still succeeds, flag is False."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd",
                        "claude --resume {session_id}")

    Proc, _captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)
    monkeypatch.setattr("shutil.which", lambda name: None)  # no clipboard tools

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["spawned"] is True
    assert body["clipboard_copied"] is False
    assert body["inner_command"]  # still returned so frontend can offer manual copy


@pytest.mark.unit
def test_resume_rejects_traversal_session_id(client, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    r = client.post("/api/agents/sessions/cc:..%2Fetc/resume", json={})
    # Either rejected by the path-traversal check (400) or normalized away by
    # FastAPI's path matching (404). Both are acceptable so long as nothing
    # outside the projects dir is touched.
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Substitution coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_substitutes_cwd_token(client, synthetic_session, wezterm_store, monkeypatch):
    """`{cwd}` is replaced with the decoded project working directory."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "echo --working-dir {cwd} --resume {session_id}")

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    assert "/home/syn/Code/A" in captured["argv"]
    assert sid in captured["argv"]


@pytest.mark.unit
def test_resume_url_encoded_substitutions_for_uri_schemes(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """`{cwd_url}` and `{session_id_url}` are URL-encoded — preserved for
    operators who override the launcher to a URI-scheme dispatcher."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(
        settings, "cc_resume_cmd",
        "echo warp://action/new_tab?path={cwd_url}&command=claude+--resume+{session_id_url}",
    )

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    full = " ".join(captured["argv"])
    assert "%2Fhome%2Fsyn%2FCode%2FA" in full
    assert "{session_id_url}" not in full
    assert "{cwd_url}" not in full


@pytest.mark.unit
def test_resume_returns_inner_command_for_clipboard(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """The inner-command setting is rendered with substitutions and surfaced
    in the response as `inner_command`."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")
    monkeypatch.setattr(
        settings, "cc_resume_inner_cmd",
        "claude --resume {session_id} --workdir {cwd}",
    )

    Proc, _captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sid in body["inner_command"]
    assert "/home/syn/Code/A" in body["inner_command"]
    assert "{session_id}" not in body["inner_command"]
    assert "{cwd}" not in body["inner_command"]


@pytest.mark.unit
def test_resume_empty_inner_command_yields_empty_string(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """Operators can disable the clipboard copy by setting INNER_CMD empty."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd", "")

    Proc, _captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    assert r.json()["inner_command"] == ""


@pytest.mark.unit
def test_resume_env_file_overrides_systemd_env(
    client, synthetic_session, wezterm_store, monkeypatch, tmp_path
):
    """LIFEOS_CC_RESUME_ENV_FILE values appear in the Popen env kwarg."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")
    env_path = tmp_path / "env.txt"
    env_path.write_text("DISPLAY=:0\nXAUTHORITY=/run/user/1000/x\n# comment\n\n", encoding="utf-8")
    monkeypatch.setattr(settings, "cc_resume_env_file", str(env_path))

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    assert captured["env"]["DISPLAY"] == ":0"
    assert captured["env"]["XAUTHORITY"] == "/run/user/1000/x"


# ---------------------------------------------------------------------------
# WEZTERM_PANE auto-injection (for the default wezterm launcher)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_injects_wezterm_pane_env_when_launcher_is_wezterm(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """Default `wezterm cli spawn` errors with 'WEZTERM_PANE not set' when
    called from outside wezterm. The resume endpoint probes `wezterm cli
    list` first and pins WEZTERM_PANE into the spawn env, preferring the
    `default` workspace's pane."""
    from config.settings import settings
    # Strip any inherited WEZTERM_PANE — tests must assert on what the
    # injection helper produces, not whatever value the dev's outer shell
    # happens to have (running pytest inside a wezterm session leaks
    # WEZTERM_PANE=<host pane> into the child process and masks the test).
    monkeypatch.delenv("WEZTERM_PANE", raising=False)
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "cc_resume_inner_cmd",
                        "claude --resume {session_id}")

    Proc, captured = _make_fake_proc(stdout=b"42\n", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)

    # Pretend wezterm is on PATH so the injection helper runs.
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)

    # Fake `wezterm cli list --format json` — two windows; the default
    # workspace pane should be chosen over the other.
    class _ListedCP:
        returncode = 0
        stdout = (
            b'[{"window_id":0,"tab_id":0,"pane_id":99,"workspace":"scratch"},'
            b'{"window_id":1,"tab_id":1,"pane_id":7,"workspace":"default"}]'
        )
        stderr = b""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _ListedCP())

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    # The Popen env now carries WEZTERM_PANE pointing at the default-workspace pane.
    assert captured["env"].get("WEZTERM_PANE") == "7"


@pytest.mark.unit
def test_resume_skips_pane_injection_when_launcher_is_not_wezterm(
    client, synthetic_session, wezterm_store, monkeypatch
):
    """Operator-overridden non-wezterm launcher → no WEZTERM_PANE meddling."""
    from config.settings import settings
    # Same env-isolation reason as the sibling test above.
    monkeypatch.delenv("WEZTERM_PANE", raising=False)
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo launching")

    Proc, captured = _make_fake_proc(stdout=b"", returncode=0)
    monkeypatch.setattr("subprocess.Popen", Proc)
    # Even if wezterm is on PATH, the injection helper only runs when the
    # launcher argv[0] is wezterm — non-wezterm launchers stay untouched.
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200
    assert "WEZTERM_PANE" not in captured["env"]


# ---------------------------------------------------------------------------
# /focus endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_focus_rejects_non_cc_session(client):
    r = client.post("/api/agents/sessions/some-lifeos-uuid/focus")
    assert r.status_code == 400


@pytest.mark.unit
def test_focus_disabled_by_default(client, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", False)
    r = client.post("/api/agents/sessions/cc:any-id/focus")
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"].lower()


@pytest.mark.unit
def test_focus_returns_404_when_no_mapping(client, wezterm_store, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    r = client.post("/api/agents/sessions/cc:no-mapping/focus")
    assert r.status_code == 404
    assert "resume" in r.json()["detail"].lower()


@pytest.mark.unit
def test_focus_calls_activate_pane_when_mapping_exists(
    client, wezterm_store, monkeypatch
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    wezterm_store.upsert("cc:session-x", pane_id=17, cwd="/tmp/proj")

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(argv, **kwargs):
        captured.setdefault("calls", []).append(argv)
        return _Completed()

    # `wezterm` must be locatable for the focus code to assemble the argv;
    # notify-send is optional (covered by a separate test).
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    r = client.post("/api/agents/sessions/cc:session-x/focus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["focused"] is True
    assert body["pane_id"] == 17
    assert body["cwd"] == "/tmp/proj"

    # Activate-pane was called with the stored pane id.
    calls = captured["calls"]
    assert any(
        argv[0].endswith("wezterm") and argv[1:] == ["cli", "activate-pane", "--pane-id", "17"]
        for argv in calls
    )


@pytest.mark.unit
def test_focus_returns_410_and_clears_mapping_when_activate_fails(
    client, wezterm_store, monkeypatch
):
    """Most common failure: user closed the tab. Server clears the stale
    mapping and returns 410 so the UI can prompt for Resume."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    wezterm_store.upsert("cc:session-x", pane_id=17, cwd="/tmp/proj")

    class _Failed:
        returncode = 1
        stdout = b""
        stderr = b"no such pane: 17\n"

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Failed())

    r = client.post("/api/agents/sessions/cc:session-x/focus")
    assert r.status_code == 410
    assert "no such pane" in r.json()["detail"]
    # Stale mapping is cleared so the next Resume can write a fresh one.
    assert wezterm_store.get("cc:session-x") is None
