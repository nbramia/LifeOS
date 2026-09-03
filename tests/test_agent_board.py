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
# plan_lane_move — landing lane invariant (#850 round-1 finding 1)
#
# Dropping a card into Done or In progress must actually land it there — a
# leftover Human queue / Review / agent-running tag must not pull it back to
# its old lane once `derive_lane` re-runs. One case per lane-table starting
# lane, crossed with each of the two target lanes this finding fixed.
# assigned/unassigned are deliberately excluded: those targets are tags-only
# by design (an assignee change must not pull a card out of Human queue —
# the lane table says Human queue beats Assigned), so asserting
# landed-lane == target for them would fight that intended behavior.
# ---------------------------------------------------------------------------

class TestPlanLaneMoveLandsInTargetLane:
    STARTING_STATES = {
        "unassigned": ("todo", []),
        "assigned": ("todo", ["codex"]),
        "in_progress": ("in_progress", ["codex", "agent", "agent-running"]),
        "human_queue_human_tag": ("todo", ["human"]),
        "human_queue_agent_blocked": ("todo", ["agent-blocked"]),
        "human_queue_blocked_status": ("blocked", []),
        "review": ("done", ["codex", "agent-completed"]),
        "done": ("done", ["codex", "accepted"]),
    }

    @pytest.mark.parametrize("target_lane", ["in_progress", "done"])
    @pytest.mark.parametrize("start_name,start", list(STARTING_STATES.items()))
    def test_lands_in_target_or_is_rejected(self, start_name, start, target_lane):
        current_status, current_tags = start
        plan = agent_board.plan_lane_move(current_status, current_tags, target_lane, "me")
        if plan.error is not None:
            # Only a legitimate rejection: the worker owns agent-assigned
            # cards, so those can't be dragged straight to In progress.
            assert plan.error[0] == 409, (start_name, target_lane, plan.error)
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

    def test_done_strips_agent_blocked_and_agent_running(self):
        plan = agent_board.plan_lane_move(
            "blocked", ["codex", "agent", "agent-blocked", "agent-running"], "done", None,
        )
        assert plan.status == "done"
        assert set(plan.tags) == {"codex", "agent"}

    def test_done_appends_accepted_when_agent_completed_present(self):
        plan = agent_board.plan_lane_move("done", ["codex", "agent-completed"], "done", None)
        assert plan.status == "done"
        assert set(plan.tags) == {"codex", "agent-completed", "accepted"}

    def test_done_does_not_double_append_accepted(self):
        plan = agent_board.plan_lane_move(
            "done", ["codex", "agent-completed", "accepted"], "done", None,
        )
        assert plan.tags is None or plan.tags.count("accepted") == 1

    def test_in_progress_strips_human_and_agent_blocked(self):
        plan = agent_board.plan_lane_move("todo", ["human"], "in_progress", None)
        assert plan.status == "in_progress"
        assert "human" not in plan.tags

        plan2 = agent_board.plan_lane_move("todo", ["agent-blocked"], "in_progress", None)
        assert plan2.status == "in_progress"
        assert "agent-blocked" not in plan2.tags


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
