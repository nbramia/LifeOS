# ADR-026: A Budget Breach Asks; It Does Not Fail

**Status:** Complete
**Last Updated:** 2026-09-20
**Decision:** Accepted

## Context

Agent task budgets bound three things: wall-clock time, token count, and dollars. Before this decision, any breach — on an in-process route (local Gemma, the flag-gated remote fallback, or Managed Agents) — ended the session outright: status flipped to `budget_exceeded`, the vault card swapped to `#agent-budget-exceeded`, and the operator got a one-way notice after the fact. There was no way to say "keep going" without re-tagging the card and losing whatever context the session had built up, since a fresh claim starts a new attempt rather than resuming the old conversation.

With the token cap now opt-in and the dollar default sized as a real backstop rather than a quota (the change this ADR's issue opened with), a breach should be rarer — but when it does happen, ending the session outright is still the wrong response to "this task is taking longer or costing more than expected." Most of the time the operator would have said yes to a few more dollars or another hour; the old contract never gave them the chance to say so before the work was already thrown away.

## Decision

A budget breach on an in-process route (local, the remote-forced route, or Managed Agents) parks the session instead of ending it:

- The executor ends its turn with session status `yielded` (the same status a sleep uses), not `budget_exceeded`. The conversation already in the session store is left untouched — nothing is cleared or re-seeded.
- The worker turns that yield into a pending question of kind `budget`, sent through the same Telegram/Hermes channel a mid-task clarification already uses. The question names the breached dimension, the spend (dollars to two decimals and active minutes — never a raw token count), and the cap.
- The vault card's tag swaps to `agent-blocked` — the same tag a clarification uses — so the card lands in the Human queue lane through the existing tag-based lane derivation, even though the session's own status is `yielded`, not `blocked`. These two facts are allowed to diverge: `session.status` says what a resume needs to know (there is a stored, unfinished turn to continue), and the vault tag says where the card belongs on the board.
- Replying `yes` doubles the breached cap and resumes the session from its stored conversation on the same route; `yes $12` / `yes 90 min` set the cap to a specific value instead. `stop` finalizes exactly as an unattended breach used to — same `budget_exceeded` status, same `#agent-budget-exceeded` tag, same cut-off notice — proving that path still exists rather than only ever being reachable by accident. An unparseable reply gets a short usage note and leaves the session parked. No reply at all leaves it parked indefinitely, at zero cost: `resume_pending`'s existing unconditional skip of every `yielded` session (there for sleeps) already covers a budget-parked one too, and a parked Managed Agents session's remote handle is left alive but untouched — `poll()` skips it while its question is open, so it costs nothing either.
- Managed Agents is the one route whose "resume" doesn't call the executor again: it never posts a new message or kills the remote session on breach, so extending the cap is just flipping status back to `RUNNING` and letting the ordinary poll loop continue a conversation that was never actually interrupted.
- A spawned child can only ever breach the lineage-aggregate dollar cap (the check that finds a breach explicitly excludes a session that is its own root), and that's the one dimension a child is allowed to ask about even though children otherwise have no operator-facing channel — the cap it's asking to raise is the family's, carried on the lineage root's own `budget_json`, not anything the child owns itself. Every other dimension on a spawned child keeps the pre-existing terminal behavior: there's no operator channel to ask through, and the parent already consumes a child's terminal outcome as an internal event.
- The Claude Code and Codex routes are unaffected — they carry no wall/token/dollar enforcement today and this decision doesn't add any.

## Rationale

- **Reuses the clarification round-trip instead of inventing a second one.** A `budget` question is exactly a `pending_questions` row of a different `kind`, sent through `ask_user_via_telegram`/`_ask_user_via_hermes` (both already used for clarifications), and read back through the same `_process_clarification_answers` dispatch that already branches on `kind`. The board drawer's Continue/Stop buttons post the same `yes`/`stop` text a free-text Answer would.
- **`yielded`, not a new status.** A budget-parked session and a sleeping session are the same shape from the store's point of view: non-terminal, no provider calls, waiting for an external event to resume it. Giving budget its own status would have meant teaching every place that already treats `yielded` as "safe to leave alone on restart" (`resume_pending`, the lineage cascade, the board's lane derivation) about a second non-terminal status with the same properties.
- **The tag and the session status are allowed to disagree.** `session.status == "yielded"` is what an in-process resume path checks before touching the conversation again; the vault tag is what the board reads to place the card. Keeping them as two independent signals, rather than forcing the tag to imply a specific session status, is what lets the SAME tag (`agent-blocked`) serve both a genuine `STATUS_BLOCKED` clarification and a `STATUS_YIELDED` budget park without the board needing to know the difference.
- **`stop` reuses the old terminal path exactly, rather than reimplementing it.** Feeding a synthetic `ExecutorOutcome(status=STATUS_BUDGET_EXCEEDED, ...)` through the same `_handle_outcome` a real terminal breach used to hit means the tag swap, the vault status, and the cut-off notice's wording are one code path with one set of tests, not two that could drift.
- **A parked family member doesn't get killed to protect the family budget.** The prior lineage-breach behavior cascade-killed every descendant the instant one of them tripped the root's aggregate cap — a blunt instrument that discarded every sibling's in-progress work for a cap the operator might raise in one word. Yielding the one session that noticed, and letting any other descendant notice independently on its own next check, costs nothing extra once parked and preserves everyone's context for a cap increase.

## Alternatives Considered

### Give a budget park its own status (`budget_yielded`) instead of reusing `yielded`

A dedicated status would make a budget park visible as its own thing wherever session status is inspected, without needing to also check `termination_evidence`.

**Rejected because:** every place that currently treats `STATUS_YIELDED` as "leave this alone, it's parked and safe" — `resume_pending`'s startup sweep, the lineage-descendant scan, `lane_for_session`'s status-to-lane map — would need to learn about a second non-terminal status with identical handling, for a distinction that only matters at the one point (`_handle_outcome`) that decides whether to ask a question. Carrying the distinction in `termination_evidence["budget_breach"]` (present only on a budget yield) gets the same information to the one place that needs it without widening the status enum.

### Keep the vault tag and session status in lock-step (write `STATUS_BLOCKED` for a budget park, matching the `agent-blocked` tag)

Mirrors the existing clarification flow exactly: `BLOCKED_TAG` on the card, `STATUS_BLOCKED` on the session.

**Rejected because:** `resume_pending`'s crash-recovery sweep treats `STATUS_BLOCKED` as "an operator clarification wait, safe to leave" but treats `STATUS_YIELDED` the same way for a different reason (a sleep or a budget park) — both end up skipped either way, so this wouldn't have changed startup-recovery behavior. What it would have broken is `execute()`'s own resume path: a `STATUS_BLOCKED` session re-entering `execute()` is read by existing code as "this was blocked awaiting a clarification answer that's about to be injected as a new user turn," which is a different shape than "just extend the cap and keep running the turn that was already in progress." `STATUS_YIELDED` was already the correct signal for "there's a stored, unfinished turn — just keep going."

### Cascade-kill the rest of a lineage on a `lineage_max_dollars` breach, same as before, and only ask about the single session that detected it

Keep the family-wide kill as an immediate safety net, and let the one descendant that noticed the breach ask on its own behalf.

**Rejected because:** killing siblings the instant one descendant notices a shared cap is exhausted directly contradicts the same decision's own premise for every other dimension — that a breach should ask before discarding work, not discard first and ask second. A killed sibling's conversation is gone before the operator even sees the question; raising the cap in reply no longer helps it. Leaving siblings running costs nothing once each of them independently reaches its own next budget check and parks the same way — the cap has already stopped increasing (nothing gets a fresh reservation while parked), so the family's total spend is bounded by whatever was already in flight, not further growth.

## Consequences

### Positive

- An operator no longer loses a session's work and full conversation to a budget cap they would have happily raised — replying `yes` (or naming an amount) resumes exactly where the session left off.
- The existing terminal `budget_exceeded` outcome (tag, notice, and vault status) is preserved, exactly reachable via `stop`, rather than only a change in when it's *reached*, so nothing downstream of that terminal state (dashboards, `#agent-budget-exceeded` tag consumers) needs to change.
- A budget-parked session costs strictly nothing beyond what it had already spent — no polling, no provider calls, no re-dispatch — for as long as it sits unanswered.

### Negative

- `session.status` and the vault tag can now genuinely disagree (`yielded` + `agent-blocked`) in a way no other flow produces, which is one more shape a future reader of either field has to know about rather than assuming the two always move together.
- A `lineage_max_dollars` breach with no sibling cascade-kill means the family's aggregate spend can, in principle, keep climbing very slightly past the cap while multiple descendants independently notice and park on their own separate schedules — bounded (nothing gets to spend indefinitely once parked), but not the hard, instant stop the old cascade-kill provided.
- The Managed Agents route's "resume" leaves a killed-nothing remote session sitting idle (still provisioned, still accruing the per-session-hour overhead already charged elsewhere in this codebase) for as long as the question goes unanswered — a real, if usually small, ongoing cost the local/remote routes don't share, since those routes have no idle infrastructure to keep alive while parked.

## Related Documents

### Design Context
- [ADR-024: Remote Provider as a Third LIFEOS_LLM_BACKEND Value](024-remote-llm-backend.md) — the priced remote route this backstop change most affects
- [ADR-025: Specialist Calls Fall Back When No Anthropic Key Is Set](025-specialist-call-fallback.md) — the most recent prior ADR in this area, for numbering context only (no direct relationship)

### Specifications
- [Agent Worker (Product)](../specs/product/agent-worker.md) — Budgets and Notifications sections describe the operator-facing contract this ADR implements
- [Agent Worker (Technical)](../specs/technical/agent-worker.md) — the session status table and budget-enforcement section carry the implementation detail
- [Agent Viz (Product)](../specs/product/agent-viz.md) — the board drawer's Continue/Stop affordance for a `budget` question

### Operational
- [Human Queue Guide](../guides/human-queue.md) — how a `budget` question surfaces as a Human-queue card

### Code References
- [`api/services/agent_worker/local_executor.py`](../../api/services/agent_worker/local_executor.py) — `_finalize_budget_yielded`, the between-turn breach checks
- [`api/services/agent_worker/managed_executor.py`](../../api/services/agent_worker/managed_executor.py) — the Managed Agents dollar/token breach and `poll`'s parked-session guard
- [`api/services/agent_worker/worker.py`](../../api/services/agent_worker/worker.py) — `_ask_budget_question`, `_resume_budget`, `_extend_budget`, `_finalize_budget_stop`
- [`api/services/agent_worker/session_store.py`](../../api/services/agent_worker/session_store.py) — `update_budget`, `has_open_budget_question`, the `pending_questions.kind` column
- [`api/services/agent_board.py`](../../api/services/agent_board.py) — tag-based lane derivation, unchanged but load-bearing for this decision
- [`web/agents/session_actions.js`](../../web/agents/session_actions.js) — Continue/Stop on a `budget` pending question
