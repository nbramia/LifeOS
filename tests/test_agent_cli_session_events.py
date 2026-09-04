"""Tests for POST /api/agents/cli-sessions/events (issue #849).

Covers the bearer-token gate (mirrors `hermes_proxy._check_hermes_inbound_auth`)
and the `cli_sessions` status machine driven by `scripts/lifeos-agent-hook.sh`
lifecycle posts: session_start -> idle, user_prompt_submit -> running (+
truncated prompt preview), stop -> idle, session_end -> ended. Uses a
temp-dir-backed SessionStore so the real data/ directory is never touched,
and always sets its own `agent_hook_token` via monkeypatch rather than
reading whatever the operator's real .env has configured.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import SessionStore


TOKEN = "test-hook-token-value"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hook_token", TOKEN)
    yield session_store


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def loopback_client():
    # Pin the client host to loopback (default TestClient uses host=
    # "testclient", which is NOT loopback) — same approach as
    # tests/test_agent_cc_pane_bind.py's `client` fixture. Needed for the
    # pane-store mirror, which requires the request itself to originate
    # from loopback, not just a matching `host` field in the body.
    return TestClient(api_main.app, client=("127.0.0.1", 50000))


def _post(client, body, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/api/agents/cli-sessions/events", json=body, headers=headers)


def _event(**overrides):
    body = {
        "engine": "claude_code",
        "event": "session_start",
        "session_id": "abc-123",
        "host": "laptop-a",
        "cwd": "/home/synthetic/Code/X",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_events_503_when_token_unset(client, stores, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hook_token", "")
    r = _post(client, _event())
    assert r.status_code == 503


@pytest.mark.unit
def test_events_401_when_no_bearer_header(client, stores):
    r = client.post("/api/agents/cli-sessions/events", json=_event())
    assert r.status_code == 401


@pytest.mark.unit
def test_events_401_when_wrong_token(client, stores):
    r = _post(client, _event(), token="wrong-token")
    assert r.status_code == 401


@pytest.mark.unit
def test_events_200_with_correct_token(client, stores):
    r = _post(client, _event())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered"] is True
    assert body["session_id"] == "cc:abc-123"
    assert body["status"] == "idle"


# ---------------------------------------------------------------------------
# Status machine
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_start_creates_idle_row(client, stores):
    r = _post(client, _event(event="session_start", session_id="s1"))
    assert r.status_code == 200
    cli = stores.get_cli_session("cc:s1")
    assert cli is not None
    assert cli.status == "idle"
    assert cli.host == "laptop-a"
    assert cli.engine == "claude_code"


@pytest.mark.unit
def test_user_prompt_submit_sets_running_and_truncates_preview(client, stores):
    _post(client, _event(event="session_start", session_id="s2"))
    long_prompt = "x" * 500
    r = _post(client, _event(event="user_prompt_submit", session_id="s2",
                              prompt_preview=long_prompt))
    assert r.status_code == 200
    cli = stores.get_cli_session("cc:s2")
    assert cli.status == "running"
    assert cli.prompt_preview == long_prompt[:200]
    assert len(cli.prompt_preview) == 200


@pytest.mark.unit
def test_stop_sets_idle(client, stores):
    _post(client, _event(event="session_start", session_id="s3"))
    _post(client, _event(event="user_prompt_submit", session_id="s3", prompt_preview="hi"))
    r = _post(client, _event(event="stop", session_id="s3"))
    assert r.status_code == 200
    cli = stores.get_cli_session("cc:s3")
    assert cli.status == "idle"


@pytest.mark.unit
def test_session_end_sets_ended(client, stores):
    _post(client, _event(event="session_start", session_id="s4"))
    r = _post(client, _event(event="session_end", session_id="s4"))
    assert r.status_code == 200
    cli = stores.get_cli_session("cc:s4")
    assert cli.status == "ended"
    assert cli.ended_at is not None


@pytest.mark.unit
def test_codex_engine_uses_cx_prefix(client, stores):
    r = _post(client, _event(engine="codex", session_id="cx1"))
    assert r.status_code == 200
    assert r.json()["session_id"] == "cx:cx1"
    assert stores.get_cli_session("cx:cx1") is not None


@pytest.mark.unit
def test_task_id_stored(client, stores):
    r = _post(client, _event(session_id="s5", task_id="task-42"))
    assert r.status_code == 200
    cli = stores.get_cli_session("cc:s5")
    assert cli.task_id == "task-42"


@pytest.mark.unit
def test_unknown_event_returns_422(client, stores):
    r = _post(client, _event(event="not_a_real_event"))
    assert r.status_code == 422


@pytest.mark.unit
def test_unknown_engine_returns_422(client, stores):
    r = _post(client, _event(engine="not_a_real_engine"))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Pane store mirroring (#849 trap: local host events go into cc_wezterm_store)
# ---------------------------------------------------------------------------


@pytest.fixture
def wezterm_store(tmp_path: Path, monkeypatch):
    from api.services import cc_wezterm_store as mod
    store = mod.CCWezTermStore(db_path=tmp_path / "cc_wezterm.db")
    monkeypatch.setattr(mod, "_default_store", store)
    yield store
    store.close()


@pytest.mark.unit
def test_pane_id_from_api_host_written_to_pane_store(loopback_client, stores, wezterm_store, monkeypatch):
    # Both conditions must hold: body.host matches this API's own host,
    # AND the request itself arrived from loopback.
    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    r = _post(loopback_client, _event(host="this-api-host", session_id="s6",
                                       pane_id=7, wezterm_pid=999))
    assert r.status_code == 200, r.text
    mapping = wezterm_store.get("cc:s6")
    assert mapping is not None
    assert mapping.pane_id == 7


@pytest.mark.unit
def test_pane_id_from_remote_host_not_written_to_pane_store(loopback_client, stores, wezterm_store, monkeypatch):
    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    r = _post(loopback_client, _event(host="a-different-laptop", session_id="s7", pane_id=9))
    assert r.status_code == 200, r.text
    assert wezterm_store.get("cc:s7") is None
    # Still stored on the cli_sessions row itself.
    cli = stores.get_cli_session("cc:s7")
    assert cli.pane_id == 9


@pytest.mark.unit
def test_pane_id_from_non_loopback_client_with_spoofed_host_not_written_to_pane_store(
    client, stores, wezterm_store, monkeypatch,
):
    """A non-loopback, bearer-authenticated caller naming this API's own
    hostname must NOT be able to write into the shared pane store — that
    would let it redirect Go To for a real local session (#849 round-1
    security finding). The default `client` fixture is non-loopback
    (TestClient's default host is "testclient"), so this exercises exactly
    that path: body.host matches api_host_name() but request.client.host
    does not pass the loopback gate.
    """
    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    r = _post(client, _event(host="this-api-host", session_id="s8", pane_id=13))
    assert r.status_code == 200, r.text
    assert wezterm_store.get("cc:s8") is None
    # The event and pane id are still recorded on the cli_sessions row —
    # only the shared pane-store mirror is withheld.
    cli = stores.get_cli_session("cc:s8")
    assert cli.pane_id == 13
