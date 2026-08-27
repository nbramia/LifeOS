# ADR-009: LIFEOS_LLM_BACKEND Toggle for Synthesis and Orchestration

**Status:** Complete
**Last Updated:** 2026-05-27
**Decision:** Superseded
**Superseded By:** [ADR-022](022-remote-llm-backend.md) — a third `remote` backend value is added alongside `anthropic`/`local`; both original values are unchanged.
**Amended by:** [ADR-023](023-specialist-call-fallback.md) — the "specialized calls retained on Anthropic regardless of toggle" clause no longer holds when no Anthropic key is configured.

## Context

[ADR-007](007-linux-migration.md) established **local LLM orchestration** as LifeOS's default — chat synthesis, intent orchestration, and agentic tool-calling all run against a local `llama-server` on the operator's GPU, with Claude API used only for specialized calls (relationship insights, fact extraction, web search). This was the right default for the LifeOS maintainer's hardware tier.

In practice, operators have meaningfully different deployments:

- Some operators don't have a high-VRAM GPU and want Anthropic by default — for them, requiring a local model is a hard blocker.
- Some operators (including the maintainer) prefer local by default for privacy and zero marginal cost.
- A single operator may want to switch backends mid-debug to compare local vs cloud behavior on the same query.

ADR-007 mentioned `LIFEOS_LLM_BACKEND` as part of its design but treated it as an implementation detail. In hindsight the toggle is a **load-bearing architectural choice**: it determines what model handles every chat turn, where personal data flows on each query, what cost surface the operator is exposed to, and what calibration is required when prompts evolve.

A unified wrapper (`api/services/llm_client.py`) was also built as part of this work. It hides the Anthropic/OpenAI shape differences — tool definitions, tool-call response shapes, system-message handling, streaming format — so tool definitions and prompts are written once and translated at call time. The wrapper's existence is what makes the toggle viable; without it, switching backends would require forking the orchestrator. This wrapper deserves its own architectural record alongside the toggle.

## Decision

`LIFEOS_LLM_BACKEND` is a first-class operator-facing knob with two values:

- `local` — Route synthesis and orchestration to a local OpenAI-compatible `llama-server`. Model determined by which server is running on `http://localhost:8080`.
- `anthropic` — Route synthesis and orchestration to Anthropic's API. Model determined by `LIFEOS_ANTHROPIC_MODEL` (default `claude-haiku-4-5`).

The toggle controls **only synthesis + orchestration + intent classification**. Specialized calls retained on Anthropic regardless of toggle:

- Relationship insights (Opus-class reasoning)
- Fact extraction (Sonnet-class reliability for structured output)
- Tone analysis
- Web search (Anthropic's built-in `web_search_20250305` tool has no local equivalent)

A unified wrapper at `api/services/llm_client.py` translates between Anthropic and OpenAI tool formats at call time. Tool definitions and prompts are backend-agnostic; the wrapper handles:

- **Tool schema translation** — Anthropic `tool_use` blocks ↔ OpenAI `tool_calls`. Wrapper functions like `openai_tool_calls_to_anthropic` and the assistant-message converter normalize both sides.
- **System message handling** — Anthropic separates `system` from `messages`; OpenAI inlines as a `role: "system"` message.
- **Response-shape normalization** — `stop_reason` field is mapped (e.g., `tool_use` → `tool_calls`) so callers see one shape.
- **Streaming events** — both backends' streaming chunks are mapped to a single event vocabulary.

This is an **amendment** to [ADR-007](007-linux-migration.md), not a supersession. ADR-007's other decisions (Linux primary platform, systemd, agent-worker as a separate process, retaining the external venv convention) all remain.

## Rationale

- **Separation of concerns.** Orchestrator logic shouldn't fork by backend. With the wrapper, tools and prompts are write-once; only the wrapper knows the backend-specific shapes.
- **Operator flexibility without code changes.** Switching backends requires only an `.env` change and a service restart. The same codebase ships to operators on either backend.
- **Auditable choice.** The toggle's value (`local` vs `anthropic`) appears in perf traces and conversation logs, so it's obvious which backend handled a given turn when debugging.
- **Aligns with ADR-007's hybrid posture.** ADR-007 already established that specialized calls remain on Anthropic. The toggle is the synthesis/orchestration analogue of that pattern — operator chooses the default, specialists still go where they're best.
- **Cache strategy is backend-specific.** Anthropic prompt caching is automatic and significant for orchestration; local llama-server uses its own kv-cache. The wrapper exposes the right cache hints to each backend.

## Alternatives Considered

### Fork the codebase per backend

Maintain a local-backend branch and an anthropic-backend branch (or two top-level orchestrators).

**Rejected because:** Maintenance nightmare. Every prompt change, every tool addition, every bug fix has to be synced. The orchestrator logic is the same for both backends — only the API shape differs.

### Local-only (drop Anthropic from synthesis)

Make local the only synthesis backend.

**Rejected because:** Excludes operators without a high-VRAM GPU. LifeOS is open-source; the default has to work for someone on a moderate workstation. It also means the maintainer can't temporarily compare local vs Anthropic behavior without code changes.

### Anthropic-only (drop local from synthesis)

Make Anthropic the only synthesis backend.

**Rejected because:** Violates the privacy and zero-cost guarantees that ADR-007 was designed around. Operators who do have the hardware should be able to keep their chat context off-cloud.

### Per-request runtime selection (model parameter on each call)

Accept a backend parameter on each chat call, with no default.

**Rejected because:** Pushes the choice onto the caller, which is the wrong layer. Almost every caller wants the same backend across the conversation, and per-call selection adds noise to every call site. A process-level `.env` knob is the right granularity.

### Wrap each backend's SDK directly, no translation layer

Use the Anthropic SDK and OpenAI SDK at call sites, without a unified wrapper.

**Rejected because:** Tools, prompts, message construction, streaming would all fork by backend. Adding a new tool would require updating both code paths and keeping them in sync. The wrapper exists specifically so the orchestrator and tool catalog don't know which backend is in use.

## Consequences

### Positive

- Operators with different hardware tiers can run the same LifeOS codebase by changing one `.env` value.
- Tool definitions and prompts are write-once (in the wrapper's canonical shape) and translated at call time.
- Single test surface for the orchestrator — behavioral differences narrow to the wrapper layer.
- Debug ergonomics: the operator can switch backends mid-debug and re-run the same query.
- Cache strategies are wrapped: callers don't need to know about Anthropic prompt caching vs local kv-cache.

### Negative

- Tool-format translation surface to maintain. When either API evolves (new tool-block types, new message-role conventions, new streaming events), the wrapper has to be updated. Anthropic and OpenAI both ship API changes regularly.
- Default backend choice is consequential and worth periodic re-evaluation. Today (`claude-haiku-4-5`) the Anthropic default trades cost for latency; the right default may change as model pricing and local hardware evolve.
- Subtle behavior differences between backends: Anthropic prompt caching makes long-context turns much cheaper than local equivalents; local model tool-calling reliability lags Claude on ambiguous calls. Operators switching backends should expect calibration drift.
- An additional layer of indirection at every chat call. Stack traces include the wrapper layer; debugging requires understanding the translation step.
- New backends (e.g., adding a third option) would require extending the wrapper's translation matrix — currently 2x2 (Anthropic↔canonical, OpenAI↔canonical); adding a third doubles edge cases.

### Relationship to ADR-007

ADR-007 said "replace Claude API orchestrator with local llama-server". ADR-009 reframes that as "make the orchestrator backend operator-configurable, defaulting to local where local is viable". The Linux migration, the systemd unit set, the local-LLM-first hardware target, the agent-worker as a separate process, the external venv convention — all of ADR-007's other decisions stand. ADR-007 gets `**Amended by:** ADR-009` in its frontmatter as the only acceptable in-place edit.

## Related Documents

### Design Context
- [ADR-007: Linux Migration and Local LLM Orchestration](007-linux-migration.md) — Original decision establishing local-first orchestration; this ADR amends
- [ADR-008: Managed Agents Cloud Routing](008-managed-agents-cloud-routing.md) — The parallel local/cloud split for the agent worker (different layer, same hybrid-model philosophy)

### Specifications
- [Architecture](../specs/technical/architecture.md) — Where the LLM client sits in the request flow
- [Chat UI](../specs/product/chat-ui.md) — Consumer-facing chat that hits the wrapper
- [Observability](../specs/technical/observability.md) — Perf traces include backend identifier per turn

### Operational
- [Configuration](../guides/configuration.md) — `LIFEOS_LLM_BACKEND` / `LIFEOS_ANTHROPIC_MODEL` env var reference (canonical home after #188 consolidation)

### Code References
- [`api/services/llm_client.py`](../../api/services/llm_client.py) — Unified wrapper; tool-format translation logic in `openai_tool_calls_to_anthropic` and surrounding helpers (lines ~60–200)
- [`config/settings.py`](../../config/settings.py) — `LIFEOS_LLM_BACKEND`, `LIFEOS_ANTHROPIC_MODEL` definitions
