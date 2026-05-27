# ADR-007: Linux Migration and Local LLM Orchestration

> **Decision:** Migrate the LifeOS server from Mac Mini (macOS) to a Linux workstation, and replace the Claude API orchestrator with a local LLM.
> **Date:** 2026-03-04
> **Status:** Accepted
> **Last Updated:** 2026-05-27
> **Supersedes:** [ADR-005](005-external-venv-macos-tcc.md) (external venv rationale — TCC no longer applies, but the practice is retained by convention)
> **Amended by:** [ADR-009](009-llm-backend-toggle.md) (the local-first orchestrator default became an operator-configurable toggle)

## Context

LifeOS was running on a Mac Mini (macOS) as its primary server, with all AI orchestration (agentic chat loop, RAG synthesis) handled by the Claude API (Anthropic). This architecture worked but had growing limitations:

1. **Compute ceiling**: The Mac Mini lacked GPU resources for running large local models. Growing interest in local LLM orchestration (for privacy, cost, and latency) required hardware with significant VRAM — at least 96GB to run a 120B-parameter model.

2. **Cost accumulation**: Every agentic chat interaction required multiple Claude API calls (tool-calling rounds, synthesis, streaming). For a personal assistant handling daily queries, API costs accumulated steadily with no upper bound.

3. **Privacy posture**: Although Claude API calls are discrete and LifeOS stores no data remotely, the orchestrator — which sees the full context of every query including personal notes, emails, and messages — was running through a third-party API. Running orchestration locally eliminates this data transit entirely.

4. **Platform constraints**: macOS-specific requirements (launchd, FDA wrappers, TCC scanning workarounds) added operational complexity. The `LifeOS.app` FDA wrapper, launchd plists, and TCC-driven venv placement (ADR-005) were all workarounds for macOS restrictions that don't exist on Linux.

5. **ROCm and GPU ecosystem**: The target workstation ships with an AMD GPU requiring ROCm, which has mature Linux support but no macOS support.

## Decision

### Primary server migration

Migrate the LifeOS server from the Mac Mini (macOS) to a Linux workstation with a high-VRAM AMD GPU. The Linux machine becomes the primary host for:
- FastAPI server
- ChromaDB vector store
- Ollama (query routing)
- Local LLM server (orchestration and synthesis)
- Nightly sync pipeline
- All portable data sources (Gmail, Calendar, Slack, financial data, notes vault)

### Local LLM for orchestration

Replace the Claude API as the orchestrator with a local LLM (GPT-OSS-120B) served via `llama-server` on the local GPU. The agentic chat loop and RAG synthesis — the highest-volume LLM calls — now run entirely locally.

Create a unified LLM client (`api/services/llm_client.py`) that abstracts over both local (OpenAI-compatible) and Anthropic backends. This client handles:
- Bidirectional tool schema translation between Anthropic format (`input_schema`) and OpenAI format (`parameters`)
- Streaming format differences
- Stop reason normalization (`tool_use` vs `tool_calls`)
- System prompt format conversion (content blocks vs single string)

Add `LIFEOS_LLM_BACKEND` setting to `config/settings.py` to switch between `local` and `anthropic` backends without code changes. Specialized Claude API calls (relationship insights, fact extraction, tone analysis, web search) are retained on Anthropic where frontier model quality provides clear value.

### Mac Mini role change

Demote the Mac Mini to an "Apple Data Agent" role. It runs a single nightly cron job that:
1. Exports Apple-only data (iMessage, phone calls, contacts, photos face data)
2. Pushes exports to the Linux server via rsync/SSH

### Service management

Replace macOS launchd and FDA wrappers with systemd on Linux:
- `lifeos-api.service` — API server (uses `scripts/server.sh foreground`)
- `lifeos-chromadb.service` — ChromaDB vector store
- `lifeos-sync.timer` — Nightly sync pipeline
- `lifeos-watchdog.timer` — ChromaDB health monitoring

The `LifeOS.app` FDA wrapper is no longer needed. Linux has no TCC-equivalent file access restrictions.

### Codebase compatibility

Keep the codebase dual-compatible (Linux and macOS) via OS-detection wrappers:
- Shell scripts use `uname` checks for BSD vs GNU tool differences (e.g., `stat -f%z` vs `stat -c%s`)
- Python code uses `sys.platform` guards for macOS-specific imports (e.g., `pyobjc`)
- Scripts synced between the MacBook (macOS, editor) and Linux (server) must work on both platforms

### External venv convention

The external venv pattern from ADR-005 (`~/.venvs/lifeos`) is retained by convention, even though the original TCC motivation no longer applies on Linux. The practice has proven useful regardless of platform: it keeps the venv out of file sync tools (previously iCloud, now Syncthing) and avoids bloating the project directory with machine-specific binary artifacts.

## Rationale

- **Privacy**: The orchestrator — which processes the full context of every query — now runs locally. No personal data transits to any external API for the primary chat flow.
- **Cost**: Orchestration and synthesis (the highest-volume API calls) become zero marginal cost. Only specialized calls (relationship insights, fact extraction, web search) still use paid API tiers.
- **Latency**: Local LLM inference on a high-VRAM GPU eliminates network round-trips for the agentic loop, reducing per-turn latency.
- **Hardware utilization**: A 96GB VRAM GPU can run a 120B-parameter model with overhead for prefix caching and concurrent requests — capability that was impossible on the Mac Mini.
- **Operational simplicity**: systemd is more transparent and debuggable than launchd + FDA wrappers. No more TCC scanning delays, no more `.app` bundles for file access permissions.
- **Retained flexibility**: The `LIFEOS_LLM_BACKEND` toggle and unified LLM client mean the system can fall back to the Claude API for orchestration if local model quality degrades for a specific use case. Specialist calls remain on Anthropic regardless.

## Alternatives Considered

### Keep everything on Claude API (no local LLM)

Running LifeOS on Linux with all LLM calls still going to the Claude API was the simplest option and was used as a stabilization milestone (Phases 1-2 of the migration). However, this leaves the cost and privacy motivations unaddressed. The local LLM was the primary driver for acquiring high-VRAM hardware in the first place. Keeping Claude API as the sole orchestrator would make the GPU investment largely wasted.

### Full local (no Claude API at all)

Replacing all Claude API calls — including relationship insights (Opus), fact extraction (Sonnet/Haiku), tone analysis (Sonnet), and web search (Sonnet with built-in web search tool) — with local models. This was rejected because frontier model quality provides measurable value for these specialized tasks. Relationship analysis over sensitive personal data benefits from Opus-level reasoning. Web search requires Anthropic's built-in search tool, which has no local equivalent. The hybrid approach (local orchestrator + cloud specialists) captures the majority of cost and privacy benefits while preserving quality where it matters most.

### vLLM instead of llama-server

vLLM was initially planned as the local inference server. However, llama-server (from llama.cpp) provided better ROCm compatibility with the target GPU and simpler deployment for a single-user system. vLLM's strengths (high-throughput batching, PagedAttention for concurrent users) are less relevant for a system serving one user. llama-server's lower operational overhead was a better fit.

### Cloud VM with GPU

Running LifeOS on a cloud VM with an attached GPU (e.g., AWS p4d, Lambda Labs) would avoid hardware procurement. However, this contradicts LifeOS's core principle that all data stays local. Hosting personal emails, messages, therapy notes, and financial data on a cloud VM — even one you control — introduces attack surface and ongoing cost that self-hosted hardware avoids. The workstation is a one-time capital expense with no recurring cloud bills.

## Consequences

**Positive:**
- Orchestration runs locally — no personal data sent to external APIs for the primary chat flow.
- Zero marginal cost for orchestration and synthesis (previously the largest API expense).
- Lower latency for agentic chat interactions (no network round-trips per tool-calling round).
- systemd provides cleaner service management than launchd + FDA wrappers.
- Hardware overhead allows future model upgrades without architecture changes.
- Dual-compatible codebase means development workflow (edit on MacBook, run on server) is preserved.

**Negative:**
- Hardware dependency: LifeOS now requires specific GPU hardware. If the workstation fails, there is no automatic fallback (though `LIFEOS_LLM_BACKEND=anthropic` can restore Claude API orchestration manually).
- Model quality variance: The local 120B model may underperform Claude for certain query types, particularly complex multi-step reasoning or nuanced tool selection. Ongoing prompt tuning may be required.
- Operational complexity: Two machines (Linux server + Mac for Apple data) instead of one Mac Mini. The Apple Data Agent adds a nightly rsync dependency.
- ROCm ecosystem: AMD GPU tooling (ROCm, PyTorch ROCm builds) is less mature than NVIDIA CUDA. Occasional compatibility issues with new PyTorch versions are expected.
- Dual-platform maintenance: OS-detection wrappers in shell scripts and Python add conditional complexity that must be tested on both platforms.

**Risks:**
- Local model tool-calling reliability may be lower than Claude's, causing agent loop failures or degraded responses. Mitigation: the backend toggle allows falling back to Claude API per-session or globally.
- Apple Data Agent timing: if the Mac fails to export before the nightly sync runs on Linux, Apple data sources will be stale. Mitigation: staleness checks and alerting before sync.
- ROCm version drift: PyTorch ROCm builds may lag behind CUDA builds, limiting access to the latest model optimizations. Currently mitigated with `HSA_OVERRIDE_GFX_VERSION` for GPU compatibility.

## Related Documents

**Design Context:**
- [ADR-005: External Venv](005-external-venv-macos-tcc.md) — Superseded; TCC rationale no longer applies, but external venv practice retained
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The backend stack, unchanged by this migration

**Specifications:**
- [Architecture](../specs/technical/architecture.md) — System architecture (updated for Linux deployment)
- [Data & Sync](../specs/technical/data-and-sync.md) — Sync pipeline details (Apple Data Agent added)
- [Observability](../specs/technical/observability.md) — Monitoring and alerting (systemd integration)

**Plans (Archived):**
- [Linux Migration Plan](../plans/archive/2026-03-linux-migration.md) — Full migration plan (Phases 1-6)
- [Remaining Steps](../plans/archive/linux-migration-remaining-steps.md) — Post-migration operational checklist
