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
CANCEL_ALREADY_FINISHED_ERROR: tuple[int, str] = (
    409,
    "this card is already finished — nothing to cancel",
)

# The actions every server write path that can touch an agent-owned card's
# lane, assignee, or status funnels through `evaluate_card_action`.
CARD_ACTIONS: tuple[str, ...] = ("lane_move", "assignee_change", "field_edit", "cancel")


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
    engine-specific assignee tag at all. The worker selects candidates by
    the bare `agent` queue tag alone and its claim swap
    (`agent` -> `agent-running`) never touches assignee tags, so a card
    can be claimed while `derive_assignee` still returns `None` — that
    shape must still count as agent-owned, or Cancel (and every other
    agent-owned-only action) would refuse the one card that most needs
    it, with no assignee tag left to edit it back to a workable state."""
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
    Kill, and Accept are the only actions left. A card assigned to `me`,
    or with no assignee, is entirely unaffected by any of this — every
    rule below for those matches ordinary human-card behavior exactly.
    """
    if action not in CARD_ACTIONS:
        # Every caller passes a literal from CARD_ACTIONS, so reaching here
        # at all means a caller bug; fail closed instead of falling through
        # to an implicit allow.
        raise ValueError(f"unknown card action: {action!r}")

    claimed = is_claimed(current_status, current_tags, has_live_session)
    agent_owned = is_agent_owned(current_tags)
    is_review = is_review_pending(current_tags)

    if action == "lane_move":
        if target_lane not in LANES:
            return (400, f"unknown lane '{target_lane}'")
        if target_lane in ("review", "scheduled"):
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
    has_live_session: bool = False,
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
    )
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
