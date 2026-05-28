# ADR-006: Ollama for Local Query Routing

**Status:** Complete
**Last Updated:** 2026-02-19
**Decision:** Accepted

## Context

Every chat query in LifeOS must be classified before processing to determine the correct pipeline: general knowledge (LLM answers directly, no data fetched), web search (external information retrieved), personal data (routed to the hybrid search pipeline), or compound (multiple steps). This classification happens before the main synthesis call.

Using the main LLM for routing would add approximately 1 second of latency and API/inference cost to every single query, including trivial ones. At hundreds of queries per week, this adds up. The classification task itself is straightforward — a four-category intent classification — and does not require frontier-model capability. A smaller, local model can achieve high accuracy on this task at a fraction of the cost and latency.

The routing system must also be resilient. If the primary routing model is unavailable (e.g., after a system restart before the service auto-starts), queries should still be processed rather than failing. This requires a fallback chain that degrades gracefully from high-quality local inference to simpler heuristics.

## Decision

Use Ollama running locally with a small language model (Qwen 2.5 7B Instruct at decision time) for query routing and intent classification. Implement a three-level fallback chain:

1. **Ollama** (primary): Local inference, ~200ms latency, zero API cost.
2. **Anthropic Haiku** (fallback): Cloud-based, ~500ms latency, minimal cost. Used when Ollama is unavailable.
3. **Pattern matching** (last resort): Regex-based heuristics, <1ms, zero cost. Handles basic routing when both LLM services are unavailable.

## Rationale

- **Cost**: Local inference has zero marginal cost. Over hundreds of daily queries this avoids meaningful API spend on a task that does not require frontier capability.
- **Latency**: Local Ollama responds in ~200ms vs ~500ms–1s for a cloud API call. For a classification task that gates every query, the 300–800ms saving is noticeable. Over a conversation with 10+ turns the cumulative time savings are significant.
- **Sufficient accuracy**: A 7B-parameter model achieves high accuracy on four-category classification. Edge cases (compound queries like "look up X and create a task for Y") degrade gracefully — the query is still processed, just potentially with a suboptimal pipeline.
- **Graceful degradation**: The fallback chain ensures routing never hard-fails. If Ollama is down, Haiku handles it. If Haiku is unreachable, pattern matching provides basic coverage. Degradation events are tracked and reported via the alerting system.

## Alternatives Considered

### Always Use the Main LLM for Routing

Use the same Claude model (Sonnet or Opus) for every routing decision — highest accuracy, especially for ambiguous and compound queries.

**Rejected because:** It adds ~1 second of latency and ~$0.01 per query in API cost. Over hundreds of queries per week this adds noticeable lag and meaningful cost. The accuracy improvement over a 7B local model is marginal for four-category classification — frontier-model strengths (reasoning, nuance, long-context understanding) are wasted on "is this a personal data query or a general knowledge question?"

### Rules-Based Only (No LLM)

A purely regex-based router would be the fastest and cheapest option — no model loading, no API calls, sub-millisecond classification.

**Rejected because:** Natural language is inherently ambiguous. "Tell me about the meeting with Sarah" could be personal data or general knowledge depending on context. "What did we discuss at dinner?" requires understanding that "we" implies personal data. A rules-only approach can't handle this ambiguity, leading to frequent misrouting and degraded search results. It works as a last-resort fallback but is too rigid as the primary router.

### Fine-Tuned Classifier

A purpose-built classifier (e.g., fine-tuned BERT or a small transformer trained on labeled routing data) would be fast and potentially very accurate.

**Rejected because:** It requires creating a training dataset, building an evaluation pipeline, and retraining when categories evolve. For a four-class problem that a general-purpose 7B model handles well, the engineering overhead is unjustified. Maintenance burden of keeping training data current and retraining on changes outweighs the marginal accuracy improvement.

### No Routing (Uniform Pipeline)

Send every query through the same pipeline — always search the vault, always check web, always query the LLM.

**Rejected because:** It wastes resources on irrelevant searches (general knowledge queries don't need vault search), adds latency (every query pays the cost of the full pipeline), and produces noisy results (vault search results appearing alongside the LLM's direct answer for "what is the capital of France?"). Routing exists to match queries to the appropriate pipeline.

## Consequences

### Positive

- Fast classification (~200ms) with zero marginal API cost.
- Graceful degradation through three fallback levels — the system never hard-fails on routing.
- Ollama is useful beyond routing (can serve other local inference tasks in the future).

### Negative

- Requires Ollama service running on the host (managed via systemd on Linux, launchd on macOS).
- Model updates must be pulled manually (`ollama pull qwen2.5:7b-instruct`).
- Local model is less accurate than Claude for ambiguous or compound queries — these occasionally route incorrectly and produce suboptimal responses.
- Additional service to monitor (tracked via the health/alerting system as a WARNING-level dependency).
- Ollama auto-start reliability after system updates or power events is imperfect — monitoring the fallback rate helps detect when the system has been on Haiku for an extended period.
- The Qwen 2.5 7B model may be superseded by better options. Periodic evaluation of newer small models (Phi, Gemma, Llama) is worthwhile.
- If LifeOS adds more routing categories beyond the current four, the 7B model's classification accuracy may degrade, potentially requiring a larger model or a different approach.

## Related Documents

### Design Context
- [ADR-004: Hybrid Search](004-hybrid-search.md) — The search pipeline that routing feeds into
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The backend that integrates Ollama

### Specifications
- [Architecture](../specs/technical/architecture.md) — System architecture including query flow
- [Observability](../specs/technical/observability.md) — How routing fallbacks are monitored

### Operational
- [Troubleshooting](../guides/troubleshooting.md) — Ollama debugging and restart procedures
- [Root AGENTS.md](../../AGENTS.md) — Health check commands and observability overview

### Code References
- [`api/services/query_classifier.py`](../../api/services/query_classifier.py) — Intent classifier
- [`api/services/query_router.py`](../../api/services/query_router.py) — Pipeline routing built on the classifier
- [`api/services/ollama_client.py`](../../api/services/ollama_client.py) — Ollama client wrapper used by the classifier
