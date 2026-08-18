# ADR-018: API Spend Requires Operator Consent

**Status:** Complete
**Last Updated:** 2026-08-18
**Decision:** Accepted
**Amends:** [ADR-008](008-managed-agents-cloud-routing.md) — routing signal #2 ("title cue infers cloud") no longer dispatches; it asks.

## Context

LifeOS can run a turn or a task on four kinds of engine, and they do not cost the same thing:

| Engine | Billing |
|--------|---------|
| Local `llama-server` (Gemma) | Free — the operator already bought the GPU |
| Claude Code CLI, Codex CLI | Flat subscription, no per-token charge |
| Anthropic Managed Agents, per-turn Anthropic models | **Per-token API credits** |

Three paths let LifeOS put work on the last row without the operator asking:

1. **The escalation ladder** (#303/#305c). On refusal+pushback the chat orchestrator climbed `[LIFEOS_AGENT_ESCALATION_MODEL, claude_code]`. The API model was rung 0 — the free engine only arrived on the second pushback.
2. **Preflight's inferred cloud route** ([ADR-008](008-managed-agents-cloud-routing.md), routing signal #2). A title implying a cloud connector — "search my gmail", "check my calendar" — routed an untagged `#agent` task straight to Managed Agents. The only brake was `agent_cost_confirm_threshold_dollars`, which fires above a $1 estimate, and the path is reachable from `#agent` tasks, `/agent <task>` in web chat, and operator spawns.
3. **Credential inheritance** (#578). The agent worker inherits the LifeOS `.env` through systemd's `EnvironmentFile`, which carries `ANTHROPIC_API_KEY` for the in-process API services. The CLI executors stripped only `CLAUDE*`, so the key reached the spawned `claude` process — and Claude Code prefers an API key over the claude.ai login. Every doctor session was API-billed while the code, and this repo's own docs, described the CLI routes as subscription-billed.

Path 3 made the cost model actively wrong rather than merely permissive. `inter_agent.spawn` exempts CLI routes from the per-task dollar ceiling *because* they are subscription-billed, so those sessions were both API-billed and uncapped. One 51-minute doctor run cost ≈$72 with no ceiling in play.

The common thread is that each path had LifeOS choosing the expensive engine on the operator's behalf, in a system whose free alternatives were already wired up and working.

## Decision

**LifeOS never selects an API-billed engine on its own. Explicit operator intent is the only thing that authorizes API spend.**

Three mechanisms, one per path:

1. **Automatic escalation is limited to non-API rungs** — `claude_code`, `codex`, `local`. The default ladder is `[claude_code, codex]`. An Anthropic model id in a configured `LIFEOS_AGENT_ESCALATION_LADDER` is filtered out of the climb with a log line rather than rejected, so existing configuration keeps working. `LIFEOS_AGENT_ESCALATION_MODEL` survives as the switch that says escalation is configured, and as the target for user-directed escalation.
2. **An inferred cloud route is downgraded to `ROUTE_ASK`.** Preflight reports whether the operator named the engine (`routing_explicit`), and that flag is corroborated against the title before any dispatch, so a hallucinated `true` cannot spend credits. The resulting confirmation offers `claude code` / `codex` / `local` / `cloud`, in that order, with the API option labelled as costing credits.
3. **The CLI subprocess gets no API credential.** `ClaudeCodeExecutor._clean_env` and `CodexExecutor._clean_env` strip `ANTHROPIC_*` alongside `CLAUDE*`, and a lineage rooted in a CLI session may not spawn a Managed Agents child.

**What still dispatches without a prompt**, because it is the operator asking: a `#cloud` / `#cloud-haiku` / `#cloud-sonnet` tag, `/agent claude <task>`, a title naming an engine or model, a `Sonnet`/`Opus` pick in the chat model picker, and a user-directed "escalate to opus" in a message.

The chat orchestrator's base model stays `claude-haiku-4-5` on the API. That is a configured default the operator chose in `.env`, not a choice LifeOS makes per query, and gating it would put a confirmation in front of ordinary use.

## Rationale

- **Asymmetric cost of being wrong.** A gate that fires unnecessarily costs one question. A gate that fails open costs money, silently, at a rate the operator discovers on a bill. Every ambiguous case therefore resolves toward the free engine — including a bare "claude" in a confirmation reply, which now means the CLI.
- **The free engines were already good enough.** `claude_code` and `codex` have the full tool catalog and a real browser; the cases that motivated automatic cloud escalation are cases they handle. The API was the default out of history, not capability.
- **Enforcement by omission survives refactoring.** Stripping the credential is not a policy the CLI consults — it is the absence of a thing to authenticate with. A future caller that reaches for `os.environ` cannot accidentally re-enable API billing, and the failure mode is a loud auth error rather than a quiet charge.
- **Inference is a guess, and guesses shouldn't spend.** ADR-008's title-cue heuristic is good at what it does, but "this title mentions gmail" is evidence about *capability*, not authorization. Splitting the two keeps the heuristic's usefulness (it still explains why the question is being asked) while removing its power to bill.
- **Consistency with the tag model.** `#cloud` already existed as the operator's way to say "use the API". Making that the *only* way makes the tag mean what it appears to mean.

## Alternatives Considered

### Confirm on cost estimate only (raise `agent_cost_confirm_threshold_dollars` to 0)

Keep automatic routing, but confirm every cloud dispatch whose preflight estimate exceeds zero.

**Rejected because:** it gates the wrong variable. The estimate is a guess layered on the routing guess, and cheap-looking tasks are exactly the ones that ran unattended and accumulated. It also confirms *after* the decision is made, framing the question as "this costs $0.30, proceed?" rather than "which engine?" — which hides that three free engines could do the job.

### Remove the Managed Agents path entirely

Delete `routing=claude` and run everything on the CLIs and local Gemma.

**Rejected because:** the connector story in [ADR-008](008-managed-agents-cloud-routing.md) still holds — Vault-authenticated Gmail/Calendar/Drive/Slack connectors are not reachable from a local model or a CLI session, and the operator sometimes genuinely wants them. The problem was never that the path exists; it was that LifeOS chose it unprompted.

### Trust the classifier's explicitness flag on its own

Let preflight's `routing_explicit` decide, without the title cross-check.

**Rejected because:** the flag is produced by the same LLM whose inference is being gated. A single hallucinated `true` would restore the original behavior invisibly, and nothing downstream would notice. The cross-check costs one regex and makes the failure mode deterministic.

### Ask once and remember the answer per task shape

Cache "yes, cloud is fine for gmail tasks" and stop asking.

**Rejected because:** it is a cache of an authorization decision keyed on a fuzzy similarity judgment — the same guess, one layer removed, now invisible because it fires without a prompt. If the recurring cost of the question becomes real, `#cloud` on the task already solves it explicitly.

## Consequences

### Positive

- No path remains by which LifeOS spends API credits without the operator having said so, and the CLI routes' documented "subscription-billed" property is now enforced rather than assumed — which also makes their exemption from the dollar ceiling correct.
- Escalation gets *cheaper and stronger* at once: a Claude Code handoff has tools and a browser, where the old rung 0 was a bigger model with the same tool catalog as the one that just refused.
- The confirmation surfaces the engine menu at the moment of choice, so the operator learns the four routes exist instead of discovering them in configuration docs.
- Automatic escalation now works identically on a fresh clone with no API key configured.

### Negative

- More questions. An untagged task whose title implies connectors now blocks on Telegram where it previously ran. `#cloud` is the escape hatch, but the operator has to learn to reach for it.
- `LIFEOS_AGENT_ESCALATION_MODEL` no longer means what its name says — it switches escalation on rather than naming the escalation target. Renaming it would break existing `.env` files, so the name stays and the description carries the correction.
- A configured ladder can now be silently shorter than written, since API rungs are filtered. The log line is the only signal.
- Preflight depends on one more classifier-emitted field. When the classifier omits it, everything cloud-routed becomes a question — the safe direction, but noisier.

## Related Documents

### Design Context

- [ADR-008](008-managed-agents-cloud-routing.md) — established the local/cloud split and the routing signals this ADR amends
- [ADR-009](009-llm-backend-toggle.md) — the `LIFEOS_LLM_BACKEND` switch that decides whether the orchestrator is on the API at all
- [ADR-007](007-linux-migration.md) — the local-first posture this restores at the routing layer

### Specifications

- [Agent worker (technical)](../specs/technical/agent-worker.md) — routing pipeline, the consent gate, budget enforcement
- [Agent worker (product)](../specs/product/agent-worker.md) — tags, the engine question, what the operator sees
- [Chat UI](../specs/product/chat-ui.md) — per-query escalation and engine handoff
- [Client surfaces](../specs/technical/client-surfaces.md) — the model picker's explicit-pick contract

### Operational

- [Configuration](../guides/configuration.md) — `LIFEOS_AGENT_ESCALATION_MODEL` / `_LADDER`
- [Doctor bot](../guides/doctor-bot.md) — the orchestrator whose sessions were API-billed
- [Claude Code orchestration](../guides/claude-code-orchestration.md) — CLI auth, and why an API key in the environment matters

### Code References

- `api/services/agent_loop.py` — `NON_API_RUNGS`, `DEFAULT_LADDER`, `_escalation_ladder`, `resolve_orchestrator_model`
- `api/services/agent_worker/preflight.py` — `routing_explicit`, `_TITLE_NAMES_A_CLOUD_ENGINE`, `_apply_tag_overrides`
- `api/services/agent_worker/worker.py` — `ROUTING_ASK_QUESTION`, `_parse_routing_answer`
- `api/services/agent_worker/claude_code_executor.py` — `_ALTERNATE_AUTH_ENV_PREFIXES`, `_clean_env`
- `api/services/agent_worker/inter_agent.py` — `spawn`'s `api_billing_blocked` guard
