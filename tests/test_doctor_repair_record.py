"""Durable doctor repair record: versioned goals, approval idempotency, the
pre-approval dispatch gate, and what evidence proves a repair shipped.

Every goal here is synthetic ("merge the parser fix and verify the service
health check"); no repository, GitHub, or deployment is contacted.
"""
from __future__ import annotations

import pytest

from api.services.agent_worker import doctor_repair
from api.services.agent_worker.session_store import (
    PROPOSAL_APPROVED,
    PROPOSAL_DECLINED,
    PROPOSAL_PROPOSED,
    PROPOSAL_SUPERSEDED,
    REPAIR_AWAITING_APPROVAL,
    REPAIR_CANCELLED,
    REPAIR_DECLINED,
    REPAIR_DEPLOYING,
    REPAIR_IMPLEMENTING,
    REPAIR_SHIPPED,
    REPAIR_VERIFYING,
    SessionStore,
)

pytestmark = pytest.mark.unit

GOAL ="merge the parser fix and verify the service health check"
REFINED_GOAL = "merge the parser fix, verify the health check, and restart the worker"
MERGED_SHA = "a" * 40
STALE_SHA = "b" * 40


@pytest.fixture
def store(tmp_path):
    return SessionStore(db_path=tmp_path / "sessions.db")


def _propose(store, workflow_id=None, condition=GOAL, question_id=None):
    if workflow_id is None:
        workflow_id = store.create_repair()["workflow_id"]
    proposal = store.propose_goal(
        workflow_id,
        condition=condition,
        resume_action=doctor_repair.goal_resume_action(condition),
        pending_question_id=question_id,
    )
    return workflow_id, proposal


def shipped_evidence(goal_version=1, **overrides):
    """A complete, internally consistent evidence bundle for one repair."""
    evidence = {
        "goal_version": goal_version,
        "pull_requests": [4321],
        "review": {"outcome": "approved", "rounds": 1},
        "verification": {
            "candidate_id": "cand-synthetic",
            "evidence_key": "key-synthetic",
            "lanes": ["fast-unit", "browser-free"],
            "result": "success",
            "source_sha": MERGED_SHA,
        },
        "merge": {"pr": 4321, "merged_commit": MERGED_SHA},
        "deployment": {
            "accepted": True,
            "expected_revision": MERGED_SHA,
            "restart_result": "success",
            "health_ok": True,
            "observed": {
                "lifeos-api": {"service": "lifeos-api", "revision": MERGED_SHA},
                "lifeos-agent-worker": {
                    "service": "lifeos-agent-worker", "revision": MERGED_SHA,
                },
            },
            "revert_ref": STALE_SHA,
        },
        "revert_handle": "gh pr revert 4321",
    }
    evidence.update(overrides)
    return evidence


class TestProposalVersioning:
    def test_first_proposal_is_version_one_and_awaits_approval(self, store):
        workflow_id, proposal = _propose(store)
        assert proposal["version"] == 1
        assert proposal["status"] == PROPOSAL_PROPOSED
        assert proposal["condition"] == GOAL
        assert proposal["resume_action"] == {"kind": "goal_command", "payload": f"/goal {GOAL}"}
        repair = store.get_repair(workflow_id)
        assert repair["phase"] == REPAIR_AWAITING_APPROVAL
        assert repair["waiting_reason"] == "goal_approval"

    def test_refinement_supersedes_prior_and_creates_next_version(self, store):
        workflow_id, first = _propose(store)
        _, second = _propose(store, workflow_id, condition=REFINED_GOAL)

        assert second["version"] == 2
        assert store.get_proposal(first["proposal_id"])["status"] == PROPOSAL_SUPERSEDED
        assert store.get_proposal(second["proposal_id"])["status"] == PROPOSAL_PROPOSED

    def test_only_the_current_version_can_start_execution(self, store):
        workflow_id, first = _propose(store)
        _, second = _propose(store, workflow_id, condition=REFINED_GOAL)

        assert store.approve_goal(first["proposal_id"]) is None
        assert store.get_repair(workflow_id)["phase"] == REPAIR_AWAITING_APPROVAL

        approved = store.approve_goal(second["proposal_id"])
        assert approved["status"] == PROPOSAL_APPROVED
        repair = store.get_repair(workflow_id)
        assert repair["phase"] == REPAIR_IMPLEMENTING
        assert repair["approved_proposal_id"] == second["proposal_id"]
        assert repair["approved_version"] == 2

    def test_approval_is_consumed_once(self, store):
        _, proposal = _propose(store)
        assert store.approve_goal(proposal["proposal_id"]) is not None
        assert store.approve_goal(proposal["proposal_id"]) is None

    def test_declined_goal_launches_nothing(self, store):
        workflow_id, proposal = _propose(store)
        assert store.decline_goal(proposal["proposal_id"]) is not None

        repair = store.get_repair(workflow_id)
        assert repair["phase"] == REPAIR_DECLINED
        assert repair["approved_proposal_id"] is None
        assert store.get_proposal(proposal["proposal_id"])["status"] == PROPOSAL_DECLINED
        assert store.approve_goal(proposal["proposal_id"]) is None

    def test_a_delivered_repair_takes_follow_up_work_as_a_new_revision(self, store):
        workflow_id, first = _propose(store)
        store.approve_goal(first["proposal_id"])
        store.set_repair_phase(workflow_id, REPAIR_SHIPPED)

        follow_up = store.propose_goal(
            workflow_id, condition=REFINED_GOAL,
            resume_action=doctor_repair.goal_resume_action(REFINED_GOAL),
        )
        assert follow_up["version"] == 2
        repair = store.get_repair(workflow_id)
        assert repair["phase"] == REPAIR_AWAITING_APPROVAL
        assert doctor_repair.dispatch_allowed(repair, "implement").allowed is False

    def test_a_cancelled_repair_accepts_no_new_revision(self, store):
        workflow_id, _ = _propose(store)
        store.cancel_repair(workflow_id)
        assert store.propose_goal(
            workflow_id, condition=REFINED_GOAL,
            resume_action=doctor_repair.goal_resume_action(REFINED_GOAL),
        ) is None
        assert store.get_repair(workflow_id)["phase"] == REPAIR_CANCELLED

    def test_question_resolves_to_exactly_one_revision(self, store):
        workflow_id, first = _propose(store, question_id=11)
        _, second = _propose(store, workflow_id, condition=REFINED_GOAL, question_id=12)

        assert store.get_proposal_by_question_id(11)["proposal_id"] == first["proposal_id"]
        assert store.get_proposal_by_question_id(12)["proposal_id"] == second["proposal_id"]


class TestPreApprovalDispatchGate:
    """The orchestration gate over LifeOS's own dispatch boundaries. It says
    what LifeOS starts on a repair's behalf, not what a running CLI process is
    permitted to do, so read-only investigation stays available throughout."""

    def test_implementation_dispatch_is_refused_before_approval(self, store):
        workflow_id, _ = _propose(store)
        decision = doctor_repair.dispatch_allowed(store.get_repair(workflow_id), "implement")
        assert decision.allowed is False
        assert decision.reason == "repair_awaiting_approval"

    @pytest.mark.parametrize("action", ["implement", "merge", "deploy", "restart"])
    def test_every_gated_action_is_refused_before_approval(self, store, action):
        workflow_id, _ = _propose(store)
        assert doctor_repair.dispatch_allowed(
            store.get_repair(workflow_id), action,
        ).allowed is False

    @pytest.mark.parametrize("action", ["investigate", "diagnose", "propose"])
    def test_read_only_investigation_stays_available(self, store, action):
        workflow_id, _ = _propose(store)
        assert doctor_repair.dispatch_allowed(
            store.get_repair(workflow_id), action,
        ).allowed is True

    def test_approval_opens_the_gate(self, store):
        workflow_id, proposal = _propose(store)
        store.approve_goal(proposal["proposal_id"])
        assert doctor_repair.dispatch_allowed(
            store.get_repair(workflow_id), "implement",
        ).allowed is True

    def test_a_session_outside_a_repair_is_never_gated(self):
        assert doctor_repair.dispatch_allowed(None, "implement").allowed is True

    def test_cancelled_repair_dispatches_nothing(self, store):
        workflow_id, proposal = _propose(store)
        store.approve_goal(proposal["proposal_id"])
        store.cancel_repair(workflow_id)
        decision = doctor_repair.dispatch_allowed(store.get_repair(workflow_id), "merge")
        assert decision.allowed is False
        assert decision.reason == "repair_cancelled"


class TestShippedEvidence:
    """A repair is `shipped` only on evidence that the approved revision is the
    revision running in production."""

    def test_complete_evidence_ships(self):
        decision = doctor_repair.evaluate_shipped(shipped_evidence(), approved_version=1)
        assert decision.shipped is True
        assert decision.missing == ()

    def test_prose_only_success_ships_nothing(self):
        decision = doctor_repair.evaluate_shipped(
            {"summary": "All done, everything merged and deployed."}, approved_version=1,
        )
        assert decision.shipped is False
        assert "no_pull_requests" in decision.missing

    def test_stale_commit_verification_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["verification"] = {**evidence["verification"], "source_sha": STALE_SHA}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "verification_stale_commit" in decision.missing

    def test_failed_verification_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["verification"] = {**evidence["verification"], "result": "failure"}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "verification_failed" in decision.missing

    def test_review_comment_alone_does_not_ship(self):
        """An agent-authored review comment is not verification evidence."""
        evidence = shipped_evidence()
        evidence.pop("verification")
        evidence["review"] = {"outcome": "approved", "source": "pr_comment"}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "verification_missing" in decision.missing

    def test_unapproved_review_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["review"] = {"outcome": "escalated"}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "review_not_approved" in decision.missing

    def test_failed_restart_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["deployment"] = {**evidence["deployment"], "restart_result": "failure"}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "restart_failed" in decision.missing

    def test_failed_health_check_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["deployment"] = {**evidence["deployment"], "health_ok": False}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "health_check_failed" in decision.missing

    def test_rejected_deployment_does_not_ship(self):
        evidence = shipped_evidence()
        evidence["deployment"] = {**evidence["deployment"], "accepted": False}
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "deployment_not_accepted" in decision.missing

    def test_checkout_head_matching_while_running_revision_is_stale(self):
        """The checkout is on the merged commit; one process is still on the
        old revision. That is a deployment failure, not a ship."""
        evidence = shipped_evidence()
        evidence["deployment"] = {
            **evidence["deployment"],
            "observed": {
                "lifeos-api": {"service": "lifeos-api", "revision": MERGED_SHA},
                "lifeos-agent-worker": {
                    "service": "lifeos-agent-worker", "revision": STALE_SHA,
                },
            },
        }
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "running_revision_stale" in decision.missing

    def test_merge_without_deployment_does_not_ship(self):
        evidence = shipped_evidence()
        evidence.pop("deployment")
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "deployment_missing" in decision.missing

    def test_missing_revert_handle_does_not_ship(self):
        evidence = shipped_evidence()
        evidence.pop("revert_handle")
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "revert_handle_missing" in decision.missing

    def test_result_for_another_goal_revision_does_not_ship(self):
        decision = doctor_repair.evaluate_shipped(
            shipped_evidence(goal_version=1), approved_version=2,
        )
        assert decision.shipped is False
        assert "goal_revision_mismatch" in decision.missing


class TestDeploymentEvidenceContract:
    """The deployment half of the bundle is the deployment verifier's own
    output, so it is built here with that verifier rather than by hand."""

    @staticmethod
    def _identity(service, revision):
        from api.services.runtime_identity import RuntimeIdentity

        return RuntimeIdentity(
            service=service, revision=revision, clean=True, process_id=4242,
            startup_id="startup-synthetic", source_root="/srv/synthetic",
            started_at_utc="2026-01-01T00:00:00Z", process_start_time=1.0,
        )

    def _evidence(self, running_revision):
        from api.services.runtime_identity import evaluate_runtime_evidence

        return evaluate_runtime_evidence(
            expected_revision=MERGED_SHA,
            target_services=["lifeos-api"],
            restart_result="success",
            observations={"lifeos-api": self._identity("lifeos-api", running_revision)},
            health_ok=True,
            revert_ref=STALE_SHA,
        ).as_dict()

    def test_verifier_output_satisfies_the_shipped_requirements(self):
        evidence = shipped_evidence(deployment=self._evidence(MERGED_SHA))
        assert doctor_repair.evaluate_shipped(evidence, approved_version=1).shipped is True

    def test_verifier_output_for_a_stale_running_process_does_not_ship(self):
        evidence = shipped_evidence(deployment=self._evidence(STALE_SHA))
        decision = doctor_repair.evaluate_shipped(evidence, approved_version=1)
        assert decision.shipped is False
        assert "running_revision_stale" in decision.missing


class TestApplyResult:
    def _approved_repair(self, store, version=1):
        workflow_id = store.create_repair()["workflow_id"]
        proposal = None
        for _ in range(version):
            proposal = store.propose_goal(
                workflow_id,
                condition=GOAL,
                resume_action=doctor_repair.goal_resume_action(GOAL),
            )
        store.approve_goal(proposal["proposal_id"])
        return workflow_id

    def test_complete_result_moves_the_repair_to_shipped(self, store):
        workflow_id = self._approved_repair(store)
        transition = doctor_repair.apply_result(
            store.get_repair(workflow_id), shipped_evidence(),
        )
        assert transition.applied is True
        assert transition.phase == REPAIR_SHIPPED
        assert transition.waiting_reason is None

    def test_failed_deployment_is_a_deployment_failure_not_shipped(self, store):
        workflow_id = self._approved_repair(store)
        evidence = shipped_evidence()
        evidence["deployment"] = {**evidence["deployment"], "restart_result": "failure"}
        transition = doctor_repair.apply_result(store.get_repair(workflow_id), evidence)
        assert transition.applied is True
        assert transition.phase == REPAIR_VERIFYING
        assert transition.waiting_reason == "restart_failed"

    def test_deploy_started_without_observed_processes_stays_deploying(self, store):
        workflow_id = self._approved_repair(store)
        evidence = shipped_evidence()
        evidence["deployment"] = {
            **evidence["deployment"], "observed": {}, "health_ok": False,
        }
        transition = doctor_repair.apply_result(store.get_repair(workflow_id), evidence)
        assert transition.phase == REPAIR_DEPLOYING

    def test_a_result_asserting_its_own_phase_does_not_set_it(self, store):
        """The phase is derived from evidence, so a worker claiming to be
        shipped without the evidence for it is not."""
        workflow_id = self._approved_repair(store)
        evidence = shipped_evidence()
        evidence.pop("deployment")
        evidence["phase"] = REPAIR_SHIPPED
        transition = doctor_repair.apply_result(store.get_repair(workflow_id), evidence)
        assert transition.phase != REPAIR_SHIPPED
        assert "phase" not in transition.evidence

    def test_result_for_an_unapproved_revision_is_rejected(self, store):
        workflow_id = self._approved_repair(store)
        transition = doctor_repair.apply_result(
            store.get_repair(workflow_id), shipped_evidence(goal_version=7),
        )
        assert transition.applied is False
        assert transition.reason == "goal_revision_mismatch"

    def test_no_result_advances_a_repair_before_approval(self, store):
        workflow_id, _ = _propose(store)
        transition = doctor_repair.apply_result(
            store.get_repair(workflow_id), shipped_evidence(),
        )
        assert transition.applied is False
        assert transition.reason == "repair_awaiting_approval"

    def test_cancelled_repair_is_not_revived_by_a_late_result(self, store):
        workflow_id = self._approved_repair(store)
        store.cancel_repair(workflow_id, "operator killed the run")
        transition = doctor_repair.apply_result(
            store.get_repair(workflow_id), shipped_evidence(),
        )
        assert transition.applied is False
        assert transition.reason == "repair_cancelled"

    def test_set_repair_phase_refuses_to_reopen_a_terminal_repair(self, store):
        workflow_id = self._approved_repair(store)
        store.cancel_repair(workflow_id)
        assert store.set_repair_phase(workflow_id, REPAIR_IMPLEMENTING) is False
        assert store.get_repair(workflow_id)["phase"] == REPAIR_CANCELLED


class TestRoutinePhaseChanges:
    def test_routine_phase_advance_keeps_the_same_approval(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        proposal = store.propose_goal(
            workflow_id, condition=GOAL,
            resume_action=doctor_repair.goal_resume_action(GOAL),
        )
        store.approve_goal(proposal["proposal_id"])

        for phase in (REPAIR_IMPLEMENTING, REPAIR_DEPLOYING, REPAIR_VERIFYING):
            assert store.set_repair_phase(workflow_id, phase) is True
            repair = store.get_repair(workflow_id)
            assert repair["approved_proposal_id"] == proposal["proposal_id"]
            assert repair["approved_version"] == 1

    def test_expanded_outcome_needs_a_newly_approved_revision(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        first = store.propose_goal(
            workflow_id, condition=GOAL,
            resume_action=doctor_repair.goal_resume_action(GOAL),
        )
        store.approve_goal(first["proposal_id"])
        store.set_repair_scope(workflow_id, {"repository": "LifeOS"})

        # A materially larger outcome is a new revision, and until it is
        # approved the repair is back at the gate.
        second = store.propose_goal(
            workflow_id, condition=REFINED_GOAL,
            resume_action=doctor_repair.goal_resume_action(REFINED_GOAL),
        )
        assert second["version"] == 2
        repair = store.get_repair(workflow_id)
        assert repair["phase"] == REPAIR_AWAITING_APPROVAL
        assert doctor_repair.dispatch_allowed(repair, "merge").allowed is False

        store.approve_goal(second["proposal_id"])
        repair = store.get_repair(workflow_id)
        assert repair["approved_version"] == 2
        assert doctor_repair.dispatch_allowed(repair, "merge").allowed is True


class TestEventIdempotency:
    def test_a_repeated_event_is_claimed_once(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        assert store.claim_repair_event(workflow_id, "sess-1:attempt-1:turn-1") is True
        assert store.claim_repair_event(workflow_id, "sess-1:attempt-1:turn-1") is False

    def test_distinct_events_each_claim_once(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        assert store.claim_repair_event(workflow_id, "sess-1:attempt-1:turn-1") is True
        assert store.claim_repair_event(workflow_id, "sess-1:attempt-1:turn-2") is True
        assert store.claim_repair_event(workflow_id, "sess-1:attempt-1:turn-2") is False

    def test_an_event_id_is_not_matched_as_a_prefix_of_another(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        assert store.claim_repair_event(workflow_id, "turn-1") is True
        assert store.claim_repair_event(workflow_id, "turn-10") is True


class TestResultParsing:
    def test_a_structured_result_line_is_parsed(self):
        text = (
            "Shipped the parser fix.\n"
            'LIFEOS_REPAIR_RESULT:{"goal_version": 1, "pull_requests": [4321]}\n'
        )
        assert doctor_repair.parse_result(text) == {
            "goal_version": 1, "pull_requests": [4321],
        }

    def test_prose_carries_no_result(self):
        assert doctor_repair.parse_result("Merged and deployed, all green.") is None

    def test_the_latest_result_line_wins(self):
        text = (
            'LIFEOS_REPAIR_RESULT:{"goal_version": 1, "pull_requests": [1]}\n'
            'LIFEOS_REPAIR_RESULT:{"goal_version": 1, "pull_requests": [2]}\n'
        )
        assert doctor_repair.parse_result(text)["pull_requests"] == [2]

    def test_malformed_json_is_not_a_result(self):
        assert doctor_repair.parse_result("LIFEOS_REPAIR_RESULT:{not json}") is None


class TestSessionLinking:
    def test_a_session_links_to_one_repair(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        store.create(task_id="task-link", routing="claude_code", origin="operator")
        assert store.link_session_to_repair("task-link", workflow_id) is True
        assert store.get("task-link").workflow_id == workflow_id
        assert store.link_session_to_repair("task-link", workflow_id) is True

    def test_a_session_cannot_be_repointed_at_another_repair(self, store):
        first = store.create_repair()["workflow_id"]
        second = store.create_repair()["workflow_id"]
        store.create(task_id="task-link-2", routing="claude_code", origin="operator")
        store.link_session_to_repair("task-link-2", first)
        assert store.link_session_to_repair("task-link-2", second) is False
        assert store.get("task-link-2").workflow_id == first

    def test_cancelling_the_root_session_cancels_the_repair(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        session = store.create(
            task_id="task-root", routing="claude_code", origin="operator",
            workflow_id=workflow_id,
        )
        assert store.mark_cancelled(
            "task-root", attempt_id=session.attempt_id, reason="operator kill",
        ) is True
        assert store.get_repair(workflow_id)["phase"] == REPAIR_CANCELLED

    def test_cancelling_a_child_session_leaves_the_repair_alive(self, store):
        workflow_id = store.create_repair()["workflow_id"]
        root = store.create(
            task_id="task-root-2", routing="claude_code", origin="operator",
            workflow_id=workflow_id,
        )
        child = store.create(
            task_id="task-child", routing="claude_code",
            parent_session_id=root.session_id, root_session_id=root.session_id,
            workflow_id=workflow_id,
        )
        store.mark_cancelled("task-child", attempt_id=child.attempt_id)
        assert store.get_repair(workflow_id)["phase"] != REPAIR_CANCELLED


class TestLegacySessionsAreUnaffected:
    def test_an_ordinary_session_has_no_workflow(self, store):
        session = store.create(task_id="task-ordinary", routing="local")
        assert session.workflow_id is None
        assert store.get("task-ordinary").workflow_id is None

    def test_a_database_without_the_workflow_column_migrates_in_place(self, tmp_path):
        """A store opened over a sessions table that lacks `workflow_id` gains
        the column and its index, keeping the existing row readable."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE sessions (
                    task_id                   TEXT PRIMARY KEY,
                    session_id                TEXT UNIQUE NOT NULL,
                    status                    TEXT NOT NULL,
                    routing                   TEXT,
                    budget_json               TEXT,
                    started_at                INTEGER NOT NULL,
                    last_activity_at          INTEGER NOT NULL,
                    total_input_tokens        INTEGER NOT NULL DEFAULT 0,
                    total_output_tokens       INTEGER NOT NULL DEFAULT 0,
                    total_dollars             REAL    NOT NULL DEFAULT 0.0,
                    expected_output           TEXT,
                    parent_session_id         TEXT,
                    root_session_id           TEXT,
                    spawn_depth               INTEGER NOT NULL DEFAULT 0,
                    yield_waiting_for         TEXT,
                    managed_agent_session_id  TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO sessions (task_id, session_id, status, started_at, "
                "last_activity_at) VALUES (?, ?, ?, ?, ?)",
                ("legacy-task", "sess_legacy", "completed", 1000, 1000),
            )
            conn.commit()
        finally:
            conn.close()

        migrated = SessionStore(db_path=db_path)
        assert migrated.get("legacy-task").workflow_id is None
        workflow_id = migrated.create_repair()["workflow_id"]
        assert migrated.link_session_to_repair("legacy-task", workflow_id) is True
        assert migrated.get("legacy-task").workflow_id == workflow_id
