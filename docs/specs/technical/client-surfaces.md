# HTTP Client Surfaces

> **Status:** Complete
> **Owner:** Platform
> **Last Updated:** 2026-08-19

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
- **Orchestrating personas always run on LifeOS, regardless of the selected text backend (#596).** The spawn above is LifeOS-native (background session + thread linking) with no Hermes/Agent equivalent, so `web/chat/ask-stream.js` diverts an orchestrating persona's turn to `POST /api/ask/stream` even while Hermes is selected, instead of the Hermes proxy — the picker still offers the persona on Hermes (unchanged from #590), and the UI shows a "Runs on LifeOS" badge next to the picker whenever the selected persona orchestrates. `web/chat/persona.js`'s `personaOrchestrates()` reflects this: true for an orchestrating persona on both `lifeos` and `hermes`, false on `agent` (no persona pass-through there at all). `personaSupportsHandoff()` is unchanged and still false on both `agent` and `hermes` — restoring orchestration does not restore handoff; they are different mechanisms, and a diverted turn runs the spawn wholesale rather than handing off mid-stream. The agent backend never diverts (it never sends `persona_id` in the first place, so nothing to key the diversion on).
- **Answering a spawned session's `[CLARIFY]`/`[GOAL]` from web/voice:** on a successful spawn the server links the conversation to the spawned agent session (`conversations.agent_session_id`). If that session emits `[CLARIFY]`/`[GOAL]`, the worker registers an open question keyed on the session (not just a Telegram `message_id`), and the web/voice client can answer it **without Telegram**:
  - `GET /api/conversations/{id}` includes a `pending_question` object (`{session_id, question, kind}`) **only while** the spawned session is awaiting an answer; it is absent/`null` otherwise. The client renders an answer affordance when present.
  - `POST /api/conversations/{id}/answer` `{answer}` deposits the answer onto that **existing** open question via the session-keyed deposit, preserving its `kind`. The worker's existing tick (`_resume_goal` for `goal_approval`, `_resume_as_followup` / clarification otherwise) resumes the session — the **same** single resume mechanism Telegram replies use. **No second resume path.** Empty answer → **400**; no spawned session / no open question (never asked, already answered, or timed out) → **409**. The Telegram reply-to-resume round-trip is unchanged.
  - This is the **input** direction (answering / resuming). The complementary **output** direction (streaming the session's results back into the web thread) is #311; until then, results still arrive via the bot's Telegram + the `/agents` page.
  - This answer path is **backend-agnostic**: it reads `conversations.agent_session_id`, never `backend`, so it works unchanged whether the linked conversation was created on `lifeos` or diverted here from a Hermes-selected turn (#596).
- **`backend` tag on `POST /api/ask/stream` (#596).** An optional field, used **solely** to label a newly created conversation for sidebar filtering — never to route, resolve a persona, or pick a model. The client sets it only when diverting an orchestrating persona's turn from Hermes (above), to `"hermes"`, so the resulting LifeOS-native conversation stays visible in the Hermes-filtered sidebar instead of vanishing into the `lifeos` bucket. Omitted (the default on every non-diverted turn) tags the conversation `"lifeos"`, reproducing pre-#596 behavior exactly. `GET /api/conversations` gained a matching optional `backend` query filter (unset = unfiltered, preserving today's behavior for every existing caller).

Web chat implements this contract in `web/chat/persona.js`: a top-of-chat toolbar `<select>` populated from `/api/personas`, the selection persisted across refresh in `sessionStorage` (`lifeos:chat:persona_id`), capability-gated `claude_intent` handoff, and a persona-scoped sidebar. Switching persona starts a fresh, persona-scoped conversation.

**Model picker.** The same toolbar carries a per-turn model picker (`web/chat/model.js`): `Auto` (Haiku + escalation) / `Sonnet` / `Opus` / `Gemma (local)` / `Claude Code`. An explicit `Sonnet`/`Opus` pick is the operator asking for an API model, which is why it dispatches without a prompt — `Auto`'s own escalation only climbs to non-API engines. The choice persists in `sessionStorage` (`lifeos:chat:model`) and rides along on `/api/ask/stream` as `model_override` (omitted for `auto`, so the default request is unchanged). For the inline model picks the server pins the turn to that model — cloud picks reuse the per-turn escalation client; `gemma` builds a per-turn `LocalLLMClient`. Context is preserved the same way every turn is: the full `conversation_history` is replayed into the chosen client. `Claude Code` is not an inline model: selecting it short-circuits the turn into the same engine handoff the orchestrator emits for an inferred "use claude code" directive (a `claude_intent` SSE event → `/api/chat/handoff` → `spawn_claude_code_session`), so the message runs in a background Claude Code worker rather than answering inline, on any backend. The frontend treats an explicit `claude_code` pick as its own handoff opt-in, bypassing the persona capability gate that filters inferred handoffs. The **voice** surface carries the same model selection, lifeos-backend only: `web/chat/voice.js` forwards `model_override` on `/api/voice/turn/stream` exactly when the selected backend is `lifeos` (#593 — deliberately not extended to hermes, where model selection is the harness's call, not LifeOS's); whisper-relay relays it to `/api/ask/stream` (#24). The picker is shown in voice mode as well as text — hidden on the Agent and Hermes backends (the persona picker stays visible on Hermes, unlike Agent).

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

- `POST /api/voice/turn/stream` — multipart `audio` (or `transcript`) + `backend` (`lifeos`|`agent`|`hermes`) + `persona_id` (lifeos and hermes backends, #593; omitted for agent — no persona pass-through there) + `model_override` (lifeos backend only — model selection on hermes belongs to the harness, not LifeOS) + `conversation_id`. Responds with an SSE stream: `started` (turn_id, for cancel), `transcript`, `status_audio`/`main_audio` (clip URLs, played as they arrive), `response`, and a terminal `done` whose `data` is **authoritative**: `{transcript, response_text, status_audio_urls, audio_url, conversation_id, handoff, timings_ms}`. `error`/`cancelled` terminate the turn.
- `POST /api/voice/turn/{turn_id}/cancel` — cancel an in-flight turn.
- `GET /api/voice/audio/{turn_id}/{clip_id}` — WAV clips (status + main).

**Topology:** browser → LifeOS `/chat` → (proxy) → gateway → for the `lifeos` backend, the gateway calls back into LifeOS `/api/ask/stream` (the consumer-into-LifeOS direction above); for the `hermes` backend (#593), the gateway is expected to call LifeOS's **own** Hermes proxy, `POST /api/hermes/ask/stream` — not the Hermes harness directly — setting `modality: "voice"` on that call so the persona's spoken-style rules are attached exactly like a native spoken turn. Routing a Hermes voice turn past the proxy would skip the persona resolution, `lifeos_context` envelope, and conversation persistence that only exist at that seam (see "The `lifeos_context` envelope" below) — the same gap this closes on the text path (#590/#592). A gateway not yet updated to route this way (`nbramia/whisper-relay#32`) must fail the turn visibly with its own error rather than silently answering without persona or context. Conversation/persona listing is owned by LifeOS, not the gateway.

**Orchestrating personas have no voice equivalent of the text-path diversion.** `web/chat/ask-stream.js` diverts a Hermes-selected orchestrating persona's *text* turn back to `POST /api/ask/stream` (#596, above) because the Hermes proxy backstops it with a 400. Voice never diverts — a spoken turn for an orchestrating persona on the Hermes backend reaches that same 400 backstop with nothing to catch it first. `web/chat/voice.js` accounts for this: it only starts pending-question polling (`startPendingQuestionPolling()`) after a `lifeos`-backend turn, never a `hermes`- or `agent`-backend one, since only a LifeOS-backend spawn ever has a session to poll for.

Web chat implements voice mode in `web/chat/voice.js` — tap-to-talk turn lifecycle (Voice|Text toggle, SSE `done` data, sequential audio, cancel via `AbortController`), same-origin via the reverse proxy. Mode persists in `sessionStorage` (`lifeos:chat:voice_mode`).

## Text Backends

`/chat` (and voice, via the gateway) targets one of three text backends, carried as an optional `backend` field (`lifeos` | `agent` | `hermes`, omitted/`lifeos` reproducing pre-#361 behavior exactly). Each gets its own section below because their capabilities genuinely differ — treat no statement in one backend's section as implying anything about another's.

### LifeOS

The native orchestrator (`POST /api/ask/stream`, documented above and in [api-reference.md](../product/api-reference.md)). Full personas (including orchestrating ones, which spawn a background Claude Code session — see the Persona contract above), spoken-style rules on voice turns, the per-turn model picker, CLI-engine handoff, and conversation history that has always lived in LifeOS's own `ConversationStore`. Every other backend is described relative to this one.

### Agent

`agent` is the OpenClaw voice-adapter, reached at `LIFEOS_AGENT_BACKEND_URL` (optional bearer token) and proxied at `POST /api/agent/ask/stream` — LifeOS **adds the bearer server-side** so it never reaches the browser, and `GET /api/agent/status` reports whether it's configured (drives the UI selector). It speaks the same `/api/ask/stream` SSE contract as the native path. It has **no personas at all** — `persona_id` is never sent, so the persona and model pickers are both hidden while it's selected, no persona is ever diverted (nothing to divert), and no per-turn context is attached. It has **no handoff**. The route (`api/routes/agent_proxy.py`) is a pure byte relay (`request.stream()`, unbuffered — no `transform_body`, no `make_observer`), so its behavior is byte-for-byte what it was before Hermes existed. Its conversation history is **not** LifeOS-owned: nothing here persists it, so switching to `agent` shows a fresh view even though the stored conversation id is retained for continuity with whatever does own that history upstream. Because it isn't tee'd at all, its turns are also invisible to the usage store — unlike Hermes, below.

**These are properties of the Agent backend specifically, not of "external backends" generally** — Hermes, below, shares only some of them.

### Hermes

`hermes` is an agent harness reached as a gateway (#587), at `LIFEOS_HERMES_BACKEND_URL` (optional bearer token), proxied at `POST /api/hermes/ask/stream` with the same server-side bearer injection and a `GET /api/hermes/status` availability check. Both proxies are built by the same `make_backend_router()` factory in `api/routes/_proxy.py`; `agent_proxy.py` and `hermes_proxy.py` each just name their own settings fields and `_client()` test seam. Hermes has **no handoff** either — but unlike Agent, it keeps the persona picker visible, carries spoken-style rules on voice turns, receives the orchestrator's auto-injected turn context, and its history **is** LifeOS-owned. Its route (`api/routes/hermes_proxy.py`) buffers the request body (`transform_body`) to resolve the selected persona and attach it as the `lifeos_context` envelope below — the only text-backend proxy that does. Orchestrating personas never reach this route: they're diverted client-side to `POST /api/ask/stream` instead (#596, above), because the spawn they trigger is LifeOS-native and Hermes has no equivalent.

#### Hermes turn persistence (#592) and usage capture (#595)

Unlike the Agent backend, whose history genuinely lives elsewhere, Hermes turns are persisted into the same conversation store the native path uses, and their usage/cost is recorded into the same usage store. `make_backend_router()`'s relay loop (`api/routes/_proxy.py`) accepts an optional `make_observer` hook: given the same raw request body `transform_body` already buffers, it returns a `_HermesTurnPersister` (`api/routes/hermes_proxy.py`) whose `observe(chunk)` is called with a copy of each chunk immediately *before* that chunk is yielded to the browser (so an early client disconnect — which raises `GeneratorExit` at the `yield` — can never skip a chunk the tee hasn't seen yet), and whose `finalize()` runs whenever the relay ends — normal completion or an early disconnect alike. The persister reassembles SSE frames from the observed bytes (a chunk is a network read, not a frame, so frames can split across chunk boundaries) and reacts to the event types the native path emits: on the first `conversation_id` event it adopts that id via `ConversationStore.create_conversation(conv_id=..., persona_id=..., backend="hermes")` (existing rows are returned unchanged, so a continuing thread doesn't get re-tagged) and stores the user's question; `content` events accumulate into the assistant reply, written on `finalize()` — including whatever arrived if the stream died mid-turn; a `usage` event (`input_tokens`, `output_tokens`, `cost_usd`, `model`) is captured and, on `finalize()`, written to `UsageStore.record_usage()` tagged with the conversation id — **cost is recorded verbatim, never recomputed** (Hermes runs DeepSeek via Fireworks; LifeOS's calculator only knows Anthropic pricing), and a cost-less event records a zero cost rather than an invented one. Conversation persistence and usage persistence are independent: a turn with no `usage` event writes no usage row (and vice versa), and each is retried/skipped on its own. A malformed or partial `usage` event (missing/wrong-typed model or token counts) is ignored entirely. Every store call is wrapped so a persistence failure is logged and swallowed, never surfacing as a broken turn. The Agent proxy passes neither `transform_body` nor `make_observer`, so its relay is byte-for-byte what it was before this hook existed.

#### Usage and cost reporting (external backends) — #595

Both proxied backends are expected to emit a `usage` SSE event matching LifeOS's native one — but a **subset** of its fields, since an external backend has no prompt-cache accounting to report: `type: "usage"`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (no `cache_read_tokens`/`cache_creation_tokens`). Today only Hermes is actually tee'd and captured (above); the Agent proxy has no observer, so an Agent-backend `usage` event, if one were ever emitted, would simply pass through unrecorded. [`GET /api/admin/usage`](../product/api-reference.md#get-apiadminusage) includes these rows in its totals — the usage store applies no per-model or per-backend filter, so anything written to it (native or relayed) counts.

`cost_usd` on this event carries three distinct states, and the client (`web/chat/ask-stream.js`) is expected to keep them distinct rather than collapsing to two (#602): a **priced** turn (a real number) adds to the running session total; a **free** turn (a real `0`) adds nothing and looks exactly like a session with no unpriced turns; an **unpriced** turn (the key absent, or present but non-numeric — the shape an external backend sends when it genuinely can't price a turn, deliberately rather than guessing) also adds nothing but marks the session total as a lower bound (a `~` prefix on the header's `#sessionCost` display and the usage modal's mirrored figure, with a tooltip naming how many turns were unpriced) for the rest of the session. The distinction hinges on an explicit presence-and-type check (`typeof cost === 'number' && Number.isFinite(cost)`) rather than truthiness — `data.cost_usd || 0` treats a real `0` the same as an absent value, which silently turns "unknown" into a confident (wrong) claim of "free" the first time a model appears with no rate. Server-side persistence (`_HermesTurnPersister._handle_usage`, above) is unaffected by this — it still records a missing `cost_usd` as `0.0` in the usage store, since recomputing a real cost from a local pricing table is explicitly out of scope (the calculator only knows Anthropic pricing and would misprice an unrecognized upstream model); the "unknown, not free" distinction is a display-layer concern for the one figure a person actually watches in the moment, the live session total. It does **not** survive into the usage store: #610's session-to-date cost (below) reads the same `cost_usd=0.0` rows and reports the resulting sum as an unconditional floor rather than attempting to infer, from persisted data alone, which zero-cost turns were free and which were merely unreported — that inference isn't recoverable from this data and would be a guess presented as fact.

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
      "session_output_tokens": 130
    }
  }
}
```

- `schema_version` is currently `1`.
- `modality` duplicates the request's `modality` (`"voice"` or `"text"`) so the envelope is self-contained.
- `persona.id` defaults to `primary` when the client sends no `persona_id`; `preamble` is the persona's markdown body verbatim (may be empty); `voice_rules` is populated only on voice turns, matching the `modality == "voice"` gate `ask_stream` in `api/routes/chat.py` uses for the native path; `orchestrates` is always `false` here — an orchestrating persona (`doctor`) never reaches this envelope, because the route rejects it with a 400 first (see below).
- `turn` (#591) is a **sibling** of `persona`, resolved by `build_turn_context()` in `api/services/agent_system_prompt.py` — the same function [`GET /api/chat/turn-context`](../product/api-reference.md#get-apichatturn-context) returns, so the two shapes can never drift apart. **Never merge `turn` into `persona`** — `persona` is stable across a whole conversation (cacheable), `turn` changes every turn; merging them would invalidate a consumer's prompt cache on every turn. The `session_*` fields (#610, below) are exactly why: a cumulative cost changes on every turn by definition, so it belongs here and must never move to `persona`.

**Validation and rejection**, in order, before any upstream request is made: malformed JSON → 400; unknown `persona_id` → 400 naming the id; a known but *orchestrating* `persona_id` → 400. That last case is a **backstop, not the user-facing path**: the client-side diversion (#596, above) is what's supposed to route an orchestrating persona's turn to `POST /api/ask/stream` instead of here, so this 400 firing means that diversion didn't happen — a bug, not a normal outcome. The persona itself stays selectable on the Hermes backend regardless (a routing guard, not a picker exclusion). Attachment size/type caps are enforced via the same `AskStreamRequest` model the native endpoint uses, so the limits can't drift between the two paths.

#### Turn-context payload

The `turn` object above — identical in shape to the response body of [`GET /api/chat/turn-context`](../product/api-reference.md#get-apichatturn-context), since both come from the same `build_turn_context()` call:

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `current_datetime` | string | always | Human-formatted local date/time, prompt-ready (`%A, %B %d, %Y at %I:%M %p %Z`). |
| `current_datetime_iso` | string | always | The same instant as ISO 8601 with offset, for machine use. |
| `timezone` | string | always | IANA zone name, e.g. `"America/New_York"` (`settings.timezone`). |
| `time_resolution_instruction` | string | always | Prompt-ready instruction to resolve relative time expressions ("last week") into concrete `YYYY-MM-DD` ranges before calling search tools. |
| `personal_context` | string | always present, often empty | A persona-scoped people block. Non-empty only for the `therapist` persona (and only once the relevant config is set); empty string for every other persona. |
| `existing_tags` | array of `{tag, count}` | always present, may be empty | Task tags already in use, for reuse when tagging. Empty when there are no tags or the task manager is unreachable — a normal degraded case, not an error. |
| `tags_instruction` | string | always | Prompt-ready instruction to prefer an existing tag over inventing a near-duplicate; pair with `existing_tags`. |
| `session_cost_usd` | number | always | Session-to-date cost (#610): the verbatim sum of `cost_usd` for every turn already recorded for this request's `conversation_id`, in USD. Excludes the turn currently being built — its own cost isn't recorded until its stream finishes, after this context was already handed out. `0` for a conversation with no recorded usage yet (including a brand-new conversation with no id). Never recomputed from token counts. **This is a floor, not an exact total** — see "Session-to-date cost" below for why. |
| `session_turn_count` | integer | always | How many already-recorded turns the sum above covers. `0` for a conversation with no recorded usage yet. |
| `session_input_tokens` | integer | always | Sum of `input_tokens` across the same already-recorded turns. |
| `session_output_tokens` | integer | always | Sum of `output_tokens` across the same already-recorded turns. |

#### Session-to-date cost (#610)

A Hermes session had no way to answer "what has this cost?" even though the browser header already shows a running total (`web/chat/ask-stream.js`'s `state.sessionCost`, accumulated client-side from each turn's `usage` SSE event). The four `session_*` fields above close that gap by exposing the recorded totals — read from `UsageStore.get_conversation_usage(conversation_id)`, never recomputed — through the same channel that already carries per-turn context. No database schema change was needed or made; the existing `usage` table already has everything this reads.

- **Scope is "turns completed so far," not "this turn."** The in-flight turn's own cost is unknowable until its own stream finishes, so `session_cost_usd` deliberately excludes it — the only honest reading.
- **`session_cost_usd` is always a floor, stated as one, never inferred to be exact.** A turn whose provider reported no cost is recorded as `cost_usd=0.0` — the same repo-wide convention every `record_usage()` caller uses (native `agent_loop.py`, `synthesizer.py`, the CLI-worker executors, and this Hermes path alike; see "Usage and cost reporting" above). There is no marker anywhere in the usage store distinguishing that from a turn that genuinely cost zero, and inferring one — e.g. guessing "unpriced" from a model name or a zero balance — would be a fabricated distinction presented as fact, not an honest one. So the field is documented, here and in its own description, as a floor unconditionally; a consumer that states it as a floor (`~$0.003 so far`) rather than an exact figure (`$0.003`) is being honest regardless of which case actually happened.
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
| Per-turn model selection | Yes (`model_override`: Auto/Sonnet/Opus/Gemma/Claude Code) | No — picker hidden | No — picker hidden; model choice is the harness's call, not LifeOS's |
| Conversation history LifeOS-owned | Yes (always has been) | No | Yes, since #592 (tee-persisted) |

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

---

## Before changing chat or conversation APIs

1. Read [api-reference.md](../product/api-reference.md) § Chat and Conversations endpoints.
2. Compare `web/index.html`, `api/services/telegram.py`, `~/Code/whisper-relay/src/voice_gateway/adapters/lifeos.py`, and the Hermes proxy (`api/routes/hermes_proxy.py`, plus its shared plumbing in `api/routes/_proxy.py` — a change there also affects `api/routes/agent_proxy.py`).
3. Run contract tests listed in [testing-standards.md](../standards/testing-standards.md#http-client-contract-tests).
4. Treat removals or renames of public fields/events as a **breaking change** — update whisper-relay in the same release or maintain backward compatibility.

---

## Related Documents

### Specifications
- [API Reference](../product/api-reference.md) — Canonical endpoint and SSE shapes
- [Chat UI](../product/chat-ui.md) — Web chat product behavior
- [Architecture](architecture.md) — Code layout for chat routes and services
- [Testing Standards](../standards/testing-standards.md) — Contract regression tests

### Operational
- [Voice Setup](../../guides/voice-setup.md) — Operator-facing voice mode setup, including the Hermes/Agent backend contract this doc specifies

### Code References
- [Chat route](../../api/routes/chat.py) — SSE emission and handoff handler
- [Conversations route](../../api/routes/conversations.py) — List/detail handlers
- [Proxy factory](../../api/routes/_proxy.py) — Shared status/ask-stream plumbing for the Agent and Hermes backends
- [Agent proxy](../../api/routes/agent_proxy.py) — Agent text-backend routes
- [Hermes proxy](../../api/routes/hermes_proxy.py) — Hermes text-backend routes, `lifeos_context` envelope, turn persistence
