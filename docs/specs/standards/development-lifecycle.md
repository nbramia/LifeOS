# Development Lifecycle

**Status:** Partial
**Last Updated:** 2026-09-09
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

Routine work may complete these phases inline or with one proportionate review.
The lifecycle does not require a delegate for every task, impose a hard PR
line-count cap, or create a separate documentation specialist for ordinary
changes.

## Risk and review

Classify risk by the behavior and failure impact, not by file count:

| Work | Minimum review | Verification expectation |
| --- | --- | --- |
| Typo or mechanical correction | Brief inline review | Focused check when observable; otherwise document-only result |
| Small isolated Python bug | One proportionate review | Focused regression test and selected lane evidence |
| Frontend behavior change | One proportionate review | Browser or executable UI scenario plus selected lane evidence |
| Concurrency or resource ownership | Independent adversarial reviewer from the other model family | Deterministic contention/cancellation/ownership evidence |
| Schema or public API change | Independent adversarial reviewer from the other model family | Compatibility, migration, and boundary evidence |
| Substantive review correction | Re-review the changed behavior | Re-run affected focused checks; preserve prior evidence where valid |
| Infrastructure retry | Same review requirement as original work | Record the reason, command, result, and whether the retry is comparable |

Complex work requires an identifiable reviewer from the other model family
(Claude reviews OpenAI work, and OpenAI reviews Claude work). The reviewer
must inspect executable evidence and record findings and disposition. If the
other family is unavailable, readiness remains pending unless the operator
grants an explicit exception; a same-family review is not relabeled as
independent. Re-review substantive fixes, not formatting-only changes.

Use specialists only for a concrete risk such as security, architecture,
concurrency, privacy, schema, or test strategy. Ordinary documentation
relevance is part of the proportionate review. Unresolved defects block
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
reason to guess or silently rerun a broad suite.

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
| Complex concurrency | Other-family adversarial review and deterministic evidence required |
| Schema/API change | Other-family adversarial review and compatibility evidence required |
| Mechanical review correction | Recheck affected behavior; no new specialist unless risk changes |
| Substantive review correction | Re-review the changed behavior and rerun affected checks |
| Infrastructure failure | Preserve failure; retry only with an explicit reason |
| Other-family unavailable | Readiness pending unless an explicit operator exception exists |

## Privacy and boundaries

Use synthetic identifiers and payloads in tests and lifecycle artifacts. Never
publish environment dumps, credentials, private absolute paths, or personal
data. The contract does not create a public API, a general scheduler, or a
mandatory every-task delegation pipeline. Existing forced garbage collection,
GPU guards, and deployment controls remain owned by their current components.

## Related Documents

### Specifications

- [Testing Standards](testing-standards.md) — Lane selection and evidence rules

### Code References

- [Project Instructions](../../../AGENTS.md) — Repository-wide workflow and privacy invariants
- [Claude Instructions](../../../CLAUDE.md) — Claude harness entry points
