# HTTP Client Surfaces

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-08-18

LifeOS exposes the orchestrator to **HTTP consumers** — thin clients that submit text and consume SSE without importing LifeOS Python modules. Endpoint and event **shapes** are defined in [api-reference.md](../product/api-reference.md); this doc covers **who consumes them**, **whisper-relay integration**, and **breaking-change policy**.

---

## Surfaces

| Surface | Transport | Chat/conversation endpoints |
|---------|-----------|----------------------------|
| Web chat | Browser → FastAPI | personas, ask/stream, handoff, conversation CRUD — `web/index.html` + `web/chat/` |
| Telegram | In-process `chat_via_api` | Same SSE as ask/stream; handoffs spawn in-process — `api/services/telegram.py` |
| **whisper-relay** | Separate app → HTTP (voice transport API) | Server-side only: `POST /api/ask/stream`, handoff — via `src/voice_gateway/adapters/lifeos.py`; no browser UI after #21 |
| **Voice (web)** | Browser → LifeOS reverse-proxy → whisper-relay | LifeOS `/chat` + `web/chat/voice.js`; proxies `/api/voice/*` — `api/routes/voice.py` |
| MCP / Managed Agents | stdio or HTTP MCP | Tool catalog only — `mcp_server.py` |

---

## whisper-relay (voice transport API)

Voice transport in the same GitHub org: [github.com/nbramia/whisper-relay](https://github.com/nbramia/whisper-relay). Local checkout: `~/Code/whisper-relay`. After #21 it is **API-only** — no `static/` UI, no `/api/voice/personas` or `/api/voice/conversations*` proxies. Review `src/voice_gateway/adapters/lifeos.py` when changing chat or conversation APIs.

The gateway connects to LifeOS at `LIFEOS_BASE_URL` for orchestration (ask/stream, handoff). Persona listing and conversation CRUD are **LifeOS-owned** — consumed by `/chat` directly, not through whisper-relay.

**LifeOS endpoints the gateway calls** (shapes: [api-reference.md](../product/api-reference.md)): `POST /api/ask/stream`, `POST /api/chat/handoff`. At startup it may call `GET /api/personas` server-side for handoff capability caching (not exposed to browsers).

**Persona contract** (shared by web and voice; lets a thin client expose LifeOS's multi-bot personas without reading LifeOS config):

- `GET /api/personas` lists selectable personas (`primary` + configured specialized bots). The client renders these as a picker; ids are stable, labels are display-only. The `primary` persona and orchestrating bots (e.g. the doctor self-repair bot) carry `handoff`/`agent` capabilities — gate any handoff UI on a persona's `capabilities`, not on a hardcoded `primary` check.
- Send the chosen `persona_id` on `POST /api/ask/stream`. The server applies the matching persona preamble and tags a newly created conversation with that persona. Unknown ids and `persona`+`persona_id` together are **400** — surface as a turn-level failure, not a crash.
- Scope the thread sidebar with `GET /api/conversations?persona_id=<id>`. Omitting the param shows the `primary` persona's threads (default web behavior). Conversation detail (`GET /api/conversations/{id}`) is not persona-scoped — fetch by id directly.
- Send `modality: "voice"` on `POST /api/ask/stream` for spoken turns: the server appends the selected persona's `voice` frontmatter rules (speech formatting) to the system prompt. `None`/`"text"` is a normal typed turn. Honored only on the `persona_id` path (the raw-`persona` Telegram path has no id to key voice rules to). Set by the voice gateway (whisper-relay) — see whisper-relay#27.
- Selecting an **orchestrating** persona (`orchestrates: true`, e.g. `doctor`) on `POST /api/ask/stream` does **not** answer inline — the server spawns a background Claude Code session (the persona pipeline + the user's message, in the canonical checkout, tagged with that bot for `[NOTIFY]`/`[CLARIFY]`/completion) and streams a `content` ack + `done` (the only `claude_code` outcome that emits no `claude_intent`). Mirrors the Telegram orchestration path; results arrive via that bot's Telegram + the `/agents` page. Gated on `persona_id` (web/voice); Telegram's own path handles its bots, so this never double-spawns.
- **Answering a spawned session's `[CLARIFY]`/`[GOAL]` from web/voice:** on a successful spawn the server links the conversation to the spawned agent session (`conversations.agent_session_id`). If that session emits `[CLARIFY]`/`[GOAL]`, the worker registers an open question keyed on the session (not just a Telegram `message_id`), and the web/voice client can answer it **without Telegram**:
  - `GET /api/conversations/{id}` includes a `pending_question` object (`{session_id, question, kind}`) **only while** the spawned session is awaiting an answer; it is absent/`null` otherwise. The client renders an answer affordance when present.
  - `POST /api/conversations/{id}/answer` `{answer}` deposits the answer onto that **existing** open question via the session-keyed deposit, preserving its `kind`. The worker's existing tick (`_resume_goal` for `goal_approval`, `_resume_as_followup` / clarification otherwise) resumes the session — the **same** single resume mechanism Telegram replies use. **No second resume path.** Empty answer → **400**; no spawned session / no open question (never asked, already answered, or timed out) → **409**. The Telegram reply-to-resume round-trip is unchanged.
  - This is the **input** direction (answering / resuming). The complementary **output** direction (streaming the session's results back into the web thread) is #311; until then, results still arrive via the bot's Telegram + the `/agents` page.

Web chat implements this contract in `web/chat/persona.js`: a top-of-chat toolbar `<select>` populated from `/api/personas`, the selection persisted across refresh in `sessionStorage` (`lifeos:chat:persona_id`), capability-gated `claude_intent` handoff, and a persona-scoped sidebar. Switching persona starts a fresh, persona-scoped conversation.

**Model picker.** The same toolbar carries a per-turn model picker (`web/chat/model.js`): `Auto` (Haiku + escalation) / `Sonnet` / `Opus` / `Gemma (local)` / `Claude Code`. An explicit `Sonnet`/`Opus` pick is the operator asking for an API model, which is why it dispatches without a prompt — `Auto`'s own escalation only climbs to non-API engines. The choice persists in `sessionStorage` (`lifeos:chat:model`) and rides along on `/api/ask/stream` as `model_override` (omitted for `auto`, so the default request is unchanged). For the inline model picks the server pins the turn to that model — cloud picks reuse the per-turn escalation client; `gemma` builds a per-turn `LocalLLMClient`. Context is preserved the same way every turn is: the full `conversation_history` is replayed into the chosen client. `Claude Code` is not an inline model: selecting it short-circuits the turn into the same engine handoff the orchestrator emits for an inferred "use claude code" directive (a `claude_intent` SSE event → `/api/chat/handoff` → `spawn_claude_code_session`), so the message runs in a background Claude Code worker rather than answering inline, on any backend. The frontend treats an explicit `claude_code` pick as its own handoff opt-in, bypassing the persona capability gate that filters inferred handoffs. The **voice** surface carries the same model selection: `web/chat/voice.js` forwards `model_override` on `/api/voice/turn/stream`; whisper-relay relays it to `/api/ask/stream` (#24). The picker is shown in voice mode as well as text — hidden only on the Agent backend.

**Gateway behavior** (LifeOS API unchanged):

- Speaks `status` events immediately during long tool rounds.
- On `claude_intent`, POSTs handoff after the SSE stream ends; **replaces** accumulated `content` with the handoff confirmation.
- Explicit `model_override` of `claude_code`/`codex` enables handoff parsing even on personas without the `handoff` capability (whisper-relay ADR-004 / #24).
- Cancel = `POST /api/voice/turn/{turn_id}/cancel` (proxied).

Upstream mirror of this integration: `whisper-relay/docs/adr/002-upstream-integration-boundaries.md`.

---

## Voice transport (reverse proxy)

With #361, LifeOS `/chat` is the unified text+voice client. Voice *transport* stays in whisper-relay; LifeOS **reverse-proxies** `/api/voice/*` to `LIFEOS_VOICE_GATEWAY_URL` (default `http://127.0.0.1:9788`) so the browser stays same-origin for the mic (HTTPS) and audio. LifeOS adds no voice logic — it forwards and streams both directions (`api/routes/voice.py`). See [ADR-016](../../adr/016-voice-gateway-reverse-proxy.md).

**Voice turn contract** (the gateway's API, reached through the proxy):

- `POST /api/voice/turn/stream` — multipart `audio` (or `transcript`) + `backend` (`lifeos`|`agent`) + `persona_id` (lifeos backend) + `conversation_id`. Responds with an SSE stream: `started` (turn_id, for cancel), `transcript`, `status_audio`/`main_audio` (clip URLs, played as they arrive), `response`, and a terminal `done` whose `data` is **authoritative**: `{transcript, response_text, status_audio_urls, audio_url, conversation_id, handoff, timings_ms}`. `error`/`cancelled` terminate the turn.
- `POST /api/voice/turn/{turn_id}/cancel` — cancel an in-flight turn.
- `GET /api/voice/audio/{turn_id}/{clip_id}` — WAV clips (status + main).

**Topology:** browser → LifeOS `/chat` → (proxy) → gateway → for the `lifeos` backend, the gateway calls back into LifeOS `/api/ask/stream` (the consumer-into-LifeOS direction above). Conversation/persona listing is owned by LifeOS, not the gateway.

Web chat implements voice mode in `web/chat/voice.js` — tap-to-talk turn lifecycle (Voice|Text toggle, SSE `done` data, sequential audio, cancel via `AbortController`), same-origin via the reverse proxy. Mode persists in `sessionStorage` (`lifeos:chat:voice_mode`).

**Agent text backend.** Both modes carry a `backend` (`lifeos` | `agent`). The `agent` backend is the OpenClaw voice-adapter, which speaks the same `/api/ask/stream` SSE contract at `LIFEOS_AGENT_BACKEND_URL` and may require a bearer token. LifeOS proxies it at `POST /api/agent/ask/stream` (`api/routes/agent_proxy.py`), **adding the bearer server-side** so it never reaches the browser; `GET /api/agent/status` reports whether it's configured (drives the UI toggle). The agent backend has no personas and no handoff. Conversation ids are stored per backend in `sessionStorage` (`lifeos:chat:conv:agent`, and `lifeos:chat:conv:lifeos:<persona>` for lifeos) so switching and refreshing continues the right thread. `web/chat/backend.js` owns the toggle + per-backend conversation persistence.

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
