# HTTP Client Surfaces

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-06-21

LifeOS exposes the orchestrator to **HTTP consumers** — thin clients that submit text and consume SSE without importing LifeOS Python modules. Endpoint and event **shapes** are defined in [api-reference.md](../product/api-reference.md); this doc covers **who consumes them**, **whisper-relay integration**, and **breaking-change policy**.

---

## Surfaces

| Surface | Transport | Chat/conversation endpoints |
|---------|-----------|----------------------------|
| Web chat | Browser → FastAPI | ask/stream, handoff, conversation CRUD — `web/index.html` |
| Telegram | In-process `chat_via_api` | Same SSE as ask/stream; handoffs spawn in-process — `api/services/telegram.py` |
| **whisper-relay** | Separate app → HTTP | ask/stream, handoff, `GET /api/conversations`, `GET /api/conversations/{id}` — see below |
| MCP / Managed Agents | stdio or HTTP MCP | Tool catalog only — `mcp_server.py` |

---

## whisper-relay

Voice transport in the same GitHub org as LifeOS: [github.com/nbramia/whisper-relay](https://github.com/nbramia/whisper-relay). Local checkout: `~/Code/whisper-relay` (review the consumer implementation in `src/voice_gateway/adapters/lifeos.py` when changing chat or conversation APIs).

Connects to LifeOS at `LIFEOS_BASE_URL` (default `http://127.0.0.1:8000`), 300s timeout on ask/stream, no auth headers — same trust model as web chat on localhost/Tailscale.

**Endpoints used** (request/response shapes: [api-reference.md](../product/api-reference.md)): `GET /api/personas`, `POST /api/ask/stream`, `POST /api/chat/handoff`, `GET /api/conversations`, `GET /api/conversations/{id}`.

**Persona contract** (shared by web and voice; lets a thin client expose LifeOS's multi-bot personas without reading LifeOS config):

- `GET /api/personas` lists selectable personas (`primary` + configured specialized bots). The client renders these as a picker; ids are stable, labels are display-only. Only `primary` carries `handoff`/`agent` capabilities — gate any handoff UI on that.
- Send the chosen `persona_id` on `POST /api/ask/stream`. The server applies the matching persona preamble and tags a newly created conversation with that persona. Unknown ids and `persona`+`persona_id` together are **400** — surface as a turn-level failure, not a crash.
- Scope the thread sidebar with `GET /api/conversations?persona_id=<id>`. Omitting the param shows the `primary` persona's threads (default web behavior). Conversation detail (`GET /api/conversations/{id}`) is not persona-scoped — fetch by id directly.

**Consumer-specific behavior** (LifeOS API unchanged; how whisper-relay interprets it):

- Speaks `status` events immediately during long tool rounds.
- On `claude_intent`, POSTs handoff after the SSE stream ends; **replaces** accumulated `content` with the handoff confirmation (does not append).
- Reads handoff `message`, then `ack`, then a generic phrase using `session_id`; non-200 handoff still completes the turn with a spoken failure message.
- Proxies conversation list/detail to its mobile UI (`/api/voice/conversations*`). No `channel` or modality field — voice text is identical to typed chat at the API layer.
- Cancel = close the SSE connection (LifeOS has no cancel endpoint).

Upstream mirror of this integration: `whisper-relay/docs/adr/002-upstream-integration-boundaries.md`.

---

## Before changing chat or conversation APIs

1. Read [api-reference.md](../product/api-reference.md) § Chat and Conversations endpoints.
2. Compare `web/index.html`, `api/services/telegram.py`, and `~/Code/whisper-relay/src/voice_gateway/adapters/lifeos.py`.
3. Run contract tests listed in [testing-standards.md](../standards/testing-standards.md#http-client-contract-tests).
4. Treat removals or renames of public fields/events as a **breaking change** — update whisper-relay in the same release or maintain backward compatibility.

---

## Related Documents

### Specifications
- [API Reference](../product/api-reference.md) — Canonical endpoint and SSE shapes
- [Chat UI](../product/chat-ui.md) — Web chat product behavior
- [Architecture](architecture.md) — Code layout for chat routes and services
- [Testing Standards](../standards/testing-standards.md) — Contract regression tests

### Code References
- [Chat route](../../api/routes/chat.py) — SSE emission and handoff handler
- [Conversations route](../../api/routes/conversations.py) — List/detail handlers
