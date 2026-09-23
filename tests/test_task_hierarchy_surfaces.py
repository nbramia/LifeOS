"""Task-hierarchy contracts exposed to native chat and MCP clients."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from api.services import agent_tools
from api.routes import tasks as task_routes
from api.services.agent_tools import (
    TOOL_DEFINITIONS,
    _journal_tool_gate,
    _tool_manage_tasks,
    execute_tool_parallel,
)
from api.services.agent_worker.session_store import SessionStore, STATUS_RUNNING
from api.services.agent_worker.tools import ToolRegistry
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    COORDINATOR_SESSION_FIELD,
    ProjectConflictError,
    ProjectTaskService,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("mcp_server_hierarchy", _ROOT / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _native_task_schema():
    return next(t for t in TOOL_DEFINITIONS if t["name"] == "manage_tasks")["input_schema"]


@pytest.fixture
def native_projects(tmp_path, monkeypatch):
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    sessions = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")

    import api.services.task_manager as task_manager_module

    monkeypatch.setattr(task_manager_module, "get_task_manager", lambda: manager)
    monkeypatch.setattr(
        agent_tools,
        "_task_project_service",
        lambda tm, *, lifecycle=False: ProjectTaskService(tm, sessions, transcripts),
    )
    return manager, sessions, transcripts


def test_native_task_tool_advertises_hierarchy_and_project_actions():
    schema = _native_task_schema()
    actions = schema["properties"]["action"]["enum"]
    assert {
        "create", "list", "update", "complete", "children",
        "start_project", "complete_project", "plan_project",
        "cancel_project", "resume_execution",
    } <= set(actions)

    props = schema["properties"]
    assert "parent_id" in props
    assert "operation_id" in props
    assert "operation_key" in props
    assert "confirm" in props
    assert "acknowledge_cancelled_children" in props
    assert "parent references" in props["parent_id"]["description"]


@pytest.mark.parametrize(
    "action",
    ["update", "complete", "start_project", "complete_project", "plan_project", "cancel_project", "resume_execution"],
)
def test_journal_persona_cannot_invoke_task_or_project_mutations(action):
    allowed, error = _journal_tool_gate("manage_tasks", {"action": action}, "synthetic")
    assert allowed is None
    assert error and "does not edit existing tasks or projects" in error


def test_journal_persona_strips_an_invented_parent_relationship():
    filtered, error = _journal_tool_gate(
        "manage_tasks",
        {"action": "create", "description": "Synthetic follow-up", "parent_id": "project-1"},
        "Remember to do the synthetic follow-up",
    )
    assert error is None
    assert "parent_id" not in filtered


def test_journal_persona_preserves_operator_attested_parent_relationship():
    filtered, error = _journal_tool_gate(
        "manage_tasks",
        {"action": "create", "description": "Synthetic follow-up", "parent_id": "project-1"},
        "Create a subtask parent_id:project-1 for the synthetic follow-up",
    )
    assert error is None
    assert filtered["parent_id"] == "project-1"


def test_journal_persona_preserves_a_parent_created_earlier_this_turn():
    filtered, error = _journal_tool_gate(
        "manage_tasks",
        {"action": "create", "description": "Clear the shelves", "parent_id": "project-1"},
        "Make a project to renovate the synthetic garage with subtasks clear the shelves",
        {"project-1"},
    )
    assert error is None
    assert filtered["parent_id"] == "project-1"


def test_journal_persona_strips_a_parent_id_from_a_different_turns_set():
    filtered, error = _journal_tool_gate(
        "manage_tasks",
        {"action": "create", "description": "Clear the shelves", "parent_id": "project-1"},
        "Make a project to renovate the synthetic garage with subtasks clear the shelves",
        {"some-other-turns-task-id"},
    )
    assert error is None
    assert "parent_id" not in filtered


def test_journal_persona_strips_a_parent_id_only_named_in_prose_even_with_a_turn_set():
    # The id merely appears in the message's own text (not as a
    # `parent_id:<id>` attestation and not in this turn's created set) --
    # still stripped, matching the existing prose-only case.
    filtered, error = _journal_tool_gate(
        "manage_tasks",
        {"action": "create", "description": "Clear the shelves", "parent_id": "project-1"},
        "This follows from project-1, the garage renovation",
        set(),
    )
    assert error is None
    assert "parent_id" not in filtered


def test_mcp_catalog_exposes_every_project_action_endpoint():
    module = _load_mcp_module()
    by_name = {config["name"]: config for config in module.CURATED_ENDPOINTS.values()}
    assert {
        "lifeos_task_children",
        "lifeos_project_start",
        "lifeos_project_complete",
        "lifeos_project_plan",
        "lifeos_project_cancel",
        "lifeos_task_resume_execution",
    } <= set(by_name)

    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    schemas = server._fallback_schemas()
    assert "operation_key" in schemas["lifeos_task_create"]["properties"]
    assert "parent_id" in schemas["lifeos_task_create"]["properties"]["fields"]["description"]
    assert "parent_id" in schemas["lifeos_task_update"]["properties"]["fields"]["description"]
    assert schemas["lifeos_task_children"]["required"] == ["task_id"]
    assert schemas["lifeos_project_cancel"]["properties"]["confirm"]["type"] == "boolean"
    assert schemas["lifeos_project_cancel"]["required"] == ["task_id"]


def _mcp_server_with_client(module):
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {}
    response.raise_for_status = MagicMock()
    client.get.return_value = response
    client.post.return_value = response
    server.client = client
    return server, client


def test_mcp_children_dispatches_parent_id_in_path_and_paging_as_query():
    module = _load_mcp_module()
    server, client = _mcp_server_with_client(module)

    server._call_api(
        "lifeos_task_children",
        {"task_id": "project-1", "limit": 25, "offset": 50},
    )

    assert client.get.call_count == 1
    assert client.get.call_args.args[0].endswith("/api/tasks/project-1/children")
    assert client.get.call_args.kwargs["params"] == {"limit": 25, "offset": 50}


def test_mcp_task_create_forwards_retry_operation_key():
    module = _load_mcp_module()
    server, client = _mcp_server_with_client(module)
    operation_key = "project:project-1:plan:synthetic:child:1"

    server._call_api(
        "lifeos_task_create",
        {
            "description": "Synthetic retry-safe child",
            "operation_key": operation_key,
            "fields": {"parent_id": "project-1"},
        },
    )

    assert client.post.call_count == 1
    assert client.post.call_args.args[0].endswith("/api/tasks")
    assert client.post.call_args.kwargs["json"]["operation_key"] == operation_key
    assert client.post.call_args.kwargs["json"]["fields"] == {"parent_id": "project-1"}


@pytest.mark.parametrize(
    ("tool_name", "suffix", "body"),
    [
        ("lifeos_project_start", "/api/tasks/project-1/project/start", {}),
        (
            "lifeos_project_complete",
            "/api/tasks/project-1/project/complete",
            {"acknowledge_cancelled_children": True},
        ),
        (
            "lifeos_project_plan",
            "/api/tasks/project-1/project/plan",
            {"operation_id": "op-synthetic-plan"},
        ),
        (
            "lifeos_project_cancel",
            "/api/tasks/project-1/project/cancel",
            {"confirm": True, "operation_id": "op-synthetic-cancel"},
        ),
        (
            "lifeos_task_resume_execution",
            "/api/tasks/project-1/resume-execution",
            {},
        ),
    ],
)
def test_mcp_project_actions_dispatch_to_authoritative_http_routes(
    tool_name, suffix, body,
):
    module = _load_mcp_module()
    server, client = _mcp_server_with_client(module)
    arguments = {"task_id": "project-1", **body}

    server._call_api(tool_name, arguments)

    assert client.post.call_count == 1
    assert client.post.call_args.args[0].endswith(suffix)
    assert client.post.call_args.kwargs["json"] == body


def test_live_openapi_mcp_schemas_describe_hierarchy_inputs():
    module = _load_mcp_module()
    from api.main import app

    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    server.openapi_spec = app.openapi()
    server._build_tools_from_spec()
    tools = {tool["name"]: tool for tool in server.tools}

    for name in (
        "lifeos_task_children", "lifeos_project_start", "lifeos_project_complete",
        "lifeos_project_plan", "lifeos_project_cancel", "lifeos_task_resume_execution",
    ):
        assert name in tools

    for name in ("lifeos_task_create", "lifeos_task_update"):
        description = tools[name]["inputSchema"]["properties"]["fields"]["description"]
        assert "parent_id" in description
    assert "operation_key" in tools["lifeos_task_create"]["inputSchema"]["properties"]
    update_description = tools["lifeos_task_update"]["inputSchema"]["properties"]["fields"]["description"]
    assert "null" in update_description and "detach" in update_description

    cancel = tools["lifeos_project_cancel"]["inputSchema"]["properties"]
    assert cancel["confirm"]["type"] == "boolean"
    assert "operation_id" in cancel


def test_mcp_task_output_discloses_real_filtered_and_paginated_responses(
    tmp_path: Path, monkeypatch,
):
    module = _load_mcp_module()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    manager = TaskManager(tmp_path / "vault", tmp_path / "task-index.json")
    parent = manager.create("Synthetic project")
    manager.create(
        "Synthetic accepted child", status="done", tags=["agent-completed", "accepted"],
        fields={"parent_id": parent.id},
    )
    manager.create(
        "Synthetic review child", tags=["agent-completed"], fields={"parent_id": parent.id},
    )
    manager.create(
        "Synthetic cancelled child", status="cancelled", fields={"parent_id": parent.id},
    )
    manager.create(
        "Synthetic blocked child", tags=["agent-blocked"], fields={"parent_id": parent.id},
    )
    for index in range(3):
        manager.create(f"Synthetic child {index}", fields={"parent_id": parent.id})
    manager.create("Synthetic done task", status="done")
    sessions = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", sessions)
    monkeypatch.setattr(task_routes, "_transcript_store", TranscriptStore(tmp_path / "transcripts"))
    client = TestClient(app)

    children_response = client.get(f"/api/tasks/{parent.id}/children?limit=2&offset=0")
    assert children_response.status_code == 200
    children_text = server._format_response(
        "lifeos_task_children", children_response.json(),
        {"task_id": parent.id, "limit": 2, "offset": 0},
    )
    assert "Found 7 child tasks" in children_text
    assert "Showing 2 of 7 children; offset 0" in children_text
    assert f"Parent: Synthetic project [id:{parent.id}]" in children_text

    empty_page = client.get(f"/api/tasks/{parent.id}/children?limit=2&offset=20")
    assert empty_page.status_code == 200
    assert "Showing 0 of 7 children; offset 20" in server._format_response(
        "lifeos_task_children", empty_page.json(),
        {"task_id": parent.id, "limit": 2, "offset": 20},
    )

    server._call_api = MagicMock(return_value=children_response.json())
    dispatched = module.dispatch(server, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "lifeos_task_children",
            "arguments": {"task_id": parent.id, "limit": 2, "offset": 0},
        },
    })
    assert "Showing 2 of 7 children; offset 0" in dispatched["result"]["content"][0]["text"]

    server.tools = [{"name": "lifeos_task_children"}]
    worker_result = ToolRegistry(lifeos_mcp_server=server).dispatch(
        "lifeos_task_children",
        {"task_id": parent.id, "limit": 2, "offset": 0},
    )
    assert "Showing 2 of 7 children; offset 0" in worker_result.output

    filtered_response = client.get("/api/tasks?status=todo")
    assert filtered_response.status_code == 200
    filtered_text = server._format_response(
        "lifeos_task_list", filtered_response.json(), {"status": "todo"},
    )
    assert "Scope: filtered task list" in filtered_text
    assert "Project: 7 children" in filtered_text
    for count in ("1 done", "1 awaiting review", "1 cancelled", "1 blocked"):
        assert count in filtered_text

    server.tools = [{"name": "lifeos_task_list"}]
    server._call_api = MagicMock(return_value=filtered_response.json())
    worker_filtered = ToolRegistry(lifeos_mcp_server=server).dispatch(
        "lifeos_task_list", {"status": "todo"},
    )
    assert "Scope: filtered task list" in worker_filtered.output


@pytest.mark.parametrize("data", [
    {"tasks": [{"id": "task-1", "description": "Synthetic task", "status": "todo"}], "total": 1},
    {"tasks": [], "total": 0},
])
def test_mcp_empty_filter_does_not_claim_a_filtered_task_list(data):
    module = _load_mcp_module()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()

    text = server._format_response("lifeos_task_list", data, {"status": ""})

    assert "filtered task list" not in text


def test_mcp_project_cancel_preview_and_partial_result_are_unambiguous():
    module = _load_mcp_module()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()

    preview = server._format_response("lifeos_project_cancel", {
        "project_id": "project-1",
        "operation_id": "op-synthetic-pending",
        "cancellation_pending": True,
        "unfinished_count": 3,
        "running_count": 1,
        "awaiting_review_count": 1,
        "confirmation_required": True,
        "children": [],
    })
    assert "Confirmation required" in preview
    assert "3 unfinished" in preview
    assert "1 running" in preview
    assert "1 awaiting review" in preview
    assert "abandoned" in preview
    assert "op-synthetic-pending" in preview
    assert "reuse" in preview

    partial = server._format_response("lifeos_project_cancel", {
        "project_id": "project-1",
        "operation_id": "op-synthetic",
        "complete": False,
        "pending": True,
        "cancelled_child_ids": ["child-1"],
        "preserved_child_ids": ["child-done"],
        "abandoned_review_ids": ["child-review"],
        "stopped_session_ids": [],
        "failures": [{"session_id": "sess_synthetic", "reason": "manual stop required"}],
    })
    assert "Cancellation incomplete" in partial
    assert "op-synthetic" in partial
    assert "sess_synthetic" in partial
    assert "manual stop required" in partial
    assert "Cancelled children: child-1" in partial
    assert "preserved done children: child-done" in partial
    assert "abandoned review children: child-review" in partial
    assert "retry with the same operation_id" in partial


def test_plain_task_mcp_output_remains_flat_and_compatible():
    module = _load_mcp_module()
    with patch.object(module.LifeOSMCPServer, "_load_openapi_spec", lambda self: None):
        server = module.LifeOSMCPServer()
    text = server._format_response("lifeos_task_list", {
        "tasks": [{
            "id": "task-1", "description": "Synthetic task", "status": "todo",
            "context": "Inbox", "tags": [], "is_project": False, "child_count": 0,
        }],
        "total": 1,
    })
    assert "Synthetic task" in text
    assert "Project:" not in text
    assert "Parent:" not in text


def test_native_create_and_read_use_durable_parent_ids(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic project objective")

    created = _tool_manage_tasks({
        "action": "create",
        "description": "Synthetic independent subtask",
        "parent_id": parent.id,
        "tags": ["local"],
    })
    assert f"Parent: Synthetic project objective [id:{parent.id}]" in created

    listed = _tool_manage_tasks({"action": "list", "query": "Synthetic"})
    assert "Project: 1 children" in listed
    assert f"Parent: Synthetic project objective [id:{parent.id}]" in listed
    assert "Filtered list; project summaries still use the complete task set" in listed

    children = _tool_manage_tasks({"action": "children", "task_id": parent.id})
    assert "Synthetic independent subtask" in children
    assert "Showing 1 of 1 children" in children


def test_native_child_create_reuses_a_stable_operation_key(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic project objective")
    request = {
        "action": "create",
        "description": "Synthetic retry-safe subtask",
        "parent_id": parent.id,
        "operation_key": f"project:{parent.id}:plan:synthetic:child:1",
    }

    first = _tool_manage_tasks(request)
    second = _tool_manage_tasks(request)

    assert "Task created" in first
    assert "Task recovered" in second
    assert len(manager.list_children(parent.id)) == 1
    assert manager.list_children(parent.id)[0].fields["operation_key"] == request["operation_key"]


def test_native_update_preserves_an_existing_parent_relationship(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic project objective")
    child = manager.create(
        "Synthetic assigned subtask",
        tags=["local"],
        fields={"parent_id": parent.id, "assigned_by": "operator"},
    )

    result = _tool_manage_tasks({
        "action": "update",
        "task_id": child.id,
        "tags": ["local", "review"],
    })

    assert f"Parent: Synthetic project objective [id:{parent.id}]" in result
    updated = manager.get(child.id)
    assert updated.fields["parent_id"] == parent.id
    assert updated.fields["assigned_by"] == "operator"


def test_native_complete_project_validates_children_before_closing(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic validated project")
    child = manager.create("Synthetic open child", fields={"parent_id": parent.id})

    with pytest.raises(ProjectConflictError, match="unresolved children"):
        _tool_manage_tasks({"action": "complete_project", "task_id": parent.id})

    manager.complete(child.id)
    result = _tool_manage_tasks({"action": "complete_project", "task_id": parent.id})
    assert f'Project completed: "Synthetic validated project" (id: {parent.id})' == result
    assert manager.get(parent.id).status == "done"


async def test_native_project_cancel_requires_preview_then_cascades(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic cancellation project")
    child = manager.create("Synthetic unfinished child", fields={"parent_id": parent.id})

    preview = await _tool_manage_tasks({"action": "cancel_project", "task_id": parent.id})
    assert "Confirmation required" in preview
    assert "1 unfinished" in preview
    assert "Awaiting-review output will be abandoned" in preview
    assert manager.get(child.id).status == "todo"

    result = await _tool_manage_tasks({
        "action": "cancel_project",
        "task_id": parent.id,
        "confirm": True,
        "operation_id": "op-synthetic-cancel",
    })
    assert "Project cancellation complete" in result
    assert f"Cancelled children: {child.id}" in result
    assert manager.get(child.id).status == "cancelled"
    assert manager.get(parent.id).status == "cancelled"


async def test_native_cancel_preview_exposes_pending_operation_id(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic pending cancellation project")
    manager.create("Synthetic unfinished child", fields={"parent_id": parent.id})
    manager.update(
        parent.id,
        fields={"project_cancel_operation_id": "op-synthetic-pending"},
        _project_operation="cancel",
    )

    preview = await _tool_manage_tasks({"action": "cancel_project", "task_id": parent.id})

    assert "op-synthetic-pending" in preview
    assert "reuse" in preview


async def test_parallel_native_dispatch_awaits_project_cancellation(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic async dispatch project")
    child = manager.create("Synthetic async child", fields={"parent_id": parent.id})

    result = await execute_tool_parallel(
        "manage_tasks",
        {
            "action": "cancel_project",
            "task_id": parent.id,
            "confirm": True,
            "operation_id": "op-synthetic-dispatch",
        },
    )

    assert "Project cancellation complete" in result
    assert manager.get(child.id).status == "cancelled"


async def test_native_and_http_project_start_share_live_coordinator_guard(
    native_projects, monkeypatch,
):
    manager, sessions, transcripts = native_projects
    parent = manager.create("Synthetic guarded project")
    manager.create("Synthetic child", fields={"parent_id": parent.id})
    coordinator = sessions.create(
        "synthetic-project-coordinator", status=STATUS_RUNNING, origin="operator",
    )
    manager.update(
        parent.id,
        fields={COORDINATOR_SESSION_FIELD: coordinator.session_id},
        _project_action=True,
    )

    with pytest.raises(ProjectConflictError, match="coordinator is already live"):
        _tool_manage_tasks({"action": "start_project", "task_id": parent.id})

    from api.routes import tasks as task_routes

    monkeypatch.setattr(task_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(task_routes, "_session_store", sessions)
    monkeypatch.setattr(task_routes, "_transcript_store", transcripts)
    with pytest.raises(HTTPException) as error:
        await task_routes.start_project(parent.id)
    assert error.value.status_code == 409
    assert "coordinator is already live" in error.value.detail


def test_native_plan_project_reuses_stable_operation(native_projects):
    manager, _sessions, _transcripts = native_projects
    parent = manager.create("Synthetic delegated project", tags=["local"])
    manager.create("Synthetic planning child", fields={"parent_id": parent.id})
    request = {
        "action": "plan_project",
        "task_id": parent.id,
        "operation_id": "op-synthetic-plan",
    }
    first = _tool_manage_tasks(request)
    second = _tool_manage_tasks(request)
    assert "Project planning started" in first
    assert "Project planning recovered" in second
    assert first.split("session ", 1)[1].split(" |", 1)[0] == second.split("session ", 1)[1].split(" |", 1)[0]
    listed = _tool_manage_tasks({"action": "list", "query": "Synthetic delegated project"})
    assert "Coordinator: claimed" in listed
