"""API tests for the read-only agent activity visualization (issue #133).

Covers /api/agents/snapshot, /api/agents/sessions/{sid}/events, and the
per-session SSE transcript tail. Uses temp-dir-backed stores via monkeypatch
so the real data/ directory is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.routes import agents as agents_route
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Point the agents route at temp-dir-backed stores for the duration of the test."""
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    # Clear the label cache so labels are recomputed against the test fixtures.
    agents_route._label_cache.clear()
    # Disable the Claude Code and Codex unions so these tests only exercise the
    # LifeOS agent path — otherwise the snapshot would mix in real CC sessions
    # from ~/.claude/projects/ and Codex sessions from the test machine.
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))
    yield session_store, transcript_store
    agents_route._label_cache.clear()


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.mark.unit
def test_snapshot_empty(client, stores):
    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert body["edges"] == []
    assert "generated_at" in body


@pytest.mark.unit
def test_snapshot_sessions_and_edges(client, stores):
    session_store, transcript_store = stores
    parent = session_store.create(task_id="root-task", status=STATUS_RUNNING, routing="claude")
    # Spawned child under the same root.
    child = session_store.create(
        task_id="child-task",
        status=STATUS_RUNNING,
        routing="local",
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    transcript_store.append(parent.session_id, "claim", {"description": "Synthesize the briefing"})
    transcript_store.append(parent.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(parent.session_id, "tool_call", {"name": "lifeos_calendar_upcoming"})
    transcript_store.append(child.session_id, "spawn", {"prompt": "Sub-task: find conflicts"})
    transcript_store.append(child.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(child.session_id, "managed_failed", {"reason": "synthetic test"})

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    body = r.json()
    sessions = {s["session_id"]: s for s in body["sessions"]}
    assert parent.session_id in sessions
    assert child.session_id in sessions

    parent_s = sessions[parent.session_id]
    assert parent_s["label"] == "Synthesize the briefing"
    assert parent_s["tool_call_count"] == 2
    assert parent_s["error_count"] == 0
    assert parent_s["last_event_kind"] == "tool_call"
    assert parent_s["routing"] == "claude"

    child_s = sessions[child.session_id]
    assert child_s["tool_call_count"] == 1
    # `managed_failed` should be counted as an error.
    assert child_s["error_count"] == 1
    assert child_s["last_event_kind"] == "managed_failed"

    edges = body["edges"]
    assert {"from": parent.session_id, "to": child.session_id, "type": "spawn"} in edges


@pytest.mark.unit
def test_events_endpoint_returns_tail(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    for i in range(10):
        transcript_store.append(s.session_id, "tool_call", {"i": i})

    r = client.get(f"/api/agents/sessions/{s.session_id}/events?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10
    assert len(body["events"]) == 3
    # Tail should be the most-recent 3, in order.
    assert [ev["payload"]["i"] for ev in body["events"]] == [7, 8, 9]


@pytest.mark.unit
def test_events_endpoint_rejects_traversal(client, stores):
    r = client.get("/api/agents/sessions/..%2Fetc/events")
    # FastAPI normalizes %2F so the traversal token reaches the handler;
    # either 400 (validation) or 404 (path mismatch) is acceptable so long as
    # the endpoint does not return file contents from outside the transcripts dir.
    assert r.status_code in (400, 404)

    # Direct traversal segment in the session_id parameter.
    r2 = client.get("/api/agents/sessions/foo..bar/events")
    assert r2.status_code == 400


@pytest.mark.unit
def test_events_endpoint_missing_session(client, stores):
    r = client.get("/api/agents/sessions/missing-session/events")
    assert r.status_code == 200
    assert r.json() == {"session_id": "missing-session", "events": [], "total": 0}


@pytest.mark.unit
def test_per_session_stream_backfill_and_terminate_on_completed(client, stores):
    """Verify the per-session SSE stream sends backfill events and closes on terminal status."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "claim", {"description": "Test session"})
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})

    # Mark the session terminal so the stream closes promptly when the
    # terminal-check fires inside the generator.
    session_store.update_status("t1", STATUS_COMPLETED)

    with client.stream("GET", f"/api/agents/sessions/{s.session_id}/stream?backfill=5") as resp:
        assert resp.status_code == 200
        chunks: list[str] = []
        # Collect output until the stream closes (it should, after the
        # generator hits the terminal-status check). Bail out defensively
        # after a reasonable amount of data.
        for chunk in resp.iter_text():
            chunks.append(chunk)
            if "event: closed" in chunk:
                break
            if sum(len(c) for c in chunks) > 50_000:
                pytest.fail("stream did not close on terminal status")
        body = "".join(chunks)
        assert "event: transcript_event" in body
        assert '"kind": "claim"' in body
        assert '"kind": "tool_call"' in body
        assert "event: closed" in body


@pytest.mark.unit
def test_per_session_stream_rejects_traversal(client, stores):
    r = client.get("/api/agents/sessions/foo..bar/stream")
    assert r.status_code == 400


@pytest.mark.unit
def test_per_session_stream_emits_events_before_close(client, stores):
    """Verifies the stream emits transcript_event chunks before the terminal close event."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-mid", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "claim", {"description": "Running session"})
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})
    session_store.update_status("t-mid", STATUS_COMPLETED)

    transcript_seen = False
    closed = False
    with client.stream("GET", f"/api/agents/sessions/{s.session_id}/stream?backfill=5") as resp:
        assert resp.status_code == 200
        for chunk in resp.iter_text():
            if "event: transcript_event" in chunk and not closed:
                transcript_seen = True
            if "event: closed" in chunk:
                closed = True
                break
    assert transcript_seen, "transcript_event chunks should arrive before the close event"
    assert closed, "stream should emit event: closed when the session is terminal"


@pytest.mark.unit
def test_label_falls_back_to_task_manager_for_root_sessions(client, stores, monkeypatch):
    """When transcript events don't carry a description (the real worker case),
    the label should come from the task manager rather than being cached as
    the task_id fallback."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-tm", status=STATUS_RUNNING)
    # Real worker emits this payload — no description.
    transcript_store.append(s.session_id, "claim", {"task_id": "t-tm", "worker": "agent-worker"})

    # Stub the task manager so the label lookup hits a known value.
    class StubTask:
        description = "Review the Q4 budget"

    class StubManager:
        def get(self, task_id):
            return StubTask() if task_id == "t-tm" else None

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: StubManager(),
    )

    r = client.get("/api/agents/snapshot")
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["label"] == "Review the Q4 budget"


@pytest.mark.unit
def test_error_count_includes_killed_kinds(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-killed", status=STATUS_RUNNING)
    transcript_store.append(s.session_id, "tool_call", {"name": "lifeos_search"})
    transcript_store.append(s.session_id, "killed", {"by": "operator"})
    transcript_store.append(s.session_id, "cascade_killed", {"reason": "parent killed"})

    r = client.get("/api/agents/snapshot")
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["error_count"] == 2
    assert sess["tool_call_count"] == 1


@pytest.mark.unit
def test_label_cache_does_not_pin_fallback(client, stores, monkeypatch):
    """If the first snapshot tick fires before any descriptive event lands,
    the fallback (`task_id`) must not get cached. A subsequent tick with the
    real label available should pick it up."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t-race", status=STATUS_RUNNING)
    # No transcript yet — simulate the race.

    class _MissingManager:
        def get(self, task_id):
            return None

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: _MissingManager(),
    )

    r1 = client.get("/api/agents/snapshot")
    sess1 = next(x for x in r1.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess1["label"] == "t-race"  # fallback to task_id

    # Now the real description becomes available (later claim event lands, OR
    # task_manager would resolve it). The cache must NOT have pinned the
    # fallback, so the new label takes effect.
    class _StubTask:
        description = "Real label arrives late"

    class _StubManager:
        def get(self, task_id):
            return _StubTask()

    monkeypatch.setattr(
        "api.services.task_manager.get_task_manager",
        lambda: _StubManager(),
    )

    r2 = client.get("/api/agents/snapshot")
    sess2 = next(x for x in r2.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess2["label"] == "Real label arrives late"


@pytest.mark.unit
def test_snapshot_filters_yield_waiting_for(client, stores):
    """Ensure yield_waiting_for survives JSON round-trip as an array."""
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_BLOCKED)
    session_store.set_yield_waiting_for(s.task_id, ["child-1", "child-2"])

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    sess = next(x for x in r.json()["sessions"] if x["session_id"] == s.session_id)
    assert sess["yield_waiting_for"] == ["child-1", "child-2"]


@pytest.mark.unit
def test_label_truncates_long_descriptions(client, stores):
    session_store, transcript_store = stores
    s = session_store.create(task_id="t1", status=STATUS_RUNNING)
    long_desc = "x" * 200
    transcript_store.append(s.session_id, "claim", {"description": long_desc})

    r = client.get("/api/agents/snapshot")
    sess = r.json()["sessions"][0]
    assert len(sess["label"]) <= 60
    assert sess["label"].endswith("…")


@pytest.mark.unit
def test_model_label_local_routing(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t-local", status=STATUS_RUNNING, routing="local")
    r = client.get("/api/agents/snapshot")
    sess = r.json()["sessions"][0]
    assert sess["model_label"] == "Local"


@pytest.mark.unit
def test_model_label_claude_routing_derives_from_settings(client, stores, monkeypatch):
    """Claude-routed sessions surface a short label derived from the configured
    `agent_managed_model` setting — Sonnet / Haiku / Opus / Claude."""
    from config import settings as settings_mod
    session_store, _ = stores
    session_store.create(task_id="t-claude", status=STATUS_RUNNING, routing="claude")

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-haiku-4-5")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Haiku"

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-sonnet-4-6")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Sonnet"

    monkeypatch.setattr(settings_mod.settings, "agent_managed_model", "claude-opus-4-7")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Opus"


# ---------------------------------------------------------------------------
# model_label per routing arm, host passthrough, board-assignment field
# passthrough. A session parked on `ask` must never render a Claude tier
# guess; `remote` must honor the configured label; `hermes` must never
# surface the model it ran, staying plain "Hermes" regardless of any
# observed turn; `claude_code`/`codex` routing (an operator-directed
# escalation, not just CLI ingest) must not fall through to a Claude-tier
# guess either.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_label_ask_routing_is_waiting_on_you(client, stores):
    """A session parked waiting on the operator must never render a Claude
    model name."""
    session_store, _ = stores
    session_store.create(task_id="t-ask", status=STATUS_RUNNING, routing="ask")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Waiting on you"


@pytest.mark.unit
def test_model_label_remote_routing_uses_configured_label(client, stores, monkeypatch):
    from config import settings as settings_mod
    session_store, _ = stores
    session_store.create(task_id="t-remote", status=STATUS_RUNNING, routing="remote")

    monkeypatch.setattr(settings_mod.settings, "remote_llm_label", "DeepSeek (Fireworks)")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "DeepSeek (Fireworks)"


@pytest.mark.unit
def test_model_label_remote_routing_falls_back_when_label_unset(client, stores, monkeypatch):
    from config import settings as settings_mod
    session_store, _ = stores
    session_store.create(task_id="t-remote2", status=STATUS_RUNNING, routing="remote")

    monkeypatch.setattr(settings_mod.settings, "remote_llm_label", "")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Remote"


@pytest.mark.unit
def test_model_label_hermes_routing_says_plain_hermes_without_an_observed_turn(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t-herm-bare", status=STATUS_RUNNING, routing="hermes")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Hermes"


@pytest.mark.unit
def test_model_label_hermes_routing_ignores_the_board_model_picker(client, stores):
    """`Session.model` is the board's operator-chosen model *picker* value
    (`SessionStore.set_assignment`) — `HermesExecutor` never reads or writes
    it, so it must not affect the Hermes badge. A session with a picker
    `model` set still reads plain "Hermes": the badge is plain "Hermes"
    unconditionally, never a `Hermes · <model>` suffix sourced from a
    process-wide "last observed" reading, since that reading is a
    cross-session misattribution and not merely decoupled from the
    picker."""
    session_store, _ = stores
    session_store.create(
        task_id="t-herm-picker", status=STATUS_RUNNING, routing="hermes",
        model="deepseek-v4-flash",
    )
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model_label"] == "Hermes"


@pytest.mark.unit
def test_model_label_claude_code_and_codex_routing(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t-cc", status=STATUS_RUNNING, routing="claude_code")
    session_store.create(task_id="t-code", status=STATUS_RUNNING, routing="code")
    session_store.create(task_id="t-codex", status=STATUS_RUNNING, routing="codex")
    sessions = {s["task_id"]: s for s in client.get("/api/agents/snapshot").json()["sessions"]}
    assert sessions["t-cc"]["model_label"] == "Claude Code"
    assert sessions["t-code"]["model_label"] == "Claude Code"
    assert sessions["t-codex"]["model_label"] == "Codex"


@pytest.mark.unit
def test_snapshot_host_defaults_to_api_host_when_unset(client, stores):
    from api.routes import agents as agents_route
    session_store, _ = stores
    session_store.create(task_id="t-nohost", status=STATUS_RUNNING, routing="local")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["host"] == agents_route.api_host_name()


@pytest.mark.unit
def test_snapshot_host_uses_session_host_when_set(client, stores):
    session_store, _ = stores
    session_store.create(task_id="t-host", status=STATUS_RUNNING, routing="local", host="build-box")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["host"] == "build-box"


@pytest.mark.unit
def test_snapshot_passes_through_board_assignment_fields(client, stores):
    session_store, _ = stores
    session_store.create(
        task_id="t-fields", status=STATUS_RUNNING, routing="claude",
        model="claude-sonnet-5", effort="high", bot="doctor", origin="operator",
    )
    session_store.set_conversation_id("t-fields", "conv-123")
    sess = client.get("/api/agents/snapshot").json()["sessions"][0]
    assert sess["model"] == "claude-sonnet-5"
    assert sess["effort"] == "high"
    assert sess["conversation_id"] == "conv-123"
    assert sess["bot"] == "doctor"
    assert sess["origin"] == "operator"


# ---------------------------------------------------------------------------
# Summary search (issue #252) — GET /api/agents/search + the cache-only
# search_cached_summaries helper. Seeds the disk cache directly so no LLM runs.
# ---------------------------------------------------------------------------


@pytest.fixture
def summary_db(tmp_path: Path, monkeypatch):
    """Point the summary cache at a temp DB and return a seeding helper."""
    from api.services import agent_viz_summary as avs

    monkeypatch.setattr(avs, "_DB_PATH", str(tmp_path / "summaries.db"))
    avs._init_db()
    avs.reset_cache()

    def seed(session_id: str, short_label: str, summary: str,
             last_activity_at: float = 1_000_000.0):
        avs._disk_put(
            session_id,
            last_activity_at,
            avs.SummaryResult(short_label=short_label, summary=summary),
        )

    return seed


@pytest.mark.unit
def test_search_matches_short_label(summary_db):
    from api.services import agent_viz_summary as avs

    summary_db("s1", "Fix Login Bug", "Resolved an auth regression.")
    summary_db("s2", "Refactor Search", "Cleaned up the indexer.")

    matches = avs.search_cached_summaries("login")
    assert len(matches) == 1
    assert matches[0]["session_id"] == "s1"
    assert matches[0]["field"] == "short_label"
    assert "Login" in matches[0]["snippet"]


@pytest.mark.unit
def test_search_matches_summary_when_short_label_misses(summary_db):
    from api.services import agent_viz_summary as avs

    summary_db("s1", "Fix Login Bug", "Resolved an auth regression in the indexer.")

    matches = avs.search_cached_summaries("indexer")
    assert len(matches) == 1
    assert matches[0]["field"] == "summary"
    assert "indexer" in matches[0]["snippet"]


@pytest.mark.unit
def test_search_is_case_insensitive(summary_db):
    from api.services import agent_viz_summary as avs

    summary_db("s1", "Fix Login Bug", "Auth work.")
    assert [m["session_id"] for m in avs.search_cached_summaries("LOGIN")] == ["s1"]


@pytest.mark.unit
def test_search_no_match_returns_empty(summary_db):
    from api.services import agent_viz_summary as avs

    summary_db("s1", "Fix Login Bug", "Auth work.")
    assert avs.search_cached_summaries("nonexistent") == []
    assert avs.search_cached_summaries("   ") == []


@pytest.mark.unit
def test_search_treats_like_wildcards_literally(summary_db):
    from api.services import agent_viz_summary as avs

    summary_db("s1", "100% Coverage", "Hit the goal.")
    summary_db("s2", "1000 Lines", "Big refactor.")

    # "00%" must match only the literal "100%" — not behave as a SQL wildcard
    # that would also sweep in "1000".
    matches = avs.search_cached_summaries("00%")
    assert [m["session_id"] for m in matches] == ["s1"]


@pytest.mark.unit
def test_search_endpoint_returns_matches(client, summary_db):
    summary_db("s1", "Fix Login Bug", "Resolved an auth regression.")

    r = client.get("/api/agents/search", params={"q": "login"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "login"
    assert len(body["matches"]) == 1
    assert body["matches"][0]["session_id"] == "s1"
    assert body["matches"][0]["field"] == "short_label"


@pytest.mark.unit
def test_search_endpoint_requires_query(client, summary_db):
    # The app maps RequestValidationError to 400 (see api/main.py).
    assert client.get("/api/agents/search").status_code == 400
    assert client.get("/api/agents/search", params={"q": ""}).status_code == 400


# ---------------------------------------------------------------------------
# `_fallback_label` must not hand back the row's own raw identifier — a
# uuid, a `t-...` task id, or a "cc:"/"cx:"-prefixed session id — even
# re-spaced: turning "cx:remote-cx-1" into "cx remote-cx-1" would not be
# caught by `web/agents/graph.js`'s equality guard, since a re-spaced value
# does not equal the session id, the bare session id, or the task id
# character-for-character.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("identifier", [
    "0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42",  # bare uuid
    "t-orphan-deleted",  # bare task id
    "cx:remote-cx-1",  # cli-prefixed session id (would re-space to "cx remote-cx-1")
    "cc:0e6b2c14-9f77-4a1e-8b55-3c2f9d10aa42",  # cli-prefixed uuid
])
def test_fallback_label_refuses_to_echo_a_raw_identifier(identifier):
    from api.services.agent_viz_summary import _fallback_label

    assert _fallback_label(identifier) == ""


@pytest.mark.unit
def test_fallback_label_still_renders_a_real_multi_word_title():
    from api.services.agent_viz_summary import _fallback_label

    assert _fallback_label("Clean Up The Indexer") == "Clean Up The Indexer"


# ---------------------------------------------------------------------------
# A genuinely non-empty, non-Latin title tokenizes to zero ASCII words — the
# same shape `_fallback_label` sees for a truly empty input — but it is NOT
# the same case: there is a real title in `label` for the caller to fall
# through to, so this must return "" (like the raw-identifier guard above),
# never "Untitled". Returning "Untitled" here would clobber the real title,
# since "Untitled" is truthy and outranks `label` in
# `web/agents/graph.js`'s `nodeLabel` precedence.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("title", [
    "検索インデックスをリファクタリングする",  # Japanese (CJK)
    "Исправить парсер виджетов",  # Russian (Cyrillic)
    "重构搜索索引",  # Chinese (CJK)
    "🚀 🐛",  # emoji-only
])
def test_fallback_label_non_latin_title_falls_through_not_untitled(title):
    from api.services.agent_viz_summary import _fallback_label

    assert _fallback_label(title) == ""


@pytest.mark.unit
def test_fallback_label_empty_input_is_untitled():
    from api.services.agent_viz_summary import _fallback_label

    assert _fallback_label("") == "Untitled"
    assert _fallback_label("   ") == "Untitled"


# ---------------------------------------------------------------------------
# No-content fallback caching. A session whose transcript carries no
# user/assistant text (only a seed + tool calls — the real agent-worker case)
# hits the deterministic fallback in summarize_session. A *terminal* such
# session must cache the fallback so it drops out of the prefetch candidate
# list; otherwise the prefetch loop re-picks it every tick in ~0s and starves
# every other candidate behind it (head-of-line starvation).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_content_terminal_session_caches_fallback(summary_db):
    import asyncio
    from api.services import agent_viz_summary as avs

    sid = "sess_nocontent_terminal"
    # Only a seed (task_id) + tool calls — no user/assistant text. This is the
    # shape that produced the production starvation bug.
    events = [
        {"kind": "seed", "payload": {"task_id": "t1"}},
        {"kind": "tool_call", "payload": {"tool": "Bash"}},
    ]
    result = asyncio.run(avs.summarize_session(
        sid, label="Clean Up The Indexer", last_activity_at=1000.0,
        events=events, status=STATUS_COMPLETED,
    ))
    # Fallback derived from the label — no LLM call.
    assert result.short_label == "Clean Up The Indexer"
    # Cached, so the snapshot peek returns it and the session drops out of the
    # prefetch candidate list instead of being re-picked forever.
    cached = avs.get_cached_summary(sid, 1000.0, status=STATUS_COMPLETED)
    assert cached is not None
    assert cached.short_label == "Clean Up The Indexer"


@pytest.mark.unit
def test_no_content_live_session_not_cached(summary_db):
    import asyncio
    from api.services import agent_viz_summary as avs

    sid = "sess_nocontent_live"
    events = [{"kind": "seed", "payload": {"task_id": "t1"}}]
    result = asyncio.run(avs.summarize_session(
        sid, label="Running Task", last_activity_at=1000.0,
        events=events, status=STATUS_RUNNING,
    ))
    assert result.short_label == "Running Task"
    # A live session may gain content later, so the fallback must NOT be
    # cached — the next access should retry.
    assert avs.get_cached_summary(sid, 1000.0, status=STATUS_RUNNING) is None


# ---------------------------------------------------------------------------
# CLI-only terminal statuses ("ended"/"inactive") are unioned into the
# terminal set, and a summary call that keeps *raising* gets the same
# cache-the-fallback treatment as the no-content case above. Without this,
# a CLI session whose summarizer errors is retried by the prefetcher on
# every tick forever.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("status", ["completed", "failed", "budget_exceeded", "ended", "inactive"])
def test_raising_summarizer_caches_fallback_for_terminal_statuses(summary_db, monkeypatch, status):
    import asyncio
    from api.services import agent_viz_summary as avs

    calls = {"n": 0}

    async def _raise(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("synthetic LLM failure")

    monkeypatch.setattr(avs, "generate_text", _raise)

    sid = f"sess_raise_{status}"
    events = [
        {"kind": "user_message", "payload": {"text": "do the thing"}},
        {"kind": "assistant_message", "payload": {"text": "done"}},
    ]
    for _ in range(2):
        result = asyncio.run(avs.summarize_session(
            sid, label="Some Task", last_activity_at=1000.0, events=events, status=status,
        ))
        assert "Summary unavailable" in result.summary

    # Second call was a cache hit — the raising summarizer ran exactly once.
    assert calls["n"] == 1
    cached = avs.get_cached_summary(sid, 1000.0, status=status)
    assert cached is not None


# ---------------------------------------------------------------------------
# An error fallback (the summarizer *raised*) must never be persisted to
# disk, unlike the deterministic no-content fallback above. `prune_disk_cache`
# has no scheduled caller, so anything written to disk there effectively
# never expires — a transient Gemma timeout during one summarize attempt
# must not permanently pin "(Summary unavailable: ...)" as a terminal
# session's label/summary.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_raising_summarizer_error_fallback_is_not_persisted_to_disk(summary_db, monkeypatch):
    import asyncio
    import sqlite3
    from api.services import agent_viz_summary as avs

    async def _raise(*args, **kwargs):
        raise RuntimeError("synthetic LLM failure")

    monkeypatch.setattr(avs, "generate_text", _raise)

    sid = "sess_error_fallback_disk_check"
    events = [
        {"kind": "user_message", "payload": {"text": "do the thing"}},
        {"kind": "assistant_message", "payload": {"text": "done"}},
    ]
    result = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert "Summary unavailable" in result.summary
    # In-process cache does hold it (AC 7 — no immediate re-call)...
    assert avs.get_cached_summary(sid, 1000.0, status=STATUS_COMPLETED) is not None
    # ...but the disk row must not exist at all. Read straight from the
    # sqlite table so this can't be fooled by `_disk_get`'s own freshness
    # filtering.
    with sqlite3.connect(avs._resolve_db_path()) as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_viz_summary WHERE session_id = ?", (sid,)
        ).fetchone()
    assert row is None, "error fallback must never reach the disk cache (#863 finding N)"


@pytest.mark.unit
def test_raising_summarizer_recovers_after_ttl_expires_not_pinned_by_cached_error(
    summary_db, monkeypatch
):
    """A terminal session whose summarizer raised, then recovered, gets the
    real summary once the bounded error-fallback TTL elapses — not the
    cached error forever. Also proves AC 7 still holds up to that point:
    an immediate re-query at the same activity is still a cache hit."""
    import asyncio
    import sqlite3
    import time as time_mod
    from api.services import agent_viz_summary as avs

    calls = {"n": 0}
    should_raise = {"value": True}

    async def _flaky(*args, **kwargs):
        calls["n"] += 1
        if should_raise["value"]:
            raise RuntimeError("synthetic LLM failure")
        return json.dumps({"short_label": "Fixed The Bug", "summary": "Fixed it."})

    monkeypatch.setattr(avs, "generate_text", _flaky)

    sid = "sess_raise_then_recover"
    events = [
        {"kind": "user_message", "payload": {"text": "do the thing"}},
        {"kind": "assistant_message", "payload": {"text": "done"}},
    ]
    first = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert "Summary unavailable" in first.summary
    assert calls["n"] == 1

    # Immediately re-querying at the same activity is still within the TTL
    # — AC 7 holds, no re-call yet.
    still_cached = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert "Summary unavailable" in still_cached.summary
    assert calls["n"] == 1

    # The recovery: the summarizer would now succeed, and wall-clock time
    # has advanced past the error-fallback TTL.
    should_raise["value"] = False
    real_time = time_mod.time
    monkeypatch.setattr(
        time_mod, "time", lambda: real_time() + avs._FAILURE_FALLBACK_TTL_SECONDS + 1
    )
    recovered = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert calls["n"] == 2
    assert recovered.short_label == "Fixed The Bug"
    assert recovered.summary == "Fixed it."
    # Real summaries do reach disk, unlike the error fallback.
    with sqlite3.connect(avs._resolve_db_path()) as conn:
        row = conn.execute(
            "SELECT summary FROM agent_viz_summary WHERE session_id = ?", (sid,)
        ).fetchone()
    assert row is not None
    assert row[0] == "Fixed it."


# ---------------------------------------------------------------------------
# "Frozen ⇒ trust the cache regardless of new activity" must be restricted
# to a REAL cached summary. A no-content
# fallback costs nothing to re-derive (no LLM call on that path), so it
# gets no such leniency — re-summarizing when activity advances is free and
# strictly more correct.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_real_summary_still_served_from_cache_when_activity_advances_for_terminal_session(
    summary_db, monkeypatch
):
    import asyncio
    from api.services import agent_viz_summary as avs

    calls = {"n": 0}

    async def _succeed(*args, **kwargs):
        calls["n"] += 1
        return json.dumps({"short_label": "Ship The Feature", "summary": "Shipped it."})

    monkeypatch.setattr(avs, "generate_text", _succeed)

    sid = "sess_real_summary_frozen"
    events = [
        {"kind": "user_message", "payload": {"text": "ship the feature"}},
        {"kind": "assistant_message", "payload": {"text": "shipped"}},
    ]
    first = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert first.summary == "Shipped it."
    assert calls["n"] == 1

    # `completed` is frozen — a real cached summary is trusted even though
    # this query reports a later activity than what was cached (e.g. an
    # operator re-picked a model on the completed card, which bumps
    # `last_activity_at` without changing `status`).
    cached = avs.get_cached_summary(sid, 2000.0, status=STATUS_COMPLETED)
    assert cached is not None
    assert cached.summary == "Shipped it."
    assert calls["n"] == 1


@pytest.mark.unit
def test_no_content_fallback_not_trusted_forever_when_activity_advances_for_terminal_session(
    summary_db,
):
    """Unlike a real summary, the no-content fallback does NOT get the
    "frozen ⇒ trust regardless of activity" leniency (REC-1) — re-deriving
    it costs no LLM call, so there's no reason to risk serving a stale
    fallback once a completed session's transcript could plausibly differ."""
    import asyncio
    from api.services import agent_viz_summary as avs

    sid = "sess_no_content_frozen_resummarize"
    events = [{"kind": "seed", "payload": {"task_id": "t1"}}]
    first = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_COMPLETED,
    ))
    assert first.summary == avs._NO_CONTENT_SUMMARY
    # Cached at the original activity...
    assert avs.get_cached_summary(sid, 1000.0, status=STATUS_COMPLETED) is not None
    # ...but NOT trusted once activity has moved on, even though `completed`
    # is frozen — a real summary would be trusted here (see the sibling test
    # above); a no-content fallback is not.
    assert avs.get_cached_summary(sid, 2000.0, status=STATUS_COMPLETED) is None


@pytest.mark.unit
def test_raising_summarizer_not_cached_for_live_session(summary_db, monkeypatch):
    import asyncio
    from api.services import agent_viz_summary as avs

    calls = {"n": 0}

    async def _raise(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("synthetic LLM failure")

    monkeypatch.setattr(avs, "generate_text", _raise)

    sid = "sess_raise_live"
    events = [
        {"kind": "user_message", "payload": {"text": "do the thing"}},
        {"kind": "assistant_message", "payload": {"text": "done"}},
    ]
    for _ in range(2):
        asyncio.run(avs.summarize_session(
            sid, label="Some Task", last_activity_at=1000.0, events=events, status=STATUS_RUNNING,
        ))
    # A live session isn't cached, so both calls hit the (raising) summarizer.
    assert calls["n"] == 2
    assert avs.get_cached_summary(sid, 1000.0, status=STATUS_RUNNING) is None


@pytest.mark.unit
def test_inactive_session_fallback_caches_but_is_not_served_stale_after_resuming(
    summary_db, monkeypatch,
):
    """`inactive` is in the extended terminal set so a
    session's fallback gets cached — otherwise the prefetcher would retry
    it every tick forever (AC 7). But `inactive` is NOT truly frozen
    (`web/agents/panel.js`'s `TERMINAL` set explicitly excludes it — a
    Claude Code session idle >30 min can resume), so once its
    `last_activity_at` actually advances, the cached fallback must not be
    served as if the session were still exactly where it was."""
    import asyncio
    from api.services import agent_viz_summary as avs

    calls = {"n": 0}

    async def _raise(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("synthetic LLM failure")

    monkeypatch.setattr(avs, "generate_text", _raise)

    sid = "cc:resumes-after-inactive"
    events = [
        {"kind": "user_message", "payload": {"text": "do the thing"}},
        {"kind": "assistant_message", "payload": {"text": "done"}},
    ]
    first = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=1000.0, events=events, status="inactive",
    ))
    assert "Summary unavailable" in first.summary
    assert calls["n"] == 1
    # AC 7: the fallback is cached so a repeat query at the same activity
    # doesn't re-invoke the summarizer.
    assert avs.get_cached_summary(sid, 1000.0, status="inactive") is not None
    assert calls["n"] == 1

    # The session resumes: last_activity_at advances. Even a query that
    # still reports "inactive" (e.g. a stale status label racing the
    # transcript rescan) must not serve the old cache as fresh.
    assert avs.get_cached_summary(sid, 2000.0, status="inactive") is None

    # And an explicit re-summarize call actually re-invokes the summarizer
    # rather than trusting the stale entry.
    second = asyncio.run(avs.summarize_session(
        sid, label="Some Task", last_activity_at=2000.0, events=events, status="inactive",
    ))
    assert calls["n"] == 2
    assert "Summary unavailable" in second.summary


@pytest.mark.unit
def test_frozen_status_cache_valid_regardless_of_new_activity(summary_db):
    """`_is_frozen` (worker-terminal statuses + `ended`,
    deliberately excluding `inactive`) is the strict set consulted by
    `_is_fresh_enough` for "is this cache valid no matter what a later read
    reports" — as opposed to `_is_terminal`'s broader "should a fallback be
    cached at all" question used by `_cache_if_terminal`. A frozen status's
    activity genuinely can't advance again, so even a stray/misreported
    higher `last_activity_at` must not force a pointless re-summarize."""
    import time as time_mod
    from api.services import agent_viz_summary as avs

    assert avs._is_frozen("completed") is True
    assert avs._is_frozen("ended") is True
    assert avs._is_frozen("inactive") is False
    assert avs._is_frozen("running") is False

    assert avs._is_fresh_enough(1000.0, time_mod.time(), 2000.0, "completed") is True
    assert avs._is_fresh_enough(1000.0, time_mod.time(), 2000.0, "inactive") is False


# ---------------------------------------------------------------------------
# The prefetcher must dispatch a `cx:` id to the Codex path rather than
# falling through to the LifeOS TranscriptStore (which returns [] for a
# Codex id) — the `cc:`/`cx:`/other three-way split covers Codex sessions,
# not just Claude Code and LifeOS.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prefetch_summarizes_codex_session_and_snapshot_carries_short_label(
    client, tmp_path: Path, monkeypatch
):
    import asyncio
    from api.services import agent_viz_summary as avs
    from api.services import agent_viz_summary_prefetch as prefetch
    from api.services.codex import session_ingest as cx
    from api.routes import agents as agents_route
    from api.services.agent_worker.session_store import SessionStore
    from api.services.agent_worker.transcript_store import TranscriptStore
    from config.settings import settings as settings_obj

    monkeypatch.setattr(avs, "_DB_PATH", str(tmp_path / "summaries.db"))
    avs._init_db()
    avs.reset_cache()

    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    agents_route._label_cache.clear()

    # A bare `session_meta`-only rollout produces an empty
    # `_extract_context` result regardless of whether events came from the
    # `cx:` dispatch branch or fell through to the LifeOS TranscriptStore
    # (which returns `[]` for a Codex id) — so every assertion below held
    # identically with the `cx:` branch removed, and the test proved
    # nothing about acceptance criterion 6. Give the rollout a real
    # user/assistant turn and assert the transcript text actually reaches
    # the summarizer's prompt, which only happens if `_summarize_one`
    # genuinely dispatched via `cx.read_normalized_events`.
    SYNTHETIC_MARKER = "synthetic-widget-parser-xyzzy"
    codex_dir = tmp_path / "codex_sessions"
    sub = codex_dir / "2026" / "05" / "30"
    sub.mkdir(parents=True)
    raw_id = "prefetch-target"
    rollout = sub / f"rollout-2026-05-30T10-41-38-{raw_id}.jsonl"
    with rollout.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-05-30T10:41:38Z",
            "type": "session_meta",
            "payload": {
                "id": raw_id, "cwd": "/home/synthetic/proj", "originator": "codex_exec",
                "cli_version": "0.135.0", "source": "exec", "model_provider": "openai",
            },
        }) + "\n")
        f.write(json.dumps({
            "timestamp": "2026-05-30T10:41:39Z",
            "type": "event_msg",
            "payload": {"type": "user_message",
                        "message": f"please {SYNTHETIC_MARKER} in the parser"},
        }) + "\n")
        f.write(json.dumps({
            "timestamp": "2026-05-30T10:41:40Z",
            "type": "event_msg",
            "payload": {"type": "agent_message",
                        "message": f"done, the {SYNTHETIC_MARKER} is fixed",
                        "phase": "final_answer"},
        }) + "\n")

    monkeypatch.setattr(settings_obj, "codex_sessions_dir", str(codex_dir))
    cx.invalidate_cache()

    cx_sessions, _ = cx.build_snapshot(sessions_dir=codex_dir, lookback_days=7)
    target = next(s for s in cx_sessions if s["session_id"] == "cx:prefetch-target")

    captured_prompts: list[str] = []

    async def _fake_generate_text(prompt, **kwargs):
        captured_prompts.append(prompt)
        return json.dumps({
            "short_label": "Synthetic Widget Fix",
            "summary": "Fixed the synthetic widget parser.",
        })

    monkeypatch.setattr(avs, "generate_text", _fake_generate_text)

    ok = asyncio.run(prefetch._summarize_one(dict(target, status="completed")))
    assert ok is True
    # Proves events reached `_extract_context` via the `cx:` branch rather
    # than the deterministic no-content fallback, which never calls
    # `generate_text` at all.
    assert captured_prompts, (
        "generate_text was never called — no transcript content reached "
        "the summarizer, so the cx: dispatch branch isn't being exercised"
    )
    assert SYNTHETIC_MARKER in captured_prompts[0]

    cached = avs.get_cached_summary(
        target["session_id"], target["last_activity_at"], status="completed",
    )
    assert cached is not None
    assert cached.short_label == "Synthetic Widget Fix"

    r = client.get("/api/agents/snapshot")
    assert r.status_code == 200
    sessions = {s["session_id"]: s for s in r.json()["sessions"]}
    assert sessions["cx:prefetch-target"]["short_label"] == cached.short_label


@pytest.mark.unit
def test_prefetch_with_engine_disabled_still_caches_fallback_and_drops_out(
    tmp_path: Path, monkeypatch
):
    """`_build_snapshot` emits a synthetic `cli_sessions` row for a
    hook-registered session on another host *unconditionally* — it's never
    gated by `codex_viz_enabled` / `claude_code_viz_enabled`. With the
    engine disabled, `_summarize_one` can't read the transcript. Returning
    False before `summarize_session` ever runs would leave the fallback
    uncached and the row re-picked as a prefetch candidate on every tick
    forever. It must instead fall through to an empty event list so the
    no-content path caches a fallback and the row drops out of the
    candidate list, same as the base-branch behavior for an
    unreachable/malformed session."""
    import asyncio
    from api.services import agent_viz_summary as avs
    from api.services import agent_viz_summary_prefetch as prefetch
    from api.routes import agents as agents_route
    from api.services.agent_worker.session_store import SessionStore
    from api.services.agent_worker.transcript_store import TranscriptStore
    from config.settings import settings as settings_obj

    monkeypatch.setattr(avs, "_DB_PATH", str(tmp_path / "summaries.db"))
    avs._init_db()
    avs.reset_cache()

    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    transcript_store = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    monkeypatch.setattr(agents_route, "_session_store", session_store)
    monkeypatch.setattr(agents_route, "_transcript_store", transcript_store)
    agents_route._label_cache.clear()

    # A hook-registered Codex session on a different host — no local rollout
    # to read, which is exactly the row shape that leaked through the flag
    # guard unconditionally.
    session_store.record_cli_session_event(
        engine="codex", event="session_end", session_id="remote-cx-1",
        host="a-different-synthetic-host", cwd="/home/synthetic/proj",
        prompt="refactor the synthetic widget",
    )

    monkeypatch.setattr(settings_obj, "codex_viz_enabled", False)
    monkeypatch.setattr(settings_obj, "codex_sessions_dir", str(tmp_path / "no-such-codex-dir"))
    # Also isolate the Claude Code side, or
    # `_build_snapshot()` scans the operator's real `~/.claude/projects` —
    # not hermetic, and its runtime scales with the operator's own history.
    monkeypatch.setattr(settings_obj, "claude_code_projects_dir", str(tmp_path / "no-such-cc-dir"))

    snap = agents_route._build_snapshot()
    rows = [s for s in snap["sessions"] if s["session_id"].startswith("cx:")]
    assert len(rows) == 1
    row = rows[0]

    candidates_before = [s["session_id"] for s in prefetch._candidate_sessions()]
    assert row["session_id"] in candidates_before

    for _ in range(3):
        ok = asyncio.run(prefetch._summarize_one(row))
        assert ok is True

    cached = avs.get_cached_summary(
        row["session_id"], row.get("last_activity_at") or 0.0,
        status=row.get("status") or "",
    )
    assert cached is not None, "fallback was never cached — row will retry forever"

    candidates_after = [s["session_id"] for s in prefetch._candidate_sessions()]
    assert row["session_id"] not in candidates_after


@pytest.mark.unit
def test_candidate_sessions_sort_agrees_with_is_terminal(monkeypatch):
    """`_candidate_sessions` must sort using the same terminal definition as
    `agent_viz_summary._is_terminal`, not `TERMINAL_STATUSES` imported
    straight from `session_store`, which knows nothing about the CLI
    statuses ("ended"/"inactive") that `_is_terminal` also treats as
    terminal — two definitions of terminal disagreeing inside one feature
    would sort a CLI row with status "ended" as if it were still live. A
    CLI row with status "ended" must sort ahead of a "running" one
    (terminal summaries are cached forever, so they're the more valuable
    prefetch target)."""
    from api.services import agent_viz_summary_prefetch as prefetch
    from api.routes import agents as agents_route

    sessions = [
        {"session_id": "sess_running", "status": "running", "last_activity_at": 2000},
        {"session_id": "cc:ended-one", "status": "ended", "last_activity_at": 1000},
    ]
    monkeypatch.setattr(
        agents_route, "_build_snapshot",
        lambda: {"sessions": sessions, "edges": [], "generated_at": 0},
    )
    ordered = [s["session_id"] for s in prefetch._candidate_sessions()]
    assert ordered == ["cc:ended-one", "sess_running"]


# ---------------------------------------------------------------------------
# Manual label overrides — a node name the operator pins by hand, overriding
# the auto-derived label everywhere. Store + PUT endpoint + snapshot wiring.
# ---------------------------------------------------------------------------


@pytest.fixture
def label_override_db(tmp_path: Path, monkeypatch):
    """Point the label-override store at a temp DB."""
    from api.services import agent_viz_label_override as alo

    monkeypatch.setattr(alo, "_DB_PATH", str(tmp_path / "label_overrides.db"))
    alo._init_db()
    alo.reset_cache()
    return alo


@pytest.mark.unit
def test_label_override_set_get_clear(label_override_db):
    alo = label_override_db
    assert alo.get_override("s1") is None
    assert alo.set_override("s1", "  My Custom Name  ") == "My Custom Name"
    assert alo.get_override("s1") == "My Custom Name"
    # Empty / blank clears the override.
    assert alo.set_override("s1", "   ") == ""
    assert alo.get_override("s1") is None


@pytest.mark.unit
def test_label_override_persists_across_cache_reset(label_override_db):
    alo = label_override_db
    alo.set_override("s1", "Durable Label")
    alo.reset_cache()  # forces a reload from disk
    assert alo.get_override("s1") == "Durable Label"


@pytest.mark.unit
def test_label_override_clamps_length(label_override_db):
    alo = label_override_db
    stored = alo.set_override("s1", "x" * 500)
    assert len(stored) == 120
    assert stored.endswith("…")


@pytest.mark.unit
def test_put_label_sets_and_clears(client, stores, label_override_db):
    session_store, _ = stores
    s = session_store.create(task_id="t-rename", status=STATUS_RUNNING)

    r = client.put(
        f"/api/agents/sessions/{s.session_id}/label",
        json={"label": "Renamed Session"},
    )
    assert r.status_code == 200
    assert r.json() == {"session_id": s.session_id, "custom_label": "Renamed Session"}

    # Snapshot surfaces the override so the node + panel can use it.
    snap = client.get("/api/agents/snapshot").json()
    sess = next(x for x in snap["sessions"] if x["session_id"] == s.session_id)
    assert sess["custom_label"] == "Renamed Session"

    # Empty label clears it (revert to auto-naming).
    r2 = client.put(
        f"/api/agents/sessions/{s.session_id}/label",
        json={"label": ""},
    )
    assert r2.status_code == 200
    assert r2.json()["custom_label"] is None
    snap2 = client.get("/api/agents/snapshot").json()
    sess2 = next(x for x in snap2["sessions"] if x["session_id"] == s.session_id)
    assert sess2["custom_label"] is None
