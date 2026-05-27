"""
Tasks API routes for LifeOS.

CRUD endpoints for tasks stored in Obsidian-compatible markdown.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.services.task_manager import get_task_manager, Task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    description: str = Field(..., min_length=1, description="Task description")
    priority: Optional[str] = Field(default="", description="Priority: high, medium, low, or empty")
    due_date: Optional[str] = Field(default=None, description="Due date (YYYY-MM-DD)")
    tags: Optional[list[str]] = Field(default=None, description="List of tags (e.g., ['work', 'urgent'])")
    reminder_id: Optional[str] = Field(default=None, description="Associated reminder ID")
    dry_run: Optional[bool] = Field(
        default=False,
        description="When true and the task carries the #agent tag, run the "
                    "Haiku preflight and return the routing + cost estimate "
                    "without creating the task. Used by prompt-engineering "
                    "iteration to inspect routing decisions without dispatching "
                    "a managed session. Costs ~$0.001 for the preflight call.",
    )


class PreflightPreviewResponse(BaseModel):
    """Response shape when `dry_run=true` is supplied to task creation.

    Returns the preflight routing + budget + cost estimate without creating
    the task. Used by dev iteration to inspect routing decisions cheaply.
    """
    dry_run: bool = True
    routing: str = Field(description="Routing decision: local / claude / ask")
    routing_reason: str
    expected_output: str
    budget: dict
    estimated_dollars: float = Field(
        description="Cost estimate for the routed session (model token cost "
                    "given budget.max_tokens, plus session-hour overhead for "
                    "managed sessions). Excludes the preflight call cost."
    )
    sane: bool
    sane_reason: str
    ambiguity: Optional[dict] = None


class UpdateTaskRequest(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = None
    context: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    tags: Optional[list[str]] = None


class TaskResponse(BaseModel):
    id: str
    description: str
    status: str
    context: str
    priority: str
    due_date: Optional[str]
    created_date: str
    done_date: Optional[str]
    cancelled_date: Optional[str]
    tags: list[str]
    reminder_id: Optional[str]
    source_file: str
    line_number: int

    @classmethod
    def from_task(cls, t: Task) -> "TaskResponse":
        return cls(
            id=t.id,
            description=t.description,
            status=t.status,
            context=t.context,
            priority=t.priority,
            due_date=t.due_date,
            created_date=t.created_date,
            done_date=t.done_date,
            cancelled_date=t.cancelled_date,
            tags=t.tags,
            reminder_id=t.reminder_id,
            source_file=t.source_file,
            line_number=t.line_number,
        )


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


# ---------------------------------------------------------------------------
# Routes (static paths MUST come before {id} to avoid capture)
# ---------------------------------------------------------------------------

@router.post("")
async def create_task(request: CreateTaskRequest):
    """Create a new task, or preview its agent routing without creating it.

    When `dry_run=true` and the request carries the #agent tag, the route runs
    the Haiku preflight classifier and returns the routing decision + cost
    estimate without persisting a task or dispatching a session. Used by
    prompt-engineering iteration to inspect routing decisions cheaply
    (only the preflight call costs anything, ~$0.001).

    For non-#agent tasks (or `dry_run=false`), the task is created normally.
    """
    if request.dry_run and _has_agent_tag(request.tags):
        return _build_preflight_preview(request)
    manager = get_task_manager()
    task = manager.create(
        description=request.description,
        priority=request.priority or "",
        due_date=request.due_date,
        tags=request.tags,
        reminder_id=request.reminder_id,
    )
    return TaskResponse.from_task(task)


def _has_agent_tag(tags: Optional[list[str]]) -> bool:
    if not tags:
        return False
    return any(t.lstrip("#").lower() == "agent" for t in tags)


def _build_preflight_preview(request: CreateTaskRequest) -> PreflightPreviewResponse:
    """Run preflight and synthesize a cost estimate without dispatching."""
    # Imports kept local so the route module stays import-cheap for non-agent
    # task operations; agent_worker pulls in the LLM client.
    from api.services.agent_worker.preflight import (
        ROUTE_CLAUDE,
        run_preflight,
    )
    from api.services.agent_worker.pricing import cost_for, MANAGED_SESSION_HOUR_OVERHEAD
    from config.settings import settings

    pre = run_preflight(request.description, tags=request.tags or [])
    # Worst-case estimate: assume budget.max_tokens is fully consumed, split
    # 50/50 between input and output. Excludes cache_creation because dry_run
    # can't know preset size; this is a floor, not a calibrated estimate.
    model = (
        settings.agent_managed_model_for_tests or settings.agent_managed_model
        if pre.routing == ROUTE_CLAUDE
        else "local"
    )
    half_tokens = max(0, pre.budget.max_tokens // 2)
    estimated = cost_for(model, half_tokens, half_tokens)
    if pre.routing == ROUTE_CLAUDE:
        estimated += (pre.budget.wall_seconds / 3600.0) * MANAGED_SESSION_HOUR_OVERHEAD
    return PreflightPreviewResponse(
        routing=pre.routing,
        routing_reason=pre.routing_reason,
        expected_output=pre.expected_output,
        budget={
            "wall_seconds": pre.budget.wall_seconds,
            "max_tokens": pre.budget.max_tokens,
            "max_dollars": pre.budget.max_dollars,
        },
        estimated_dollars=round(estimated, 4),
        sane=pre.sane,
        sane_reason=pre.sane_reason,
        ambiguity={"question": pre.ambiguity.question} if pre.ambiguity else None,
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[str] = None,
    context: Optional[str] = None,
    tag: Optional[str] = None,
    due_before: Optional[str] = None,
    query: Optional[str] = None,
):
    """
    List and filter tasks.

    Query parameters:
    - status: Filter by status (todo, done, in_progress, cancelled, deferred, blocked, urgent)
    - context: Filter by context/category
    - tag: Filter by tag (with or without '#')
    - due_before: Filter tasks due before this date (YYYY-MM-DD)
    - query: Fuzzy text search on description
    """
    manager = get_task_manager()
    tasks = manager.list_tasks(
        status=status,
        context=context,
        tag=tag,
        due_before=due_before,
        query=query,
    )
    return TaskListResponse(
        tasks=[TaskResponse.from_task(t) for t in tasks],
        total=len(tasks),
    )


class TagUsage(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    tags: list[TagUsage]


@router.get("/tags", response_model=TagListResponse)
async def list_tags():
    """List all distinct tags across all tasks (any status) with usage counts."""
    manager = get_task_manager()
    return TagListResponse(tags=[TagUsage(**t) for t in manager.list_tags()])


class SwapTagResponse(BaseModel):
    swapped: bool
    reason: Optional[str] = None


@router.post("/{task_id}/swap-tag", response_model=SwapTagResponse)
async def swap_tag(
    task_id: str,
    from_tag: str = Query(..., alias="from", description="Tag to remove (with or without '#')"),
    to_tag: str = Query(..., alias="to", description="Tag to add in its place"),
):
    """Atomically replace one tag with another on a task.

    Used by the external agent worker to claim `#agent` tasks. Returns
    `{swapped: false}` (with a `reason`) when the task does not exist or
    `from` is not currently among the task's tags — both indicate the worker
    should move on to the next candidate rather than retry.
    """
    manager = get_task_manager()
    if manager.get(task_id) is None:
        return SwapTagResponse(swapped=False, reason="task not found")
    ok = manager.swap_tag(task_id, from_tag, to_tag)
    return SwapTagResponse(swapped=ok, reason=None if ok else f"tag '{from_tag}' not present")


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get a specific task by ID."""
    manager = get_task_manager()
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.from_task(task)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, request: UpdateTaskRequest):
    """Update an existing task."""
    manager = get_task_manager()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    task = manager.update(task_id, **updates)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.from_task(task)


@router.put("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str):
    """Mark a task as done (shortcut endpoint)."""
    manager = get_task_manager()
    task = manager.complete(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.from_task(task)


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """Delete a task."""
    manager = get_task_manager()
    deleted = manager.delete(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted", "id": task_id}
