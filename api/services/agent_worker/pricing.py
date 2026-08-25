"""Per-model pricing for budget enforcement.

Prices are dollars per token. The Anthropic figures match
https://platform.claude.com/docs/en/about-claude/pricing as verified 2026-08-23
(#655 — this pass also caught the Opus 4.6/4.7/4.8 and Sonnet 5 entries
below being wrong; see the inline notes).
Update when models change.

This is the **only** live pricing table in LifeOS — `cost_for()` below is
called from every place that turns tokens into dollars: the agent worker
(`managed_executor.py`, `managed_driver.py`, `local_executor.py`), the
Claude Code session cost rollup (`claude_code/session_ingest.py`), and the
cost-estimate endpoint (`routes/tasks.py`). A second, long-dead pricing
table used to live in `api/services/cost_tracker.py` (tier-word keyed,
disagreed with this one on Haiku) — it had no callers outside its own
module and was removed in #656 rather than left as a second
plausible-looking source of truth. Hermes-routed turns are the one
exception: Hermes prices those upstream and LifeOS records `cost_usd`
verbatim (see `_HermesTurnPersister` in `routes/hermes_proxy.py`) rather
than recomputing it through this table.

For the local backend, both rates are 0 — compute is "free" (operator paid
upfront for the hardware). Time and tokens are still tracked for budget
enforcement, but they don't contribute to the dollar budget or the daily cap.
"""
from __future__ import annotations

import re


# Anthropic Managed Agents charge a flat per-session-hour overhead on top of
# standard token rates (per https://platform.claude.com/docs/en/managed-agents/overview,
# announced April 2026). Long-idle sessions accrue this even when nothing is
# happening, which motivates the yield-and-resume pattern in Issue E.
MANAGED_SESSION_HOUR_OVERHEAD: float = 0.08


# Dollars per token. Keys match the model id strings used by the routers.
# Verified 2026-08-23 against https://platform.claude.com/docs/en/about-claude/pricing.
PRICING: dict[str, dict[str, float]] = {
    # Fable 5 / Mythos 5 (limited availability / Project Glasswing) — the
    # most expensive tier Anthropic currently serves, at $10/$50 per Mtok.
    # Not routed to by any LifeOS alias today, but listed here so a usage
    # row naming either (e.g. a manually-configured escalation model)
    # prices correctly instead of falling through to fallback_rates() (#655).
    "claude-fable-5":    {"input": 10.0e-6, "output": 50.0e-6},
    "claude-mythos-5":   {"input": 10.0e-6, "output": 50.0e-6},
    # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5 all share the same $5/$25-per-Mtok rate.
    # 4.8/4.7/4.6 were incorrectly 15.0e-6/75.0e-6 (Opus 4/4.1's retired
    # rate) until #655.
    "claude-opus-5":     {"input":  5.0e-6, "output": 25.0e-6},
    "claude-opus-4-8":   {"input":  5.0e-6, "output": 25.0e-6},
    "claude-opus-4-7":   {"input":  5.0e-6, "output": 25.0e-6},
    "claude-opus-4-6":   {"input":  5.0e-6, "output": 25.0e-6},
    "claude-opus-4-5":   {"input":  5.0e-6, "output": 25.0e-6},
    # $2.00/$10.00 per Mtok — Sonnet 5's launch "introductory" rate became
    # the permanent rate (Anthropic cancelled the scheduled 2026-09-01
    # increase to $3/$15). Was incorrectly 3.0e-6/15.0e-6 until #655.
    "claude-sonnet-5":   {"input":  2.0e-6, "output": 10.0e-6},
    "claude-sonnet-4-6": {"input":  3.0e-6, "output": 15.0e-6},
    "claude-sonnet-4-5": {"input":  3.0e-6, "output": 15.0e-6},
    # Retired but still referenced by historical usage rows (#656) — same
    # rate as Sonnet 4.5/4.6 per Anthropic's pricing page.
    "claude-sonnet-4":   {"input":  3.0e-6, "output": 15.0e-6},
    # $1.00/$5.00 per Mtok (Haiku 4.5's actual published rate). Was
    # incorrectly 0.8e-6/4.0e-6 (Haiku 3.5's retired rate) until #656.
    "claude-haiku-4-5":  {"input":  1.0e-6, "output":  5.0e-6},
    # Retired tiers, still served on Bedrock/Vertex and still named by
    # historical usage rows -- absent until #669, which meant a row
    # referencing one of these resolved to fallback_rates() and *understated*
    # the Opus pair (fallback is $10/$50; these are the pricier $15/$75).
    # $15/$75 per Mtok, per https://platform.claude.com/docs/en/about-claude/pricing
    # (verified 2026-08-24).
    "claude-opus-4-1":   {"input": 15.0e-6, "output": 75.0e-6},
    "claude-opus-4":     {"input": 15.0e-6, "output": 75.0e-6},
    # $0.80/$4.00 per Mtok -- Haiku 3.5's actual (retired) rate, same figure
    # claude-haiku-4-5 was incorrectly assigned until #656.
    "claude-haiku-3-5":  {"input":  0.8e-6, "output":  4.0e-6},

    # Local backend (llama-server) — compute is free.
    "local":             {"input": 0.0,     "output": 0.0},
}

# Ids Anthropic has retired from the first-party API (still served on Bedrock /
# Vertex, still named by historical usage rows). They stay in PRICING so those
# rows price correctly, but they are excluded from fallback_rates(): the ceiling
# for an *unrecognized* model must be the priciest tier still being served, not
# a retired one. Without this, adding Opus 4/4.1 ($15/$75 — pricier than any
# current tier) silently raised every unknown-model estimate by 50% (#669).
RETIRED_MODELS: frozenset[str] = frozenset({
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-3-5",
})

# Matches a trailing dated-snapshot suffix on an Anthropic model id, e.g.
# "claude-sonnet-4-5-20250929" -> "claude-sonnet-4-5". Real usage rows
# (Claude Code sessions in particular) record the exact snapshot id the API
# echoed back rather than the bare tier id above, so a lookup needs both
# forms to keep historical rows priced (#656).
_DATED_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


# Anthropic prompt-cache rate multipliers, relative to the model's base input
# rate (see https://www.anthropic.com/pricing). cache_creation writes are
# 1.25× input — slightly more expensive than a normal input token because of
# the indexing work. cache_read hits are 0.10× input — a 10× discount on
# tokens that come from the cache instead of being processed fresh.
CACHE_CREATION_RATE_MULTIPLIER: float = 1.25
CACHE_READ_RATE_MULTIPLIER: float = 0.10


def is_known_model(model: str) -> bool:
    """True when `model` (or its bare tier, stripping a dated snapshot
    suffix) has a rate in PRICING.

    Exists for a caller that must distinguish "this model is genuinely
    free" from "this model's rate is unknown" (#661) — `cost_for` collapses
    that distinction into a conservative Opus-rate estimate, which is the
    right call for its existing budget-enforcement callers (an
    underestimate there could blow past a spend cap) but wrong for a usage
    reader deciding whether to mark a row `unpriced`: silently billing an
    unrecognized model at Opus rates would misrepresent the actual (unknown)
    cost just as much as recording it as free.
    """
    return model in PRICING or _DATED_SNAPSHOT_SUFFIX.sub("", model) in PRICING


def fallback_rates() -> dict[str, float]:
    """Rates for an unrecognized model id: the priciest *currently-served* tier.

    Computed from the table rather than naming a specific model id, so this
    doesn't itself go stale the next time a new top-tier model ships (as
    happened across Opus 4.6/4.7/4.8 before #655).
    """
    return max(
        (
            rates
            for name, rates in PRICING.items()
            if name != "local" and name not in RETIRED_MODELS
        ),
        key=lambda rates: rates["output"],
    )


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
    working. A dated snapshot id (e.g. "claude-sonnet-4-5-20250929") that
    isn't itself a key resolves to its bare tier if that tier is priced.
    Anything still unresolved falls through to the priciest known tier's
    rate so a typo can't accidentally suppress budget enforcement.
    """
    rates = PRICING.get(model)
    if rates is None:
        rates = PRICING.get(_DATED_SNAPSHOT_SUFFIX.sub("", model))
    if rates is None:
        rates = fallback_rates()
    input_rate = rates["input"]
    return (
        tokens_in * input_rate
        + tokens_out * rates["output"]
        + cache_creation_tokens * input_rate * CACHE_CREATION_RATE_MULTIPLIER
        + cache_read_tokens * input_rate * CACHE_READ_RATE_MULTIPLIER
    )
