# ADR-021: Provider and Model Registry

**Status:** Accepted  
**Date:** 2026-08-21

## Context

LifeOS already supports Anthropic and one local OpenAI-compatible server, but
the selection mechanism is a binary backend switch. Specialist extraction,
preflight, and per-turn model overrides consequently contain provider-specific
assumptions. Adding another provider would otherwise require changes across the
application and would risk coupling personal data to an inference vendor.

## Decision

LifeOS keeps a provider-neutral client contract and resolves providers through
named model profiles. Providers describe connection details and models describe
which provider/model to use for an operation. OpenAI-compatible providers share
one HTTP implementation; Anthropic keeps its native SDK adapter where its wire
features differ.

The registry is configured with `LIFEOS_LLM_PROVIDERS` and
`LIFEOS_LLM_MODELS`. API keys are referenced through `api_key_env` and are never
stored in the registry. Existing `LIFEOS_LLM_BACKEND`, Anthropic settings, and
local llama-server settings remain valid fallbacks.

## Consequences

- OpenAI, OpenRouter, DeepSeek, Gemini-compatible gateways, and local servers
  can be added through configuration.
- Named profiles can route chat, fast extraction, specialist analysis, and
  private processing independently.
- Memories, CRM records, and source data remain independent of providers.
- Provider-specific capabilities such as Anthropic prompt caching remain
  optional rather than becoming requirements of the data layer.
- Existing deployments retain their previous default behavior when the new
  registry variables are absent.

## Configuration example

```bash
LIFEOS_LLM_PROVIDERS='{"openai":{"type":"openai_compatible","base_url":"https://api.openai.com/v1","api_key_env":"OPENAI_API_KEY"}}'
LIFEOS_LLM_MODELS='{"default":{"provider":"openai","model":"gpt-4o-mini"},"fast":{"provider":"openai","model":"gpt-4o-mini"}}'
```
