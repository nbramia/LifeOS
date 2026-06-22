# HTTP Client Surfaces

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-06-22

LifeOS exposes the orchestrator to **HTTP consumers** — thin clients that submit text and consume SSE without importing LifeOS Python modules. Endpoint and event **shapes** are defined in [api-reference.md](../product/api-reference.md); this doc covers **who consumes them**, **whisper-relay integration**, and **breaking-change policy**.

---

## Surfaces

| Surface | Transport | Chat/conversation endpoints |
|---------|-----------|----------------------------|
| Web chat | Browser → FastAPI | personas, ask/stream, handoff, conversation CRUD — `web/index.html` + `web/chat/` |
| Telegram | In-process `chat_via_api` | Same SSE as ask/stream; handoffs spawn in-process — `api/services/telegram.py` |
| **whisper-relay** | Separate app → HTTP | ask/stream, handoff, `GET /api/conversations`, `GET /api/conversations/{id}` — see below |
| **Voice (web)** | Browser → FastAPI reverse-proxy → whisper-relay | LifeOS proxies `/api/voice/*` to the gateway; turn SSE + audio — `web/chat/voice.js`, `api/routes/voice.py` |
| MCP / Managed Agents | stdio or HTTP MCP | Tool catalog only — `mcp_server.py` |

---

## whisper-relay

Voice transport in the same GitHub org as LifeOS: [github.com/nbramia/whisper-relay](https://github.com/nbramia/whisper-relay). Local checkout: `~/Code/whisper-relay` (review the consumer implementation in `src/voice_gateway/adapters/lifeos.py` when changing chat or conversation APIs).

Connects to LifeOS at `LIFEOS_BASE_URL` (default `http://127.0.0.1:8000`), 300s timeout on ask/stream, no auth headers — same trust model as web chat on localhost/Tailscale.

**Endpoints used** (request/response shapes: [api-reference.md](../product/api-reference.md)): `GET /api/personas`, `POST /api/ask/stream`, `POST /api/chat/handoff`, `GET /api/conversations`, `GET /api/conversations/{id}`.

**Persona contract** (shared by web and voice; lets a thin client expose LifeOS's multi-bot personas without reading LifeOS config):

- `GET /api/personas` lists selectable personas (`primary` + configured specialized bots). The client renders these as a picker; ids are stable, labels are display-only. The `primary` persona and orchestrating bots (e.g. the doctor self-repair bot) carry `handoff`/`agent` capabilities — gate any handoff UI on a persona's `capabilities`, not on a hardcoded `primary` check.
- Send the chosen `persona_id` on `POST /api/ask/stream`. The server applies the matching persona preamble and tags a newly created conversation with that persona. Unknown ids and `persona`+`persona_id` together are **400** — surface as a turn-level failure, not a crash.
- Scope the thread sidebar with `GET /api/conversations?persona_id=<id>`. Omitting the param shows the `primary` persona's threads (default web behavior). Conversation detail (`GET /api/conversations/{id}`) is not persona-scoped — fetch by id directly.

Web chat implements this contract in `web/chat/persona.js`: a header `<select>` populated from `/api/personas`, the selection persisted across refresh in `sessionStorage` (`lifeos:chat:persona_id`), capability-gated `claude_intent` handoff, and a persona-scoped sidebar. Switching persona starts a fresh, persona-scoped conversation.

**Consumer-specific behavior** (LifeOS API unchanged; how whisper-relay interprets it):

- Speaks `status` events immediately during long tool rounds.
- On `claude_intent`, POSTs handoff after the SSE stream ends; **replaces** accumulated `content` with the handoff confirmation (does not append).
- Reads handoff `message`, then `ack`, then a generic phrase using `session_id`; non-200 handoff still completes the turn with a spoken failure message.
- Proxies conversation list/detail to its mobile UI (`/api/voice/conversations*`). No `channel` or modality field — voice text is identical to typed chat at the API layer.
- Cancel = close the SSE connection (LifeOS has no cancel endpoint).

Upstream mirror of this integration: `whisper-relay/docs/adr/002-upstream-integration-boundaries.md`.

---

## Voice transport (reverse proxy)

With #361, LifeOS `/chat` is the unified text+voice client. Voice *transport* stays in whisper-relay; LifeOS **reverse-proxies** `/api/voice/*` to `LIFEOS_VOICE_GATEWAY_URL` (default `http://127.0.0.1:9788`) so the browser stays same-origin for the mic (HTTPS) and audio. LifeOS adds no voice logic — it forwards and streams both directions (`api/routes/voice.py`). See [ADR-016](../../adr/016-voice-gateway-reverse-proxy.md).

**Voice turn contract** (the gateway's API, reached through the proxy):

- `POST /api/voice/turn/stream` — multipart `audio` (or `transcript`) + `backend` (`lifeos`|`agent`) + `persona_id` (lifeos backend) + `conversation_id`. Responds with an SSE stream: `started` (turn_id, for cancel), `transcript`, `status_audio`/`main_audio` (clip URLs, played as they arrive), `response`, and a terminal `done` whose `data` is **authoritative**: `{transcript, response_text, status_audio_urls, audio_url, conversation_id, handoff, timings_ms}`. `error`/`cancelled` terminate the turn.
- `POST /api/voice/turn/{turn_id}/cancel` — cancel an in-flight turn.
- `GET /api/voice/audio/{turn_id}/{clip_id}` — WAV clips (status + main).

**Topology:** browser → LifeOS `/chat` → (proxy) → gateway → for the `lifeos` backend, the gateway calls back into LifeOS `/api/ask/stream` (the consumer-into-LifeOS direction above). Conversation/persona listing is owned by LifeOS, not the gateway.

Web chat implements voice mode in `web/chat/voice.js` — a faithful port of whisper-relay's `static/app.js` turn lifecycle (Voice|Text toggle, hold-to-talk, render the transcript + response from the `done` data, sequential audio queue, cancel via `AbortController`), adapted to LifeOS state and same-origin endpoints. The mode persists in `sessionStorage` (`lifeos:chat:voice_mode`).

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
