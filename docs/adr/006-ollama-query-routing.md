# ADR-006: Ollama for Local Query Routing

> **Decision:** Use Ollama with a local LLM for query routing and intent classification, with a multi-level fallback chain.
> **Date:** 2026-02-19
> **Status:** Accepted
> **Last Updated:** 2026-02-19

## Context

Every chat query in LifeOS must be classified before processing to determine the correct pipeline: general knowledge (Claude answers directly, no data fetched), web search (external information retrieved), personal data (routed to the hybrid search pipeline), or compound (multiple steps). This classification happens before the main Claude API call.

Using Claude for routing would add approximately 1 second of latency and API cost to every single query, including trivial ones like "what time is it?" or "tell me a joke." At hundreds of queries per week, this adds up in both time and money. The classification task itself is straightforward — a four-category intent classification — and does not require frontier model capability. A smaller, local model can achieve high accuracy on this task at a fraction of the cost and latency.

The routing system must also be resilient. If the primary routing model is unavailable (e.g., after a macOS restart before Ollama auto-starts), queries should still be processed rather than failing. This requires a fallback chain that degrades gracefully from high-quality local inference to simpler heuristics.

## Decision

Use Ollama running locally with a small language model (Qwen 2.5 7B Instruct) for query routing and intent classification. Implement a three-level fallback chain:

1. **Ollama** (primary): Local inference, ~200ms latency, zero API cost.
2. **Anthropic Haiku** (fallback): Cloud-based, ~500ms latency, minimal cost. Used when Ollama is unavailable (e.g., after a macOS restart before Ollama auto-starts).
3. **Pattern matching** (last resort): Regex-based heuristics, <1ms, zero cost. Handles basic routing when both LLM services are unavailable.

## Rationale

- **Cost**: Local inference has zero marginal cost. Over hundreds of daily queries, this avoids meaningful API spend on a task that does not require frontier model capability. At ~$0.001 per Haiku call, routing all queries through Haiku would cost roughly $3-5/month — not prohibitive, but unnecessary when a local model performs comparably.
- **Latency**: Local Ollama responds in ~200ms vs ~500ms-1s for a cloud API call. For a classification task that gates every query, this 300-800ms saving is noticeable in the user experience. Over a conversation with 10+ turns, the cumulative time savings are significant.
- **Sufficient accuracy**: Intent classification is a straightforward task. A 7B parameter model achieves high accuracy on the four-category classification. Edge cases (compound queries like "look up X and create a task for Y") are the main failure mode, and these degrade gracefully — the query is still processed, just potentially with a suboptimal pipeline.
- **Graceful degradation**: The fallback chain ensures the system never hard-fails on routing. If Ollama is down, Haiku handles it. If Haiku is unreachable, pattern matching provides basic coverage. Degradation events are tracked and reported via the alerting system.

## Alternatives Considered

### Always Use Claude for Routing

Using Claude (Sonnet or Opus) for every routing decision would provide the highest classification accuracy, especially for ambiguous and compound queries. However, it adds ~1 second of latency and ~$0.01 per query in API cost. Over hundreds of queries per week, this adds noticeable lag to every interaction and meaningful cost. More importantly, the accuracy improvement over a 7B local model is marginal for a four-category classification task — Claude's strengths (reasoning, nuance, long-context understanding) are wasted on "is this a personal data query or a general knowledge question?"

### Rules-Based Only (No LLM)

A purely regex-based router would be the fastest and cheapest option — no model loading, no API calls, sub-millisecond classification. Simple patterns work well for obvious cases ("create a task" → compound, "what's the weather" → web search). However, natural language is inherently ambiguous. "Tell me about the meeting with Sarah" could be personal data or general knowledge depending on context. "What did we discuss at dinner?" requires understanding that "we" implies personal data. A rules-only approach cannot handle this ambiguity, leading to frequent misrouting and degraded search results. It works as a last-resort fallback but is too rigid as the primary router.

### Fine-Tuned Classifier

A purpose-built classifier (e.g., fine-tuned BERT or a small transformer trained on labeled routing data) would be fast and potentially very accurate for the specific four-category classification. However, this requires creating a training dataset, building an evaluation pipeline, and retraining when categories evolve (e.g., adding a new routing category). For a four-class problem that a general-purpose 7B model handles well, the engineering overhead of a custom classifier is unjustified. The maintenance burden of keeping training data current and retraining on changes outweighs the marginal accuracy improvement.

### No Routing (Uniform Pipeline)

Sending every query through the same pipeline — always searching the vault, always checking web, always querying Claude — would eliminate the routing step entirely. However, this wastes resources on irrelevant searches (general knowledge queries don't need vault search), adds latency (every query pays the cost of the full pipeline), and produces noisy results (vault search results appearing alongside Claude's direct answer for "what is the capital of France?"). Routing exists to match queries to the appropriate pipeline, reducing both latency and noise.

## Consequences

**Positive:**
- Fast classification (~200ms) with zero marginal API cost.
- Graceful degradation through three fallback levels — the system never hard-fails on routing.
- Ollama is useful beyond routing (can serve other local inference tasks in the future).

**Negative:**
- Requires Ollama service running on the Mac Mini (managed via launchd).
- Model updates must be pulled manually (`ollama pull qwen2.5:7b-instruct`).
- Local model is less accurate than Claude for ambiguous or compound queries — these occasionally route incorrectly and produce suboptimal responses.
- Additional service to monitor (tracked via the health/alerting system as a WARNING-level dependency).

**Risks:**
- Ollama's macOS integration depends on launchd auto-start working reliably. After macOS updates or power events, Ollama may not restart automatically, causing the system to fall back to Haiku for extended periods. Monitoring the fallback rate helps detect this.
- The Qwen 2.5 7B model may be superseded by better options. Periodic evaluation of newer small models (Phi, Gemma, Llama) is worthwhile to maintain or improve routing accuracy.
- If LifeOS adds more routing categories (beyond the current four), the 7B model's classification accuracy may degrade, potentially requiring a larger model or a different approach.

## Related Documents

**Design Context:**
- [ADR-004: Hybrid Search](004-hybrid-search.md) — The search pipeline that routing feeds into
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The backend that integrates Ollama

**Specifications:**
- [Architecture](../specs/technical/architecture.md) — System architecture including query flow
- [Observability](../specs/technical/observability.md) — How routing fallbacks are monitored

**Operational:**
- [Troubleshooting](../guides/troubleshooting.md) — Ollama debugging and restart procedures
- [AGENTS.md](../../AGENTS.md) — Health check commands and observability overview
