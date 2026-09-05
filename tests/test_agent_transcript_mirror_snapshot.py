"""Snapshot-ingest tests for mirrored remote transcripts.

Covers the acceptance criteria that `GET /api/agents/snapshot` returns one
row for a mirrored Claude Code/Codex session, host attribution, the
event-over-transcript status merge (reusing `_apply_cli_session_to_dict`
against a MIRRORED row instead of a local one), liveness exclusion (a
mirrored row must never be promoted to `running` by a local process scan),
and id-collision resolution (local transcript wins).

Builds real synthetic jsonl fixtures under a tmp mirror root and exercises
the real `agent_transcript_mirror.mirrored_snapshot()` -> `_build_snapshot()`
path — not a mocked snapshot — so the liveness-exclusion wiring
(`build_snapshot(..., live_counts={})`) is actually under test.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services import agent_transcript_mirror
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit

# Captured before any test monkeypatches `agents_route._claude_code_snapshot`
# to a stub — lets the liveness-exclusion contrast test restore the REAL
# local scan for its second half without depending on monkeypatch's
# per-test teardown ordering relative to its own re-patch.
_REAL_CLAUDE_CODE_SNAPSHOT = agents_route._claude_code_snapshot


def _iso(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_cc_transcript(path: Path, session_id: str) -> None:
    """A minimal-but-real Claude Code transcript: one user turn, one
    assistant turn with real usage tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": _iso(-10), "message": {"role": "user", "content": "hello"}},
        {
            "type": "assistant",
            "timestamp": _iso(-5),
            "message": {
                "role": "assistant", "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "hi there"}],
                "usage": {
                    "input_tokens": 321, "output_tokens": 654,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                },
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "api_host_name", lambda: "this-api-host")
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def mirror_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "mirror-root"
    monkeypatch.setattr(agent_transcript_mirror, "mirror_root", lambda: root)
    return root


@pytest.fixture
def client():
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# One row, host attribution, real token/dollar fields
# ---------------------------------------------------------------------------


def test_mirrored_cc_session_produces_one_row_with_host_and_real_tokens(client, stores, mirror_root):
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-1.jsonl", "sess-1")

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cc:sess-1"]
    assert len(matches) == 1
    row = matches[0]
    assert row["host"] == "laptop"
    assert row["mirrored"] is True
    assert row["total_input_tokens"] == 321
    assert row["total_output_tokens"] == 654
    assert row["source"] == "claude_code"


def test_mirrored_subagent_spawn_edge_is_preserved(client, stores, mirror_root):
    """`_build_snapshot`'s merge block extends `edges` with
    `build_snapshot`'s edges return value, so a mirrored session's own
    Task/Agent tool-use spawn produces both the synthetic subagent NODE
    and the edge connecting it to its parent, rather than rendering the
    node disconnected in the graph."""
    from tests.test_claude_code_ingest import _assistant_event

    proj = mirror_root / "laptop" / "claude_code" / "-home-user-proj"
    proj.mkdir(parents=True)
    with (proj / "sess-edge.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(_assistant_event(tool_uses=[
            {"id": "tu_mirror_1", "name": "Agent", "input": {"prompt": "child task"}},
        ])) + "\n")

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    ids = [s["session_id"] for s in body["sessions"]]
    child_id = next((i for i in ids if ":agent:tu_mirror_1" in i), None)
    assert child_id is not None, f"expected a synthetic subagent row, got {ids}"
    assert any(
        e["type"] == "spawn" and e["from"] == "cc:sess-edge" and e["to"] == child_id
        for e in body["edges"]
    ), f"expected a spawn edge cc:sess-edge -> {child_id}, got {body['edges']}"


def test_mirrored_subagent_spawn_edge_survives_hook_host_relabel(client, stores, mirror_root):
    """The parent row has a hook (`cli_sessions`) row whose `host` differs
    from the mirror directory name it was ingested from — e.g. the
    registry key `mac-mini` used by `LIFEOS_AGENT_HOSTS` versus the
    machine's own reported hostname `Mac-Mini`. The subagent row has no
    hook row of its own (its id is synthesized from the tool_use id), so
    it keeps the mirror directory's host untouched.

    Comparing the two endpoints' hosts must use the SOURCE host each row
    was mirrored from, not the host left in `sd["host"]` after the hook
    overlay ran on the parent — otherwise a mismatched registry key vs.
    hostname makes every mirrored parent+subagent edge look like it
    crosses hosts and gets dropped.
    """
    from tests.test_claude_code_ingest import _assistant_event

    session_store, _ = stores
    proj = mirror_root / "Mac-Mini" / "claude_code" / "-home-user-proj"
    proj.mkdir(parents=True)
    with (proj / "sess-relabel.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(_assistant_event(tool_uses=[
            {"id": "tu_relabel_1", "name": "Agent", "input": {"prompt": "child task"}},
        ])) + "\n")

    session_store.record_cli_session_event(
        engine="claude_code", event="user_prompt_submit",
        session_id="sess-relabel", host="mac-mini", cwd="/home/user/proj",
    )

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    parent = next(s for s in body["sessions"] if s["session_id"] == "cc:sess-relabel")
    assert parent["host"] == "mac-mini"  # hook-overlaid host wins for the parent row
    child_id = next(
        (s["session_id"] for s in body["sessions"] if ":agent:tu_relabel_1" in s["session_id"]),
        None,
    )
    assert child_id is not None, f"expected a synthetic subagent row, got {[s['session_id'] for s in body['sessions']]}"
    assert any(
        e["type"] == "spawn" and e["from"] == "cc:sess-relabel" and e["to"] == child_id
        for e in body["edges"]
    ), f"expected a spawn edge cc:sess-relabel -> {child_id} despite the host relabel, got {body['edges']}"


def test_mirrored_codex_session_produces_one_row_with_host(client, stores, mirror_root):
    cx_dir = mirror_root / "studio" / "codex" / "2026" / "08" / "01"
    cx_dir.mkdir(parents=True)
    rollout = cx_dir / "rollout-2026-08-01T00-00-00-cx-sess-2.jsonl"
    lines = [
        {"type": "session_meta", "timestamp": _iso(-10), "payload": {"cwd": "/home/user/proj2"}},
        {"type": "event_msg", "timestamp": _iso(-5), "payload": {"type": "user_message", "message": "hi"}},
    ]
    with rollout.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cx:cx-sess-2"]
    assert len(matches) == 1
    assert matches[0]["host"] == "studio"
    assert matches[0]["mirrored"] is True
    assert matches[0]["source"] == "codex"


# ---------------------------------------------------------------------------
# Status merge: hook events win over the transcript's own inferred status;
# token/cost stay transcript-derived.
# ---------------------------------------------------------------------------


def test_status_merge_prefers_hook_events_over_mirrored_transcript(client, stores, mirror_root):
    session_store, _ = stores
    _write_cc_transcript(mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-3.jsonl", "sess-3")

    session_store.record_cli_session_event(
        engine="claude_code", event="user_prompt_submit",
        session_id="sess-3", host="laptop", cwd="/home/user/proj",
    )

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cc:sess-3"]
    assert len(matches) == 1
    row = matches[0]
    assert row["status"] == "running"  # from the hook event, not the transcript's mtime guess
    assert row["status_inferred"] is False
    # Token/cost detail is still the transcript's, not zeroed by the merge.
    assert row["total_input_tokens"] == 321
    assert row["total_output_tokens"] == 654


def test_mirrored_row_with_fresh_mtime_and_no_hook_row_is_not_running(client, stores, mirror_root):
    """Round-1 finding #4 (the negative half — the positive half is
    `test_status_merge_prefers_hook_events_over_mirrored_transcript`
    above, which already proves a hook row reporting `running` survives
    onto a mirrored row untouched, `status_inferred: False`).

    AC 7 is unambiguous: "a remote session's `running` status comes only
    from hook events." `live_counts={}` only blocks `_infer_status`'s
    PROCESS-SCAN branch (`has_live_process`) — its separate
    mtime-under-10-minutes branch still returns `("running", True)`
    regardless of `live_counts`, since `_write_cc_transcript` writes the
    file "now". With NO cli_sessions row at all (zero hook events), a
    freshly-mirrored transcript must not show `running`."""
    _write_cc_transcript(
        mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-fresh.jsonl", "sess-fresh",
    )

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cc:sess-fresh"]
    assert len(matches) == 1
    # Pin the exact demotion TARGET, not merely "not
    # running" — `showResume` (web/agents/panel.js) only renders Resume
    # for a status in {TERMINAL, "inactive", "yielded"}, so demoting to
    # any other non-resumable status (e.g. "blocked", "idle") would still
    # satisfy `!= "running"` while hiding the Resume control on every
    # freshly-mirrored remote session — the exact defect this guard exists
    # to prevent.
    assert matches[0]["status"] == "inactive"
    assert matches[0]["status_inferred"] is True


def test_demoted_status_is_a_showresume_eligible_status():
    """Ties `_demote_inferred_running`'s hardcoded
    demotion target to the frontend's `showResumeFor` acceptance set, so a
    change to either side that breaks the pairing is caught here rather
    than only by a live drawer. `web/agents/panel.js`'s `showResumeFor`
    accepts exactly `{"inactive", "yielded"}` plus the TERMINAL statuses
    (`completed`, `failed`, `budget_exceeded`, `ended` — see `TERMINAL` in
    `web/agents/panel.js`); demoting to anything outside that set (e.g.
    "blocked", "idle") would hide Resume on every freshly-mirrored session."""
    import re

    panel_js = Path(__file__).resolve().parent.parent / "web" / "agents" / "panel.js"
    source = panel_js.read_text(encoding="utf-8")
    # `updateMeta` and `_renderHeader` both call a single shared
    # `showResumeFor(s)` function — anchor on that function body (not
    # `const showResume = ...`, which isn't the definition site) so this
    # binds the one copy both call sites share, rather than a copy that
    # could drift.
    match = re.search(
        r"function showResumeFor\(s\) \{[\s\S]*?status === '(\w+)' \|\| s\.status === '(\w+)'\)",
        source,
    )
    assert match, "showResumeFor's status literals moved — update this test's regex"
    eligible_non_terminal = {match.group(1), match.group(2)}

    row: dict = {"status": "running", "status_inferred": True}
    agent_transcript_mirror._demote_inferred_running(row)
    assert row["status"] in eligible_non_terminal, (
        f"_demote_inferred_running's target {row['status']!r} is not one of "
        f"showResume's eligible non-terminal statuses {eligible_non_terminal!r} "
        "— Resume would be hidden on every freshly-mirrored session"
    )


def test_hook_reported_running_survives_even_if_demotion_ran_after_the_merge():
    """The `test_status_merge_prefers_hook_events_over_mirrored_transcript`
    test above doesn't bind the demote-before-overlay ordering: it would
    pass identically even with `_demote_inferred_running` deleted
    entirely, since `_apply_cli_session_to_dict` always overwrites
    `status` last in the real pipeline.

    This test proves the actual safety property instead: even if a future
    refactor called `_demote_inferred_running` a second time AFTER the
    hook overlay already ran, a hook row genuinely reporting `running`
    must still survive, because `_demote_inferred_running` only touches a
    row whose `status_inferred` is `True` — and the overlay always sets
    it `False`. Mutation-proved: dropping that guard (demoting whenever
    `status == "running"`, regardless of `status_inferred`) makes this
    test fail while leaving the pipeline's own call site behavior
    unchanged."""
    from api.routes.agents import _apply_cli_session_to_dict
    from api.services.agent_worker.session_store import CliSession

    row: dict = {"status": "running", "status_inferred": True}
    cli = CliSession(
        session_id="cc:sess-order", engine="claude_code", host="laptop",
        status="running", started_at=0, last_event_at=0,
        cwd="/home/user/proj", branch="", prompt_preview="",
    )

    # Overlay first (hook event wins), THEN demotion — a reversed order a
    # future refactor might introduce.
    _apply_cli_session_to_dict(row, cli)
    agent_transcript_mirror._demote_inferred_running(row)

    assert row["status"] == "running"
    assert row["status_inferred"] is False


# ---------------------------------------------------------------------------
# Cache aliasing: a hook overlay must never leak into the
# ingest cache that `mirrored_snapshot()` reads from.
# ---------------------------------------------------------------------------


def test_hook_overlay_does_not_leak_into_ingest_cache_within_ttl(
    client, stores, mirror_root,
):
    """`cc.build_snapshot()`/`cx.build_snapshot()` return
    `list(entry.sessions)` — a shallow copy of the LIST, so the row DICTS
    inside are the SAME objects held by `cc`'s own ingest cache
    (`_snapshot_cache`, 30s default TTL — live in production via
    `mirrored_snapshot()`'s own `cache_ttl=30.0` default). Without
    `mirrored_snapshot()` copying each row before mutating it (`host`,
    `mirrored`, the demotion), `_build_snapshot`'s hook overlay
    (`_apply_cli_session_to_dict`) would mutate them AGAIN with a genuine
    `status="running", status_inferred=False` — writing that straight into
    the cached entry. If the hook row then vanishes (pruned, or a failed
    `list_cli_sessions()` call) and a rebuild happens INSIDE the cache TTL,
    `_demote_inferred_running` (guarded on `status_inferred is True`) can't
    fix the now-`False` cached row, so it replays `running` with zero
    hook evidence — a straight violation of AC 7 ("a remote session's
    `running` status comes only from hook events").

    Mutation-proved: reverting `mirrored_snapshot()`'s `sessions = [dict(row)
    for row in sessions]` copy (mutating the cache's own dicts again)
    makes this fail — the second snapshot stays `running`/`status_inferred:
    False` instead of demoting back to `inactive`/`True`."""
    import sqlite3

    session_store, _ = stores
    _write_cc_transcript(
        mirror_root / "laptop" / "claude_code" / "-home-user-proj" / "sess-ttl.jsonl", "sess-ttl",
    )

    # A genuine hook event ("user_prompt_submit" -> CLI_STATUS_RUNNING)
    # overlays `status="running", status_inferred=False` onto the mirrored
    # row on this first request.
    session_store.record_cli_session_event(
        engine="claude_code", event="user_prompt_submit",
        session_id="sess-ttl", host="laptop", cwd="/home/user/proj",
    )

    r1 = client.get("/api/agents/snapshot")
    matches1 = [s for s in r1.json()["sessions"] if s["session_id"] == "cc:sess-ttl"]
    assert len(matches1) == 1
    assert matches1[0]["status"] == "running"
    assert matches1[0]["status_inferred"] is False

    # Drop the hook row — simulates it being pruned, or a `list_cli_sessions`
    # read that raced ahead and missed it. Deletes directly against the
    # store's sqlite file rather than through a public API, since
    # SessionStore exposes no delete for cli_sessions rows.
    conn = sqlite3.connect(str(session_store.db_path))
    try:
        conn.execute("DELETE FROM cli_sessions WHERE session_id = ?", ("cc:sess-ttl",))
        conn.commit()
    finally:
        conn.close()

    # Rebuild INSIDE the cache TTL (no sleep, no invalidate_cache() call) —
    # the whole point is that this hits the still-warm 30s cache.
    r2 = client.get("/api/agents/snapshot")
    matches2 = [s for s in r2.json()["sessions"] if s["session_id"] == "cc:sess-ttl"]
    assert len(matches2) == 1
    assert matches2[0]["status"] == "inactive", (
        "the hook overlay leaked into the ingest cache — a rebuild inside "
        "the TTL replayed 'running' with no hook row backing it"
    )
    assert matches2[0]["status_inferred"] is True


# ---------------------------------------------------------------------------
# Liveness exclusion
# ---------------------------------------------------------------------------


def test_mirrored_row_not_promoted_to_running_by_local_process_scan(client, stores, mirror_root, monkeypatch):
    """A mirrored session's cwd matching a LOCAL live `claude` process must
    NOT promote it to `running` — that would be a false liveness signal for
    a process that isn't actually on this machine. Contrast with an
    equivalent LOCAL row, which the live-process-scan liveness check DOES
    promote."""
    cc_dir = mirror_root / "laptop" / "claude_code" / "-home-user-proj"
    _write_cc_transcript(cc_dir / "sess-4.jsonl", "sess-4")
    # Old mtime so the mtime-only heuristic alone would NOT say "running".
    import os
    old = time.time() - 3600
    os.utime(cc_dir / "sess-4.jsonl", (old, old))

    from api.services.claude_code import session_ingest as cc
    # Pretend a LOCAL claude process has this cwd live — if liveness
    # exclusion is broken (live_counts=None instead of {} for mirrored
    # rows), this would promote the mirrored row to `running`.
    monkeypatch.setattr(cc, "live_claude_cwd_counts", lambda now=None: {"/home/user/proj": 1})
    cc.invalidate_cache()

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cc:sess-4"]
    assert len(matches) == 1
    assert matches[0]["status"] != "running"
    assert matches[0]["status_inferred"] is True  # never touched by the (excluded) process scan

    # Contrast: an equivalent LOCAL (non-mirrored) session in the SAME cwd
    # with the SAME old mtime DOES get promoted — proving the local process
    # scan itself still works and the mirrored row is being deliberately
    # excluded, not just failing to match for an unrelated reason.
    local_projects_dir = mirror_root.parent / "local-claude-projects"
    local_cc_dir = local_projects_dir / "-home-user-proj"
    _write_cc_transcript(local_cc_dir / "sess-4-local.jsonl", "sess-4-local")
    os.utime(local_cc_dir / "sess-4-local.jsonl", (old, old))
    from config.settings import settings
    monkeypatch.setattr(settings, "claude_code_projects_dir", str(local_projects_dir), raising=False)
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", _REAL_CLAUDE_CODE_SNAPSHOT)
    cc.invalidate_cache()

    r2 = client.get("/api/agents/snapshot")
    local_matches = [s for s in r2.json()["sessions"] if s["session_id"] == "cc:sess-4-local"]
    assert len(local_matches) == 1
    assert local_matches[0]["status"] == "running"
    assert local_matches[0]["status_inferred"] is False


def test_mirrored_codex_row_not_promoted_to_running_by_local_process_scan(client, stores, mirror_root, monkeypatch):
    """Codex sibling of the claude_code liveness-exclusion test above —
    `cx.build_snapshot`'s own `live_counts` parameter must be honored the
    same way."""
    cx_dir = mirror_root / "studio" / "codex" / "2026" / "08" / "01"
    cx_dir.mkdir(parents=True)
    rollout = cx_dir / "rollout-2026-08-01T00-00-00-cx-sess-5.jsonl"
    lines = [
        {"type": "session_meta", "timestamp": _iso(-3700), "payload": {"cwd": "/home/user/proj5"}},
        {"type": "event_msg", "timestamp": _iso(-3700), "payload": {"type": "user_message", "message": "hi"}},
    ]
    with rollout.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    import os
    old = time.time() - 3600
    os.utime(rollout, (old, old))

    from api.services.codex import session_ingest as cx
    # Pretend a LOCAL codex process has this cwd live — if liveness
    # exclusion is broken, this would promote the mirrored row to `running`.
    monkeypatch.setattr(cx, "live_codex_cwd_counts", lambda now=None: {"/home/user/proj5": 1})
    cx.invalidate_cache()

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == "cx:cx-sess-5"]
    assert len(matches) == 1
    assert matches[0]["status"] != "running"
    assert matches[0]["status_inferred"] is True


# ---------------------------------------------------------------------------
# Id collision — local transcript wins
# ---------------------------------------------------------------------------


def test_id_collision_local_transcript_wins_single_row(client, stores, mirror_root, monkeypatch):
    session_id = "collide-1"
    _write_cc_transcript(
        mirror_root / "laptop" / "claude_code" / "-home-user-proj" / f"{session_id}.jsonl", session_id,
    )

    local_row = {
        "session_id": f"cc:{session_id}",
        "task_id": session_id,
        "status": "inactive",
        "routing": "claude_code",
        "parent_session_id": None,
        "root_session_id": f"cc:{session_id}",
        "spawn_depth": 0,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": 1000,
        "last_activity_at": 2000,
        "total_input_tokens": 999,  # deliberately different from the mirrored copy
        "total_output_tokens": 999,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_dollars": 0.0,
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": "local-copy",
        "model_label": "Sonnet",
        "last_event_kind": "assistant",
        "tool_call_count": 0,
        "error_count": 0,
        "source": "claude_code",
        "status_inferred": True,
        "project_key": "-home-user-proj",
        "decoded_cwd": "/home/user/proj",
    }
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([local_row], []))

    r = client.get("/api/agents/snapshot")
    matches = [s for s in r.json()["sessions"] if s["session_id"] == f"cc:{session_id}"]
    assert len(matches) == 1  # exactly one row despite existing in both sources
    assert matches[0]["total_input_tokens"] == 999  # the LOCAL copy's data, not the mirrored one's
    assert matches[0].get("mirrored") is not True


def test_id_collision_does_not_reparent_mirrored_subagent_onto_local_row(
    client, stores, mirror_root, monkeypatch,
):
    """`cc:collide-2` exists LOCALLY (no subagent) and is
    separately MIRRORED from `studio` WITH a subagent spawn. The id
    collision means the local row wins (same as the test above) — but the
    mirrored session's spawn edge must NOT survive by attaching itself to
    the (unrelated) local row that happens to share the id: that would
    show the LOCAL session spawning a subagent it never spawned. Mutation-
    proved: reverting the edge filter to check `from`/`to` against the
    broader `local_ids` (which already contains every collided id) instead
    of `appended_mirrored_ids` makes this edge reappear."""
    from tests.test_claude_code_ingest import _assistant_event

    session_id = "collide-2"
    proj = mirror_root / "studio" / "claude_code" / "-remote-elsewhere"
    proj.mkdir(parents=True)
    with (proj / f"{session_id}.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(_assistant_event(tool_uses=[
            {"id": "tu_collide_2", "name": "Agent", "input": {"prompt": "child task"}},
        ])) + "\n")

    local_row = {
        "session_id": f"cc:{session_id}",
        "task_id": session_id,
        "status": "inactive",
        "routing": "claude_code",
        "parent_session_id": None,
        "root_session_id": f"cc:{session_id}",
        "spawn_depth": 0,
        "yield_waiting_for": [],
        "managed_agent_session_id": None,
        "started_at": 1000,
        "last_activity_at": 2000,
        "total_input_tokens": 111,
        "total_output_tokens": 111,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_dollars": 0.0,
        "total_active_seconds": 0.0,
        "expected_output": None,
        "label": "local-copy",
        "model_label": "Sonnet",
        "last_event_kind": "assistant",
        "tool_call_count": 0,
        "error_count": 0,
        "source": "claude_code",
        "status_inferred": True,
        "project_key": "-home-user-localproj",
        "decoded_cwd": "/home/user/localproj",
    }
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([local_row], []))

    r = client.get("/api/agents/snapshot")
    body = r.json()
    matches = [s for s in body["sessions"] if s["session_id"] == f"cc:{session_id}"]
    assert len(matches) == 1
    assert matches[0]["decoded_cwd"] == "/home/user/localproj"  # the local row won, as above

    # The child subagent row itself doesn't collide (its id is derived
    # from the tool_use id) and still gets mirrored in as its own row —
    # but no edge should point FROM the collided parent id at all.
    ids = [s["session_id"] for s in body["sessions"]]
    child_id = next((i for i in ids if ":agent:tu_collide_2" in i), None)
    assert child_id is not None, f"expected the mirrored subagent's own row, got {ids}"
    assert not any(
        e["type"] == "spawn" and e["from"] == f"cc:{session_id}"
        for e in body["edges"]
    ), f"a collided mirrored session's spawn edge leaked onto the local row: {body['edges']}"


def test_mirrored_spawn_edge_not_duplicated_when_mirrored_from_two_hosts(
    client, stores, mirror_root,
):
    """The identical session+subagent transcript mirrored
    from TWO hosts must not emit the same spawn edge twice — each host's
    own `cc.build_snapshot()` call independently derives the same
    `(from, to, type)` edge, and only ONE copy should survive the merge."""
    from tests.test_claude_code_ingest import _assistant_event

    payload = json.dumps(_assistant_event(tool_uses=[
        {"id": "tu_dup_1", "name": "Agent", "input": {"prompt": "child task"}},
    ])) + "\n"
    for host in ("hostA", "hostB"):
        proj = mirror_root / host / "claude_code" / "-remote-dupproj"
        proj.mkdir(parents=True)
        (proj / "dup-sess.jsonl").write_text(payload)

    r = client.get("/api/agents/snapshot")
    body = r.json()
    spawn_edges = [
        e for e in body["edges"]
        if e["type"] == "spawn" and e["from"] == "cc:dup-sess"
    ]
    assert len(spawn_edges) == 1, f"expected exactly one spawn edge, got {spawn_edges}"


def test_mirrored_vs_mirrored_id_collision_does_not_reparent_subagent_across_hosts(
    client, stores, mirror_root,
):
    """`test_id_collision_does_not_reparent_mirrored_subagent_onto_local_row`
    above covers the local-vs-mirrored id-collision edge leak; this covers
    the MIRRORED-vs-MIRRORED case: the same session id present on TWO
    mirrored hosts (e.g. two `LIFEOS_AGENT_HOSTS` entries resolving to the
    same machine) must not re-parent a subagent edge across hosts.
    `hostA` mirrors `cc:shared-parent` with no subagent;
    `hostB` mirrors a DIFFERENT session that happens to carry the SAME id
    `cc:shared-parent`, WITH a subagent spawn. `hostA` sorts first
    (`_mirrored_host_dirs()` is alphabetical), so `hostA`'s row wins the id
    collision and is the one actually present in the final snapshot under
    `cc:shared-parent`. `hostB`'s child subagent row doesn't collide (its id
    is derived from the tool_use id) and is mirrored in as its own row —
    but the spawn edge `cc:shared-parent -> child` must NOT survive: the
    parent id in the snapshot belongs to `hostA`'s unrelated session, not
    `hostB`'s. Mutation-proved: reverting to a flat appended-ids SET
    (dropping the per-host mapping) makes this edge reappear, because
    `cc:shared-parent` and the child id are both "appended" ids even though
    they came from different hosts' batches."""
    from tests.test_claude_code_ingest import _assistant_event

    session_id = "shared-parent"
    proj_a = mirror_root / "hostA" / "claude_code" / "-remote-hosta-proj"
    proj_a.mkdir(parents=True)
    (proj_a / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": _iso(-10),
                    "message": {"role": "user", "content": "hello from hostA"}}) + "\n"
    )

    proj_b = mirror_root / "hostB" / "claude_code" / "-remote-hostb-proj"
    proj_b.mkdir(parents=True)
    (proj_b / f"{session_id}.jsonl").write_text(
        json.dumps(_assistant_event(tool_uses=[
            {"id": "tu_reparent", "name": "Agent", "input": {"prompt": "child task"}},
        ])) + "\n"
    )

    r = client.get("/api/agents/snapshot")
    body = r.json()
    matches = [s for s in body["sessions"] if s["session_id"] == f"cc:{session_id}"]
    assert len(matches) == 1
    assert matches[0]["host"] == "hostA"  # confirms hostA's row is the one that won

    ids = [s["session_id"] for s in body["sessions"]]
    child_id = next((i for i in ids if ":agent:tu_reparent" in i), None)
    assert child_id is not None, f"expected hostB's mirrored subagent row, got {ids}"
    assert any(s["session_id"] == child_id and s["host"] == "hostB" for s in body["sessions"])

    assert not any(
        e["type"] == "spawn" and e["from"] == f"cc:{session_id}" and e["to"] == child_id
        for e in body["edges"]
    ), (
        f"hostB's subagent edge re-parented onto hostA's unrelated "
        f"cc:{session_id} row: {body['edges']}"
    )
