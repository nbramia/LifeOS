"""Policy for the doctor repair workflow.

The durable records live in `session_store` (`doctor_repairs`,
`goal_proposals`, `sessions.workflow_id`). This module holds the decisions
taken over them:

* which LifeOS-owned dispatches the single human gate blocks before approval,
* how a structured worker result maps onto the phase ladder, and
* what evidence a repair must carry before it may be called `shipped`.

Evidence is consumed from the systems that already produce it — the candidate
verifier's lane result and the deployment verifier's `RuntimeEvidence` — so
there is no second verifier, deployer, or doctor-specific review policy here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from api.services.agent_worker.session_store import (
    REPAIR_DEPLOYING,
    REPAIR_IMPLEMENTING,
    REPAIR_MERGING,
    REPAIR_PREAPPROVAL_PHASES,
    REPAIR_REVIEWING,
    REPAIR_SHIPPED,
    REPAIR_TERMINAL_PHASES,
    REPAIR_VERIFYING,
)

# One machine-readable line a repair worker emits to report structured
# results, matching the repository's existing `SYNC_STATS:{json}` convention.
# Prose describing success carries no marker and therefore advances nothing.
RESULT_MARKER = "LIFEOS_REPAIR_RESULT:"
_RESULT_LINE = re.compile(rf"^\s*{re.escape(RESULT_MARKER)}\s*(\{{.*\}})\s*$", re.MULTILINE)

# The goal-approval prompt registered against a pending question is the goal
# body followed by the reply instructions. Splitting on the instruction lead-in
# recovers the condition from a question row alone, so an approval whose repair
# record has yet to be opened can adopt a revision without its transcript.
_INSTRUCTION_LEAD_IN = "Reply to this message with"

# The persona whose sessions are self-repair runs. A session carrying it —
# as `persona_id` or as the `bot` its notices route through — owns a repair
# record from the moment it is created; every other session, including an
# ordinary `#agent` task that happens to emit a `[GOAL]`, owns none and is
# never gated here.
DOCTOR_PERSONA_ID = "doctor"

# LifeOS-owned dispatches that the single human gate covers. Spawning a worker
# to implement the goal is the one such dispatch LifeOS makes on a repair's
# behalf: merging, deploying, and restarting are shell work the supervisor
# does inside its own session, which this gate deliberately does not sandbox.
# Anything else — investigating, diagnosing, proposing a goal — is read-only
# orchestration and stays available before approval.
GATED_ACTIONS = frozenset({"implement"})


def is_doctor_session(persona_id: str | None, bot: str | None) -> bool:
    """Whether a session belongs to the doctor, and so to a repair run."""
    return DOCTOR_PERSONA_ID in {persona_id, bot}


@dataclass(frozen=True)
class GateDecision:
    """Whether one LifeOS-owned dispatch may start for a repair."""

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class ShipDecision:
    """Whether the collected evidence proves the approved goal actually shipped."""

    shipped: bool
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairTransition:
    """The phase a structured result moves a repair to, and its evidence."""

    applied: bool
    phase: str = ""
    waiting_reason: str | None = None
    evidence: dict = field(default_factory=dict)
    reason: str = ""


def goal_resume_action(condition: str) -> dict:
    """The resume payload for the native CLI: its own goal command.

    Stored on the proposal at proposal time, so approval replays an exact
    recorded action instead of reconstructing one from a reply's wording.
    """
    return {"kind": "goal_command", "payload": f"/goal {condition}"}


def resume_message(resume_action: Mapping[str, Any] | None) -> str | None:
    """The executor input for an approved proposal's stored resume action."""
    if not resume_action:
        return None
    payload = resume_action.get("payload")
    if not isinstance(payload, str) or not payload.strip():
        return None
    return payload


def condition_from_question(question: str) -> str:
    """Recover a proposed condition from the goal-approval prompt text.

    Empty unless the prompt carries the instruction lead-in that separates the
    goal body from the reply mechanics: a prompt without that boundary offers
    no reliable way to tell condition from instructions, and the caller falls
    back to the condition the executor recorded.
    """
    text = question or ""
    if _INSTRUCTION_LEAD_IN not in text:
        return ""
    return text.split(_INSTRUCTION_LEAD_IN)[0].strip()


def dispatch_allowed(repair: Mapping[str, Any] | None, action: str) -> GateDecision:
    """Whether `action` may be dispatched for `repair` right now.

    A session with no repair is an ordinary worker task and is never gated
    here. Read-only investigation is allowed in every phase. An implementation
    dispatch requires an approved goal revision and a live repair. This is an
    orchestration gate over LifeOS's own dispatch boundaries, not a sandbox
    over what a CLI process can do once running.
    """
    if repair is None:
        return GateDecision(True)
    if action not in GATED_ACTIONS:
        return GateDecision(True)
    phase = repair.get("phase")
    if phase in REPAIR_TERMINAL_PHASES:
        return GateDecision(False, f"repair_{phase}")
    if phase in REPAIR_PREAPPROVAL_PHASES or not repair.get("approved_proposal_id"):
        return GateDecision(False, "repair_awaiting_approval")
    return GateDecision(True)


def parse_result(text: str | None) -> dict | None:
    """The structured repair result carried by a worker's final text, if any."""
    if not text:
        return None
    match = None
    for match in _RESULT_LINE.finditer(text):
        pass
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _shas_match(left: Any, right: Any) -> bool:
    return (
        isinstance(left, str) and isinstance(right, str)
        and bool(left) and left.strip().lower() == right.strip().lower()
    )


def evaluate_shipped(
    evidence: Mapping[str, Any], *, approved_version: int | None,
) -> ShipDecision:
    """Whether `evidence` proves the approved goal revision reached production.

    Every clause names a way a repair can look finished without being finished:
    a result for a different goal revision, a verification that never ran, a
    review that never passed, a deployment whose running process is still on
    the old revision, a failed restart or health check, or prose with no
    structured result behind it at all.
    """
    missing: list[str] = []

    if approved_version is None:
        missing.append("no_approved_goal")
    elif evidence.get("goal_version") != approved_version:
        missing.append("goal_revision_mismatch")

    prs = evidence.get("pull_requests")
    if not isinstance(prs, list) or not prs:
        missing.append("no_pull_requests")

    review = evidence.get("review")
    if not isinstance(review, Mapping):
        missing.append("review_missing")
    elif review.get("outcome") != "approved":
        missing.append("review_not_approved")

    merge = evidence.get("merge")
    merged_commit = merge.get("merged_commit") if isinstance(merge, Mapping) else None
    if not isinstance(merged_commit, str) or not merged_commit:
        missing.append("merge_missing")

    # The candidate verifier's own lane result, relayed verbatim. It is pinned
    # to the candidate it ran over — its `candidate_id` — not to a commit: the
    # verifier emits no commit identity, so requiring one here could only ever
    # be satisfied by a sha the reporting agent typed itself. What binds the
    # repair to the merged commit is the deployment evidence below, whose
    # revisions come from the running processes rather than from prose. A
    # bundle with no `candidate_id` is not a verifier result at all — an
    # agent-authored review comment produces no such record.
    verification = evidence.get("verification")
    if not isinstance(verification, Mapping):
        missing.append("verification_missing")
    else:
        candidate_id = verification.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            missing.append("verification_not_candidate_pinned")
        if verification.get("result") != "success":
            missing.append("verification_failed")

    # RuntimeEvidence from the deployment verifier. `expected_revision` is the
    # checkout HEAD; the observed identities are the revisions the processes
    # are actually running, and both must be the merged commit.
    deployment = evidence.get("deployment")
    if not isinstance(deployment, Mapping):
        missing.append("deployment_missing")
    else:
        if not deployment.get("accepted"):
            missing.append("deployment_not_accepted")
        if merged_commit and not _shas_match(
            deployment.get("expected_revision"), merged_commit,
        ):
            missing.append("deploy_head_mismatch")
        if deployment.get("restart_result") != "success":
            missing.append("restart_failed")
        if not deployment.get("health_ok"):
            missing.append("health_check_failed")
        observed = deployment.get("observed")
        if not isinstance(observed, Mapping) or not observed:
            missing.append("running_revision_unknown")
        elif merged_commit and not all(
            isinstance(identity, Mapping)
            and _shas_match(identity.get("revision"), merged_commit)
            for identity in observed.values()
        ):
            missing.append("running_revision_stale")

    handle = evidence.get("revert_handle")
    if not isinstance(handle, str) or not handle.strip():
        missing.append("revert_handle_missing")

    return ShipDecision(shipped=not missing, missing=tuple(missing))


def _reached_phase(evidence: Mapping[str, Any]) -> str:
    """The furthest rung the evidence bundle actually reaches."""
    deployment = evidence.get("deployment")
    if isinstance(deployment, Mapping):
        observed = deployment.get("observed")
        if isinstance(observed, Mapping) and observed:
            return REPAIR_VERIFYING
        return REPAIR_DEPLOYING
    merge = evidence.get("merge")
    if isinstance(merge, Mapping) and merge.get("merged_commit"):
        return REPAIR_MERGING
    if evidence.get("verification") or evidence.get("review") or evidence.get("pull_requests"):
        return REPAIR_REVIEWING
    return REPAIR_IMPLEMENTING


def apply_result(
    repair: Mapping[str, Any], result: Mapping[str, Any],
) -> RepairTransition:
    """Fold one structured result into a repair's phase and evidence.

    A result is applied only to a live repair that has an approved revision,
    and only when it reports that same revision. `shipped` is reachable solely
    through `evaluate_shipped`; anything short of it leaves the repair on the
    rung its evidence actually reaches, with the first unmet requirement as the
    waiting reason.
    """
    phase = repair.get("phase")
    if phase in REPAIR_TERMINAL_PHASES:
        return RepairTransition(False, reason=f"repair_{phase}")
    approved_version = repair.get("approved_version")
    if approved_version is None or not repair.get("approved_proposal_id"):
        return RepairTransition(False, reason="repair_awaiting_approval")
    if result.get("goal_version") != approved_version:
        return RepairTransition(False, reason="goal_revision_mismatch")

    evidence = dict(repair.get("evidence") or {})
    evidence.update({k: v for k, v in result.items() if k != "phase"})

    decision = evaluate_shipped(evidence, approved_version=approved_version)
    if decision.shipped:
        return RepairTransition(True, phase=REPAIR_SHIPPED, evidence=evidence)
    return RepairTransition(
        True,
        phase=_reached_phase(evidence),
        waiting_reason=decision.missing[0],
        evidence=evidence,
    )
