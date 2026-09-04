"""Tests for POST /api/agents/board/cards/{id}/open (#851, AC9) and the
GET /api/agents/models catalog endpoint wired through the same router.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agent_assignment
from api.services.agent_worker.session_store import SessionStore
from api.services.task_manager import TaskManager


pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    import api.services.task_manager as tm_mod
    m = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "index.json")
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: m)
    return m


@pytest.fixture
def session_store(tmp_path: Path, monkeypatch):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(agent_assignment, "_session_store", store)
    return store


@pytest.fixture(autouse=True)
def _clear_opening_guard():
    """`agent_assignment._opening_card_ids` (round 1, finding #6) is
    module-level, process-lifetime state — clear it around every test so
    one test's card_id can't spuriously 409 a later test that reuses it."""
    agent_assignment._opening_card_ids.clear()
    yield
    agent_assignment._opening_card_ids.clear()


@pytest.fixture(autouse=True)
def _disable_launcher_prereqs(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    monkeypatch.setattr(settings, "cc_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "codex_resume_enabled", True)
    monkeypatch.setattr(settings, "codex_resume_cmd", "wezterm cli spawn --cwd {cwd} -- {inner_command}")
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)


def test_open_card_not_found(client, manager, session_store):
    resp = client.post("/api/agents/board/cards/nonexistent/open")
    assert resp.status_code == 404


def test_open_card_no_assignee_tag(client, manager, session_store):
    task = manager.create(description="fix the thing", tags=["agent"])
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert "assignee" in resp.json()["detail"]


def test_open_claude_card_spawns_local_launcher(client, manager, session_store, monkeypatch):
    task = manager.create(description="fix the printer", tags=["agent", "claude"])

    proc = MagicMock()
    proc.pid = 4242
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    body = resp.json()
    assert body["opened"] is True
    assert body["pid"] == 4242

    argv = popen_mock.call_args.args[0]
    inner_idx = next(i for i, a in enumerate(argv) if a == "--")
    inner = " ".join(argv[inner_idx + 1:])
    assert f"LIFEOS_TASK_ID={task.id}" in inner
    assert "claude" in inner
    assert "fix the printer" in inner


def test_open_codex_card_spawns_local_launcher(client, manager, session_store, monkeypatch):
    task = manager.create(description="deploy the service", tags=["agent", "codex"])

    proc = MagicMock()
    proc.pid = 5151
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    argv = popen_mock.call_args.args[0]
    inner_idx = next(i for i, a in enumerate(argv) if a == "--")
    inner = " ".join(argv[inner_idx + 1:])
    assert "codex" in inner


def test_open_card_not_todo_is_409(client, manager, session_store, monkeypatch):
    task = manager.create(description="fix the printer", tags=["agent", "claude"])
    manager.update(task.id, status="in_progress")
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert "Assigned" in resp.json()["detail"]


def test_open_card_with_running_session_is_409(client, manager, session_store):
    task = manager.create(description="fix the printer", tags=["agent", "claude"])
    session_store.create(task_id=task.id, status="running", routing="claude_code")
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert "running session" in resp.json()["detail"]


def test_open_card_double_click_is_serialized(client, manager, session_store, monkeypatch):
    """Round 1, finding #6: a genuine double-click — two OS threads racing
    the SAME check-then-spawn window — must produce exactly one spawn and
    one 409, not two terminals opened onto the same card. A `threading.Lock`
    alone doesn't guarantee this (neither `task.status` nor `session_store`
    change as a side effect of a spawn, so a naive lock would just
    serialize two SUCCESSFUL spawns) — this proves the `_opening_card_ids`
    guard actually closes that gap."""
    import threading as _threading
    import time as _time

    task = manager.create(description="fix the printer", tags=["agent", "claude"])

    start_barrier = _threading.Barrier(2)
    call_count = {"n": 0}
    call_lock = _threading.Lock()

    def _popen_mock(*args, **kwargs):
        with call_lock:
            call_count["n"] += 1
        _time.sleep(0.1)  # widen the race window so an unlocked version would double-spawn
        proc = MagicMock()
        proc.pid = 4242
        return proc

    monkeypatch.setattr("subprocess.Popen", _popen_mock)

    results = []

    def _call():
        start_barrier.wait()
        resp = client.post(f"/api/agents/board/cards/{task.id}/open")
        results.append(resp.status_code)

    threads = [_threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [200, 409]
    assert call_count["n"] == 1


def test_open_card_reopens_after_grace_period_following_success(client, manager, session_store, monkeypatch):
    """Round 2, finding #1: `_opening_card_ids` used to be discarded ONLY on
    a spawn failure, so a card that opened successfully stayed 409'd for
    the rest of the process lifetime even after the session reached a
    terminal status and the task went back to `todo`. Advancing past
    `_OPENING_GRACE_SECONDS` must let a second, genuinely new open through."""
    task = manager.create(description="fix the printer", tags=["agent", "claude"])

    proc = MagicMock()
    proc.pid = 4242
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    fake_time = {"t": 1_000.0}
    monkeypatch.setattr(agent_assignment.time, "monotonic", lambda: fake_time["t"])

    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    assert popen_mock.call_count == 1

    # A re-open immediately after (still within the grace window) stays
    # 409'd — the card's just-claimed spot isn't cleared on success.
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert popen_mock.call_count == 1

    # The session the first spawn (eventually) registered reaches a
    # terminal status, and the card goes back to `todo` for reassignment —
    # mirroring what actually happens minutes later in production.
    session_store.create(task_id=task.id, status="completed", routing="claude_code")
    manager.update(task.id, status="todo")

    # Still within the grace window: stays 409'd even though the task/
    # session state now looks fully reopenable.
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert popen_mock.call_count == 1

    # Advance past the grace period — the stale claim expires and a fresh
    # open is allowed to spawn again.
    fake_time["t"] += agent_assignment._OPENING_GRACE_SECONDS + 1
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    assert popen_mock.call_count == 2


def test_open_card_remote_host_wraps_in_ssh(client, manager, session_store, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_hosts", {"studio": "user@studio.example"}, raising=False)
    task = manager.create(
        description="fix the printer", tags=["agent", "claude"],
        fields={"host": "studio"},
    )

    proc = MagicMock()
    proc.pid = 6161
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    argv = popen_mock.call_args.args[0]
    assert argv[0] == "ssh"
    assert "user@studio.example" in argv
    assert popen_mock.call_args.kwargs["cwd"] is None


def test_open_card_unregistered_host_is_409(client, manager, session_store, monkeypatch):
    task = manager.create(
        description="fix the printer", tags=["agent", "claude"],
        fields={"host": "vanished-box"},
    )
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409
    assert "vanished-box" in resp.json()["detail"]


def test_open_hermes_card_no_conversation_yet_is_409(client, manager, session_store):
    task = manager.create(description="ask hermes something", tags=["agent", "hermes"])
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 409


def test_open_hermes_card_with_conversation_returns_open_url(client, manager, session_store):
    task = manager.create(description="ask hermes something", tags=["agent", "hermes"])
    session_store.create(task_id=task.id, status="completed", routing="hermes")
    session_store.set_conversation_id(task.id, "conv-xyz")
    resp = client.post(f"/api/agents/board/cards/{task.id}/open")
    assert resp.status_code == 200
    assert resp.json() == {"open_url": "/chat?conversation=conv-xyz"}


def test_get_models_endpoint_returns_engines_shape(client, monkeypatch):
    from api.services.agent_worker import model_catalog as mc_mod

    async def _fake_get(self, ttl_seconds=None):
        return {
            "engines": {"claude": [], "codex": [], "local": [], "hermes": []},
            "refreshed_at": "2026-01-01T00:00:00Z",
            "stale": False,
        }

    monkeypatch.setattr(mc_mod.ModelCatalog, "get", _fake_get)
    # Force a fresh singleton so the patched class method is used cleanly.
    monkeypatch.setattr(mc_mod, "_catalog", None)

    resp = client.get("/api/agents/models")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["engines"].keys()) == {"claude", "codex", "local", "hermes"}
    assert body["stale"] is False
