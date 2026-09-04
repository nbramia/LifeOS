"""Kanban board view-model helpers for `/agents` (#850).

Pure functions only — no I/O, no vault or scheduler access — so lane
derivation and lane-move planning can be unit-tested exhaustively against the
lane table in issue #850 without a TaskManager or SchedulerStore fixture.
`api/routes/agents.py` wires these against the real stores and stays thin:
it reads a task/schedule entry, calls into this module for the *decision*,
then performs the write. See docs/specs/technical/agent-viz.md.

Lanes are derived from task status + tags on every read — there is no stored
lane field anywhere in the vault or the task index.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Tags the agent worker itself writes as it drives a task through its
# lifecycle — see AGENT_TAG / RUNNING_TAG / COMPLETED_TAG / BLOCKED_TAG in
# api/services/agent_worker/worker.py. Mirrored here (not imported) so this
# module stays import-light — the board treats them as opaque strings, not
# worker internals.
AGENT_TAG = "agent"
RUNNING_TAG = "agent-running"
COMPLETED_TAG = "agent-completed"
BLOCKED_TAG = "agent-blocked"

# A `#human` card is filed for the operator directly (not by the worker).
HUMAN_TAG = "human"

# The accepted marker for Review -> Done (#850) — a tag, not a new status
# symbol, per the issue's constraints.
ACCEPTED_TAG = "accepted"

# Assignee is exactly one tag from this set. "me" is the operator; the rest
# are agent engines. This is a labeling convention only at this stage —
# actually dispatching to an engine from an assignee tag is issue #851
# (assignment and execution), out of scope here.
ASSIGNEE_TAGS: tuple[str, ...] = ("me", "claude", "codex", "hermes", "local")
AGENT_ASSIGNEES: tuple[str, ...] = ("claude", "codex", "hermes", "local")

LANES: tuple[str, ...] = (
    "unassigned",
    "assigned",
    "in_progress",
    "human_queue",
    "scheduled",
    "review",
    "done",
)

# Lanes a task can derive into (excludes "scheduled", which only ever holds
# scheduler entries).
TASK_LANES: tuple[str, ...] = tuple(lane for lane in LANES if lane != "scheduled")


def _norm_tags(tags: Iterable[str]) -> set[str]:
    return {str(t).lstrip("#").lower() for t in (tags or [])}


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


def derive_lane(status: str, tags: Iterable[str]) -> str:
    """Derive a task's board lane from its status + tags.

    See the lane table in issue #850. Never stored — recomputed on every
    read from the task's current status/tags.

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
    if BLOCKED_TAG in tset or HUMAN_TAG in tset or status_norm == "blocked":
        return "human_queue"
    if status_norm == "in_progress" or RUNNING_TAG in tset:
        return "in_progress"
    if status_norm in ("done", "cancelled"):
        return "done"
    if derive_assignee(tset) is not None:
        return "assigned"
    return "unassigned"


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

# The actions every server write path that can touch an agent-owned card's
# lane, assignee, or status funnels through `evaluate_card_action` for (#881).
CARD_ACTIONS: tuple[str, ...] = ("lane_move", "assignee_change", "field_edit", "cancel")


def is_claimed(tags: Iterable[str]) -> bool:
    """True once the worker owns the card — actively running it, or waiting
    on an answer from the operator (`agent-running` / `agent-blocked`)."""
    return bool(_norm_tags(tags) & {RUNNING_TAG, BLOCKED_TAG})


def is_agent_owned(tags: Iterable[str]) -> bool:
    """True when the card's assignee is an agent engine, not `me` or unset."""
    return derive_assignee(tags) in AGENT_ASSIGNEES


def is_review_pending(tags: Iterable[str]) -> bool:
    """True for a worker-completed card the operator hasn't accepted yet."""
    tset = _norm_tags(tags)
    return COMPLETED_TAG in tset and ACCEPTED_TAG not in tset


def evaluate_card_action(
    current_status: str,
    current_tags: Iterable[str],
    action: str,
    target_lane: Optional[str] = None,
) -> Optional[tuple[int, str]]:
    """The one decision every server write path consults before touching an
    agent-owned card's lane, assignee, or status (#881).

    `None` means the action is allowed; `(http_status, detail)` means it's
    refused and the caller should raise an `HTTPException` with that shape
    and perform no write. Pure — no I/O, no vault or scheduler access.

    The intent (see docs/specs/product/agent-viz.md's Lanes section): a
    human may assign, reassign, unassign, or cancel an agent-owned card
    before the worker claims it; once claimed (`agent-running` /
    `agent-blocked`), every drag and every assignee/model/effort/host edit
    is refused — Answer, Kill, and Accept are the only actions left. A card
    assigned to `me`, or with no assignee, is entirely unaffected — every
    rule below for those is byte-identical to the pre-#881 behavior.
    """
    claimed = is_claimed(current_tags)
    agent_owned = is_agent_owned(current_tags)
    is_review = is_review_pending(current_tags)

    if action == "lane_move":
        if target_lane not in LANES:
            return (400, f"unknown lane '{target_lane}'")
        if target_lane in ("review", "scheduled"):
            return (400, f"lane '{target_lane}' cannot be set directly")
        # The worker owns this card while it's actively running or waiting
        # on an answer — every drag is refused, not just In progress/Done
        # (round-2 finding 1 for #850 caught the desync on those two; #881
        # extends the same protection to Unassigned/Assigned/Human queue,
        # which a human could still silently detach a live task through).
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
                # Only the worker claims agent-assigned tasks (swaps #agent
                # -> #agent-running itself); a human dragging such a card to
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
        return None

    return None


@dataclass
class LaneMovePlan:
    """What a `PUT /board/cards/{id}/lane` request should write, or why not.

    `status` / `tags` are `None` when that field shouldn't change. `error`
    is `(http_status, detail)` when the move is invalid or forbidden — the
    caller should raise an `HTTPException` and perform no write.
    """
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    error: Optional[tuple[int, str]] = None


def plan_lane_move(
    current_status: str,
    current_tags: Iterable[str],
    target_lane: str,
    assignee: Optional[str] = None,
) -> LaneMovePlan:
    """Compute the status/tags write for a card dropped into `target_lane`.

    Pure — the caller reads the current task, applies this plan via
    `TaskManager.update`, and (optionally) re-derives the lane from the
    written task to confirm it landed where expected. Never touches the
    vault or scheduler directly. The decision of whether the move is
    allowed at all lives in `evaluate_card_action` — this function only
    computes the resulting write once that's cleared it.
    """
    error = evaluate_card_action(current_status, current_tags, "lane_move", target_lane)
    if error is not None:
        return LaneMovePlan(error=error)

    tags_list = [str(t) for t in (current_tags or [])]
    tset_lower = {t.lstrip("#").lower() for t in tags_list}
    is_review = is_review_pending(tags_list)

    if target_lane == "unassigned":
        # Dropping into Unassigned clears the assignee tag.
        new_tags = [t for t in tags_list if t.lstrip("#").lower() not in ASSIGNEE_TAGS]
        return LaneMovePlan(tags=new_tags)

    if target_lane == "assigned":
        assignee_norm = (assignee or "").lstrip("#").lower()
        if assignee_norm not in ASSIGNEE_TAGS:
            return LaneMovePlan(error=(
                400,
                "assignee is required and must be one of: " + ", ".join(ASSIGNEE_TAGS),
            ))
        new_tags = [t for t in tags_list if t.lstrip("#").lower() not in ASSIGNEE_TAGS]
        new_tags.append(assignee_norm)
        return LaneMovePlan(tags=new_tags)

    if target_lane == "in_progress":
        # A card can arrive here from Human queue (#human) — strip it so it
        # actually leaves Human queue instead of derive_lane immediately
        # pulling it back (Human queue outranks In progress).
        strip = {HUMAN_TAG}
        if tset_lower & strip:
            new_tags = [t for t in tags_list if t.lstrip("#").lower() not in strip]
            return LaneMovePlan(status="in_progress", tags=new_tags)
        return LaneMovePlan(status="in_progress")

    if target_lane == "human_queue":
        return LaneMovePlan(status="blocked")

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
            return LaneMovePlan(status="done", tags=new_tags)
        return LaneMovePlan(status="done")

    # Review and Scheduled are derived — Review from the worker's
    # agent-completed/accepted tags (use POST .../accept instead), Scheduled
    # from the scheduler store. Neither is directly settable by dragging a
    # card there. Unreachable given evaluate_card_action's checks above;
    # kept as a defensive fallback.
    return LaneMovePlan(error=(400, f"lane '{target_lane}' cannot be set directly"))


def is_schedule_active(enabled: bool, next_trigger_at: Optional[str]) -> bool:
    """True -> Scheduled lane; False -> Done lane.

    A schedule entry is Scheduled while it's enabled and has a future fire.
    `SchedulerStore` already clears `next_trigger_at` when a recurring entry
    is disabled and when a one-off fires (see `update()` / trigger recording
    in `scheduler_store.py`), so `enabled and next_trigger_at is not None` is
    sufficient — it covers "fired one-off" and "disabled recurring" the same
    way, matching the issue's rule that both show in Done.
    """
    return bool(enabled) and next_trigger_at is not None
