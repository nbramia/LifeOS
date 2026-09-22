"""Telegram notices for agent-initiated project activity.

Covers Worker._reconcile_project_notices: a one-time operator notice on
handoff activation, a one-time notice the first time a project's
agent-created children exceed five, durable dedupe across a worker
restart, retry-after-send-failure, and that the reconciler still fires
while the daily spend cap is reached (it only notifies, never spends).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.task_projects import (
    CHILD_ORIGIN_AGENT,
    CHILD_ORIGIN_FIELD,
    LAST_HANDOFF_OPERATION_FIELD,
    PARENT_ID_FIELD,
)

pytestmark = pytest.mark.unit


class FakeApi:
    """In-memory stand-in for `/api/tasks` — the only endpoint the project
    reconcilers call."""

    def __init__(self, tasks=None):
        self.tasks = {t["id"]: t for t in (tasks or [])}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            return httpx.Response(
                200, json={"tasks": list(self.tasks.values()), "total": len(self.tasks)},
            )
        return httpx.Response(404)


def _agent_child(task_id: str, project_id: str, description: str, *, status: str = "todo") -> dict:
    return {
        "id": task_id,
        "description": description,
        "status": status,
        "tags": [],
        "fields": {PARENT_ID_FIELD: project_id, CHILD_ORIGIN_FIELD: CHILD_ORIGIN_AGENT},
    }


def _operator_child(task_id: str, project_id: str, description: str) -> dict:
    return {
        "id": task_id,
        "description": description,
        "status": "todo",
        "tags": [],
        "fields": {PARENT_ID_FIELD: project_id},
    }


def _project(
    task_id: str, description: str, *, tags=None, handoff_operation_id: str | None = None,
) -> dict:
    fields = {}
    if handoff_operation_id:
        fields[LAST_HANDOFF_OPERATION_FIELD] = handoff_operation_id
    return {
        "id": task_id,
        "description": description,
        "status": "in_progress",
        "tags": list(tags or []),
        "fields": fields,
    }


def _make_worker(
    tmp_path: Path, api: FakeApi, *, telegram_send=None, daily_cap_dollars: float = 100.0,
) -> Worker:
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []

    def _default_send(text, chat_id=None, bot=None):
        sent.append(text)
        return True

    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(
            db_path=tmp_path / "sessions.db", daily_cap_dollars=daily_cap_dollars,
        ),
        poll_seconds=0.01,
        telegram_send=telegram_send if telegram_send is not None else _default_send,
        http_client=client,
    )
    w._sent_telegram = sent  # type: ignore[attr-defined]
    return w


@pytest.mark.unit
def test_handoff_activation_sends_one_notice_naming_title_count_owner_and_board_link(
    tmp_path: Path,
):
    api = FakeApi(tasks=[
        _project("proj1", "Website redesign", tags=["claude"], handoff_operation_id="op-1"),
        _agent_child("c1", "proj1", "Design homepage"),
        _agent_child("c2", "proj1", "Write copy"),
        _agent_child("c3", "proj1", "Build nav"),
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 1
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "Website redesign" in sent[0]
    assert "3" in sent[0]
    assert "#claude" in sent[0]
    assert "/agents?card=proj1" in sent[0]
    assert w.session_store.has_project_notice("proj1", "handoff")
    assert not w.session_store.has_project_notice("proj1", "agent_children_gt5")

    # A second tick doesn't re-notify.
    assert w._reconcile_project_notices() == 0
    assert len(w._sent_telegram) == 1  # type: ignore[attr-defined]


@pytest.mark.unit
def test_restart_with_fresh_worker_same_db_sends_no_duplicate(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Website redesign", tags=["claude"], handoff_operation_id="op-1"),
        _agent_child("c1", "proj1", "Design homepage"),
    ])
    first = _make_worker(tmp_path, api)
    assert first._reconcile_project_notices() == 1
    assert len(first._sent_telegram) == 1  # type: ignore[attr-defined]

    # Fresh Worker instance, same on-disk session store — simulates a
    # worker-process restart mid- or post-activation.
    second = _make_worker(tmp_path, api)
    assert second._reconcile_project_notices() == 0
    assert second._sent_telegram == []  # type: ignore[attr-defined]
    assert second.session_store.has_project_notice("proj1", "handoff")


@pytest.mark.unit
def test_staged_then_cancelled_handoff_sends_nothing(tmp_path: Path):
    """A handoff that was staged but never activated (or was cancelled
    before activating) never sets LAST_HANDOFF_OPERATION_FIELD, so no
    notice fires for it."""
    api = FakeApi(tasks=[
        _project("proj1", "Website redesign", tags=["claude"]),
        _agent_child("c1", "proj1", "Design homepage"),
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 0
    assert w._sent_telegram == []  # type: ignore[attr-defined]
    assert not w.session_store.has_project_notice("proj1", "handoff")


@pytest.mark.unit
def test_spend_cap_reached_still_sends_project_notices(tmp_path: Path):
    """The notice step runs even while the daily spend cap is reached —
    it only notifies, never spends."""
    api = FakeApi(tasks=[
        _project("proj1", "Website redesign", tags=["claude"], handoff_operation_id="op-1"),
        _agent_child("c1", "proj1", "Design homepage"),
    ])
    w = _make_worker(tmp_path, api, daily_cap_dollars=0.0)

    assert w.tick() == 0
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("Website redesign" in text for text in sent)
    assert w.session_store.has_project_notice("proj1", "handoff")


@pytest.mark.unit
def test_operator_created_children_do_not_count_toward_fanout_threshold(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Big project", tags=["claude"]),
        *[_operator_child(f"c{i}", "proj1", f"Operator child {i}") for i in range(8)],
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 0
    assert w._sent_telegram == []  # type: ignore[attr-defined]
    assert not w.session_store.has_project_notice("proj1", "agent_children_gt5")


@pytest.mark.unit
def test_agent_children_crossing_five_to_six_sends_one_fanout_notice(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Big project", tags=["claude"]),
        *[_agent_child(f"c{i}", "proj1", f"Agent child {i}") for i in range(6)],
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 1
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "6" in sent[0]
    assert "Agent child 0" in sent[0]
    assert "/agents?card=proj1" in sent[0]
    assert w.session_store.has_project_notice("proj1", "agent_children_gt5")
    assert not w.session_store.has_project_notice("proj1", "handoff")

    # A second tick doesn't re-notify.
    assert w._reconcile_project_notices() == 0
    assert len(w._sent_telegram) == 1  # type: ignore[attr-defined]


@pytest.mark.unit
def test_five_agent_children_does_not_cross_the_threshold(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Big project", tags=["claude"]),
        *[_agent_child(f"c{i}", "proj1", f"Agent child {i}") for i in range(5)],
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 0
    assert w._sent_telegram == []  # type: ignore[attr-defined]


@pytest.mark.unit
def test_fanout_notice_lists_at_most_ten_titles(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Big project", tags=["claude"]),
        *[_agent_child(f"c{i}", "proj1", f"Agent child {i}") for i in range(12)],
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 1
    body = w._sent_telegram[0]  # type: ignore[attr-defined]
    assert "12" in body
    for i in range(10):
        assert f"Agent child {i}" in body
    assert "Agent child 10" not in body
    assert "Agent child 11" not in body


@pytest.mark.unit
def test_handoff_activation_with_more_than_five_agent_children_sends_one_combined_message(
    tmp_path: Path,
):
    api = FakeApi(tasks=[
        _project("proj1", "Big project", tags=["claude"], handoff_operation_id="op-1"),
        *[_agent_child(f"c{i}", "proj1", f"Agent child {i}") for i in range(6)],
    ])
    w = _make_worker(tmp_path, api)

    assert w._reconcile_project_notices() == 1
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "Big project" in sent[0]
    assert "6" in sent[0]
    assert "Agent child 0" in sent[0]
    assert w.session_store.has_project_notice("proj1", "handoff")
    assert w.session_store.has_project_notice("proj1", "agent_children_gt5")


@pytest.mark.unit
def test_send_failure_is_retried_on_the_next_tick(tmp_path: Path):
    api = FakeApi(tasks=[
        _project("proj1", "Website redesign", tags=["claude"], handoff_operation_id="op-1"),
        _agent_child("c1", "proj1", "Design homepage"),
    ])
    attempts: list[str] = []

    def _flaky_send(text, chat_id=None, bot=None):
        attempts.append(text)
        return False

    w = _make_worker(tmp_path, api, telegram_send=_flaky_send)

    assert w._reconcile_project_notices() == 0
    assert len(attempts) == 1
    assert not w.session_store.has_project_notice("proj1", "handoff")

    # The next tick retries the same unsent notice; this time it succeeds.
    w._raw_telegram_send = lambda text, chat_id=None, bot=None: True
    assert w._reconcile_project_notices() == 1
    assert len(attempts) == 1  # the successful send used the new sender, not the flaky one
    assert w.session_store.has_project_notice("proj1", "handoff")
