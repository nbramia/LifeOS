"""Billing-consent regressions for actual durable-project handoffs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from api.services.agent_worker.execution import (
    BillingClass,
    ExecutionConstraints,
    ExecutionSpec,
)
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, dispatch
from api.services.agent_worker.session_store import STATUS_CLAIMED, SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import TaskManager
from api.services.task_projects import HANDOFF_OPERATION_FIELD


pytestmark = pytest.mark.unit


def _execution_spec(executor: str) -> dict:
    return ExecutionSpec(
        executor=executor,
        provider=executor,
        runtime="in_process",
        model_id=None,
        effort=None,
        host=None,
        working_dir=None,
        persona_id=None,
        parent_session_id=None,
        root_session_id=None,
        reply_destination=None,
        budget=None,
        constraints=ExecutionConstraints(),
        billing=BillingClass.LOCAL_FREE if executor == "local" else BillingClass.METERED,
        resolved_at=datetime.now(timezone.utc),
    ).to_dict()


def _handoff_context(tmp_path: Path, routing: str):
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Coordinate synthetic consent work",
        status="in_progress",
        tags=[routing, "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing=routing,
        execution_spec=_execution_spec(routing),
    )
    source = store.begin_executor_turn(parent.id, "execute", session=source)
    assert store.mark_executor_turn_running(parent.id, source.attempt_id, source.turn_id)
    source = store.get(parent.id)
    return (
        manager,
        store,
        transcripts,
        source,
        InterAgentContext(
            store,
            transcripts,
            source.session_id,
            Caps(),
            caller_attempt_id=source.attempt_id,
            caller_turn_id=source.turn_id,
            task_manager=manager,
        ),
    )


def _request(target_executor: str) -> dict:
    return {
        "operation_id": "synthetic-billing-v1",
        "children": [{
            "key": "metered",
            "description": "Run bounded synthetic metered work",
            "execution": {"executor": target_executor},
        }],
    }


@pytest.mark.parametrize(
    ("routing", "target_executor"),
    [
        ("claude_code", "claude"),
        ("codex", "remote"),
        ("hermes", "claude"),
        ("hermes", "remote"),
        ("local", "claude"),
        ("local", "remote"),
    ],
)
def test_handoff_never_expands_billing_consent_before_any_write(
    tmp_path: Path, routing: str, target_executor: str,
):
    manager, store, transcripts, source, ctx = _handoff_context(tmp_path, routing)

    result = dispatch(ctx, "lifeos_agent_project_handoff", _request(target_executor))

    assert result == {
        "ok": False,
        "error": "api_billing_blocked",
        "message": (
            f"child metered requests metered executor {target_executor} outside "
            "the source turn's explicit target"
        ),
    }
    assert [task.id for task in manager.list_tasks()] == [source.task_id]
    assert store.list_sessions() == [source]
    assert HANDOFF_OPERATION_FIELD not in manager.get(source.task_id).fields
    assert transcripts.read(source.session_id) == []


def test_handoff_allows_explicit_same_metered_target_and_unassigned_child(tmp_path: Path):
    manager, store, transcripts, source, ctx = _handoff_context(tmp_path, "claude")
    request = _request("claude")
    request["children"].append({
        "key": "unassigned",
        "description": "Leave synthetic follow-up independently unassigned",
    })

    result = dispatch(ctx, "lifeos_agent_project_handoff", request)

    assert result["ok"] is True
    assert result["state"] == "staged"
    assert len(manager.list_tasks()) == 3
    assert len(store.list_sessions()) == 2
    assert HANDOFF_OPERATION_FIELD in manager.get(source.task_id).fields
    assert [event["kind"] for event in transcripts.read(source.session_id)] == [
        "project_handoff_requested",
        "project_handoff_staged",
    ]
