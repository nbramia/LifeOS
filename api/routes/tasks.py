"""
Tasks API routes for LifeOS.

CRUD endpoints for tasks stored in Obsidian-compatible markdown.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.task_manager import get_task_manager, Task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    description: str = Field(..., min_length=1, description="Task description")
    context: str = Field(default="Inbox", description="Task context/category (default: Inbox)")
    priority: Optional[str] = Field(default="", description="Priority: high, medium, low, or empty")
    due_date: Optional[str] = Field(default=None, description="Due date (YYYY-MM-DD)")
    tags: Optional[list[str]] = Field(default=None, description="List of tags (e.g., ['work', 'urgent'])")
    reminder_id: Optional[str] = Field(default=None, description="Associated reminder ID")


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

@router.post("", response_model=TaskResponse)
async def create_task(request: CreateTaskRequest):
    """Create a new task."""
    manager = get_task_manager()
    task = manager.create(
        description=request.description,
        context=request.context,
        priority=request.priority or "",
        due_date=request.due_date,
        tags=request.tags,
        reminder_id=request.reminder_id,
    )
    return TaskResponse.from_task(task)


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
