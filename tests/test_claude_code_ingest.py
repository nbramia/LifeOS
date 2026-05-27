"""Tests for the Claude Code CLI session ingest adapter (issue #144).

All fixtures are synthetic — real Claude Code transcripts can contain
secrets, code, and PII, and are explicitly out of scope for the test
suite per the privacy section of #144.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from api.services.claude_code import session_ingest as cc


# ---------------------------------------------------------------------------
# Helpers — build a synthetic Claude Code projects dir on disk
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def _now_iso(offset: float = 0.0) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _assistant_event(text: str = "ok", tool_uses: list[dict] | None = None,
                     usage: dict | None = None, ts_offset: float = 0.0,
                     model: str = "claude-sonnet-4-6") -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    return {
        "type": "assistant",
        "timestamp": _now_iso(ts_offset),
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": usage or {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def _user_event(text: str = "hi", tool_results: list[dict] | None = None, ts_offset: float = 0.0) -> dict:
    content: list[dict]
    if tool_results is not None:
        content = []
        for tr in tool_results:
            content.append({"type": "tool_result", **tr})
        msg_content: object = content
    else:
        msg_content = text
    return {
        "type": "user",
        "timestamp": _now_iso(ts_offset),
        "message": {"role": "user", "content": msg_content},
    }


def _noise_events() -> list[dict]:
    return [
        {"type": "mode", "mode": "normal"},
        {"type": "permission-mode", "permissionMode": "default"},
        {"type": "ai-title", "aiTitle": "test session"},
        {"type": "last-prompt", "lastPrompt": "x"},
        {"type": "worktree-state"},
        {"type": "file-history-snapshot", "snapshot": {}},
        {"type": "queue-operation", "operation": "x"},
        {"type": "attachment", "attachment": {}},
        {"type": "pr-link"},
    ]


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_session_id_rejects_traversal():
    for bad in ["", "..", "../etc", "a/b", "a\\b", "a..b", "../../etc/passwd"]:
        with pytest.raises(ValueError):
            cc.validate_session_id(bad)


@pytest.mark.unit
def test_validate_session_id_accepts_cc_prefix():
    raw = cc.validate_session_id("cc:abc-123-def")
    assert raw == "abc-123-def"


@pytest.mark.unit
def test_validate_session_id_accepts_subagent_synthetic():
    # Subagent ids look like cc:<parent>:agent:<tool_use_id>
    raw = cc.validate_session_id("cc:abc-123:agent:tu_456")
    assert raw == "abc-123:agent:tu_456"


# ---------------------------------------------------------------------------
# Project key decoding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decode_project_key():
    assert cc.decode_project_key("-home-nathan-Code-LifeOS") == "/home/nathan/Code/LifeOS"
    assert cc.decode_project_key("") == ""
    assert cc.basename_for("/home/nathan/Code/LifeOS") == "LifeOS"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_sessions_walks_projects(tmp_path: Path):
    proj_a = tmp_path / "-home-syn-Code-A"
    proj_b = tmp_path / "-home-syn-Code-B"
    _write_jsonl(proj_a / "session-a.jsonl", [_user_event("hello")])
    _write_jsonl(proj_b / "session-b.jsonl", [_user_event("hi")])

    metas = cc.discover_sessions(projects_dir=tmp_path)
    ids = sorted(m.raw_session_id for m in metas)
    assert ids == ["session-a", "session-b"]
    assert all(m.session_id.startswith("cc:") for m in metas)
    decoded = {m.raw_session_id: m.decoded_cwd for m in metas}
    assert decoded["session-a"] == "/home/syn/Code/A"


@pytest.mark.unit
def test_discover_respects_lookback_days(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    fresh = proj / "fresh.jsonl"
    stale = proj / "stale.jsonl"
    _write_jsonl(fresh, [_user_event("recent")])
    _write_jsonl(stale, [_user_event("old")])
    # Backdate the stale file's mtime by 30 days.
    old_ts = time.time() - 30 * 86_400
    os.utime(stale, (old_ts, old_ts))

    metas = cc.discover_sessions(projects_dir=tmp_path, lookback_days=7)
    raw_ids = [m.raw_session_id for m in metas]
    assert raw_ids == ["fresh"]


@pytest.mark.unit
def test_discover_skips_non_jsonl_and_state_subdirs(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "s1.jsonl", [_user_event("x")])
    # Same-named subdir holds working state, not transcript data.
    (proj / "s1").mkdir()
    (proj / "s1" / "task.json").write_text("{}")
    metas = cc.discover_sessions(projects_dir=tmp_path)
    assert [m.raw_session_id for m in metas] == ["s1"]


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_filters_noise_event_types():
    for raw in _noise_events():
        assert cc.normalize_event(raw) is None


@pytest.mark.unit
def test_normalize_assistant_event_extracts_text_thinking_tool_uses():
    raw = {
        "type": "assistant",
        "timestamp": _now_iso(),
        "message": {
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}},
            ],
            "usage": {"input_tokens": 200, "output_tokens": 80,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    }
    ev = cc.normalize_event(raw)
    assert ev is not None
    assert ev["kind"] == "assistant_message"
    payload = ev["payload"]
    assert payload["text"] == "hello"
    assert payload["thinking_chars"] == len("let me think")
    assert payload["tool_uses"] == [{"id": "tu_1", "name": "Bash", "input_keys": ["command"]}]
    assert payload["usage"]["input_tokens"] == 200


@pytest.mark.unit
def test_normalize_user_event_with_tool_results_becomes_tool_result_kind():
    raw = _user_event(tool_results=[
        {"tool_use_id": "tu_1", "is_error": False,
         "content": [{"type": "text", "text": "ok"}]},
    ])
    ev = cc.normalize_event(raw)
    assert ev is not None
    assert ev["kind"] == "tool_result"
    assert ev["payload"]["tool_results"][0]["tool_use_id"] == "tu_1"
    assert ev["payload"]["tool_results"][0]["is_error"] is False


@pytest.mark.unit
def test_normalize_user_plain_text_becomes_user_message():
    ev = cc.normalize_event(_user_event(text="hello"))
    assert ev is not None
    assert ev["kind"] == "user_message"
    assert ev["payload"]["text"] == "hello"


@pytest.mark.unit
def test_payload_truncation_at_240_chars():
    big = "x" * 1000
    ev = cc.normalize_event(_user_event(text=big))
    assert ev is not None
    assert len(ev["payload"]["text"]) <= 240
    assert ev["payload"]["text"].endswith("…")


# ---------------------------------------------------------------------------
# Cost rollup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_session_sums_tokens_and_cost(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "s-cost.jsonl"
    _write_jsonl(path, [
        _assistant_event(usage={"input_tokens": 100, "output_tokens": 50,
                                "cache_creation_input_tokens": 200,
                                "cache_read_input_tokens": 1000}),
        _assistant_event(usage={"input_tokens": 50, "output_tokens": 25,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 500}),
    ])
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert meta.total_input_tokens == 150
    assert meta.total_output_tokens == 75
    assert meta.total_cache_creation_tokens == 200
    assert meta.total_cache_read_tokens == 1500
    # Cost matches the agent worker after cache-aware pricing landed
    # (#145 / #157). Sonnet rates: $3/M input, $15/M output. cache_creation
    # is 1.25× input ($3.75/M); cache_read is 0.10× input ($0.30/M).
    # Msg 1: 100*3e-6 + 50*15e-6 + 200*3.75e-6 + 1000*0.30e-6
    #      = 0.0003 + 0.00075 + 0.00075 + 0.0003 = 0.00210
    # Msg 2: 50*3e-6 + 25*15e-6 + 0 + 500*0.30e-6
    #      = 0.00015 + 0.000375 + 0.00015 = 0.000675
    # Total: 0.002775
    assert meta.total_dollars == pytest.approx(0.002775, rel=1e-3)


# ---------------------------------------------------------------------------
# Status inference
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_inference_running_when_mtime_fresh(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "live.jsonl"
    _write_jsonl(path, [_user_event("hi"), _assistant_event("ok")])
    # mtime is fresh (just written).
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert meta.status == "running"


@pytest.mark.unit
def test_status_inference_yielded_when_idle(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "idle.jsonl"
    _write_jsonl(path, [_user_event("hi"), _assistant_event("ok")])
    # Backdate to 2h ago — past 60s threshold but before 24h.
    two_hours = time.time() - 7200
    os.utime(path, (two_hours, two_hours))
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert meta.status == "yielded"


@pytest.mark.unit
def test_status_inference_completed_old_clean_session(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "done.jsonl"
    _write_jsonl(path, [_user_event("hi"), _assistant_event("ok")])
    # 2 days ago, no pending tool, no errors.
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert meta.status == "completed"


@pytest.mark.unit
def test_status_inference_failed_old_with_error_tail(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "bad.jsonl"
    _write_jsonl(path, [
        _user_event("hi"),
        _assistant_event(tool_uses=[{"id": "tu_1", "name": "Bash", "input": {"command": "x"}}]),
        _user_event(tool_results=[{"tool_use_id": "tu_1", "is_error": True,
                                    "content": [{"type": "text", "text": "boom"}]}]),
    ])
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert meta.status == "failed"
    assert meta.error_count == 1


# ---------------------------------------------------------------------------
# Subagent correlation (Agent / Task tool)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_tool_uses_surface_as_nested_sessions(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    path = proj / "sub.jsonl"
    _write_jsonl(path, [
        _assistant_event(tool_uses=[
            {"id": "tu_1", "name": "Agent", "input": {"prompt": "do a thing"}},
        ]),
        _user_event(tool_results=[
            {"tool_use_id": "tu_1", "is_error": False,
             "content": [{"type": "text", "text": "done"}]},
        ]),
    ])
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    assert len(meta.subagents) == 1
    sa = meta.subagents[0]
    assert sa["name"] == "Agent"
    assert sa["status"] == "completed"
    assert sa["tool_use_id"] == "tu_1"


# ---------------------------------------------------------------------------
# Snapshot builder (edges between parent and synthetic subagent nodes)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_snapshot_emits_subagent_edges(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "s1.jsonl", [
        _assistant_event(tool_uses=[
            {"id": "tu_1", "name": "Agent", "input": {"prompt": "child task"}},
        ]),
    ])
    # Force-invalidate the cache so this test doesn't depend on test ordering.
    cc.invalidate_cache()
    sessions, edges = cc.build_snapshot(projects_dir=tmp_path, cache_ttl=0)
    ids = [s["session_id"] for s in sessions]
    assert any(":agent:tu_1" in i for i in ids)
    assert any(e["type"] == "spawn" for e in edges)


@pytest.mark.unit
def test_build_snapshot_returns_empty_when_dir_missing(tmp_path: Path):
    missing = tmp_path / "no-such-dir"
    cc.invalidate_cache()
    sessions, edges = cc.build_snapshot(projects_dir=missing, cache_ttl=0)
    assert sessions == []
    assert edges == []


# ---------------------------------------------------------------------------
# Snapshot dict shape — must match the agent-worker contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_session_dict_includes_required_fields(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "shape.jsonl", [_assistant_event("hi")])
    metas = cc.discover_sessions(projects_dir=tmp_path)
    meta, _ = cc.parse_session(metas[0])
    d = cc.to_session_dict(meta)
    for key in (
        "session_id", "status", "routing", "parent_session_id", "root_session_id",
        "started_at", "last_activity_at", "total_input_tokens", "total_output_tokens",
        "total_dollars", "label", "model_label", "last_event_kind", "tool_call_count",
        "error_count", "source", "status_inferred", "project_key", "decoded_cwd",
    ):
        assert key in d, f"missing field: {key}"
    assert d["source"] == "claude_code"
    assert d["routing"] == "claude_code"


@pytest.mark.unit
def test_model_label_normalizes_known_models():
    assert cc.model_label("claude-haiku-4-5") == "Haiku"
    assert cc.model_label("claude-sonnet-4-6") == "Sonnet"
    assert cc.model_label("claude-opus-4-7") == "Opus"
    assert cc.model_label("") == "Claude Code"


# ---------------------------------------------------------------------------
# Reading events back by session_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_normalized_events_finds_file_by_session_id(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "lookup.jsonl", [_user_event("hi"), _assistant_event("ok")])
    events = cc.read_normalized_events("cc:lookup", projects_dir=tmp_path)
    kinds = [e["kind"] for e in events]
    assert "user_message" in kinds
    assert "assistant_message" in kinds


@pytest.mark.unit
def test_read_normalized_events_for_subagent_falls_back_to_parent(tmp_path: Path):
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "parent.jsonl", [_assistant_event("hi", tool_uses=[
        {"id": "tu_99", "name": "Agent", "input": {"prompt": "sub"}},
    ])])
    events = cc.read_normalized_events("cc:parent:agent:tu_99", projects_dir=tmp_path)
    assert any(e["kind"] == "assistant_message" for e in events)


@pytest.mark.unit
def test_read_normalized_events_rejects_traversal():
    with pytest.raises(ValueError):
        cc.read_normalized_events("cc:../etc/passwd", projects_dir=Path("/"))


# ---------------------------------------------------------------------------
# Subagent transcript window filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_window_returns_spawn_to_result_slice(tmp_path: Path):
    """Synthetic subagent id returns only events between the spawn assistant
    turn and its matching tool_result — not the full parent transcript."""
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "parent.jsonl", [
        _user_event("pre-spawn user turn"),
        _assistant_event("before spawn"),
        # The spawn: assistant turn with an Agent tool_use.
        _assistant_event("spawning", tool_uses=[
            {"id": "tu_42", "name": "Agent", "input": {"prompt": "do sub"}},
        ]),
        # Subagent inline activity.
        _user_event("intermediate"),
        # Closing tool_result.
        _user_event(tool_results=[
            {"tool_use_id": "tu_42", "is_error": False,
             "content": [{"type": "text", "text": "sub done"}]},
        ]),
        # Stuff after — must NOT appear in the slice.
        _user_event("post-spawn"),
        _assistant_event("after"),
    ])
    events = cc.read_normalized_events("cc:parent:agent:tu_42", projects_dir=tmp_path)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "assistant_message"
    assert kinds[-1] == "tool_result"
    # 'post-spawn' user turn should not appear.
    texts = [(e.get("payload") or {}).get("text", "") for e in events]
    assert "post-spawn" not in texts
    # Pre-spawn user turn also excluded.
    assert "pre-spawn user turn" not in texts


@pytest.mark.unit
def test_subagent_window_returns_tail_when_result_pending(tmp_path: Path):
    """A subagent that hasn't returned yet returns spawn → end-of-file."""
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "parent.jsonl", [
        _user_event("user"),
        _assistant_event("spawn", tool_uses=[
            {"id": "tu_inflight", "name": "Agent", "input": {"prompt": "running"}},
        ]),
        _user_event("intermediate-1"),
        _user_event("intermediate-2"),
        # No tool_result for tu_inflight — subagent still running.
    ])
    events = cc.read_normalized_events("cc:parent:agent:tu_inflight", projects_dir=tmp_path)
    kinds = [e["kind"] for e in events]
    assert kinds == ["assistant_message", "user_message", "user_message"]


@pytest.mark.unit
def test_subagent_window_empty_when_tool_use_id_missing(tmp_path: Path):
    """Unknown tool_use_id returns no events (slice never started)."""
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "parent.jsonl", [
        _user_event("hi"),
        _assistant_event("ok"),
    ])
    events = cc.read_normalized_events("cc:parent:agent:tu_nonexistent", projects_dir=tmp_path)
    assert events == []


# ---------------------------------------------------------------------------
# Live-process detection (psutil)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_live_process_detection_promotes_to_running(tmp_path: Path, monkeypatch):
    """When a live `claude` process matches the project cwd, status is
    `running` and `status_inferred` is False (authoritative)."""
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "old-but-live.jsonl", [_user_event("hi"), _assistant_event("ok")])
    # Backdate the mtime so the heuristic alone would say `completed`.
    old = time.time() - 48 * 3600
    os.utime(proj / "old-but-live.jsonl", (old, old))

    # Force the live-process scan to claim this cwd is held by `claude`.
    monkeypatch.setattr(cc, "live_claude_cwds", lambda now=None: frozenset({"/home/syn/Code/A"}))
    cc.invalidate_process_cache()

    metas = cc.discover_sessions(projects_dir=tmp_path, lookback_days=365)
    meta, _ = cc.parse_session(metas[0], live_cwds=cc.live_claude_cwds())
    assert meta.status == "running"
    assert meta.status_inferred is False


@pytest.mark.unit
def test_live_process_detection_no_match_falls_back_to_heuristic(tmp_path: Path, monkeypatch):
    """When no live process matches, mtime heuristic still applies and the
    status is flagged as inferred."""
    proj = tmp_path / "-home-syn-Code-A"
    _write_jsonl(proj / "idle.jsonl", [_user_event("hi"), _assistant_event("ok")])
    old = time.time() - 7200
    os.utime(proj / "idle.jsonl", (old, old))
    monkeypatch.setattr(cc, "live_claude_cwds", lambda now=None: frozenset())
    cc.invalidate_process_cache()

    metas = cc.discover_sessions(projects_dir=tmp_path, lookback_days=365)
    meta, _ = cc.parse_session(metas[0], live_cwds=cc.live_claude_cwds())
    assert meta.status == "yielded"
    assert meta.status_inferred is True


@pytest.mark.unit
def test_live_process_cache_avoids_repeated_scans(monkeypatch):
    """The process-cwd cache serves repeat calls within the TTL without re-scanning."""
    calls: list[int] = []

    class _FakeProc:
        def __init__(self, name, cmdline, cwd_val):
            self.info = {"name": name, "cmdline": cmdline}
            self._cwd = cwd_val

        def cwd(self):
            return self._cwd

    def _fake_iter(*_, **__):
        calls.append(1)
        return iter([_FakeProc("claude", [], "/some/cwd")])

    import psutil as _psutil
    cc.invalidate_process_cache()
    monkeypatch.setattr(_psutil, "process_iter", _fake_iter)
    a = cc.live_claude_cwds()
    b = cc.live_claude_cwds()  # within TTL — should hit cache
    assert a == b == frozenset({"/some/cwd"})
    assert len(calls) == 1  # process_iter only ran once
