# ADR-025: Specialist Calls Fall Back When No Anthropic Key Is Set

**Status:** Complete
**Last Updated:** 2026-08-27
**Decision:** Accepted
**Amends:** [ADR-009](009-llm-backend-toggle.md) — narrows one clause of an already-superseded ADR: "specialized calls retained on Anthropic regardless of toggle" no longer holds when no Anthropic key is configured.

## Context

[ADR-009](009-llm-backend-toggle.md) established that `LIFEOS_LLM_BACKEND` controls synthesis and orchestration only — relationship insights, fact extraction, and tone analysis ("specialized calls") always use the Claude API regardless of the toggle, on the reasoning that these particular calls benefit enough from frontier-model quality to justify staying on Anthropic even when the operator has chosen `local` for everyday chat.

That reasoning assumed every install had an Anthropic key available to spend, even if the operator preferred not to spend it by default. [ADR-024](024-remote-llm-backend.md) already found and fixed the analogous gap for the *default orchestrator*: a real second-user deployment with no Anthropic key, no local server, and only a remote provider configured. The same gap exists here, one layer down: on a keyless install, `get_anthropic_llm()` (`api/services/llm_client.py`) always builds an `AnthropicLLMClient`, which fails immediately. Every caller already wraps its LLM call in a broad `try`/`except` that logs and degrades to an empty result — so the failure produces no error, no crash, and no data. Relationship insights, fact extraction, and both CRM tone-analysis endpoints all silently produce nothing, indistinguishable from "no data yet."

This was found on the same real keyless second-user deployment ADR-024 was written for: none of these four features functioned at all, and nothing indicated why.

## Decision

`get_anthropic_llm()` (the shared specialist-client constructor used by relationship insights, fact extraction, and both CRM tone-analysis endpoints) falls back when `ANTHROPIC_API_KEY` is unset, in this priority order:

1. **Anthropic**, when a key is configured — unchanged from ADR-009. Same client construction, same model (`LIFEOS_ANTHROPIC_SPECIALIST_MODEL`), same behavior for every existing caller.
2. **The local llama-server**, when reachable (`LocalLLMClient().is_available()` — one short `GET /health`), if no key is set.
3. **The configured remote paid provider** (`LIFEOS_REMOTE_LLM_*`, from #654/ADR-024), if no key is set and the local server isn't reachable.
4. **The (unreachable) local client anyway**, if none of the above apply. This function still never raises for "nothing is configured" — each caller's existing `try`/`except` around its own `.create()` call degrades to its current empty/no-op result, exactly as it already does today for any other transient failure. Nothing new needs to be added to the four callers themselves.

A single log line records the first time a keyless install falls back, naming which backend it fell back to (or that none is usable).

Web search is explicitly out of scope: it uses Anthropic's built-in `web_search_20250305` tool, which has no local or OpenAI-compatible equivalent, and it already goes through a separate mechanism (`web_search.py`'s `_use_native_anthropic()`, gated on `LIFEOS_LLM_BACKEND`, not on `get_anthropic_llm()`) that degrades to DuckDuckGo on any non-Anthropic backend today. This decision does not touch it.

## Rationale

- **Matches ADR-024's precedent exactly.** The default orchestrator and the specialist calls had the identical failure shape (always-Anthropic construction, no key, immediate failure) on the same real deployment. Using the same fix — fall back to whatever else is configured — keeps the two decisions consistent rather than solving the same problem two different ways.
- **The failure mode differs from ADR-024's, so the fix does too.** `get_local_llm()` (ADR-024) fails *loudly*, by design — an operator's explicit `LIFEOS_LLM_BACKEND` choice should error clearly if misconfigured rather than silently run somewhere else. `get_anthropic_llm()` has no equivalent explicit per-feature setting; an operator never chose "Anthropic specifically" for relationship insights the way they choose `LIFEOS_LLM_BACKEND` for chat. Since there's no explicit choice being second-guessed, and the failure mode without a fallback is *silent data loss* rather than a clear error, auto-probing (mirroring the agent worker's `_default_llm_caller` in `agent_worker/preflight.py`) is the right shape here — the opposite conclusion ADR-024 reached for the standing orchestrator default, and for a specific, applicable reason.
- **No new callers to touch.** All four callers already lazily fetch `get_anthropic_llm()` once and already wrap their use of it in a broad exception handler. Centralizing the fallback in the one shared constructor means zero changes to `relationship_insights.py`, `person_facts.py`, or the two `crm.py` tone-analysis endpoints — they keep calling the same function and get correct behavior automatically.
- **One log line, not four.** Because the fallback lives in the shared singleton constructor rather than in each caller, the "which backend did we fall back to" question only needs answering once per process, at the point the singleton is actually built — not once per feature.

## Alternatives Considered

### Fail fast, like `get_local_llm()` (ADR-024)

Raise a named `LLMBackendNotConfiguredError`-style exception from `get_anthropic_llm()` when no key is set, mirroring the orchestrator's fail-fast behavior exactly.

**Rejected because:** there is no explicit operator setting analogous to `LIFEOS_LLM_BACKEND` for specialist calls to "fail fast" against — an operator who set `LIFEOS_LLM_BACKEND=local` never claimed anything about whether relationship insights should also be local, so there's no misconfiguration to report. Raising here would also change the shape of every caller's existing degrade path (`try`/`except` → empty result) into an unhandled exception unless all four callers were also updated to catch the new error type specifically — more invasive than the actual problem requires, for a set of features whose current fallback-to-empty already reads as "nothing to show" rather than "system broken."

### Add a dedicated `LIFEOS_SPECIALIST_LLM_BACKEND` setting

Give specialist calls their own explicit backend toggle, independent of `LIFEOS_LLM_BACKEND`, so the choice is as auditable as the orchestrator's.

**Rejected because:** no operator has asked to run relationship insights on a *different* backend than everyday chat while a key is present — the actual gap is narrower (what happens with *no* key at all), and a new setting for a choice nobody has needed to make yet is exactly the speculative configurability the project's simplicity principle rules out.

### Route specialist calls through `get_local_llm()` directly instead of a separate fallback

Since `get_local_llm()` (ADR-024) already resolves `LIFEOS_LLM_BACKEND` to a working client, have specialist calls just use that client too when no key is set.

**Rejected because:** `get_local_llm()` is explicit-config, fail-fast by design (ADR-024) — on the exact keyless-with-nothing-set-explicitly install this issue was found on, `LIFEOS_LLM_BACKEND` was left at its default (`anthropic`), so `get_local_llm()` would raise `LLMBackendNotConfiguredError` too. Reusing it here would just move the silent-failure problem into a loud one, not solve it — specialist calls need their own reachability probe precisely because there's no reliable explicit setting to read instead.

## Consequences

### Positive

- A keyless install (no Anthropic key, whether or not `LIFEOS_LLM_BACKEND` is set) now gets real relationship insights, fact extraction, and tone analysis instead of a feature that silently produces nothing.
- Zero changes required to the four call sites — the fix is fully contained in the shared constructor.
- A keyed install (both live deployments today) is byte-for-byte unaffected — the `settings.anthropic_api_key` branch is untouched.

### Negative

- `get_anthropic_llm()`'s return type is now `AnthropicLLMClient | LocalLLMClient` instead of always `AnthropicLLMClient` — a small increase in what callers need to reason about, though all four already treat it as a duck-typed `.create()`-capable object and none inspect its concrete type.
- The local-reachability probe (`is_available()`, a ~3-second-timeout `GET /health`) runs once, synchronously, the first time any specialist call happens on a keyless install — a one-time blocking cost at first use, not per-call, matching the existing singleton-construction pattern for both `get_local_llm()` and the pre-existing `get_anthropic_llm()`.
- Specialist output quality on a keyless install now depends on whichever backend it fell back to (a local model or a remote provider, both likely lower-capability than the Sonnet-tier client ADR-009 originally chose for these features) — an acceptable degrade given the alternative is no output at all, but a real quality difference an operator should be aware of.

## Related Documents

### Design Context
- [ADR-009: LIFEOS_LLM_BACKEND Toggle](009-llm-backend-toggle.md) — Established specialist calls staying on Anthropic regardless of toggle; this ADR narrows that clause for the keyless case
- [ADR-024: Remote Provider as a Third LIFEOS_LLM_BACKEND Value](024-remote-llm-backend.md) — The analogous fix for the default orchestrator; this ADR is the specialist-call counterpart, with a deliberately different (auto-probe, not fail-fast) mechanism

### Specifications
- [Client Surfaces](../specs/technical/client-surfaces.md) — No public HTTP response field changed by this ADR

### Operational
- [Configuration Guide](../guides/configuration.md) — `ANTHROPIC_API_KEY` / specialist-call fallback reference

### Code References
- [`api/services/llm_client.py`](../../api/services/llm_client.py) — `get_anthropic_llm()`, the fallback logic
- [`api/services/agent_worker/preflight.py`](../../api/services/agent_worker/preflight.py) — `_default_llm_caller`, the priority-order fallback pattern this ADR mirrors
- [`api/services/relationship_insights.py`](../../api/services/relationship_insights.py), [`api/services/person_facts.py`](../../api/services/person_facts.py), [`api/routes/crm.py`](../../api/routes/crm.py) — the four unchanged callers
