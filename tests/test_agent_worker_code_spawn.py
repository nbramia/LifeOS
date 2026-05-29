"""Tests for the routing='code' operator-spawn helper (#275)."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.code_spawn import (
    parse_code_spawn_payload,
    spawn_code_session,
)
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    SessionStore,
)


pytestmark = pytest.mark.unit


def test_spawn_creates_routing_code_operator_session(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = spawn_code_session(
        store, "write a haiku",
        working_dir="/tmp/wd", plan_mode=True, chat_id="123",
    )
    assert result["ok"]
    session = store.get_by_session_id(result["session_id"])
    assert session is not None
    assert session.routing == "code"
    assert session.origin == "operator"
    assert session.parent_session_id is None
    assert session.status == STATUS_CLAIMED
    # Prompt + dispatch metadata are bundled into the first pending message.
    pending = store.drain_pending_messages(session.session_id)
    assert len(pending) == 1
    payload = parse_code_spawn_payload(pending[0]["content"])
    assert payload["prompt"] == "write a haiku"
    assert payload["working_dir"] == "/tmp/wd"
    assert payload["plan_mode"] is True
    assert payload["chat_id"] == "123"


def test_spawn_rejects_empty_prompt(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "s.db")
    result = spawn_code_session(store, "   ")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_parse_legacy_string_payload_falls_back_to_prompt():
    payload = parse_code_spawn_payload("just a plain string")
    assert payload["prompt"] == "just a plain string"
    assert payload["working_dir"] is None
    assert payload["plan_mode"] is False
    assert payload["chat_id"] is None
