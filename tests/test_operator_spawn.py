"""Tests for operator root-spawn (#235, Phase 2 of #233).

Covers `create_operator_session`: explicit-routing override, preflight
auto-route, the ambiguous (ROUTE_ASK) clarification path, the operator origin
marker, prompt enqueueing, and input validation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.agent_worker.operator_spawn import create_operator_session
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_CLAIMED,
    SessionStore,
)


def _preflight(routing: str, *, sane: bool = True, sane_reason: str = ""):
    """Build a fake preflight caller that returns the given routing."""
    def _caller(prompt: str) -> str:
        return json.dumps({
            "budget": {"wall_seconds": 3600, "max_tokens": 1000, "max_dollars": 5.0},
            "routing": routing, "routing_reason": "test",
            "expected_output": "text",
            "ambiguity": None, "sane": sane, "sane_reason": sane_reason,
        })
    return _caller


@pytest.mark.unit
def test_explicit_claude_routing_skips_preflight(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    # Preflight that would say "local" — explicit should win and it shouldn't
    # even be consulted (we still pass it to prove override precedence).
    result = create_operator_session(
        store, "refactor the parser", explicit_routing="claude",
        preflight_caller=_preflight("local"),
    )
    assert result["ok"]
    assert result["routing"] == "claude"
    assert result["routing_source"] == "explicit"
    assert not result["needs_routing"]
    sess = store.get_by_session_id(result["session_id"])
    assert sess.status == STATUS_CLAIMED
    assert sess.origin == "operator"
    assert sess.parent_session_id is None
    assert sess.root_session_id == sess.session_id


@pytest.mark.unit
def test_explicit_local_routing(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(
        store, "draft a reply", explicit_routing="local",
        preflight_caller=_preflight("claude"),
    )
    assert result["ok"] and result["routing"] == "local"
    assert store.get_by_session_id(result["session_id"]).routing == "local"


@pytest.mark.unit
def test_no_keyword_routes_via_preflight(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(
        store, "summarize my week", preflight_caller=_preflight("claude"),
    )
    assert result["ok"]
    assert result["routing"] == "claude"
    assert result["routing_source"] == "preflight"
    assert not result["needs_routing"]
    assert store.get_by_session_id(result["session_id"]).status == STATUS_CLAIMED


@pytest.mark.unit
def test_ambiguous_preflight_parks_for_routing(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(
        store, "do the thing", preflight_caller=_preflight("ask"),
    )
    assert result["ok"]
    assert result["needs_routing"] is True
    assert result["routing"] == "ask"
    sess = store.get_by_session_id(result["session_id"])
    assert sess.status == STATUS_BLOCKED
    assert sess.routing == "ask"
    assert sess.origin == "operator"


@pytest.mark.unit
def test_prompt_is_enqueued_as_pending_message(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(
        store, "email the landlord", explicit_routing="local",
    )
    pending = store.drain_pending_messages(result["session_id"])
    assert len(pending) == 1
    assert pending[0]["content"] == "email the landlord"


@pytest.mark.unit
def test_empty_prompt_rejected(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(store, "   ", explicit_routing="local")
    assert not result["ok"]
    assert "prompt is required" in result["error"]


@pytest.mark.unit
def test_unsafe_preflight_rejected(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(
        store, "rm -rf everything",
        preflight_caller=_preflight("ask", sane=False, sane_reason="destructive"),
    )
    assert not result["ok"]
    assert "unsafe" in result["error"]


@pytest.mark.unit
def test_operator_budget_defaults_applied(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = create_operator_session(store, "do X", explicit_routing="local")
    sess = store.get_by_session_id(result["session_id"])
    assert sess.budget is not None
    assert set(sess.budget) == {"wall_seconds", "max_tokens", "max_dollars"}
