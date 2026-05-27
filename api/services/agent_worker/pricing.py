"""Per-model pricing for budget enforcement.

Prices are dollars per token. The Anthropic figures match
https://www.anthropic.com/pricing as of 2026-05. Update when models change.

For the local backend, both rates are 0 — compute is "free" (operator paid
upfront for the hardware). Time and tokens are still tracked for budget
enforcement, but they don't contribute to the dollar budget or the daily cap.
"""
from __future__ import annotations


# Anthropic Managed Agents charge a flat per-session-hour overhead on top of
# standard token rates (per https://platform.claude.com/docs/en/managed-agents/overview,
# announced April 2026). Long-idle sessions accrue this even when nothing is
# happening, which motivates the yield-and-resume pattern in Issue E.
MANAGED_SESSION_HOUR_OVERHEAD: float = 0.08


# Dollars per token. Keys match the model id strings used by the routers.
PRICING: dict[str, dict[str, float]] = {
    # Claude 4.7 / 4.6 / 4.5 share the same prices per their respective tiers.
    "claude-opus-4-7":   {"input": 15.0e-6, "output": 75.0e-6},
    "claude-opus-4-6":   {"input": 15.0e-6, "output": 75.0e-6},
    "claude-sonnet-4-6": {"input":  3.0e-6, "output": 15.0e-6},
    "claude-sonnet-4-5": {"input":  3.0e-6, "output": 15.0e-6},
    "claude-haiku-4-5":  {"input":  0.8e-6, "output":  4.0e-6},

    # Local backend (llama-server) — compute is free.
    "local":             {"input": 0.0,     "output": 0.0},
}


# Anthropic prompt-cache rate multipliers, relative to the model's base input
# rate (see https://www.anthropic.com/pricing). cache_creation writes are
# 1.25× input — slightly more expensive than a normal input token because of
# the indexing work. cache_read hits are 0.10× input — a 10× discount on
# tokens that come from the cache instead of being processed fresh.
CACHE_CREATION_RATE_MULTIPLIER: float = 1.25
CACHE_READ_RATE_MULTIPLIER: float = 0.10


def cost_for(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return the dollar cost of a single LLM call.

    Anthropic charges four token buckets:
    - `tokens_in` — uncached input tokens, at the model's input rate.
    - `tokens_out` — output tokens, at the model's output rate.
    - `cache_creation_tokens` — tokens written into the prompt cache on a
      cache-cold turn, at 1.25× the input rate.
    - `cache_read_tokens` — tokens served from the prompt cache on a cache-
      warm turn, at 0.10× the input rate.

    Cache buckets default to zero so existing two-arg call sites keep
    working. Unknown models fall through to a conservative Opus-rate
    estimate so a typo can't accidentally suppress budget enforcement.
    """
    rates = PRICING.get(model)
    if rates is None:
        rates = PRICING["claude-opus-4-7"]
    input_rate = rates["input"]
    return (
        tokens_in * input_rate
        + tokens_out * rates["output"]
        + cache_creation_tokens * input_rate * CACHE_CREATION_RATE_MULTIPLIER
        + cache_read_tokens * input_rate * CACHE_READ_RATE_MULTIPLIER
    )
