"""
Chat API endpoints with streaming support.
"""
import json
import asyncio
import logging
import re
import base64
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from api.services.vectorstore import VectorStore
from api.services.hybrid_search import HybridSearch
from api.services.synthesizer import construct_prompt, get_synthesizer
from api.services.query_router import QueryRouter
from api.services.conversation_store import get_store, generate_title
from api.services.calendar import CalendarService
from api.services.drive import DriveService
from api.services.gmail import GmailService
from api.services.usage_store import get_usage_store
from api.services.briefings import get_briefings_service
from api.services.chat_helpers import (
    extract_search_keywords,
    expand_followup_query,
    detect_compose_intent,
    detect_reminder_intent,
    extract_date_context,
    extract_message_date_range,
    extract_message_search_terms,
    format_messages_for_synthesis as _format_messages_for_synthesis,
    format_raw_qa_section as _format_raw_qa_section,
    ReminderIntentType,
    classify_reminder_intent,
    detect_reminder_edit_intent,
    detect_reminder_list_intent,
    detect_reminder_delete_intent,
    extract_reminder_topic,
)
from api.services.time_parser import (
    get_smart_default_time,
    parse_contextual_time,
    format_time_for_display,
    extract_time_from_query,
)
from config.settings import settings
from api.services.google_auth import GoogleAccount

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


async def extract_draft_params(query: str, conversation_history: list = None) -> Optional[dict]:
    """
    Use Claude to extract email draft parameters from a compose request.

    Returns dict with: to, subject, body, account (personal/work)
    Or None if extraction fails.
    """
    # Build context from conversation if available
    context = ""
    if conversation_history:
        recent_msgs = conversation_history[-6:]  # Last 3 exchanges
        context_parts = []
        for msg in recent_msgs:
            context_parts.append(f"{msg.role}: {msg.content[:500]}")
        if context_parts:
            context = "\n\nConversation context:\n" + "\n".join(context_parts)

    extraction_prompt = f"""Extract email draft parameters from this request.{context}

User request: {query}

Return a JSON object with these fields (leave empty string if not specified):
- "to": recipient email or name (required)
- "subject": email subject line
- "body": the email body content to write
- "account": "personal" or "work" (default to "personal" unless work/professional context is mentioned)

If the user is asking to draft a follow-up or reply based on conversation context, use that context to fill in the body.

Return ONLY valid JSON, no other text. Example:
{{"to": "john@example.com", "subject": "Follow up on meeting", "body": "Hi John,\\n\\nI wanted to follow up...", "account": "personal"}}"""

    try:
        synthesizer = get_synthesizer()
        response_text = await synthesizer.get_response(
            extraction_prompt,
            max_tokens=1024,
            model_tier="haiku"  # Fast, cheap for structured extraction
        )

        # Find JSON in response
        json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if json_match:
            params = json.loads(json_match.group())
            # Validate required field
            if params.get("to"):
                return params
    except Exception as e:
        logger.error(f"Failed to extract draft params: {e}")

    return None


async def extract_reminder_params(query: str, conversation_history: list = None) -> Optional[dict]:
    """
    Use Claude to extract reminder parameters from a reminder creation request.

    Uses smart time defaults when no specific time is mentioned:
    - "remind me to X" without time -> tomorrow 9am
    - "remind me later today" -> 5pm or 8pm depending on current time
    - "remind me tonight" -> 8pm today

    Returns dict with:
        - name: Human-readable reminder name
        - schedule_type: 'once' or 'cron'
        - schedule_value: ISO datetime or cron expression
        - message_type: 'static' (default)
        - message_content: The reminder message
        - display_time: Human-readable time for confirmation
    Or None if extraction fails.
    """
    from zoneinfo import ZoneInfo

    # Get current time for context
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    current_datetime = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    # Try to parse time from query using smart time parser first
    time_expr = extract_time_from_query(query)
    parsed_time = None
    if time_expr:
        parsed_time = parse_contextual_time(time_expr, now)

    # If no time expression found or couldn't parse, use smart default
    if parsed_time is None:
        # Check if query has any time indicators at all
        has_time_indicator = any(word in query.lower() for word in [
            'at', 'pm', 'am', 'tomorrow', 'today', 'tonight', 'morning',
            'afternoon', 'evening', 'next', 'in ', 'daily', 'weekly',
            'every', 'hour', 'minute'
        ])
        if not has_time_indicator:
            # No time mentioned - use smart default (tomorrow 9am)
            parsed_time = get_smart_default_time(now)

    # Build context from conversation if available
    context = ""
    if conversation_history:
        recent_msgs = conversation_history[-4:]  # Last 2 exchanges
        context_parts = []
        for msg in recent_msgs:
            context_parts.append(f"{msg.role}: {msg.content[:300]}")
        if context_parts:
            context = "\n\nConversation context:\n" + "\n".join(context_parts)

    # Include parsed time hint for Claude
    time_hint = ""
    if parsed_time:
        time_hint = f"\n\nNote: Based on the query, the intended time appears to be: {parsed_time.isoformat()}"

    extraction_prompt = f"""Extract reminder parameters from this request.{context}

Current date/time: {current_datetime}{time_hint}

User request: {query}

Return a JSON object with these fields:
- "name": Short descriptive name for the reminder (e.g., "Library Book Reminder")
- "schedule_type": "once" for one-time, "cron" for recurring
- "schedule_value": For "once" use ISO datetime (e.g., "2026-02-07T18:00:00-05:00"). For "cron" use cron expression (e.g., "0 18 * * *" for daily at 6pm)
- "message_content": The reminder message to send (include any URLs mentioned)
- "timezone": The timezone mentioned or implied (default to "America/New_York" for ET/Eastern)

IMPORTANT:
- If no specific time is mentioned, use the time hint provided above
- If the time hint shows tomorrow at 9am, use that as the schedule_value
- Daily at 6pm ET = "0 18 * * *"
- Every weekday at 9am = "0 9 * * 1-5"
- Convert times to 24-hour format for cron. 6pm = 18, 9am = 9, etc.

Return ONLY valid JSON, no other text. Example:
{{"name": "Library Book Reminder", "schedule_type": "once", "schedule_value": "2026-02-09T09:00:00-05:00", "message_content": "Return the library book", "timezone": "America/New_York"}}"""

    try:
        synthesizer = get_synthesizer()
        response_text = await synthesizer.get_response(
            extraction_prompt,
            max_tokens=512,
            model_tier="haiku"  # Fast, cheap for structured extraction
        )

        # Find JSON in response
        json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if json_match:
            params = json.loads(json_match.group())
            # Validate required fields
            if params.get("schedule_type") and params.get("schedule_value"):
                # Default message_type to static
                params["message_type"] = "static"

                # Add human-readable display time
                if params["schedule_type"] == "once":
                    try:
                        trigger_dt = datetime.fromisoformat(params["schedule_value"])
                        params["display_time"] = format_time_for_display(trigger_dt, now)
                    except (ValueError, TypeError):
                        params["display_time"] = params["schedule_value"]

                return params
    except Exception as e:
        logger.error(f"Failed to extract reminder params: {e}")

    return None


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

    eastern = ZoneInfo("America/New_York")
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


def find_reminder_by_topic(
    topic: Optional[str],
    reminders: list,
    context_reminder_id: Optional[str] = None,
    context_reminder_name: Optional[str] = None,
) -> Optional[tuple]:
    """
    Find a reminder by topic/name using fuzzy matching.

    Resolution order:
    1. If topic provided, search all reminders for fuzzy match
    2. If no topic, use context (last created reminder in conversation)
    3. Return None if no match

    Args:
        topic: Topic extracted from query (e.g., "library book")
        reminders: List of Reminder objects to search
        context_reminder_id: Last reminder ID from conversation context
        context_reminder_name: Last reminder name from conversation context

    Returns:
        Tuple of (reminder, match_type) or None
        match_type: "exact", "fuzzy", "context", "ambiguous"
    """
    if not reminders:
        return None

    # If topic provided, search by name/content
    if topic:
        topic_lower = topic.lower()
        matches = []

        for reminder in reminders:
            name_lower = reminder.name.lower()
            content_lower = (reminder.message_content or "").lower()

            # Exact match in name
            if topic_lower == name_lower or topic_lower in name_lower:
                matches.append((reminder, "exact"))
            # Fuzzy match - topic words appear in name or content
            elif any(word in name_lower or word in content_lower
                     for word in topic_lower.split()):
                matches.append((reminder, "fuzzy"))

        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            # Multiple matches - return first with "ambiguous" flag
            return (matches, "ambiguous")

    # Fall back to conversation context
    if context_reminder_id:
        for reminder in reminders:
            if reminder.id == context_reminder_id:
                return (reminder, "context")

    if context_reminder_name:
        for reminder in reminders:
            if reminder.name.lower() == context_reminder_name.lower():
                return (reminder, "context")

    return None


router = APIRouter(prefix="/api", tags=["chat"])

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


class SaveToVaultRequest(BaseModel):
    """Request for save to vault endpoint.

    Supports two modes:
    1. Full conversation mode: provide conversation_id
    2. Single Q&A mode: provide question and answer (backward compatible)
    """
    # Content - supports full conversation
    conversation_id: Optional[str] = None
    question: Optional[str] = None  # Fallback for single Q&A
    answer: Optional[str] = None

    # User customization
    title: Optional[str] = None
    folder: Optional[str] = None
    tags: Optional[list[str]] = None

    # Content toggles
    include_sources: bool = True
    include_raw_qa: bool = False
    full_conversation: bool = True

    # Custom guidance
    guidance: Optional[str] = None


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

    async def generate():
        try:
            # Get or create conversation
            store = get_store()
            conversation_id = request.conversation_id

            if not conversation_id:
                # Create new conversation
                conv = store.create_conversation()
                conversation_id = conv.id
                # Generate title from question
                title = generate_title(request.question)
                store.update_title(conversation_id, title)
                print(f"Created new conversation: {conversation_id} - {title}")

            # Send conversation ID to client
            yield f"data: {json.dumps({'type': 'conversation_id', 'conversation_id': conversation_id})}\n\n"

            # Save user message
            store.add_message(conversation_id, "user", request.question)

            # Get conversation history for context in follow-up questions
            conversation_history = store.get_messages(conversation_id, limit=10)
            # Exclude current message to avoid duplication
            if conversation_history and conversation_history[-1].role == "user" and conversation_history[-1].content == request.question:
                conversation_history = conversation_history[:-1]

            # Check for email compose intent - handle as action, not search
            if detect_compose_intent(request.question):
                print("DETECTED COMPOSE INTENT - handling email draft")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['gmail_draft'], 'reasoning': 'Email composition detected', 'latency_ms': 0})}\n\n"

                draft_params = await extract_draft_params(request.question, conversation_history)
                if draft_params:
                    try:
                        # Determine account
                        account_str = draft_params.get("account", "personal").lower()
                        account_type = GoogleAccount.WORK if account_str == "work" else GoogleAccount.PERSONAL

                        gmail = GmailService(account_type)
                        draft = gmail.create_draft(
                            to=draft_params["to"],
                            subject=draft_params.get("subject", ""),
                            body=draft_params.get("body", ""),
                        )

                        if draft:
                            # Construct Gmail URL (same as API route)
                            gmail_url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft.draft_id}"

                            response_text = f"I've created a draft email for you:\n\n"
                            response_text += f"**To:** {draft.to}\n"
                            response_text += f"**Subject:** {draft.subject}\n"
                            response_text += f"**Account:** {draft.source_account}\n\n"
                            response_text += f"[Open draft in Gmail]({gmail_url})\n\n"
                            response_text += f"Review and send when ready."

                            # Stream the response
                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.01)

                            # Save assistant response
                            store.add_message(conversation_id, "assistant", response_text)

                            # Send done signal
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                        else:
                            error_msg = "Failed to create draft. Please try again."
                            yield f"data: {json.dumps({'type': 'content', 'content': error_msg})}\n\n"
                            store.add_message(conversation_id, "assistant", error_msg)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                    except Exception as e:
                        error_msg = f"Error creating draft: {str(e)}"
                        logger.error(error_msg)
                        yield f"data: {json.dumps({'type': 'content', 'content': error_msg})}\n\n"
                        store.add_message(conversation_id, "assistant", error_msg)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                else:
                    # Couldn't extract params, fall through to normal flow
                    # which will use Claude to ask for clarification
                    print("Could not extract draft params, falling through to normal flow")

            # Check for any reminder-related intent (CREATE, EDIT, LIST, DELETE)
            reminder_intent = classify_reminder_intent(request.question)
            if reminder_intent:
                print(f"DETECTED REMINDER INTENT: {reminder_intent.value}")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['reminder'], 'reasoning': f'Reminder {reminder_intent.value} detected', 'latency_ms': 0})}\n\n"

                # Check if Telegram is configured (settings is imported at module level)
                if not settings.telegram_enabled:
                    error_msg = "Telegram is not configured. To set up reminders, please configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file."
                    for chunk in error_msg:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", error_msg)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                from api.services.reminder_store import get_reminder_store
                from api.services.conversation_context import extract_context_from_history

                reminder_store = get_reminder_store()

                # Get conversation context for reminder follow-ups
                conv_context = extract_context_from_history(conversation_history) if conversation_history else None

                # Handle LIST intent
                if reminder_intent == ReminderIntentType.LIST:
                    all_reminders = reminder_store.list_all()
                    enabled_reminders = [r for r in all_reminders if r.enabled]

                    if not enabled_reminders:
                        response_text = "You don't have any active reminders."
                    else:
                        response_text = f"You have {len(enabled_reminders)} active reminder(s):\n\n"
                        for r in enabled_reminders:
                            if r.schedule_type == "cron":
                                schedule_desc = f"recurring ({r.schedule_value})"
                            else:
                                # Format one-time reminders nicely
                                try:
                                    trigger_dt = datetime.fromisoformat(r.schedule_value)
                                    schedule_desc = format_time_for_display(trigger_dt)
                                except (ValueError, TypeError):
                                    schedule_desc = r.schedule_value
                            response_text += f"- **{r.name}**: {schedule_desc}\n"
                            if r.message_content:
                                response_text += f"  _{r.message_content[:50]}{'...' if len(r.message_content) > 50 else ''}_\n"

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle DELETE intent
                if reminder_intent == ReminderIntentType.DELETE:
                    topic = extract_reminder_topic(request.question)
                    all_reminders = reminder_store.list_all()

                    match_result = find_reminder_by_topic(
                        topic,
                        all_reminders,
                        conv_context.last_reminder_id if conv_context else None,
                        conv_context.last_reminder_name if conv_context else None,
                    )

                    if match_result is None:
                        response_text = "I couldn't find a reminder to delete. "
                        if topic:
                            response_text += f"No reminder matching \"{topic}\" was found."
                        else:
                            response_text += "Please specify which reminder to delete, or say \"list my reminders\" to see them."
                    elif match_result[1] == "ambiguous":
                        matches = match_result[0]
                        response_text = f"I found multiple reminders matching \"{topic}\". Which one?\n\n"
                        for r in matches:
                            response_text += f"- {r[0].name}\n"
                    else:
                        reminder, match_type = match_result
                        reminder_store.delete(reminder.id)
                        response_text = f"I've deleted the reminder **\"{reminder.name}\"**."

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle EDIT intent
                if reminder_intent == ReminderIntentType.EDIT:
                    topic = extract_reminder_topic(request.question)
                    all_reminders = reminder_store.list_all()

                    match_result = find_reminder_by_topic(
                        topic,
                        all_reminders,
                        conv_context.last_reminder_id if conv_context else None,
                        conv_context.last_reminder_name if conv_context else None,
                    )

                    if match_result is None:
                        response_text = "I couldn't find a reminder to edit. "
                        if topic:
                            response_text += f"No reminder matching \"{topic}\" was found."
                        else:
                            response_text += "Please specify which reminder to change, or say \"list my reminders\" to see them."
                    elif match_result[1] == "ambiguous":
                        matches = match_result[0]
                        response_text = f"I found multiple reminders matching \"{topic}\". Which one?\n\n"
                        for r in matches:
                            response_text += f"- {r[0].name}\n"
                    else:
                        reminder, match_type = match_result
                        edit_params = await extract_reminder_edit_params(request.question, reminder.name)

                        if edit_params:
                            reminder_store.update(
                                reminder.id,
                                schedule_type=edit_params.get("schedule_type", reminder.schedule_type),
                                schedule_value=edit_params["schedule_value"],
                            )
                            display_time = edit_params.get("display_time", edit_params["schedule_value"])
                            response_text = f"I've updated **\"{reminder.name}\"** to {display_time}."
                        else:
                            response_text = f"I couldn't understand the new time. Please try again with something like \"change it to 7pm\" or \"move it to tomorrow\"."

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle CREATE intent
                if reminder_intent == ReminderIntentType.CREATE:
                    reminder_params = await extract_reminder_params(request.question, conversation_history)
                    if reminder_params:
                        try:
                            reminder = reminder_store.create(
                                name=reminder_params.get("name", "Reminder"),
                                schedule_type=reminder_params["schedule_type"],
                                schedule_value=reminder_params["schedule_value"],
                                message_type=reminder_params.get("message_type", "static"),
                                message_content=reminder_params.get("message_content", ""),
                                enabled=True,
                            )

                            # Build response with clear time confirmation
                            display_time = reminder_params.get("display_time", "")
                            if not display_time and reminder.schedule_type == "cron":
                                # Parse cron for human-readable description
                                cron_parts = reminder.schedule_value.split()
                                if len(cron_parts) >= 5:
                                    minute, hour = cron_parts[0], cron_parts[1]
                                    hour_int = int(hour)
                                    am_pm = "AM" if hour_int < 12 else "PM"
                                    hour_12 = hour_int % 12 or 12
                                    time_str = f"{hour_12}:{minute.zfill(2)} {am_pm}"
                                    day_of_week = cron_parts[4]
                                    if day_of_week == "*":
                                        display_time = f"daily at {time_str}"
                                    elif day_of_week == "1-5":
                                        display_time = f"weekdays at {time_str}"
                                    else:
                                        display_time = f"at {time_str}"

                            response_text = f"Done! I've set a reminder for **{display_time}**.\n\n"
                            response_text += f"**{reminder.name}**\n"
                            response_text += f"{reminder.message_content}\n\n"
                            response_text += f"Reply to change the time or say \"cancel that reminder\" to remove it."

                            # Stream the response
                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.005)

                            # Save assistant response with reminder context for follow-ups
                            routing_metadata = {
                                "sources": ["reminder"],
                                "reasoning": "Reminder created",
                                "created_reminder": {
                                    "id": reminder.id,
                                    "name": reminder.name,
                                },
                            }
                            store.add_message(
                                conversation_id,
                                "assistant",
                                response_text,
                                routing=routing_metadata,
                            )

                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                        except Exception as e:
                            error_msg = f"Error creating reminder: {str(e)}"
                            logger.error(error_msg)
                            yield f"data: {json.dumps({'type': 'content', 'content': error_msg})}\n\n"
                            store.add_message(conversation_id, "assistant", error_msg)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                    else:
                        # Couldn't extract params, fall through to normal flow
                        print("Could not extract reminder params, falling through to normal flow")

            # Expand follow-up queries with conversation context
            # v3: Use enhanced conversation context for better follow-up handling
            from api.services.conversation_context import (
                extract_context_from_history,
                expand_followup_with_context,
            )
            query_for_routing = request.question
            conv_context = None
            if conversation_history:
                # First try the legacy expansion
                query_for_routing = expand_followup_query(request.question, conversation_history)
                if query_for_routing != request.question:
                    print(f"Expanded query (legacy): '{request.question}' -> '{query_for_routing}'")
                else:
                    # Try enhanced context-based expansion
                    conv_context = extract_context_from_history(conversation_history)
                    if conv_context.has_person_context():
                        query_for_routing = expand_followup_with_context(
                            request.question,
                            conv_context
                        )
                        if query_for_routing != request.question:
                            print(f"Expanded query (context): '{request.question}' -> '{query_for_routing}'")

            # Route query to determine sources
            query_router = QueryRouter()
            routing_result = await query_router.route(query_for_routing)

            # Console logging for debugging
            print(f"\n{'='*60}")
            print(f"QUERY: {request.question}")
            print(f"CONVERSATION: {conversation_id}")
            print(f"{'='*60}")
            print(f"ROUTING: {routing_result.sources}")
            print(f"  Reasoning: {routing_result.reasoning}")
            print(f"  Confidence: {routing_result.confidence}")
            print(f"  Latency: {routing_result.latency_ms}ms")

            logger.info(
                f"Query routed to: {routing_result.sources} "
                f"(latency: {routing_result.latency_ms}ms, "
                f"confidence: {routing_result.confidence})"
            )

            # Check for name resolution failures (disambiguation or fuzzy suggestions)
            if routing_result.relationship_context and routing_result.relationship_context.get("resolution_failed"):
                failure_type = routing_result.relationship_context.get("failure_type")
                query_name = routing_result.relationship_context.get("query_name", "")

                if failure_type == "ambiguous":
                    # Multiple people with same name - ask user to clarify
                    candidates = routing_result.relationship_context.get("candidates", [])
                    yield f"data: {json.dumps({'type': 'routing', 'sources': ['people'], 'reasoning': 'Name disambiguation needed', 'latency_ms': routing_result.latency_ms})}\n\n"
                    response_text = f"I found multiple people named \"{query_name}\". Which one did you mean?\n\n"
                    for i, c in enumerate(candidates, 1):
                        context = c.get("context", "")
                        strength = c.get("strength", 0)
                        response_text += f"{i}. **{c['name']}** ({context}) - strength: {strength:.0f}/100\n"
                    response_text += "\nPlease specify which person you're asking about.\n"

                    # Stream the response
                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)

                    # Save assistant response
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                elif failure_type == "no_match":
                    # No match - show fuzzy suggestions
                    suggestions = routing_result.relationship_context.get("suggestions", [])
                    yield f"data: {json.dumps({'type': 'routing', 'sources': ['people'], 'reasoning': 'Name not found, suggesting alternatives', 'latency_ms': routing_result.latency_ms})}\n\n"

                    if suggestions:
                        suggestion_text = ", ".join(f'"{s}"' for s in suggestions)
                        response_text = f"I couldn't find anyone named \"{query_name}\" in your contacts. Did you mean: {suggestion_text}?\n\n"
                    else:
                        response_text = f"I couldn't find anyone named \"{query_name}\" in your contacts.\n\n"

                    # Stream the response
                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)

                    # Save assistant response
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

            # Add "attachment" to sources if attachments are present
            effective_sources = list(routing_result.sources)
            if request.attachments:
                effective_sources.append("attachment")
                # Log attachment metadata (not content)
                for att in request.attachments:
                    size_kb = att.get_size_bytes() / 1024
                    print(f"  Attachment: {att.filename} ({att.media_type}, {size_kb:.1f}KB)")

            # Send routing info first (with attachment source if applicable)
            yield f"data: {json.dumps({'type': 'routing', 'sources': effective_sources, 'reasoning': routing_result.reasoning, 'latency_ms': routing_result.latency_ms})}\n\n"

            # Get relevant data based on routing
            chunks = []
            extra_context = []  # For calendar/drive/gmail results
            skipped_sources = []  # Track sources skipped due to sparse data

            # v3: Determine adaptive limits based on fetch_depth
            from api.services.query_router import FETCH_DEPTH_LIMITS
            depth_limits = FETCH_DEPTH_LIMITS.get(routing_result.fetch_depth, FETCH_DEPTH_LIMITS["normal"])
            email_char_limit = depth_limits["email_char_limit"]
            vault_chunk_limit = depth_limits["vault_chunks"]
            message_limit = depth_limits["message_limit"]
            print(f"  Fetch depth: {routing_result.fetch_depth} -> limits: email={email_char_limit}, vault={vault_chunk_limit}, msgs={message_limit}")

            # v3: Smart source skipping based on relationship context
            rel_ctx = routing_result.relationship_context
            if rel_ctx:
                active_channels = rel_ctx.get("active_channels", [])
                email_count = rel_ctx.get("email_count", 0)
                message_count = rel_ctx.get("message_count", 0)

                # Skip gmail if no email history with this person
                if "gmail" in routing_result.sources and email_count < 3:
                    if "gmail" not in active_channels:
                        skipped_sources.append("gmail")
                        print(f"  Skipping gmail (only {email_count} emails, not active)")

                # Note: Don't skip slack here - it's not in relationship_context channels yet

            # Handle calendar queries - ALWAYS query both personal and work calendars
            if "calendar" in routing_result.sources:
                print("FETCHING CALENDAR DATA (both personal and work, parallel)...")
                all_events = []

                # v3: Check if we should filter by person (for meeting prep queries)
                person_filter_email = None
                person_filter_name = routing_result.extracted_person_name
                if person_filter_name and routing_result.relationship_context:
                    # Check if query is about meeting prep with specific person
                    query_lower = request.question.lower()
                    prep_patterns = ["prep", "meeting with", "1:1 with", "call with"]
                    if any(p in query_lower for p in prep_patterns):
                        # Try to get email from CRM context for filtering
                        try:
                            from api.services.entity_resolver import get_entity_resolver
                            resolver = get_entity_resolver()
                            result = resolver.resolve(name=person_filter_name)
                            if result and result.entity and result.entity.emails:
                                person_filter_email = result.entity.emails[0]
                                print(f"  Person filter: {person_filter_name} ({person_filter_email})")
                        except Exception as e:
                            print(f"  Could not resolve person for calendar filter: {e}")

                # Parallel fetch from both accounts
                date_ref = extract_date_context(request.question)
                calendar_results = await asyncio.gather(
                    _fetch_calendar_account(GoogleAccount.PERSONAL, date_ref),
                    _fetch_calendar_account(GoogleAccount.WORK, date_ref),
                    return_exceptions=True
                )
                for result in calendar_results:
                    if isinstance(result, Exception):
                        print(f"  Calendar fetch error: {result}")
                    else:
                        account, events = result
                        all_events.extend(events)
                        print(f"  Found {len(events)} events from {account} calendar")

                # v3: Filter events by person if specified
                if person_filter_email or person_filter_name:
                    filtered_events = []
                    for event in all_events:
                        # Check if person is an attendee
                        attendee_match = False
                        for attendee in event.attendees:
                            if person_filter_email and person_filter_email.lower() in attendee.lower():
                                attendee_match = True
                                break
                            if person_filter_name and person_filter_name.lower() in attendee.lower():
                                attendee_match = True
                                break
                        # Also check title
                        if person_filter_name and person_filter_name.lower() in event.title.lower():
                            attendee_match = True
                        if attendee_match:
                            filtered_events.append(event)
                    if filtered_events:
                        print(f"  Filtered to {len(filtered_events)} events with {person_filter_name}")
                        all_events = filtered_events

                # Sort all events by start time
                all_events.sort(key=lambda e: e.start_time)

                if all_events:
                    event_text = "Calendar Events (Personal + Work):\n"
                    calendar_sources = []  # Track individual events for source links
                    for e in all_events:
                        start = e.start_time.strftime("%Y-%m-%d %H:%M") if e.start_time else "TBD"
                        account_label = f"[{e.source_account}]" if e.source_account else ""
                        event_text += f"- {e.title} ({start}) {account_label}"
                        if e.attendees:
                            event_text += f" with {', '.join(e.attendees[:3])}"
                        event_text += "\n"
                        # Store event for source linking
                        calendar_sources.append({
                            "title": e.title,
                            "start_time": start,
                            "html_link": e.html_link,
                            "source_account": e.source_account
                        })
                    extra_context.append({
                        "source": "calendar",
                        "content": event_text,
                        "events": calendar_sources  # Include event links
                    })
                    print(f"  Total: {len(all_events)} calendar events from both accounts")

            # Handle drive queries - query both personal and work accounts
            if "drive" in routing_result.sources:
                print("FETCHING DRIVE DATA (both personal and work, parallel)...")

                # Extract keywords for search
                keywords = extract_search_keywords(request.question)
                search_term = " ".join(keywords) if keywords else None
                print(f"  Search keywords: {keywords}")

                name_matched_files = []  # Files matching by name (higher priority)
                content_matched_files = []  # Files matching by content
                seen_file_ids = set()

                # Parallel fetch from both accounts
                drive_results = await asyncio.gather(
                    _fetch_drive_account(GoogleAccount.PERSONAL, search_term),
                    _fetch_drive_account(GoogleAccount.WORK, search_term),
                    return_exceptions=True
                )
                for result in drive_results:
                    if isinstance(result, Exception):
                        print(f"  Drive fetch error: {result}")
                    else:
                        account, name_files, content_files = result
                        # Track name matches separately (higher priority for reading content)
                        for f in name_files:
                            if f.file_id not in seen_file_ids:
                                seen_file_ids.add(f.file_id)
                                name_matched_files.append(f)
                        for f in content_files:
                            if f.file_id not in seen_file_ids:
                                seen_file_ids.add(f.file_id)
                                content_matched_files.append(f)
                        if name_files or content_files:
                            print(f"  Found {len(name_files)} by name, {len(content_files)} by content from {account} drive")

                # Prioritize name matches first, then content matches
                all_files = name_matched_files + content_matched_files
                print(f"  Prioritizing {len(name_matched_files)} name-matched files")

                # Adaptive retrieval settings
                INITIAL_MAX_FILES = 2  # Read content from 2 files initially
                INITIAL_CHAR_LIMIT = 1000  # 1000 chars per file initially
                EXPANDED_CHAR_LIMIT = 4000  # Can expand to 4000 chars on request

                if all_files:
                    drive_text = "Google Drive Files:\n"
                    files_with_content = 0

                    # Track all available files for potential follow-up reads
                    available_for_deeper_read = []

                    for f in all_files:
                        name = f.name if hasattr(f, 'name') else f.get('name', 'Unknown')
                        mime = f.mime_type if hasattr(f, 'mime_type') else f.get('mimeType', 'file')
                        account = f.source_account if hasattr(f, 'source_account') else ''
                        file_id = f.file_id if hasattr(f, 'file_id') else f.get('id', '')

                        # Track file for potential deeper reading
                        available_for_deeper_read.append({
                            "name": name,
                            "file_id": file_id,
                            "mime_type": mime,
                            "account": account
                        })

                        drive_text += f"\n### {name} [{account}]\n"

                        # For Google Docs/Sheets, fetch actual content (limited initially)
                        if files_with_content < INITIAL_MAX_FILES and file_id:
                            try:
                                account_type = GoogleAccount.WORK if account == 'work' else GoogleAccount.PERSONAL
                                drive_for_content = DriveService(account_type)
                                content = drive_for_content.get_file_content(file_id, mime)
                                if content:
                                    # Initial read is limited to INITIAL_CHAR_LIMIT
                                    if len(content) > INITIAL_CHAR_LIMIT:
                                        content = content[:INITIAL_CHAR_LIMIT] + f"\n... [truncated - {len(content)} total chars available, use [EXPAND:{name}] to read more]"
                                    drive_text += f"{content}\n"
                                    files_with_content += 1
                                    print(f"    Read {min(len(content), INITIAL_CHAR_LIMIT)} chars from: {name}")
                            except Exception as e:
                                print(f"    Could not read {name}: {e}")
                                drive_text += f"(Could not read content)\n"
                        else:
                            drive_text += f"(Preview not loaded - use [READ_MORE:{name}] to read this document)\n"

                    # Add instructions for adaptive retrieval
                    if len(all_files) > INITIAL_MAX_FILES:
                        unread_files = [f["name"] for f in available_for_deeper_read[INITIAL_MAX_FILES:]]
                        drive_text += f"\n---\nAdditional documents available (not yet read): {', '.join(unread_files)}\n"
                        drive_text += "Use [READ_MORE:filename] to read any unread document, or [EXPAND:filename] to get more content from a truncated document.\n"

                    extra_context.append({"source": "drive", "content": drive_text})
                    # Store available files for follow-up (will be used by adaptive retrieval)
                    extra_context.append({"source": "_drive_files_available", "files": available_for_deeper_read})
                    print(f"  Total: {len(all_files)} drive files, {files_with_content} with initial content")

            # Handle gmail queries - query both personal and work accounts
            if "gmail" in routing_result.sources and "gmail" not in skipped_sources:
                print("FETCHING GMAIL DATA (both personal and work, parallel)...")
                from api.services.entity_resolver import get_entity_resolver

                # Extract keywords for search
                keywords = extract_search_keywords(request.question)
                search_term = " ".join(keywords) if keywords else None
                print(f"  Search keywords: {keywords}")

                # Resolve person name to email for targeted search
                person_email = None
                is_sent_to = False  # Whether query is about emails sent TO the person
                person_name = query_router._extract_person_name(request.question)
                if person_name:
                    print(f"  Detected person name: {person_name}")
                    try:
                        resolver = get_entity_resolver()
                        result = resolver.resolve(name=person_name)
                        if result and result.entity:
                            # Get primary email from entity
                            entity = result.entity
                            if entity.emails:
                                person_email = entity.emails[0]
                                print(f"  Resolved to email: {person_email}")
                            elif entity.email:
                                person_email = entity.email
                                print(f"  Resolved to email: {person_email}")
                    except Exception as e:
                        print(f"  Entity resolution error: {e}")

                    # Check if query is about emails sent TO the person
                    query_lower = request.question.lower()
                    if any(phrase in query_lower for phrase in [
                        "i sent", "sent to", "emailed to", "wrote to",
                        "email to", "message to", "i emailed", "i wrote"
                    ]):
                        is_sent_to = True
                        print(f"  Query is about emails SENT TO {person_name}")

                # Parallel fetch from both accounts
                gmail_results = await asyncio.gather(
                    _fetch_gmail_account(GoogleAccount.PERSONAL, person_email, is_sent_to, search_term),
                    _fetch_gmail_account(GoogleAccount.WORK, person_email, is_sent_to, search_term),
                    return_exceptions=True
                )
                all_messages = []
                for result in gmail_results:
                    if isinstance(result, Exception):
                        print(f"  Gmail fetch error: {result}")
                    else:
                        account, messages = result
                        all_messages.extend(messages)
                        print(f"  Found {len(messages)} emails from {account} gmail")

                if all_messages:
                    from zoneinfo import ZoneInfo
                    eastern = ZoneInfo("America/New_York")

                    email_text = "Recent Emails:\n"
                    for m in all_messages:
                        sender = m.sender if hasattr(m, 'sender') else m.get('from', 'Unknown')
                        recipient = m.to if hasattr(m, 'to') else m.get('to', '')
                        subject = m.subject if hasattr(m, 'subject') else m.get('subject', 'No subject')
                        snippet = m.snippet if hasattr(m, 'snippet') else m.get('snippet', '')
                        body = m.body if hasattr(m, 'body') else m.get('body', '')
                        account = m.source_account if hasattr(m, 'source_account') else ''

                        # Convert to Eastern time
                        date_str = ''
                        if hasattr(m, 'date') and m.date:
                            try:
                                eastern_time = m.date.astimezone(eastern)
                                date_str = eastern_time.strftime('%Y-%m-%d %I:%M %p ET')
                            except Exception:
                                date_str = m.date.strftime('%Y-%m-%d %H:%M')

                        email_text += f"- From: {sender} [{account}]\n"
                        if recipient:
                            email_text += f"  To: {recipient}\n"
                        email_text += f"  Subject: {subject}\n"
                        if date_str:
                            email_text += f"  Date: {date_str}\n"
                        # Show full body if available, otherwise snippet
                        # v3: Use adaptive limit based on fetch_depth
                        if body:
                            body_preview = body[:email_char_limit] + "..." if len(body) > email_char_limit else body
                            email_text += f"  Body:\n{body_preview}\n"
                        elif snippet:
                            email_text += f"  Preview: {snippet[:200]}...\n"
                    extra_context.append({"source": "gmail", "content": email_text})
                    print(f"  Total: {len(all_messages)} emails from both accounts")

            # Handle slack queries - search Slack DMs and channels
            if "slack" in routing_result.sources:
                print("SEARCHING SLACK (async)...")
                slack_results = await _fetch_slack(request.question, top_k=10)

                if slack_results:
                    slack_text = "\n\n### Slack Messages\n\n"
                    for msg in slack_results:
                        channel_name = msg.get("channel_name", "Unknown")
                        user_name = msg.get("user_name", "Unknown")
                        timestamp = msg.get("timestamp", "")
                        content = msg.get("content", "")

                        # Parse timestamp for display
                        try:
                            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                            date_str = dt.strftime("%Y-%m-%d %H:%M")
                        except:
                            date_str = timestamp[:10] if timestamp else ""

                        slack_text += f"**{channel_name}** - {user_name} ({date_str}):\n"
                        slack_text += f"  {content[:500]}{'...' if len(content) > 500 else ''}\n\n"

                    extra_context.append({"source": "slack", "content": slack_text})
                    print(f"  Found {len(slack_results)} Slack messages")
                else:
                    print("  No Slack messages found")

            # Handle vault queries (always include as fallback)
            if "vault" in routing_result.sources or not routing_result.sources or not extra_context:
                # v3: Use adaptive chunk limit based on fetch_depth
                effective_vault_limit = vault_chunk_limit

                # Check for date context in query
                date_filter = extract_date_context(request.question)
                if date_filter:
                    print(f"DATE CONTEXT DETECTED: {date_filter}")

                # Use async vault fetch
                chunks = await _fetch_vault(request.question, effective_vault_limit, date_filter)

                # Log search results
                print(f"\nVAULT SEARCH RESULTS (top {len(chunks)}):")
                for i, chunk in enumerate(chunks):
                    fn = chunk.get('file_name', 'unknown')
                    score = chunk.get('score', 0)
                    semantic = chunk.get('semantic_score', 0)
                    recency = chunk.get('recency_score', 0)
                    mod_date = chunk.get('modified_date', 'unknown')
                    print(f"  {i+1}. {fn} ({mod_date})")
                    print(f"      combined={score:.3f} semantic={semantic:.3f} recency={recency:.3f}")

            # Handle people queries - generate stakeholder briefings + message history
            if "people" in routing_result.sources:
                print("PROCESSING PEOPLE QUERY...")

                # Use router's extracted name (from route()), fall back to re-extraction
                person_name = routing_result.extracted_person_name or query_router._extract_person_name(request.question)
                person_email = None
                entity_id = None

                if person_name:
                    print(f"  Extracted person name: {person_name}")

                    # Search calendar for person's email (7 days back and forward)
                    for account_type in [GoogleAccount.PERSONAL, GoogleAccount.WORK]:
                        try:
                            calendar = CalendarService(account_type)
                            events = calendar.search_events(
                                attendee=person_name,
                                days_forward=7,
                                days_back=7
                            )
                            for event in events:
                                for attendee in event.attendees:
                                    if '@' in attendee:
                                        # Check if name appears in email or we have a name match
                                        if person_name.lower() in attendee.lower():
                                            person_email = attendee
                                            break
                                if person_email:
                                    break
                            if person_email:
                                break
                        except Exception as e:
                            print(f"  Calendar search error ({account_type.value}): {e}")

                    if person_email:
                        print(f"  Found email from calendar: {person_email}")

                    # Resolve entity to get entity_id for message queries
                    relationship_summary = None
                    try:
                        from api.services.entity_resolver import get_entity_resolver
                        resolver = get_entity_resolver()
                        result = resolver.resolve(name=person_name, email=person_email)
                        if result:
                            entity_id = result.entity.id
                            print(f"  Resolved entity: {result.entity.canonical_name} ({entity_id})")

                            # Fetch relationship context for smart routing
                            from api.services.relationship_summary import (
                                get_relationship_summary,
                                format_relationship_context,
                            )
                            relationship_summary = get_relationship_summary(entity_id)
                            if relationship_summary:
                                print(f"  Relationship strength: {relationship_summary.relationship_strength}/100")
                                print(f"  Active channels: {relationship_summary.active_channels or 'none'}")
                                print(f"  Primary channel: {relationship_summary.primary_channel}")

                                # Add relationship context for synthesis
                                context_str = format_relationship_context(relationship_summary)
                                extra_context.append({
                                    "source": "relationship_context",
                                    "content": context_str,
                                })
                    except Exception as e:
                        print(f"  Entity resolution error: {e}")

                    # Check if query asks for specific message context
                    start_date, end_date = extract_message_date_range(request.question)
                    search_term = extract_message_search_terms(request.question, person_name)

                    # Determine if we should auto-query messages based on active channels
                    # Auto-query if: (1) explicit date/search, OR (2) imessage/whatsapp is active
                    active_channels = relationship_summary.active_channels if relationship_summary else []
                    should_query_messages = (
                        entity_id and (
                            start_date or end_date or search_term or
                            "imessage" in active_channels or
                            "whatsapp" in active_channels
                        )
                    )

                    if should_query_messages:
                        try:
                            from api.services.imessage import query_person_messages

                            # If no explicit date range but auto-querying, use last 30 days
                            effective_start = start_date
                            effective_end = end_date
                            if not start_date and not end_date and not search_term:
                                from datetime import datetime, timedelta
                                effective_start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
                                print(f"  Auto-querying messages (active channel): last 30 days")
                            else:
                                print(f"  Querying messages: dates={start_date} to {end_date}, search={search_term}")

                            # v3: Use message_limit from fetch_depth (set at top of function)
                            # Explicit queries override with full context
                            effective_message_limit = message_limit
                            if start_date or search_term:
                                effective_message_limit = 200  # Explicit queries get full context
                            print(f"  Message limit: {effective_message_limit} (depth={routing_result.fetch_depth})")

                            msg_result = query_person_messages(
                                entity_id=entity_id,
                                search_term=search_term,
                                start_date=effective_start,
                                end_date=effective_end,
                                limit=effective_message_limit,
                            )

                            if msg_result["count"] > 0:
                                date_info = ""
                                if msg_result["date_range"]:
                                    dr = msg_result["date_range"]
                                    date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})"

                                extra_context.append({
                                    "source": "imessage",
                                    "content": f"## Text Message History with {person_name}{date_info}\n\n{msg_result['formatted']}",
                                    "count": msg_result["count"],
                                })
                                print(f"  Found {msg_result['count']} messages in range")
                            else:
                                print(f"  No messages found for query")
                        except Exception as e:
                            print(f"  Message query error: {e}")
                            logger.error(f"Failed to query messages for {person_name}: {e}")

                    # Fetch photos if photos source is requested
                    if "photos" in routing_result.sources and entity_id:
                        try:
                            from api.services.interaction_store import get_interaction_store
                            interaction_store = get_interaction_store()
                            photo_interactions = interaction_store.get_for_person(
                                entity_id,
                                source_type="photos",
                                limit=20,
                            )
                            if photo_interactions:
                                photo_lines = []
                                for pi in photo_interactions:
                                    ts = pi.timestamp.strftime("%Y-%m-%d %H:%M") if pi.timestamp else "unknown date"
                                    photo_lines.append(f"- {ts}")
                                photo_summary = f"## Photos with {person_name}\n\nFound {len(photo_interactions)} photos (showing most recent):\n" + "\n".join(photo_lines)
                                extra_context.append({
                                    "source": "photos",
                                    "content": photo_summary,
                                    "count": len(photo_interactions),
                                })
                                print(f"  Found {len(photo_interactions)} photos with {person_name}")
                            else:
                                print(f"  No photos found with {person_name}")
                        except Exception as e:
                            print(f"  Photos query error: {e}")
                            logger.error(f"Failed to query photos for {person_name}: {e}")

                    # Generate briefing (always include for context)
                    try:
                        briefing_service = get_briefings_service()
                        briefing_result = await briefing_service.generate_briefing(
                            person_name,
                            email=person_email
                        )

                        if briefing_result.get("status") == "success":
                            briefing_text = briefing_result.get("briefing", "")
                            extra_context.append({
                                "source": "people_briefing",
                                "content": f"## Stakeholder Briefing: {person_name}\n\n{briefing_text}",
                                "metadata": briefing_result.get("metadata", {})
                            })
                            print(f"  Generated briefing with {briefing_result.get('notes_count', 0)} notes")
                        else:
                            print(f"  Briefing failed: {briefing_result.get('message')}")
                    except Exception as e:
                        print(f"  Briefing generation error: {e}")
                        logger.error(f"Failed to generate briefing for {person_name}: {e}")

            print(f"{'='*60}\n")

            # Collect sources
            sources = []
            vault_prefix = str(settings.vault_path) + "/"
            if chunks:
                seen_files = set()
                for chunk in chunks:
                    # Metadata is spread directly on chunk, not nested
                    file_name = chunk.get('file_name', '')
                    file_path = chunk.get('file_path', '')
                    if file_name and file_name not in seen_files:
                        seen_files.add(file_name)
                        # Compute relative path from vault for Obsidian links
                        if file_path.startswith(vault_prefix):
                            obsidian_path = file_path[len(vault_prefix):]
                        else:
                            obsidian_path = file_name  # Fallback to filename
                        sources.append({
                            'file_name': file_name,
                            'file_path': file_path,
                            'obsidian_path': obsidian_path,
                            'source_type': 'vault',
                        })

            # Add calendar event sources with Google Calendar links
            for ctx in extra_context:
                if ctx.get("source") == "calendar" and ctx.get("events"):
                    for event in ctx["events"]:
                        sources.append({
                            'file_name': f"📅 {event['title']} ({event['start_time']})",
                            'source_type': 'calendar',
                            'url': event.get('html_link'),
                            'source_account': event.get('source_account'),
                        })
                # Add iMessage source
                elif ctx.get("source") == "imessage":
                    msg_count = ctx.get("count", 0)
                    sources.insert(0, {  # Put at beginning since it's most relevant
                        'file_name': f"💬 Text Messages ({msg_count} messages)",
                        'source_type': 'imessage',
                    })
                # Add Slack source
                elif ctx.get("source") == "slack":
                    sources.insert(0, {
                        'file_name': "💬 Slack Messages",
                        'source_type': 'slack',
                    })
                # Add Photos source
                elif ctx.get("source") == "photos":
                    photo_count = ctx.get("count", 0)
                    sources.insert(0, {
                        'file_name': f"📷 Photos ({photo_count} photos)",
                        'source_type': 'photos',
                    })

            # Send sources to client
            if request.include_sources:
                yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

            # Construct prompt with all context
            # Add extra context (calendar/drive/gmail) to chunks
            # Skip internal metadata entries (prefixed with _)
            for ctx in extra_context:
                if ctx.get("source", "").startswith("_"):
                    continue  # Skip internal metadata like _drive_files_available
                chunks.insert(0, {
                    "content": ctx["content"],
                    "file_name": f"[{ctx['source'].upper()}]",
                    "file_path": ctx["source"],
                    "metadata": {"source": ctx["source"]}
                })

            # Use conversation_history we already retrieved earlier for follow-up expansion
            if conversation_history:
                print(f"Including {len(conversation_history)} messages of conversation history for synthesis")

            # v3: Build confidence metadata for synthesis
            confidence_metadata = None
            if "people" in routing_result.sources:
                # Extract relationship context if available
                rel_context = None
                for ctx in extra_context:
                    if ctx.get("source") == "relationship_context":
                        rel_context = ctx
                        break

                # Calculate data quality metrics
                vault_chunk_count = len([c for c in chunks if c.get("metadata", {}).get("source") != "relationship_context"])
                message_count = sum(ctx.get("count", 0) for ctx in extra_context if ctx.get("source") == "imessage")

                confidence_metadata = {
                    "routing_confidence": routing_result.confidence,
                    "sources_queried": routing_result.sources,
                    "vault_chunks": vault_chunk_count,
                    "message_count": message_count,
                }
                if rel_context:
                    # Parse relationship strength from context if available
                    confidence_metadata["relationship_context_available"] = True

                print(f"  Confidence metadata: {confidence_metadata}")

                # Add confidence context to first chunk for Claude's awareness
                confidence_block = f"""## Query Confidence
- Routing confidence: {routing_result.confidence:.0%}
- Sources queried: {', '.join(routing_result.sources)}
- Vault chunks found: {vault_chunk_count}
- Messages found: {message_count}

Note: If data is sparse, acknowledge limitations. If relationship context is rich, synthesize deeply.
"""
                chunks.insert(0, {
                    "content": confidence_block,
                    "file_name": "[SYSTEM_CONTEXT]",
                    "file_path": "_system",
                    "metadata": {"source": "_system"}
                })

            prompt = construct_prompt(request.question, chunks, conversation_history=conversation_history)

            # Prepare attachments for synthesizer (convert Pydantic models to dicts)
            attachments_for_api = None
            if request.attachments:
                attachments_for_api = [
                    {
                        "filename": att.filename,
                        "media_type": att.media_type,
                        "data": att.data
                    }
                    for att in request.attachments
                ]

            # Stream from Claude with adaptive retrieval support
            synthesizer = get_synthesizer()
            full_response = ""

            # Get available files for adaptive retrieval (if any)
            available_files = {}
            for ctx in extra_context:
                if ctx.get("source") == "_drive_files_available":
                    for f in ctx.get("files", []):
                        available_files[f["name"]] = f

            async for chunk in synthesizer.stream_response(prompt, attachments=attachments_for_api):
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    # Record usage to database for historical tracking
                    usage_store = get_usage_store()
                    usage_store.record_usage(
                        model=chunk.get("model", "sonnet"),
                        input_tokens=chunk.get("input_tokens", 0),
                        output_tokens=chunk.get("output_tokens", 0),
                        cost_usd=chunk.get("cost_usd", 0.0),
                        conversation_id=conversation_id
                    )
                    yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    full_response += chunk
                    yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                await asyncio.sleep(0)

            # Check for adaptive retrieval requests in the response
            read_more_pattern = r'\[READ_MORE:([^\]]+)\]'
            expand_pattern = r'\[EXPAND:([^\]]+)\]'

            read_more_matches = re.findall(read_more_pattern, full_response)
            expand_matches = re.findall(expand_pattern, full_response)

            if (read_more_matches or expand_matches) and available_files:
                print(f"ADAPTIVE RETRIEVAL: Detected requests - READ_MORE: {read_more_matches}, EXPAND: {expand_matches}")
                yield f"data: {json.dumps({'type': 'status', 'message': 'Fetching additional document content...'})}\n\n"

                # Fetch additional content
                additional_content = []
                files_fetched = 0
                MAX_FOLLOW_UP_FILES = 2

                for filename in (read_more_matches + expand_matches)[:MAX_FOLLOW_UP_FILES]:
                    # Find the file in available files (fuzzy match)
                    matched_file = None
                    for name, file_info in available_files.items():
                        if filename.lower() in name.lower() or name.lower() in filename.lower():
                            matched_file = file_info
                            break

                    if matched_file and files_fetched < MAX_FOLLOW_UP_FILES:
                        try:
                            account_type = GoogleAccount.WORK if matched_file["account"] == 'work' else GoogleAccount.PERSONAL
                            drive = DriveService(account_type)
                            content = drive.get_file_content(matched_file["file_id"], matched_file["mime_type"])
                            if content:
                                # Expanded read gets up to 4000 chars
                                if len(content) > 4000:
                                    content = content[:4000] + "\n... [truncated at 4000 chars]"
                                additional_content.append(f"\n### Expanded: {matched_file['name']}\n{content}")
                                files_fetched += 1
                                print(f"  Fetched expanded content for: {matched_file['name']} ({len(content)} chars)")
                        except Exception as e:
                            print(f"  Failed to fetch {filename}: {e}")

                if additional_content:
                    # Make a follow-up call with the additional content
                    follow_up_prompt = f"""Based on your previous response, here is the additional document content you requested:

{chr(10).join(additional_content)}

Please continue your response, incorporating this additional information. Do NOT repeat your previous response - just provide the additional insights from this new content."""

                    separator = '\n\n---\n*Additional content retrieved:*\n\n'
                    yield f"data: {json.dumps({'type': 'content', 'content': separator})}\n\n"

                    async for chunk in synthesizer.stream_response(follow_up_prompt, attachments=None):
                        if isinstance(chunk, dict) and chunk.get("type") == "usage":
                            # Record usage to database for historical tracking
                            usage_store = get_usage_store()
                            usage_store.record_usage(
                                model=chunk.get("model", "sonnet"),
                                input_tokens=chunk.get("input_tokens", 0),
                                output_tokens=chunk.get("output_tokens", 0),
                                cost_usd=chunk.get("cost_usd", 0.0),
                                conversation_id=conversation_id
                            )
                            yield f"data: {json.dumps(chunk)}\n\n"
                        else:
                            full_response += chunk
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0)

            # Save assistant response with enhanced routing metadata
            routing_metadata = {
                "sources": effective_sources,
                "reasoning": routing_result.reasoning,
                "fetch_depth": routing_result.fetch_depth,
            }
            if skipped_sources:
                routing_metadata["skipped_sources"] = skipped_sources
            if routing_result.extracted_person_name:
                routing_metadata["person"] = routing_result.extracted_person_name

            store.add_message(
                conversation_id,
                "assistant",
                full_response,
                sources=sources,
                routing=routing_metadata
            )
            print(f"Saved assistant response ({len(full_response)} chars)")

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@router.post("/save-to-vault")
async def save_to_vault(request: SaveToVaultRequest):
    """
    Save conversation to vault as a note.

    Supports two modes:
    1. Full conversation mode: provide conversation_id to save entire thread
    2. Single Q&A mode: provide question and answer (backward compatible)

    Additional options:
    - title: Override auto-generated title
    - folder: Override auto-detected folder
    - tags: Include specific tags in frontmatter
    - guidance: Custom instructions for synthesis
    - include_sources: Include source references in prompt
    - include_raw_qa: Append raw conversation to note
    """
    # Determine content source: conversation or single Q&A
    conversation_text = None
    raw_messages = []

    if request.full_conversation and request.conversation_id:
        # Full conversation mode
        store = get_store()
        messages = store.get_messages(request.conversation_id)
        if not messages:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_text = _format_messages_for_synthesis(messages, request.include_sources)
        raw_messages = messages
    elif request.question and request.answer and request.question.strip() and request.answer.strip():
        # Single Q&A mode (backward compatible)
        conversation_text = f"Question: {request.question}\n\nAnswer: {request.answer}"
        # Create fake message objects for raw Q&A if needed
        from api.services.conversation_store import Message
        raw_messages = [
            Message(id="", conversation_id="", role="user", content=request.question,
                    created_at=datetime.now(), sources=None, routing=None),
            Message(id="", conversation_id="", role="assistant", content=request.answer,
                    created_at=datetime.now(), sources=None, routing=None),
        ]
    else:
        raise HTTPException(
            status_code=400,
            detail="Either conversation_id or both question and answer are required"
        )

    try:
        synthesizer = get_synthesizer()

        # Build synthesis prompt
        prompt_parts = [
            "Based on this conversation, create a well-structured note for my Obsidian vault.",
            "",
            "Conversation:",
            conversation_text,
            "",
        ]

        # Add custom guidance if provided
        if request.guidance:
            prompt_parts.extend([
                "Additional guidance:",
                request.guidance,
                "",
            ])

        # Add tags hint if provided
        if request.tags:
            prompt_parts.extend([
                f"Include these tags in the frontmatter: {', '.join(request.tags)}",
                "",
            ])

        prompt_parts.extend([
            "Create a note with:",
            "1. A clear, concise title (not 'Q&A' or 'Conversation')",
            "2. YAML frontmatter with: created date, source: lifeos, relevant tags",
            "3. A TL;DR section at the top",
            "4. Well-organized content (not just the raw Q&A)",
            "5. Any relevant insights or key takeaways",
            "",
            "Output ONLY the markdown content for the note, starting with the frontmatter.",
        ])

        save_prompt = "\n".join(prompt_parts)

        # Get synthesized note content
        note_content = await synthesizer.get_response(save_prompt)

        # Append raw Q&A if requested
        if request.include_raw_qa and raw_messages:
            note_content += _format_raw_qa_section(raw_messages)

        # Determine title: user override or extract from content
        if request.title:
            title = request.title
        else:
            # Extract title from frontmatter or first heading
            lines = note_content.split('\n')
            title = "LifeOS Note"
            for line in lines:
                if line.startswith('# '):
                    title = line[2:].strip()
                    break
                if line.startswith('title:'):
                    title = line.split(':', 1)[1].strip().strip('"\'')
                    break

        # Clean filename
        safe_title = "".join(c for c in title if c.isalnum() or c in ' -_').strip()
        safe_title = safe_title[:50]  # Limit length
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        filename = f"{safe_title} ({timestamp}).md"

        # Determine folder: user override or auto-detect
        if request.folder:
            folder = request.folder
        else:
            folder = "LifeOS/Research"  # Default
            lower_content = note_content.lower()
            if any(word in lower_content for word in ['meeting', 'calendar', 'schedule']):
                folder = "LifeOS/Meetings"
            elif any(word in lower_content for word in ['todo', 'action', 'task']):
                folder = "LifeOS/Actions"
            elif any(word in lower_content for word in ['person', 'about', 'briefing']):
                folder = "LifeOS/People"

        # Write to vault
        vault_path = settings.vault_path
        note_path = vault_path / folder / filename

        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note_content)

        # Return obsidian link
        from urllib.parse import quote
        vault_name = quote(vault_path.name)
        obsidian_url = f"obsidian://open?vault={vault_name}&file={folder}/{filename}"

        return {
            "status": "saved",
            "path": str(note_path),
            "obsidian_url": obsidian_url,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")
