"""Tests for the routing='claude_code' operator-spawn helper."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.claude_code_spawn import (
    parse_claude_code_spawn_payload,
    should_use_plan_mode,
    spawn_claude_code_session,
)
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    SessionStore,
)
from api.services.jev_task_routing import JevAnswer, TaskJudgment


pytestmark = pytest.mark.unit


def test_spawn_creates_routing_code_operator_session(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = spawn_claude_code_session(
        store, "write a haiku",
        working_dir="/tmp/wd", plan_mode=True, chat_id="123",
    )
    assert result["ok"]
    session = store.get_by_session_id(result["session_id"])
    assert session is not None
    assert session.routing == "claude_code"
    assert session.origin == "operator"
    assert session.parent_session_id is None
    assert session.status == STATUS_CLAIMED
    # Prompt + dispatch metadata are bundled into the first pending message.
    pending = store.drain_pending_messages(session.session_id)
    assert len(pending) == 1
    payload = parse_claude_code_spawn_payload(pending[0]["content"])
    assert payload["prompt"] == "write a haiku"
    assert payload["working_dir"] == "/tmp/wd"
    assert payload["plan_mode"] is True
    assert payload["chat_id"] == "123"


def test_spawn_rejects_empty_prompt(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = spawn_claude_code_session(store, "   ")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_parse_legacy_string_payload_falls_back_to_prompt():
    payload = parse_claude_code_spawn_payload("just a plain string")
    assert payload["prompt"] == "just a plain string"
    assert payload["working_dir"] is None
    assert payload["plan_mode"] is False
    assert payload["chat_id"] is None


# ---------------------------------------------------------------------------
# should_use_plan_mode — Jev fan-out difficulty judgment, keyword fallback
# ---------------------------------------------------------------------------

def _judgment(score: float | None, confidence: float) -> TaskJudgment:
    return TaskJudgment(
        location=None,
        difficulty=JevAnswer(score=score, confidence=confidence),
        preset_class=None,
        software_work=None,
    )


def test_plan_mode_high_confidence_high_score_true(monkeypatch):
    """confidence >= 0.6 and score >= 2.5 -> plan mode, regardless of title wording."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _judgment(score=2.6, confidence=0.6),
    )
    assert should_use_plan_mode("do a small thing") is True


def test_plan_mode_high_confidence_low_score_false(monkeypatch):
    """confidence >= 0.6 but score < 2.5 -> no plan mode, even for a
    keyword-matching title — the Jev answer wins outright above the
    confidence floor."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _judgment(score=2.4, confidence=0.6),
    )
    assert should_use_plan_mode("refactor the module") is False


def test_plan_mode_low_confidence_falls_back_to_keywords(monkeypatch):
    """confidence just under the 0.6 floor -> ignore the Jev score, use
    the keyword heuristic instead."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _judgment(score=4.9, confidence=0.59),
    )
    assert should_use_plan_mode("fix a typo") is False
    assert should_use_plan_mode("refactor the module") is True


def test_plan_mode_no_judgment_falls_back_to_keywords(monkeypatch):
    """Jev unconfigured or the call failed (judge_task returns None) ->
    unchanged keyword behavior."""
    monkeypatch.setattr("api.services.jev_task_routing.judge_task", lambda title: None)
    assert should_use_plan_mode("build a new integration") is True
    assert should_use_plan_mode("check the weather") is False
