# ADR-017: Retire Ollama — llama-server for Query Routing

**Status:** Complete
**Last Updated:** 2026-08-11
**Decision:** Accepted
**Supersedes:** [ADR-006](006-ollama-query-routing.md) — Ollama is no longer used anywhere in LifeOS; query routing runs on the same local `llama-server` as orchestration and synthesis.

## Context

[ADR-006](006-ollama-query-routing.md) chose Ollama as a second, purpose-built local LLM runtime dedicated to query routing and intent classification, separate from whatever served chat orchestration at the time. [ADR-007](007-linux-migration.md) and [ADR-009](009-llm-backend-toggle.md) subsequently established `llama-server` as the local runtime for chat orchestration and synthesis, unified behind `api/services/llm_client.py`. That left two independent local LLM runtimes on the same GPU: Ollama for routing, `llama-server` for everything else.

On 2026-05-28 this coexistence caused a host freeze: Ollama and `llama-server` both held models resident in VRAM at the same time, and the combined footprint exhausted the GPU. A single-user, single-GPU deployment has no headroom for two runtimes that each expect to own the device.

Running one runtime per host is the only sustainable posture for LifeOS's hardware target. Since `llama-server` was already required for orchestration and already wrapped by a unified client with fallback handling, routing gained nothing from a second, separately-managed runtime — it was duplicate infrastructure for a task the existing runtime could serve directly.

## Decision

Retire Ollama from LifeOS entirely. Query routing (`api/services/query_router.py`) and intent classification (`api/services/query_classifier.py`) call the same local `llama-server` used for orchestration and synthesis, through the existing `api/services/llm_client.py` wrapper (`generate_text`, `is_local_routing_llm_available`).

The fallback chain simplifies from three levels to two:

1. **llama-server** (primary): Local inference on the same runtime as orchestration, zero marginal cost.
2. **Pattern matching** (fallback): Regex-based heuristics, used when `llama-server` is unavailable.

The Anthropic Haiku fallback tier from ADR-006 is dropped — with routing and orchestration sharing one runtime, a separate cloud fallback for routing alone added complexity without a corresponding reliability gain; `LIFEOS_LLM_BACKEND=anthropic` already covers the case where an operator wants the whole local stack replaced by the Anthropic API.

`api/services/ollama_client.py` is deleted. `OLLAMA_HOST` / `OLLAMA_MODEL` / `OLLAMA_TIMEOUT` / `OLLAMA_RETRY_TIMEOUT` env vars remain accepted as inert aliases in `config/settings.py` so existing operator `.env` files don't break on upgrade; `ollama_host` / `ollama_model` are vestigial, and only the two timeout fields are still read (as generic request timeouts, unrelated to Ollama specifically).

## Rationale

- **One GPU, one resident model.** LifeOS's target hardware is a single consumer/prosumer GPU. Two LLM runtimes each trying to keep a model resident is a resource-exhaustion bug waiting to happen — 2026-05-28 was that bug happening.
- **The unified client already existed.** ADR-009's `llm_client.py` abstracts backend differences and already has fallback handling. Routing gains that fallback handling for free by using the same client, instead of maintaining its own Ollama-specific request/retry logic.
- **No accuracy loss.** The routing task (four-category classification) doesn't need a model dedicated to it; it needs *a* local model, and `llama-server` already runs one for orchestration. The chat-orchestration model, sized for the harder job, is more than sufficient for routing.
- **Less to operate.** One fewer systemd unit, one fewer health check, one fewer service to keep warm across nightly sync's VRAM juggling (`scripts/run_all_syncs.py`'s stop/restart-around-embeddings logic no longer has to account for Ollama's model residency on this host).

## Alternatives Considered

### Keep Ollama, add VRAM accounting to prevent both runtimes loading at once

Coordinate Ollama and `llama-server` so only one has a model loaded at a time, similar to how `scripts/run_all_syncs.py` already stops the LLM around embedding phases.

**Rejected because:** It solves the crash but not the duplication. Two runtimes still means two sets of retry/timeout/health-check logic, two services to keep current, and a coordination path that has to be right every time either service starts. The unified client already exists for `llama-server`; extending it to cover Ollama-specific coordination is more code than removing Ollama.

### Move routing to Anthropic Haiku as the primary (drop local routing)

Since routing already had a Haiku fallback in ADR-006, promote it to primary and drop local inference for routing entirely.

**Rejected because:** Every chat query would pay a network round-trip and API cost for a classification task, undoing the latency and zero-marginal-cost rationale ADR-006 established and ADR-007/009 reaffirmed for orchestration. `llama-server` was already resident for orchestration on every query; routing through it is strictly cheaper than a cloud call.

### Give routing a dedicated small model, still on Ollama, but never let it load concurrently with the orchestration model

Keep Ollama specifically for a smaller model than the orchestration model uses, on the theory that a smaller routing-only model is cheaper to keep warm.

**Rejected because:** This is a real trade-off (a small dedicated model does use less VRAM than a slice of the orchestration model's context) but it re-introduces the two-runtime coordination problem this ADR exists to remove, for a VRAM saving that hasn't been shown to matter in practice — the orchestration model was already resident on every query this router serves.

## Consequences

### Positive

- Eliminates the failure mode that caused the 2026-05-28 freeze: only one local LLM runtime is ever resident.
- One fewer systemd unit (no `lifeos-ollama` equivalent ever existed as a unit, but one fewer *implicit* dependency to keep running and monitored).
- Routing inherits `llm_client.py`'s existing retry/fallback/backend-toggle behavior instead of maintaining parallel logic.
- Simpler nightly-sync VRAM management (`scripts/run_all_syncs.py`) — one local runtime's residency to reason about around embedding phases, not two.
- Simpler fallback chain (two levels instead of three) is easier to reason about and test.

### Negative

- Dropping the Haiku fallback tier means a `llama-server` outage now falls straight to pattern matching, which is less accurate than an LLM-based fallback would be. Mitigated by `is_local_routing_llm_available()` health-checking before every routing call and by `llama-server` running under systemd (auto-restart on crash).
- Operators who genuinely want a second, independently-tuned small model just for routing (distinct from whatever model serves orchestration) no longer have a first-class path to that; they'd need to run a second `llama-server` instance themselves.
- The `OLLAMA_*` env var aliases in `config/settings.py` are permanent-until-removed cruft — a future cleanup could drop them once enough time has passed that no operator upgrade path depends on them.

## Related Documents

### Design Context
- [ADR-006: Ollama for Local Query Routing](006-ollama-query-routing.md) — Superseded by this ADR
- [ADR-007: Linux Migration and Local LLM Orchestration](007-linux-migration.md) — Established `llama-server` for orchestration; this ADR extends that runtime to routing
- [ADR-009: LIFEOS_LLM_BACKEND Toggle](009-llm-backend-toggle.md) — The unified `llm_client.py` wrapper that routing now calls through

### Specifications
- [Architecture](../specs/technical/architecture.md) — System architecture including query flow
- [Observability](../specs/technical/observability.md) — How routing fallbacks are monitored

### Operational
- [Configuration](../guides/configuration.md) — `LIFEOS_LLM_BACKEND` / `LIFEOS_LLM_MODEL` env var reference
- [Root AGENTS.md](../../AGENTS.md) — Health check commands and observability overview

### Code References
- [`api/services/query_router.py`](../../api/services/query_router.py) — Pipeline routing, now calling `llm_client.py` directly
- [`api/services/query_classifier.py`](../../api/services/query_classifier.py) — Intent classifier
- [`api/services/llm_client.py`](../../api/services/llm_client.py) — Unified LLM wrapper shared by routing, orchestration, and synthesis
- [`config/settings.py`](../../config/settings.py) — `OLLAMA_*` inert aliases kept for upgrade compatibility
