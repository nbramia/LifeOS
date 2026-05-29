"""Tests for the /api/agents/cc-pane-bind endpoint + the FD-probe fallback
in /api/agents/sessions/{id}/focus (issue #251).

`cc-pane-bind` is called by the SessionStart hook each time a `claude`
process starts in a wezterm pane. The fallback path in /focus runs the
`cc_pane_locate` probe when the cache misses, so Go To works for
sessions started outside the /agents Resume flow.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main


@pytest.fixture
def client():
    # Pin the client host to loopback so the /cc-pane-bind endpoint's
    # localhost-only check accepts requests from the test transport
    # (default TestClient sets host="testclient" which would 403).
    return TestClient(api_main.app, client=("127.0.0.1", 50000))


@pytest.fixture
def wezterm_store(tmp_path: Path, monkeypatch):
    """Swap the cc_wezterm singleton for a tmp-path-backed store."""
    from api.services import cc_wezterm_store as mod
    store = mod.CCWezTermStore(db_path=tmp_path / "cc_wezterm.db")
    monkeypatch.setattr(mod, "_default_store", store)
    yield store
    store.close()


@pytest.fixture
def synthetic_session(tmp_path: Path, monkeypatch):
    """Build a synthetic ~/.claude/projects/-tmp-x/<uuid>.jsonl so the
    focus endpoint's session lookup resolves to a real path. Returns
    (jsonl_path, session_id_with_cc_prefix).
    """
    from config.settings import settings

    proj = tmp_path / "-home-syn-Code-Repo"
    sid = "focus-target-uuid"
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{sid}.jsonl"
    import json
    jsonl.write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n", encoding="utf-8")

    monkeypatch.setattr(settings, "claude_code_projects_dir", str(tmp_path))
    monkeypatch.setattr(settings, "claude_code_lookback_days", 365)
    from api.services.claude_code import session_ingest as cc
    cc.invalidate_cache()
    cc.invalidate_process_cache()
    monkeypatch.setattr("shutil.which", lambda name: None)
    return jsonl, f"cc:{sid}"


# ---------------------------------------------------------------------------
# /cc-pane-bind — auth + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bind_stores_mapping_from_localhost(client, wezterm_store):
    """TestClient hits the app from 127.0.0.1, so the localhost check passes."""
    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "abc-def-123",
        "pane_id": 42,
        "cwd": "/home/u/Code/X",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] is True
    assert body["session_id"] == "cc:abc-def-123"
    assert body["pane_id"] == 42

    mapping = wezterm_store.get("cc:abc-def-123")
    assert mapping is not None
    assert mapping.pane_id == 42
    assert mapping.cwd == "/home/u/Code/X"


@pytest.mark.unit
def test_bind_accepts_already_prefixed_session_id(client, wezterm_store):
    """Hook may pass `cc:<uuid>` or bare `<uuid>` — both store under cc:<uuid>."""
    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "cc:abc-def-123",
        "pane_id": 7,
        "cwd": "/x",
    })
    assert r.status_code == 200
    assert wezterm_store.get("cc:abc-def-123") is not None


@pytest.mark.unit
def test_bind_rejects_non_localhost(client, wezterm_store, monkeypatch):
    """A request whose client.host isn't loopback must be denied with 403."""
    # Patch the request.client.host that FastAPI surfaces. Easiest via
    # TestClient's transport — but more robust: directly call the route
    # function with a fake request.
    from api.routes import agents as agents_route
    from api.routes.agents import CCPaneBindRequest

    class _Client:
        host = "10.0.0.5"

    class _Req:
        client = _Client()

    import asyncio
    with pytest.raises(Exception) as exc_info:
        asyncio.run(agents_route.cc_pane_bind(
            _Req(),  # type: ignore[arg-type]
            CCPaneBindRequest(session_id="x", pane_id=1),
        ))
    # FastAPI HTTPException — check status_code attr.
    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.unit
def test_bind_rejects_path_traversal_session_id(client, wezterm_store):
    """`validate_session_id` blocks `..` and slashes."""
    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "../etc/passwd",
        "pane_id": 1,
        "cwd": "/x",
    })
    assert r.status_code == 400


@pytest.mark.unit
def test_bind_rejects_negative_pane_id(client, wezterm_store):
    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "abc",
        "pane_id": -3,
        "cwd": "/x",
    })
    # LifeOS rewrites pydantic 422s to 400; the body still carries the
    # ge=0 constraint failure so we can assert on it.
    assert r.status_code == 400
    body = r.json()
    assert "pane_id" in str(body)


@pytest.mark.unit
def test_bind_accepts_empty_cwd(client, wezterm_store):
    """Hook may run before $PWD is reliably set — empty cwd should still bind."""
    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "abc",
        "pane_id": 1,
    })
    assert r.status_code == 200
    mapping = wezterm_store.get("cc:abc")
    assert mapping is not None
    assert mapping.cwd == ""


# ---------------------------------------------------------------------------
# /focus — probe fallback when cache misses
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_focus_probes_when_cache_miss_and_caches_result(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """Cache empty → probe returns a pane_id → mapping is upserted → activate
    is called with the probed pane_id.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _jsonl, sid = synthetic_session

    # Probe returns pane 99 — patch the locate helper directly so we
    # don't need to fake lsof + /proc + wezterm cli list.
    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": 99)

    # activate-pane succeeds.
    captured = {}

    class _Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(argv, **kwargs):
        captured.setdefault("calls", []).append(argv)
        return _Completed()

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["focused"] is True
    assert body["pane_id"] == 99

    # The probed mapping was cached for future calls.
    mapping = wezterm_store.get(sid)
    assert mapping is not None
    assert mapping.pane_id == 99

    # activate-pane was called with the probed id.
    assert any(
        argv[0].endswith("wezterm") and argv[1:] == ["cli", "activate-pane", "--pane-id", "99"]
        for argv in captured["calls"]
    )


@pytest.mark.unit
def test_focus_404_when_cache_miss_and_probe_finds_nothing(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": None)

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 404
    detail = r.json()["detail"].lower()
    assert "couldn't locate pane" in detail


@pytest.mark.unit
def test_focus_reprobes_when_cached_pane_is_stale_and_finds_new_one(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """Cached pane 17 is gone → activate fails → store is cleared → probe
    finds pane 42 → activate succeeds at pane 42.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    # Cache the mapping with the current wezterm boot id so #257's
    # invalidation doesn't pre-empt the activate-then-stale flow this
    # test exercises.
    wezterm_store.upsert(sid, pane_id=17, cwd="/repo", wezterm_pid=99001)
    monkeypatch.setattr("api.routes.agents._live_wezterm_pids", lambda xdg: {99001})

    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": 42)

    # First activate (pane 17) fails; second (pane 42) succeeds.
    calls: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""

    class _Stale:
        returncode = 1
        stdout = b""
        stderr = b"no such pane: 17\n"

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if "--pane-id" in argv:
            idx = argv.index("--pane-id")
            if argv[idx + 1] == "17":
                return _Stale()
            return _OK()
        return _OK()

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 200, r.text
    assert r.json()["pane_id"] == 42

    # Both activate-pane invocations happened, in that order.
    activate_calls = [argv for argv in calls if "activate-pane" in argv]
    assert len(activate_calls) == 2
    assert "17" in activate_calls[0]
    assert "42" in activate_calls[1]

    # Final mapping is the new pane.
    mapping = wezterm_store.get(sid)
    assert mapping is not None
    assert mapping.pane_id == 42


@pytest.mark.unit
def test_focus_410_when_cached_pane_stale_and_reprobe_finds_nothing(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    # Same setup as the sibling test — cache the mapping under the live
    # wezterm boot so we exercise the activate-then-stale flow rather
    # than the boot-id invalidation.
    wezterm_store.upsert(sid, pane_id=17, cwd="/repo", wezterm_pid=99001)
    monkeypatch.setattr("api.routes.agents._live_wezterm_pids", lambda xdg: {99001})

    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": None)

    class _Stale:
        returncode = 1
        stdout = b""
        stderr = b"no such pane\n"

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Stale())

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 410
    # Stale mapping was cleared.
    assert wezterm_store.get(sid) is None


@pytest.mark.unit
def test_focus_410_when_probed_pane_also_fails(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """Cache miss + probe success but the just-discovered pane is also dead
    (rare race) → 410, not 404 — a pane *was* identified, it just won't
    activate.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": 88)

    class _Stale:
        returncode = 1
        stdout = b""
        stderr = b"no such pane: 88\n"

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Stale())

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 410
    assert wezterm_store.get(sid) is None


@pytest.mark.unit
def test_focus_resolves_session_without_calling_discover_sessions(
    tmp_path: Path, client, wezterm_store, monkeypatch,
):
    """The /focus session-meta lookup should glob project dirs directly
    instead of running the full `discover_sessions` walk (which parses
    every transcript under ~/.claude/projects/). Build a layout with
    ≥2 project dirs and monkeypatch `discover_sessions` to fail loudly —
    if focus still works, we've confirmed the helper no longer relies on it.
    """
    from config.settings import settings
    from api.services.claude_code import session_ingest as cc

    monkeypatch.setattr(settings, "cc_resume_enabled", True)

    # Two project dirs; target jsonl lives in the second one.
    proj_a = tmp_path / "-home-syn-Code-Other"
    proj_b = tmp_path / "-home-syn-Code-Target"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    (proj_a / "decoy-uuid.jsonl").write_text("{}\n", encoding="utf-8")
    sid_bare = "real-target-uuid"
    (proj_b / f"{sid_bare}.jsonl").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(settings, "claude_code_projects_dir", str(tmp_path))
    monkeypatch.setattr(settings, "claude_code_lookback_days", 365)
    cc.invalidate_cache()
    cc.invalidate_process_cache()

    def _boom(*args, **kwargs):
        raise AssertionError("discover_sessions must not be called by _lookup_cc_session_meta")
    monkeypatch.setattr(cc, "discover_sessions", _boom)

    # Probe returns a pane_id so the focus path completes.
    from api.services import cc_pane_locate
    captured_jsonl: dict[str, str] = {}

    def _probe(path, env=None, proc_root="/proc"):
        captured_jsonl["path"] = path
        return 55
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript", _probe)

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _OK())

    r = client.post(f"/api/agents/sessions/cc:{sid_bare}/focus")
    assert r.status_code == 200, r.text
    assert r.json()["pane_id"] == 55

    # The probe was handed the jsonl from the *target* project dir, not the decoy.
    assert captured_jsonl["path"] == str(proj_b / f"{sid_bare}.jsonl")


# ---------------------------------------------------------------------------
# #257 — cache invalidation across wezterm restart
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_focus_invalidates_cache_when_wezterm_pid_no_longer_live(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """The acceptance test for #257: a cached mapping written under one
    wezterm boot must NOT be honored after a wezterm restart (pane ids
    reset, so cached pane_id=5 could activate a different session's pane
    in the new wezterm). The focus endpoint should drop the stale mapping
    and re-probe instead.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    # Pre-restart: mapping cached under wezterm boot pid 11111.
    wezterm_store.upsert(sid, pane_id=5, cwd="/repo", wezterm_pid=11111)

    # Post-restart: live wezterm pid is now 22222 (a different process).
    # The cached pid is no longer in the live set → mapping is stale.
    monkeypatch.setattr("api.routes.agents._live_wezterm_pids", lambda xdg: {22222})

    # Probe finds pane 7 in the new wezterm — the session has a fresh pane.
    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": 7)
    # And pin the "current wezterm pid" the upsert path records.
    monkeypatch.setattr("api.routes.agents._current_wezterm_pid", lambda xdg: 22222)

    calls: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _OK()

    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 200, r.text
    assert r.json()["pane_id"] == 7

    # The stale cached pane (5) was NOT activated — only the freshly
    # probed pane (7) was.
    activate_calls = [argv for argv in calls if "activate-pane" in argv]
    assert len(activate_calls) == 1
    assert "7" in activate_calls[0]
    assert "5" not in activate_calls[0]

    # The mapping was refreshed with the new wezterm pid.
    mapping = wezterm_store.get(sid)
    assert mapping is not None
    assert mapping.pane_id == 7
    assert mapping.wezterm_pid == 22222


@pytest.mark.unit
def test_focus_invalidates_pre_257_rows_with_wezterm_pid_zero(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """Mappings written before #257 lack a wezterm boot id (DEFAULT 0 on
    migration). The focus endpoint treats wezterm_pid=0 as stale so the
    first Go To after the upgrade re-probes and pins a real pid.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    # wezterm_pid defaults to 0 — simulates a row migrated from pre-#257.
    wezterm_store.upsert(sid, pane_id=5, cwd="/repo")
    assert wezterm_store.get(sid).wezterm_pid == 0

    monkeypatch.setattr("api.routes.agents._live_wezterm_pids", lambda xdg: {33333})
    monkeypatch.setattr("api.routes.agents._current_wezterm_pid", lambda xdg: 33333)

    from api.services import cc_pane_locate
    monkeypatch.setattr(cc_pane_locate, "locate_pane_for_transcript",
                        lambda path, env=None, proc_root="/proc": 9)

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _OK())

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 200, r.text
    assert r.json()["pane_id"] == 9

    # Refreshed mapping now carries a real wezterm_pid.
    mapping = wezterm_store.get(sid)
    assert mapping.pane_id == 9
    assert mapping.wezterm_pid == 33333


@pytest.mark.unit
def test_focus_honors_cache_when_wezterm_pid_matches_one_live_gui(
    client, wezterm_store, synthetic_session, monkeypatch,
):
    """Multi-mux: several wezterm-gui instances running simultaneously.
    The cache is valid if its recorded pid is in the live set (regardless
    of which gui is "primary" right now).
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "cc_resume_enabled", True)
    _, sid = synthetic_session

    wezterm_store.upsert(sid, pane_id=12, cwd="/repo", wezterm_pid=11111)
    # Two live wezterm-gui processes; the cache pid is one of them.
    monkeypatch.setattr("api.routes.agents._live_wezterm_pids",
                        lambda xdg: {11111, 99999})

    class _OK:
        returncode = 0
        stdout = b""
        stderr = b""
    calls: list[list[str]] = []
    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _OK()
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "wezterm" else None)
    monkeypatch.setattr("subprocess.run", _fake_run)

    r = client.post(f"/api/agents/sessions/{sid}/focus")
    assert r.status_code == 200, r.text
    # Activate-pane was called with the cached pane — no probe needed.
    assert any("activate-pane" in argv and "12" in argv for argv in calls)


@pytest.mark.unit
def test_cc_pane_bind_records_current_wezterm_pid(
    client, wezterm_store, monkeypatch,
):
    """The SessionStart hook → /cc-pane-bind path captures the current
    wezterm boot id so subsequent focus calls can validate against it.
    """
    monkeypatch.setattr("api.routes.agents._current_wezterm_pid", lambda xdg: 44444)

    r = client.post("/api/agents/cc-pane-bind", json={
        "session_id": "bind-target",
        "pane_id": 3,
        "cwd": "/x",
    })
    assert r.status_code == 200, r.text

    mapping = wezterm_store.get("cc:bind-target")
    assert mapping is not None
    assert mapping.pane_id == 3
    assert mapping.wezterm_pid == 44444
