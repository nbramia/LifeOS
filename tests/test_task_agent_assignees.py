"""External agent assignees (duet, muse) and the rules that stop agents
fighting over tasks.

An agent may claim an unassigned task, update and release its own, and edit
the operator's tasks without taking them. It never touches a task assigned to
another agent or run by an engine, and never assigns anyone but itself. Only
the operator reassigns across agents.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services import agent_board
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


# ── agent_board rules ─────────────────────────────────────────────────────


def test_duet_and_muse_are_assignees_the_worker_never_picks_up():
    for name in ("duet", "muse"):
        assert name in agent_board.ASSIGNEE_TAGS
        assert name in agent_board.EXTERNAL_AGENT_ASSIGNEES
        assert name in agent_board.PROTECTED_TAGS
        assert name not in agent_board.AGENT_ASSIGNEES
        assert name not in agent_board.AGENT_PICKUP_TAGS
    assert agent_board.derive_assignee(["errand", "duet"]) == "duet"
    assert agent_board.natural_lane("todo", ["muse"]) == "assigned"


@pytest.mark.parametrize("tags,assignee", [
    ([], None),
    ([], "duet"),          # claim an unassigned task
    (["duet"], None),
    (["duet"], "duet"),
    (["duet"], "none"),    # release its own
    (["me"], None),        # edit the operator's task without taking it
    (["errand"], "duet"),
])
def test_duet_may(tags, assignee):
    assert agent_board.agent_task_edit_error("todo", tags, "duet", assignee) is None


@pytest.mark.parametrize("tags,assignee", [
    (["muse"], None),                 # another agent's task
    (["muse"], "duet"),               # taking another agent's task
    (["muse"], "none"),               # unassigning another agent's task
    ([], "muse"),                     # assigning another agent
    ([], "me"),                       # assigning the operator
    ([], "claude"),                   # assigning an engine
    (["me"], "duet"),                 # taking the operator's task
    (["me"], "none"),
    (["claude"], None),               # engine-owned
    (["codex", "agent-running"], None),
    (["agent"], None),
    (["cloud-haiku"], None),
    (["human"], None),                # operator queue card
    (["duet", "agent-blocked"], None),
])
def test_duet_may_not(tags, assignee):
    error = agent_board.agent_task_edit_error("todo", tags, "duet", assignee)
    assert error is not None
    assert error[0] in (403, 409)


def test_swap_assignee_keeps_other_tags_and_clears_consent_tags():
    assert agent_board.swap_assignee(["errand", "me", "#cloud-haiku", "x"], "duet") == ["errand", "x", "duet"]
    assert agent_board.swap_assignee(["errand", "duet"], "none") == ["errand"]


# ── API, against a real task store ────────────────────────────────────────


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from api.routes import tasks as task_routes
    from api.services.agent_worker.session_store import SessionStore

    vault = tmp_path / "vault"
    (vault / "LifeOS" / "Tasks").mkdir(parents=True)
    tm = TaskManager(vault_path=vault, index_path=tmp_path / "index.json",
                     live_session_checker=lambda *a: False, live_coordinator_checker=lambda *a: False)
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: tm)
    monkeypatch.setattr(task_routes, "_session_store", SessionStore(db_path=tmp_path / "sessions.db"))
    return tm


@pytest.fixture
def api(manager):
    from api.routes import tasks as task_routes

    app = FastAPI()
    app.include_router(task_routes.router)
    return TestClient(app)


def _task(manager, tags=()):
    return manager.create(description="Synthetic errand", tags=list(tags)).id


def _tags(manager, task_id):
    manager.rebuild_index()
    return set(manager.get(task_id).tags)


def test_agent_claims_updates_and_releases_an_unassigned_task(api, manager):
    task_id = _task(manager, ["errand"])
    resp = api.put(f"/api/tasks/{task_id}", json={
        "acting_agent": "duet", "assignee": "duet", "status": "in_progress",
    })
    assert resp.status_code == 200, resp.text
    assert _tags(manager, task_id) == {"errand", "duet"}
    assert manager.get(task_id).status == "in_progress"

    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "notes": "Drafted the reply"})
    assert resp.status_code == 200, resp.text
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "status": "done"})
    assert resp.status_code == 200, resp.text
    assert manager.get(task_id).status == "done"

    other = _task(manager, ["duet"])
    resp = api.put(f"/api/tasks/{other}", json={"acting_agent": "duet", "assignee": "none"})
    assert resp.status_code == 200, resp.text
    assert _tags(manager, other) == set()


@pytest.mark.parametrize("tags,body,code", [
    (["muse"], {"status": "in_progress"}, 409),
    (["muse"], {"assignee": "duet"}, 409),
    ([], {"assignee": "muse"}, 403),
    ([], {"assignee": "claude"}, 403),
    (["me"], {"assignee": "duet"}, 409),
    (["claude"], {"notes": "x"}, 409),
    (["human"], {"status": "done"}, 409),
])
def test_agent_cannot_fight_over_tasks(api, manager, tags, body, code):
    task_id = _task(manager, tags)
    before = (manager.tasks_dir / "Inbox.md").read_text()
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", **body})
    assert resp.status_code == code, resp.text
    assert (manager.tasks_dir / "Inbox.md").read_text() == before


def test_agent_may_finish_the_operators_task_without_taking_it(api, manager):
    task_id = _task(manager, ["me"])
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "status": "done"})
    assert resp.status_code == 200, resp.text
    assert _tags(manager, task_id) == {"me"}


@pytest.mark.parametrize("body", [
    {"acting_agent": "duet", "tags": ["duet"]},
    {"acting_agent": "duet", "fields": {"parent_id": "x"}},
    {"acting_agent": "duet", "context": "Personal"},
    {"acting_agent": "duet", "priority": "high #claude"},
    {"acting_agent": "duet", "priority": "urgent"},
    {"acting_agent": "duet", "due_date": "2026-12-01 #claude"},
    {"acting_agent": "duet", "due_date": "2026-13-45"},
    {"acting_agent": "duet", "due_date": "12/01/2026"},
    {"acting_agent": "duet", "status": "finished"},
    {"acting_agent": "duet", "actor": "worker", "status": "done"},
    {"acting_agent": "claude", "status": "done"},
    {"assignee": "somebody"},
    {"assignee": "duet", "tags": ["duet"]},
])
def test_malformed_agent_requests_are_422(api, manager, body):
    task_id = _task(manager)
    before = (manager.tasks_dir / "Inbox.md").read_text()
    assert api.put(f"/api/tasks/{task_id}", json=body).status_code == 422
    assert (manager.tasks_dir / "Inbox.md").read_text() == before


def test_agent_may_set_documented_priority_and_due_date_or_clear_them(api, manager):
    task_id = _task(manager)
    resp = api.put(f"/api/tasks/{task_id}", json={
        "acting_agent": "duet", "priority": "high", "due_date": "2026-12-01",
    })
    assert resp.status_code == 200, resp.text
    assert (manager.get(task_id).priority, manager.get(task_id).due_date) == ("high", "2026-12-01")
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "priority": "", "due_date": ""})
    assert resp.status_code == 200, resp.text
    assert not manager.get(task_id).priority and not manager.get(task_id).due_date


def test_context_refusal_names_the_field(api, manager):
    task_id = _task(manager)
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "context": "Personal"})
    assert resp.status_code == 422 and "context" in resp.json()["detail"]


def test_assignee_swap_keeps_a_tag_added_after_the_agent_read_the_task(api, manager, monkeypatch):
    import copy

    task_id = _task(manager, ["errand"])
    stale = copy.copy(manager.get(task_id))
    path = manager.tasks_dir / "Inbox.md"
    path.write_text(path.read_text().replace("Synthetic errand", "Synthetic errand #late-tag"))
    monkeypatch.setattr(manager, "get", lambda _id: stale)
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "assignee": "duet"})
    assert resp.status_code == 200, resp.text
    line = next(ln for ln in path.read_text().splitlines() if task_id in ln)
    assert {t for t in line.split() if t.startswith("#")} == {"#errand", "#late-tag", "#duet"}


def test_review_reassign_to_an_external_agent_leaves_the_card_editable_by_it():
    for target in ("duet", "muse"):
        plan = agent_board.plan_review_action("done", ["claude", "agent-completed"], "reassign", target)
        assert plan.error is None and target in plan.tags
        assert "agent-reassigned" not in plan.tags
        assert agent_board.agent_task_edit_error(plan.status, plan.tags, target, None) is None


def test_review_reassign_to_an_engine_still_marks_the_card_reassigned():
    plan = agent_board.plan_review_action("done", ["claude", "agent-completed"], "reassign", "codex")
    assert "agent-reassigned" in plan.tags


def test_rule_is_rechecked_against_the_snapshot_being_written(api, manager, monkeypatch):
    """Another agent claims the task between the route's read and the write."""
    import copy

    task_id = _task(manager)
    stale = copy.copy(manager.get(task_id))
    path = manager.tasks_dir / "Inbox.md"
    path.write_text(path.read_text().replace("Synthetic errand", "Synthetic errand #muse"))
    monkeypatch.setattr(manager, "get", lambda _id: stale)
    resp = api.put(f"/api/tasks/{task_id}", json={"acting_agent": "duet", "assignee": "duet"})
    assert resp.status_code == 409, resp.text
    assert "muse" in path.read_text() and "#duet" not in path.read_text()


def test_operator_can_assign_any_assignee_and_keeps_other_tags(api, manager):
    task_id = _task(manager, ["errand", "duet"])
    resp = api.put(f"/api/tasks/{task_id}", json={"assignee": "muse"})
    assert resp.status_code == 200, resp.text
    assert _tags(manager, task_id) == {"errand", "muse"}


def test_operator_assignee_change_on_a_claimed_card_is_refused(api, manager):
    task_id = _task(manager, ["claude", "agent-running"])
    resp = api.put(f"/api/tasks/{task_id}", json={"assignee": "duet"})
    assert resp.status_code == 409, resp.text
    assert _tags(manager, task_id) == {"claude", "agent-running"}
