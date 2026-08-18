# ADR-008: Managed Agents Cloud Routing for the Agent Worker

**Status:** Complete
**Last Updated:** 2026-05-27
**Decision:** Accepted
**Amended by:** [ADR-018](018-api-spend-requires-consent.md) — routing signal #2 (title cue infers cloud) now asks instead of dispatching; API spend requires explicit operator intent.

## Context

[ADR-007](007-linux-migration.md) established a local-LLM-first posture for LifeOS: orchestration and synthesis default to a local `llama-server` running on the operator's GPU. The agent worker (`#agent`-tagged Obsidian tasks running autonomously — see [agent-worker product spec](../specs/product/agent-worker.md)) initially shipped local-only on the same model.

Two pressures emerged that local-only couldn't satisfy:

1. **Cloud connectors.** Many useful agent tasks ("draft a reply to last week's intro thread", "find every meeting that mentioned the Q3 launch", "post to the partnership Slack channel") require Gmail, Calendar, Drive, Slack, Asana, etc. Those connectors live in Anthropic's Vault as OAuth-authenticated MCP servers — they cannot be wired to a local LLM without re-implementing OAuth flows for each service and re-creating the connector inventory.
2. **Model capability for ambiguous tasks.** The agent loop is sensitive to model quality on judgment calls (which tool to call next, when to stop, how to phrase a clarification). For tasks where the operator wants frontier-model behavior, falling back to a hosted model is the simpler path than fine-tuning a local one.

The decision is **how** to route between local and cloud — what triggers the choice, how the two paths share infrastructure, what new attack surface cloud routing introduces, and how the cost model is exposed to the operator.

## Decision

Add a second execution path to the agent worker: **Anthropic Managed Agents** (`api.anthropic.com/v1/sessions`). Route between local and cloud at preflight time, on three signals (in order of precedence):

1. **Explicit tag** — `#local` or `#cloud` on the task forces the corresponding path.
2. **Title cue** — phrases like "draft an email", "check my calendar", "search my gmail" infer cloud (those need connectors); "with local agent" / "using gemma" force local.
3. **Ask the operator** — if the preflight can't determine routing, the task parks at `#agent-blocked` and the worker asks via Telegram.

The Managed Agents path uses:

- **MCP HTTP transport** for LifeOS tool access (`config/systemd/lifeos-mcp-http.service`, bearer-token-gated, exposed via Tailscale Funnel). The cloud session needs to reach LifeOS tools from outside the operator's LAN.
- **Anthropic Vault** for cloud-only connector OAuth credentials (Gmail, Calendar, Drive, Slack, etc.) — provisioned by the operator out-of-band in the Anthropic Console.
- **Anthropic Console agent preset** that bundles the Vault connectors, system prompt, and model choice. The preset name is referenced from `.env`; the actual agent configuration lives in the Anthropic Console.

Both paths share the same session store, transcript store, spend tracker, preflight, inter-agent coordination, and Telegram delivery. The split is at the executor level (`local_executor.py` vs `managed_executor.py`); everything above and below them is shared.

## Rationale

- **Connector authenticatability.** Cloud-only connectors aren't authenticatable to a local agent. Routing tasks that need them to Managed Agents is the only path that doesn't require re-implementing OAuth for every connector.
- **Frontier model quality** for ambiguous judgment calls. Local Gemma is fine for deterministic tasks; for "decide what to do here", a hosted Claude Sonnet 4.6 has noticeably better tool selection and stopping behavior.
- **Shared infrastructure.** The session store, transcripts, spend ledger, and inter-agent tools work identically for both paths. The operator's mental model is one worker, two executors — not two parallel systems.
- **Operator-controlled cost surface.** Per-task `$`/wall/token budgets and the global daily $-cap apply identically to both paths. The flat `$0.08/hour` Managed-session-hour overhead (per [Anthropic pricing](https://platform.claude.com/docs/en/managed-agents/overview), announced April 2026) is baked into the cost calculation in `pricing.py:MANAGED_SESSION_HOUR_OVERHEAD`, so budget enforcement accounts for it without operator intervention.
- **Aligned with [ADR-007](007-linux-migration.md)'s hybrid model.** ADR-007 already established that specialized Claude API calls remain on Anthropic while orchestration runs locally. Agent worker cloud routing is the same pattern at a different layer.

## Alternatives Considered

### Local-only with mock connectors

Build local stubs that fake Gmail/Calendar/Drive responses for development, with a roadmap to re-implement the real connectors against local OAuth flows.

**Rejected because:** Re-implementing N OAuth flows + connector inventories + per-service rate-limit handling + token refresh logic is a multi-quarter project. Anthropic Vault solves the connector problem today. The mock approach also blocks users from doing the actual tasks they want (drafting real emails, checking real calendars).

### Third-party agent runtime (LangGraph cloud, OpenAI Assistants)

Use an external agent platform instead of Anthropic's Managed Agents.

**Rejected because:** Two reasons. First, the LifeOS MCP server is already authored to Anthropic's MCP conventions — running against an Anthropic-native runtime is the lowest-impedance path. Second, third-party runtimes add a vendor dependency that LifeOS hasn't otherwise taken on. The fewer external runtimes that see operator data, the smaller the privacy surface.

### Cloud-only routing (drop the local path)

Route every `#agent` task through Managed Agents.

**Rejected because:** It gives up the privacy and zero-cost guarantees that motivated [ADR-007](007-linux-migration.md) in the first place. Many tasks (vault grepping, file authoring, local CLI work) don't need cloud connectors and shouldn't pay the Managed-session-hour fee or send their context off-machine. The local path stays the default for that class of work.

### Single-executor with a connector shim layer

Keep one executor but introduce a "connector adapter" layer that transparently routes Gmail/Calendar/etc. calls through Anthropic-hosted MCP while keeping the agent loop local.

**Rejected because:** Routing only the tool calls externally still requires the local agent to reason about each cloud connector's response shape, latency, and failure modes — and the agent loop itself isn't where the model-quality gap lives. Splitting at the executor level (each path has its own end-to-end agent runtime) is cleaner than splitting at the tool-call level.

## Consequences

### Positive

- Operator can complete tasks that require cloud connectors (Gmail/Calendar/Drive/Slack/etc.) without re-implementing OAuth for each service.
- Frontier model available for ambiguous tasks where local model quality is the bottleneck.
- Cost surface is explicit and bounded: per-task budgets and a global daily cap, with session-hour overhead included in the math.
- Both executors share state, transcripts, and inter-agent tools — operator sees one queue, one transcript stream, one Telegram interface regardless of routing.
- Routing is transparent: operator can override via tag, accept the preflight's inference, or be asked.

### Negative

- New per-session-hour billing dimension. Long-idle Managed sessions accrue `$0.08/hr` even when no model calls happen — motivated the `yield-and-resume` pattern (see [agent worker — session state machine](../specs/technical/agent-worker.md#session-state-machine)) so sessions don't sit idle paying the overhead.
- MCP HTTP transport is now a load-bearing piece of agent-worker infrastructure. The bearer token must remain secret, the Tailscale Funnel exposure must remain narrow (only the `/mcp` endpoint), and kill/resume endpoints must **not** be exposed through it.
- Operator must provision Anthropic Console artifacts out-of-band: a Vault with connector OAuth credentials, an agent preset, and an environment binding. This is documented in [agent-worker-setup.md](../guides/agent-worker-setup.md) but adds setup steps a local-only deployment doesn't need.
- Cloud sessions run in an Anthropic-managed ephemeral container by default — the agent doesn't have the operator's local filesystem. For tasks that need both cloud connectors and local file access, the operator chooses one or splits the task.
- More moving parts to monitor: Managed session creation can fail (4xx), session events polling has its own failure modes, and rate limits are now in the cost picture.
- Routing inference can be wrong. A task title that doesn't clearly cue local or cloud may pause indefinitely until the operator answers the Telegram clarification.

## Related Documents

### Design Context
- [ADR-007: Linux Migration](007-linux-migration.md) — Established the local-default posture this ADR adds the cloud path to
- [ADR-009: LIFEOS_LLM_BACKEND toggle](009-llm-backend-toggle.md) — The parallel local/cloud toggle for chat synthesis and orchestration (separate code path; same hybrid-model philosophy)

### Specifications
- [Agent Worker (product)](../specs/product/agent-worker.md) — Consumer view: tag conventions, budgets, lifecycle, capability boundaries
- [Agent Worker (technical)](../specs/technical/agent-worker.md) — Implementation: executor split, MCP transport, session state machine, restart resumability

### Operational
- [Agent Worker Setup](../guides/agent-worker-setup.md) — Operator setup including Anthropic Vault, preset, MCP HTTP transport, Tailscale Funnel

### Code References
- [`api/services/agent_worker/managed_executor.py`](../../api/services/agent_worker/managed_executor.py) — Managed session lifecycle (`start()` → `poll()` → `_finalize_remote()`)
- [`api/services/agent_worker/managed_driver.py`](../../api/services/agent_worker/managed_driver.py) — HTTP wrapper for `api.anthropic.com/v1/sessions` + events
- [`api/services/agent_worker/router.py`](../../api/services/agent_worker/router.py) — Thin local-vs-cloud dispatch helper
- [`api/services/agent_worker/preflight.py`](../../api/services/agent_worker/preflight.py) — Haiku classifier producing the routing decision
- [`api/services/agent_worker/pricing.py`](../../api/services/agent_worker/pricing.py) — Per-model pricing table; `MANAGED_SESSION_HOUR_OVERHEAD = 0.08`
- [`config/systemd/lifeos-mcp-http.service`](../../config/systemd/lifeos-mcp-http.service) — Bearer-gated MCP HTTP transport used by Managed Agents
