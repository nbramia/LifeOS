"""
Tasks API routes for LifeOS.

CRUD endpoints for tasks stored in Obsidian-compatible markdown.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from api.services import human_queue
from api.services.agent_board import AGENT_PICKUP_TAGS
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.task_manager import get_task_manager, Task, TaskConflictError, VALID_STATUSES
from api.services.task_projects import (
    COORDINATOR_SESSION_FIELD,
    ProjectConflictError,
    ProjectHandoffError,
    ProjectTaskService,
    TaskHierarchy,
    build_task_hierarchy,
    clean_parent_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Caller-asserted worker identity the MCP proxy adds to curated task create/
# update when it has one — same header name as `mcp_server.py`'s
# `AGENT_SESSION_HEADER`, same trust model as the existing `actor` and
# `fields.assigned_by` caller-asserted fields on this API: not
# cryptographically attested, just believed the way those already are.
AGENT_SESSION_HEADER = "X-LifeOS-Agent-Session"
# Tags a project child cannot carry when the write is agent-attributed.
_HERMES_TAG = "hermes"
_METERED_CHILD_TAGS = frozenset({"cloud", "cloud-haiku", "cloud-sonnet"})

# Same claim vocabulary the worker fans out on (engine assignees + Managed
# Agents consent tags). dry_run previews only when the create would be
# worker-claimable.
_AGENT_PICKUP_TAGS = frozenset(AGENT_PICKUP_TAGS)
_AGENT_CLAIM_EXCLUSION_TAGS = frozenset({
    "agent-running", "agent-blocked", "agent-completed",
    "agent-failed", "agent-budget-exceeded",
})
_AGENT_PICKUP_STATUSES = frozenset({"todo", "urgent"})

# Lazy module-level singleton, mirroring api/routes/agents.py's own
# `_get_session_store()` — needed here to answer "is there actually a live
# session behind this card's claim" before a tags write can be allowed to
# touch a claim tag (see `SessionStore.has_live_session`, called through
# `_get_session_store()` below).
_session_store: SessionStore | None = None
_transcript_store: TranscriptStore | None = None


def _get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


def _get_transcript_store() -> TranscriptStore:
    global _transcript_store
    if _transcript_store is None:
        _transcript_store = TranscriptStore()
    return _transcript_store


def _project_service(manager=None) -> ProjectTaskService:
    return ProjectTaskService(
        manager or get_task_manager(),
        _get_session_store(),
        _get_transcript_store(),
    )


def _task_response(
    manager,
    task: Task,
    *,
    hierarchy: TaskHierarchy | None = None,
    service: ProjectTaskService | None = None,
) -> "TaskResponse":
    hierarchy = hierarchy or build_task_hierarchy(manager.list_tasks())
    service = service or _project_service(manager)
    return TaskResponse.from_task(
        task,
        hierarchy.read_fields(task.id, service.coordinator_view(task)),
    )


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


def _enforce_agent_child_tag_guard(
    manager, *, parent_id: str, tags: Optional[list[str]],
) -> None:
    """Refuse #hermes and an out-of-scope paid route on an agent-attributed
    project-child write.

    Only called when the caller asserted `AGENT_SESSION_HEADER` and the task
    being written is, or would become, a project child (`parent_id` set or
    already parented). `#hermes` is refused outright — the operator assigns
    it from the board. A metered route (`#cloud`/`#cloud-haiku`/
    `#cloud-sonnet`) is refused unless the project's owner (its
    `project_coordinator_session_id`) already carries that exact route —
    mirrors the handoff handler's own metered-scope rule
    (`inter_agent.metered_target_out_of_scope`).
    """
    from api.services import agent_board
    from api.services.agent_worker.execution import parse_legacy_route_alias
    from api.services.agent_worker.inter_agent import metered_target_out_of_scope

    normalized = agent_board.normalize_tags(tags or [])
    if _HERMES_TAG in normalized:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "hermes_delegation_forbidden",
                "message": (
                    "Agents cannot assign #hermes to project children; "
                    "the operator can assign it from the board."
                ),
            },
        )
    metered_tag = next((tag for tag in normalized if tag in _METERED_CHILD_TAGS), None)
    if metered_tag is None:
        return
    alias = parse_legacy_route_alias(f"#{metered_tag}")
    target_executor = alias.request.executor if alias.recognized and alias.request else None
    if target_executor is None:
        return
    session_store = _get_session_store()
    parent = manager.get(parent_id)
    owner_session_id = (
        (parent.fields.get(COORDINATOR_SESSION_FIELD) or "").strip() if parent else ""
    )
    owner = session_store.get_by_session_id(owner_session_id) if owner_session_id else None
    if owner is None or metered_target_out_of_scope(session_store, owner, target_executor):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "api_billing_blocked",
                "message": (
                    f"agent-attributed project children cannot request metered "
                    f"route #{metered_tag} unless the project owner already "
                    "carries it"
                ),
            },
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
                    "tags the operator named. A routing tag (local/claude/"
                    "codex/hermes/cloud/cloud-haiku/cloud-sonnet) only if the "
                    "operator explicitly named that engine — these tags are "
                    "operator-authority and outrank every routing safeguard, "
                    "so inventing one injects your own engine preference at "
                    "the highest-precedence slot. On a project child from an "
                    "agent, #hermes is always refused and a paid route is "
                    "refused unless the project owner already carries it.",
    )
    reminder_id: Optional[str] = Field(default=None, description="Associated reminder ID")
    operation_key: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Stable caller-generated key for retry-safe creation. Reusing the key "
            "returns the task already created for that operation."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Multi-line notes body, stored as indented '> ' lines beneath the task line.",
    )
    fields: Optional[dict[str, Optional[str]]] = Field(
        default=None,
        description="Operator-editable inline fields (e.g. host, effort, model, "
                    "key) plus any custom [key:: value] field. Set parent_id to "
                    "an existing stable task ID to create this task as that "
                    "project's child; child links support one hierarchy level. "
                    "Round-trips untouched through any later rewrite of the task.",
    )
    dry_run: Optional[bool] = Field(
        default=False,
        description="When true and the task carries an engine assignee or "
                    "Managed Agents consent tag, run the Haiku preflight and "
                    "return the routing + cost estimate without creating the "
                    "task. Used by prompt-engineering iteration to inspect "
                    "routing decisions without dispatching a managed session. "
                    "Costs ~$0.001 for the preflight call.",
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
                    "operator named. A routing tag (local/claude/codex/"
                    "hermes/cloud/cloud-haiku/cloud-sonnet) only if the operator "
                    "explicitly named that engine — these tags are operator-"
                    "authority and outrank every routing safeguard. On a project "
                    "child from an agent, #hermes is always refused and a paid "
                    "route is refused unless the project owner already carries it.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Replaces the task's notes body (indented '> ' lines beneath the task line).",
    )
    fields: Optional[dict[str, Optional[str]]] = Field(
        default=None,
        description="Merged into the task's operator/unknown fields, not replaced: "
                    "a string value sets that field, a null value removes it. "
                    "Set parent_id to an existing stable task ID to attach or "
                    "reparent this child; set parent_id to null to detach it. "
                    "Fields not mentioned are left alone.",
    )
    actor: Optional[str] = Field(
        default=None,
        description="Caller identity for the claimed-card guard below. The only "
                    "recognized value is 'worker', asserted by the agent worker's "
                    "own lifecycle projector so its writes on a card it already "
                    "claims are not read as a human reassigning that card. Not "
                    "persisted — stripped from the patch before it reaches the "
                    "task store. Caller-asserted, not authenticated: the same "
                    "trust `fields.assigned_by` already relies on.",
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
    parent_id: Optional[str] = None
    parent_title: Optional[str] = None
    is_project: bool = False
    child_count: int = 0
    hierarchy_valid: bool = True
    hierarchy_error: Optional[str] = None
    parent_cancellation_pending: bool = False
    parent_handoff_pending: bool = False
    project: Optional[dict] = None

    @classmethod
    def from_task(cls, t: Task, hierarchy_fields: Optional[dict] = None) -> "TaskResponse":
        hierarchy_fields = hierarchy_fields or {}
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
            **hierarchy_fields,
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
async def create_task(
    request: CreateTaskRequest,
    x_lifeos_agent_session: Optional[str] = Header(default=None, alias=AGENT_SESSION_HEADER),
):
    """Create a new task, or preview its agent routing without creating it.

    When `dry_run=true` and the request carries an engine assignee or Managed
    Agents consent tag, the route runs the Haiku preflight classifier and
    returns the routing decision + cost estimate without persisting a task or
    dispatching a session. Used by prompt-engineering iteration to inspect
    routing decisions cheaply (only the preflight call costs anything,
    ~$0.001).

    Otherwise (no engine/consent tag, or `dry_run=false`), the task is created
    normally. `AGENT_SESSION_HEADER`, when present, marks this create as
    agent-attributed: a project-child create (`fields.parent_id` set) is
    refused if it carries `#hermes` or an out-of-scope paid route (see
    `_enforce_agent_child_tag_guard`), and on success the child is stamped
    with its agent origin and creator session — an ordinary operator create
    carries neither header nor stamp.
    """
    if request.dry_run and _has_agent_pickup_tag(request.tags):
        return _build_preflight_preview(request)
    _require_valid_status(request.status)
    manager = get_task_manager()
    fields = {k: v for k, v in (request.fields or {}).items() if v is not None}
    agent_session_id = (x_lifeos_agent_session or "").strip() or None
    parent_id = (fields.get("parent_id") or "").strip()
    if agent_session_id and parent_id:
        _enforce_agent_child_tag_guard(manager, parent_id=parent_id, tags=request.tags)
    child_creator_session = agent_session_id if (agent_session_id and parent_id) else None
    try:
        if request.operation_key:
            task, _created = manager.create_or_find_by_operation(
                request.operation_key,
                description=request.description,
                context=request.context or "Inbox",
                status=request.status or "todo",
                priority=request.priority or "",
                due_date=request.due_date,
                tags=request.tags,
                reminder_id=request.reminder_id,
                notes=request.notes,
                fields=fields,
                _project_child_creator_session=child_creator_session,
            )
        else:
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
                _project_child_creator_session=child_creator_session,
            )
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _task_response(manager, task)


def _has_agent_pickup_tag(tags: Optional[list[str]]) -> bool:
    if not tags:
        return False
    return any(t.lstrip("#").lower() in _AGENT_PICKUP_TAGS for t in tags)


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
        # `#cloud` — priced from the remote provider's own configured
        # rate, not the Anthropic table `cost_for` looks up. Unset rates
        # mean "unknown, not free" — the estimate floors
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
    - due_before: Filter tasks due on or before the given date (YYYY-MM-DD)
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
    all_tasks = list(manager.all_tasks_snapshot())
    hierarchy = build_task_hierarchy(all_tasks or tasks)
    service = _project_service(manager)
    return TaskListResponse(
        tasks=[
            TaskResponse.from_task(
                t,
                hierarchy.read_fields(t.id, service.coordinator_view(t)),
            )
            for t in tasks
        ],
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
# Human queue — fire-and-forget cards any agent can file/resolve for
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

    Used by the external agent worker for lifecycle tag transitions (e.g.
    `#agent-running` → `#agent-completed`). Returns `{swapped: false}` (with
    a `reason`) when the task does not exist or `from` is not currently among
    the task's tags — both indicate the worker should move on to the next
    candidate rather than retry.
    """
    manager = get_task_manager()
    if manager.get(task_id) is None:
        return SwapTagResponse(swapped=False, reason="task not found")
    try:
        ok = manager.swap_tag(task_id, from_tag, to_tag)
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return SwapTagResponse(swapped=ok, reason=None if ok else f"tag '{from_tag}' not present")


class MutateTagResponse(BaseModel):
    ok: bool
    reason: Optional[str] = None


class ClaimAgentResponse(BaseModel):
    claimed: bool
    consumed_queue_tag: bool = False
    reason: Optional[str] = None


@router.post("/{task_id}/claim-agent", response_model=ClaimAgentResponse)
async def claim_agent_task(task_id: str):
    """Atomically claim a currently eligible task for the agent worker."""
    manager = get_task_manager()
    if manager.get(task_id) is None:
        return ClaimAgentResponse(claimed=False, reason="task not found")
    try:
        claimed, consumed_queue_tag = manager.claim_for_agent(
            task_id,
            pickup_tags=set(_AGENT_PICKUP_TAGS),
            exclusion_tags=set(_AGENT_CLAIM_EXCLUSION_TAGS),
            eligible_statuses=set(_AGENT_PICKUP_STATUSES),
        )
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ClaimAgentResponse(
        claimed=claimed,
        consumed_queue_tag=consumed_queue_tag,
        reason=None if claimed else "task is no longer eligible",
    )


@router.post("/{task_id}/remove-tag", response_model=MutateTagResponse)
async def remove_tag(
    task_id: str,
    tag: str = Query(..., description="Tag to remove if present (with or without '#')"),
):
    """Atomically remove a tag when present.

    Used by the agent worker to roll back a claim by dropping `#agent-running`.
    """
    manager = get_task_manager()
    if manager.get(task_id) is None:
        return MutateTagResponse(ok=False, reason="task not found")
    try:
        ok = manager.remove_tag_if_present(task_id, tag)
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return MutateTagResponse(ok=ok, reason=None if ok else f"tag '{tag}' not present")


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get a specific task by ID."""
    manager = get_task_manager()
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(manager, task)


class TaskChildrenResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int
    limit: int
    offset: int


@router.get("/{task_id}/children", response_model=TaskChildrenResponse)
async def get_task_children(
    task_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a project's actual children by stable parent id."""
    manager = get_task_manager()
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    all_tasks = manager.list_tasks()
    hierarchy = build_task_hierarchy(all_tasks)
    children = hierarchy.children(task_id)
    page = children[offset:offset + limit]
    service = _project_service(manager)
    return TaskChildrenResponse(
        tasks=[
            TaskResponse.from_task(
                child,
                hierarchy.read_fields(child.id, service.coordinator_view(child)),
            )
            for child in page
        ],
        total=len(children),
        limit=limit,
        offset=offset,
    )


class CompleteProjectRequest(BaseModel):
    acknowledge_cancelled_children: bool = False


class ProjectOperationRequest(BaseModel):
    operation_id: str = Field(..., min_length=1, max_length=200)


class CancelProjectRequest(BaseModel):
    confirm: bool = False
    operation_id: Optional[str] = Field(default=None, min_length=1, max_length=200)


class FinalizeProjectHandoffRequest(BaseModel):
    operation_id: str = Field(..., min_length=1, max_length=128)
    source_session_id: str = Field(..., min_length=1, max_length=200)
    source_attempt_id: str = Field(..., min_length=1, max_length=200)
    source_turn_id: str = Field(..., min_length=1, max_length=200)


@router.post("/{task_id}/project/start", response_model=TaskResponse)
async def start_project(task_id: str):
    manager = get_task_manager()
    try:
        task = _project_service(manager).start_project(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _task_response(manager, task)


@router.post("/{task_id}/project/complete", response_model=TaskResponse)
async def complete_project(task_id: str, body: CompleteProjectRequest):
    manager = get_task_manager()
    try:
        task = _project_service(manager).complete_project(
            task_id,
            acknowledge_cancelled_children=body.acknowledge_cancelled_children,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(manager, task)


@router.post("/{task_id}/project/plan")
async def plan_project(task_id: str, body: ProjectOperationRequest):
    try:
        return _project_service().plan_and_delegate(task_id, operation_id=body.operation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TaskConflictError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{task_id}/project/cancel")
async def cancel_project(task_id: str, body: CancelProjectRequest):
    service = _project_service()
    try:
        if not body.confirm:
            return service.cancel_preview(task_id)
        if not body.operation_id:
            raise HTTPException(status_code=400, detail="operation_id is required when confirm=true")
        return await service.cancel_project(task_id, operation_id=body.operation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{task_id}/project/handoff/finalize")
async def finalize_project_handoff(
    task_id: str, body: FinalizeProjectHandoffRequest,
):
    """Release one staged handoff after the worker recorded exact-turn quiescence."""
    try:
        return _project_service().finalize_handoff(
            task_id,
            operation_id=body.operation_id,
            source_session_id=body.source_session_id,
            source_attempt_id=body.source_attempt_id,
            source_turn_id=body.source_turn_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectHandoffError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except (TaskConflictError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{task_id}/resume-execution", response_model=TaskResponse)
async def resume_task_execution(task_id: str):
    manager = get_task_manager()
    try:
        task = _project_service(manager).resume_execution(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _task_response(manager, task)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    x_lifeos_agent_session: Optional[str] = Header(default=None, alias=AGENT_SESSION_HEADER),
):
    """Update an existing task.

    `AGENT_SESSION_HEADER`, when present, marks this update as agent-
    attributed: a tags patch that adds `#hermes` or an out-of-scope paid
    route to a task that is, or would become, a project child is refused —
    see `_enforce_agent_child_tag_guard`. An operator write (no header)
    is unaffected.
    """
    _require_valid_status(request.status)
    manager = get_task_manager()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    actor = updates.pop("actor", None)
    agent_session_id = (x_lifeos_agent_session or "").strip() or None

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

        if agent_session_id and "tags" in updates:
            effective_parent_id = (
                clean_parent_id(fields_patch.get("parent_id"))
                if fields_patch and "parent_id" in fields_patch
                else clean_parent_id(current.fields.get("parent_id"))
            )
            if effective_parent_id:
                _enforce_agent_child_tag_guard(
                    manager, parent_id=effective_parent_id, tags=updates["tags"],
                )

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
            # never trusted from the request) — the one exception is the
            # worker-actor carve-out below, which is keyed on the request's
            # `actor` field precisely because it exists to tell the
            # worker's own write apart from everyone else's.
            old_tags = agent_board.normalize_tags(current.tags)
            new_tags = agent_board.normalize_tags(updates["tags"])
            # Managed Agents consent tags are executor assignments too. They
            # are not board assignee lanes, but removing one through a raw
            # tags PUT would silently change the worker's requested target.
            assignee_tag_set = set(agent_board.AGENT_EXECUTOR_TAGS)
            # Every lifecycle tag the worker or the accept endpoint writes
            # is off-limits to a bare tags PUT, not just the two claim
            # tags — `agent-completed` and `accepted` are just as
            # unreachable through any legitimate HTTP caller, and letting
            # either be manufactured this way would fake a Review state
            # (or a fake accept out of one) the same way a manufactured
            # claim tag fakes a claim.
            # Deliberately narrower than board.js's client-side
            # LIFECYCLE_TAGS, which also hides agent-failed/
            # agent-budget-exceeded from the Tags box: those two are
            # terminal outcomes `derive_lane`/`is_claimed` never look at,
            # so manufacturing either one through a bare tags PUT can't
            # fake a lane or a claim the way a RUNNING/BLOCKED/COMPLETED/
            # ACCEPTED tag could — there's no state here worth guarding.
            claim_tag_set = {
                agent_board.RUNNING_TAG, agent_board.BLOCKED_TAG,
                agent_board.COMPLETED_TAG, agent_board.ACCEPTED_TAG,
            }

            # The agent worker's own lifecycle projector (`LifecycleProjector`,
            # api/services/agent_worker/lifecycle.py) is the one caller allowed
            # to change a claim/lifecycle tag on a card it already claims: it
            # asserts `actor: "worker"` on a write that leaves the assignee-tag
            # set untouched, targets a card `is_claimed` already sees as
            # claimed, and carries a `status` actually changing away from the
            # one on file — recording its own session's status transition, not
            # reassigning the card. `actor` is caller-asserted, not
            # authenticated (the same trust `fields.assigned_by` already
            # relies on), so every other condition here narrows the carve-out
            # to a shape a reassignment can never take: changing the assignee
            # tag, or asserting the marker against a card that was never
            # actually claimed, still falls through to the refusals below.
            worker_lifecycle_write = (
                actor == "worker"
                and (old_tags & assignee_tag_set) == (new_tags & assignee_tag_set)
                and agent_board.is_claimed(current.status, current.tags)
                and updates.get("status") not in (None, current.status)
            )

            added_claim_tags = (new_tags & claim_tag_set) - (old_tags & claim_tag_set)
            if added_claim_tags and not worker_lifecycle_write:
                # A claim/lifecycle tag (`agent-running`/`agent-blocked`/
                # `agent-completed`/`accepted`) is added via a plain PUT only
                # by the worker's own lifecycle projector recording its
                # session's transition on a card it already claims (see the
                # carve-out above) — every other caller, and the same
                # projector write aimed at a card it does not already claim,
                # is refused unconditionally rather than only when the card
                # is already claimed: an unclaimed (including `me`) card
                # gaining one of these through this path would fake a claim
                # or a review state on the very next policy read, which is a
                # false state this endpoint must never manufacture.
                raise HTTPException(
                    status_code=agent_board.WORKER_OWNED_ERROR[0],
                    detail=agent_board.WORKER_OWNED_ERROR[1],
                )

            if (
                (old_tags & assignee_tag_set) != (new_tags & assignee_tag_set)
                or (old_tags & claim_tag_set) != (new_tags & claim_tag_set)
            ) and not worker_lifecycle_write:
                has_live = _get_session_store().has_live_session(
                    task_id, status=current.status, tags=current.tags,
                )
                error = agent_board.evaluate_card_action(
                    current.status, current.tags, "assignee_change", has_live_session=has_live,
                )
                if error is not None:
                    raise HTTPException(status_code=error[0], detail=error[1])

    # The guard above clears a board-marked field change against a read taken
    # before the write. Re-check it against the snapshot the write lands on,
    # under the store's own lock, so a card the worker claims in that window
    # cannot take the planned write. Requests without the marker never reach
    # this and pay for no extra work.
    from api.services.agent_board import CardDecisionChanged as _CardDecisionChanged

    precondition = None
    if board_marked_field_change:
        from api.services import agent_board

        def precondition(current) -> None:  # noqa: F811 — only defined on the guarded path
            fresh_error = agent_board.evaluate_card_action(
                current.status, current.tags, "field_edit",
                has_live_session=_get_session_store().has_live_session(
                    task_id, status=current.status, tags=current.tags,
                ),
            )
            if fresh_error is not None:
                raise agent_board.CardDecisionChanged(fresh_error)

    try:
        task = manager.update(task_id, _precondition=precondition, **updates)
    except _CardDecisionChanged as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(manager, task)


@router.put("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str):
    """Mark a task as done (shortcut endpoint)."""
    manager = get_task_manager()
    try:
        task = manager.complete(task_id)
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(manager, task)


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """Delete a task."""
    manager = get_task_manager()
    try:
        deleted = manager.delete(task_id)
    except ProjectConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except TaskConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        _get_session_store().purge_task(task_id)
    except Exception:
        # The operator's delete already succeeded against the task store;
        # the worker's own bookkeeping is a separate database and its
        # unavailability must not turn a successful delete into an error.
        logger.warning("purge_task failed for %s", task_id, exc_info=True)
    return {"status": "deleted", "id": task_id}
