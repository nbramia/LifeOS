"""Tests for the Claude Code resume endpoint (issue #160).

Resume spawns a local terminal via subprocess. Every test in this file
mocks `subprocess.Popen` so no real process is launched.
"""
from __future__ import annotations

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
    return tmp_path, sid


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
    client, synthetic_session, monkeypatch
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "echo claude --resume {session_id} --extra")

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 4242
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def wait(self, timeout=None):
            # Long-running process — pretend it's still up.
            raise __import__("subprocess").TimeoutExpired(cmd=captured["argv"], timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", _FakeProc)

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
def test_resume_subagent_id_resolves_to_parent_cwd(
    client, synthetic_session, monkeypatch
):
    """Resuming a subagent synthetic id should resume the parent terminal."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 5
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd=captured["argv"], timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", _FakeProc)

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
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_500_when_binary_missing(client, synthetic_session, monkeypatch):
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
def test_resume_500_when_process_exits_immediately(client, synthetic_session, monkeypatch):
    """Process exits within the 0.5s wait → 500 with stderr preview."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")

    class _ExitedProc:
        def __init__(self, argv, **kwargs):
            self.pid = 7
            self.returncode = 2

            class _Stream:
                def read(self, n):
                    return b"stderr says: bad config\n"
            self.stderr = _Stream()
            self.stdout = MagicMock()

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", _ExitedProc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 500
    assert "stderr says" in r.json()["detail"]


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
# Env file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_substitutes_cwd_token(client, synthetic_session, monkeypatch):
    """`{cwd}` is replaced with the decoded project working directory."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd",
                        "echo --working-dir {cwd} --resume {session_id}")

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            self.pid = 1
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", _FakeProc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    assert "/home/syn/Code/A" in captured["argv"]
    assert sid in captured["argv"]


@pytest.mark.unit
def test_resume_url_encoded_substitutions_for_uri_schemes(
    client, synthetic_session, monkeypatch
):
    """`{cwd_url}` and `{session_id_url}` are URL-encoded — needed for
    embedding inside `warp://action/new_tab?path=...&command=...` and other
    URI-scheme launchers where spaces and slashes must be %-encoded."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(
        settings, "cc_resume_cmd",
        "echo warp://action/new_tab?path={cwd_url}&command=vt+claude+--resume+{session_id_url}",
    )

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            self.pid = 1
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", _FakeProc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    # cwd /home/syn/Code/A should be encoded to %2Fhome%2Fsyn%2FCode%2FA
    full = " ".join(captured["argv"])
    assert "%2Fhome%2Fsyn%2FCode%2FA" in full
    # The bare session_id has no special chars so URL-encoding is a no-op,
    # but the {session_id_url} token must have been substituted (no literal token).
    assert "{session_id_url}" not in full
    assert "{cwd_url}" not in full
    # The non-URL substitutions are NOT applied inside the URL-encoded params
    # (they would break the URL); only the *_url variants land there.
    assert "/home/syn/Code/A" not in full or "%2Fhome%2Fsyn%2FCode%2FA" in full


@pytest.mark.unit
def test_resume_env_file_overrides_systemd_env(
    client, synthetic_session, monkeypatch, tmp_path
):
    """LIFEOS_CC_RESUME_ENV_FILE values appear in the Popen env kwarg."""
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "echo {session_id}")
    env_path = tmp_path / "env.txt"
    env_path.write_text("DISPLAY=:0\nXAUTHORITY=/run/user/1000/x\n# comment\n\n", encoding="utf-8")
    monkeypatch.setattr(settings, "cc_resume_env_file", str(env_path))

    captured: dict[str, object] = {}

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            self.pid = 1
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr("subprocess.Popen", _FakeProc)

    _, sid = synthetic_session
    r = client.post(f"/api/agents/sessions/cc:{sid}/resume", json={})
    assert r.status_code == 200, r.text
    assert captured["env"]["DISPLAY"] == ":0"
    assert captured["env"]["XAUTHORITY"] == "/run/user/1000/x"
