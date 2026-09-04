"""Unit tests for api/services/agent_board.py — pure lane derivation and
lane-move planning (#850). One test per row of the lane table in the issue,
plus the scheduler-entry bucketing rule.
"""
import pytest

from api.services import agent_board

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# derive_assignee
# ---------------------------------------------------------------------------

class TestDeriveAssignee:
    def test_no_assignee_tag(self):
        assert agent_board.derive_assignee(["docs", "writing"]) is None

    def test_me(self):
        assert agent_board.derive_assignee(["me"]) == "me"

    @pytest.mark.parametrize("engine", ["claude", "codex", "hermes", "local"])
    def test_agent_engines(self, engine):
        assert agent_board.derive_assignee([engine]) == engine

    def test_hash_prefix_and_case_insensitive(self):
        assert agent_board.derive_assignee(["#Codex"]) == "codex"

    def test_empty_tags(self):
        assert agent_board.derive_assignee([]) is None

    def test_multiple_assignee_tags_precedence_is_ASSIGNEE_TAGS_order(self):
        # Round-1 finding 15: precedence follows ASSIGNEE_TAGS order
        # ("me" first), not the order the tags happen to appear in the list.
        assert agent_board.derive_assignee(["codex", "me"]) == "me"


class TestNormalizeTags:
    def test_strips_hash_and_lowercases(self):
        # (#881 AR2/AR3) The public wrapper `api/routes/tasks.py` uses to
        # compare tag SETS, not just the single derive_assignee() value.
        assert agent_board.normalize_tags(["#Codex", "Agent-Running"]) == {"codex", "agent-running"}

    def test_empty(self):
        assert agent_board.normalize_tags([]) == set()
        assert agent_board.normalize_tags(None) == set()


# ---------------------------------------------------------------------------
# derive_lane — one test per row of the issue's lane table
# ---------------------------------------------------------------------------

class TestDeriveLaneTable:
    def test_unassigned_open_task_no_assignee(self):
        assert agent_board.derive_lane("todo", []) == "unassigned"

    def test_assigned_me_tag_status_todo(self):
        assert agent_board.derive_lane("todo", ["me"]) == "assigned"

    def test_assigned_agent_engine_tag_not_yet_claimed(self):
        assert agent_board.derive_lane("todo", ["codex"]) == "assigned"

    def test_in_progress_status(self):
        assert agent_board.derive_lane("in_progress", []) == "in_progress"

    def test_in_progress_agent_running_tag(self):
        # AC: tags [agent, agent-running] -> In progress, regardless of status.
        assert agent_board.derive_lane("todo", ["agent", "agent-running"]) == "in_progress"

    def test_human_queue_human_tag_and_blocked_status(self):
        assert agent_board.derive_lane("blocked", ["human"]) == "human_queue"

    def test_human_queue_agent_blocked_tag(self):
        assert agent_board.derive_lane("todo", ["agent-blocked"]) == "human_queue"

    def test_human_queue_status_blocked_alone(self):
        assert agent_board.derive_lane("blocked", []) == "human_queue"

    def test_scheduled_is_not_a_task_lane(self):
        # Scheduled cards never come from derive_lane — they're built
        # directly from scheduler entries in the route layer.
        assert "scheduled" not in [
            agent_board.derive_lane(s, t)
            for s in ("todo", "in_progress", "blocked", "done", "cancelled")
            for t in ([], ["me"], ["agent-completed"])
        ]

    def test_review_agent_completed_status_done_not_accepted(self):
        assert agent_board.derive_lane("done", ["agent-completed"]) == "review"

    def test_review_beats_done_once_accepted_it_moves_to_done(self):
        assert agent_board.derive_lane("done", ["agent-completed", "accepted"]) == "done"

    def test_done_status_done_no_special_tags(self):
        assert agent_board.derive_lane("done", []) == "done"

    def test_done_status_cancelled(self):
        assert agent_board.derive_lane("cancelled", []) == "done"


# ---------------------------------------------------------------------------
# derive_lane — additional edge cases / priority ordering
# ---------------------------------------------------------------------------

class TestDeriveLanePriority:
    def test_human_queue_beats_in_progress(self):
        # agent-blocked + agent-running (worker rolled it to blocked but a
        # stale running tag lingers) -> human queue wins.
        assert agent_board.derive_lane("blocked", ["agent-running", "agent-blocked"]) == "human_queue"

    def test_review_beats_human_queue(self):
        assert agent_board.derive_lane("done", ["agent-completed", "human"]) == "review"

    def test_assigned_status_urgent_still_assigned(self):
        assert agent_board.derive_lane("urgent", ["local"]) == "assigned"

    def test_unassigned_status_deferred_no_assignee(self):
        assert agent_board.derive_lane("deferred", []) == "unassigned"


# ---------------------------------------------------------------------------
# plan_lane_move
# ---------------------------------------------------------------------------

class TestPlanLaneMove:
    def test_unknown_lane_errors(self):
        plan = agent_board.plan_lane_move("todo", [], "nonsense", None)
        assert plan.error == (400, "unknown lane 'nonsense'")

    def test_done_marks_task_done(self):
        plan = agent_board.plan_lane_move("todo", ["me"], "done", None)
        assert plan.status == "done"
        assert plan.tags is None
        assert plan.error is None

    def test_unassigned_removes_assignee_tag(self):
        plan = agent_board.plan_lane_move("todo", ["codex", "urgent-work"], "unassigned", None)
        assert plan.error is None
        assert plan.status is None
        assert sorted(plan.tags) == ["urgent-work"]

    def test_assigned_sets_codex_and_removes_other_assignee(self):
        plan = agent_board.plan_lane_move("todo", ["me", "docs"], "assigned", "codex")
        assert plan.error is None
        assert plan.status is None
        assert sorted(plan.tags) == ["codex", "docs"]

    def test_assigned_without_assignee_body_errors(self):
        plan = agent_board.plan_lane_move("todo", [], "assigned", None)
        assert plan.error is not None
        assert plan.error[0] == 400

    def test_assigned_invalid_assignee_errors(self):
        plan = agent_board.plan_lane_move("todo", [], "assigned", "gpt5")
        assert plan.error is not None
        assert plan.error[0] == 400

    def test_in_progress_agent_assignee_is_409(self):
        for engine in ("claude", "codex", "hermes", "local"):
            plan = agent_board.plan_lane_move("todo", [engine], "in_progress", None)
            assert plan.error == (409, "only the worker claims agent-assigned tasks"), engine

    def test_in_progress_me_assignee_sets_status(self):
        plan = agent_board.plan_lane_move("todo", ["me"], "in_progress", None)
        assert plan.error is None
        assert plan.status == "in_progress"

    def test_in_progress_multiple_assignee_tags_uses_precedence(self):
        # Round-1 finding 15: with both "me" and "codex" tags present,
        # derive_assignee resolves "me" (ASSIGNEE_TAGS precedence) — since
        # "me" isn't in AGENT_ASSIGNEES, the move succeeds instead of 409ing.
        plan = agent_board.plan_lane_move("todo", ["me", "codex"], "in_progress", None)
        assert plan.error is None
        assert plan.status == "in_progress"

    def test_in_progress_no_assignee_sets_status(self):
        plan = agent_board.plan_lane_move("todo", [], "in_progress", None)
        assert plan.error is None
        assert plan.status == "in_progress"

    def test_human_queue_sets_blocked_status(self):
        plan = agent_board.plan_lane_move("todo", [], "human_queue", None)
        assert plan.error is None
        assert plan.status == "blocked"

    def test_review_cannot_be_set_directly(self):
        plan = agent_board.plan_lane_move("todo", [], "review", None)
        assert plan.error is not None
        assert plan.error[0] == 400

    def test_scheduled_cannot_be_set_directly(self):
        plan = agent_board.plan_lane_move("todo", [], "scheduled", None)
        assert plan.error is not None
        assert plan.error[0] == 400


# ---------------------------------------------------------------------------
# plan_lane_move — landing lane invariant (#850 round-1 finding 1, sharpened
# by round-2 findings 1 and 2; extended by #881)
#
# Dropping a card on a settable lane must either land it there, or be
# rejected for one of exactly four reasons derived straight from the
# starting tags — never "any 409 passes" (round-2 finding 12/2c):
#   * the worker owns the card (agent-running / agent-blocked present) —
#     EVERY target lane is refused, not just In progress/Done (#881
#     extends round-2 finding 1's In-progress/Done-only guard to
#     Unassigned/Assigned/Human queue too — a human could otherwise still
#     silently detach a live worker task by dragging it there)
#   * an agent-engine assignee tag is present (not yet claimed by the
#     worker, not a pending review) and the target is In progress
#     (round-1 finding, unchanged), or Human queue/Done (#881: agent-owned
#     cards are managed by the agent — reassign, unassign, or cancel
#     instead of silently closing or re-routing them)
#   * the card is a pending review (agent-completed, not yet accepted) and
#     the target is In progress or Human queue — Done still doubles as the
#     accept path (round-2 finding 2a)
# Assigned/unassigned are tags-only by design (an assignee change must not
# pull a card out of Human queue — the lane table says Human queue beats
# Assigned), so for those two targets this only checks the assignee-tag
# outcome and that `status` is left untouched, never landed-lane == target
# — except when the worker owns the card, where they're refused outright.
# ---------------------------------------------------------------------------

class TestPlanLaneMoveLandsInTargetLane:
    STARTING_STATES = {
        "unassigned": ("todo", []),
        "assigned_me": ("todo", ["me"]),
        "assigned_codex": ("todo", ["codex"]),
        "in_progress_status": ("in_progress", []),
        "in_progress_worker_running": ("todo", ["agent", "agent-running"]),
        "human_queue_human_tag": ("todo", ["human"]),
        "human_queue_human_tag_and_blocked_status": ("blocked", ["human"]),
        "human_queue_agent_blocked": ("todo", ["agent", "agent-blocked"]),
        "human_queue_blocked_status": ("blocked", []),
        "review_agent_completed_status_todo": ("todo", ["agent-completed"]),
        "review_agent_completed_status_done": ("done", ["agent-completed"]),
        "review_assignee_me": ("done", ["me", "agent-completed"]),
        "done_plain": ("done", []),
        "done_accepted": ("done", ["codex", "accepted"]),
        "cancelled": ("cancelled", []),
    }

    SETTABLE_TARGETS = ["unassigned", "assigned", "in_progress", "human_queue", "done"]

    WORKER_OWNED_ERROR = (
        409,
        "the worker owns this task while it is running or waiting on an "
        "answer — answer or kill the session first",
    )
    AGENT_ASSIGNEE_ERROR = (409, "only the worker claims agent-assigned tasks")
    REVIEW_ERROR = (409, "accept the review first")
    AGENT_OWNED_MANAGED_ERROR = (
        409,
        "agent-owned cards are managed by the agent — reassign, unassign, or "
        "cancel this card instead",
    )

    @classmethod
    def _expected_error(cls, current_status, current_tags, target_lane):
        """The one legitimate rejection reason for this (state, target)
        pair, or None if the move must succeed. Derived independently of
        `plan_lane_move` so the test can't just echo the implementation."""
        tags_lower = {t.lstrip("#").lower() for t in current_tags}
        worker_owned = bool(tags_lower & {"agent-running", "agent-blocked"})
        current_assignee = agent_board.derive_assignee(current_tags)
        is_review = "agent-completed" in tags_lower and "accepted" not in tags_lower

        # #881: a worker-owned card refuses EVERY target lane, not just
        # In progress/Done.
        if worker_owned:
            return cls.WORKER_OWNED_ERROR
        if target_lane == "in_progress" and current_assignee in agent_board.AGENT_ASSIGNEES:
            return cls.AGENT_ASSIGNEE_ERROR
        if target_lane in ("in_progress", "human_queue") and is_review:
            return cls.REVIEW_ERROR
        # #881: an agent-owned, unclaimed, non-review card also refuses
        # Human queue and Done — only the agent (via accept/complete) or an
        # explicit Cancel may land it there, not a human drag.
        if (
            target_lane in ("human_queue", "done")
            and current_assignee in agent_board.AGENT_ASSIGNEES
            and not is_review
        ):
            return cls.AGENT_OWNED_MANAGED_ERROR
        return None

    @pytest.mark.parametrize("target_lane", SETTABLE_TARGETS)
    @pytest.mark.parametrize("start_name,start", list(STARTING_STATES.items()))
    def test_matrix(self, start_name, start, target_lane):
        current_status, current_tags = start
        plan = agent_board.plan_lane_move(current_status, current_tags, target_lane, "me")
        expected_error = self._expected_error(current_status, current_tags, target_lane)

        if expected_error is not None:
            assert plan.error == expected_error, (start_name, target_lane, plan.error)
            assert plan.status is None and plan.tags is None, (start_name, target_lane)
            return

        assert plan.error is None, (start_name, target_lane, plan.error)

        if target_lane in ("assigned", "unassigned"):
            assert plan.status is None, (start_name, target_lane, plan.status)
            landed_tags = plan.tags if plan.tags is not None else list(current_tags)
            new_assignee = agent_board.derive_assignee(landed_tags)
            expected_assignee = "me" if target_lane == "assigned" else None
            assert new_assignee == expected_assignee, (start_name, target_lane, landed_tags)
            return

        final_status = plan.status if plan.status is not None else current_status
        final_tags = plan.tags if plan.tags is not None else list(current_tags)
        assert agent_board.derive_lane(final_status, final_tags) == target_lane, (
            start_name, target_lane, final_status, final_tags,
        )

    def test_done_strips_human_tag(self):
        plan = agent_board.plan_lane_move("todo", ["human"], "done", None)
        assert plan.status == "done"
        assert "human" not in plan.tags

    def test_done_rejects_worker_owned_agent_blocked_and_agent_running(self):
        # Round-2 finding 1: the worker owns a card carrying agent-running or
        # agent-blocked — dropping it on Done must 409 and write nothing,
        # not silently strip the tag out from under a live worker task.
        plan = agent_board.plan_lane_move(
            "blocked", ["codex", "agent", "agent-blocked", "agent-running"], "done", None,
        )
        assert plan.error == (
            409,
            "the worker owns this task while it is running or waiting on an "
            "answer — answer or kill the session first",
        )
        assert plan.status is None
        assert plan.tags is None

    def test_done_appends_accepted_when_agent_completed_present(self):
        plan = agent_board.plan_lane_move("done", ["codex", "agent-completed"], "done", None)
        assert plan.status == "done"
        assert set(plan.tags) == {"codex", "agent-completed", "accepted"}

    def test_done_does_not_double_append_accepted(self):
        plan = agent_board.plan_lane_move(
            "done", ["codex", "agent-completed", "accepted"], "done", None,
        )
        assert plan.tags is None or plan.tags.count("accepted") == 1

    def test_in_progress_strips_human(self):
        plan = agent_board.plan_lane_move("todo", ["human"], "in_progress", None)
        assert plan.status == "in_progress"
        assert "human" not in plan.tags

    def test_in_progress_rejects_worker_owned_agent_blocked(self):
        # Round-2 finding 1: agent-blocked means the worker owns this card
        # (it's waiting on an answer) — 409, not a silent strip.
        plan = agent_board.plan_lane_move("todo", ["agent-blocked"], "in_progress", None)
        assert plan.error == (
            409,
            "the worker owns this task while it is running or waiting on an "
            "answer — answer or kill the session first",
        )
        assert plan.status is None
        assert plan.tags is None


# ---------------------------------------------------------------------------
# is_schedule_active — scheduler-entry bucketing rules from the issue
# ---------------------------------------------------------------------------

class TestIsScheduleActive:
    def test_enabled_cron_with_future_fire_is_active(self):
        assert agent_board.is_schedule_active(True, "2099-01-01T00:00:00+00:00") is True

    def test_disabled_recurring_is_not_active(self):
        assert agent_board.is_schedule_active(False, None) is False

    def test_disabled_but_stale_next_trigger_is_not_active(self):
        # Defensive: SchedulerStore clears next_trigger_at on disable, but the
        # function shouldn't trust a stale value if enabled=False anyway.
        assert agent_board.is_schedule_active(False, "2099-01-01T00:00:00+00:00") is False

    def test_fired_one_off_has_no_next_trigger(self):
        assert agent_board.is_schedule_active(False, None) is False

    def test_enabled_with_no_next_trigger_is_not_active(self):
        assert agent_board.is_schedule_active(True, None) is False


# ---------------------------------------------------------------------------
# evaluate_card_action — the #881 shared decision function. Every server
# write path that can touch an agent-owned card's lane, assignee, or status
# (the lane endpoint, the cancel endpoint, and the guarded
# `PUT /api/tasks/{id}`) calls this ONE function — this is the exhaustive
# (state, action) decision table the issue asks for.
#
# States are every combination of:
#   assignee   -- none, `me`, or one of the four agent engines
#   condition  -- unclaimed, agent-running, agent-blocked, a pending review
#                 (agent-completed, not accepted), accepted, done, cancelled,
#                 cli_opened (status="in_progress", no agent-running tag —
#                 the state cli_session_event leaves behind after the board's
#                 Open button spawns a session but before the worker ever
#                 claims the card itself, #881 AR4/RC9)
# crossed against every action:
#   lane_move  -- to each of the seven lanes
#   assignee_change, field_edit, cancel
#
# `_expected_outcome` below is a second, independent statement of the rules
# from docs/specs/product/agent-viz.md's Lanes section / the issue's spec —
# written against the RULE TEXT, not by re-deriving what the implementation
# happens to return, so a regression in evaluate_card_action's precedence
# or wording actually fails this table instead of the test quietly agreeing
# with whatever the code does.
# ---------------------------------------------------------------------------

ALL_LANES = (
    "unassigned", "assigned", "in_progress", "human_queue", "scheduled", "review", "done",
)
ALL_ASSIGNEES = (None, "me", "claude", "codex", "hermes", "local")
ALL_CONDITIONS = (
    "unclaimed", "agent_running", "agent_blocked", "review", "accepted", "done", "cancelled",
    "cli_opened",
)
ALL_ACTIONS = ("lane_move", "assignee_change", "field_edit", "cancel")

_WORKER_OWNED = (
    409,
    "the worker owns this task while it is running or waiting on an "
    "answer — answer or kill the session first",
)
_REVIEW_FIRST = (409, "accept the review first")
_ONLY_WORKER_CLAIMS = (409, "only the worker claims agent-assigned tasks")
_AGENT_MANAGED = (
    409,
    "agent-owned cards are managed by the agent — reassign, unassign, or "
    "cancel this card instead",
)
_ACCEPT_OR_REJECT = (409, "accept or reject the review")
_CANCEL_NOT_AGENT_OWNED = (409, "cancel is only available for agent-assigned cards")
_CANCEL_ALREADY_FINISHED = (409, "this card is already finished — nothing to cancel")


def _state_to_status_tags(assignee, condition):
    """Build a (status, tags) pair for one (assignee, condition) state."""
    tags = [assignee] if assignee else []
    status = "todo"
    if condition == "agent_running":
        tags += ["agent", "agent-running"]
    elif condition == "agent_blocked":
        tags += ["agent", "agent-blocked"]
    elif condition == "review":
        tags += ["agent-completed"]
        status = "done"
    elif condition == "accepted":
        tags += ["agent-completed", "accepted"]
        status = "done"
    elif condition == "done":
        status = "done"
    elif condition == "cancelled":
        status = "cancelled"
    elif condition == "cli_opened":
        # (#881 AR4/RC9) cli_session_event sets status="in_progress" the
        # first time a card's Open button spawns a session, but never adds
        # agent-running (only the worker's own #agent claim flow does) —
        # no extra tag beyond the plain assignee.
        status = "in_progress"
    return status, tags


def _expected_outcome(assignee, condition, action, target_lane=None):
    """The one legitimate outcome for this (assignee, condition, action,
    target_lane) combination, per the issue's rule text — `None` for
    allowed, `(status_code, detail)` for refused."""
    agent_owned = assignee in ("claude", "codex", "hermes", "local")
    # (#881 AR4) A CLI-opened, agent-owned card (status="in_progress", no
    # agent-running tag) is claimed the same way agent-running/agent-blocked
    # are — a live session exists either way. `condition == "cli_opened"`
    # for a `me`/unassigned card is deliberately NOT claimed (only an
    # agent-owned assignee triggers the status-derived claim).
    claimed = condition in ("agent_running", "agent_blocked") or (
        condition == "cli_opened" and agent_owned
    )
    is_review = condition == "review"

    if action == "lane_move":
        if target_lane not in ALL_LANES:
            return (400, f"unknown lane '{target_lane}'")
        if target_lane in ("review", "scheduled"):
            return (400, f"lane '{target_lane}' cannot be set directly")
        # Rule 3: claimed refuses EVERY target lane.
        if claimed:
            return _WORKER_OWNED
        # Rule 4: a pending review only allows Done (the accept path) plus
        # the tags-only Unassigned/Assigned targets; In progress/Human
        # queue must go through accept/reject first.
        if is_review:
            if target_lane in ("in_progress", "human_queue"):
                return _REVIEW_FIRST
            return None
        # Rule 5: an unclaimed, non-review agent-owned card may be
        # (re)assigned or cancelled, but not claimed by a human (In
        # progress) or silently closed/re-routed (Human queue, Done).
        if agent_owned:
            if target_lane == "in_progress":
                return _ONLY_WORKER_CLAIMS
            if target_lane in ("human_queue", "done"):
                return _AGENT_MANAGED
        # Rule 6: `me` or unassigned — every existing rule/outcome is
        # unaffected by #881.
        return None

    if action in ("assignee_change", "field_edit"):
        return _WORKER_OWNED if claimed else None

    if action == "cancel":
        if is_review:
            return _ACCEPT_OR_REJECT
        if not agent_owned:
            return _CANCEL_NOT_AGENT_OWNED
        # (#881 RC2) A card that's already finished (accepted-and-done, or
        # cancelled some other way than through the endpoint's own
        # idempotent 200) has nothing left to cancel — narrows the
        # operator's literal "any agent-assigned card not in Review"
        # wording to exclude already-finished cards. Checked against the
        # actual STATUS the condition produces (both "accepted" and "done"
        # conditions carry status="done"), matching what the real function
        # checks — not the condition label itself.
        status, _tags = _state_to_status_tags(assignee, condition)
        if status.lower() in ("done", "cancelled"):
            return _CANCEL_ALREADY_FINISHED
        return None

    raise AssertionError(f"unhandled action {action!r} in test oracle")


class TestEvaluateCardActionDecisionTable:
    @pytest.mark.parametrize("target_lane", ALL_LANES)
    @pytest.mark.parametrize("action", ["lane_move"])
    @pytest.mark.parametrize("condition", ALL_CONDITIONS)
    @pytest.mark.parametrize("assignee", ALL_ASSIGNEES)
    def test_lane_move_matrix(self, assignee, condition, action, target_lane):
        status, tags = _state_to_status_tags(assignee, condition)
        expected = _expected_outcome(assignee, condition, action, target_lane)
        actual = agent_board.evaluate_card_action(status, tags, action, target_lane)
        assert actual == expected, (assignee, condition, action, target_lane, actual)

    @pytest.mark.parametrize("action", ["assignee_change", "field_edit", "cancel"])
    @pytest.mark.parametrize("condition", ALL_CONDITIONS)
    @pytest.mark.parametrize("assignee", ALL_ASSIGNEES)
    def test_non_lane_actions_matrix(self, assignee, condition, action):
        status, tags = _state_to_status_tags(assignee, condition)
        expected = _expected_outcome(assignee, condition, action)
        actual = agent_board.evaluate_card_action(status, tags, action)
        assert actual == expected, (assignee, condition, action, actual)

    # ---- Explicit "unchanged for `me` / unassigned" spot-checks, per the
    # issue's call-out that this is the easiest rule to accidentally break.

    @pytest.mark.parametrize("assignee", [None, "me"])
    @pytest.mark.parametrize("target_lane", ["unassigned", "assigned", "in_progress", "human_queue", "done"])
    def test_me_and_unassigned_unclaimed_lane_moves_are_never_refused(self, assignee, target_lane):
        status, tags = _state_to_status_tags(assignee, "unclaimed")
        assert agent_board.evaluate_card_action(status, tags, "lane_move", target_lane) is None

    @pytest.mark.parametrize("assignee", [None, "me"])
    def test_me_and_unassigned_assignee_and_field_actions_never_refused_when_unclaimed(self, assignee):
        status, tags = _state_to_status_tags(assignee, "unclaimed")
        assert agent_board.evaluate_card_action(status, tags, "assignee_change") is None
        assert agent_board.evaluate_card_action(status, tags, "field_edit") is None

    @pytest.mark.parametrize("assignee", [None, "me"])
    def test_me_and_unassigned_cannot_cancel(self, assignee):
        # Cancel is agent-cards-only — `me`/unassigned refuse it regardless
        # of claimed state (there's no worker to tear down or reassign).
        status, tags = _state_to_status_tags(assignee, "unclaimed")
        assert agent_board.evaluate_card_action(status, tags, "cancel") == _CANCEL_NOT_AGENT_OWNED

    @pytest.mark.parametrize("assignee", ["claude", "codex", "hermes", "local"])
    def test_agent_engines_claimed_refuses_every_lane_but_allows_cancel(self, assignee):
        for condition in ("agent_running", "agent_blocked"):
            status, tags = _state_to_status_tags(assignee, condition)
            for lane in ("unassigned", "assigned", "in_progress", "human_queue", "done"):
                assert agent_board.evaluate_card_action(status, tags, "lane_move", lane) == _WORKER_OWNED
            assert agent_board.evaluate_card_action(status, tags, "assignee_change") == _WORKER_OWNED
            assert agent_board.evaluate_card_action(status, tags, "field_edit") == _WORKER_OWNED
            # Cancel is the one action still allowed on a claimed card —
            # it's how a human gets rid of it without dragging it anywhere.
            assert agent_board.evaluate_card_action(status, tags, "cancel") is None

    # ---- #881 AR4: a CLI-opened card (status="in_progress", no
    # agent-running tag) is claimed the same way a tag-based claim is.

    @pytest.mark.parametrize("assignee", ["claude", "codex", "hermes", "local"])
    def test_cli_opened_agent_card_is_claimed_same_as_agent_running(self, assignee):
        status, tags = _state_to_status_tags(assignee, "cli_opened")
        for lane in ("unassigned", "assigned", "in_progress", "human_queue", "done"):
            assert agent_board.evaluate_card_action(status, tags, "lane_move", lane) == _WORKER_OWNED, lane
        assert agent_board.evaluate_card_action(status, tags, "assignee_change") == _WORKER_OWNED
        assert agent_board.evaluate_card_action(status, tags, "field_edit") == _WORKER_OWNED
        # Cancel still works on a CLI-opened card, same as a tag-claimed one.
        assert agent_board.evaluate_card_action(status, tags, "cancel") is None

    def test_cli_opened_review_card_still_treated_as_review_not_claimed(self):
        """(#881 AR4 ordering) A card opened via a CLI session before the
        worker later completed it can retain `status == "in_progress"`
        (cli_session_event's guard only flips todo -> in_progress once)
        while also carrying `agent-completed` without `accepted` — this
        must still resolve as Review, not as claimed, or the accept-by-drag
        Done carve-out regresses."""
        status, tags = "in_progress", ["codex", "agent-completed"]
        assert agent_board.evaluate_card_action(status, tags, "lane_move", "done") is None
        assert agent_board.evaluate_card_action(status, tags, "lane_move", "in_progress") == _REVIEW_FIRST
        assert agent_board.evaluate_card_action(status, tags, "lane_move", "human_queue") == _REVIEW_FIRST
        assert agent_board.evaluate_card_action(status, tags, "assignee_change") is None
        assert agent_board.evaluate_card_action(status, tags, "field_edit") is None
        assert agent_board.evaluate_card_action(status, tags, "cancel") == _ACCEPT_OR_REJECT

    @pytest.mark.parametrize("assignee", [None, "me"])
    def test_cli_opened_status_on_me_or_unassigned_card_is_unaffected(self, assignee):
        """(#881 AR4) status == "in_progress" alone must never make a `me`
        or unassigned card look claimed — only an agent-owned assignee
        triggers the CLI-opened claim."""
        status, tags = _state_to_status_tags(assignee, "cli_opened")
        for lane in ("unassigned", "assigned", "in_progress", "human_queue", "done"):
            assert agent_board.evaluate_card_action(status, tags, "lane_move", lane) is None, lane
        assert agent_board.evaluate_card_action(status, tags, "assignee_change") is None
        assert agent_board.evaluate_card_action(status, tags, "field_edit") is None

    # ---- #881 RC1: evaluate_card_action must fail closed on an action it
    # doesn't recognize, not silently allow it.

    def test_unrecognized_action_raises_instead_of_silently_allowing(self):
        with pytest.raises(ValueError):
            agent_board.evaluate_card_action("todo", ["codex"], "delete_card")

    # ---- #881 RC2: cancel refuses an already-finished (done/cancelled)
    # agent-owned, non-review card, not just a review or non-agent-owned one.

    def test_cancel_refused_on_terminal_status_agent_owned_non_review_card(self):
        for status in ("done", "cancelled"):
            assert (
                agent_board.evaluate_card_action(status, ["codex"], "cancel")
                == agent_board.CANCEL_ALREADY_FINISHED_ERROR
            )
        # Positive case: a not-yet-terminal status on the same card is
        # still allowed — the new check is status-specific, not a blanket
        # "cancel is now half-broken" regression.
        assert agent_board.evaluate_card_action("todo", ["codex"], "cancel") is None
        assert agent_board.evaluate_card_action("in_progress", ["codex"], "cancel") is None
