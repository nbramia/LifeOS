"""
Tasks API routes for LifeOS.

CRUD endpoints for tasks stored in Obsidian-compatible markdown.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.services import human_queue
from api.services.agent_worker.session_store import SessionStore
from api.services.task_manager import get_task_manager, Task, TaskConflictError, VALID_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Lazy module-level singleton, mirroring api/routes/agents.py's own
# `_get_session_store()` — needed here to answer "is there actually a live
# session behind this card's claim" before a tags write can be allowed to
# touch a claim tag (see `_has_live_session` below).
_session_store: SessionStore | None = None


def _get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


def _require_valid_status(status: Optional[str]) -> None:
    """Raise 422 for an unrecognized status.

    Deliberately a manual check + HTTPException rather than a pydantic
    `field_validator` — the app's global `RequestValidationError` handler
    (api/main.py) converts every pydantic validation failure to 400, and (as
    a separate, pre-existing bug) can't JSON-serialize a validator's raised
    exception object at all. Matches the pattern already used for schedules'
    bot-name validation (api/routes/scheduler.py `_require_known_bot`)."""
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    description: str = Field(..., min_length=1, description="Task description")
    context: Optional[str] = Field(default=None, description="Context/category; defaults to 'Inbox'.")
    status: Optional[str] = Field(
        default=None,
        description=(
            "Initial status; defaults to 'todo'. One of: todo, done, in_progress, "
            "cancelled, deferred, blocked, urgent."
        ),
    )
    priority: Optional[str] = Field(default="", description="Priority: high, medium, low, or empty")
    due_date: Optional[str] = Field(default=None, description="Due date (YYYY-MM-DD)")
    tags: Optional[list[str]] = Field(
        default=None,
        description="List of tags (e.g., ['work', 'urgent']). Add exactly the "
                    "tags the operator named. A routing tag (agent/local/claude/"
                    "codex/cloud/cloud-haiku/cloud-sonnet) only if the operator "
                    "explicitly named that engine — these tags are operator-"
                    "authority and outrank every routing safeguard, so inventing "
                    "one injects your own engine preference at the highest-"
                    "precedence slot.",
    )
    reminder_id: Optional[str] = Field(default=None, description="Associated reminder ID")
    notes: Optional[str] = Field(
        default=None,
        description="Multi-line notes body, stored as indented '> ' lines beneath the task line.",
    )
    fields: Optional[dict[str, Optional[str]]] = Field(
        default=None,
        description="Operator-editable inline fields (e.g. host, effort, model, "
                    "key) plus any custom [key:: value] field. Round-trips "
                    "untouched through any later rewrite of the task.",
    )
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
    tags: Optional[list[str]] = Field(
        default=None,
        description="Replaces the task's tag list. Add exactly the tags the "
                    "operator named. A routing tag (agent/local/claude/codex/"
                    "cloud/cloud-haiku/cloud-sonnet) only if the operator "
                    "explicitly named that engine — these tags are operator-"
                    "authority and outrank every routing safeguard.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Replaces the task's notes body (indented '> ' lines beneath the task line).",
    )
    fields: Optional[dict[str, Optional[str]]] = Field(
        default=None,
        description="Merged into the task's operator/unknown fields, not replaced: "
                    "a string value sets that field, a null value removes it. "
                    "Fields not mentioned are left alone.",
    )


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
    updated_at: Optional[str]
    tags: list[str]
    reminder_id: Optional[str]
    notes: Optional[str]
    fields: dict[str, str]
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
            updated_at=t.updated_at,
            tags=t.tags,
            reminder_id=t.reminder_id,
            notes=t.notes,
            fields=t.fields,
            source_file=t.source_file,
            line_number=t.line_number,
        )


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


class ConflictFile(BaseModel):
    name: str
    mtime: str


class ConflictListResponse(BaseModel):
    conflicts: list[ConflictFile]


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
    _require_valid_status(request.status)
    manager = get_task_manager()
    fields = {k: v for k, v in (request.fields or {}).items() if v is not None}
    try:
        task = manager.create(
            description=request.description,
            context=request.context or "Inbox",
            status=request.status or "todo",
            priority=request.priority or "",
            due_date=request.due_date,
            tags=request.tags,
            reminder_id=request.reminder_id,
            notes=request.notes,
            fields=fields,
        )
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
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
        ROUTE_REMOTE,
        run_preflight,
    )
    from api.services.agent_worker.pricing import cost_for, MANAGED_SESSION_HOUR_OVERHEAD
    from config.settings import settings

    pre = run_preflight(request.description, tags=request.tags or [])
    # Worst-case estimate: assume budget.max_tokens is fully consumed, split
    # 50/50 between input and output. Excludes cache_creation because dry_run
    # can't know preset size; this is a floor, not a calibrated estimate.
    half_tokens = max(0, pre.budget.max_tokens // 2)
    if pre.routing == ROUTE_REMOTE:
        # (#809) `#cloud` — priced from the remote provider's own configured
        # rate, not the Anthropic table `cost_for` looks up. Unset rates
        # mean "unknown, not free" (#669's convention) — the estimate floors
        # at 0 rather than guessing, same as an unrecognized model would.
        input_price = settings.remote_llm_input_price_per_mtok
        output_price = settings.remote_llm_output_price_per_mtok
        if input_price is not None and output_price is not None:
            estimated = (
                (half_tokens / 1_000_000) * input_price
                + (half_tokens / 1_000_000) * output_price
            )
        else:
            estimated = 0.0
    else:
        model = (
            settings.agent_managed_model_for_tests or settings.agent_managed_model
            if pre.routing == ROUTE_CLAUDE
            else "local"
        )
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
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by status, matched exactly and case-sensitively. Valid values: "
            "todo, done, in_progress, cancelled, deferred, blocked, urgent. Omit to "
            "return every status — in an established vault most tasks are done or "
            "cancelled, so pass status='todo' for open/outstanding work."
        ),
    ),
    context: Optional[str] = Query(
        None,
        description=(
            "Filter by context, matched exactly but case-insensitively. Contexts are "
            "vault-defined (one markdown file each) and default to 'Inbox'; there is "
            "no fixed set. A context that is not in use returns zero tasks, so omit "
            "this filter unless you know the value exists."
        ),
    ),
    tag: Optional[str] = Query(
        None,
        description=(
            "Filter by tag, case-insensitive, with or without a leading '#'."
        ),
    ),
    due_before: Optional[str] = Query(
        None,
        description=(
            "Only tasks whose due date is on or before this date (YYYY-MM-DD). "
            "Tasks with no due date are excluded."
        ),
    ),
    query: Optional[str] = Query(
        None,
        description=(
            "Fuzzy text search over task descriptions (e.g. 'taxes' matches '1099')."
        ),
    ),
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


@router.get("/conflicts", response_model=ConflictListResponse)
async def list_conflicts():
    """List Syncthing conflict copies / in-progress temp files sitting in the
    tasks folder. These are never indexed as tasks and never reindexed —
    surfaced here so a client (the board) can warn the operator to resolve
    them by hand in Obsidian/Syncthing.

    Registered before `/{task_id}` so FastAPI doesn't treat "conflicts" as
    a task id.
    """
    manager = get_task_manager()
    return ConflictListResponse(conflicts=[ConflictFile(**c) for c in manager.list_conflicts()])


# ---------------------------------------------------------------------------
# Human queue (#852) — fire-and-forget cards any agent can file/resolve for
# the operator. Business logic (dedupe, done_when validation, card shape)
# lives in api/services/human_queue.py, shared with the native chat tool and
# the briefing line. Registered before /{task_id} — see the module comment
# above "Routes (static paths MUST come before {id})".
# ---------------------------------------------------------------------------

class HumanQueueAddRequest(BaseModel):
    title: str = Field(..., min_length=1, description="Card title.")
    notes: Optional[str] = Field(default=None, description="Notes body.")
    key: Optional[str] = Field(
        default=None,
        description="Dedupe key. Filing with an existing OPEN card's key updates "
                    "its notes instead of creating a duplicate.",
    )
    done_when: Optional[dict] = Field(
        default=None,
        description="Auto-resolve check: {type: 'endpoint', path, pointer, equals} "
                    "or {type: 'file_exists', path}.",
    )
    source_host: Optional[str] = Field(default=None, description="Filing session's hostname.")
    source_cwd: Optional[str] = Field(default=None, description="Filing session's working directory.")
    source_session: Optional[str] = Field(default=None, description="Filing session's id, if any.")


class HumanQueueAddResponse(BaseModel):
    id: str


class HumanQueueCard(BaseModel):
    id: str
    title: str
    key: Optional[str] = None
    notes: Optional[str] = None
    age_hours: Optional[float] = None
    source_host: Optional[str] = None
    source_cwd: Optional[str] = None
    source_session: Optional[str] = None
    done_when: Optional[dict] = None


class HumanQueueListResponse(BaseModel):
    cards: list[HumanQueueCard]
    total: int


class HumanQueueResolveRequest(BaseModel):
    note: Optional[str] = Field(default=None, description="Resolution note, appended to the card's notes.")


class HumanQueueResolveResponse(BaseModel):
    id: str
    status: str = "done"


@router.post("/human-queue", response_model=HumanQueueAddResponse)
async def add_human_queue_card(request: HumanQueueAddRequest):
    """File a human-queue card (status blocked, tag human). Filing with an
    existing open `key` updates that card's notes instead of duplicating it.
    """
    try:
        task = human_queue.add_card(
            title=request.title,
            notes=request.notes,
            key=request.key,
            done_when=request.done_when,
            source_host=request.source_host,
            source_cwd=request.source_cwd,
            source_session=request.source_session,
        )
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (human_queue.DoneWhenError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return HumanQueueAddResponse(id=task.id)


@router.get("/human-queue", response_model=HumanQueueListResponse)
async def list_human_queue_cards():
    """List open human-queue cards (status blocked, tag human)."""
    cards = [HumanQueueCard(**c) for c in human_queue.list_open_cards()]
    return HumanQueueListResponse(cards=cards, total=len(cards))


@router.put("/human-queue/{id_or_key}/resolve", response_model=HumanQueueResolveResponse)
async def resolve_human_queue_card(id_or_key: str, request: HumanQueueResolveRequest):
    """Mark an open human-queue card done, by task id or dedupe key."""
    try:
        task = human_queue.resolve_card(id_or_key, note=request.note)
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if task is None:
        raise HTTPException(status_code=404, detail="Human-queue card not found")
    return HumanQueueResolveResponse(id=task.id)


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
    try:
        ok = manager.swap_tag(task_id, from_tag, to_tag)
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
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
    _require_valid_status(request.status)
    manager = get_task_manager()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}

    # The board's assignment pickers stamp `fields.assigned_by: "board"` on
    # every write (see web/agents/assignment.js) — that marker routes a
    # model/effort/host change through the same claimed-card guard the
    # lane endpoint enforces, so those fields can't be changed through the
    # drawer while dragging the card is refused. A request without the
    # marker (agent-side or vault-side writes) never pays for the extra
    # task read for this check.
    fields_patch = updates.get("fields")
    is_board_marked = bool(
        fields_patch and str(fields_patch.get("assigned_by", "")).strip().lower() == "board"
    )
    # A board-marked `status` patch reaches the same claimed-card lock-down
    # as model/effort/host — the board itself never sends one this way
    # (lane moves go through the lane endpoint), but nothing stops another
    # caller stamping the marker onto a raw status write, and a claimed
    # card's status is exactly what the worker owns while it's running.
    board_marked_field_change = is_board_marked and (
        bool({"model", "effort", "host"} & set((fields_patch or {}).keys()))
        or "status" in updates
    )
    needs_current = "tags" in updates or board_marked_field_change
    current = manager.get(task_id) if needs_current else None
    if needs_current and current is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if current is not None:
        from api.services import agent_board

        if board_marked_field_change:
            has_live = _get_session_store().has_live_session(
                task_id, status=current.status, tags=current.tags,
            )
            error = agent_board.evaluate_card_action(
                current.status, current.tags, "field_edit", has_live_session=has_live,
            )
            if error is not None:
                raise HTTPException(status_code=error[0], detail=error[1])

        if "tags" in updates:
            # Compare the normalized assignee-tag *and* claim-tag SETS,
            # not just `derive_assignee`'s single first-match-wins value —
            # that value is blind to two holes a free-text tags patch can
            # otherwise slip through on a claimed card:
            #   - adding a SECOND assignee tag alongside the existing one
            #     (`["claude","agent-running"]` -> `["claude",
            #     "agent-running","codex"]`) still derives "claude" (first
            #     match in ASSIGNEE_TAGS order), so a single-value
            #     comparison would see no change and never run the guard.
            #   - dropping `agent-running`/`agent-blocked` from the tags
            #     box also leaves the derived assignee unchanged, so the
            #     same blind spot would let a claimed card's claim tag be
            #     silently stripped through this path.
            # This check runs on every tags patch regardless of the
            # `assigned_by` marker — the marker only gates the
            # model/effort/host check above, since the drawer's Tags field
            # writes a bare `{"tags": [...]}` patch with no `fields` key at
            # all, so a marker-gated guard here could never fire for the
            # request the product actually sends. The guard keys on the
            # card's own claim state (computed from ITS OWN tags/status,
            # never trusted from the request), not on who's asking.
            old_tags = agent_board.normalize_tags(current.tags)
            new_tags = agent_board.normalize_tags(updates["tags"])
            assignee_tag_set = set(agent_board.ASSIGNEE_TAGS)
            # Every lifecycle tag the worker or the accept endpoint writes
            # is off-limits to a bare tags PUT, not just the two claim
            # tags — `agent-completed` and `accepted` are just as
            # unreachable through any legitimate HTTP caller, and letting
            # either be manufactured this way would fake a Review state
            # (or a fake accept out of one) the same way a manufactured
            # claim tag fakes a claim.
            claim_tag_set = {
                agent_board.RUNNING_TAG, agent_board.BLOCKED_TAG,
                agent_board.COMPLETED_TAG, agent_board.ACCEPTED_TAG,
            }

            added_claim_tags = (new_tags & claim_tag_set) - (old_tags & claim_tag_set)
            if added_claim_tags:
                # A claim/lifecycle tag (`agent-running`/`agent-blocked`/
                # `agent-completed`/`accepted`) is written only by the
                # worker (through `/swap-tag`) or the accept endpoint — no
                # other HTTP caller in this codebase adds one via a plain
                # PUT. Refuse it here unconditionally rather than only when
                # the card is already claimed: an unclaimed (including
                # `me`) card gaining one of these through this path would
                # fake a claim or a review state on the very next policy
                # read, which is a false state this endpoint must never
                # manufacture.
                raise HTTPException(
                    status_code=agent_board.WORKER_OWNED_ERROR[0],
                    detail=agent_board.WORKER_OWNED_ERROR[1],
                )

            if (
                (old_tags & assignee_tag_set) != (new_tags & assignee_tag_set)
                or (old_tags & claim_tag_set) != (new_tags & claim_tag_set)
            ):
                has_live = _get_session_store().has_live_session(
                    task_id, status=current.status, tags=current.tags,
                )
                error = agent_board.evaluate_card_action(
                    current.status, current.tags, "assignee_change", has_live_session=has_live,
                )
                if error is not None:
                    raise HTTPException(status_code=error[0], detail=error[1])

    try:
        task = manager.update(task_id, **updates)
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.from_task(task)


@router.put("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str):
    """Mark a task as done (shortcut endpoint)."""
    manager = get_task_manager()
    try:
        task = manager.complete(task_id)
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.from_task(task)


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """Delete a task."""
    manager = get_task_manager()
    try:
        deleted = manager.delete(task_id)
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted", "id": task_id}
