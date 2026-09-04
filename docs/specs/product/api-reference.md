# API Reference

**Status:** Complete
**Owner:** API Gateway
**Last Updated:** 2026-09-04

Catalog of every HTTP endpoint LifeOS exposes, with request/response shapes. Two adjacent catalogs split out for size:

- **CRM endpoints** (`/api/crm/*`) → [api-crm.md](api-crm.md)
- **MCP tool catalog** (Claude Code / Managed Agents tools) → [mcp-tools.md](mcp-tools.md)

---

## Table of Contents

1. [API Overview](#api-overview)
2. [Chat & Search Endpoints](#chat--search-endpoints)
3. [Google Integration](#google-integration)
4. [Messaging Endpoints](#messaging-endpoints)
5. [CRM Endpoints — see api-crm.md](api-crm.md)
6. [Memories Endpoints](#memories-endpoints)
7. [Conversations Endpoints](#conversations-endpoints)
8. [Briefing Endpoints](#briefing-endpoints)
9. [People Endpoints](#people-endpoints)
10. [Photos Endpoints](#photos-endpoints)
11. [Task Endpoints](#task-endpoints)
12. [Scheduler & Telegram Endpoints](#scheduler--telegram-endpoints)
13. [Monarch Money Endpoints](#monarch-money-endpoints)
14. [Job Queue Endpoints](#job-queue-endpoints)
15. [Performance Trace Endpoints](#performance-trace-endpoints)
16. [Admin Endpoints](#admin-endpoints)
17. [MCP Tools — see mcp-tools.md](mcp-tools.md)

---

## API Overview

**Base URL:** `http://localhost:8000`

**Authentication:** None (Tailscale-only access)

**External HTTP clients:** See [Client Surfaces](../technical/client-surfaces.md) for consumers (web, Telegram, whisper-relay) and breaking-change policy.

**OpenAPI Spec:** `GET /openapi.json`

---

## Chat & Search Endpoints

### POST /api/ask/stream

Streaming chat with an agentic pipeline. Claude autonomously decides which tools to call, iterating when initial results are insufficient. Returns an SSE stream.

**Request:**
```json
{
  "question": "What did we discuss in the product meeting?",
  "conversation_id": "optional-uuid",
  "include_sources": true,
  "persona_id": "fitness"
}
```

- `persona_id` (optional) — selects a chat persona by id (see [`GET /api/personas`](#get-apipersonas)). The server applies the same system-prompt preamble the matching Telegram bot uses. Unknown ids return **400**. Omit it for the default (`primary`) persona. A new conversation created in this call is tagged with the persona so it can be filtered later (see [`GET /api/conversations`](#get-apiconversations)).
- `persona` (optional, internal) — raw preamble text used by the in-process Telegram client. Mutually exclusive with `persona_id` (sending both returns **400**); HTTP clients should use `persona_id`.
- `model_override` (optional) — pins the model for **this turn**. `"sonnet"` / `"opus"` (or a full model id) run the turn on that cloud model; `"gemma"` / `"local"` run it on the local llama-server; `"remote"` (#654) runs it on the configured paid OpenAI-compatible provider (e.g. Fireworks) — ignored, falling back to `"auto"`, unless that provider is fully configured (base URL, model, API key); `"auto"` or omitted uses the default orchestrator (Haiku) with escalation — which climbs only to non-API engines (`claude_code` / `codex` / `local`), so a cloud model or the remote provider is reached only by an explicit pick here or (cloud models only) a user-directed "escalate to opus" in the message. An explicit pick takes precedence over auto-escalation. Honored on the Anthropic backend; unknown values fall back to `auto`. Drives the web chat model picker.
- `backend` (optional) — tags a **newly created** conversation for sidebar filtering (see `?backend=` on [`GET /api/conversations`](#get-apiconversations)). This is the only thing it does: it never changes routing, model selection, or persona resolution. Omitted, it tags `"lifeos"` (today's behavior, unchanged). Through #641 the web client set this to `"hermes"` itself when diverting an orchestrating persona's turn from a Hermes-selected composer to this endpoint — that persona's spawn was LifeOS-native with no Hermes equivalent, so the turn landed here regardless of the selected backend. #642 gave Hermes its own way to drive an orchestrating persona and removed that diversion, so the web client no longer sends this field on any turn; it remains a generic, supported field on this endpoint for any other caller.
- `client_turn_id` (optional, #611) — an opaque key the **client** generates before sending the request, used to cancel this turn via [`POST /api/chat/cancel`](#post-apichatcancel). Closes a gap `conversation_id` alone can't: a brand-new conversation's id doesn't exist until the `conversation_id` SSE event arrives, so a client that wants to cancel before then (the common voice barge-in case) has nothing to cancel by unless it minted this key itself. Bounded to 200 chars, no control characters; not required to be a UUID — any locally-unique string works. Reusing the same key on a later request supersedes (cancels) whichever turn currently holds it, the same as reusing `conversation_id` does. Purely additive — omitting it changes nothing about the turn.

**Response:** Server-Sent Events stream with event types:

| Event | Fields | Description |
|-------|--------|-------------|
| `routing` | `sources`, `reasoning`, `latency_ms` | Which pipeline path was selected |
| `status` | `message` | Tool execution status (e.g. "Searching notes...") |
| `content` | `content` | Streamed response text chunk |
| `self_correction` | — | Model retrying; consumers should clear buffered text |
| `conversation_id` | `conversation_id` | Assigned or confirmed thread id (required for multi-turn clients) |
| `sources` | `sources` | Data sources used (vault, calendar, gmail, etc.) |
| `claude_intent` | `task`, `engine` (`claude_code` \| `codex`) | CLI engine handoff; HTTP clients POST `/api/chat/handoff` |
| `error` | `message` | Fatal error for this turn |
| `usage` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd` | Token usage and cost |
| `done` | — | Stream complete |

**Wire format:** lines `data: {json}\n`. Clients may ignore `routing`, `sources`, `usage`, and `done` if they handle stream close.

`cost_usd` on the `usage` event is omitted (not `0`) for a turn this server can't price — e.g. the `remote` model pick (#654) with no configured rate — so a client must distinguish "absent/non-numeric" (unpriced) from a real `0` (genuinely free) rather than treating both as free; `web/chat/ask-stream.js` already does this.

**A disconnect no longer stops the turn (#611).** The turn keeps running server-side and persists its complete reply once done — closing the tab, backgrounding the app, or a network switch no longer loses the answer. Two ways to cancel it explicitly: `POST /api/conversations/{id}/cancel` (by conversation id) and [`POST /api/chat/cancel`](#post-apichatcancel) (by `client_turn_id`, for a turn that hasn't reached its first SSE frame yet). `GET /api/conversations/{id}`'s `active_turn` field reports whether one is still running. `modality: "voice"` is the one exception — a voice turn's disconnect still cancels it immediately, matching pre-#611 behavior, because whisper-relay uses abandoning the stream as its deliberate barge-in/hangup cancel gesture. See [client-surfaces.md § Turn lifetime and cancellation](../technical/client-surfaces.md#turn-lifetime-and-cancellation-611) for the full contract.

**Pipeline routing (in order of priority):**
1. **Ambiguous task/reminder** — asks user for clarification (task vs reminder vs both).
2. **Claude intent** — terminal, filesystem, browser tasks. Yields `claude_intent` event for Telegram to spawn Claude Code.
3. **Agentic loop** — everything else (including compose, tasks, reminders). Claude gets 21 tools and up to 5 rounds to fetch data and synthesize an answer. See `api/services/agent_tools.py::TOOL_DEFINITIONS` for the canonical list — count `len(TOOL_DEFINITIONS)` to re-derive this number.

**Agentic loop tools (21):**

| Tool | Description |
|------|-------------|
| `search_vault` | Obsidian notes, journals, meeting transcripts |
| `read_vault_file` | Read full vault file by name (fuzzy matched) |
| `search_calendar` | Google Calendar (personal + work) |
| `search_email` | Gmail (personal + work) |
| `search_drive` | Google Drive files |
| `search_slack` | Slack messages |
| `search_web` | Web search (weather, news, public info) |
| `get_message_history` | iMessage/WhatsApp (requires entity_id) |
| `person_info` | Lookup or briefing (action: lookup/briefing) |
| `search_finances` | Monarch Money live data (action: accounts/transactions/cashflow/budgets) |
| `manage_tasks` | Create, list, or complete tasks (action: create/list/complete) |
| `manage_schedules` | Create or list schedules (action: create/list; schedule_action: notify/prompt/endpoint/agent). `manage_reminders` kept as a deprecated alias |
| `create_email_draft` | Gmail draft (returns a draft_id; never sends) |
| `send_email_draft` | Send an existing Gmail draft by draft_id (gated: only after the user confirms in a later turn — a draft created this turn cannot be sent) |
| `create_calendar_event` | Create a Google Calendar event |
| `update_calendar_event` | Update a Google Calendar event |
| `delete_calendar_event` | Delete a Google Calendar event |
| `save_memory` | Save a memory for future reference |
| `search_memories` | Search previously saved memories |
| `manage_workouts` | Log and query the workout log and fitness metrics (action: log/update/list/history/summary/log_metric/metrics/get_profile/set_profile/readiness) |

**Prompt caching (Anthropic backend only):** System prompt and tool definitions use Anthropic `cache_control` breakpoints. Cache reads cost 0.1x input price; repeated queries within 5 minutes hit the cache.

### GET /api/personas

List chat personas available to HTTP clients (web chat, voice/whisper-relay). Returns the `primary` persona plus each configured specialized Telegram bot whose token env is set. No secrets are exposed. Adding a registry entry (`config/telegram_bots.json`) plus its token env var surfaces a new persona after restart — no code change.

**Response:**
```json
{
  "personas": [
    { "id": "primary", "label": "LifeOS", "capabilities": ["handoff", "agent"], "orchestrates": false },
    { "id": "doctor", "label": "Doctor", "capabilities": ["handoff", "agent"], "orchestrates": true },
    { "id": "fitness", "label": "Fitness", "capabilities": [], "orchestrates": false },
    { "id": "therapist", "label": "Therapist", "capabilities": [], "orchestrates": false }
  ]
}
```

- `id` — pass as `persona_id` on [`POST /api/ask/stream`](#post-apiaskstream) and as the `persona_id` query param on [`GET /api/conversations`](#get-apiconversations).
- `label` — display name; defaults to the capitalized id when the registry entry omits an explicit `label`.
- `capabilities` — `["handoff", "agent"]` (CLI engine handoff and `/agent` spawns) for the `primary` persona and any orchestrating bot (`orchestrates: true` in the registry, e.g. the doctor self-repair bot). Pure-chat specialized personas advertise `[]`. Note that `capabilities` alone doesn't distinguish `primary` from an orchestrating bot — both carry `["handoff", "agent"]` — use `orchestrates` for that (#643).
- `orchestrates` (#643) — whether this persona spawns a background Claude Code session on send (e.g. `doctor`) rather than answering inline, mirroring `settings.persona_orchestrates()` — the same check real routing (`api/routes/chat.py`, `api/routes/hermes_proxy.py`) applies. Always `false` for `primary`, which carries handoff/agent capabilities but answers inline itself.

### GET /api/chat/turn-context

Read-only per-turn context: the current date/time, the relative-time-resolution instruction, a persona-scoped personal-context block, existing task tags with usage counts, and (#610) session-to-date cost/token totals. Exports the same computation the native orchestrator folds into its system prompt (`build_turn_context()` in `api/services/agent_system_prompt.py`), as plain JSON with no dependency on the Anthropic content-block format — any MCP client (registered as `lifeos_turn_context`) or the Hermes backend can pull it at the start of a turn without a LifeOS-specific integration (#591). Never creates, mutates, or persists anything.

**Query Parameters:**
- `persona_id` (optional, default `"primary"`) — same registry `GET /api/personas` resolves against. Unknown ids return **400**.
- `modality` (optional, default `"text"`) — accepted for shape symmetry with [`POST /api/ask/stream`](#post-apiaskstream); no field in the response currently varies with it (voice-specific material lives in a persona's `voice_rules`, not here).
- `conversation_id` (optional, default none) — scopes `session_cost_usd` and friends (#610) to that conversation's already-recorded usage. Omitted, or a conversation with no recorded usage yet, reports those fields present and zero rather than omitting them.

**Response:**
```json
{
  "current_datetime": "Wednesday, August 19, 2026 at 09:14 AM EDT",
  "current_datetime_iso": "2026-08-19T09:14:22-04:00",
  "timezone": "America/New_York",
  "time_resolution_instruction": "When the user asks for something time-relative...",
  "personal_context": "",
  "existing_tags": [{ "tag": "ai-agent", "count": 12 }],
  "tags_instruction": "When the user asks to tag a task, prefer an existing tag...",
  "session_cost_usd": 0.0031,
  "session_turn_count": 2,
  "session_input_tokens": 300,
  "session_output_tokens": 130,
  "session_cost_is_lower_bound": false
}
```

- `personal_context` is non-empty only for the `therapist` persona (and only once `LIFEOS_PARTNER_NAME`/`LIFEOS_THERAPIST_PATTERNS` are configured); empty string for every other persona.
- `existing_tags` is `[]` when there are no tags or the task manager is unreachable — a normal degraded case, not an error.
- `session_cost_usd`/`session_input_tokens`/`session_output_tokens` are the verbatim sum of every turn already recorded under `conversation_id`, excluding the turn currently being built; `session_turn_count` is how many recorded turns that sum covers. `session_cost_is_lower_bound` is `true` when any summed turn was recorded `unpriced` — its provider reported no cost, rather than a real zero (the `unpriced` column on the `usage` table). `false` means every row the sum touches was written with the real distinction available and none of them were unpriced — **except** for a sum spanning a row written before that column existed: that history can't be reclassified and always reads as priced, so such a sum is still only a floor even when this flag says `false`. See [client-surfaces.md](../technical/client-surfaces.md) § "Session-to-date cost" for the full contract, including that retroactive gap.
- This is the exact shape embedded as `lifeos_context.turn` in the Hermes envelope — see [client-surfaces.md](../technical/client-surfaces.md) § "The `lifeos_context` envelope" — both come from the same function call, so they cannot diverge for these fields. The one exception is `caller_session_id` (#640), added only to the Hermes envelope's `turn` (an agent-worker session identity this endpoint has no reason to hand out) — see § "Hermes agent-worker session identity".

### POST /api/chat/cancel

Cancel a chat turn (native or Hermes-relayed) by `client_turn_id` (#611) — the key the client generated and sent on [`POST /api/ask/stream`](#post-apiaskstream), before it necessarily had a `conversation_id` to cancel by instead. This is the endpoint for the "cancel before the first SSE frame arrives" case (e.g. a voice barge-in on a first turn); once a conversation id is known, `POST /api/conversations/{id}/cancel` works too, and either can supersede the same turn.

**Request:**
```json
{ "client_turn_id": "some-opaque-client-generated-key" }
```

**Response:**
```json
{ "ok": true, "cancelled": true }
```

`cancelled` is `false` when nothing was in flight under that key (never claimed, already finished, or already cancelled) — never an error; there's no resource to 404 on here (unlike the conversation-scoped endpoint), since an unknown or expired key legitimately has nothing to report. Calling this while the turn's SSE reader is still attached works correctly — the reader gets a clean end-of-stream rather than an error, and a client that then also disconnects sees a no-op, not a second finalize. **Errors:** `422` empty/oversized (>200 chars)/control-character `client_turn_id` (standard FastAPI request validation).

### POST /api/chat/handoff

Spawn a CLI engine worker session when the orchestrator emits `claude_intent` on the SSE stream. HTTP clients call this endpoint; Telegram spawns in-process instead.

**Request:**
```json
{
  "engine": "claude_code",
  "task": "refactor the parser",
  "conversation_id": "optional-uuid"
}
```

- `engine` — `"claude_code"` or `"codex"` only
- `task` — non-empty task description forwarded to the worker
- `conversation_id` — optional; when set, an assistant acknowledgment is appended to the thread

**Response (200):**
```json
{
  "ok": true,
  "engine": "claude_code",
  "session_id": "sess_abc123",
  "working_dir": "/path/to/repo",
  "message": "Handed off to Claude Code — running in the background..."
}
```

### Voice, Agent & Hermes backends

`/chat` reaches three transport surfaces through LifeOS reverse proxies so the browser stays same-origin (#361, #587). The shapes are owned upstream; LifeOS only forwards.

- `POST /api/voice/turn/stream` — multipart voice turn (`audio` or `transcript`, plus `backend`, `persona_id`, `conversation_id`) → SSE turn events (`started` / `transcript` / `status_audio` / `response` / `main_audio` / `done` / `error` / `cancelled`); the `done` data is authoritative. Proxied to `LIFEOS_VOICE_GATEWAY_URL` (whisper-relay). Also `POST /api/voice/turn/{turn_id}/cancel` and `GET /api/voice/audio/{turn_id}/{clip_id}`.
- `POST /api/agent/ask/stream` — text turn for the "Agent" backend; same SSE as [`POST /api/ask/stream`](#post-apiaskstream) but no handoff and no persona. Proxied to `LIFEOS_AGENT_BACKEND_URL` with a bearer token added **server-side** (never exposed to the browser). `GET /api/agent/status` → `{"available": bool, "configured": bool, "reachable": bool}` (drives the UI selector) — Agent doesn't opt into the reachability probe (see the Hermes entry below), so `configured` and `reachable` both just mirror `available` here: configuration alone, no network call.
- `POST /api/hermes/ask/stream` — text turn for the "Hermes" backend; same SSE and proxy behavior as the Agent backend above (same factory, `LIFEOS_HERMES_BACKEND_URL`), but the persona picker stays visible client-side, and the route resolves the selected persona (with `surface="hermes"`, so a persona with a Hermes-specific body — e.g. `doctor` — gets that instead of its plain one) and per-turn context and attaches them to the forwarded body as a `lifeos_context` envelope (`{schema_version, modality, persona: {id, label, preamble, voice_rules, orchestrates}, turn: {...}}`, `turn` a sibling of `persona` per [`GET /api/chat/turn-context`](#get-apichatturn-context)) — a cross-repo contract with `nbramia/hermes` pinned on issue #590. Rejects with 400 (before forwarding) on malformed JSON or an unknown `persona_id`. An orchestrating persona (e.g. `doctor`) reaching this route used to be a third 400 case, backstopping a client-side diversion to [`POST /api/ask/stream`](#post-apiaskstream) (#596) — **as of #642 that diversion and backstop are both gone**: the persona reaches this route like any other, and `lifeos_context.persona.orchestrates` can now be `true` (a cross-repo contract change from its previous always-`false` guarantee — **this merge is gated on `nbramia/hermes#57`**, which stops Hermes from treating `true` as fatal; see client-surfaces.md for the full picture). `GET /api/hermes/status` → `{"available": bool, "configured": bool, "reachable": bool}` — `available` is true only when both `configured` (a URL is set) and `reachable` (a cached, short-timeout probe of that URL succeeded) hold, distinguishing "not set up" from "set up but down" (`GET /api/agent/status`'s `configured`/`reachable` fields, by contrast, both just mirror `available` — configuration alone, no reachability probe). See [client-surfaces.md](../technical/client-surfaces.md) § "The `lifeos_context` envelope" for the full schema.
- **Hermes turns are persisted** (#592) **and survive a client disconnect** (#611), unlike the Agent backend: the route tees the relayed SSE bytes to the browser unchanged and, in parallel, reconstructs the turn from the `conversation_id` and `content` events it already emits (same shapes the native `POST /api/ask/stream` uses) — adopting the id Hermes minted, creating the conversation row (tagged `persona_id` + `backend: "hermes"`) on first sight of it, and storing the user question and assembled assistant reply as two messages. Since #611 the upstream drain runs as a background pump independent of the browser connection, so a disconnect no longer cuts a Hermes turn short either — it persists the *complete* reply, not just whatever had streamed so far. A turn whose upstream connection genuinely ends before a `done` event ever arrived still gets a truncation marker + `routing.truncated` (see [client-surfaces.md](../technical/client-surfaces.md#turn-lifetime-and-cancellation-611)). A persistence failure is logged and never breaks the turn. This is why `GET /api/conversations?backend=hermes` and the sidebar now show Hermes history at all.
- **Hermes usage is captured too** (#595), by the same tee: a relayed `usage` event (`input_tokens`, `output_tokens`, `cost_usd`, `model` — the same shape the native path emits) is recorded to the usage store on turn completion, tagged with the conversation id. The cost is recorded **verbatim** from that event, never recomputed — the cost calculator only knows Anthropic pricing and would misprice a non-Anthropic upstream model (Hermes runs DeepSeek via Fireworks). A `usage` event with no `cost_usd` records a zero cost rather than a guess; a turn with no `usage` event writes no row; a malformed one (missing/wrong-typed model or token counts) is ignored. Conversation persistence and usage persistence are independent — neither gates the other. Since this is the same event shape and client handler (`web/chat/ask-stream.js`'s `data.type === 'usage'` branch) the native path already uses, the browser's session-cost display updates with **no client change**. The Agent backend has no equivalent — it isn't tee'd at all, so its turns stay invisible to the usage store. See [client-surfaces.md](../technical/client-surfaces.md) § "Hermes turn persistence" for the observer internals.
- `POST /api/hermes/resolve-persona` — **inbound**, called by Hermes's own Telegram front door (not by `/chat`), with the raw incoming text. Resolves `@tag` → persona, or inherits a persona from the message being replied to (via a persona-thread store), or resolves to nothing; returns `{persona_id, text, lifeos_context}` for Hermes to use when it makes its own upstream call. Requires the bearer configured as `LIFEOS_HERMES_BACKEND_TOKEN`; empty (unset) means the route always 503s.
- `POST /api/hermes/register-persona-message` — **inbound**, same auth. Anchors a Hermes-authored reply's message id to the persona that produced it, so a later reply-to-that-message inherits the same persona in `resolve-persona` above.

See [client-surfaces.md](../technical/client-surfaces.md) and [ADR-016](../../adr/016-voice-gateway-reverse-proxy.md).

### POST /api/search

Vector similarity search across indexed content.

**Request:**
```json
{
  "query": "budget planning",
  "filters": {
    "note_type": ["meeting"],
    "people": ["John"],
    "date_from": "2023-01-01",
    "date_to": "2023-01-31"
  },
  "top_k": 20
}
```

---

## Google Integration

### GET /api/calendar/upcoming

Get upcoming calendar events.

**Query Parameters:**
- `days` (int): Days to look ahead (default: 7)

### GET /api/calendar/search

Search calendar events.

**Query Parameters:**
- `q` (string): Search query
- `attendee` (string): Filter by attendee

### GET /api/calendar/meeting-prep

Get intelligent meeting preparation context for a date.

**Query Parameters:**
- `date` (string): Date in YYYY-MM-DD format (defaults to today)
- `include_all_day` (bool): Include all-day events (default: false)
- `max_related_notes` (int): Max notes per meeting (1-10, default: 4)

**Response:**
```json
{
  "date": "2023-02-03",
  "count": 5,
  "meetings": [
    {
      "event_id": "...",
      "title": "1:1 with Kevin",
      "start_time": "10:00 AM",
      "end_time": "10:30 AM",
      "attendees": ["kevin@example.com"],
      "related_notes": [
        {
          "title": "Kevin",
          "path": "/path/to/People/Kevin.md",
          "relevance": "attendee"
        },
        {
          "title": "1:1 with Kevin 20230127",
          "path": "/path/to/Meetings/...",
          "relevance": "past_meeting",
          "date": "2023-01-27"
        }
      ],
      "attachments": []
    }
  ]
}
```

### GET /api/gmail/search

Search emails.

**Query Parameters:**
- `q` (string): Search query
- `from` (string): Filter by sender
- `after` (string): After date
- `before` (string): Before date
- `account` (string): personal or work

### POST /api/gmail/drafts

Create a Gmail draft. LifeOS records the returned draft id in its send-safety ledger with the creation timestamp and optional turn identifier.

Header: `X-LifeOS-Turn-ID` (optional). Set the same opaque value on every Gmail draft/send call made during one agent turn to get exact same-turn enforcement.

**Request:**
```json
{
  "to": "recipient@example.com",
  "subject": "Subject line",
  "body": "Email content",
  "cc": "optional@example.com",
  "html": false,
  "account": "personal"
}
```

**Response:**
```json
{
  "draft_id": "draft-id",
  "gmail_url": "https://mail.google.com/..."
}
```

### POST /api/gmail/send

Send an existing draft by its `draft_id`. The exact draft is sent; there is no compose-and-send shortcut.

Safety gate: if the draft was created by `POST /api/gmail/drafts`, the send endpoint checks the draft ledger before sending. A send with the same `X-LifeOS-Turn-ID` as draft creation is refused with HTTP 409 regardless of age, as long as that turn-id record hasn't aged out of the ledger's row cap (`LIFEOS_GMAIL_DRAFT_LEDGER_MAX_TURN_TAGGED_ROWS`, default 10,000 — oldest-first eviction once exceeded, not time-based). Without an exact different turn id, LifeOS-created drafts are refused with HTTP 409 during `LIFEOS_GMAIL_DRAFT_SEND_COOLDOWN_SECONDS` (default 300 seconds). Draft ids not present in the ledger, such as drafts composed by hand in Gmail, send normally. If the ledger cannot be read, or if it shows signs of having lost data, the endpoint fails closed with HTTP 409.

**Request:**
```json
{
  "draft_id": "draft-id"
}
```
Query param: `account` (personal or work; must match where the draft was created).
Header: `X-LifeOS-Turn-ID` (optional). Use a different value from the draft-creation turn only after user confirmation.

**Response:**
```json
{
  "message_id": "sent-message-id",
  "source_account": "personal"
}
```

### GET /api/drive/search

Search Google Drive files.

**Query Parameters:**
- `q` (string): Search query
- `account` (string): personal or work

---

## Messaging Endpoints

### GET /api/imessage/search

Search iMessage/SMS history.

**Query Parameters:**
- `q` (string): Text content search
- `phone` (string): Filter by phone (E.164 format)
- `entity_id` (string): Filter by PersonEntity ID
- `after` (string): Messages after date (YYYY-MM-DD)
- `before` (string): Messages before date
- `direction` (string): sent or received
- `max_results` (int): Max results (1-200, default: 50)

### GET /api/imessage/conversations

Recent conversations summary.

### GET /api/imessage/statistics

Message database statistics.

### GET /api/imessage/person/{entity_id}

Messages with a specific person.

### GET /api/slack/status

Slack integration status and index statistics.

### POST /api/slack/search

Semantic search across Slack messages.

**Request:**
```json
{
  "query": "project update",
  "top_k": 20,
  "channel_id": "optional",
  "user_id": "optional"
}
```

### GET /api/slack/my-messages

Every message the user sent on a given day — DMs, group DMs, public/private channels, thread replies. Exact source-of-truth pull via Slack's `search.messages` (independent of the sync index). Day boundaries follow the user's Slack timezone. Requires the `search:read` user-token scope.

**Query parameters:** `date` (required, zero-padded `YYYY-MM-DD`), `user` (optional Slack user ID; defaults to the token owner).

**Response:**
```json
{
  "date": "2026-07-08",
  "user_id": "U12AB34CD",
  "total": 2,
  "truncated": false,
  "total_available": 2,
  "messages": [
    {
      "ts": "1751980000.000100",
      "timestamp": "2026-07-08T14:26:40+00:00",
      "text": "hello team",
      "channel_id": "C123",
      "channel_name": "general",
      "channel_type": "channel",
      "thread_ts": null,
      "permalink": "https://example.slack.com/archives/C123/p1751980000000100"
    }
  ]
}
```

`truncated: true` means the day exceeded the pagination cap (1,000 messages); `total_available` is Slack's full match count.

### GET /api/slack/conversations

List DMs and channels.

### POST /api/slack/sync

Trigger full or incremental sync.

### GET /api/slack/channels/{channel_id}/messages

Get live messages from a channel.

---

## CRM Endpoints

CRM endpoints (`/api/crm/*`) live in [api-crm.md](api-crm.md). Pulled out into a separate file because the CRM catalog is large enough on its own to warrant one.

---

## Performance Trace Endpoints

### GET /api/perf/traces

List recent performance traces with summary.

**Query Parameters:**
- `conversation_id` (string, optional): Filter by conversation
- `since` (string, optional): ISO timestamp to filter from
- `limit` (int): Max results (default: 50)

### GET /api/perf/traces/{trace_id}

Get a single trace with all spans.

### GET /api/perf/stats

Aggregate performance stats: avg/p50/p95/max per stage across recent traces.

**Query Parameters:**
- `since` (string, optional): ISO timestamp to filter from
- `limit` (int): Number of recent traces to aggregate (default: 100)

### GET /api/perf/routes

Rolling per-route request timing summary (#877) -- every HTTP request, not just chat turns. Backed by an in-memory rolling window (last 200 samples per route), process-local, reset on restart. See [Observability](../technical/observability.md#route-timing).

Returns `{"routes": [...], "count": N, "streams": [...], "stream_count": M}`. Each `routes` row: `method`, `route` (route template, e.g. `/api/crm/people/{person_id}`, never the raw path), `count`, `p50_ms`, `p95_ms`, `max_ms`, `slow_count`, `last_slow_at`. Sorted by `p95_ms` descending.

`streams` holds `text/event-stream` (SSE) responses separately -- `method`, `route`, `count`, `bytes` only, no duration or percentiles. An SSE connection's duration is however long the client kept it open (a browser tab, a live agent transcript), not a latency signal, so it's excluded from `routes` and the slow-request log entirely rather than dominating either. Sorted by `bytes` descending.

---

## Memories Endpoints

### POST /api/memories

Create a new memory.

**Request:**
```json
{
  "content": "Remember to follow up with Alex about the proposal",
  "category": "context"
}
```

### GET /api/memories

List all memories.

**Query Parameters:**
- `category` (string): Filter by category

### GET /api/memories/{id}

Get a specific memory.

### PUT /api/memories/{id}

Update an existing memory's content.

**Request:**
```json
{
  "content": "Updated memory text"
}
```

### DELETE /api/memories/{id}

Delete a memory.

### GET /api/memories/search/{query}

Search memories by keyword.

---

## Conversations Endpoints

### GET /api/conversations

List conversations for a persona (most recent first, up to 50).

**Query params:**
- `persona_id` (optional, default `"primary"`) — scope to a persona's threads (e.g. `?persona_id=fitness`). Omitting it returns the `primary` persona's threads, preserving web-chat behavior. Persona ids come from [`GET /api/personas`](#get-apipersonas).
- `backend` (optional, default unset) — scope to threads tagged with that backend (e.g. `?backend=hermes`). Unset returns threads from every backend, preserving behavior for every caller that predates this filter (#596).

**Response:**
```json
{
  "conversations": [
    {
      "id": "conv-uuid",
      "title": "Budget planning",
      "created_at": "2026-06-01T12:00:00",
      "updated_at": "2026-06-01T12:05:00",
      "message_count": 4,
      "persona_id": "primary",
      "backend": "lifeos"
    }
  ]
}
```

Conversations are tagged with `persona_id` when created via `POST /api/ask/stream` (default `primary`); rows created before this field existed backfill to `primary`. They're tagged with `backend` too (default `"lifeos"`; rows predating the column backfill to it) — see the `backend` field on [`POST /api/ask/stream`](#post-apiaskstream) above for how a thread ends up tagged `"hermes"` instead.

### POST /api/conversations

Create new conversation.

### GET /api/conversations/{id}

Get conversation with messages. Access by id is **not** persona-scoped — any valid id resolves regardless of its persona.

**Response:**
```json
{
  "id": "conv-uuid",
  "title": "Budget planning",
  "created_at": "2026-06-01T12:00:00",
  "updated_at": "2026-06-01T12:05:00",
  "messages": [
    {
      "id": "msg-uuid",
      "role": "user",
      "content": "What did we discuss last week?",
      "created_at": "2026-06-01T12:00:00",
      "sources": null,
      "routing": null
    }
  ],
  "pending_question": null,
  "active_turn": null
}
```

`role` is `user` or `assistant`.

`pending_question` is present only while a spawned **orchestrating-persona** session (e.g. `doctor`) started from this conversation is awaiting an answer — `{ "session_id": "...", "question": "...", "kind": "..." }` — and is `null`/absent otherwise. `kind` is `goal_approval` (a `[GOAL]` awaiting approval) or `followup` (a `[CLARIFY]`). The client renders an answer affordance when it's present and posts to `/answer` below.

`active_turn` (#611) is present only while a chat turn is running server-side for this conversation — native or Hermes-relayed alike — and `null` otherwise: `{ "turn_id": "...", "conversation_id": "...", "started_at": "2026-08-21T09:14:22" }`. Lets a client that reconnects mid-turn show "still working..." and reach the cancel affordance below. A completed turn's message carries no marker on it in the normal case; an *interrupted* one (cancelled, timed out, caught in a server shutdown, or a genuine stream error) has its content suffixed with a visible cut-off marker and `routing: {"truncated": true, "truncation_reason": "cancelled" | "deadline" | "shutdown" | "stream_error"}` — see [client-surfaces.md § Turn lifetime and cancellation](../technical/client-surfaces.md#turn-lifetime-and-cancellation-611).

### POST /api/conversations/{id}/cancel

Stop a chat turn in flight for this conversation (#611) — native or Hermes-relayed. Since a disconnect no longer stops a turn on its own, this is the explicit way to do it (also used internally when a new turn on the same conversation supersedes an old one still running).

**Response:**
```json
{ "ok": true, "cancelled": true }
```

`cancelled` is `false` when nothing was in flight (already finished, or never started) — not an error. **Errors:** `404` no such conversation.

### POST /api/conversations/{id}/answer

Answer a pending `[CLARIFY]`/`[GOAL]` from a spawned orchestrating-persona session (e.g. `doctor`) started from web/voice — without Telegram. Deposits the answer onto the session's existing open question (preserving its `kind`); the worker's normal tick then resumes the session via the **same** single resume path a Telegram reply uses (no second resume mechanism). Only meaningful while `GET /api/conversations/{id}` reports a `pending_question`.

**Request:**
```json
{ "answer": "yes" }
```

**Response:**
```json
{ "ok": true, "session_id": "sess-uuid", "status": "claimed" }
```

**Errors:** `400` empty answer · `404` no such conversation · `409` no spawned session linked, or no open question (never asked, already answered, or timed out).

### DELETE /api/conversations/{id}

Delete conversation.

### POST /api/conversations/{id}/ask

Ask a question within a conversation. Streams the response via SSE. Persists both the question and answer to the conversation, auto-generates a title on the first message.

**Request:**
```json
{
  "question": "What did we discuss last week?"
}
```

---

## Briefing Endpoints

### POST /api/briefing

Generate a comprehensive briefing about a person for meeting prep or relationship context.

**Request:**
```json
{
  "person_name": "John Smith",
  "email": "john@example.com"
}
```

### GET /api/briefing/{person_name}

Generate a briefing by person name (convenience GET endpoint).

**Query Parameters:**
- `email` (string, optional): Email for better entity resolution

---

## People Endpoints

### GET /api/people/person/{name}

Get a specific person by name.

### GET /api/people/search

Search people by name, email, alias, or display name. Matches sort exact
canonical-name matches first, then most recently seen.

**Query Parameters:**
- `q` (string, required): Search text
- `limit` (integer, optional, default 20, max 200): Max results to return

**Response:** adds a `total` field (count of all matching people, not just
the returned page) alongside the existing `people`/`count`/`query` fields.

### GET /api/people/list

List people ordered by most recently seen first, optionally filtered by
category.

**Query Parameters:**
- `limit` (integer, optional, default 50, max 500): Max results to return
- `category` (string, optional): Filter to this category

---

## Photos Endpoints

### GET /api/photos/stats

Get Apple Photos library statistics (named people, face detections, multi-person photos).

### GET /api/photos/people

List people recognized in Photos with match status to PersonEntity.

### GET /api/photos/person/{person_id}

Get photos containing a specific person.

### GET /api/photos/shared/{person_a_id}/{person_b_id}

Get photos where two people appear together.

### POST /api/photos/sync

Trigger Photos sync (matches faces to PersonEntity, creates interactions). **Errors:** `500` on a total sync failure (body still carries `success: false` and a top-level `error` key).

### GET /api/photos/thumbnail/{uuid}

Get a thumbnail image for a photo by UUID. Returns the image file from the Photos library derivatives folder. Returns 410 (Gone) with `X-iCloud-Only` header if the photo is in iCloud only.

### GET /api/photos/profile/{person_id}

Get a profile photo thumbnail for a person. Returns the most recent available photo thumbnail for use as an avatar. Also accepts `HEAD` (same status as `GET`, no body), so a client can check availability without downloading the image. Both the `200` and `404` responses carry `Cache-Control: public, max-age=3600`, so a client that already knows a person has no reachable photo doesn't need to ask again within the hour.

### GET /api/photos/open/{uuid}

Open a photo in Preview or Photos app. Tries the original file first, falls back to thumbnail, then the Photos app.

---

## Task Endpoints

Tasks can also be created, completed, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Task Management](task-management.md).

### POST /api/tasks

Create a task. Stored as an Obsidian Tasks-compatible markdown checkbox in the vault.

**Request:**
```json
{
  "description": "Call dentist",
  "context": "Personal",
  "priority": "high",
  "due_date": "2025-02-10",
  "tags": ["health"],
  "reminder_id": "optional-linked-reminder-uuid"
}
```

### GET /api/tasks

List/filter tasks.

**Query Parameters:**
- `status` (string): Filter by status (todo, done, in_progress, cancelled, deferred, blocked, urgent)
- `context` (string): Filter by context (Work, Personal, Finance, etc.)
- `tag` (string): Filter by tag
- `due_before` (string): YYYY-MM-DD, tasks due before this date
- `query` (string): Fuzzy text search across task descriptions

### GET /api/tasks/{id}

Get a specific task.

### PUT /api/tasks/{id}

Update a task (description, status, context, priority, due_date, tags).

### PUT /api/tasks/{id}/complete

Mark a task as done (adds done date automatically).

### DELETE /api/tasks/{id}

Delete a task.

---

## Scheduler & Telegram Endpoints

Schedules can also be created, edited, listed, and deleted via natural language through the chat interface (`POST /api/ask/stream`). See [Scheduler Guide](../../guides/scheduler.md).

> The legacy `/api/reminders*` endpoints remain mounted as deprecation-logged aliases over the same store.

### POST /api/scheduler

Create a schedule. Supports `schedule_type` of `once` (ISO datetime) or `cron`, and `action` of `notify` (static text), `prompt` (runs through the chat pipeline), `endpoint` (calls a LifeOS API endpoint), or `agent` (hands off to the agent worker via an `#agent` task, with `executor` = `local`/`cloud`/`cloud-haiku`/`cloud-sonnet`).

`bot` (optional) selects which Telegram bot delivers the notification. Valid values are `primary` and the names registered in `config/telegram_bots.json`; anything else returns **422** with the accepted names. Omit it, or send an empty string, for the primary bot.

### GET /api/scheduler

List all schedules.

### GET /api/scheduler/{id}

Get a specific schedule.

### PUT /api/scheduler/{id}

Update a schedule. An unrecognised `bot` returns **422** and leaves the schedule unchanged.

### DELETE /api/scheduler/{id}

Delete a schedule.

### POST /api/scheduler/{id}/trigger

Manually fire a schedule (for testing).

### POST /api/scheduler/send

Send an ad-hoc message via Telegram.

---

## Monarch Money Endpoints

Live financial data from Monarch Money. Monthly summaries are also synced to the vault.

### GET /api/monarch/session_status

Report cached Monarch session age and expiry-soon warnings, without making a network call — used by `/health/services` and dashboards to surface an impending re-auth requirement before the monthly sync hits a 401/525.

**Response:** `{exists: bool, age_days: float | null, status: "missing" | "expired" | "expiring_soon" | "ok", message: str}`

### GET /api/monarch/accounts

List all financial accounts with current balances.

**Response:** Array of `{name, type, subtype, balance, institution, last_updated}`

### GET /api/monarch/holdings

Investment holdings for one account.

**Query Parameters:**
- `account_id` (string, required)

**Response:** `{holdings: [...], count: int}` — empty list if the institution doesn't supply holdings through Plaid.

### GET /api/monarch/history

Daily balance snapshots for one account.

**Query Parameters:**
- `account_id` (string, required)
- `start_date` (string, optional): YYYY-MM-DD; omit for full history.

**Response:** `{history: [{date, balance}], count: int}`

### GET /api/monarch/transactions

Search recent transactions.

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 30 days ago)
- `end_date` (string): YYYY-MM-DD (default: today)
- `category` (string): Filter by category name
- `search` (string): Search merchant names
- `limit` (int): Max results (default: 500)
- `sort` (string, optional): `asc` (oldest first) or `desc` (newest first) by date. Omit for the default order (unchanged for existing callers).

**Response:** Array of `{date, merchant, category, amount, account, notes, pending}`

### GET /api/monarch/cashflow

Cashflow summary for a date range.

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 1st of current month)
- `end_date` (string): YYYY-MM-DD (default: today)

**Response:** `{summary: {total_income, total_expenses, savings, savings_rate}, categories: [{category, amount}]}`

### GET /api/monarch/budgets

Budget status (budgeted vs actual).

**Query Parameters:**
- `start_date` (string): YYYY-MM-DD (default: 1st of current month)
- `end_date` (string): YYYY-MM-DD (default: today)

**Response:** Array of `{category, budgeted, actual, remaining}`

---

## Job Queue Endpoints

### GET /api/jobs

List recent jobs with optional filtering.

**Query Parameters:**
- `status` (string): Filter by status (pending, running, completed, failed, cancelled)
- `type` (string): Filter by job type (e.g., reindex_vault, sync_source)
- `limit` (int): Max results (default: 50)

### GET /api/jobs/{job_id}

Get a specific job's status, result, and retry information.

### POST /api/jobs/{job_id}/cancel

Cancel a pending job. Only pending jobs can be cancelled.

---

## Admin Endpoints

### GET /health

Basic health check. Verifies API key and scheduler status. `api_key_configured` reports whether `ANTHROPIC_API_KEY` is set — a supported `LIFEOS_LLM_BACKEND=local` or `=remote` install with no Anthropic key still reports this as unset/degraded here; that's expected, not a sign the install is broken (#797).

### GET /health/full

Comprehensive health check. Tests all services (ChromaDB, vault search, calendar, Gmail, Drive, people, conversations, memories, iMessage) with per-service latency. The `local_llm` check reports `{"status": "not_in_use", "detail": "LIFEOS_LLM_BACKEND=... not in use"}` rather than probing reachability when `LIFEOS_LLM_BACKEND` isn't `local` — an honest "not applicable" instead of a stale "ok" (#797).

### GET /health/services

Real-time external service health. Returns per-service status, degradation events (last 24h), and critical issues.

### POST /api/admin/reindex

Enqueue a vault reindex job. Returns immediately with a job ID. Use `GET /api/jobs/{job_id}` to check progress. Prevents duplicates (won't enqueue if already pending/running).

**Response:**
```json
{
  "status": "started",
  "message": "Reindex enqueued. Check /api/jobs/{job_id} for progress.",
  "job_id": "abc123"
}
```

### POST /api/admin/reindex/sync

Trigger vault reindex (blocking). Use for initial setup only.

### GET /api/admin/calendar/status

Calendar indexer status.

### POST /api/admin/calendar/sync

Trigger calendar sync. A partial result (some calendar accounts synced, one failed) is a `200` with `status: "partial"`. **Errors:** `500` on a total sync failure (body still carries `status: "error"` and a top-level `error` key).

### POST /api/admin/calendar/start

Start calendar sync scheduler. Supports time-of-day scheduling (default: 8 AM, noon, 3 PM) or fixed interval.

### POST /api/admin/calendar/stop

Stop calendar sync scheduler.

### Maintenance Mode

#### POST /api/admin/maintenance

Enter maintenance mode. Suppresses CRITICAL alerts for the given duration (default: 4 hours). Use before operations that cause transient service unavailability.

**Query Parameters:**
- `duration_seconds` (int): Duration to suppress alerts (default: 14400)

#### DELETE /api/admin/maintenance

Exit maintenance mode early. Re-enables CRITICAL alerts.

### Usage Tracking

#### GET /api/admin/usage

Get usage summary with stats for 24h, 7d, 30d, and all-time. Includes daily cost breakdown for charting. Totals include Hermes-proxied (external-backend) turns alongside native ones (#595) — the usage store has no per-model or per-backend filtering, so any row written to it (native or relayed) counts.

---


## Related Documents

- [api-crm.md](api-crm.md) — `/api/crm/*` HTTP endpoints (split out from this file)
- [mcp-tools.md](mcp-tools.md) — MCP tool catalog (the canonical home — was previously duplicated here)
- [Data & Sync](../technical/data-and-sync.md) — Data sources and sync pipeline
- [Chat UI](chat-ui.md) — Chat interface product spec
- [Client Surfaces](../technical/client-surfaces.md) — HTTP consumers and breaking-change policy
- [CRM UI](crm-ui.md) — CRM index pointing at the four product sub-specs
- [Configuration](../../guides/configuration.md) — Env vars referenced by several endpoints (LIFEOS_USER_NAME, LIFEOS_WORK_DOMAIN, etc.)
- [Observability](../technical/observability.md#route-timing) — How `/api/perf/routes` is populated (`RouteTimingMiddleware`, the slow-request threshold)
