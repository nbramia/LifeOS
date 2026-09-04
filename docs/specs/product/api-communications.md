# Chat, Google & Messaging Endpoints

**Status:** Complete
**Owner:** API Gateway
**Last Updated:** 2026-09-04

Chat/search, Google integration (Calendar/Gmail/Drive), and messaging (iMessage/Slack) HTTP endpoints, with request/response shapes. Split out of [api-reference.md](api-reference.md) alongside its other adjacent catalogs (CRM, MCP tools, agent activity) because the combined file was over the product-spec size target.

For the SSE contract, persona/turn-context fields, and breaking-change policy these endpoints must honor, see [Client Surfaces](../technical/client-surfaces.md).

---

## Table of Contents

1. [Chat & Search Endpoints](#chat--search-endpoints)
2. [Google Integration](#google-integration)
3. [Messaging Endpoints](#messaging-endpoints)

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

- `persona_id` (optional) — selects a chat persona by id (see [`GET /api/personas`](#get-apipersonas)). The server applies the same system-prompt preamble the matching Telegram bot uses. Unknown ids return **400**. Omit it for the default (`primary`) persona. A new conversation created in this call is tagged with the persona so it can be filtered later (see [`GET /api/conversations`](api-reference.md#get-apiconversations)).
- `persona` (optional, internal) — raw preamble text used by the in-process Telegram client. Mutually exclusive with `persona_id` (sending both returns **400**); HTTP clients should use `persona_id`.
- `model_override` (optional) — pins the model for **this turn**. `"sonnet"` / `"opus"` (or a full model id) run the turn on that cloud model; `"gemma"` / `"local"` run it on the local llama-server; `"remote"` (#654) runs it on the configured paid OpenAI-compatible provider (e.g. Fireworks) — ignored, falling back to `"auto"`, unless that provider is fully configured (base URL, model, API key); `"auto"` or omitted uses the default orchestrator (Haiku) with escalation — which climbs only to non-API engines (`claude_code` / `codex` / `local`), so a cloud model or the remote provider is reached only by an explicit pick here or (cloud models only) a user-directed "escalate to opus" in the message. An explicit pick takes precedence over auto-escalation. Honored on the Anthropic backend; unknown values fall back to `auto`. Drives the web chat model picker.
- `backend` (optional) — tags a **newly created** conversation for sidebar filtering (see `?backend=` on [`GET /api/conversations`](api-reference.md#get-apiconversations)). This is the only thing it does: it never changes routing, model selection, or persona resolution. Omitted, it tags `"lifeos"` (today's behavior, unchanged). Through #641 the web client set this to `"hermes"` itself when diverting an orchestrating persona's turn from a Hermes-selected composer to this endpoint — that persona's spawn was LifeOS-native with no Hermes equivalent, so the turn landed here regardless of the selected backend. #642 gave Hermes its own way to drive an orchestrating persona and removed that diversion, so the web client no longer sends this field on any turn; it remains a generic, supported field on this endpoint for any other caller.
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
3. **Agentic loop** — everything else (including compose, tasks, reminders). Claude gets 22 tools and up to 5 rounds to fetch data and synthesize an answer. See `api/services/agent_tools.py::TOOL_DEFINITIONS` for the canonical list — count `len(TOOL_DEFINITIONS)` to re-derive this number.

**Agentic loop tools (22):**

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
| `manage_human_queue` | File, list, or resolve Human-queue cards — things only the operator can do (action: add/list/resolve) |
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

- `id` — pass as `persona_id` on [`POST /api/ask/stream`](#post-apiaskstream) and as the `persona_id` query param on [`GET /api/conversations`](api-reference.md#get-apiconversations).
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
- `POST /api/hermes/ask/stream` — text turn for the "Hermes" backend; same SSE and proxy behavior as the Agent backend above (same factory, `LIFEOS_HERMES_BACKEND_URL`), but the persona picker stays visible client-side, and the route resolves the selected persona (with `surface="hermes"`, so a persona with a Hermes-specific body — e.g. `doctor` — gets that instead of its plain one) and per-turn context and attaches them to the forwarded body as a `lifeos_context` envelope (`{schema_version, modality, persona: {id, label, preamble, voice_rules, orchestrates}, turn: {...}}`, `turn` a sibling of `persona` per [`GET /api/chat/turn-context`](#get-apichatturn-context)) — a cross-repo contract with `nbramia/hermes`. Rejects with 400 (before forwarding) on malformed JSON or an unknown `persona_id`. An orchestrating persona (e.g. `doctor`) reaches this route like any other, and `lifeos_context.persona.orchestrates` can be `true` (see client-surfaces.md for the full picture). `GET /api/hermes/status` → `{"available": bool, "configured": bool, "reachable": bool}` — `available` is true only when both `configured` (a URL is set) and `reachable` (a cached, short-timeout probe of that URL succeeded) hold, distinguishing "not set up" from "set up but down" (`GET /api/agent/status`'s `configured`/`reachable` fields, by contrast, both just mirror `available` — configuration alone, no reachability probe). See [client-surfaces.md](../technical/client-surfaces.md) § "The `lifeos_context` envelope" for the full schema.
- **Hermes turns are persisted and survive a client disconnect**, unlike the Agent backend: the route tees the relayed SSE bytes to the browser unchanged and, in parallel, reconstructs the turn from the `conversation_id` and `content` events it already emits (same shapes the native `POST /api/ask/stream` uses) — adopting the id Hermes minted, creating the conversation row (tagged `persona_id` + `backend: "hermes"`) on first sight of it, and storing the user question and assembled assistant reply as two messages. The upstream drain runs as a background pump independent of the browser connection, so a disconnect doesn't cut a Hermes turn short: it persists the *complete* reply, not just whatever had streamed so far. A turn whose upstream connection ends before a `done` event ever arrived gets a truncation marker + `routing.truncated` (see [client-surfaces.md](../technical/client-surfaces.md#turn-lifetime-and-cancellation-611)). A persistence failure is logged and never breaks the turn. `GET /api/conversations?backend=hermes` and the sidebar show Hermes history.
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

## Related Documents

- [api-reference.md](api-reference.md) — The other endpoint catalogs (Memories, Conversations, Tasks, Agent Board, Admin, etc.) and the split-catalog index
- [Client Surfaces](../technical/client-surfaces.md) — SSE contract, persona/turn-context shape, and breaking-change policy for the chat endpoints above
- [api-crm.md](api-crm.md) — CRM endpoints, split out the same way
- [mcp-tools.md](mcp-tools.md) — MCP tool catalog (Claude Code / Managed Agents)
