"""Kanban board view-model helpers for `/agents`.

Pure functions only — no I/O, no vault or scheduler access — so lane
derivation and lane-move planning can be unit-tested exhaustively against the
lane table without a TaskManager or SchedulerStore fixture.
`api/routes/agents.py` wires these against the real stores and stays thin:
it reads a task/schedule entry, calls into this module for the *decision*,
then performs the write. See docs/specs/technical/agent-viz.md.

Lanes are derived from task status + tags on every read — there is no stored
lane field anywhere in the vault or the task index.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

# Tags the agent worker itself writes as it drives a task through its
# lifecycle — see RUNNING_TAG / COMPLETED_TAG / BLOCKED_TAG in
# api/services/agent_worker/worker.py. Mirrored here (not imported) so this
# module stays import-light — the board treats them as opaque strings, not
# worker internals.
RUNNING_TAG = "agent-running"
COMPLETED_TAG = "agent-completed"
BLOCKED_TAG = "agent-blocked"
REASSIGNED_TAG = "agent-reassigned"
MACHINE_WAIT_TAGS = frozenset({"agent-wait-provider", "agent-wait-dependency"})

# A `#human` card is filed for the operator directly (not by the worker).
HUMAN_TAG = "human"

# The accepted marker for Review -> Done — a tag, not a new status
# symbol.
ACCEPTED_TAG = "accepted"

# Tags whose ownership/lifecycle is outside the board's free-text Tags field.
# Board tag edits replace only the user-editable portion and preserve every
# member of this set from the latest CAS snapshot.
PROTECTED_TAGS: frozenset[str] = frozenset({
    "me", "claude", "codex", "hermes", "local", "cloud",
    "cloud-haiku", "cloud-sonnet",
    RUNNING_TAG, BLOCKED_TAG, COMPLETED_TAG, "agent-failed",
    "agent-budget-exceeded", REASSIGNED_TAG, ACCEPTED_TAG,
})

# Assignee is exactly one tag from this set. "me" is the operator; the rest
# are agent engines. An engine assignee on a todo/urgent task is what makes
# it claimable by the worker — see agent-worker claim pickup.
ASSIGNEE_TAGS: tuple[str, ...] = ("me", "claude", "codex", "hermes", "local", "cloud")
AGENT_ASSIGNEES: tuple[str, ...] = ("claude", "codex", "hermes", "local", "cloud")
# Execution-capable sub-tags are not board lanes, but they are valid explicit
# task/schedule handoffs. Keep this shared with the worker, task API, and
# bounded capture consumers so a renamed route cannot leave a stale local
# allowlist that silently grants or drops execution authority.
MANAGED_AGENT_ASSIGNEES: tuple[str, ...] = ("cloud-haiku", "cloud-sonnet")
AGENT_EXECUTOR_TAGS: tuple[str, ...] = (*AGENT_ASSIGNEES, *MANAGED_AGENT_ASSIGNEES)
AGENT_PICKUP_TAGS: tuple[str, ...] = ("agent", *AGENT_EXECUTOR_TAGS)

LANES: tuple[str, ...] = (
    "unassigned",
    "assigned",
    "in_progress",
    "human_queue",
    "scheduled",
    "review",
    "done",
    "snoozed",
)

# Lanes a task can derive into (excludes "scheduled", which only ever holds
# scheduler entries).
TASK_LANES: tuple[str, ...] = tuple(lane for lane in LANES if lane != "scheduled")

# The custom `[snoozed_until:: <ISO-8601 with offset>]` task field that
# drives the Snoozed lane — a wake-up time and nothing else; status and
# tags are left untouched by a snooze.
SNOOZED_UNTIL_FIELD = "snoozed_until"

# The natural lanes (status/tags alone, ignoring any snooze) a snooze is
# ever allowed to override. A card whose natural lane is In progress or
# Done is never shown as Snoozed, no matter what `snoozed_until` says —
# the operator's intent is "set aside a dormant card", not "hide a
# running or finished one". `snooze_board_card`'s eligibility check and
# `TaskManager`'s write-time clearing (see task_manager.py) both key off
# this same set so the three stay in lockstep.
SNOOZABLE_LANES: frozenset[str] = frozenset({"unassigned", "assigned", "human_queue", "review"})


def parse_snoozed_until(value: Optional[str]) -> Optional[datetime]:
    """Parse a `snoozed_until` field value into an aware UTC datetime.

    Returns None for a missing, unparseable, or offset-less value. An
    offset-less ISO-8601 string parses successfully but carries no
    timezone, and a naive local time must never be treated as a wake time —
    the API layer rejects such a value outright rather than guessing a zone.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_snoozed(fields: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True while `fields[SNOOZED_UNTIL_FIELD]` parses to a moment after `now`.

    `now` defaults to the real current time; callers that need a fixed
    instant (tests, and the board's own stream tick — see
    `api/routes/agents.py`) pass one explicitly so the same task derives
    differently on either side of its wake-up time without a sleep. A past
    or missing wake-up time returns False and is otherwise ignored. The
    background snooze notifier removes an expired value after it successfully
    delivers the wake-up message.
    """
    until = parse_snoozed_until((fields or {}).get(SNOOZED_UNTIL_FIELD))
    if until is None:
        return False
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return until > current


def _norm_tags(tags: Iterable[str]) -> set[str]:
    return {str(t).lstrip("#").lower() for t in (tags or [])}


def normalize_tags(tags: Iterable[str]) -> set[str]:
    """Public wrapper around the internal tag normalization above — `#`
    stripped, lowercased. Exposed for write paths outside this module (e.g.
    `api/routes/tasks.py`'s claimed-card tags guard) that need to compare
    raw tag *sets* themselves rather than only a single derived value like
    `derive_assignee`'s first-match-wins result, which a second assignee
    tag or a dropped claim tag can slip past unnoticed.
    """
    return _norm_tags(tags)


def derive_assignee(tags: Iterable[str]) -> Optional[str]:
    """Return the single assignee tag on `tags`, or None.

    First match (in `ASSIGNEE_TAGS` order) wins if a task somehow carries
    more than one — the lane-move endpoint always replaces, never adds, an
    assignee tag, so this should not happen in practice.
    """
    tset = _norm_tags(tags)
    for a in ASSIGNEE_TAGS:
        if a in tset:
            return a
    return None


def natural_lane(status: str, tags: Iterable[str]) -> str:
    """Derive a task's board lane from status + tags alone, ignoring any
    snooze — what `derive_lane` would return if the task carried no
    `snoozed_until` field at all.

    Priority (highest first), and why:
      1. Review — an `agent-completed` tag without `accepted` wins over
         everything else, INCLUDING a terminal status, so a task the worker
         marked done still surfaces for the operator's accept/reject instead
         of silently landing in Done.
      2. Human queue — an agent question (`agent-blocked`), an operator-filed
         `#human` card, or a manually-blocked status all mean "needs a human
         right now"; this must win over In progress / Done so a blocked task
         is never hidden behind a stale status.
      3. In progress — status `in_progress`, or the worker's `agent-running`
         tag.
      4. Done — status `done` or `cancelled`.
      5. Assigned — an assignee tag is present but the task isn't yet in any
         of the working lanes above.
      6. Unassigned — the default: an open task with no assignee tag.
    """
    tset = _norm_tags(tags)
    status_norm = (status or "todo").lower()

    if COMPLETED_TAG in tset and ACCEPTED_TAG not in tset:
        return "review"
    # Provider/dependency waits are machine-owned and remain In Progress. The
    # wait-reason badge is derived from these tags; Human Queue is reserved for
    # operator cards/questions and legacy untyped blocked tasks.
    if tset & MACHINE_WAIT_TAGS:
        return "in_progress"
    if BLOCKED_TAG in tset or HUMAN_TAG in tset or status_norm == "blocked":
        return "human_queue"
    if status_norm == "in_progress" or RUNNING_TAG in tset:
        return "in_progress"
    if status_norm in ("done", "cancelled"):
        return "done"
    if derive_assignee(tset) is not None:
        return "assigned"
    return "unassigned"


def derive_lane(
    status: str,
    tags: Iterable[str],
    fields: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> str:
    """Derive a task's board lane from its status + tags (+ snooze field).

    Never stored — recomputed on every read from the task's current
    status/tags/fields. Computes the `natural_lane` (see above) first, then
    overrides it with `snoozed` only when that natural lane is itself
    snooze-eligible (`SNOOZABLE_LANES` — Unassigned, Assigned, Human queue,
    or Review) AND the wake-up time is still in the future. A card whose
    natural lane is In progress or Done is never shown as Snoozed, even
    with a future `snoozed_until` still on it — a running or finished card
    must never be hidden. In practice a stale future value shouldn't
    survive onto such a card anyway: `TaskManager`'s write path clears it
    the moment a write's own status/tags land the task in an unsnoozable
    natural lane (see task_manager.py), and every board write path that
    transitions a card out of a snooze-eligible lane (lane move, Accept,
    Cancel, Reject, Reassign) clears it too. This override rule is what
    keeps derivation correct even if some future write path forgets to.
    A past or missing `snoozed_until` is ignored, and derivation is exactly
    the natural lane.
    """
    lane = natural_lane(status, tags)
    if lane in SNOOZABLE_LANES and is_snoozed(fields, now):
        return "snoozed"
    return lane


WORKER_OWNED_ERROR: tuple[int, str] = (
    409,
    "the worker owns this task while it is running or waiting on an "
    "answer — answer or kill the session first",
)
REVIEW_ERROR: tuple[int, str] = (409, "accept the review first")
AGENT_ONLY_CLAIM_ERROR: tuple[int, str] = (409, "only the worker claims agent-assigned tasks")
AGENT_OWNED_MANAGED_ERROR: tuple[int, str] = (
    409,
    "agent-owned cards are managed by the agent — reassign, unassign, or "
    "cancel this card instead",
)
REVIEW_UNRESOLVED_ERROR: tuple[int, str] = (409, "accept or reject the review")
CANCEL_NOT_AGENT_OWNED_ERROR: tuple[int, str] = (
    409,
    "cancel is only available for agent-assigned cards",
)
CANCEL_ALREADY_FINISHED_ERROR: tuple[int, str] = (
    409,
    "this card is already finished — nothing to cancel",
)
PROJECT_EXPLICIT_ACTION_ERROR: tuple[int, str] = (
    409,
    "projects use Start, Complete project, and Cancel project actions",
)
PROJECT_COORDINATOR_LIVE_ERROR: tuple[int, str] = (409, "project coordinator is live")
PROJECT_CANCELLATION_PENDING_ERROR: tuple[int, str] = (409, "project cancellation is pending")
PROJECT_HIERARCHY_INVALID_ERROR: tuple[int, str] = (409, "task hierarchy is invalid; repair the parent link first")
SNOOZE_INELIGIBLE_ERROR: tuple[int, str] = (
    409,
    "only Unassigned, Assigned, Human queue, and Review cards can be snoozed",
)
SNOOZE_UNTIL_INVALID_ERROR: tuple[int, str] = (
    400,
    "until must be an ISO-8601 timestamp with a UTC offset, in the future",
)

class CardDecisionChanged(Exception):
    """A guarded write's decision does not hold for the state being written.

    Raised from a `TaskManager.update` precondition, which runs against the
    exact snapshot the write will land on. It carries the refusal the fresh
    evaluation produced, so the caller reports the real reason — that the card
    is worker-owned, say — rather than a generic conflict.
    """

    def __init__(self, error: tuple[int, str]) -> None:
        super().__init__(error[1])
        self.status_code, self.detail = error


# The actions every server write path that can touch an agent-owned card's
# lane, assignee, or status funnels through `evaluate_card_action`.
CARD_ACTIONS: tuple[str, ...] = ("lane_move", "assignee_change", "field_edit", "cancel")

REVIEW_ASSIGNEE_ERROR: tuple[int, str] = (
    400,
    "assignee is required and must be one of: " + ", ".join(ASSIGNEE_TAGS),
)


def status_claim_possible(status: str, tags: Iterable[str]) -> bool:
    """True iff a live-session lookup could change the claim outcome for
    this status/tags pair — i.e. no claim tag is present, but the status
    and assignee combination is the one shape a live CLI-opened session
    produces. Callers that can afford I/O use this to decide whether a
    session-store lookup is worth paying for before calling `is_claimed`
    with `has_live_session=True`; skipping it whenever this returns False
    is always safe, since `is_claimed` ignores `has_live_session` in every
    other case. Pure — the live-session lookup itself lives in
    `SessionStore`.
    """
    tset = _norm_tags(tags)
    if tset & {RUNNING_TAG, BLOCKED_TAG}:
        return False
    if (status or "").lower() != "in_progress":
        return False
    if COMPLETED_TAG in tset and ACCEPTED_TAG not in tset:
        return False
    return derive_assignee(tset) in AGENT_ASSIGNEES


def is_claimed(status: str, tags: Iterable[str], has_live_session: bool = False) -> bool:
    """True once the worker owns the card — actively running it, or waiting
    on an answer from the operator (`agent-running` / `agent-blocked`
    tag present, regardless of `has_live_session`), OR a live session is
    genuinely open on an agent-owned, non-review card whose status is
    `in_progress` (the state left behind the moment a `#claude`/`#codex`
    card's Open button spawns a CLI session, before the worker's own claim
    ever adds `agent-running`). That's still a live agent session the
    board must protect the same way, or a human could reassign/unassign/
    edit right out from under it — but `status == "in_progress"` alone
    isn't enough proof: a plain vault edit or an API status write can set
    that with no session behind it at all, and the board's own reassign
    move can leave a stale `in_progress` status on a card whose assignee
    just changed. `has_live_session` is the caller's own answer to "is
    there actually a session" (see `SessionStore.has_live_session`) —
    this function stays pure and takes that answer as given rather than
    querying for it.

    Excludes a pending Review card (`agent-completed` without `accepted`):
    the same status can linger at `in_progress` after the worker completes
    a card that was earlier opened via a CLI session (nothing resets it),
    and Review must stay reachable through the accept-by-drag Done
    carve-out rather than being swallowed by this status-derived claim.
    """
    tset = _norm_tags(tags)
    if tset & {RUNNING_TAG, BLOCKED_TAG}:
        return True
    if not has_live_session:
        return False
    if (status or "").lower() != "in_progress":
        return False
    if COMPLETED_TAG in tset and ACCEPTED_TAG not in tset:
        return False  # Review — see docstring
    return derive_assignee(tset) in AGENT_ASSIGNEES


def is_agent_owned(tags: Iterable[str]) -> bool:
    """True when the card is managed by an agent rather than a human —
    either its assignee tag names an agent engine, or the worker has
    already claimed it (`agent-running`/`agent-blocked`) even with no
    engine-specific assignee tag. Claimed cards without an assignee must
    still count as agent-owned so Cancel (and every other agent-owned-only
    action) remains available."""
    tset = _norm_tags(tags)
    return derive_assignee(tset) in AGENT_ASSIGNEES or bool(tset & {RUNNING_TAG, BLOCKED_TAG})


def is_review_pending(tags: Iterable[str]) -> bool:
    """True for a worker-completed card the operator hasn't accepted yet."""
    tset = _norm_tags(tags)
    return COMPLETED_TAG in tset and ACCEPTED_TAG not in tset


def evaluate_card_action(
    current_status: str,
    current_tags: Iterable[str],
    action: str,
    target_lane: Optional[str] = None,
    has_live_session: bool = False,
    *,
    is_project: bool = False,
    cancellation_pending: bool = False,
    has_live_coordinator: bool = False,
    hierarchy_valid: bool = True,
) -> Optional[tuple[int, str]]:
    """The one decision every server write path consults before touching an
    agent-owned card's lane, assignee, or status.

    `None` means the action is allowed; `(http_status, detail)` means it's
    refused and the caller should raise an `HTTPException` with that shape
    and perform no write. Pure — no I/O, no vault or scheduler access;
    `has_live_session` is the caller's own answer to whether a live agent
    session actually backs this card (see `is_claimed`).

    The intent (see docs/specs/product/agent-viz.md's Lanes section): a
    human may assign, reassign, unassign, or cancel an agent-owned card
    before the worker claims it; once claimed (`agent-running` /
    `agent-blocked`, or a live session on an `in_progress` card), every
    drag and every assignee/model/effort/host edit is refused — Answer,
    Kill, Accept, and Cancel are the actions that remain. A card assigned to `me`,
    or with no assignee, is entirely unaffected by any of this — every
    rule below for those matches ordinary human-card behavior exactly.
    """
    if action not in CARD_ACTIONS:
        # Every caller passes a literal from CARD_ACTIONS, so reaching here
        # at all means a caller bug; fail closed instead of falling through
        # to an implicit allow.
        raise ValueError(f"unknown card action: {action!r}")

    if not hierarchy_valid:
        return PROJECT_HIERARCHY_INVALID_ERROR
    if cancellation_pending:
        return PROJECT_CANCELLATION_PENDING_ERROR
    if has_live_coordinator and action in {"lane_move", "assignee_change", "field_edit"}:
        return PROJECT_COORDINATOR_LIVE_ERROR
    if is_project and action in {"lane_move", "cancel"}:
        return PROJECT_EXPLICIT_ACTION_ERROR

    claimed = is_claimed(current_status, current_tags, has_live_session)
    agent_owned = is_agent_owned(current_tags)
    is_review = is_review_pending(current_tags)

    if action == "lane_move":
        if target_lane not in LANES:
            return (400, f"unknown lane '{target_lane}'")
        if target_lane in ("review", "scheduled", "snoozed"):
            return (400, f"lane '{target_lane}' cannot be set directly")
        # The worker owns this card while it's actively running or waiting
        # on an answer — every drag is refused, on every lane, since a
        # human dragging it anywhere would silently detach a live task
        # from the process actually working it.
        if claimed:
            return WORKER_OWNED_ERROR
        if is_review:
            # A pending review (agent-completed, not yet accepted) must be
            # accepted or rejected before it can be reassigned to work or
            # handed to a human — only Done (the accept path) may act on it
            # directly.
            if target_lane in ("in_progress", "human_queue"):
                return REVIEW_ERROR
            return None
        if agent_owned:
            if target_lane == "in_progress":
                # Only the worker claims agent-assigned tasks (adds
                # #agent-running itself); a human dragging such a card to
                # In progress would desync the tag from the actual claim
                # state.
                return AGENT_ONLY_CLAIM_ERROR
            if target_lane in ("human_queue", "done"):
                # Agent-owned cards are managed by the agent — a human may
                # reassign, unassign, or cancel, but not silently close or
                # re-route work that was handed to an agent.
                return AGENT_OWNED_MANAGED_ERROR
        return None

    if action in ("assignee_change", "field_edit"):
        return WORKER_OWNED_ERROR if claimed else None

    if action == "cancel":
        if is_review:
            return REVIEW_UNRESOLVED_ERROR
        if not agent_owned:
            return CANCEL_NOT_AGENT_OWNED_ERROR
        # A card that's already finished — accepted-and-done, or cancelled
        # some other way than through this endpoint's own idempotent
        # short-circuit — has nothing left to cancel. The route
        # (`cancel_board_card`) still special-cases "already cancelled" as
        # a 200 no-op, but only AFTER checking ownership/review above, so a
        # `me` or Review card that happens to already carry
        # status="cancelled" gets its real refusal reason instead of a
        # misleading success.
        if (current_status or "").lower() in ("done", "cancelled"):
            return CANCEL_ALREADY_FINISHED_ERROR
        return None

    raise AssertionError(f"unhandled action {action!r} despite CARD_ACTIONS validation above")


def project_action_policy(
    current_status: str,
    current_tags: Iterable[str],
    project_summary: Optional[dict],
    *,
    hierarchy_valid: bool = True,
    execution_paused: bool = False,
    handoff_pending: bool = False,
) -> dict[str, bool]:
    """Pure project/paused-task action availability for board consumers."""
    summary = project_summary or {}
    coordinator = summary.get("coordinator") or {}
    live = bool(coordinator.get("live"))
    pending = bool(summary.get("cancellation_pending"))
    handoff_pending = bool(handoff_pending or summary.get("handoff_pending"))
    paused = bool(summary.get("paused"))
    is_project = bool(project_summary)
    terminal = (current_status or "").lower() in {"done", "cancelled"}
    agent_owner = derive_assignee(current_tags) in AGENT_ASSIGNEES
    return {
        "can_start_project": bool(
            is_project and hierarchy_valid and not terminal and not live
            and not pending and not handoff_pending
        ),
        "can_plan_project": bool(
            is_project and agent_owner and hierarchy_valid and not terminal and not live
            and not pending and not handoff_pending and not paused
        ),
        "can_complete_project": bool(
            is_project and hierarchy_valid and not terminal and not live and not pending
            and not handoff_pending
            and summary.get("ready_to_close")
        ),
        # Pending cancellation remains actionable: the retry must reuse the
        # operation ID returned by the preview endpoint.
        "can_cancel_project": bool(
            (is_project or handoff_pending) and hierarchy_valid and not terminal
        ),
        "can_resume_execution": bool(
            not is_project and execution_paused and hierarchy_valid and not pending
            and not handoff_pending
        ),
        # Pause never depends on `live` — pausing a running project is the
        # whole point (it stops new child claims while current work
        # finishes). Resume is refused for an agent-attributed caller at the
        # route layer, not here — this policy only decides whether the
        # action is offered at all.
        "can_pause_project": bool(
            is_project and hierarchy_valid and not terminal
            and not pending and not handoff_pending and not paused
        ),
        "can_resume_project": bool(
            is_project and hierarchy_valid and not terminal
            and not pending and not handoff_pending and paused
        ),
    }


# Session statuses that map to a board lane when a session carries no
# linked task (see `lane_for_session` below).
_SESSION_STATUS_LANES: dict[str, str] = {
    "running": "in_progress",
    "claimed": "in_progress",
    "yielded": "in_progress",
    "blocked": "human_queue",
    "completed": "done",
    "ended": "done",
    "failed": "done",
    "budget_exceeded": "done",
}


def lane_for_session(
    session_status: str,
    task_status: Optional[str],
    task_tags: Optional[Iterable[str]] = None,
    task_fields: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> str:
    """Derive the board lane a session's node should render in.

    A session linked to a task (`task_status is not None`) always takes that
    task's own derived lane (`derive_lane`, including its snooze — a
    snoozed Review card's session must render `snoozed` on the graph too,
    the same lane its board card shows), so the graph's node colour always
    agrees with that card's column on the board. A session with no linked
    task (most CLI and ad hoc sessions) instead maps from its own status:
    `running`/`claimed`/`yielded` -> in_progress, `blocked` -> human_queue,
    a terminal status -> done, anything else -> unassigned.
    """
    if task_status is not None:
        return derive_lane(task_status, task_tags or [], task_fields, now)
    return _SESSION_STATUS_LANES.get((session_status or "").lower(), "unassigned")


@dataclass
class LaneMovePlan:
    """What a `PUT /board/cards/{id}/lane` request should write, or why not.

    `status` / `tags` are `None` when that field shouldn't change. `fields`
    is the `TaskManager.update(fields=...)` patch to apply alongside them —
    every successful plan clears `snoozed_until`: dragging a snoozed card
    to any other lane always wakes it, whether or not it was actually
    snoozed to begin with (clearing an absent field is a no-op).
    `error` is `(http_status, detail)` when the move is invalid or
    forbidden — the caller should raise an `HTTPException` and perform no
    write.
    """
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    fields: Optional[dict] = None
    error: Optional[tuple[int, str]] = None


@dataclass
class ReviewActionPlan:
    """The task mutation for an operator action on a completed review."""

    status: Optional[str] = None
    tags: Optional[list[str]] = None
    error: Optional[tuple[int, str]] = None


def plan_review_action(
    current_status: str,
    current_tags: Iterable[str],
    action: str,
    assignee: Optional[str] = None,
) -> ReviewActionPlan:
    """Plan a review reject or reassignment without touching any stores.

    Reject returns the same card to worker-owned execution. Reassignment
    clears worker lifecycle markers and leaves the task in Assigned so the
    worker can claim it later; the existing session/transcript is deliberately
    not changed here, preserving prior-run context for that claim.
    """
    if action not in ("reject", "reassign"):
        raise ValueError(f"unknown review action: {action!r}")
    tags = [str(t) for t in (current_tags or [])]
    if not is_review_pending(tags):
        return ReviewActionPlan(error=(409, "card is not in the Review lane"))

    normalized = (assignee or "").lstrip("#").lower()
    if action == "reassign" and normalized not in ASSIGNEE_TAGS:
        return ReviewActionPlan(error=REVIEW_ASSIGNEE_ERROR)

    lifecycle = {
        RUNNING_TAG, BLOCKED_TAG, COMPLETED_TAG, "agent-failed",
        "agent-budget-exceeded", REASSIGNED_TAG, ACCEPTED_TAG,
    }
    cleaned = [t for t in tags if t.lstrip("#").lower() not in lifecycle]
    if action == "reject":
        cleaned.append(RUNNING_TAG)
        return ReviewActionPlan(status="in_progress", tags=cleaned)

    # Managed Agents consent tags are executor assignments too, even though
    # they are not board lanes/assignees. A reassignment must remove both
    # vocabularies so a stale #cloud-haiku cannot outrank the requested target.
    cleaned = [t for t in cleaned if t.lstrip("#").lower() not in AGENT_EXECUTOR_TAGS]
    cleaned.append(normalized)
    cleaned.append(REASSIGNED_TAG)
    return ReviewActionPlan(status="todo", tags=cleaned)


def plan_lane_move(
    current_status: str,
    current_tags: Iterable[str],
    target_lane: str,
    assignee: Optional[str] = None,
    has_live_session: bool = False,
    *,
    is_project: bool = False,
    cancellation_pending: bool = False,
    has_live_coordinator: bool = False,
    hierarchy_valid: bool = True,
) -> LaneMovePlan:
    """Compute the status/tags write for a card dropped into `target_lane`.

    Pure — the caller reads the current task, applies this plan via
    `TaskManager.update`, and (optionally) re-derives the lane from the
    written task to confirm it landed where expected. Never touches the
    vault or scheduler directly. The decision of whether the move is
    allowed at all lives in `evaluate_card_action` — this function only
    computes the resulting write once that's cleared it. `has_live_session`
    passes straight through to that check (see `is_claimed`).
    """
    error = evaluate_card_action(
        current_status, current_tags, "lane_move", target_lane, has_live_session=has_live_session,
        is_project=is_project,
        cancellation_pending=cancellation_pending,
        has_live_coordinator=has_live_coordinator,
        hierarchy_valid=hierarchy_valid,
    )
    if error is not None:
        return LaneMovePlan(error=error)

    # Every successful move clears `snoozed_until` — a lane move always
    # wakes the card, whether or not it was actually snoozed (clearing an
    # absent field is a no-op in `TaskManager.update`).
    def _plan(*, status: Optional[str] = None, tags: Optional[list[str]] = None) -> LaneMovePlan:
        return LaneMovePlan(status=status, tags=tags, fields={SNOOZED_UNTIL_FIELD: None})

    tags_list = [str(t) for t in (current_tags or [])]
    tset_lower = {t.lstrip("#").lower() for t in tags_list}
    is_review = is_review_pending(tags_list)

    if target_lane == "unassigned":
        # Dropping into Unassigned clears the assignee tag.
        new_tags = [t for t in tags_list if t.lstrip("#").lower() not in ASSIGNEE_TAGS]
        return _plan(tags=new_tags)

    if target_lane == "assigned":
        assignee_norm = (assignee or "").lstrip("#").lower()
        if assignee_norm not in ASSIGNEE_TAGS:
            return LaneMovePlan(error=(
                400,
                "assignee is required and must be one of: " + ", ".join(ASSIGNEE_TAGS),
            ))
        new_tags = [t for t in tags_list if t.lstrip("#").lower() not in ASSIGNEE_TAGS]
        new_tags.append(assignee_norm)
        return _plan(tags=new_tags)

    if target_lane == "in_progress":
        # A card can arrive here from Human queue (#human) — strip it so it
        # actually leaves Human queue instead of derive_lane immediately
        # pulling it back (Human queue outranks In progress).
        strip = {HUMAN_TAG}
        if tset_lower & strip:
            new_tags = [t for t in tags_list if t.lstrip("#").lower() not in strip]
            return _plan(status="in_progress", tags=new_tags)
        return _plan(status="in_progress")

    if target_lane == "human_queue":
        return _plan(status="blocked")

    if target_lane == "done":
        # A card can arrive here from Human queue — strip `human` so it
        # actually leaves that lane (it outranks Done in derive_lane). A
        # pending agent-completed review also needs the `accepted` tag or
        # Review would keep claiming it (Review outranks Done too) — this is
        # the one case dragging to Done still doubles as an accept.
        strip = {HUMAN_TAG}
        needs_strip = bool(tset_lower & strip)
        needs_accept = is_review
        if needs_strip or needs_accept:
            new_tags = [t for t in tags_list if t.lstrip("#").lower() not in strip]
            if needs_accept:
                new_tags.append(ACCEPTED_TAG)
            return _plan(status="done", tags=new_tags)
        return _plan(status="done")

    # Review, Scheduled, and Snoozed are derived — Review from the worker's
    # agent-completed/accepted tags (use POST .../accept instead), Scheduled
    # from the scheduler store, Snoozed from `snoozed_until` (use PUT
    # .../snooze instead). None of the three is directly settable by
    # dragging a card there. Unreachable given evaluate_card_action's checks
    # above; kept as a defensive fallback.
    return LaneMovePlan(error=(400, f"lane '{target_lane}' cannot be set directly"))


def is_schedule_active(enabled: bool, next_trigger_at: Optional[str], schedule_type: str = "") -> bool:
    """True -> Scheduled lane; False -> Done lane.

    A schedule entry is Scheduled while it's enabled and either has a future
    fire (cron/once) or is a `manual` schedule — which never has a next fire
    at all, so an enabled manual entry with `next_trigger_at is None` is its
    normal, permanent state, not a fired/disabled one. `SchedulerStore`
    already clears `next_trigger_at` when a recurring entry is disabled and
    when a one-off fires (see `update()` / trigger recording in
    `scheduler_store.py`), so for cron/once, `enabled and next_trigger_at is
    not None` is sufficient — it covers "fired one-off" and "disabled
    recurring" the same way, so both show in Done.
    """
    if schedule_type == "manual":
        return bool(enabled)
    return bool(enabled) and next_trigger_at is not None
