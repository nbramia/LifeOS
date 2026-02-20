# ADR-001: Python/FastAPI as Primary Stack

> **Decision:** Use Python 3.11+ with FastAPI as the backend framework for LifeOS.
> **Date:** 2026-02-19
> **Status:** Accepted
> **Last Updated:** 2026-02-19

## Context

LifeOS is a self-hosted AI assistant that indexes personal data (notes, emails, messages, photos, financial data) for semantic search and synthesis. The backend framework choice is foundational — it determines the ecosystem of libraries available, development velocity, deployment model, and long-term maintainability.

The backend needs to integrate tightly with ML/NLP libraries (embedding models, vector stores, LLM clients), support async I/O for concurrent API requests, SSE streaming, and external service calls, and enable rapid prototyping for a single-developer project where iteration speed matters more than raw throughput. It must also generate an OpenAPI schema automatically for MCP tool integration, which enables Claude to discover and call LifeOS tools programmatically.

The project is solo-developed and self-hosted on a Mac Mini. Enterprise-scale concerns (horizontal scaling, microservices, container orchestration) are not relevant. The right choice optimizes for developer productivity, library ecosystem breadth, and ML/AI integration depth.

## Decision

Python 3.11+ with the FastAPI framework, using Pydantic for request/response validation and uvicorn as the ASGI server.

## Rationale

- **ML ecosystem**: Python has first-class support for sentence-transformers, ChromaDB, the Anthropic SDK, and Ollama bindings. No other language comes close for ML tooling breadth or library maturity.
- **FastAPI strengths**: Native async/await, automatic OpenAPI spec generation (critical for MCP tool discovery), Pydantic validation, and dependency injection.
- **Development velocity**: Single-developer project benefits from Python's rapid prototyping cycle. Type hints + Pydantic catch errors early without Java-level ceremony.
- **Community**: Large ecosystem of middleware, auth libraries, and deployment guides. Problems are well-documented and solutions are readily available.

## Alternatives Considered

### Go

Go offers excellent performance, low memory usage, and easy deployment via static binaries. For a networked service handling concurrent requests, Go's goroutine model is compelling. However, the ML/NLP ecosystem in Go is immature — there are no native equivalents to sentence-transformers, ChromaDB's Python client, or the Anthropic SDK. Using Go would require either CGo bindings (which negate many of Go's deployment advantages) or running Python as a sidecar service for all ML operations, effectively maintaining two runtimes. For a project where ML integration is central, not peripheral, this tradeoff is unfavorable.

### Node.js / TypeScript

Node.js has strong async capabilities and TypeScript provides good type safety. However, the ML library ecosystem in JavaScript is significantly weaker than Python's. Critical dependencies — sentence-transformers for embeddings, ChromaDB's native client, Ollama's Python bindings — either don't exist in JS or are community-maintained wrappers around Python. Using Node would require a Python sidecar for embedding generation and vector operations anyway, adding operational complexity without meaningful benefit. The JavaScript ecosystem excels at frontend and real-time applications, but LifeOS's core workload is ML-heavy backend processing.

### Django

Django is a mature, full-featured Python framework with ORM, admin interface, template engine, and authentication built in. However, this "batteries included" approach adds weight that LifeOS doesn't need — there's no relational ORM (data lives in SQLite with raw queries and ChromaDB), no need for Django's template engine (frontend is static HTML/JS), and the admin interface adds complexity without value for a single-user system. Django's async support via Django Channels requires additional setup and is less native than FastAPI's built-in async. FastAPI's lighter footprint and OpenAPI generation are better fits.

### Flask

Flask is lightweight and flexible but lacks native async support — a significant limitation for a server that must handle concurrent API requests, SSE streaming, and external service calls simultaneously. Flask also has no built-in request validation (requiring marshmallow or similar) and no automatic OpenAPI generation. Reaching feature parity with FastAPI would require assembling multiple extensions (Flask-RESTful, Flask-Marshmallow, Flask-SocketIO), resulting in more code and more maintenance surface than using FastAPI directly.

## Consequences

**Positive:**
- Rapid development with excellent library support across ML, API, and data processing.
- Automatic OpenAPI generation enables seamless MCP tool integration.
- Async I/O handles concurrent requests, SSE streaming, and external API calls well.
- Easy to onboard contributors familiar with Python.

**Negative:**
- Slower than Go/Rust for CPU-bound tasks (mitigated: CPU-heavy work is in C extensions like numpy/torch, not pure Python).
- GIL limits true parallelism for CPU-bound threads (mitigated: workload is I/O-bound, and async handles concurrency).
- Deployment requires managing a Python virtual environment and dependencies.

**Risks:**
- Python's packaging ecosystem remains fragile — dependency conflicts and version pinning require ongoing vigilance. The external venv (see ADR-005) adds a layer of operational complexity.
- If LifeOS ever needs to handle significantly higher throughput (e.g., serving multiple users or real-time indexing), Python's per-request overhead may become a bottleneck. This would require profiling and potentially offloading hot paths to compiled extensions.
- FastAPI's rapid release cycle occasionally introduces breaking changes in minor versions. Pinning uvicorn and FastAPI versions in requirements.txt is essential.

## Related Documents

**Design Context:**
- [ADR-005: External Venv](005-external-venv-macos-tcc.md) — Why the virtual environment lives outside the project directory

**Specifications:**
- [Architecture](../specs/technical/architecture.md) — Code structure and module layout built on this stack
- [Python Conventions](../specs/standards/python-conventions.md) — Coding standards for this Python/FastAPI codebase

**Operational:**
- [Installation Guide](../guides/installation.md) — How to set up the Python environment
- [Scripts Reference](../guides/scripts.md) — Server management scripts that wrap uvicorn
