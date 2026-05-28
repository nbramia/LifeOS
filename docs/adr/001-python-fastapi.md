# ADR-001: Python/FastAPI as Primary Stack

**Status:** Complete
**Last Updated:** 2026-02-19
**Decision:** Accepted

## Context

LifeOS is a self-hosted AI assistant that indexes personal data (notes, emails, messages, photos, financial data) for semantic search and synthesis. The backend framework choice is foundational — it determines the ecosystem of libraries available, development velocity, deployment model, and long-term maintainability.

The backend needs to integrate tightly with ML/NLP libraries (embedding models, vector stores, LLM clients), support async I/O for concurrent API requests, SSE streaming, and external service calls, and enable rapid prototyping for a single-developer project where iteration speed matters more than raw throughput. It must also generate an OpenAPI schema automatically for MCP tool integration, which enables Claude to discover and call LifeOS tools programmatically.

The project is solo-developed and self-hosted. Enterprise-scale concerns (horizontal scaling, microservices, container orchestration) are not relevant. The right choice optimizes for developer productivity, library ecosystem breadth, and ML/AI integration depth.

## Decision

Use Python 3.11+ with the FastAPI framework, Pydantic for request/response validation, and uvicorn as the ASGI server.

## Rationale

- **ML ecosystem**: Python has first-class support for sentence-transformers, ChromaDB, the Anthropic SDK, and Ollama bindings. No other language comes close for ML tooling breadth or library maturity.
- **FastAPI strengths**: Native async/await, automatic OpenAPI spec generation (critical for MCP tool discovery), Pydantic validation, dependency injection.
- **Development velocity**: A single-developer project benefits from Python's rapid prototyping cycle. Type hints + Pydantic catch errors early without Java-level ceremony.
- **Community**: Large ecosystem of middleware, auth libraries, and deployment guides. Problems are well-documented and solutions are readily available.

## Alternatives Considered

### Go

Go offers excellent performance, low memory usage, and easy deployment via static binaries. The goroutine model is compelling for a networked service handling concurrent requests.

**Rejected because:** The ML/NLP ecosystem in Go is immature — no native equivalents to sentence-transformers, ChromaDB's Python client, or the Anthropic SDK. Using Go would require either CGo bindings (which negate many of Go's deployment advantages) or running Python as a sidecar service for all ML operations, effectively maintaining two runtimes. For a project where ML integration is central, not peripheral, the tradeoff is unfavorable.

### Node.js / TypeScript

Node.js has strong async capabilities and TypeScript provides good type safety.

**Rejected because:** The ML library ecosystem in JavaScript is significantly weaker than Python's. Critical dependencies — sentence-transformers, ChromaDB's native client, Ollama bindings — either don't exist in JS or are community-maintained wrappers around Python. Using Node would require a Python sidecar for embedding generation and vector operations anyway, adding operational complexity without meaningful benefit.

### Django

Django is a mature, full-featured Python framework with ORM, admin, template engine, and authentication built in.

**Rejected because:** Its "batteries included" approach adds weight LifeOS doesn't need — no relational ORM (data lives in SQLite with raw queries and ChromaDB), no template engine (frontend is static HTML/JS), no admin interface for a single-user system. Django's async support via Channels requires additional setup and is less native than FastAPI's built-in async. FastAPI's lighter footprint and built-in OpenAPI generation are a better fit.

### Flask

Flask is lightweight and flexible.

**Rejected because:** It lacks native async support — a significant limitation for a server that must handle concurrent API requests, SSE streaming, and external service calls simultaneously. Flask has no built-in request validation and no automatic OpenAPI generation. Reaching FastAPI parity requires assembling multiple extensions (Flask-RESTful, Flask-Marshmallow, Flask-SocketIO), resulting in more code and more maintenance than using FastAPI directly.

## Consequences

### Positive

- Rapid development with excellent library support across ML, API, and data processing.
- Automatic OpenAPI generation enables seamless MCP tool integration.
- Async I/O handles concurrent requests, SSE streaming, and external API calls well.
- Easy to onboard contributors familiar with Python.

### Negative

- Slower than Go/Rust for CPU-bound tasks (mitigated: CPU-heavy work is in C extensions like numpy/torch, not pure Python).
- GIL limits true parallelism for CPU-bound threads (mitigated: workload is I/O-bound, async handles concurrency).
- Deployment requires managing a Python virtual environment and pinned dependencies.
- Python's packaging ecosystem remains fragile — dependency conflicts and version pinning require ongoing vigilance. The external venv (see [ADR-005](005-external-venv-macos-tcc.md)) adds a layer of operational complexity.
- FastAPI's release cycle occasionally introduces breaking changes in minor versions. Pinning `uvicorn` and `fastapi` versions in `requirements.txt` is essential.
- If LifeOS ever needs to serve significantly higher throughput (multiple users, real-time indexing), Python's per-request overhead may become a bottleneck — would require profiling and offloading hot paths to compiled extensions.

## Related Documents

### Design Context
- [ADR-005: External Venv](005-external-venv-macos-tcc.md) — Why the virtual environment lives outside the project directory (TCC-driven; superseded by ADR-007)
- [ADR-007: Linux Migration](007-linux-migration.md) — Stack carried forward to Linux; venv convention retained

### Specifications
- [Architecture](../specs/technical/architecture.md) — Code structure and module layout built on this stack
- [Python Conventions](../specs/standards/python-conventions.md) — Coding standards for this Python/FastAPI codebase

### Operational
- [Installation Guide](../guides/installation.md) — How to set up the Python environment
- [Scripts Reference](../guides/scripts.md) — Server management scripts that wrap uvicorn

### Code References
- [`api/main.py`](../../api/main.py) — FastAPI application entry point
- [`requirements.txt`](../../requirements.txt) — Pinned dependency set
