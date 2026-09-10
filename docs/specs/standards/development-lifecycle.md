# Development Lifecycle

**Status:** Partial
**Last Updated:** 2026-09-10
**Owner:** Development workflow

This contract defines the shared implementation, review, verification, and
merge decisions for LifeOS. Harnesses provide mechanics; they read this
contract and the repository instructions at runtime rather than carrying a
second copy of LifeOS policy.

## Lifecycle phases

Every change receives the smallest lifecycle that fits its risk:

1. Understand the request, repository instructions, and affected behavior.
2. Implement with focused tests and a proportionate self-review.
3. Review the resulting diff and evidence, then address accepted defects.
4. Run the applicable verification lane and preserve its result for the exact
   candidate under test.
5. Merge only after required evidence, approvals, and repository checks pass.

Work outside the six risk triggers in [Risk and review](#risk-and-review)
completes these phases inline, with one proportionate review standing in for
phase 3 in full. The lifecycle does not require a delegate for every task,
impose a hard PR line-count cap, or create a separate documentation
specialist or verification findings loop for that work.

## Risk and review

Classify risk by the behavior and failure impact, not by file count. Six
triggers require an independent adversarial review — someone other than the
implementer, reading the diff adversarially against what it claims to do:

- Behavior only observable in the running application
- Concurrency or resource ownership
- Schema or public API compatibility
- Authentication or privacy boundaries
- Money movement
- Irreversible data loss

| Work | Minimum review | Verification expectation |
| --- | --- | --- |
| Typo or mechanical correction | Brief inline review | Focused check when observable; otherwise document-only result |
| Small isolated Python bug | One proportionate review | Focused regression test and selected lane evidence |
| Frontend behavior change | One proportionate review | Browser or executable UI scenario plus selected lane evidence |
| Behavior only observable in the running application | Independent adversarial review | Executable scenario driving the running application, not module-level tests alone |
| Concurrency or resource ownership | Independent adversarial review | Deterministic contention/cancellation/ownership evidence |
| Schema or public API compatibility | Independent adversarial review | Compatibility, migration, and boundary evidence |
| Authentication or privacy boundaries | Independent adversarial review | Boundary and authorization evidence |
| Money movement | Independent adversarial review | Correctness and idempotency evidence for the affected transaction path |
| Irreversible data loss | Independent adversarial review | Evidence that the destructive path is gated, confirmed, or recoverable |
| Substantive review correction | Re-review the changed behavior | Re-run affected focused checks; preserve prior evidence where valid |
| Infrastructure retry | Same review requirement as original work | Record the reason, command, result, and whether the retry is comparable |

For work outside those six triggers, one proportionate review is the
complete review requirement. No separate documentation specialist and no
separate verification findings loop apply to that work — documentation
relevance and verification evidence are considerations inside the one
review, not additional gates with their own rounds.

A review reports exactly two kinds of finding: a defect reachable on a path
a caller or user actually takes, and a missing test for behavior the diff
claims to deliver. An observation below that bar — a stylistic preference, an
unproven hypothetical, a rephrasing with no behavior change — is not
recorded as a finding.

Review converges when a round produces no accepted finding; that is the
default outcome after the first round. A further round happens only because
the preceding round produced an accepted finding — a round never exists to
reconfirm that nothing is wrong. An accepted finding is fixed in the branch
under review when it is in scope, not deferred to a follow-up pull request
that pays a fresh gate run and review cycle.

An independent reviewer from the other model family (Claude reviewing OpenAI
work, or OpenAI reviewing Claude work) is advisory: when available, it
informs the review; its absence never places readiness in a pending state
and never requires an operator exception. A same-family independent review
still satisfies the six triggers above. Re-review substantive fixes, not
formatting-only changes.

Use specialists only for a concrete risk such as security, architecture,
concurrency, privacy, schema, or test strategy. Unresolved defects block
readiness; optional, unrelated work is not a readiness blocker.

## Verification and evidence

Verification evidence identifies a candidate with opaque run, task, and
candidate labels and records the lane, result, duration, worker count, cache
reuse, and exact command outcome. Evidence is current only when it matches the
same source candidate, lane definition, environment/resource contract, and
required command boundaries. A reused receipt is not a new attempt and does
not justify a second telemetry or verification implementation. A green
reviewer checkmark or an unrun benchmark/schema-only remote-support claim is
never itself completion evidence — evidence means an actually executed
command's preserved result, not the absence of an objection.

One verification phase owns each authoritative broad command or ordered plan
and records it at most once for the exact candidate. Implementers, reviewers,
documentation checks, PR standards checks, and merge preparation may run
focused commands but do not consume or rerun that authoritative plan. Broad
gates remain required until an approved enforcement change records a safe
reuse policy. Missing or mismatched evidence is a readiness failure, not a
reason to guess or silently rerun a broad suite. Verification preserves this
evidence for the exact candidate; it does not run a separate multi-round
findings loop of its own — anything it surfaces is a finding under [Risk and
review](#risk-and-review) and is resolved within that one review.

Verification separates queue/waiting, execution, and transfer time. It
preserves positive and signal-derived exit status, cancellation, interruption,
infrastructure failure, and cache reuse. It reports unsupported measurements
as unsupported rather than manufacturing timing or memory data. Repeated
timings are unprofiled unless a separate profiler run is explicitly requested.

## Convergence and merge

The task has one convergence bound covering implementation, review, correction,
and verification. The bound is tracked across all harnesses and cannot be
reset, hidden, or bypassed by rewriting history. When the bound is reached or
progress stalls, preserve the findings and request a scope or operator
decision; do not erase history with a branch reset. A coherent change may be
large, while a split is warranted when independent concerns cannot be reviewed
together. PR size is advisory unless a concrete cohesion or reviewability
problem is demonstrated.

Merge readiness requires the current candidate, repository-required checks,
accepted review state, and matching verification evidence. Publication and
post-publication history are immutable under normal operation; no `--no-verify`
or main-history rewrite is a lifecycle escape hatch. Production restart is a
deployment concern only; isolated testing never restarts production.

## Scenario outcomes

Harness adapters must produce equivalent normalized acceptance, verification,
and merge-readiness outcomes for the same synthetic scenario. Only delegation,
tool invocation, and output formatting may differ between Claude, Codex, and a
generic compatible adapter.

The bounded evaluation matrix is:

| Scenario | Expected outcome |
| --- | --- |
| Typo | Routine; inline review; no mandatory delegate |
| Small Python bug | Routine; focused regression evidence |
| Frontend behavior | Routine; executable browser/UI evidence |
| Behavior only observable in the running application | Independent adversarial review; executable evidence from the running application |
| Concurrency or resource ownership | Independent adversarial review and deterministic evidence required |
| Schema or public API compatibility | Independent adversarial review and compatibility evidence required |
| Authentication or privacy boundaries | Independent adversarial review and boundary evidence required |
| Money movement | Independent adversarial review and transaction-path evidence required |
| Irreversible data loss | Independent adversarial review and recoverability evidence required |
| Mechanical review correction | Recheck affected behavior; no new specialist unless risk changes |
| Substantive review correction | Re-review the changed behavior and rerun affected checks |
| Infrastructure failure | Preserve failure; retry only with an explicit reason |
| Other-family reviewer unavailable | A same-family independent review satisfies the trigger; readiness proceeds |

## Privacy and boundaries

Use synthetic identifiers and payloads in tests and lifecycle artifacts. Never
publish environment dumps, credentials, private absolute paths, or personal
data. The contract does not create a public API, a general scheduler, or a
mandatory every-task delegation pipeline. Existing forced garbage collection,
GPU guards, and deployment controls remain owned by their current components.

## Related Documents

### Specifications

- [Testing Standards](testing-standards.md) — Lane selection and evidence rules
- [Documentation Strategy](../../AGENTS.md) — Pull-request description expectations that reference this contract's risk triggers

### Code References

- [Project Instructions](../../../AGENTS.md) — Repository-wide workflow and privacy invariants
- [Claude Instructions](../../../CLAUDE.md) — Claude harness entry points
