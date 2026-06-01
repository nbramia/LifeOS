"""Tests for the Codex CLI session ingest adapter.

All fixtures are synthetic — real Codex rollouts can contain secrets,
code, and PII and are out of scope.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from api.services.codex import session_ingest as cx


# ---------------------------------------------------------------------------
# Helpers — build a synthetic Codex sessions dir on disk
# ---------------------------------------------------------------------------


def _now_iso(offset: float = 0.0) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_rollout(root: Path, raw_id: str, lines: list[dict],
                   ymd: tuple[str, str, str] = ("2026", "05", "30")) -> Path:
    """Write a synthetic rollout JSONL and return its path."""
    sub = root / ymd[0] / ymd[1] / ymd[2]
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"rollout-2026-05-30T10-41-38-{raw_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    return path


def _session_meta(cwd: str = "/home/test/proj", cli_version: str = "0.135.0",
                  ts_offset: float = 0.0) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "session_meta",
        "payload": {
            "id": "synthetic-uuid",
            "cwd": cwd,
            "originator": "codex_exec",
            "cli_version": cli_version,
            "source": "exec",
            "model_provider": "openai",
            "git": {"branch": "main", "commit_hash": "abc"},
        },
    }


def _turn_context(model: str = "gpt-5.4", ts_offset: float = 0.1) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "turn_context",
        "payload": {"turn_id": "t1", "model": model, "cwd": "/x"},
    }


def _user_event_msg(text: str = "hello", ts_offset: float = 0.2) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _agent_event_msg(text: str = "hi back", ts_offset: float = 0.3) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text, "phase": "final_answer"},
    }


def _token_count(input_tokens: int = 1000, cached: int = 200,
                 output_tokens: int = 50, reasoning: int = 0,
                 ts_offset: float = 0.4) -> dict:
    total = input_tokens + output_tokens
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                },
                "model_context_window": 258400,
            },
        },
    }


def _response_item_message(role: str = "user", text: str = "bundled",
                           ts_offset: float = 0.15) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role != "assistant" else "output_text", "text": text}],
        },
    }


def _developer_message(ts_offset: float = 0.05) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "<permissions instructions>"}],
        },
    }


def _shell_tool_call(call_id: str = "c1", ts_offset: float = 0.25) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "response_item",
        "payload": {
            "type": "local_shell_call",
            "call_id": call_id,
            "name": "shell",
            "arguments": '{"cmd": "ls"}',
        },
    }


def _shell_tool_result(call_id: str = "c1", is_error: bool = False,
                       ts_offset: float = 0.26) -> dict:
    return {
        "timestamp": _now_iso(ts_offset),
        "type": "response_item",
        "payload": {
            "type": "local_shell_call_output",
            "call_id": call_id,
            "is_error": is_error,
            "output": "file1\nfile2\n",
        },
    }


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_session_id_rejects_traversal():
    for bad in ["", "..", "../etc", "a/b", "a\\b", "../../etc/passwd"]:
        with pytest.raises(ValueError):
            cx.validate_session_id(bad)


@pytest.mark.unit
def test_validate_session_id_strips_prefix():
    assert cx.validate_session_id("cx:019e7955-5e4f-7a01") == "019e7955-5e4f-7a01"
    assert cx.validate_session_id("019e7955") == "019e7955"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_sessions_finds_rollouts(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-aaa", [_session_meta()])
    _write_rollout(root, "session-bbb", [_session_meta()], ymd=("2026", "05", "29"))

    metas = cx.discover_sessions(sessions_dir=root)
    assert {m.raw_session_id for m in metas} == {"session-aaa", "session-bbb"}
    assert all(m.session_id.startswith("cx:") for m in metas)


@pytest.mark.unit
def test_discover_sessions_respects_lookback(tmp_path):
    root = tmp_path / "sessions"
    p_old = _write_rollout(root, "session-old", [_session_meta()])
    # Backdate well outside the lookback window.
    import os
    old_t = time.time() - 30 * 86_400
    os.utime(p_old, (old_t, old_t))
    _write_rollout(root, "session-new", [_session_meta()])

    metas = cx.discover_sessions(sessions_dir=root, lookback_days=7)
    assert {m.raw_session_id for m in metas} == {"session-new"}


@pytest.mark.unit
def test_discover_sessions_missing_dir_returns_empty(tmp_path):
    assert cx.discover_sessions(sessions_dir=tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_session_populates_metadata(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-abc", [
        _session_meta(cwd="/home/test/proj", cli_version="0.135.0"),
        _turn_context(model="gpt-5.4"),
        _developer_message(),
        _response_item_message(role="user", text="AGENTS.md bundled context here"),
        _user_event_msg(text="real prompt"),
        _agent_event_msg(text="real response"),
        _token_count(input_tokens=10_000, cached=2_000, output_tokens=500),
    ])

    metas = cx.discover_sessions(sessions_dir=root)
    assert len(metas) == 1
    parsed, events = cx.parse_session(metas[0])

    assert parsed.decoded_cwd == "/home/test/proj"
    assert parsed.cli_version == "0.135.0"
    assert parsed.model == "gpt-5.4"
    assert parsed.git_branch == "main"
    # Label uses the clean event_msg user prompt, not the response_item bundle.
    assert parsed.label == "real prompt"
    assert parsed.first_user_text == "real prompt"
    # Token rollup picks up the cumulative total.
    assert parsed.total_input_tokens == 10_000
    assert parsed.total_cached_input_tokens == 2_000
    assert parsed.total_output_tokens == 500
    # gpt-5.4 has placeholder pricing — verify > 0 so a regression to "model
    # missing" silently zeroing is caught.
    assert parsed.total_dollars > 0
    # Developer message was dropped.
    kinds = [e["kind"] for e in events]
    assert "user_message" in kinds
    assert "context_message" in kinds  # response_item user bundle
    # No assistant_message kind missing
    assert "assistant_message" in kinds


@pytest.mark.unit
def test_parse_session_counts_tool_calls_and_errors(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-tool", [
        _session_meta(),
        _turn_context(),
        _user_event_msg(text="run ls"),
        _shell_tool_call(call_id="c1"),
        _shell_tool_result(call_id="c1", is_error=False),
        _shell_tool_call(call_id="c2"),
        _shell_tool_result(call_id="c2", is_error=True),
        _agent_event_msg(text="done"),
    ])
    metas = cx.discover_sessions(sessions_dir=root)
    parsed, _ = cx.parse_session(metas[0])
    assert parsed.tool_call_count == 2
    assert parsed.error_count == 1


@pytest.mark.unit
def test_parse_session_handles_malformed_lines(tmp_path):
    root = tmp_path / "sessions"
    path = _write_rollout(root, "session-bad", [_session_meta(), _user_event_msg()])
    # Append malformed lines that should be silently skipped.
    with path.open("a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write("\n")
        f.write('{"type": "weird_unknown"}\n')
    metas = cx.discover_sessions(sessions_dir=root)
    parsed, events = cx.parse_session(metas[0])
    # The bad lines didn't break anything; we still got the user_message.
    assert any(e["kind"] == "user_message" for e in events)


# ---------------------------------------------------------------------------
# Status inference
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_infer_status_thresholds():
    now = 1_000_000.0
    # < 10 min → running
    s, inferred = cx._infer_status(mtime=now - 60, pending_tool=False,
                                   last_event_was_error=False, now=now)
    assert s == "running" and inferred is True
    # < 24h → inactive
    s, _ = cx._infer_status(mtime=now - 3600, pending_tool=False,
                            last_event_was_error=False, now=now)
    assert s == "inactive"
    # > 24h, error → failed
    s, _ = cx._infer_status(mtime=now - 2 * 86400, pending_tool=False,
                            last_event_was_error=True, now=now)
    assert s == "failed"
    # > 24h, pending tool → inactive
    s, _ = cx._infer_status(mtime=now - 2 * 86400, pending_tool=True,
                            last_event_was_error=False, now=now)
    assert s == "inactive"
    # > 24h, clean exit → completed
    s, _ = cx._infer_status(mtime=now - 2 * 86400, pending_tool=False,
                            last_event_was_error=False, now=now)
    assert s == "completed"
    # Live process is authoritative.
    s, inferred = cx._infer_status(mtime=0, pending_tool=False,
                                   last_event_was_error=False, now=now,
                                   has_live_process=True)
    assert s == "running" and inferred is False


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_snapshot_includes_codex_sessions(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-snap", [
        _session_meta(cwd="/x"),
        _turn_context(model="gpt-5.4"),
        _user_event_msg(text="hi"),
        _agent_event_msg(text="hello"),
        _token_count(),
    ])
    sessions, edges = cx.build_snapshot(sessions_dir=root, cache_ttl=0)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["source"] == "codex"
    assert s["session_id"].startswith("cx:")
    assert s["model_label"] == "GPT-5"
    assert s["decoded_cwd"] == "/x"
    assert edges == []


@pytest.mark.unit
def test_build_snapshot_caches(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-cache", [_session_meta()])
    cx.invalidate_cache()

    calls = {"n": 0}
    real_discover = cx.discover_sessions

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(cx, "discover_sessions", _spy)
    cx.build_snapshot(sessions_dir=root, cache_ttl=60)
    cx.build_snapshot(sessions_dir=root, cache_ttl=60)
    assert calls["n"] == 1  # second call hit the cache


# ---------------------------------------------------------------------------
# read_normalized_events
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_normalized_events_round_trip(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-read", [
        _session_meta(),
        _user_event_msg(text="hello"),
        _agent_event_msg(text="world"),
    ])
    events = cx.read_normalized_events("cx:session-read", sessions_dir=root)
    kinds = [e["kind"] for e in events]
    assert "user_message" in kinds and "assistant_message" in kinds


@pytest.mark.unit
def test_read_normalized_events_unknown_id(tmp_path):
    root = tmp_path / "sessions"
    _write_rollout(root, "session-real", [_session_meta()])
    assert cx.read_normalized_events("cx:session-missing", sessions_dir=root) == []
