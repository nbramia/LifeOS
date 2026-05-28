# ADR-012: Embedding Pipeline (GPU Acceleration + CPU Fallback)

**Status:** Complete
**Last Updated:** 2026-05-27
**Decision:** Accepted

## Context

LifeOS indexes ~100K personal documents (Obsidian notes, emails, calendar events, messages, transcripts, etc.) for semantic search. The embedding pipeline — model choice, hardware utilization, OOM behavior, library compatibility — directly determines:

- Search quality (higher-dimensional embeddings on a stronger encoder produce measurably better retrieval).
- ChromaDB collection dimension (changing the model requires a full reindex).
- Hardware requirements (a 1.5B-parameter encoder needs meaningful VRAM; a small encoder runs anywhere).
- Library pinning (newer encoder models require specific `transformers` versions).
- Failure modes (running an encoder out of VRAM mid-batch has historically killed the box).

LifeOS targets a wide range of operator hardware: high-VRAM Linux workstations on the maintainer's end, modest Macs and lower-VRAM systems on other operators'. The embedding pipeline must default to a quality model for the maintainer's tier while being overridable for constrained hardware, and must never crash the box when the GPU is unhappy.

This decision was made incrementally — original model selection (`mxbai-embed-large-v1`, 1024-dim, [ADR-004](004-hybrid-search.md) was written against this model), the 2026-03-05 upgrade to `gte-Qwen2-1.5B-instruct` (1536-dim, included a full reindex), the GPU→CPU fallback added after a hard hang during a long embed batch, and the pre-flight RAM check added after a kernel OOM-kill on a tight-memory run. This ADR backfills the consolidated record.

## Decision

- **Library**: `sentence-transformers` for the encoder runtime. It's the dominant Python library and has the catalog of supported models.
- **Default model**: `Alibaba-NLP/gte-Qwen2-1.5B-instruct` (1536-dim). Overridable via `LIFEOS_EMBEDDING_MODEL` for constrained hardware.
- **Hardware**: GPU (ROCm on AMD, CUDA on NVIDIA) when available. Embedding is the most GPU-hungry phase of the sync pipeline.
- **Fallback semantics**: any HIP / CUDA / GPU error during load or encode falls back to CPU automatically. The error is tracked as a degradation event in the alerting system. CPU is 10–20× slower but keeps the box up.
- **Pre-flight RAM check**: `run_all_syncs.py` checks free system RAM before phase 4 (embedding). If free RAM is below `LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB` (default 28000), the phase is skipped with a clear log line rather than risking a kernel OOM kill.
- **LLM coordination**: the local LLM (`lifeos-llm`) is paused before embedding phases when both can't coexist in VRAM, and restarted afterward. (Documented in [data-and-sync.md](../specs/technical/data-and-sync.md).)
- **Library pinning**: `transformers<5` is required for native Qwen2 support without `trust_remote_code`. The 5.x branch broke Qwen2's `rope_theta` and `tokenizer_qwen2_fast` paths.
- **Input sanitation**: `api/services/chunker.py:_strip_base64_images` removes `data:image/...;base64,...` URLs before chunking. Granola meeting notes inline base64 images; embedding the raw markdown OOMs the encoder. Replacing the payload with `[image]` keeps the chunk small.

## Rationale

- **`sentence-transformers` is the gravitational center** of Python embedding tooling. Choosing it inherits the model catalog, the encode-batch ergonomics, and the community's worked examples without operator overhead.
- **`gte-Qwen2-1.5B-instruct` outperformed mxbai on internal evals.** The 1024→1536 dimension change is a one-time reindex cost; the per-query quality lift is permanent. Worth it for a system whose primary value is retrieval quality.
- **GPU is essential at vault scale.** 100K+ docs encoded on CPU is hours; on GPU it's minutes. For routine nightly sync, the GPU path is the only one that fits the operator's expectation of "wakes up tomorrow with fresh data."
- **Fallback semantics keep the box up.** GPU OOM, ROCm misbehavior, or a transient HIP error has historically taken down the box (kernel OOM, then the whole sync stack). Catching `HIP error: out of memory` (and friends) and silently moving to CPU is uglier on latency but vastly better on availability.
- **Pre-flight RAM gate prevents kernel OOM.** Embedding peaks at 15–22 GB transient memory for the default model. If free RAM is below threshold *before* the encode starts, skipping the phase is the right call — we'd OOM the kernel anyway, and a kernel OOM kill is much worse than a skipped sync phase.
- **Operator override.** `LIFEOS_EMBEDDING_MODEL` is the supported escape hatch. Smaller models (`all-MiniLM-L6-v2`, 384-dim, ~80 MB; `mxbai-embed-large-v1`, 1024-dim, ~1.3 GB) run on modest hardware. The dimension change requires a one-time reindex, and operators on different hardware shouldn't be forced into the maintainer's model choice.

## Alternatives Considered

### Stay on `mxbai-embed-large-v1` (1024-dim)

Keep the original model; skip the reindex.

**Rejected because:** Qwen2 outperformed on internal retrieval evals. The reindex cost is one-time; the quality lift is permanent. For a system whose primary value is search quality, paying the one-time cost was the right call.

### ONNX or quantized inference

Convert the embedding model to ONNX (or use a quantized variant) for faster CPU and lower memory.

**Rejected for now:** Adds runtime complexity (ONNX runtime, quantization tooling, accuracy validation per quant level). Marginal benefit on current hardware where GPU is the primary path. The fallback case is tolerable as-is; chasing CPU performance for an edge case adds maintenance surface without commensurate value. Worth revisiting if many operators end up CPU-bound.

### Cloud embedding API (OpenAI `text-embedding-3-large` or similar)

Use a hosted embedding API instead of local inference.

**Rejected because:** Violates LifeOS's local-first principle. Sending every chunk of every personal document through a third-party API is a hard privacy regression — and a recurring cost ($0.13/M tokens for `text-embedding-3-large` at the time of writing) that scales with corpus growth. The whole point of LifeOS is that personal data stays local.

### Self-hosted Triton inference server

Run a Triton Inference Server alongside `llama-server` for embedding inference.

**Rejected because:** Operational overhead disproportionate to the use case. Triton wins when you're serving multiple models at high concurrency to many clients; LifeOS serves one operator with one embedding model. The complexity (Docker, Triton config, model repo layout, GPU sharing across services) doesn't earn its keep.

### Train a custom embedding model

Fine-tune an embedding model on the operator's vault for personal-vocab specificity.

**Rejected because:** Requires labeled retrieval data the operator doesn't have, infrastructure for training, and per-operator model artifacts. Off-the-shelf models on a strong encoder generalize well enough for personal-data retrieval that the engineering cost isn't justified.

## Consequences

### Positive

- Quality: 1536-dim Qwen2 outperforms previous 1024-dim mxbai on retrieval evals.
- Local: no embeddings sent to a third-party API; no recurring cost; aligns with LifeOS's privacy posture.
- Resilient: GPU→CPU fallback never crashes the box on a HIP/CUDA error.
- Pre-flight RAM gate prevents the worst failure mode (kernel OOM mid-encode).
- Operator-overridable: smaller models (`all-MiniLM-L6-v2`, etc.) run on constrained hardware; `LIFEOS_EMBEDDING_MODEL` is the supported knob.
- Granola-style base64-image notes don't OOM the encoder thanks to `_strip_base64_images`.

### Negative

- Model change requires a full reindex (the ChromaDB collection dimension is per-collection). For a 100K-doc vault this is a non-trivial operation.
- `transformers` must be pinned `<5` for Qwen2 native support without `trust_remote_code`. New `transformers` major releases now require a compatibility check.
- CPU fallback is 10–20× slower than GPU. A long sync that lands on the CPU path can take hours instead of minutes.
- Pre-flight RAM check is a heuristic — the chosen threshold (28000 MB default) is empirical and may need re-tuning on different hardware. Too low → kernel OOM; too high → embedding skipped unnecessarily.
- The default model needs ~15-22 GB transient memory. Operators on systems with less than that *must* override `LIFEOS_EMBEDDING_MODEL` to a smaller encoder.
- Coordination with the local LLM (`lifeos-llm` stop/start around embedding phases) is brittle — both services compete for GPU and the sync pipeline has to choreograph the handoff.
- Granola-style sources that inline non-image binary blobs (audio? video frames? other encodings) could still OOM the encoder. The current strip is image-base64 only; broader sanitation may be needed if new sources appear.

### Migration history

- **2026-03-05**: Default model changed from `mixedbread-ai/mxbai-embed-large-v1` (1024-dim) to `Alibaba-NLP/gte-Qwen2-1.5B-instruct` (1536-dim). Required a full reindex; ChromaDB collection dimension changed.

### Override path

Set `LIFEOS_EMBEDDING_MODEL` in `.env`. Smaller models suitable for constrained hardware:

- `sentence-transformers/all-MiniLM-L6-v2` — 384-dim, ~80 MB, runs on CPU comfortably.
- `mixedbread-ai/mxbai-embed-large-v1` — 1024-dim, ~1.3 GB, the previous default.

Changing the model requires reindexing the ChromaDB collection. The sync pipeline handles this automatically on the next run with a new model when the collection dimension mismatches the encoder.

## Related Documents

### Design Context
- [ADR-002: ChromaDB](002-chromadb-vector-store.md) — Vector store that consumes the embeddings produced here
- [ADR-004: Hybrid Search](004-hybrid-search.md) — Search pipeline that uses these embeddings (RRF fusion with BM25)
- [ADR-007: Linux Migration](007-linux-migration.md) — Established the GPU posture this pipeline targets

### Specifications
- [Search Indexing](../specs/technical/search-indexing.md) — Hybrid search internals (RRF, reranking) that consume the embeddings
- [Data & Sync](../specs/technical/data-and-sync.md) — Embedding phase position in the seven-phase pipeline; LLM stop/start choreography

### Operational
- [Configuration](../guides/configuration.md) — `LIFEOS_EMBEDDING_MODEL`, `LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB`, `PYTORCH_CUDA_ALLOC_CONF` env vars (canonical home after #188)
- [Troubleshooting](../guides/troubleshooting.md) — OOM symptoms and recovery steps
- [Installation](../guides/installation.md) — GPU prerequisites (ROCm version, PyTorch wheel selection)

### Code References
- [`api/services/embeddings.py`](../../api/services/embeddings.py) — `EmbeddingService` with GPU→CPU fallback (`_encode_with_fallback`), supported dimensions table
- [`api/services/chunker.py`](../../api/services/chunker.py) — `_strip_base64_images` (line ~206) called from `_chunk_markdown` before encoding
- [`scripts/run_all_syncs.py`](../../scripts/run_all_syncs.py) — `_EMBEDDING_MEMORY_THRESHOLD_MB` gate, `_stop_llm_for_embeddings` choreography
