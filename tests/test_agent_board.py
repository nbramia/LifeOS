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
# by round-2 findings 1 and 2)
#
# Dropping a card on a settable lane must either land it there, or be
# rejected for one of exactly three reasons derived straight from the
# starting tags — never "any 409 passes" (round-2 finding 12/2c):
#   * the worker owns the card (agent-running / agent-blocked present) and
#     the target is In progress or Done (round-2 finding 1)
#   * an agent-engine assignee tag is present (not yet claimed by the
#     worker) and the target is In progress (round-1 finding, unchanged)
#   * the card is a pending review (agent-completed, not yet accepted) and
#     the target is In progress or Human queue — Done still doubles as the
#     accept path (round-2 finding 2a)
# Assigned/unassigned are tags-only by design (an assignee change must not
# pull a card out of Human queue — the lane table says Human queue beats
# Assigned), so for those two targets this only checks the assignee-tag
# outcome and that `status` is left untouched, never landed-lane == target.
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

    @classmethod
    def _expected_error(cls, current_status, current_tags, target_lane):
        """The one legitimate rejection reason for this (state, target)
        pair, or None if the move must succeed. Derived independently of
        `plan_lane_move` so the test can't just echo the implementation."""
        tags_lower = {t.lstrip("#").lower() for t in current_tags}
        worker_owned = bool(tags_lower & {"agent-running", "agent-blocked"})
        current_assignee = agent_board.derive_assignee(current_tags)
        is_review = "agent-completed" in tags_lower and "accepted" not in tags_lower

        if target_lane in ("in_progress", "done") and worker_owned:
            return cls.WORKER_OWNED_ERROR
        if target_lane == "in_progress" and current_assignee in agent_board.AGENT_ASSIGNEES:
            return cls.AGENT_ASSIGNEE_ERROR
        if target_lane in ("in_progress", "human_queue") and is_review:
            return cls.REVIEW_ERROR
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
