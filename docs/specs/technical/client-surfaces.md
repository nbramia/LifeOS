# HTTP Client Surfaces

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-09-04

LifeOS exposes the orchestrator to **HTTP consumers** — thin clients that submit text and consume SSE without importing LifeOS Python modules. Endpoint and event **shapes** are defined in [api-reference.md](../product/api-reference.md); this doc covers **who consumes them**, **whisper-relay integration**, and **breaking-change policy**.

---

## Surfaces

| Surface | Transport | Chat/conversation endpoints |
|---------|-----------|----------------------------|
| Web chat | Browser → FastAPI | personas, ask/stream, handoff, conversation CRUD — `web/index.html` + `web/chat/` |
| Telegram | In-process `chat_via_api` | Native `/api/ask/stream` (primary bot) or the Hermes proxy, `/api/hermes/ask/stream` (every persona bot, #684) — `api/services/telegram.py` |
| **whisper-relay** | Separate app → HTTP (voice transport API) | Server-side only: `POST /api/ask/stream`, handoff — via `src/voice_gateway/adapters/lifeos.py`; no browser UI after #21 |
| **Voice (web)** | Browser → LifeOS reverse-proxy → whisper-relay | LifeOS `/chat` + `web/chat/voice.js`; proxies `/api/voice/*` — `api/routes/voice.py` |
| MCP / Managed Agents | stdio or HTTP MCP | Tool catalog only — `mcp_server.py` |

**Response compression.** `api/main.py` applies `GZipMiddleware` scoped to `/api/crm/*`, `/api/people/*`, and the CRM page routes (`minimum_size=1024`, `compresslevel=6`), not app-wide. This is a deliberate scoping choice, not a required safety measure: the installed Starlette (0.52.x) already refuses to compress `text/event-stream` responses on its own, so applying `GZipMiddleware` app-wide would not in fact risk buffering any of the SSE streams documented above. Scoping to an allow-list instead simply keeps the gzip CPU cost confined to the handful of routes large enough to be worth it, independent of a dependency default that could change.

---

## Telegram bot backends (#684)

Every LifeOS persona bot — fitness, therapist, doctor, finance, journal — answers through the **Hermes proxy** (`/api/hermes/ask/stream`) by default, the same backend `/chat` prefers when it's available (see "Default backend selection" below). The **primary** bot always answers through the native pipeline (`/api/ask/stream`) instead. This is entirely a decision the Telegram *listener* (`api/services/telegram.py`) makes about which endpoint to call for a given bot's turn — it is unrelated to, and does not change, the `backend` field `/chat` sends on `POST /api/ask/stream` (documented above), the `orchestrates` flag's meaning (below), or anything `GET /api/personas` reports.

**This is a distinct feature from Hermes's own Telegram front door.** "Hermes-Telegram persona selection (#644)," further down this doc, covers a *different* bot — Hermes's own, driven by its own `state/config.yaml`, with `@tag` persona selection and no LifeOS listener involved at all. This section is about LifeOS's own six Telegram bots (primary + five persona bots) and which backend answers each one.

- **`TelegramBotConfig.backend`** (`config/settings.py`) is `"hermes"` for every registry entry (`config/telegram_bots.json`) by default, and `"lifeos"` for the primary bot unconditionally (it has no registry entry to set it from). An entry may set `"backend": "lifeos"` to opt a specific persona bot out of Hermes permanently; an unrecognized value falls back to `"hermes"` with a warning, the same pattern an invalid bot name uses.
- **Persona resolution is by id, not raw text (#684 design decision).** Every specialized bot's turn — on either backend — sends `persona_id` (the bot's registry name) rather than the raw preamble text `chat_via_api()`'s `persona` argument carries for the primary bot. Sending an id, not text, is what lets the Hermes envelope (`lifeos_context`), the journal-capture gate, and the persona_id-gated orchestrating-persona spawn in `api/routes/chat.py` all resolve the right persona server-side on either backend — approximating it from raw text (as the Hermes proxy's own envelope-building code does when only `persona` is sent, without `resolve_effective_persona_id()`) would silently pick the wrong persona for anything downstream of persona resolution. The primary bot is the one exception, by design: it keeps sending its raw preamble text exactly as before, since it's the one caller `chat_via_api()`'s `persona` argument shape was originally built for.
- **Fallback, visibly (`TelegramBotListener._run_chat_turn` / `_disclose_hermes_fallback`).** A fresh clone has no Hermes configured (`LIFEOS_HERMES_BACKEND_URL` unset) — a `"hermes"`-backend bot detects this up front and answers via the native pipeline instead, logging it and sending a **one-time**, per-listener-lifetime in-channel notice (not per-message spam). The same fallback fires mid-conversation if a turn's connection to a *configured* Hermes fails: `chat_via_api()` raises `HermesUnavailable` for a 503 (unconfigured) or 502 (unreachable) response from `/api/hermes/ask/stream` — both statuses the proxy (`api/routes/_proxy.py`) returns *before* any turn-visible side effect (e.g. #685's journal capture, via its `pre_send` hook) can have happened, so a retry on the native pipeline can never double that side effect. Every other status code (e.g. a 400 for a malformed persona) is a real error, not an availability signal, and is not retried — it would just fail the same way natively.
- **Doctor (`orchestrates: true`).** A fresh message to the doctor bot flows through the same chat pipeline as every other persona bot — there is no direct-Claude-Code spawn entry that bypasses chat (no client-side spawn logic in `telegram.py` at all). On Hermes, the doctor persona supervises its own workers via `lifeos_agent_spawn` (`config/personas/doctor.hermes.md`) inside its own ordinary streamed reply. On a native fallback turn, sending `persona_id="doctor"` (not raw preamble text) is what makes `POST /api/ask/stream`'s own persona_id-gated orchestrating-persona spawn (`settings.persona_orchestrates()`, in `api/routes/chat.py`) fire — the same mechanism the web/voice surfaces already use for this persona (see the Persona contract, above) — tagging the spawned session `bot="doctor"` so Telegram-thread parity holds. **The threaded-reply resume hooks and status-anchor machinery** — `_owns_agent_sessions` (primary + any `orchestrates: true` bot) — gate on and resume a `lifeos_agent_*` child session regardless of who spawned it (Hermes, or the native fallback's spawn). **`orchestrates`'s meaning elsewhere is unaffected**: `settings.persona_orchestrates()`, `GET /api/personas`, and the web UI's own orchestration path (documented in the Persona contract above) apply the same way regardless of which backend answered the fresh message.
- **`claude_intent` and the "code task → main bot" redirect.** The Hermes backend never emits a `claude_intent` event (Hermes has no engine-handoff concept of its own), so `chat_via_api()`'s SSE parsing loop simply never sets it — the listener's existing `result.get("claude_intent")` check already tolerates its absence with no special-casing. The accepted, deliberate consequence: a specialized bot's "that looks like a coding/agent task — send it to your main LifeOS bot" redirect only ever fires on a native-pipeline turn (the primary bot, or a specialized bot's fallback turn) — a hermes-backed specialized bot never redirects an inferred code task, since Hermes never tells LifeOS it saw one.
- **journal.** The journal bot's turn — Hermes or native fallback alike — captures the fragment before the model ever answers, and requires the `journal_capture` proof event exactly as `chat_via_api()` and the ring ingest (`api/routes/journal_ingest.py`) do; the Hermes proxy emits that event too. The ring ingest itself (a separate HTTP consumer, not a Telegram bot) is unaffected by this section — it calls `chat_via_api()` directly with a raw persona preamble on the native pipeline.

---

## whisper-relay (voice transport API)

Voice transport in the same GitHub org: [github.com/nbramia/whisper-relay](https://github.com/nbramia/whisper-relay). Local checkout: `~/Code/whisper-relay`. After #21 it is **API-only** — no `static/` UI, no `/api/voice/personas` or `/api/voice/conversations*` proxies. Review `src/voice_gateway/adapters/lifeos.py` when changing chat or conversation APIs.

The gateway connects to LifeOS at `LIFEOS_BASE_URL` for orchestration (ask/stream, handoff). Persona listing and conversation CRUD are **LifeOS-owned** — consumed by `/chat` directly, not through whisper-relay.

**LifeOS endpoints the gateway calls** (shapes: [api-reference.md](../product/api-reference.md)): `POST /api/ask/stream`, `POST /api/chat/handoff`. At startup it may call `GET /api/personas` server-side for handoff capability caching (not exposed to browsers). `POST /api/chat/cancel` (`client_turn_id`, #611) is the gateway's explicit stop path (`whisper-relay#37`) on barge-in and hangup — voice's old cancel-on-disconnect fallback is retired (#616; see "Turn lifetime and cancellation" below): a gateway not yet calling this endpoint on cancel loses correct barge-in outright, since a disconnect alone now just detaches the turn like any other modality, rather than stopping it.

**Persona contract** (shared by web and voice; lets a thin client expose LifeOS's multi-bot personas without reading LifeOS config):

- `GET /api/personas` lists selectable personas (`primary` + configured specialized bots). The client renders these as a picker; ids are stable, labels are display-only. The `primary` persona and orchestrating bots (e.g. the doctor self-repair bot) carry `handoff`/`agent` capabilities — gate any handoff UI on a persona's `capabilities`, not on a hardcoded `primary` check.
- Send the chosen `persona_id` on `POST /api/ask/stream`. The server applies the matching persona preamble and tags a newly created conversation with that persona. Unknown ids and `persona`+`persona_id` together are **400** — surface as a turn-level failure, not a crash.
- Scope the thread sidebar with `GET /api/conversations?persona_id=<id>`. Omitting the param shows the `primary` persona's threads (default web behavior). Conversation detail (`GET /api/conversations/{id}`) is not persona-scoped — fetch by id directly.
- Send `modality: "voice"` on `POST /api/ask/stream` for spoken turns: the server appends the selected persona's `voice` frontmatter rules (speech formatting) to the system prompt. `None`/`"text"` is a normal typed turn. Honored only on the `persona_id` path (the raw-`persona` Telegram path has no id to key voice rules to). Set by the voice gateway (whisper-relay) — see whisper-relay#27.
- Selecting an **orchestrating** persona (`orchestrates: true`, e.g. `doctor`) on `POST /api/ask/stream` does **not** answer inline — the server spawns a background Claude Code session (the persona pipeline + the user's message, in the canonical checkout, tagged with that bot for `[NOTIFY]`/`[CLARIFY]`/completion) and streams a `content` ack + `done` (the only `claude_code` outcome that emits no `claude_intent`). Mirrors the Telegram orchestration path; results arrive via that bot's Telegram + the `/agents` page. Gated on `persona_id` (web/voice); Telegram's own path handles its bots, so this never double-spawns. **This is the `lifeos`-backend behavior specifically** — see "Orchestrating personas on Hermes vs. LifeOS" below for how the same persona behaves on the Hermes backend, and why both now exist deliberately rather than one superseding the other.
- **Orchestrating personas on Hermes vs. LifeOS (#596, revised by #642).** Through #641, an orchestrating persona's turn was diverted client-side from a Hermes-selected composer back to `POST /api/ask/stream` — the spawn above was LifeOS-native with no Hermes equivalent, so Hermes backstopped the case with a 400 and the divert was the only way to make the persona usable there at all. #642 removed both the divert and the 400: #640 gave Hermes its own way to drive a background Claude Code worker (`lifeos_agent_spawn`) with a per-conversation `caller_session_id`, and #641 gave doctor a Hermes-specific preamble (`config/personas/doctor.hermes.md`, resolved via `surface="hermes"`) describing that capability instead of the plain body's "you have shell access" framing. An orchestrating persona selected on Hermes today reaches the Hermes proxy exactly like any other persona — no diversion, no 400 — and Hermes conversationally triages, spawns, supervises, and reports on a worker itself, inside its own ordinary streamed reply. **The two backends now offer genuinely different UX for the same persona, not a workaround and its real implementation:** LifeOS's spawn is fire-and-forget (an ack, then results via Telegram/`/agents`, with no mid-flight visibility or correction); Hermes drives the same underlying capability conversationally, with the operator able to steer it as it runs. LifeOS's own decision to keep this fire-and-forget path, deliberately, is documented in "LifeOS-backend orchestration is retained deliberately" below — it does not become dead code just because Hermes offers a better experience for it.
  - The picker offers every persona identically on every backend (unchanged from #590) — orchestration status never hides a persona from a backend's picker.
  - `web/chat/persona.js`'s `personaOrchestrates()` reflects only the LifeOS-backend spawn: `true` for an orchestrating persona on `lifeos`, `false` on `hermes` (a Hermes turn is not diverted, so there is no LifeOS-linked session for this client to track) and on `agent` (no persona pass-through there at all). The "Runs on LifeOS" toolbar badge follows this flag directly, so it shows only on `lifeos`.
  - `personaSupportsHandoff()` is unchanged and still false on both `agent` and `hermes` — handoff and orchestration are different mechanisms regardless of backend.
  - `lifeos_context.persona.orchestrates` in the Hermes envelope (below) can now be `true` — this is a **cross-repo contract change** from the `false`-always guarantee #590 originally pinned; see "The `lifeos_context` envelope" below for what it means and why it isn't a silent flip.
- **Answering a spawned session's `[CLARIFY]`/`[GOAL]` from web/voice:** on a successful **LifeOS-backend** spawn the server links the conversation to the spawned agent session (`conversations.agent_session_id`). If that session emits `[CLARIFY]`/`[GOAL]`, the worker registers an open question keyed on the session (not just a Telegram `message_id`), and the web/voice client can answer it **without Telegram**:
  - `GET /api/conversations/{id}` includes a `pending_question` object (`{session_id, question, kind}`) **only while** the spawned session is awaiting an answer; it is absent/`null` otherwise. The client renders an answer affordance when present.
  - `POST /api/conversations/{id}/answer` `{answer}` deposits the answer onto that **existing** open question via the session-keyed deposit, preserving its `kind`. The worker's existing tick (`_resume_goal` for `goal_approval`, `_resume_as_followup` / clarification otherwise) resumes the session — the **same** single resume mechanism Telegram replies use. **No second resume path.** Empty answer → **400**; no spawned session / no open question (never asked, already answered, or timed out) → **409**. The Telegram reply-to-resume round-trip is unchanged.
  - This is the **input** direction (answering / resuming). The complementary **output** direction (streaming the session's results back into the web thread) is #311; until then, results still arrive via the bot's Telegram + the `/agents` page.
  - This answer path is **backend-agnostic** in mechanism — it reads `conversations.agent_session_id`, never `backend` — but in practice only ever fires for a `lifeos`-backend conversation, since Hermes does not create a LifeOS-linked spawn of its own.
- **`backend` tag on `POST /api/ask/stream`.** An optional, generic field on the request model, used **solely** to label a newly created conversation for sidebar filtering — never to route, resolve a persona, or pick a model. The `/chat` client does not set it on any turn; any HTTP caller may still tag a conversation this way. Omitted — the only case the first-party client produces — tags the conversation `"lifeos"`. `GET /api/conversations` carries a matching optional `backend` query filter (unset = unfiltered).

### LifeOS-backend orchestration is retained deliberately

#642 (which restored orchestration on Hermes, above) had to decide what an orchestrating persona should do on the `lifeos` backend now that Hermes offers a supervised alternative. Three options existed: keep the spawn path as-is, make the persona answer inline like any non-orchestrating one, or drop it from the `lifeos`-backend picker. **The spawn path is kept, unchanged**, for two reasons:

- **Hermes isn't always there.** `LIFEOS_HERMES_BACKEND_URL` is empty by default (a fresh clone has no Hermes configured at all), and `GET /api/hermes/status` can report unavailable even when it is configured. `lifeos` is the only backend guaranteed to exist on every install — removing or hiding doctor there would make it unreachable from `/chat` entirely on any installation without a working Hermes, with no fallback.
- **The other two options actively regress something, for no offsetting gain.** Answering inline would mean either serving doctor's plain preamble (which claims shell/filesystem access this backend's inline orchestrator does not have — false, and worse than today) or authoring a third, LifeOS-inline-specific persona variant nobody asked for. Hiding the persona from the `lifeos` picker breaks the pinned "the picker's contents shall not change based on the selected backend" AC (#590) and removes a working capability from any operator who prefers or is stuck on `lifeos`.

So the two paths are an intentional, permanent fork, not a transitional state: `hermes` is the recommended way to run an orchestrating persona whenever it's configured and available (the existing default-backend-selection logic in `web/chat/backend.js` already prefers `hermes` for exactly this reason — see "Default backend selection" below), and `lifeos`'s fire-and-forget spawn remains the fallback that works everywhere, including a fresh clone with nothing else configured.

Web chat implements this contract in `web/chat/persona.js`: a top-of-chat toolbar `<select>` populated from `/api/personas`, the selection persisted across refresh in `sessionStorage` (`lifeos:chat:persona_id`), capability-gated `claude_intent` handoff, and a persona-scoped sidebar. Switching persona starts a fresh, persona-scoped conversation.

**Model picker.** The same toolbar carries a per-turn model picker (`web/chat/model.js`): `Auto` (Haiku + escalation) / `Sonnet` / `Opus` / `Gemma (local)` / `Remote` / `Claude Code`. An explicit `Sonnet`/`Opus` pick is the operator asking for an API model, which is why it dispatches without a prompt — `Auto`'s own escalation only climbs to non-API engines. The choice persists in `sessionStorage` (`lifeos:chat:model`) and rides along on `/api/ask/stream` as `model_override` (omitted for `auto`, so the default request is unchanged). For the inline model picks the server pins the turn to that model — cloud picks reuse the per-turn escalation client; `gemma` builds a per-turn `LocalLLMClient`. Context is preserved the same way every turn is: the full `conversation_history` is replayed into the chosen client. `Claude Code` is not an inline model: selecting it short-circuits the turn into the same engine handoff the orchestrator emits for an inferred "use claude code" directive (a `claude_intent` SSE event → `/api/chat/handoff` → `spawn_claude_code_session`), so the message runs in a background Claude Code worker rather than answering inline, on any backend. The frontend treats an explicit `claude_code` pick as its own handoff opt-in, bypassing the persona capability gate that filters inferred handoffs. The **voice** surface carries the same model selection, lifeos-backend only: `web/chat/voice.js` forwards `model_override` on `/api/voice/turn/stream` exactly when the selected backend is `lifeos` (#593 — deliberately not extended to hermes, where model selection is the harness's call, not LifeOS's); whisper-relay relays it to `/api/ask/stream` (#24). The picker is shown in voice mode as well as text — hidden on the Agent and Hermes backends (the persona picker stays visible on Hermes, unlike Agent).

**`Remote` (#654).** A paid OpenAI-compatible provider (e.g. Fireworks running DeepSeek/Qwen/etc.), configured entirely via `LIFEOS_REMOTE_LLM_*` settings (base URL, model id, API key, and per-token rates) — never a hardcoded model. It reuses `LocalLLMClient` (the same class `gemma` uses; only `base_url`/`model`/`api_key` differ) so tool-calling, streaming, and the Anthropic↔OpenAI translation are identical to the local path. It is an **explicit pick only**: `GET /api/chat/config` reports `remote_model_available`/`remote_model_label`, and the option stays hidden (and the model picked as if `auto`) whenever the provider isn't fully configured — a fresh clone's picker looks exactly as it did before this option existed. It is never reachable from auto-escalation — `NON_API_RUNGS` in `agent_loop.py` filters any non-local rung out of a configured ladder regardless of what the operator names there (ADR-018). Its usage is priced from the configured rates and accumulates across tool rounds; a configured provider with no configured rate records its turn `unpriced` rather than guessing another model's price, the same three-state `cost_usd` contract (present number / real `0` / absent-or-non-numeric) Hermes turns already use (see "Usage and cost reporting" below).

**Gateway behavior** (LifeOS API unchanged):

- Speaks `status` events immediately during long tool rounds.
- On `claude_intent`, POSTs handoff after the SSE stream ends; **replaces** accumulated `content` with the handoff confirmation.
- Explicit `model_override` of `claude_code`/`codex` enables handoff parsing even on personas without the `handoff` capability (whisper-relay ADR-004 / #24).
- Cancel = `POST /api/voice/turn/{turn_id}/cancel` (proxied).

Upstream mirror of this integration: `whisper-relay/docs/adr/002-upstream-integration-boundaries.md`.

---

## Voice transport (reverse proxy)

With #361, LifeOS `/chat` is the unified text+voice client. Voice *transport* stays in whisper-relay; LifeOS **reverse-proxies** `/api/voice/*` to `LIFEOS_VOICE_GATEWAY_URL` (default `http://127.0.0.1:9788`) so the browser stays same-origin for the mic (HTTPS) and audio. LifeOS adds no voice logic — it forwards and streams both directions (`api/routes/voice.py`). See [ADR-016](../../adr/016-voice-gateway-reverse-proxy.md). **One exception (#711, [ADR-021](../../adr/021-voice-turn-persistence-tee.md)):** `POST turn/stream` is additionally tee'd into `ConversationStore` — see "Persistence tee" below. Every other path (cancel, audio clips) is the unmodified pass-through.

**Voice turn contract** (the gateway's API, reached through the proxy):

- `POST /api/voice/turn/stream` — multipart `audio` (or `transcript`) + `backend` (`lifeos`|`agent`|`hermes`) + `persona_id` (lifeos and hermes backends, #593; omitted for agent — no persona pass-through there) + `model_override` (lifeos backend only — model selection on hermes belongs to the harness, not LifeOS) + `conversation_id`. Responds with an SSE stream: `started` (turn_id, for cancel), `transcript`, `status_audio`/`main_audio` (clip URLs, played as they arrive), `response`, and a terminal `done` whose `data` is **authoritative**: `{transcript, response_text, status_audio_urls, audio_url, conversation_id, handoff, timings_ms}`. `error`/`cancelled` terminate the turn.
- `POST /api/voice/turn/{turn_id}/cancel` — cancel an in-flight turn.
- `GET /api/voice/audio/{turn_id}/{clip_id}` — WAV clips (status + main).
- `POST /api/voice/transcribe` — **bare STT for the "Listening" wake-word dock toggle (#710); whisper-relay does not implement this endpoint.** Multipart `audio` in, `{"transcript": "..."}` out; no LLM call, no TTS, no conversation/turn persistence (a wake check must never look like a turn to anything downstream, including the persistence tee below, which keys off `done` events this route must never emit). `web/chat/voice.js`'s `checkForWakeWord()` calls it on every captured speech burst while Listening is on; a 404 means every wake check misses.

**Topology:** browser → LifeOS `/chat` → (proxy) → gateway → for the `lifeos` backend, the gateway calls back into LifeOS `/api/ask/stream` (the consumer-into-LifeOS direction above); for the `hermes` backend (#593), the gateway's Hermes adapter (`voice_gateway/adapters/hermes_backend.py`) calls the Hermes harness directly, silently — not LifeOS's own Hermes proxy, `POST /api/hermes/ask/stream` — so a Hermes-backend voice turn gets none of that seam's persona resolution, `lifeos_context` envelope, or conversation persistence (see "The `lifeos_context` envelope" below), the same gap #590/#592 closed on the text path. `nbramia/whisper-relay#32` tracks routing the gateway through that proxy instead, with `modality: "voice"` set so the persona's spoken-style rules attach exactly like a native spoken turn. Conversation/persona listing is owned by LifeOS, not the gateway; the Persistence tee below is what covers conversation persistence across this gap.

**Persistence tee (#711, [ADR-021](../../adr/021-voice-turn-persistence-tee.md)):** because a Hermes-backend voice turn does not reach `/api/hermes/ask/stream`'s own persister (above), `api/routes/voice.py`'s `_VoiceTurnPersister` tees `POST turn/stream`'s relayed SSE response directly, independent of whichever internal call path the gateway used. Trigger: the terminal `done` event's authoritative `data` — a turn that errors, is cancelled, or disconnects before `done` persists nothing (this is also what keeps a bare-transcribe/wake-check call, `POST /api/voice/transcribe` above, from creating a conversation). Guard: writes only when the turn's `backend` form field was exactly `"hermes"` — a `lifeos`-backend turn is already persisted by the native orchestrator (its `done` arrives only after the gateway's internal `/api/ask/stream` call, and that call's persistence, already completed), and an `agent`-backend turn is never persisted, matching that backend's section below. Writes go through the exact same `ConversationStore` calls `_HermesTurnPersister` uses, so grouping (one conversation per voice session) matches the text path. Extracting `backend` requires buffering `turn/stream`'s multipart body — the one path on this proxy that isn't a pure unbuffered stream; every other `/api/voice/*` path is unchanged. **Known residual gap:** if the gateway's Hermes adapter is changed to route through LifeOS's own Hermes proxy (closing the Topology gap above), a hermes-backend voice turn would then be observed by both `_HermesTurnPersister` (via that proxy call) and this tee, double-appending that turn's messages — this tee does not guard against that; whichever change routes the gateway through the proxy must account for it.

**Orchestrating personas never had a voice equivalent of the (now-removed) text-path diversion.** Through #641, `web/chat/ask-stream.js` diverted a Hermes-selected orchestrating persona's *text* turn back to `POST /api/ask/stream` (#596) because the Hermes proxy backstopped it with a 400; voice never diverted, so a spoken turn for an orchestrating persona on Hermes reached that same 400 with nothing to catch it first. #642 removed the 400 (and the text-path divert with it): Hermes now drives that persona itself (`lifeos_agent_spawn`, #640) instead of rejecting it, on both text and voice. `web/chat/voice.js` still only starts pending-question polling (`startPendingQuestionPolling()`) after a `lifeos`-backend turn, never `hermes` or `agent` — not because Hermes rejects the turn anymore, but because only a `lifeos`-backend spawn ever creates a LifeOS-linked session for this client to poll.

Web chat implements voice mode in `web/chat/voice.js` — tap-to-talk turn lifecycle (Voice|Text toggle, SSE `done` data, sequential audio, cancel via `AbortController`), same-origin via the reverse proxy. Mode persists in `sessionStorage` (`lifeos:chat:voice_mode`).

**The user's transcript is rendered from the `transcript` event, not from `done` (#758).** The gateway emits `transcript` as soon as STT lands, well before the reply is synthesized, so the user's message appears in the thread at submit time — matching what the text path (`askStream()`) does — rather than only once the turn completes. `done` stays authoritative: its `transcript` reconciles that bubble in place (and renders it if the event never arrived), so the client never double-appends. A cancelled turn removes the bubble, since a turn that never reaches `done` persists nothing (see the Persistence tee above) — this holds whether the cancellation is this tab's own Cancel button or the SSE `cancelled` frame from elsewhere (a turn's lifetime is server-owned, #611, so another tab/device on the same conversation can cancel it too). A turn that instead *errors* leaves the bubble in place — the user really did say it, so only the assistant side reports the failure, matching `askStream()`'s error handling on the text path.

**Network resilience on the initial submission is entirely client-side (#801) — no server contract change.** `web/chat/voice.js`'s `postTurnStart()` retries the `POST turn/stream` call itself (never a mid-stream failure — see below) up to 3 times with jittered backoff (~1s/3s/9s), but only on failures proven to mean "the request never reached a response": a raw `fetch()` rejection (offline/DNS/connection-reset), or this repo's own `502` from `voice_proxy()` in `api/routes/voice.py` — read that file: it raises `502` from exactly one place, `except (httpx.RequestError, httpx.InvalidURL)` around the call to the gateway, so a `502` here can never mean a turn actually started server-side. Any `4xx`, and any *other* `5xx` (not proven side-effect-free the way this proxy's `502` is — `voice_turn_stream` always answers `200` and reports its own internal failures as an in-stream `error` event, never an HTTP-level 5xx), is terminal on the first attempt. The recorded blob is held client-side (`heldRecording`) until the turn *definitively* completes — success, an explicit cancel, or an explicit dismiss — never merely because a submission attempt failed; a terminal failure renders a Retry affordance that resubmits the same blob into the same bubble.

**A failure AFTER the initial POST answered `ok` (a mid-stream drop) is never auto-retried.** By then the turn may genuinely be running server-side, and a blind resubmit risks double-executing it. whisper-relay's `voice_gateway/` exposes no turn-status endpoint by `turn_id` (`cancel.py`'s `TurnRegistry` only supports firing a cancel; `storage.py`'s `read_meta()` has no HTTP route) — the one thing that IS reachable and answers "did it finish" is `GET /api/voice/audio/{turn_id}` (`routes/voice.py`'s `_serve_clip()`), which 404s until the final TTS clip is written, right before `done`. `handleMidStreamDrop()` polls that endpoint (`HEAD`, briefly) on a drop: found → the turn completed, offered back via the existing tap-to-replay affordance (there is no way to recover the transcript/response text for redisplay without a real status endpoint — a follow-up, not yet built); not found within the window → explicit-Retry-only, identical to any other terminal failure, never auto-retried.

## Turn lifetime and cancellation (#611)

A chat turn's lifetime is owned by the server, not the SSE connection watching
it — see [ADR-019](../../adr/019-turn-owned-by-server.md) for the full
rationale. This applies to the native (`lifeos`) backend and the Hermes
proxy; the Agent backend is unaffected (below).

- **A client disconnecting no longer stops the turn.** Closing the tab,
  backgrounding the app, or a network switch used to kill generation within
  milliseconds and lose the reply outright. Now the turn keeps running
  server-side and persists the complete reply, readable via `GET
  /api/conversations/{id}` once it's reopened. A connected client's frame
  sequence and bytes are unaffected — this is purely about what happens
  *after* a disconnect.
- **Voice is no longer an exception (#616).** whisper-relay's adapter used
  to abandon the LifeOS/Hermes stream as its only cancel gesture on
  barge-in and hangup, so a turn with `modality: "voice"` kept the *old*
  disconnect-cancels behavior — a voice-modality disconnect cancelled the
  turn immediately rather than surviving it, unlike every other turn. That
  gate is lifted now that whisper-relay's cancel gesture is calling `POST
  /api/chat/cancel` with its `client_turn_id` (below) explicitly instead of
  just walking away (whisper-relay#37). A voice turn's disconnect now
  detaches and survives exactly like a text turn's; only an explicit cancel
  — the gateway's real barge-in gesture — stops it. See
  [ADR-020](../../adr/020-voice-cancel-gate-lifted.md) for the history.
- **`POST /api/conversations/{id}/cancel`** stops whatever turn is in
  flight for that conversation, native or Hermes-relayed alike. `{ok:
  true, cancelled: true|false}` — `false` when nothing was running (already
  finished, or never started); `404` if the conversation itself doesn't
  exist. A new `POST /api/ask/stream` (or the Hermes equivalent) naming a
  conversation that already has a turn in flight cancels the old one first
  (supersede) — asking again is itself a stop gesture.
- **`client_turn_id` and `POST /api/chat/cancel` (#611 review — closes the
  first-turn barge-in gap).** `conversation_id` alone can't cancel a turn
  before its first SSE frame arrives: a brand-new conversation's id doesn't
  exist until the `conversation_id` event, so a client racing to cancel
  before then — the common case for a voice barge-in, which tends to land
  within the first second — has nothing to cancel by. `POST
  /api/ask/stream` accepts an optional `client_turn_id`: an opaque,
  client-generated key (bounded to 200 chars, no control characters — not
  assumed to be a UUID), known before the request is even sent. The
  registry indexes turns by it in addition to `conversation_id`, and `POST
  /api/chat/cancel` (body: `{"client_turn_id": "..."}`) cancels by that key
  alone. Reusing a `client_turn_id` on a later request supersedes the turn
  currently holding it, mirroring `conversation_id` supersede — a stale or
  duplicate key always resolves to its current claimant, never a stray
  earlier turn. Returning a server-minted turn id in a new SSE event or
  header was considered and rejected: either would break the byte-identity
  guarantee a connected client already relies on (no new frame, no new
  header); a request-body field costs nothing and the client knows its own
  key earlier than the server could mint one anyway.
- **Cancelling a turn whose reader is still attached is a supported,
  explicitly-tested ordering** (#611 review) — not just "cancel after
  disconnect." A gateway that fires its cancel POST from an event handler
  rather than its SSE read loop (to avoid the latency of checking a flag
  between reads) can have that POST arrive *before* the client actually
  stops reading. Cancelling in that ordering works correctly: the
  still-attached reader gets a clean end-of-stream (no exception), and a
  disconnect that follows afterward is a no-op — not a second finalize.
  Cancelling an already-finished (or already-cancelled) turn is `200` +
  `cancelled: false`, never a 4xx — a gateway treats that as success.
- **`GET /api/conversations/{id}`** gains an additive `active_turn` field:
  `{turn_id, conversation_id, started_at}` while a turn is in flight for
  that conversation, `null` otherwise. Lets a reconnecting client show
  "still working..." and reach the cancel affordance even after a reload.
- **A cut-off turn is always marked, never presented as whole.** Whatever
  text a turn produced before being cancelled, hitting its
  `LIFEOS_DETACHED_TURN_TIMEOUT_SECONDS` deadline (default 300s, clock
  starts at disconnect), being caught in a server shutdown drain, or dying
  to a genuine stream error is persisted with a trailing, visible marker
  (`"\n\n_[cut off — the turn ended before it finished]_"`) and a
  `routing.truncated: true` / `routing.truncation_reason` on the stored
  message (`"cancelled"` | `"deadline"` | `"shutdown"` | `"stream_error"`
  on the native path; Hermes can only tell "a `done` event never arrived"
  from "it did," so every Hermes truncation reports `"stream_error"`).
  Neither field is new SSE wire protocol — they only ever appear on the
  persisted message read back via `GET /api/conversations/{id}`, not on any
  `data:` event.
- **Byte identity for a connected client is unchanged.** No new SSE event
  type and no new header were added — cancellation is keyed on
  `conversation_id`, which every client already receives as the first SSE
  event on both the native and Hermes paths.

## Text Backends

`/chat` (and voice, via the gateway) targets one of three text backends, carried as an optional `backend` field (`lifeos` | `agent` | `hermes`, omitted/`lifeos` reproducing pre-#361 behavior exactly). Each gets its own section below because their capabilities genuinely differ — treat no statement in one backend's section as implying anything about another's.

### LifeOS

The native orchestrator (`POST /api/ask/stream`, documented above and in [api-reference.md](../product/api-reference.md)). Full personas (including orchestrating ones, which spawn a background Claude Code session — see the Persona contract above), spoken-style rules on voice turns, the per-turn model picker, CLI-engine handoff, and conversation history that has always lived in LifeOS's own `ConversationStore`. Every other backend is described relative to this one.

One additive SSE event is emitted only on the `journal` persona's turns: `journal_capture` (`path`, `created`), sent right after `conversation_id`, once LifeOS has written the fragment to `Personal/Log/YYYY-MM-DD.md` in code — capture does not depend on the model calling a tool. It is proof of a completed write, not a status hint: `api/routes/journal_ingest.py` refuses to report `status: "logged"`, or to burn a delivery's idempotency key, without it. A capture failure is raised **before the stream opens**, so it surfaces as an HTTP 500, never as a mid-stream `error` frame. Clients that don't know this event type simply ignore it.

### Agent

`agent` is the OpenClaw voice-adapter, reached at `LIFEOS_AGENT_BACKEND_URL` (optional bearer token) and proxied at `POST /api/agent/ask/stream` — LifeOS **adds the bearer server-side** so it never reaches the browser, and `GET /api/agent/status` reports whether it's configured (drives the UI selector). It speaks the same `/api/ask/stream` SSE contract as the native path. It has **no personas at all** — `persona_id` is never sent, so the persona and model pickers are both hidden while it's selected, no persona is ever diverted (nothing to divert), and no per-turn context is attached. It has **no handoff**. The route (`api/routes/agent_proxy.py`) is a pure byte relay (`request.stream()`, unbuffered — no `transform_body`, no `make_observer`), so its behavior is byte-for-byte what it was before Hermes existed. Its conversation history is **not** LifeOS-owned: nothing here persists it, so switching to `agent` shows a fresh view even though the stored conversation id is retained for continuity with whatever does own that history upstream. Because it isn't tee'd at all, its turns are also invisible to the usage store — unlike Hermes, below. **It's also unaffected by #611** (turn-survives-disconnect): the registry-owned pump is gated on `make_observer` being set, and Agent never sets it, so a disconnected Agent-backend turn still stops exactly as before — detaching a relay nothing persists would only spend money with nothing to show for it.

**These are properties of the Agent backend specifically, not of "external backends" generally** — Hermes, below, shares only some of them.

### Hermes

`hermes` is an agent harness reached as a gateway (#587), at `LIFEOS_HERMES_BACKEND_URL` (optional bearer token), proxied at `POST /api/hermes/ask/stream` with the same server-side bearer injection and a `GET /api/hermes/status` availability check. Both proxies are built by the same `make_backend_router()` factory in `api/routes/_proxy.py`; `agent_proxy.py` and `hermes_proxy.py` each just name their own settings fields and `_client()` test seam. Hermes has **no handoff** either — but unlike Agent, it keeps the persona picker visible, carries spoken-style rules on voice turns, receives the orchestrator's auto-injected turn context, and its history **is** LifeOS-owned. Its route (`api/routes/hermes_proxy.py`) buffers the request body (`transform_body`) to resolve the selected persona and attach it as the `lifeos_context` envelope below — the only text-backend proxy that does. **Orchestrating personas reach this route like any other, since #642** — resolved with `surface="hermes"` so a persona with a Hermes-specific variant (`config/personas/doctor.hermes.md`, #641) gets that body instead of the plain one; see "Orchestrating personas on Hermes vs. LifeOS" in the Persona contract above for what changed and why, and the envelope section below for the `orchestrates` field's revised contract. **This section describes `/chat`'s Hermes backend specifically — Hermes's own Telegram front door is a separate integration, covered in "Hermes-Telegram persona selection (#644)" below.**

#### Hermes-Telegram persona selection (#644)

The `/api/hermes/ask/stream` proxy above only ever sees a turn that `/chat` (browser or the voice gateway) already sent to LifeOS. Hermes's **own** Telegram bot is a separate front door — Hermes's own `state/config.yaml`, no LifeOS involvement — and until #644 it received no persona context at all, so asking it to "act as the doctor persona" was prompt-level roleplay with no real preamble, voice rules, or turn context behind it.

**Direction (decided, not re-open-able): Hermes fetches persona context *from* LifeOS; it is not proxied.** The Hermes-Telegram path does **not** route through `/api/hermes/ask/stream` — that would couple its availability to LifeOS being reachable, which #658's accepted criteria require it to survive without. Instead, Hermes calls a dedicated resolution endpoint, `POST /api/hermes/resolve-persona`, before it ever talks to its own backend LLM, and folds the response into its own request the way it sees fit.

**Selection mechanism (Nathan's decision): a per-message `@name` prefix, plus reply-thread inheritance.** Resolution is an ordered rule, not a single check:

1. An explicit `@persona` prefix on this message wins, always — even inside an existing persona thread, so replying with a new tag switches personas mid-thread.
2. Else, if this message is a Telegram native reply to a message whose persona is known, inherit it — no re-tagging needed to keep talking to the same persona.
3. Else, no persona — byte-identical to today.

There is still no `/persona` command and no per-chat setting: a message with neither a tag of its own nor an inheritable reply-to is evaluated exactly as if this feature didn't exist. The prefix grammar itself lives in LifeOS (`_parse_persona_tag()` in `api/routes/hermes_proxy.py`), not in Hermes — Hermes stays a transport, sending the **raw, unparsed** message text and letting LifeOS decide what's a tag: a tag is recognized only when it opens the message, the character right after `@` is a letter, and it's immediately followed by whitespace or the end of the message. This is deliberately narrower than "starts with `@`" — a bare `@`, `@3pm meeting`, or `@doctor,` (punctuation glued to the tag) are not tag attempts, since no persona id is digit-led and the one supported form is `@name <message>`.

**Reply-thread inheritance needs a small amount of state, and it lives on the LifeOS side, not Hermes's.** Putting a message-id → persona mapping in Hermes would make persona resolution two-sourced again, which the AC forbids. `HermesPersonaThreadStore` (`api/services/hermes_persona_thread_store.py`) is a `(chat_id, message_id) -> persona_id` table — nothing else, no message content — scoped per chat (Telegram message ids are unique only within a chat) and bounded: rows expire after 7 days and the table is capped at 20,000 rows, oldest evicted first. Every message this endpoint resolves a persona for — whether from an explicit tag or an inherited one — is itself recorded under that persona, which is what makes a reply-to-a-reply chain inherit transitively with no special case: each link becomes a valid anchor for the next.

**Endpoint contract — `POST /api/hermes/resolve-persona`:**

```json
// Request
{
  "text": "@doctor the sync timer looks wedged",
  "modality": "text",
  "conversation_id": "...",
  "chat_id": "12345",
  "message_id": "987",
  "reply_to_message_id": "986"
}

// Response (tag recognized, or inherited from reply_to_message_id)
{
  "persona_id": "doctor",
  "text": "the sync timer looks wedged",
  "lifeos_context": { "schema_version": 1, "modality": "text", "persona": { "...": "..." }, "turn": { "...": "..." } }
}

// Response (no tag, and no inheritable reply) — for a request of { "text": "what's on my calendar today" }
{ "persona_id": null, "text": "what's on my calendar today", "lifeos_context": null }
```

`modality`/`conversation_id` are optional, mirroring the same fields on `/api/hermes/ask/stream`. `chat_id`/`message_id`/`reply_to_message_id` are opaque strings (Hermes stringifies Telegram's integer ids, the same convention `conversation_id` already uses) and are also optional — omitting all three degrades to the base, non-threaded contract (tag or nothing). `lifeos_context` — when present — is byte-for-byte the same shape the envelope section below documents, built by the identical `_resolve_lifeos_context()` helper both entry points share (the AC's "persona resolution shall have exactly one source of truth"): the resolved preamble for a resolved persona (tagged or inherited) is guaranteed identical to what `/chat` would use for that same persona.

- **No tag and no inheritable reply → unchanged from today, by construction.** `persona_id` and `lifeos_context` are both `null`, and `text` is returned exactly as sent. Hermes needs nothing from this response in that case and should proceed exactly as it did before this endpoint existed — no LifeOS-derived preamble, no envelope, no dependency on LifeOS being reachable.
- **An unrecognized `@tag` is a 400, not a silent no-persona.** `@nonsense do a thing` matches the tag grammar (starts with a letter, ends at whitespace) but doesn't match any configured persona id — this is rejected with `400 Unknown persona_id: 'nonsense'` rather than falling back to `persona_id: null`. Deliberately: a typo that silently became "no persona selected" would look like it worked, when the user's actual intent (routing to a specific persona) was dropped on the floor.
- **An unrecognized or expired `reply_to_message_id` is NOT an error.** A miss (never recorded, expired, cross-chat, or a thread that predates this feature) falls through to rule 3 exactly as if no `reply_to_message_id` had been sent at all — a pruned mapping degrading safely is the whole point of bounding it.
- **Voice applies the same gate `/chat` uses.** `modality: "voice"` on a resolved persona populates `persona.voice_rules` exactly as a voice turn on `/api/hermes/ask/stream` would for the same persona_id — see the envelope section below.
- **Auth reuses the existing Hermes bearer, in the reverse direction.** `hermes_backend_token` (`LIFEOS_HERMES_BACKEND_TOKEN`) already authenticates LifeOS's outbound calls *to* Hermes; this endpoint requires the same token on Hermes's inbound calls *to* LifeOS (`Authorization: Bearer <token>`, checked with a constant-time compare) rather than introducing a second shared secret. An unset token disables the endpoint (`503`), matching the closed-by-default posture `api/routes/fitness.py`'s health-ingest endpoint uses for the same reason: this accepts input from the network, so a fresh clone must not expose it unauthenticated.

**Endpoint contract — `POST /api/hermes/register-persona-message`:** anchors a message Hermes itself authored — its reply, sent under a resolved persona — to that persona, so a later reply threaded off the **bot's** message inherits too, not just one threaded off the user's original tagged message. This has to be a second call, after the fact: the bot's reply doesn't have a message id until Telegram has accepted it, which is necessarily after `/resolve-persona` already returned.

```json
// Request
{ "chat_id": "12345", "message_id": "988", "persona_id": "doctor" }

// Response
{ "ok": true }
```

Same auth as `/resolve-persona`. `persona_id` must be a currently-configured persona (`400` otherwise) — defense in depth, since in practice Hermes only ever passes back an id this same service handed it moments earlier.

**The honest boundary of "independent of LifeOS":** a Telegram message that needs LifeOS to resolve its persona — whether via an explicit `@tag` or by inheriting one from its reply-to — genuinely needs LifeOS reachable, while a message with neither does not and behaves exactly as if #644 had never shipped. #658's "keeps working when LifeOS is down" guarantee therefore applies only to that untagged, non-inheriting path; a persona-tagged or persona-inheriting message during a LifeOS outage should fail (or fall back to no-persona) rather than silently pretend to apply a preamble it couldn't fetch — that failure mode is Hermes's call to make, not LifeOS's, since Hermes owns what happens when either endpoint above is unreachable.

#### Hermes turn persistence (#592, survives disconnect since #611) and usage capture (#595)

Unlike the Agent backend, whose history genuinely lives elsewhere, Hermes turns are persisted into the same conversation store the native path uses, and their usage/cost is recorded into the same usage store. `make_backend_router()`'s relay loop (`api/routes/_proxy.py`) accepts an optional `make_observer` hook: given the same raw request body `transform_body` already buffers, it returns a `_HermesTurnPersister` (`api/routes/hermes_proxy.py`) whose `observe(chunk)` is called with a copy of each chunk immediately *before* that chunk is handed onward (never altered — the byte sequence a connected client sees is unaffected), and whose `finalize()` runs once, when the turn truly ends. **Since #611**, `make_observer` being set also gates a second behavior: the upstream drain runs as a registry-owned background pump (`api/services/chat_turns.py`) rather than the browser-facing generator itself, so a client disconnect no longer cuts the drain short — `finalize()` now fires on the pump's own real end, not on whatever the browser happened to still be attached for. The Agent proxy passes neither `transform_body` nor `make_observer`, so it's untouched by this — it never detaches, and its relay stays byte-for-byte what it was before either hook existed.

The persister reassembles SSE frames from the observed bytes (a chunk is a network read, not a frame, so frames can split across chunk boundaries) and reacts to the event types the native path emits: on the first `conversation_id` event it adopts that id via `ConversationStore.create_conversation(conv_id=..., persona_id=..., backend="hermes")` (existing rows are returned unchanged, so a continuing thread doesn't get re-tagged) and stores the user's question, and — since #611 — binds the turn into the shared registry under that id, making it reachable by `POST /api/conversations/{id}/cancel` from that point on; `content` events accumulate into the assistant reply, written on `finalize()` — including whatever arrived if the pump ends early; a `usage` event (`input_tokens`, `output_tokens`, `cost_usd`, `model`) is captured and, on `finalize()`, written to `UsageStore.record_usage()` tagged with the conversation id — **cost is recorded verbatim, never recomputed** (Hermes runs DeepSeek via Fireworks; LifeOS's calculator only knows Anthropic pricing), and a cost-less event records a zero cost rather than an invented one. Conversation persistence and usage persistence are independent: a turn with no `usage` event writes no usage row (and vice versa), and each is retried/skipped on its own. A malformed or partial `usage` event (missing/wrong-typed model or token counts) is ignored entirely. Every store call is wrapped so a persistence failure is logged and swallowed, never surfacing as a broken turn.

**Truncation (#611):** the persister tracks only whether a `done` event was ever observed. `finalize()` without one appends the same truncation marker and `routing.truncated`/`routing.truncation_reason: "stream_error"` the native path uses (see "Turn lifetime and cancellation" above) — the upstream connection ending before `done` arrived (a crash, a kill, a dropped connection) is the only signal available to this side; unlike the native path, Hermes truncation always reports `"stream_error"` regardless of the underlying cause.

#### Usage and cost reporting (external backends) — #595

Both proxied backends are expected to emit a `usage` SSE event matching LifeOS's native one — but a **subset** of its fields, since an external backend has no prompt-cache accounting to report: `type: "usage"`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (no `cache_read_tokens`/`cache_creation_tokens`). Today only Hermes is actually tee'd and captured (above); the Agent proxy has no observer, so an Agent-backend `usage` event, if one were ever emitted, would simply pass through unrecorded. [`GET /api/admin/usage`](../product/api-reference.md#get-apiadminusage) includes these rows in its totals — the usage store applies no per-model or per-backend filter, so anything written to it (native or relayed) counts.

`cost_usd` on this event carries three distinct states, and the client (`web/chat/ask-stream.js`) is expected to keep them distinct rather than collapsing to two (#602): a **priced** turn (a real number) adds to the running session total; a **free** turn (a real `0`) adds nothing and looks exactly like a session with no unpriced turns; an **unpriced** turn (the key absent, or present but non-numeric — the shape an external backend sends when it genuinely can't price a turn, deliberately rather than guessing) also adds nothing but marks the session total as a lower bound (a `~` prefix on the header's `#sessionCost` display and the usage modal's mirrored figure, with a tooltip naming how many turns were unpriced) for the rest of the session. The distinction hinges on an explicit presence-and-type check (`typeof cost === 'number' && Number.isFinite(cost)`) rather than truthiness — `data.cost_usd || 0` treats a real `0` the same as an absent value, which silently turns "unknown" into a confident (wrong) claim of "free" the first time a model appears with no rate. Server-side persistence (`_HermesTurnPersister._handle_usage`, above) still records a missing `cost_usd` as `0.0` in the usage store, since recomputing a real cost from a local pricing table is explicitly out of scope (the calculator only knows Anthropic pricing and would misprice an unrecognized upstream model) — but the row is also flagged `unpriced=True` (#613: an additive `unpriced` column on the `usage` table, defaulting to priced/`0` for rows written before it existed), so the "unknown, not free" distinction survives into the usage store, not just the live display — see "Session-to-date cost" below for how `get_conversation_usage()` surfaces it.

### The `lifeos_context` envelope (Hermes only)

Because Hermes has no way to resolve a LifeOS persona id or the current per-turn context on its own, `POST /api/hermes/ask/stream` (`api/routes/hermes_proxy.py`, `_build_envelope()`) parses the JSON body, resolves both, and adds one top-level key, `lifeos_context`, before forwarding. Every field the browser sent is forwarded unchanged alongside it. This is a **cross-repo contract** with `nbramia/hermes`, pinned as the schema comment on issue #590 — the field names below are not suggestions.

```json
{
  "question": "...",
  "persona_id": "fitness",
  "modality": "voice",
  "lifeos_context": {
    "schema_version": 1,
    "modality": "voice",
    "persona": {
      "id": "fitness",
      "label": "Fitness",
      "preamble": "...",
      "voice_rules": ["..."],
      "orchestrates": false
    },
    "turn": {
      "current_datetime": "Wednesday, August 19, 2026 at 09:14 AM EDT",
      "current_datetime_iso": "2026-08-19T09:14:22-04:00",
      "timezone": "America/New_York",
      "time_resolution_instruction": "...",
      "personal_context": "",
      "existing_tags": [{ "tag": "ai-agent", "count": 12 }],
      "tags_instruction": "...",
      "session_cost_usd": 0.0031,
      "session_turn_count": 2,
      "session_input_tokens": 300,
      "session_output_tokens": 130,
      "session_cost_is_lower_bound": false,
      "caller_session_id": "sess_herm3f9a2b7c1d4e5f60"
    }
  }
}
```

- `schema_version` is currently `1`. `caller_session_id` (#640) was added without a version bump — it's purely additive, so a consumer validating `schema_version == 1` and ignoring unrecognized keys is unaffected.
- `modality` duplicates the request's `modality` (`"voice"` or `"text"`) so the envelope is self-contained.
- `persona.id` defaults to `primary` when the client sends no `persona_id`; `preamble` is the persona's markdown body verbatim (may be empty), resolved with `surface="hermes"` so a persona with a `.hermes.md` variant gets that body instead — see "Hermes" above; `voice_rules` is populated only on voice turns, matching the `modality == "voice"` gate `ask_stream` in `api/routes/chat.py` uses for the native path.
- **`orchestrates` — CROSS-REPO CONTRACT CHANGE (#642), coordinated with `nbramia/hermes#57`.** Through #641 this was always `false`: an orchestrating persona (`doctor`) was rejected with a 400 before ever reaching the envelope (see the removed validation step below), and the #590 contract told Hermes to fail loudly if it ever saw `true` — a defensive check against exactly that "shouldn't be possible" case. **That guard is gone.** `orchestrates` now reflects `settings.persona_orchestrates(persona_id)` honestly and can be `true` — sent deliberately, not hardcoded `false` to keep the old tripwire quiet, because doctor genuinely does orchestrate and that is the entire point of routing it to Hermes. **The field survives; its semantics don't**: `true` no longer means "a LifeOS bug leaked an orchestrating persona through, fail loudly" — it means "this persona supervises workers through tools," and Hermes is expected to act on the Hermes-specific preamble (`persona.preamble`, above) rather than refuse the turn. Hermes's `lifeos_adapter/envelope.py` previously raised `OrchestratingPersonaError` on `true` — a fatal exception, not a warning — so shipping this side alone would have failed every doctor-on-Hermes turn. `nbramia/hermes#57` removed that raise and shipped first (hermes `84ef8de`, deployed 2026-08-22); this side merged only after that was verified live. **If Hermes is ever rolled back past that commit, this field will start failing turns again** — the two are coupled and must move together.
- `turn` (#591) is a **sibling** of `persona`, resolved by `build_turn_context()` in `api/services/agent_system_prompt.py` — the same function [`GET /api/chat/turn-context`](../product/api-communications.md#get-apichatturn-context) returns, so every field *except* `caller_session_id` can never drift apart between the two. **Never merge `turn` into `persona`** — `persona` is stable across a whole conversation (cacheable), `turn` changes every turn (or, for `caller_session_id`, at least *could*); merging either into `persona` would invalidate a consumer's prompt cache on every turn. The `session_*` fields (#610) are exactly why a cumulative cost belongs here and not in `persona`; `caller_session_id` belongs here for the same layering reason even though its value is often stable across a conversation's turns (see below) — it's still a per-request concern, not a fact about the persona.
- `caller_session_id` (#640) is Hermes's identity for the `lifeos_agent_*` tool family (`lifeos_agent_spawn`/`check`/`send`/`kill`/`yield_until`/`transcript_read`/`sessions_list`/`user_ask` — see `api/services/agent_worker/inter_agent.py`), which `mcp_server.py` already advertises to Hermes over stdio MCP. Every one of those tools requires a `caller_session_id` argument that resolves to a real row in LifeOS's agent-worker `SessionStore`; without this field Hermes had none, and every call failed with `no_caller`. Hermes should copy this value verbatim into that argument on every `lifeos_agent_*` call it makes during the turn. **Present only in the Hermes envelope** — `GET /api/chat/turn-context` has no session to hand out and does not return this field.

**Validation and rejection**, in order, before any upstream request is made: malformed JSON → 400; unknown `persona_id` → 400 naming the id. (Through #641 there was a third step here — a known but *orchestrating* `persona_id` also → 400, backstopping the client-side diversion above. #642 removed both the diversion and this backstop; an orchestrating persona is no longer rejected.) Attachment size/type caps are enforced via the same `AskStreamRequest` model the native endpoint uses, so the limits can't drift between the two paths.

#### Turn-context payload

The `turn` object above — identical in shape to the response body of [`GET /api/chat/turn-context`](../product/api-communications.md#get-apichatturn-context) for every field except `caller_session_id`, since all the others come from the same `build_turn_context()` call:

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `current_datetime` | string | always | Human-formatted local date/time, prompt-ready (`%A, %B %d, %Y at %I:%M %p %Z`). |
| `current_datetime_iso` | string | always | The same instant as ISO 8601 with offset, for machine use. |
| `timezone` | string | always | IANA zone name, e.g. `"America/New_York"` (`settings.timezone`). |
| `time_resolution_instruction` | string | always | Prompt-ready instruction to resolve relative time expressions ("last week") into concrete `YYYY-MM-DD` ranges before calling search tools. |
| `personal_context` | string | always present, often empty | A persona-scoped people block. Non-empty only for the `therapist` persona (and only once the relevant config is set); empty string for every other persona. |
| `existing_tags` | array of `{tag, count}` | always present, may be empty | Task tags already in use, for reuse when tagging. Empty when there are no tags or the task manager is unreachable — a normal degraded case, not an error. |
| `tags_instruction` | string | always | Prompt-ready instruction to prefer an existing tag over inventing a near-duplicate; pair with `existing_tags`. |
| `session_cost_usd` | number | always | Session-to-date cost (#610): the verbatim sum of `cost_usd` for every turn already recorded for this request's `conversation_id`, in USD. Excludes the turn currently being built — its own cost isn't recorded until its stream finishes, after this context was already handed out. `0` for a conversation with no recorded usage yet (including a brand-new conversation with no id). Never recomputed from token counts. **Read `session_cost_is_lower_bound` before treating this as exact** — see "Session-to-date cost" below. |
| `session_turn_count` | integer | always | How many already-recorded turns the sum above covers. `0` for a conversation with no recorded usage yet. |
| `session_input_tokens` | integer | always | Sum of `input_tokens` across the same already-recorded turns. |
| `session_output_tokens` | integer | always | Sum of `output_tokens` across the same already-recorded turns. |
| `session_cost_is_lower_bound` | boolean | always | `true` when any turn summed into `session_cost_usd` is `unpriced` (#613) — the provider reported no cost for it, recorded as `cost_usd=0.0` alongside the flag rather than as a real zero. `false` means every *recorded* turn in the sum was either genuinely priced or genuinely free — but see the retroactive-gap note below; it does not mean the figure is guaranteed exact. |
| `caller_session_id` | string | always, Hermes envelope only | Hermes's `lifeos_agent_*` identity for this turn (#640) — see "Hermes agent-worker session identity" below. Not part of `build_turn_context()`; not returned by `GET /api/chat/turn-context`. |

#### Hermes agent-worker session identity (#640)

`caller_session_id` is a real row LifeOS creates in the agent worker's `SessionStore` (`api/services/agent_worker/session_store.py`; the same store `/agents` reads and `lifeos_agent_*` operates on), keyed to the Hermes **conversation**, not the turn:

- **Per-conversation, not per-turn.** A session Hermes spawns with `lifeos_agent_spawn` can easily outlive the turn that created it (the operator may not check back in for several turns), and the spend caps in `inter_agent.py` (`max_spawn_depth`, `max_descendants_per_root`) are scoped to a lineage root — a fresh root every turn would silently reset those caps each time. The id is a deterministic hash of `conversation_id`, so the same conversation always resolves to the same session with no separate lookup table, and later turns can `lifeos_agent_check`/`kill`/`yield_until` a session a much earlier turn spawned.
- **The one gap: turn 1 of a brand-new conversation.** The request that starts a new conversation carries no `conversation_id` yet (Hermes mints it; LifeOS only learns it afterward, from the `conversation_id` SSE event) — nothing to hash. That turn gets its own one-off session instead of the conversation's longer-lived one. Anything it spawns is still fully usable from a later turn (`lifeos_agent_check` has no lineage restriction), it just isn't threaded into the stable per-conversation root that exists from turn 2 onward.
- **Inert by design.** The session is never dispatched or executed by the worker — it exists purely so `lifeos_agent_*` calls have something to resolve. It never appears as a running task and never sends its own Telegram notifications.
- **Spend guard.** A Hermes-rooted session is treated as a non-API-billed lineage, the same as a `claude_code`/`codex` root: `lifeos_agent_spawn(model="claude")` from it (or from any descendant of it) is refused with `api_billing_blocked`, because that model routes through the Anthropic API rather than a subscription (ADR-018). `model="claude_code"`, `"codex"`, and `"local"` are unaffected.

#### Session-to-date cost (#610, `is_lower_bound` added by #613)

A Hermes session had no way to answer "what has this cost?" even though the browser header already shows a running total (`web/chat/ask-stream.js`'s `state.sessionCost`, accumulated client-side from each turn's `usage` SSE event). The five `session_*` fields above close that gap by exposing the recorded totals — read from `UsageStore.get_conversation_usage(conversation_id)`, never recomputed — through the same channel that already carries per-turn context. No database schema change was needed for #610; #613 later added the additive `unpriced` column this section now relies on.

- **Scope is "turns completed so far," not "this turn."** The in-flight turn's own cost is unknowable until its own stream finishes, so `session_cost_usd` deliberately excludes it — the only honest reading.
- **`session_cost_usd` is exact for rows written after #613, a floor for anything spanning rows written before it.** A turn whose provider reported no cost is recorded as `cost_usd=0.0` — the same repo-wide convention every `record_usage()` caller uses (native `agent_loop.py`, `synthesizer.py`, the CLI-worker executors, and this Hermes path alike; see "Usage and cost reporting" above) — but, since #613, it is *also* flagged `unpriced=True` at write time, so a later reader no longer has to guess which zero-cost rows were actually free. `session_cost_is_lower_bound` reports that flag, summed: `false` means the conversation's recorded usage is a real total, not an estimate. **The gap that remains is retroactive, not ongoing:** a row written before this column existed has no way to be reclassified — it reads as priced/`0` regardless of what actually happened — so any window whose sum includes a pre-#613 row is still only a floor, even when `session_cost_is_lower_bound` reads `false`, because that flag can only see what was recorded after the column existed. There is no way to distinguish a pre-#613 conversation from a post-#613 one that just never hit an unpriced turn; treat any long-lived conversation's `false` with that caveat in mind.
- **`session_turn_count` gives scale, not certainty.** It's the count of recorded turns the sum covers — useful context for how much a floor might be undercounting — without claiming to know how many of them were free versus unpriced.
- **Agreement with the UI.** For the same conversation with no page reload, `session_cost_usd` sums the identical `cost_usd` values the UI's own running total accumulates, so the two agree.
- **Also on `GET /api/chat/turn-context`.** `build_turn_context()` now takes an optional `conversation_id`, so the standalone endpoint accepts one too (query param, defaulting to none) and returns the identical fields — one parser handles both sources, per the "Turn-context payload" note above.
- **Native path unaffected.** `build_system_prompt()` (the native orchestrator's actual system prompt) does not call `build_turn_context()` and was not changed — these fields exist only on the exported JSON surfaces (the envelope and the standalone endpoint), not in the native model's prompt.

### Capability comparison

| Capability | LifeOS | Agent | Hermes |
|---|---|---|---|
| Persona support | Yes (full registry) | No — `persona_id` never sent | Yes (registry, resolved server-side into the envelope) |
| Spoken-style rules (`voice_rules`) | Yes, on voice turns | No | Yes, on voice turns (in the envelope) |
| Turn-context delivery | Folded directly into the system prompt | None | Via the `lifeos_context.turn` envelope (#591) |
| Handoff (CLI engine) | Yes | No | No |
| Orchestrating personas | Runs natively (spawns a background session) | N/A — never selectable | Diverted client-side to LifeOS (#596); never actually forwarded to Hermes |
| Per-turn model selection | Yes (`model_override`: Auto/Sonnet/Opus/Gemma/Remote/Claude Code) | No — picker hidden | No — picker hidden; model choice is the harness's call, not LifeOS's |
| Conversation history LifeOS-owned | Yes (always has been) | No | Yes, since #592 (tee-persisted) |
| Survives a client disconnect (#611) | Yes, every modality (#616 lifted the voice exception — see above) | No — untouched, no observer to persist a detached turn's output | Yes, every modality (same as LifeOS) |
| Explicit cancel (`POST .../cancel`, #611) | Yes | Yes (the endpoint doesn't distinguish backends — but nothing detaches here to cancel) | Yes |

### Default backend selection

`web/chat/backend.js` owns the three-way selector. With **no stored preference**, a fresh session resolves to `hermes` if its availability check (`GET /api/hermes/status`) succeeds, else `lifeos` — this default applies **only when Hermes is configured and reachable**; a machine with no `LIFEOS_HERMES_BACKEND_URL` set behaves exactly as it did before Hermes existed. An **explicit user choice — including explicitly picking `lifeos`** — always overrides this default and is remembered for the session. A client-supplied `Authorization` header is always stripped before either proxy substitutes its own bearer.

**Boot ordering (#607).** `main.js` calls `loadPersonas()` (`persona.js`) before `initBackend()` (`backend.js`), so `config.personaId` — restored from `sessionStorage` synchronously, before `loadPersonas()`'s own `/api/personas` fetch — is always *set* by the time `initBackend()` reads it. But set isn't the same as *validated*: `loadPersonas()` only falls back to `primary` (if the fetched persona list doesn't contain the stored id, or the fetch fails outright) after that fetch resolves, and it runs unawaited alongside `initBackend()`. `loadPersonas()` deliberately does **not** list the sidebar itself, for two independent reasons that both have to clear before listing is safe: `config.backend` isn't resolved until `initBackend()`'s availability checks finish (listing any earlier would always request the `lifeos` fallback, and that request's response could land *after* the correctly-filtered one and silently overwrite it), and `config.personaId` isn't *validated* until `loadPersonas()`'s own promise settles (listing before that could key the persona-scoped conversation list on a stale, since-deleted id). `initBackend()` takes `loadPersonas()`'s promise as an argument and awaits it — alongside, not instead of, its own availability `Promise.all()`, so neither fetch is serialized behind the other — immediately before issuing the sidebar's one and only initial `GET /api/conversations`. No earlier request exists to race, and no redundant one follows it.

### Conversation-id storage keys

Conversation ids are stored per backend in `sessionStorage` so switching and refreshing continues the right thread:

| Backend | Key |
|---|---|
| `lifeos` | `lifeos:chat:conv:lifeos:<persona>` (persona-scoped) |
| `agent` | `lifeos:chat:conv:agent` (not persona-scoped — Agent has no personas) |
| `hermes` | `lifeos:chat:conv:hermes:<persona>` (persona-scoped, like `lifeos`) |

Since #592, `restoreBackendConversation()` renders the stored conversation on a switch **to either `lifeos` or `hermes`** (both are LifeOS-owned history now) — only switching to `agent` keeps the fresh-view-but-retain-the-id behavior, because that backend's history still isn't persisted here.

### Conversation titling

A conversation is retitled exactly once: after its second user message, a cheap LLM call (`api/services/conversation_titler.py`, pinned to the local routing llama-server regardless of `LIFEOS_LLM_BACKEND`, so it never touches the paid API path) generates a short title from the exchange and replaces the placeholder/first-message-truncation title. No rename feature exists, so a system-set title is never overwritten again. The seam is fired (fire-and-forget, idempotent — it checks the message count fresh against the store) from all three turn-persisting paths: the native chat turn's completion (`api/routes/chat.py`), the Hermes proxy tee (`api/routes/hermes_proxy.py`), and the voice persistence tee (`api/routes/voice.py`, see "Persistence tee" above) — so a conversation gets the same treatment regardless of which backend or surface it started on. Any titling failure is caught and logged once; the existing title is left in place.

The sidebar shows a conversation's persona as a subtitle suffix (`· <Persona Label>`) whenever it isn't `primary`, resolved client-side from `config.personas` (the same `/api/personas` payload the persona picker renders from) — never a hardcoded id-to-label map. This needed no backend change: every persisting path already recorded `persona_id` via `create_conversation()`, and the conversation list endpoint already returned it.

---

## Before changing chat or conversation APIs

1. Read [api-reference.md](../product/api-reference.md) § Chat and Conversations endpoints.
2. Compare `web/index.html`, `api/services/telegram.py`, `~/Code/whisper-relay/src/voice_gateway/adapters/lifeos.py`, and the Hermes proxy (`api/routes/hermes_proxy.py`, plus its shared plumbing in `api/routes/_proxy.py` — a change there also affects `api/routes/agent_proxy.py`).
3. Run contract tests listed in [testing-standards.md](../standards/testing-standards.md#http-client-contract-tests).
4. Treat removals or renames of public fields/events as a **breaking change** — update whisper-relay in the same release or maintain backward compatibility.

---

## Related Documents

### Design Context
- [ADR-016: Reverse-Proxy the Voice Gateway Through LifeOS](../../adr/016-voice-gateway-reverse-proxy.md) — the voice proxy's "no voice logic" default this doc's Persistence tee section is the one exception to
- [ADR-019: A Turn's Lifetime Is Owned by the Server, Not the Connection](../../adr/019-turn-owned-by-server.md) — why a disconnect no longer stops a turn, the original voice exception, and the tradeoffs
- [ADR-020: The Voice Detachment Gate Is Lifted](../../adr/020-voice-cancel-gate-lifted.md) — why ADR-019's voice exception was temporary, and what made it safe to remove
- [ADR-021: Tee Voice Turns Into the Conversation Store at the Proxy Seam](../../adr/021-voice-turn-persistence-tee.md) — the Persistence tee section's full rationale, alternatives, and the known whisper-relay#32 double-append gap

### Specifications
- [API Reference](../product/api-reference.md) — Canonical endpoint and SSE shapes
- [Chat UI](../product/chat-ui.md) — Web chat product behavior
- [Architecture](architecture.md) — Code layout for chat routes and services
- [Testing Standards](../standards/testing-standards.md) — Contract regression tests
- [Agent Worker — Technical](agent-worker.md) — The `?conversation=<id>` deep link a board-assigned Hermes card's open action lands on (#851), additive to this doc's SSE contract

### Operational
- [Voice Setup](../../guides/voice-setup.md) — Operator-facing voice mode setup, including the Hermes/Agent backend contract this doc specifies
- [Installation](../../guides/installation.md) — Config-only second-user setup checklist that references this doc's Hermes default/fallback contract
- [Telegram Setup](../../guides/telegram-setup.md) — Operator setup for the primary and persona bots this doc's "Telegram bot backends" section covers

### Code References
- [Voice proxy](../../../api/routes/voice.py) — `/api/voice/*` reverse proxy and `_VoiceTurnPersister` (#711)
- [Chat route](../../../api/routes/chat.py) — SSE emission and handoff handler
- [Conversations route](../../../api/routes/conversations.py) — List/detail handlers
- [Proxy factory](../../../api/routes/_proxy.py) — Shared status/ask-stream plumbing for the Agent and Hermes backends
- [Agent proxy](../../../api/routes/agent_proxy.py) — Agent text-backend routes
- [Hermes proxy](../../../api/routes/hermes_proxy.py) — Hermes text-backend routes, `lifeos_context` envelope, turn persistence, and `POST /api/hermes/resolve-persona` / `POST /api/hermes/register-persona-message` (#644, Hermes-Telegram persona selection + reply-thread inheritance)
- [Hermes persona thread store](../../../api/services/hermes_persona_thread_store.py) — the `(chat_id, message_id) -> persona_id` mapping behind reply-thread inheritance (#644 follow-up)
