# ADR-022: Remote Provider as a Third `LIFEOS_LLM_BACKEND` Value

**Status:** Complete
**Last Updated:** 2026-08-27
**Decision:** Accepted
**Supersedes:** [ADR-009](009-llm-backend-toggle.md) — the two-value (`anthropic`/`local`) backend decision; both original values are unchanged, `remote` is added alongside them.

## Context

[ADR-009](009-llm-backend-toggle.md) made `LIFEOS_LLM_BACKEND` an operator-facing knob with two values — `anthropic` (Claude API) and `local` (an on-box `llama-server`) — covering the two hardware tiers LifeOS's maintainer had actually seen: a GPU workstation, or none.

[#654](https://github.com/nbramia/LifeOS/issues/654) later added a third real deployment shape: a paid OpenAI-compatible remote provider (e.g. Fireworks running DeepSeek/Qwen). It was added as a **per-turn** pick only — the chat model picker's "Remote" option — never as the standing default, because at the time every known install had either an Anthropic key or a local server to fall back on.

A real second-user deployment broke that assumption: a Mac Mini with no Anthropic key, no local `llama-server`, and a working remote provider as the *only* configured engine. Every ordinary chat turn (no per-turn override) still tried to build an `AnthropicLLMClient` with no key, and the raw Anthropic SDK exception reached the user. `LIFEOS_LLM_BACKEND` had no value that meant "the remote provider is this install's default engine" — the operator could hand-pick "Remote" once per turn from the picker, but nothing let it stand in as the default the way `local` already could.

This is a straightforward extension of ADR-009's model, not a rethink of it: the toggle's job — choose which engine handles a turn when nothing overrides it — is unchanged. What changed is the number of deployment shapes worth naming: three now, not two.

## Decision

`LIFEOS_LLM_BACKEND` gains a third value, `remote`, alongside the existing `anthropic` (default) and `local`:

- `anthropic` — unchanged: Anthropic API, model from `LIFEOS_ANTHROPIC_MODEL`. **New:** if `ANTHROPIC_API_KEY` is unset, `get_local_llm()` now raises a named `LLMBackendNotConfiguredError` instead of letting `AnthropicLLMClient.__init__` construct a client that will only fail later, deep in a chat turn, with the raw SDK message.
- `local` — unchanged: local `llama-server`, per ADR-009.
- `remote` (new) — routes synthesis and orchestration to the already-configured paid OpenAI-compatible provider (`LIFEOS_REMOTE_LLM_URL`/`_MODEL`/`_API_KEY`/`_TIMEOUT`, from #654) using the same `LocalLLMClient` class the per-turn "Remote" picker option already builds, just as the process-wide singleton instead of a per-turn override. If the provider isn't fully configured (URL, model, and key all set — `remote_llm_configured`), `get_local_llm()` fails fast with a named error rather than silently falling back to another backend.

The per-turn picker's existing `model_override="remote"` is unaffected — it works the same way regardless of the default backend setting, exactly as it did before this change.

`remote` is explicitly excluded from the auto-escalation ladder, per [ADR-018](018-api-spend-requires-consent.md): `NON_API_RUNGS` in `agent_loop.py` already names only `("claude_code", "codex", "local")`, so naming the remote provider anywhere in an operator-configured ladder is a no-op, not a new bypass — LifeOS still never puts a turn on a paid API-shaped engine unless a human explicitly asked for that turn.

## Rationale

- **Matches ADR-009's own reasoning, extended.** ADR-009 rejected "local-only" and "Anthropic-only" because each excludes a real deployment shape. A remote-provider-only install (no GPU, no Anthropic key, but a working paid endpoint) is exactly that same kind of excluded shape, observed in practice rather than hypothesized.
- **No new translation surface.** `remote` reuses `LocalLLMClient` (the OpenAI-compatible wrapper) unchanged — it was already generalized by #654 to accept `base_url`/`model`/`api_key` overrides for this exact purpose. `get_local_llm()` only gains a third branch that constructs the same class with the remote provider's settings instead of the local server's.
- **Fail fast, name the fix.** An unconfigured `anthropic` or `remote` selection now raises before a turn starts, with the missing setting named, rather than surfacing a provider SDK's internal exception text to whoever is chatting (see also #787, which does the analogous thing for mid-turn provider failures).
- **The escalation boundary needed no new code.** `NON_API_RUNGS` was already an explicit allowlist rather than a subtraction from all-model-ids, so a new backend value doesn't require touching it to stay excluded — it simply isn't named there, same as `remote` wasn't named in ADR-018's original ladder either.

## Alternatives Considered

### Auto-probe: fall back to whatever's configured, in priority order

Have `get_local_llm()` silently try Anthropic key → local server reachable → remote configured → raise, mirroring the agent worker's `_default_llm_caller` fallback chain (`api/services/agent_worker/preflight.py`).

**Rejected because:** `LIFEOS_LLM_BACKEND` is meant to be an explicit, auditable operator choice (ADR-009's "auditable choice" rationale) — the value in a perf trace should say what the operator asked for, not what got probed and picked at process start. Auto-probing is the right shape for a background worker choosing an executor per session (where "something available" is the actual requirement); it's the wrong shape for the chat orchestrator's standing default, which should fail loudly if misconfigured rather than silently degrade to a different engine than the operator believes is running.

### Repurpose `local` to mean "local-or-remote"

Instead of a new value, let `local` transparently fall back to the remote provider when no local server is reachable.

**Rejected because:** conflates two operationally distinct engines (on-box, zero-marginal-cost vs. a paid third-party API) under one setting value. An operator reading `.env` should be able to tell, from the value alone, whether a turn might incur provider cost. This is the same reasoning ADR-009 itself used to keep `anthropic` and `local` as distinct values rather than one "cloud-or-local" toggle.

### Make `remote` reachable by the auto-escalation ladder

Since `remote` is now a real standing backend, also let the ladder climb to it automatically on repeated refusals, the way it can climb to `local`.

**Rejected because:** `local` is zero-marginal-cost; `remote` is a paid API call, indistinguishable in kind from `anthropic` for ADR-018's purposes. ADR-018 already drew this line for `local` specifically ("non-API rungs... nothing else"), and #654 kept `remote` off the ladder as an explicit pick only. Nothing about making it a standing default changes that cost profile — reversing it would be a silent, unrelated policy change riding along with this issue.

## Consequences

### Positive

- A remote-only install (no Anthropic key, no local server) now has a real path to a working default chat turn, with no per-turn picker interaction required.
- A keyless `anthropic` install now fails with a clear, named reason at the point `get_local_llm()` is first called, instead of a raw SDK exception surfacing mid-turn.
- No new translation surface: `remote` is `LocalLLMClient` pointed at different settings, the same mechanism #654 already established for the per-turn pick.

### Negative

- Three-way branching in `get_local_llm()` (and a corrected three-way case in `agent_loop._select_client`'s `force_local` branch — the two-value `!= "anthropic"` check would previously have misread a `remote` default as "already local") is a small but permanent increase in the number of backend combinations to reason about at each call site that branches on `llm_backend`.
- `remote` shares its failure-mode strictness with `anthropic` (fail fast, no silent fallback) but not with `local`'s existing behavior elsewhere (e.g. the agent worker's opt-in `agent_remote_executor` fallback, a different code path with different goals) — an operator reading only one of these mechanisms could reasonably expect the other to behave the same way; the difference is intentional but not obvious without reading both.

## Related Documents

### Design Context
- [ADR-009: LIFEOS_LLM_BACKEND Toggle](009-llm-backend-toggle.md) — Superseded by this ADR; the two-value decision this extends to three
- [ADR-018: API Spend Requires Consent](018-api-spend-requires-consent.md) — Why `remote` must stay off the auto-escalation ladder
- [ADR-007: Linux Migration and Local LLM Orchestration](007-linux-migration.md) — Original local-first default; amended by ADR-009, unaffected by this ADR

### Specifications
- [Client Surfaces](../specs/technical/client-surfaces.md) — No public HTTP response field changed by this ADR

### Operational
- [Configuration Guide](../guides/configuration.md) — `LIFEOS_LLM_BACKEND` and `LIFEOS_REMOTE_LLM_*` reference

### Code References
- [`api/services/llm_client.py`](../../api/services/llm_client.py) — `get_local_llm()`, the three-value switch and `LLMBackendNotConfiguredError`
- [`api/services/agent_loop.py`](../../api/services/agent_loop.py) — `_select_client`, the per-turn `force_remote`/`force_local` construction this ADR's default reuses
- [`config/settings.py`](../../config/settings.py) — `llm_backend`, `remote_llm_*` fields, `remote_llm_configured` property
