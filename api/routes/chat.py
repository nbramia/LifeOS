"""
Chat API endpoints with streaming support.
"""
import json
import asyncio
import logging
import re
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from api.services.vectorstore import VectorStore
from api.services.hybrid_search import HybridSearch
from api.services.synthesizer import get_synthesizer
from api.services.conversation_store import get_store, generate_title
from api.services.calendar import CalendarService
from api.services.drive import DriveService
from api.services.gmail import GmailService
from api.services.usage_store import get_usage_store
from api.services.chat_helpers import (
    expand_followup_query,
    classify_action_intent,
)
from api.services.time_parser import (
    parse_contextual_time,
    format_time_for_display,
    extract_time_from_query,
)
from config.settings import settings
from api.services.google_auth import GoogleAccount
from api.services.perf_trace import start_trace, trace_span, finish_trace, _current_trace
from api.services.agent_system_prompt import build_turn_context
from api.services.chat_turns import get_turn_registry, TRUNCATION_MARKER, truncation_routing
from api.services.journal_capture import (
    JOURNAL_PERSONA_ID,
    CaptureResult,
    JournalCaptureError,
    capture_fragment,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Parallel Source Fetching Helpers
# =============================================================================

async def _fetch_calendar_account(
    account_type: GoogleAccount,
    date_ref: str | None,
) -> tuple[str, list]:
    """Fetch calendar events from one account."""
    try:
        calendar = CalendarService(account_type)
        if date_ref:
            from datetime import datetime
            target_date = datetime.strptime(date_ref, "%Y-%m-%d")
            events = calendar.get_events_in_range(
                target_date,
                target_date + timedelta(days=1)
            )
        else:
            events = calendar.get_upcoming_events(max_results=10)
        return (account_type.value, events)
    except Exception as e:
        logger.warning(f"{account_type.value} calendar error: {e}")
        return (account_type.value, [])


async def _fetch_gmail_account(
    account_type: GoogleAccount,
    person_email: str | None,
    is_sent_to: bool,
    search_term: str | None,
) -> tuple[str, list]:
    """Fetch emails from one account."""
    try:
        gmail = GmailService(account_type)
        if person_email:
            if is_sent_to:
                messages = gmail.search(to_email=person_email, max_results=5, include_body=True)
            else:
                messages = gmail.search(from_email=person_email, max_results=5, include_body=True)
        elif search_term:
            messages = gmail.search(keywords=search_term, max_results=5)
        else:
            messages = gmail.search(max_results=5)
        return (account_type.value, messages)
    except Exception as e:
        logger.warning(f"{account_type.value} gmail error: {e}")
        return (account_type.value, [])


async def _fetch_drive_account(
    account_type: GoogleAccount,
    search_term: str | None,
) -> tuple[str, list, list]:
    """Fetch drive files from one account. Returns (account, name_matches, content_matches)."""
    if not search_term:
        return (account_type.value, [], [])
    try:
        drive = DriveService(account_type)
        name_files = drive.search(name=search_term, max_results=5)
        content_files = drive.search(full_text=search_term, max_results=5)
        return (account_type.value, name_files, content_files)
    except Exception as e:
        logger.warning(f"{account_type.value} drive error: {e}")
        return (account_type.value, [], [])


async def _fetch_slack(query: str, top_k: int = 10) -> list:
    """Fetch Slack messages."""
    try:
        from api.services.slack_indexer import get_slack_indexer
        from api.services.slack_integration import is_slack_enabled

        if is_slack_enabled():
            slack_indexer = get_slack_indexer()
            return slack_indexer.search(query=query, top_k=top_k)
    except Exception as e:
        logger.warning(f"Slack search error: {e}")
    return []


async def _fetch_vault(query: str, top_k: int, date_filter: str | None = None) -> list:
    """Fetch vault chunks using hybrid search."""
    try:
        hybrid_search = HybridSearch()
        if date_filter:
            vector_store = VectorStore()
            chunks = vector_store.search(query=query, top_k=top_k, filters={"modified_date": date_filter})
            if not chunks:
                chunks = hybrid_search.search(query=query, top_k=top_k)
        else:
            chunks = hybrid_search.search(query=query, top_k=top_k)
        return chunks
    except Exception as e:
        logger.warning(f"Vault search error: {e}")
        return []


async def _fetch_web(query: str) -> str:
    """Fetch web search results and format for context."""
    try:
        from api.services.web_search import search_web_with_synthesis
        synthesized, results = await search_web_with_synthesis(query)
        return synthesized
    except Exception as e:
        logger.warning(f"Web search error: {e}")
        return ""


async def extract_reminder_edit_params(query: str, reminder_name: str) -> Optional[dict]:
    """
    Extract new schedule parameters for editing an existing reminder.

    Args:
        query: User query like "change it to 7pm" or "move to tomorrow"
        reminder_name: Name of the reminder being edited

    Returns:
        dict with schedule_type, schedule_value, display_time or None
    """
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo(settings.timezone)
    now = datetime.now(eastern)

    # Try to parse time from query
    time_expr = extract_time_from_query(query)
    parsed_time = None
    if time_expr:
        parsed_time = parse_contextual_time(time_expr, now)

    if parsed_time:
        return {
            "schedule_type": "once",
            "schedule_value": parsed_time.isoformat(),
            "display_time": format_time_for_display(parsed_time, now),
        }

    # If simple parsing failed, use Claude for complex expressions
    extraction_prompt = f"""Extract the new time from this reminder edit request.

Current date/time: {now.strftime("%A, %B %d, %Y at %I:%M %p")} ET
Reminder being edited: {reminder_name}
User request: {query}

Return ONLY a JSON object with:
- "schedule_type": "once" for one-time
- "schedule_value": ISO datetime (e.g., "2026-02-08T19:00:00-05:00")

Example: {{"schedule_type": "once", "schedule_value": "2026-02-08T19:00:00-05:00"}}"""

    try:
        synthesizer = get_synthesizer()
        response_text = await synthesizer.get_response(
            extraction_prompt,
            max_tokens=256,
            model_tier="haiku"
        )

        json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if json_match:
            params = json.loads(json_match.group())
            if params.get("schedule_value"):
                try:
                    trigger_dt = datetime.fromisoformat(params["schedule_value"])
                    params["display_time"] = format_time_for_display(trigger_dt, now)
                except (ValueError, TypeError):
                    params["display_time"] = params["schedule_value"]
                return params
    except Exception as e:
        logger.error(f"Failed to extract edit params: {e}")

    return None


router = APIRouter(prefix="/api", tags=["chat"])


class PersonaInfoResponse(BaseModel):
    """A single HTTP-visible chat persona (no secrets)."""
    id: str
    label: str
    capabilities: list[str] = []
    orchestrates: bool = False


class PersonasResponse(BaseModel):
    """Response for the persona discovery endpoint."""
    personas: list[PersonaInfoResponse]


@router.get("/personas", response_model=PersonasResponse)
async def list_personas():
    """List chat personas available to HTTP clients (web, voice/whisper-relay).

    Returns the primary persona plus each configured specialized bot. Adding a
    registry entry + its token env var surfaces a new persona after restart with
    no code change. Only the primary and orchestrating bots advertise
    handoff/agent capabilities. ``orchestrates`` (#643) is the server's own
    `settings.persona_orchestrates()` verdict, so clients no longer have to
    infer it from `capabilities` alone (which look identical for `primary` and
    an orchestrating bot like `doctor`).
    """
    return PersonasResponse(
        personas=[
            PersonaInfoResponse(
                id=p.id, label=p.label, capabilities=p.capabilities,
                orchestrates=p.orchestrates,
            )
            for p in settings.list_http_personas()
        ]
    )


@router.get("/chat/config")
async def chat_config():
    """Client-facing /chat defaults. `default_voice` drives the web client's
    default input mode when there's no ?mode= param or stored preference.

    `secure_url` is the HTTPS origin (TAILNET_HTTPS_URL) the web client offers as
    a one-tap escape when the mic is blocked by an insecure context (#516).
    Trailing slash stripped so clients can append a path directly; "" when unset,
    in which case the client just reports the insecure context without a link.

    `remote_model_available`/`remote_model_label` (#654) tell the model
    picker whether to show its "Remote" option — a paid OpenAI-compatible
    provider (e.g. Fireworks) that only exists as an explicit per-turn pick,
    never auto-escalated to. Unconfigured (no base URL/model/key) means
    `remote_model_available` is False and `remote_model_label` is "" — the
    picker hides the option and every existing path is unaffected.
    """
    return {
        "default_voice": bool(settings.chat_default_voice),
        "secure_url": (settings.tailnet_https_url or "").rstrip("/"),
        "remote_model_available": settings.remote_llm_configured,
        "remote_model_label": settings.remote_llm_label if settings.remote_llm_configured else "",
    }


class TagCount(BaseModel):
    """A task tag with its usage count."""
    tag: str
    count: int


class TurnContextResponse(BaseModel):
    """Response for the per-turn context endpoint (#591, extended by #610, #613)."""
    current_datetime: str
    current_datetime_iso: str
    timezone: str
    time_resolution_instruction: str
    personal_context: str
    existing_tags: list[TagCount]
    tags_instruction: str
    session_cost_usd: float
    session_turn_count: int
    session_input_tokens: int
    session_output_tokens: int
    session_cost_is_lower_bound: bool


@router.get("/chat/turn-context", response_model=TurnContextResponse)
async def turn_context(persona_id: str = "primary", modality: str = "text", conversation_id: Optional[str] = None):
    """Read-only per-turn context: current date/time, timezone, the
    relative-time-resolution instruction, the persona-scoped personal-context
    block, existing task tags with usage counts, and (#610) session-to-date
    cost/token totals for `conversation_id`.

    This is the same computation `build_system_prompt` folds into the native
    system prompt, exported as structured JSON (no Anthropic content-block
    dependency) so any MCP client or the Hermes backend can pull it at the
    start of a turn without a LifeOS-specific integration (#591). It never
    creates, mutates, or persists anything.

    `modality` is accepted for shape symmetry with `/api/ask/stream` but
    doesn't currently change any field here — voice-specific material
    (a persona's spoken-style rules) lives in `persona`, not `turn`.

    `conversation_id` is optional — omitted (or a conversation with no
    recorded usage yet) reports the session-cost fields present and zero
    rather than erroring or omitting them. `session_cost_is_lower_bound`
    says whether `session_cost_usd` is a floor rather than an exact total
    — see `build_turn_context()`'s docstring.

    400 if `persona_id` isn't a known persona.
    """
    if settings.resolve_persona(persona_id) is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona_id: {persona_id!r}")
    return build_turn_context(persona_id, conversation_id)


# Attachment configuration
ALLOWED_MEDIA_TYPES = {
    # Images - 5MB each
    "image/png": 5 * 1024 * 1024,
    "image/jpeg": 5 * 1024 * 1024,
    "image/jpg": 5 * 1024 * 1024,
    "image/gif": 5 * 1024 * 1024,
    "image/webp": 5 * 1024 * 1024,
    # PDFs - 10MB
    "application/pdf": 10 * 1024 * 1024,
    # Text files - 1MB
    "text/plain": 1 * 1024 * 1024,
    "text/markdown": 1 * 1024 * 1024,
    "text/csv": 1 * 1024 * 1024,
    "application/json": 1 * 1024 * 1024,
}
MAX_ATTACHMENTS = 5
MAX_TOTAL_SIZE = 20 * 1024 * 1024  # 20MB


class Attachment(BaseModel):
    """Single attachment in a message."""
    filename: str
    media_type: str
    data: str  # Base64 encoded content

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, v):
        if v not in ALLOWED_MEDIA_TYPES:
            raise ValueError(f"Unsupported file type: {v}. Allowed types: images (PNG, JPG, GIF, WebP), PDFs, and text files (TXT, MD, CSV, JSON)")
        return v

    def get_size_bytes(self) -> int:
        """Calculate the size of the decoded data."""
        # Base64 encoding adds ~33% overhead
        return len(self.data) * 3 // 4

    def validate_size(self):
        """Validate the attachment size against limits."""
        size = self.get_size_bytes()
        max_size = ALLOWED_MEDIA_TYPES.get(self.media_type, 0)
        if size > max_size:
            max_mb = max_size / (1024 * 1024)
            actual_mb = size / (1024 * 1024)
            raise ValueError(
                f"File '{self.filename}' ({actual_mb:.1f}MB) exceeds "
                f"limit for {self.media_type} ({max_mb:.0f}MB)"
            )


class AskStreamRequest(BaseModel):
    """Request for streaming ask endpoint."""
    question: str
    include_sources: bool = True
    conversation_id: Optional[str] = None
    attachments: Optional[list[Attachment]] = None
    persona: Optional[str] = None
    # HTTP clients (web, voice) select a persona by id; the server resolves it to
    # the same preamble the matching Telegram bot uses. The raw `persona` field
    # above is the internal Telegram path (full preamble text); the two are
    # mutually exclusive (sending both is a 400 in the route handler).
    persona_id: Optional[str] = None
    # Per-turn chat model picker. "auto"/None = the configured orchestrator
    # (Haiku) with escalation; "sonnet"/"opus" (or a full model id) pins this
    # turn to that cloud model; "gemma"/"local" runs this turn on the local
    # llama-server; "claude_code" hands the turn off to a background Claude Code
    # worker session instead of answering inline (see the handoff short-circuit
    # in stream_response). The model picks are honored only on the Anthropic
    # backend (the local backend is already local); the "claude_code" handoff
    # works on any backend. Unknown values fall back to auto.
    model_override: Optional[str] = None
    # Response modality. "voice" tells the orchestrator this turn will be read
    # aloud, so the selected persona's `voice` rules are appended to the system
    # prompt; None/"text" is a normal typed turn. Set by the voice gateway
    # (whisper-relay) on spoken turns; omitted for text.
    modality: Optional[str] = None
    # Text backend the client had selected, used SOLELY to tag a newly created
    # conversation for sidebar filtering (#596). Through #641 the web client
    # sent this when diverting an orchestrating persona's turn here from a
    # Hermes-selected composer, because this handler was where its spawn path
    # lived and Hermes had no equivalent; #642 gave Hermes its own way to
    # drive that persona and removed the divert, so the web client no longer
    # sends this field on any turn. Kept as a generic, supported field on
    # this request for any other caller. Never used to route, resolve a
    # persona, or pick a model; omitted (the default) reproduces today's
    # tagging ("lifeos") exactly.
    backend: Optional[str] = None
    # Opaque, client-generated turn key (#611 review). `conversation_id`
    # alone can't cancel a turn before its first SSE frame ever arrives —
    # the request that started it carries `conversation_id: None` for a
    # brand-new conversation, and the real id only shows up in the
    # `conversation_id` SSE event. That's exactly the window a voice
    # barge-in on a first turn falls into, so `POST /api/chat/cancel`
    # accepts this key too. The client mints it (any opaque string works —
    # not required to be a UUID) before sending; the server never generates
    # or returns one. Optional and additive: omitting it changes nothing.
    client_turn_id: Optional[str] = None

    @field_validator("client_turn_id")
    @classmethod
    def validate_client_turn_id(cls, v):
        # Opaque bounded string, not a trusted identifier — bounded to stop
        # something huge or control-character-laden from reaching the
        # in-memory registry key space. Not assumed to be a UUID: whatever
        # scheme a client uses to generate a locally-unique key is fine.
        if v is None:
            return v
        if len(v) > 200:
            raise ValueError(f"client_turn_id exceeds 200 chars (got {len(v)})")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in v):
            raise ValueError("client_turn_id must not contain control characters")
        return v

    @field_validator("persona")
    @classmethod
    def validate_persona(cls, v):
        # Per-bot preamble injected into the system prompt. Bounded as cheap
        # defense-in-depth — personas are short profiles, not documents.
        if v is not None and len(v) > 8000:
            raise ValueError(f"persona exceeds 8000 chars (got {len(v)})")
        return v

    @field_validator("attachments")
    @classmethod
    def validate_attachments(cls, v):
        if v is None:
            return v
        if len(v) > MAX_ATTACHMENTS:
            raise ValueError(f"Maximum {MAX_ATTACHMENTS} attachments allowed, got {len(v)}")

        # Validate each attachment's size
        total_size = 0
        for att in v:
            att.validate_size()
            total_size += att.get_size_bytes()

        if total_size > MAX_TOTAL_SIZE:
            total_mb = total_size / (1024 * 1024)
            max_mb = MAX_TOTAL_SIZE / (1024 * 1024)
            raise ValueError(f"Total attachment size ({total_mb:.1f}MB) exceeds limit ({max_mb:.0f}MB)")

        return v


async def _handle_agent_slash(stripped: str, conversation_id, store):
    """Stream the SSE response for a `/agent [local|claude] <task>` chat command.

    Operator agent spawn from web chat (#235), calling the same
    `create_operator_session` entry point as the Telegram `/agent` command.
    Web has no inline reply mechanism yet (added in Phase 3 / #236), so an
    ambiguous auto-route asks the user to re-run with an explicit model.
    """
    rest = stripped[len("/agent"):].strip()
    explicit = None
    parts = rest.split(maxsplit=1)
    if parts and parts[0].lower() in ("local", "claude"):
        explicit = parts[0].lower()
        task = parts[1].strip() if len(parts) > 1 else ""
    else:
        task = rest

    yield f"data: {json.dumps({'type': 'routing', 'sources': ['agent'], 'reasoning': 'Operator agent spawn', 'latency_ms': 0})}\n\n"

    if not task:
        msg = "Usage: `/agent [local|claude] <task>` — e.g. `/agent claude refactor the parser`."
    else:
        sess_store = None
        try:
            from api.services.agent_worker.operator_spawn import create_operator_session
            from api.services.agent_worker.session_store import SessionStore
            sess_store = SessionStore()
            result = await asyncio.to_thread(
                create_operator_session, sess_store, task, explicit_routing=explicit,
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:200]}

        if not result.get("ok"):
            msg = f"Couldn't spawn agent: {result.get('error')}"
        elif result.get("needs_routing"):
            # No inline routing-clarification flow on the web surface — tear the
            # parked session down so it doesn't linger as a blocked thread.
            if sess_store is not None:
                try:
                    sess_store.delete_session(result["session_id"])
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
            msg = (
                "I couldn't tell whether to use the local or cloud model — "
                "re-run as `/agent local <task>` or `/agent claude <task>`."
            )
        else:
            label = "Claude (cloud)" if result["routing"] == "claude" else "local Gemma"
            msg = (
                f"🤖 Spawned {label} agent: {task[:120]}\n\n"
                f"Track it on the Agents page; the result appears there when it's done."
            )

    for chunk in msg:
        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
        await asyncio.sleep(0.002)
    store.add_message(conversation_id, "assistant", msg)
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def resolve_effective_persona_id(persona_id: Optional[str], persona: Optional[str]) -> Optional[str]:
    """The exact persona-shape validation and effective-id derivation
    `ask_stream()` performs for its `_effective_pid` — shared with the
    Hermes proxy's journal-capture gate (`api/routes/hermes_proxy.py`,
    #685 adversarial-review follow-up) so a raw-`persona` preamble turn
    (`chat_via_api()`'s and the ring ingest's shape, not just a
    `persona_id`-selected one) resolves identically on both surfaces —
    approximating it as `persona_id or "primary"` alone, as the proxy's
    envelope-building code does, silently misses a raw-preamble journal
    turn entirely.

    Raises `HTTPException(400)` for the same two malformed shapes
    `ask_stream()`'s own persona resolution rejects: `persona_id` and
    `persona` both given, or a `persona_id` that doesn't resolve to any
    registered persona (including `""`, which resolves to nothing).
    Returns `persona_id` once validated; otherwise, when only `persona` (a
    raw preamble) was given, the registered bot name whose preamble matches
    it verbatim — `None` if neither field is given or no bot matches.
    """
    if persona_id is not None:
        if persona is not None:
            raise HTTPException(
                status_code=400,
                detail="Provide either persona_id or persona, not both",
            )
        if settings.resolve_persona(persona_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown persona_id: {persona_id!r}",
            )
        return persona_id
    if persona:
        return next((b.name for b in settings.telegram_bots if b.persona == persona), None)
    return None


def journal_capture_gate(persona_id: Optional[str], text: str) -> Optional[CaptureResult]:
    """#674's deterministic journal capture, as a single gate shared by every
    surface that can drive a journal-persona turn — the native path below and
    the Hermes proxy relay (`api/routes/hermes_proxy.py`, #685) — so a third
    surface can't recreate the gap this closes: the journal persona used to be
    told to call `lifeos_vault_write` itself, a tool that doesn't exist in
    either agentic loop, so every fragment was silently lost while the reply
    still read like a successful capture.

    No-op (returns `None`) for any persona other than `journal`. Must be
    called BEFORE the caller's SSE stream opens / before the turn is handed
    to a downstream backend — a capture failure is then a clean HTTP 500
    instead of a mid-stream error: no caller can report success for a
    fragment that was never written, and a delivery's idempotency key (the
    ring ingest, `api/routes/journal_ingest.py`) stays unburned so a retry
    can still land.

    Raises `HTTPException(500)` on a write failure. Deliberately not
    `str(e)` in the detail — it is surfaced to the user (Telegram renders it
    verbatim) and must never quote the fragment.
    """
    if persona_id != JOURNAL_PERSONA_ID:
        return None
    try:
        return capture_fragment(text)
    except JournalCaptureError:
        raise HTTPException(
            status_code=500,
            detail="journal capture failed: the fragment was not written",
        )


@router.post("/ask/stream")
async def ask_stream(request: AskStreamRequest):
    """
    Ask a question with streaming response.

    Returns Server-Sent Events (SSE) with:
    - type: "content" - streamed answer content
    - type: "sources" - list of source documents
    - type: "done" - completion signal
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    # Resolve persona BEFORE the SSE stream opens so an invalid selection is a
    # clean 400 rather than a mid-stream error. `persona` (raw preamble text) is
    # the internal Telegram path; `persona_id` is the HTTP-client path resolved
    # against the same registry. They are mutually exclusive.
    persona_preamble = request.persona or ""
    new_conversation_persona_id = "primary"
    if request.persona_id is not None:
        if request.persona is not None:
            raise HTTPException(
                status_code=400,
                detail="Provide either persona_id or persona, not both",
            )
        resolved = settings.resolve_persona(request.persona_id)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown persona_id: {request.persona_id!r}",
            )
        persona_preamble = resolved
        new_conversation_persona_id = request.persona_id

    # Voice turns get the selected persona's spoken-response rules appended to the
    # system prompt; text turns get none. (Set by the voice gateway via `modality`.)
    # Guard on persona_id: voice rules are keyed by id, so the raw-`persona` path
    # (Telegram preamble text, no id) gets none rather than misapplying primary's.
    voice_rules = (
        settings.persona_voice(request.persona_id)
        if (request.modality or "").strip().lower() == "voice" and request.persona_id
        else ()
    )

    # Personal-context block: the therapist persona pre-resolves the user's people
    # from config so it can target sessions/messages without a lookup round. Works
    # on the persona_id path and the raw-`persona` (Telegram) path via a reverse
    # lookup of the preamble back to a bot name. By the time this runs, the block
    # above has already validated persona_id/persona's shape, so
    # resolve_effective_persona_id() below can't actually raise here — it's
    # reused rather than re-inlined so the Hermes proxy's journal-capture gate
    # (#685) shares the exact same derivation instead of approximating it.
    _effective_pid = resolve_effective_persona_id(request.persona_id, request.persona)
    personal_context = settings.personal_context(_effective_pid or "")

    # #674 (shared with the Hermes proxy via `journal_capture_gate`, #685):
    # deterministic journal capture, done here in code, not left to the
    # model. Done BEFORE the SSE stream opens (like the persona resolution
    # above) — see the helper's docstring for why. Covers every journal
    # surface at once — the Telegram bot and the ingest endpoint both arrive
    # here through `chat_via_api`'s raw-`persona` path, `/chat` and voice
    # through `persona_id`.
    journal_capture = journal_capture_gate(_effective_pid, request.question)

    # #611: the turn's lifetime is owned by the server from here on, not by
    # this SSE connection — every modality survives the client leaving
    # (#616 lifted the voice-only exception; see ChatTurn.reader() in
    # chat_turns.py). `modality` is still recorded on the turn for parity
    # with the request and for other voice-specific behavior elsewhere
    # (e.g. spoken-style system-prompt rules, below).
    _modality = (request.modality or "").strip().lower() or "text"
    # Supersede (#611): a new turn on a conversation OR client_turn_id that
    # already has one in flight cancels the old one first — asking again is
    # itself a stop gesture, and it prevents a stale reply landing after a
    # newer question. Checking client_turn_id too closes the same gap the
    # explicit cancel endpoint closes (#611 review): a reused/duplicate key
    # supersedes rather than silently colliding with an unrelated turn.
    if request.conversation_id:
        get_turn_registry().cancel_conversation(request.conversation_id)
    if request.client_turn_id:
        get_turn_registry().cancel_by_client_turn_id(request.client_turn_id)
    turn = get_turn_registry().create(
        conversation_id=request.conversation_id,
        modality=_modality,
        client_turn_id=request.client_turn_id,
    )

    async def _content(text: str) -> None:
        """Emit a `content` SSE frame AND accumulate it into `partial_text`
        (#611) — the running "what has the user actually seen so far" used
        to persist an honest partial if this turn is interrupted. Reset to
        "" on `self_correction`, mirroring `ask-stream.js`'s `fullContent`:
        `agent_result.full_text` only gains a round's text at round end, so
        it is NOT what was actually streamed and isn't safe to use here."""
        nonlocal partial_text
        partial_text += text
        await turn.emit(f"data: {json.dumps({'type': 'content', 'content': text})}\n\n")

    partial_text = ""
    # #615: live reference to the agent loop's own AgentResult, stashed from
    # the `turn_state` event so a cancel/deadline handler can read accrued
    # usage without waiting for the terminal `result` event, which a
    # cancelled turn never reaches. Stays None for a fake loop that doesn't
    # emit `turn_state` (e.g. the ones in tests/test_persona_api.py) --
    # `usage_recorded` guards against recording it twice if the turn does
    # reach the normal end-of-turn usage write below. "Accrued" means as of
    # the last round boundary, not as of the last streamed token -- see the
    # comment in the CancelledError handler below for what that leaves out.
    live_result = None
    usage_recorded = False

    async def _run_turn():
        nonlocal partial_text, live_result, usage_recorded
        # Pre-initialized outside the try (a plain assignment can't itself be
        # interrupted) so the except/finally clauses below always have a
        # defined `conversation_id`, even if a cancellation lands before the
        # branch that would otherwise set it for a brand-new conversation.
        conversation_id = turn.conversation_id
        try:
            # Get or create conversation
            store = get_store()

            if not conversation_id:
                # Create new conversation, tagged with the selected persona so
                # persona-scoped listing (e.g. the voice sidebar) can filter it,
                # and with the selected backend (default "lifeos") — the
                # `backend` field's only purpose (#596; no longer set by the
                # first-party client since #642 removed its one use case, the
                # Hermes-orchestrating-persona divert, but still a generic,
                # supported field on this request for any other caller).
                conv = store.create_conversation(
                    persona_id=new_conversation_persona_id,
                    backend=request.backend or "lifeos",
                )
                conversation_id = conv.id
                # #611: bind the turn to its conversation id now that one
                # exists, so it becomes cancellable/supersedable by that id.
                # (An id supplied in the request was already bound at
                # creation, in ask_stream(), before this task started.)
                get_turn_registry().bind(turn, conversation_id)
                # Generate title from question
                title = generate_title(request.question)
                store.update_title(conversation_id, title)
                print(f"Created new conversation: {conversation_id} - {title}")

            # Start performance trace
            start_trace(conversation_id, request.question)

            # Send conversation ID to client
            await turn.emit(f"data: {json.dumps({'type': 'conversation_id', 'conversation_id': conversation_id})}\n\n")

            # #674: tell the caller the fragment is on disk. This is the only
            # thing that makes "logged" an honest status downstream — the ring
            # ingest endpoint requires this event before it reports success or
            # burns the delivery's idempotency key, rather than inferring
            # capture from "the pipeline returned without raising", which is
            # exactly the inference that hid this bug. Additive event type;
            # clients that don't know it ignore it.
            if journal_capture is not None:
                await turn.emit(
                    "data: " + json.dumps({
                        "type": "journal_capture",
                        "path": journal_capture.path,
                        "created": journal_capture.created,
                    }) + "\n\n"
                )

            # Save user message
            store.add_message(conversation_id, "user", request.question)

            # `/agent [local|claude] <task>` — spawn an operator agent on demand
            # (#235). Equivalent affordance to Telegram's /agent command, calling
            # the same create_operator_session entry point.
            _stripped = request.question.strip()
            if _stripped.lower() == "/agent" or _stripped.lower().startswith("/agent "):
                async for _ev in _handle_agent_slash(_stripped, conversation_id, store):
                    await turn.emit(_ev)
                return

            # Model picker → "Claude Code": the toolbar model dropdown can pin a
            # turn to the Claude Code engine. Unlike the cloud/local model picks
            # (resolved further down), this isn't an LLM turn at all — it routes
            # the whole message to a background Claude Code worker session, the
            # same handoff the orchestrator emits for an inferred "use claude
            # code" directive (#305b). An explicit per-turn choice, so it precedes
            # numeric selection, classification, and the agentic loop, and works
            # regardless of the chat LLM backend (the handoff spawns a CLI worker,
            # not an LLM turn). The frontend treats an explicit pick as its own
            # handoff opt-in, bypassing the persona capability gate (#359).
            if (request.model_override or "").strip().lower() == "claude_code":
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': ['claude_code'], 'reasoning': 'Model picker → Claude Code', 'latency_ms': 0})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'claude_intent', 'task': request.question, 'engine': 'claude_code'})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            # Orchestrating personas (e.g. doctor) run as a Claude Code session,
            # not the inline orchestrator (which has no shell/git/filesystem and
            # would make the persona's "I am a Claude Code session" framing false).
            # Mirror the Telegram orchestration path: spawn with the persona as the
            # prompt + the user's message, in the canonical LifeOS checkout. Gated on
            # persona_id (the web/voice surfaces); Telegram's own orchestration path
            # handles its bots, so this never double-spawns.
            if request.persona_id and settings.persona_orchestrates(request.persona_id):
                import os
                from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
                from api.services.agent_worker.session_store import SessionStore
                working_dir = os.path.expanduser(os.path.join(str(settings.code_dir), "LifeOS"))
                spawn_prompt = (
                    f"{persona_preamble}\n\n---\n\nThe user just sent this via the "
                    f"{request.persona_id} surface:\n\n{request.question}"
                )
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': ['claude_code'], 'reasoning': f'Orchestrating persona ({request.persona_id}) → Claude Code session', 'latency_ms': 0})}\n\n")
                try:
                    _spawn = await asyncio.to_thread(
                        spawn_claude_code_session, SessionStore(), spawn_prompt,
                        working_dir=working_dir,
                        plan_mode=False,  # the doctor pipeline plans itself; web has no CLI plan-mode resume path
                        chat_id=getattr(settings, "telegram_chat_id", "") or None,
                        bot=request.persona_id,  # route [NOTIFY]/[CLARIFY]/completion via this bot (parity with Telegram)
                    )
                except Exception:  # noqa: BLE001 — never end the SSE without a `done`
                    logger.warning("orchestrating-persona spawn failed", exc_info=True)
                    _spawn = {"ok": False, "error": "could not start the session"}
                if _spawn.get("ok"):
                    _sid = _spawn.get("session_id", "")
                    # Link the conversation to the spawned session so a later
                    # [CLARIFY]/[GOAL] can be answered from this web/voice thread
                    # (no Telegram needed) via POST /api/conversations/{id}/answer
                    # → the session-keyed deposit → the worker's existing resume
                    # path (#403). Best-effort: a link failure only loses the
                    # web round-trip, not the session (Telegram parity still works).
                    try:
                        store.set_agent_session_id(conversation_id, _sid)
                    except Exception:  # noqa: BLE001
                        logger.warning("could not link conversation to spawned session", exc_info=True)
                    ack = (
                        f"🩺 On it — running as a Claude Code session in the background "
                        f"(session `{_sid[:12]}`). If I need to clarify the goal, I'll ask "
                        f"right here — reply to answer. I'll also follow up via Telegram and on the /agents page."
                    )
                else:
                    ack = f"⚠️ Couldn't start the session: {_spawn.get('error', 'spawn failed')}"
                await turn.emit(f"data: {json.dumps({'type': 'content', 'content': ack})}\n\n")
                store.add_message(conversation_id, "assistant", ack, routing={
                    "reasoning": f"orchestrating persona {request.persona_id} → claude_code",
                    "sources": ["claude_code"],
                })
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            # Get conversation history for context in follow-up questions
            conversation_history = store.get_messages(conversation_id, limit=10)
            # Exclude current message to avoid duplication
            if conversation_history and conversation_history[-1].role == "user" and conversation_history[-1].content == request.question:
                conversation_history = conversation_history[:-1]

            # Check for pending numeric selection (Fix #3: handle "1", "2" responses)
            from api.services.conversation_context import extract_context_from_history
            conv_context = extract_context_from_history(conversation_history) if conversation_history else None

            if conv_context and conv_context.has_pending_selection() and request.question.strip().isdigit():
                idx = int(request.question.strip()) - 1  # Convert to 0-based
                if 0 <= idx < len(conv_context.pending_selection_items):
                    item_id = conv_context.pending_selection_items[idx]
                    action = conv_context.pending_selection_action
                    selection_type = conv_context.pending_selection_type

                    if selection_type == "reminder":
                        from api.services.reminder_store import get_reminder_store
                        reminder_store = get_reminder_store()
                        reminder = reminder_store.get(item_id)

                        if reminder:
                            if action == "delete":
                                reminder_store.delete(item_id)
                                response_text = f"I've deleted the reminder **\"{reminder.name}\"**."
                            elif action == "edit":
                                edit_params = await extract_reminder_edit_params(request.question, reminder.name)
                                if edit_params:
                                    reminder_store.update(
                                        item_id,
                                        schedule_type=edit_params.get("schedule_type", reminder.schedule_type),
                                        schedule_value=edit_params["schedule_value"],
                                    )
                                    display_time = edit_params.get("display_time", edit_params["schedule_value"])
                                    response_text = f"I've updated **\"{reminder.name}\"** to {display_time}."
                                else:
                                    # User selected the reminder but we need the new time
                                    response_text = f"Got it, you want to change **\"{reminder.name}\"**. What time should I set it to?"
                                    # Store selected reminder for next turn
                                    routing_metadata = {
                                        "sources": ["reminder"],
                                        "reasoning": "Awaiting new time for edit",
                                        "created_reminder": {"id": reminder.id, "name": reminder.name},
                                    }
                                    for chunk in response_text:
                                        await _content(chunk)
                                        await asyncio.sleep(0.005)
                                    store.add_message(conversation_id, "assistant", response_text, routing=routing_metadata)
                                    # Persisted in full above — clear the accumulator so a
                                    # cancellation on the way out (e.g. during the emit/return
                                    # below) can't re-persist it as if it were only a partial
                                    # (#611: would otherwise double-write this message).
                                    partial_text = ""
                                    await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                                    return
                            else:
                                response_text = f"Selected reminder: **\"{reminder.name}\"**"

                            for chunk in response_text:
                                await _content(chunk)
                                await asyncio.sleep(0.005)
                            store.add_message(conversation_id, "assistant", response_text)
                            partial_text = ""  # already persisted in full -- see comment above
                            await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                            return
                else:
                    # Invalid number
                    response_text = f"Please enter a number between 1 and {len(conv_context.pending_selection_items)}."
                    for chunk in response_text:
                        await _content(chunk)
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    partial_text = ""  # already persisted in full -- see comment above
                    await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                    return

            # Explicit engine handoff (#305 part b): the user named a CLI engine
            # ("use codex", "use claude code"). Hand the task off to that worker
            # session rather than answering inline — the surface (Telegram) spawns
            # it and reports back. Explicit intent, so it precedes classification
            # and the agentic loop.
            from api.services.agent_loop import parse_engine_directive
            _engine, _engine_task = parse_engine_directive(request.question)
            if _engine:
                _label = "Codex" if _engine == "codex" else "Claude Code"
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': [_engine], 'reasoning': f'User-directed handoff to {_label}', 'latency_ms': 0})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'claude_intent', 'task': _engine_task, 'engine': _engine})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            # Intent classification for special dispatch cases only.
            # Compose, task, and reminder intents now flow through the agentic
            # loop which has dedicated tools (create_email_draft, manage_tasks,
            # manage_reminders). Only "code" and "ambiguous" need early return.
            with trace_span("intent_classify"):
                action_intent = await classify_action_intent(request.question, conversation_history)

            if action_intent and action_intent.category == "ambiguous_task_reminder":
                # ---- AMBIGUOUS: could be task or reminder ----
                print("DETECTED AMBIGUOUS TASK/REMINDER INTENT")
                response_text = "Should I add this as a **to-do** in your task list, or set a **timed reminder** to ping you about it, or both?"
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': ['clarification'], 'reasoning': 'Ambiguous task/reminder', 'latency_ms': 0})}\n\n")
                for chunk in response_text:
                    await _content(chunk)
                    await asyncio.sleep(0.005)
                store.add_message(conversation_id, "assistant", response_text)
                partial_text = ""  # already persisted in full -- see comment above
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            elif action_intent and action_intent.category == "claude":
                # ---- CLAUDE CODE: requires terminal/filesystem/browser ----
                print("DETECTED CLAUDE INTENT - delegating to Claude Code")
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': ['claude_code'], 'reasoning': 'Action requires terminal/filesystem/browser access', 'latency_ms': 0})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'claude_intent', 'task': request.question, 'engine': 'claude_code'})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            # --- REMOVED: legacy compose/task/reminder handlers ---
            # These intents now flow through the agentic loop below, which has
            # create_email_draft, manage_tasks, and manage_reminders tools.
            # Keeping this comment as a breadcrumb for future readers.

            # =============================================================
            # Agentic synthesis path: Claude decides what to fetch
            # =============================================================
            from api.services.agent_loop import run_agent_loop, resolve_orchestrator_model

            # Expand follow-up queries with conversation context
            with trace_span("query_expand"):
                effective_question = request.question
                if conversation_history:
                    expanded = expand_followup_query(request.question, conversation_history)
                    if expanded != request.question:
                        effective_question = expanded
                        print(f"Expanded query: '{request.question}' -> '{effective_question}'")
                    else:
                        from api.services.conversation_context import (
                            extract_context_from_history,
                            expand_followup_with_context,
                        )
                        conv_context = extract_context_from_history(conversation_history)
                        if conv_context.has_person_context():
                            expanded = expand_followup_with_context(request.question, conv_context)
                            if expanded != request.question:
                                effective_question = expanded
                                print(f"Expanded query (context): '{request.question}' -> '{effective_question}'")

            # The agent loop uses the orchestrator model configured by
            # LIFEOS_ANTHROPIC_MODEL (or the local backend if LIFEOS_LLM_BACKEND=local).
            # The perf-trace field is still named `model_tier` because the column
            # in perf_traces.db is `model_tier` — but the value is now a model id
            # (e.g. "claude-haiku-4-5"), not a tier label ("haiku"/"sonnet"/"opus").
            orchestrator_model = getattr(settings, "anthropic_model", "claude-haiku-4-5")
            # Escalation: pick a stronger model for this turn either because the
            # user explicitly asked ("escalate to opus", #305) or because the
            # prior turn refused and this message pushes back (#303). Anthropic
            # backend only — the local backend can't honor a per-turn model.
            escalated = False
            force_local = False
            force_remote = False
            # Per-turn model picker: an explicit pick wins over auto-escalation.
            # "gemma"/"local" → run this turn on the local backend; "remote"
            # (#654) → the configured paid OpenAI-compatible provider, an
            # explicit pick only — never reachable from auto-escalation (see
            # NON_API_RUNGS in agent_loop.py); a tier word or model id → pin
            # this turn to that cloud model. "auto"/unset falls through to the
            # normal Haiku + escalation path.
            _override = (request.model_override or "").strip().lower()
            _backend_is_anthropic = getattr(settings, "llm_backend", "anthropic").lower() == "anthropic"
            if _override == "remote" and not settings.remote_llm_configured:
                # The picker hides this option when unconfigured, but an
                # explicit-but-unusable pick from a raw API caller falls back
                # to auto rather than being treated as an Anthropic model id
                # named "remote" (which would 404).
                _override = "auto"
            if _override in ("gemma", "local"):
                force_local = True
                orchestrator_model = "local"
            elif _override == "remote":
                force_remote = True
                orchestrator_model = settings.remote_llm_model
            elif _override and _override != "auto" and _backend_is_anthropic:
                from api.services.agent_loop import resolve_model_alias
                picked = resolve_model_alias(_override)
                if picked != orchestrator_model:
                    orchestrator_model = picked
                    escalated = True  # build a dedicated per-turn client for the pick
            elif _backend_is_anthropic:
                escalation_model = getattr(settings, "agent_escalation_model", "") or ""
                orchestrator_model, escalated = resolve_orchestrator_model(
                    conversation_history, request.question, orchestrator_model, escalation_model
                )
            # A `local` rung (#584): the ladder can climb to the on-box model
            # instead of an API one. Handled here rather than below because it
            # IS an LLM turn — just on the other backend — so it must set
            # force_local before the loop builds its client, and must not fall
            # through to the model path where "local" would be sent to Anthropic
            # as a model id.
            if escalated and orchestrator_model == "local":
                force_local = True
                logger.info("escalation ladder → local (Gemma) turn")

            # Top of the escalation ladder (#305c): when repeated refusals exhaust
            # the model rungs, resolve returns an engine name — hand off to that
            # worker session instead of running the loop on a non-model.
            if escalated and orchestrator_model in ("codex", "claude_code"):
                _label = "Codex" if orchestrator_model == "codex" else "Claude Code"
                logger.info("escalation ladder reached engine handoff → %s", _label)
                # Hand off the ORIGINAL request, not the bare pushback that
                # triggered the climb ("you're wrong") — the worker has no chat
                # context.
                from api.services.agent_loop import _original_request
                _handoff_task = _original_request(conversation_history, request.question)
                await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': [orchestrator_model], 'reasoning': f'Escalation ladder → {_label}', 'latency_ms': 0})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'claude_intent', 'task': _handoff_task, 'engine': orchestrator_model})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return
            if escalated:
                logger.info("escalating chat turn to %s (user-directed or refusal+pushback)", orchestrator_model)
            _trace = _current_trace.get()
            if _trace:
                _trace.model_tier = orchestrator_model
            print(f"\n{'='*60}")
            print(f"QUERY: {request.question}")
            print(f"MODEL: {orchestrator_model}")
            print(f"CONVERSATION: {conversation_id}")
            print(f"{'='*60}")

            # Prepare attachments
            attachments_for_api = None
            if request.attachments:
                attachments_for_api = [
                    {
                        "filename": att.filename,
                        "media_type": att.media_type,
                        "data": att.data,
                    }
                    for att in request.attachments
                ]

            _routing_reason = (
                f'Agentic loop — escalated to {orchestrator_model}'
                if escalated else f'Agentic loop ({orchestrator_model})'
            )
            await turn.emit(f"data: {json.dumps({'type': 'routing', 'sources': ['agent'], 'reasoning': _routing_reason, 'latency_ms': 0})}\n\n")

            # Consume the async generator from the agent loop
            agent_result = None
            async for event in run_agent_loop(
                question=effective_question,
                conversation_history=conversation_history,
                attachments=attachments_for_api,
                model_tier=orchestrator_model,
                max_tool_rounds=5,
                model=orchestrator_model if escalated else "",
                persona=persona_preamble,
                voice_rules=voice_rules,
                personal_context=personal_context,
                force_local=force_local,
                force_remote=force_remote,
            ):
                if event["type"] == "turn_state":
                    # #615: live, mutable AgentResult -- see the comment by
                    # `live_result`'s declaration above.
                    live_result = event["result"]
                elif event["type"] == "text":
                    await _content(event['content'])
                elif event["type"] == "status":
                    await turn.emit(f"data: {json.dumps({'type': 'status', 'message': event['message']})}\n\n")
                elif event["type"] == "self_correction":
                    # #611: what was streamed so far is superseded by the
                    # self-corrected retry that follows — mirrors
                    # ask-stream.js's `fullContent = ''` reset, and matters
                    # here because `agent_result.full_text` only gains a
                    # round's text at round end, so it is NOT what the user
                    # actually saw and isn't safe to persist on a
                    # cancellation that lands mid-correction.
                    partial_text = ""
                    await turn.emit(f"data: {json.dumps({'type': 'self_correction'})}\n\n")
                elif event["type"] == "result":
                    agent_result = event["result"]

            if agent_result is None:
                await turn.emit(f"data: {json.dumps({'type': 'error', 'message': 'Agent loop returned no result'})}\n\n")
                await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")
                return

            # Record usage
            if agent_result.total_input_tokens > 0:
                # getattr tolerates an agent_result predating #654 (e.g. a
                # test double) that has no unpriced field at all.
                _unpriced = getattr(agent_result, "unpriced", False)
                usage_store = get_usage_store()
                usage_store.record_usage(
                    model=agent_result.model,
                    input_tokens=agent_result.total_input_tokens,
                    output_tokens=agent_result.total_output_tokens,
                    cost_usd=agent_result.total_cost_usd,
                    conversation_id=conversation_id,
                    unpriced=_unpriced,
                )
                usage_recorded = True  # #615: the cancel/deadline handler must not double-write this
                # #654: an unpriced turn (a remote pick with no configured
                # rate) sends no cost_usd at all rather than a confident free
                # 0 -- the same three-state contract Hermes turns already use
                # (docs/specs/technical/client-surfaces.md's "Usage and cost
                # reporting" section); ask-stream.js already parses it.
                _usage_event = {
                    'type': 'usage',
                    'input_tokens': agent_result.total_input_tokens,
                    'output_tokens': agent_result.total_output_tokens,
                    'model': agent_result.model,
                }
                if not _unpriced:
                    _usage_event['cost_usd'] = agent_result.total_cost_usd
                await turn.emit(f"data: {json.dumps(_usage_event)}\n\n")

            # Build source list from tool calls
            sources = []
            _source_type_map = {
                "search_vault": "vault",
                "read_vault_file": "vault",
                "search_calendar": "calendar",
                "search_email": "gmail",
                "search_drive": "drive",
                "search_slack": "slack",
                "search_web": "web",
                "get_message_history": "imessage",
                "person_info": "people",
            }
            for tc in agent_result.tool_calls_log:
                source_type = _source_type_map.get(tc["tool"])
                if source_type and not tc.get("is_error"):
                    sources.append({
                        "file_name": f"{tc['tool']}({json.dumps(tc['input'], default=str)[:80]})",
                        "source_type": source_type,
                    })

            if request.include_sources and sources:
                await turn.emit(f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n")

            # Save assistant response
            routing_metadata = {
                "sources": [tc["tool"] for tc in agent_result.tool_calls_log],
                "reasoning": f"agentic ({orchestrator_model})",
                "tool_rounds": len(agent_result.tool_calls_log),
            }
            store.add_message(
                conversation_id,
                "assistant",
                agent_result.full_text,
                sources=sources,
                routing=routing_metadata,
            )
            # Persisted in full above (the authoritative agent_result.full_text,
            # not the streamed partial_text) -- clear the accumulator so a
            # cancellation on the way out below can't re-persist it as a
            # truncated duplicate of the message just written (#611).
            partial_text = ""
            print(f"Saved assistant response ({len(agent_result.full_text)} chars, {len(agent_result.tool_calls_log)} tool calls)")

            # Finish performance trace and emit it
            perf_trace = finish_trace()
            if perf_trace:
                await turn.emit(f"data: {json.dumps({'type': 'perf_trace', 'trace_id': perf_trace.trace_id, 'total_ms': round(perf_trace.total_ms, 1), 'spans': [{'name': s.name, 'duration_ms': s.duration_ms, 'parent': s.parent} for s in perf_trace.spans]})}\n\n")

            await turn.emit(f"data: {json.dumps({'type': 'done'})}\n\n")

        except asyncio.CancelledError:
            # #611: the client disconnected (survivable turn, now cancelled
            # by an explicit /cancel, a supersede, the detached-lifetime
            # deadline, or a shutdown drain) — every modality alike since
            # #616 lifted the voice-only immediate-cancel gate. Persist
            # whatever the user had already seen rather than losing it
            # outright — marked so it's never mistaken for a finished reply.
            if conversation_id and partial_text:
                store.add_message(
                    conversation_id, "assistant", partial_text + TRUNCATION_MARKER,
                    routing=truncation_routing(turn.cancel_reason or "cancelled"),
                )
            # #615: the tokens for a cancelled turn were already spent (and
            # billed) even though the loop never reached its terminal
            # `result` event -- read them from the live reference stashed
            # from `turn_state` instead. `usage_recorded` guards against a
            # cancellation landing after the normal end-of-turn write above
            # (e.g. during `finish_trace`/`turn.emit` on the way out) from
            # double-recording it.
            #
            # #629 narrowed, but did not close, the gap #615 left open: both
            # LocalLLMClient.astream and AnthropicLLMClient.astream only
            # yield *confirmed* usage in the "done" event that closes out a
            # full round's stream, and agent_loop._track_usage() folds that
            # into total_input_tokens/total_output_tokens only once "done"
            # arrives. A cancellation landing mid-round used to report zero
            # for that round no matter what. Now, on the Anthropic backend,
            # agent_loop also tracks provisional_input_tokens /
            # provisional_output_tokens -- the in-flight round's cumulative
            # usage-so-far, from the "usage_update" events Anthropic's wire
            # protocol carries via `message_start`/`message_delta` (see
            # AnthropicLLMClient.astream's docstring) -- so a mid-round
            # cancellation there credits the tokens already billed instead
            # of reporting nothing. The local backend's OpenAI-compatible
            # protocol has no equivalent mid-stream signal, so
            # provisional_input_tokens/provisional_output_tokens stay 0
            # there always, and a mid-round cancellation on that backend is
            # unchanged from before #629: still a known, narrower surviving
            # gap, not a claim that nothing was spent.
            #
            # `total_input_tokens + provisional_input_tokens > 0` is NOT
            # "nothing was spent" -- it's "neither the confirmed nor the
            # provisional accumulator has moved off its initial zero." The
            # guard exists because writing a zero-token row would
            # affirmatively assert "this cost nothing," which is worse than
            # writing nothing at all. `getattr(..., 0)` tolerates a
            # `live_result` that predates #629 (e.g. a test double) and
            # therefore has no provisional_* fields at all.
            #
            # This also covers a cancelled turn before any round completed
            # (neither accumulator ever moved) and a fake test loop that
            # never emits `turn_state` (`live_result` stays None).
            if conversation_id and not usage_recorded and live_result is not None:
                cancelled_input_tokens = live_result.total_input_tokens + getattr(
                    live_result, "provisional_input_tokens", 0)
                cancelled_output_tokens = live_result.total_output_tokens + getattr(
                    live_result, "provisional_output_tokens", 0)
                if cancelled_input_tokens > 0:
                    get_usage_store().record_usage(
                        model=live_result.model,
                        input_tokens=cancelled_input_tokens,
                        output_tokens=cancelled_output_tokens,
                        cost_usd=live_result.total_cost_usd,
                        conversation_id=conversation_id,
                        # getattr tolerates a live_result predating #654/#661
                        # (or a test double) that has no unpriced field at all.
                        unpriced=getattr(live_result, "unpriced", False),
                    )
                    usage_recorded = True
            finish_trace()
            raise
        except Exception as e:
            finish_trace()  # Clean up trace on error
            if conversation_id and partial_text:
                # #611: a genuine mid-stream error (e.g. the agent loop
                # itself raised) used to leave NOTHING persisted even though
                # partial_text had already reached the browser -- an
                # invisible truncation with no signal anything went wrong.
                # Persist it, marked, same as an explicit cancellation.
                store.add_message(
                    conversation_id, "assistant", partial_text + TRUNCATION_MARKER,
                    routing=truncation_routing("stream_error"),
                )
            await turn.emit(f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n")
        finally:
            get_turn_registry().pop(turn)
            await turn.close()

    # #611: start the turn as a background task NOW -- it owns its own
    # lifetime from here on -- and hand StreamingResponse a reader over its
    # queue rather than the turn's own generator. A connected client's frame
    # sequence is unaffected (emit() backpressures exactly like a bare
    # `yield` did), but closing this reader (a disconnect) no longer stops
    # the task underneath it.
    turn.task = asyncio.create_task(_run_turn())
    return StreamingResponse(
        turn.reader(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


class ChatCancelRequest(BaseModel):
    """Cancel a chat turn by its client-supplied key (#611 review)."""
    client_turn_id: str

    @field_validator("client_turn_id")
    @classmethod
    def validate_client_turn_id(cls, v):
        if not v:
            raise ValueError("client_turn_id cannot be empty")
        if len(v) > 200:
            raise ValueError(f"client_turn_id exceeds 200 chars (got {len(v)})")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in v):
            raise ValueError("client_turn_id must not contain control characters")
        return v


@router.post("/chat/cancel")
async def chat_cancel(request: ChatCancelRequest):
    """Cancel a chat turn (native or Hermes-relayed) by `client_turn_id` —
    the key the client mints and sends on `POST /api/ask/stream`, BEFORE it
    has a `conversation_id` to cancel by (#611 review). This closes the
    "first-turn barge-in" gap `POST /api/conversations/{id}/cancel` alone
    can't: a request that hasn't reached its first SSE frame yet has no
    conversation id, but the client already has the key it generated.

    There is no 404 here — unlike the conversation-scoped endpoint, there's
    no existing resource to validate against (a `client_turn_id` might
    legitimately never have been claimed, e.g. the turn already finished
    and was popped from the registry, or the key was never used). `200` +
    `cancelled: false` covers every "nothing to cancel" case; this must
    never be a 4xx, since a gateway that fires its cancel POST slightly
    ahead of the turn actually finishing needs that to read as success, not
    an error.
    """
    cancelled = get_turn_registry().cancel_by_client_turn_id(request.client_turn_id)
    return {"ok": True, "cancelled": cancelled}


class HandoffRequest(BaseModel):
    """Web-chat → CLI-engine handoff (#305b/c)."""
    engine: str  # "codex" | "claude_code"
    task: str
    conversation_id: Optional[str] = None


@router.post("/chat/handoff")
async def chat_handoff(request: HandoffRequest):
    """Spawn a CLI engine worker session from the web chat.

    The web UI calls this when the orchestrator emits a `claude_intent` event it
    can't act on inline (an explicit "use codex" handoff, or the top rung of the
    escalation ladder). The session runs in the agent worker and reports its
    result via Telegram and `/agents`; we record an acknowledgment in the
    conversation thread so the web UI reflects the handoff.
    """
    engine = (request.engine or "").strip()
    task = (request.task or "").strip()
    if engine not in ("codex", "claude_code"):
        raise HTTPException(status_code=400, detail="engine must be 'codex' or 'claude_code'")
    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    from api.services.directory_resolver import resolve_working_directory
    from api.services.agent_worker.session_store import SessionStore

    working_dir = resolve_working_directory(task)
    # Also route the worker's completion notification to the operator's Telegram.
    # As of #311 the spawned session's progress + result are mirrored back into
    # this web/voice thread too (the conversation is linked to the session
    # below), so Telegram is now an additional delivery channel, not the only
    # one. None if Telegram isn't set.
    chat_id = getattr(settings, "telegram_chat_id", "") or None

    if engine == "codex":
        from api.services.agent_worker.codex_spawn import spawn_codex_session
        result = spawn_codex_session(SessionStore(), task, working_dir=working_dir, chat_id=chat_id)
    else:
        from api.services.agent_worker.claude_code_spawn import (
            spawn_claude_code_session,
            should_use_plan_mode,
        )
        result = spawn_claude_code_session(
            SessionStore(), task, working_dir=working_dir,
            plan_mode=should_use_plan_mode(task), chat_id=chat_id,
        )

    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "engine spawn failed"))

    label = "Codex" if engine == "codex" else "Claude Code"
    session_id = result.get("session_id", "")
    ack = (
        f"🤝 Handed off to **{label}** — running in the background "
        f"(session `{session_id[:12]}`). The result will appear right here, "
        f"and also via Telegram and on the /agents page."
    )
    if request.conversation_id:
        try:
            get_store().add_message(
                request.conversation_id, "assistant", ack,
                routing={"reasoning": f"engine handoff → {label}", "sources": [engine]},
            )
        except Exception:
            logger.warning("failed to record handoff acknowledgment", exc_info=True)
        # #311: link the conversation to the spawned session so the worker can
        # mirror the session's progress + terminal result back into this thread
        # (in addition to Telegram). Best-effort — a link failure only loses the
        # web round-trip, not the Telegram delivery. Mirrors the orchestrating-
        # persona path (chat.py set_agent_session_id after a doctor spawn).
        if session_id:
            try:
                get_store().set_agent_session_id(request.conversation_id, session_id)
            except Exception:
                logger.warning("failed to link conversation to handoff session", exc_info=True)

    return {
        "ok": True,
        "session_id": session_id,
        "engine": engine,
        "working_dir": working_dir,
        "message": ack,
    }
