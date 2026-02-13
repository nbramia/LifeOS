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
from api.services.synthesizer import get_synthesizer
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
    classify_action_intent,
    TaskIntentType,
    ActionIntent,
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


async def _fetch_web(query: str) -> str:
    """Fetch web search results and format for context."""
    try:
        from api.services.web_search import search_web_with_synthesis
        synthesized, results = await search_web_with_synthesis(query)
        return synthesized
    except Exception as e:
        logger.warning(f"Web search error: {e}")
        return ""


async def _execute_action_after(
    action_type: str,
    query: str,
    synthesis_result: str,
    conversation_history: list,
    conversation_id: str,
) -> Optional[str]:
    """
    Execute an action after synthesis completes.

    Uses the synthesis result as context for parameter extraction.

    Args:
        action_type: Type of action ("task_create", "reminder_create", "compose")
        query: Original user query
        synthesis_result: The synthesized answer to use as context
        conversation_history: Conversation history for context
        conversation_id: Current conversation ID

    Returns:
        Confirmation message or None if failed.
    """
    # Combine original query + synthesis for better param extraction
    combined_context = f"Based on this information:\n{synthesis_result}\n\nOriginal request: {query}"

    if action_type == "reminder_create":
        try:
            if not settings.telegram_enabled:
                return None

            params = await extract_reminder_params(combined_context, conversation_history)
            if params:
                from api.services.reminder_store import get_reminder_store
                reminder_store = get_reminder_store()
                reminder = reminder_store.create(
                    name=params.get("name", "Reminder"),
                    schedule_type=params["schedule_type"],
                    schedule_value=params["schedule_value"],
                    message_type=params.get("message_type", "static"),
                    message_content=params.get("message_content", ""),
                    enabled=True,
                    timezone=params.get("timezone", "America/New_York"),
                )
                display_time = params.get("display_time", params["schedule_value"])
                return f"---\nI've also set a reminder for **{display_time}**: {reminder.name}"
        except Exception as e:
            logger.error(f"Failed to create reminder after synthesis: {e}")
            return None

    elif action_type == "task_create":
        try:
            params = await extract_task_params(combined_context, conversation_history)
            if params:
                from api.services.task_manager import get_task_manager
                task_manager = get_task_manager()
                task = task_manager.create(
                    description=params["description"],
                    context=params.get("context", "Inbox"),
                    priority=params.get("priority", ""),
                    due_date=params.get("due_date"),
                    tags=params.get("tags", []),
                )
                return f"---\nI've added a task: **{task.description}** ({task.context})"
        except Exception as e:
            logger.error(f"Failed to create task after synthesis: {e}")
            return None

    elif action_type == "compose":
        try:
            params = await extract_draft_params(combined_context, conversation_history)
            if params and params.get("to"):
                account_str = params.get("account", "personal").lower()
                account_type = GoogleAccount.WORK if account_str == "work" else GoogleAccount.PERSONAL
                gmail = GmailService(account_type)
                draft = gmail.create_draft(
                    to=params["to"],
                    subject=params.get("subject", ""),
                    body=params.get("body", ""),
                )
                if draft:
                    gmail_url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft.draft_id}"
                    return f"---\nI've created a draft email to {params['to']}. [Open in Gmail]({gmail_url})"
        except Exception as e:
            logger.error(f"Failed to create draft after synthesis: {e}")
            return None

    return None


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


# =============================================================================
# LLM-Based Reminder Disambiguation (Fix #2)
# =============================================================================

def format_reminders_for_context(reminders: list) -> str:
    """
    Format reminders as a numbered list for LLM context injection.

    This allows the LLM to see ALL reminders and reason about which one
    the user is referring to, rather than relying on fuzzy string matching.
    """
    if not reminders:
        return "No active reminders."

    from api.services.reminder_store import _format_cron_human
    from api.services.time_parser import format_time_for_display

    lines = ["Current reminders:"]
    for i, r in enumerate(reminders, 1):
        if r.schedule_type == "cron":
            schedule = _format_cron_human(r.schedule_value, r.timezone or "America/New_York")
        else:
            try:
                trigger_dt = datetime.fromisoformat(r.schedule_value)
                schedule = format_time_for_display(trigger_dt)
            except (ValueError, TypeError):
                schedule = r.schedule_value

        status = "enabled" if r.enabled else "disabled"
        lines.append(f"{i}. \"{r.name}\" - {schedule} ({status})")

    return "\n".join(lines)


async def identify_reminder_with_llm(
    query: str,
    reminders: list,
    conversation_history: list = None,
) -> Optional[dict]:
    """
    Use LLM to identify which reminder the user is referring to.

    This provides better disambiguation than fuzzy string matching by:
    1. Showing the LLM the full list of reminders
    2. Letting it reason about context ("that one", "the daily one", etc.)
    3. Using conversation history for pronouns and references

    Returns dict with:
        - reminder_index: 1-based index into reminders list (or None if ambiguous)
        - confidence: 0.0-1.0
        - reason: Brief explanation
    """
    if not reminders:
        return None

    reminder_context = format_reminders_for_context(reminders)

    # Build conversation context if available
    conv_context = ""
    if conversation_history:
        recent_msgs = conversation_history[-4:]  # Last 2 exchanges
        context_parts = []
        for msg in recent_msgs:
            role = "User" if msg.role == "user" else "Assistant"
            context_parts.append(f"{role}: {msg.content[:200]}")
        if context_parts:
            conv_context = "\n\nRecent conversation:\n" + "\n".join(context_parts)

    prompt = f"""Given the user's request and the list of current reminders, identify which reminder they are referring to.

{reminder_context}{conv_context}

User request: "{query}"

Instructions:
- If the user mentions a specific name, number, or clear description, identify that reminder
- If the user says "that one", "it", "the reminder", use conversation context to infer
- If the user says "the first one", "number 2", etc., use the numbered list
- If truly ambiguous (multiple equally likely matches), return null for reminder_index

Return ONLY valid JSON:
{{"reminder_index": <1-based number or null>, "confidence": <0.0-1.0>, "reason": "<brief explanation>"}}"""

    try:
        synthesizer = get_synthesizer()
        response_text = await synthesizer.get_response(
            prompt,
            max_tokens=128,
            model_tier="haiku"  # Fast, cheap for identification
        )

        json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            return result
    except Exception as e:
        logger.error(f"Failed to identify reminder with LLM: {e}")

    return None


def format_reminder_selection_prompt(reminders: list, action: str) -> tuple[str, list[str]]:
    """
    Format a numbered list of reminders for user selection.

    Returns:
        - response_text: The message to show the user
        - reminder_ids: List of reminder IDs in display order
    """
    from api.services.reminder_store import _format_cron_human
    from api.services.time_parser import format_time_for_display

    lines = [f"Which reminder would you like to {action}?\n"]
    reminder_ids = []

    for i, r in enumerate(reminders, 1):
        if r.schedule_type == "cron":
            schedule = _format_cron_human(r.schedule_value, r.timezone or "America/New_York")
        else:
            try:
                trigger_dt = datetime.fromisoformat(r.schedule_value)
                schedule = format_time_for_display(trigger_dt)
            except (ValueError, TypeError):
                schedule = r.schedule_value

        lines.append(f"{i}. **{r.name}** - {schedule}")
        reminder_ids.append(r.id)

    lines.append("\nReply with the number.")
    return "\n".join(lines), reminder_ids


# =============================================================================
# Task Extraction & Matching Helpers
# =============================================================================

async def extract_task_params(query: str, conversation_history: list = None) -> Optional[dict]:
    """
    Use Claude to extract task parameters from a task creation request.

    Returns dict with:
        - description: Task description
        - context: Work, Personal, Finance, Health, Inbox, etc.
        - priority: high, medium, low, or ""
        - due_date: YYYY-MM-DD or None
        - tags: list of tag strings
        - has_reminder: bool — whether to also create a linked reminder
        - reminder_time: ISO datetime if has_reminder is true
    Or None if extraction fails.
    """
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    current_datetime = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    context = ""
    if conversation_history:
        recent_msgs = conversation_history[-4:]
        context_parts = []
        for msg in recent_msgs:
            context_parts.append(f"{msg.role}: {msg.content[:300]}")
        if context_parts:
            context = "\n\nConversation context:\n" + "\n".join(context_parts)

    extraction_prompt = f"""Extract task parameters from this request.{context}

Current date/time: {current_datetime}

User request: {query}

Return a JSON object with these fields:
- "description": Clear, concise task description (imperative form, e.g., "Call the dentist")
- "context": Category — one of: "Work", "Personal", "Finance", "Health", "Inbox" (default "Inbox" if unclear)
- "priority": "high", "medium", "low", or "" (empty if not specified)
- "due_date": YYYY-MM-DD if a due date is mentioned/implied, null otherwise. Resolve relative dates (e.g., "by Friday" → actual date)
- "tags": Array of lowercase tag strings inferred from content (e.g., ["tax", "schwab"] for "pull 1099 from Schwab")
- "has_reminder": true if user also wants a timed reminder (e.g., "and remind me Friday"), false otherwise
- "reminder_time": ISO datetime if has_reminder is true, null otherwise

Return ONLY valid JSON, no other text. Example:
{{"description": "Pull 1099 from Schwab", "context": "Finance", "priority": "medium", "due_date": "2026-02-15", "tags": ["tax", "schwab"], "has_reminder": false, "reminder_time": null}}"""

    try:
        synthesizer = get_synthesizer()
        response_text = await synthesizer.get_response(
            extraction_prompt,
            max_tokens=512,
            model_tier="haiku"
        )

        json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if json_match:
            params = json.loads(json_match.group())
            if params.get("description"):
                return params
    except Exception as e:
        logger.error(f"Failed to extract task params: {e}")

    return None


def find_task_by_topic(
    query: str,
    tasks: list,
) -> Optional[tuple]:
    """
    Find a task by topic using fuzzy matching on description.

    Returns:
        Tuple of (task, match_type) or None
        match_type: "exact", "fuzzy", "ambiguous"
    """
    if not tasks:
        return None

    query_lower = query.lower()

    # Extract topic from common patterns
    topic = None
    for pattern in [
        r'(?:task|to-?do|item)\s+(?:about|for|to)\s+(.+?)(?:\s+as|\s*$)',
        r'(?:mark|complete|finish|check off|delete|remove|update|change)\s+(?:the\s+)?(?:task|to-?do)?\s*(?:about|for|to)?\s*(.+?)(?:\s+as|\s*$)',
        r'"([^"]+)"',  # Quoted text
    ]:
        m = re.search(pattern, query_lower)
        if m:
            topic = m.group(1).strip()
            break

    if not topic:
        # Use the whole query minus common action words
        topic = re.sub(
            r'\b(mark|complete|finish|check|off|delete|remove|update|change|the|task|todo|to-do|about|done|as)\b',
            '', query_lower
        ).strip()

    if not topic:
        return None

    matches = []
    for task in tasks:
        desc_lower = task.description.lower()
        if topic in desc_lower:
            matches.append((task, "exact"))
        elif any(word in desc_lower for word in topic.split() if len(word) > 2):
            matches.append((task, "fuzzy"))

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        return (matches, "ambiguous")

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
                                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                        await asyncio.sleep(0.005)
                                    store.add_message(conversation_id, "assistant", response_text, routing=routing_metadata)
                                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                    return
                            else:
                                response_text = f"Selected reminder: **\"{reminder.name}\"**"

                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.005)
                            store.add_message(conversation_id, "assistant", response_text)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                else:
                    # Invalid number
                    response_text = f"Please enter a number between 1 and {len(conv_context.pending_selection_items)}."
                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

            # Unified intent classification — evaluates compose, task, and reminder
            # patterns simultaneously and picks the most specific match.
            action_intent = await classify_action_intent(request.question, conversation_history)
            _intent_handled = False

            if action_intent and action_intent.category == "compose":
                # ---- EMAIL COMPOSE (unchanged logic) ----
                print("DETECTED COMPOSE INTENT - handling email draft")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['gmail_draft'], 'reasoning': 'Email composition detected', 'latency_ms': 0})}\n\n"

                draft_params = await extract_draft_params(request.question, conversation_history)
                if draft_params:
                    try:
                        account_str = draft_params.get("account", "personal").lower()
                        account_type = GoogleAccount.WORK if account_str == "work" else GoogleAccount.PERSONAL

                        gmail = GmailService(account_type)
                        draft = gmail.create_draft(
                            to=draft_params["to"],
                            subject=draft_params.get("subject", ""),
                            body=draft_params.get("body", ""),
                        )

                        if draft:
                            gmail_url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft.draft_id}"
                            response_text = f"I've created a draft email for you:\n\n"
                            response_text += f"**To:** {draft.to}\n"
                            response_text += f"**Subject:** {draft.subject}\n"
                            response_text += f"**Account:** {draft.source_account}\n\n"
                            response_text += f"[Open draft in Gmail]({gmail_url})\n\n"
                            response_text += f"Review and send when ready."

                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.01)
                            store.add_message(conversation_id, "assistant", response_text)
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
                    print("Could not extract draft params, falling through to normal flow")

            elif action_intent and action_intent.category == "task":
                # ---- TASK MANAGEMENT ----
                print(f"DETECTED TASK INTENT: {action_intent.sub_type}")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['tasks'], 'reasoning': f'Task {action_intent.sub_type} detected', 'latency_ms': 0})}\n\n"

                from api.services.task_manager import get_task_manager
                task_manager = get_task_manager()

                # Handle TASK LIST
                if action_intent.sub_type == TaskIntentType.LIST.value:
                    # Parse natural language filters from query
                    q_lower = request.question.lower()
                    status_filter = None
                    context_filter = None
                    for s in ["done", "completed", "in_progress", "in progress", "blocked", "urgent", "deferred", "cancelled"]:
                        if s in q_lower:
                            status_filter = s.replace(" ", "_")
                            break
                    if not status_filter and ("open" in q_lower or "pending" in q_lower or "to-do" in q_lower or "todo" in q_lower):
                        status_filter = "todo"
                    for ctx in ["work", "personal", "finance", "health"]:
                        if ctx in q_lower:
                            context_filter = ctx.title()
                            break

                    tasks = task_manager.list_tasks(status=status_filter, context=context_filter)
                    if not status_filter:
                        # Default: show non-done tasks
                        tasks = [t for t in tasks if t.status not in ("done", "cancelled")]

                    if not tasks:
                        response_text = "You don't have any open tasks."
                    else:
                        from api.services.task_manager import STATUS_TO_SYMBOL
                        response_text = f"You have {len(tasks)} task(s):\n\n"
                        for t in tasks:
                            sym = STATUS_TO_SYMBOL.get(t.status, " ")
                            response_text += f"- [{sym}] **{t.description}**"
                            if t.due_date:
                                response_text += f" (due: {t.due_date})"
                            if t.priority:
                                response_text += f" [{t.priority}]"
                            response_text += f"\n  _{t.context}_"
                            if t.tags:
                                response_text += f" | {' '.join('#' + tg for tg in t.tags)}"
                            response_text += "\n"

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle TASK COMPLETE
                elif action_intent.sub_type == TaskIntentType.COMPLETE.value:
                    all_tasks = task_manager.list_tasks(status="todo") + task_manager.list_tasks(status="in_progress") + task_manager.list_tasks(status="urgent")
                    match_result = find_task_by_topic(request.question, all_tasks)

                    if match_result is None:
                        response_text = "I couldn't find a task to complete. Try saying \"list my tasks\" to see them."
                    elif match_result[1] == "ambiguous":
                        matches = match_result[0]
                        response_text = "I found multiple matching tasks. Which one?\n\n"
                        for t, _ in matches:
                            response_text += f"- {t.description}\n"
                    else:
                        task, _ = match_result
                        task_manager.complete(task.id)
                        response_text = f"Done! Marked **\"{task.description}\"** as complete."

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle TASK DELETE
                elif action_intent.sub_type == TaskIntentType.DELETE.value:
                    all_tasks = task_manager.list_tasks()
                    match_result = find_task_by_topic(request.question, all_tasks)

                    if match_result is None:
                        response_text = "I couldn't find a task to delete. Try saying \"list my tasks\" to see them."
                    elif match_result[1] == "ambiguous":
                        matches = match_result[0]
                        response_text = "I found multiple matching tasks. Which one?\n\n"
                        for t, _ in matches:
                            response_text += f"- {t.description}\n"
                    else:
                        task, _ = match_result
                        task_manager.delete(task.id)
                        response_text = f"I've deleted the task **\"{task.description}\"**."

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle TASK EDIT
                elif action_intent.sub_type == TaskIntentType.EDIT.value:
                    all_tasks = task_manager.list_tasks()
                    match_result = find_task_by_topic(request.question, all_tasks)

                    if match_result is None:
                        response_text = "I couldn't find a task to update. Try saying \"list my tasks\" to see them."
                    elif match_result[1] == "ambiguous":
                        matches = match_result[0]
                        response_text = "I found multiple matching tasks. Which one?\n\n"
                        for t, _ in matches:
                            response_text += f"- {t.description}\n"
                    else:
                        task, _ = match_result
                        # Use Claude to extract what changed
                        edit_params = await extract_task_params(request.question, conversation_history)
                        if edit_params:
                            updates = {}
                            if edit_params.get("priority"):
                                updates["priority"] = edit_params["priority"]
                            if edit_params.get("due_date"):
                                updates["due_date"] = edit_params["due_date"]
                            if edit_params.get("context") and edit_params["context"] != "Inbox":
                                updates["context"] = edit_params["context"]
                            if edit_params.get("tags"):
                                updates["tags"] = edit_params["tags"]
                            if updates:
                                task_manager.update(task.id, **updates)
                                response_text = f"I've updated **\"{task.description}\"**."
                            else:
                                response_text = "I couldn't determine what to change. Please be more specific."
                        else:
                            response_text = "I couldn't understand the update. Please try again."

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle TASK CREATE
                elif action_intent.sub_type == TaskIntentType.CREATE.value:
                    task_params = await extract_task_params(request.question, conversation_history)
                    if task_params:
                        try:
                            # Create the task
                            task = task_manager.create(
                                description=task_params["description"],
                                context=task_params.get("context", "Inbox"),
                                priority=task_params.get("priority", ""),
                                due_date=task_params.get("due_date"),
                                tags=task_params.get("tags", []),
                            )

                            # If user also wants a linked reminder
                            reminder_id = None
                            if task_params.get("has_reminder") and task_params.get("reminder_time"):
                                try:
                                    from api.services.reminder_store import get_reminder_store
                                    r_store = get_reminder_store()
                                    reminder = r_store.create(
                                        name=task_params["description"],
                                        schedule_type="once",
                                        schedule_value=task_params["reminder_time"],
                                        message_type="static",
                                        message_content=task_params["description"],
                                        timezone="America/New_York",
                                    )
                                    task_manager.update(task.id, reminder_id=reminder.id)
                                    reminder_id = reminder.id
                                except Exception as e:
                                    logger.warning(f"Failed to create linked reminder: {e}")

                            response_text = f"I've added a task to your list:\n\n"
                            response_text += f"**Task:** {task.description}\n"
                            response_text += f"**Context:** {task.context}"
                            if task.tags:
                                response_text += f" | **Tags:** {' '.join('#' + t for t in task.tags)}"
                            response_text += "\n"
                            if task.due_date:
                                response_text += f"**Due:** {task.due_date}\n"
                            if task.priority:
                                response_text += f"**Priority:** {task.priority}\n"
                            response_text += f"**File:** {task.context}.md\n"
                            if reminder_id:
                                response_text += f"\nI've also set a reminder for this task."

                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.005)
                            store.add_message(conversation_id, "assistant", response_text)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                        except Exception as e:
                            error_msg = f"Error creating task: {str(e)}"
                            logger.error(error_msg)
                            yield f"data: {json.dumps({'type': 'content', 'content': error_msg})}\n\n"
                            store.add_message(conversation_id, "assistant", error_msg)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                    else:
                        print("Could not extract task params, falling through to normal flow")

            elif action_intent and action_intent.category == "reminder":
                # ---- REMINDER MANAGEMENT (unchanged logic) ----
                reminder_intent_type = None
                for rit in ReminderIntentType:
                    if rit.value == action_intent.sub_type:
                        reminder_intent_type = rit
                        break
                if not reminder_intent_type:
                    reminder_intent_type = ReminderIntentType.CREATE

                print(f"DETECTED REMINDER INTENT: {reminder_intent_type.value}")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['reminder'], 'reasoning': f'Reminder {reminder_intent_type.value} detected', 'latency_ms': 0})}\n\n"

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
                conv_context = extract_context_from_history(conversation_history) if conversation_history else None

                # Handle LIST intent
                if reminder_intent_type == ReminderIntentType.LIST:
                    from api.services.reminder_store import _format_cron_human
                    all_reminders = reminder_store.list_all()
                    enabled_reminders = [r for r in all_reminders if r.enabled]

                    if not enabled_reminders:
                        response_text = "You don't have any active reminders."
                    else:
                        response_text = f"You have {len(enabled_reminders)} active reminder(s):\n\n"
                        for r in enabled_reminders:
                            if r.schedule_type == "cron":
                                schedule_desc = _format_cron_human(r.schedule_value, r.timezone or "America/New_York")
                            else:
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

                # Handle DELETE intent (Fix #2: LLM-based disambiguation)
                if reminder_intent_type == ReminderIntentType.DELETE:
                    all_reminders = reminder_store.list_all()
                    enabled_reminders = [r for r in all_reminders if r.enabled]

                    if not enabled_reminders:
                        response_text = "You don't have any active reminders to delete."
                        for chunk in response_text:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            await asyncio.sleep(0.005)
                        store.add_message(conversation_id, "assistant", response_text)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    # Use LLM to identify which reminder
                    llm_result = await identify_reminder_with_llm(
                        request.question,
                        enabled_reminders,
                        conversation_history
                    )

                    if llm_result and llm_result.get("reminder_index") and llm_result.get("confidence", 0) >= 0.7:
                        # High confidence match
                        idx = llm_result["reminder_index"] - 1  # Convert to 0-based
                        if 0 <= idx < len(enabled_reminders):
                            reminder = enabled_reminders[idx]
                            reminder_store.delete(reminder.id)
                            response_text = f"I've deleted the reminder **\"{reminder.name}\"**."
                        else:
                            response_text = "I couldn't identify which reminder to delete."
                    else:
                        # Ambiguous - show numbered list and store pending selection
                        response_text, reminder_ids = format_reminder_selection_prompt(enabled_reminders, "delete")
                        routing_metadata = {
                            "sources": ["reminder"],
                            "reasoning": "Awaiting reminder selection for delete",
                            "pending_selection": {
                                "type": "reminder",
                                "action": "delete",
                                "items": reminder_ids,
                            },
                        }
                        for chunk in response_text:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            await asyncio.sleep(0.005)
                        store.add_message(conversation_id, "assistant", response_text, routing=routing_metadata)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle EDIT intent (Fix #2: LLM-based disambiguation)
                if reminder_intent_type == ReminderIntentType.EDIT:
                    all_reminders = reminder_store.list_all()
                    enabled_reminders = [r for r in all_reminders if r.enabled]

                    if not enabled_reminders:
                        response_text = "You don't have any active reminders to edit."
                        for chunk in response_text:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            await asyncio.sleep(0.005)
                        store.add_message(conversation_id, "assistant", response_text)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    # Use LLM to identify which reminder
                    llm_result = await identify_reminder_with_llm(
                        request.question,
                        enabled_reminders,
                        conversation_history
                    )

                    reminder = None
                    if llm_result and llm_result.get("reminder_index") and llm_result.get("confidence", 0) >= 0.7:
                        # High confidence match
                        idx = llm_result["reminder_index"] - 1  # Convert to 0-based
                        if 0 <= idx < len(enabled_reminders):
                            reminder = enabled_reminders[idx]

                    if reminder:
                        # Found the reminder - now extract the new time
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
                            # Selected reminder but need new time
                            response_text = f"What time should I change **\"{reminder.name}\"** to?"
                            routing_metadata = {
                                "sources": ["reminder"],
                                "reasoning": "Awaiting new time for edit",
                                "created_reminder": {"id": reminder.id, "name": reminder.name},
                            }
                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.005)
                            store.add_message(conversation_id, "assistant", response_text, routing=routing_metadata)
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            return
                    else:
                        # Ambiguous - show numbered list and store pending selection
                        response_text, reminder_ids = format_reminder_selection_prompt(enabled_reminders, "edit")
                        routing_metadata = {
                            "sources": ["reminder"],
                            "reasoning": "Awaiting reminder selection for edit",
                            "pending_selection": {
                                "type": "reminder",
                                "action": "edit",
                                "items": reminder_ids,
                            },
                        }
                        for chunk in response_text:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            await asyncio.sleep(0.005)
                        store.add_message(conversation_id, "assistant", response_text, routing=routing_metadata)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    for chunk in response_text:
                        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        await asyncio.sleep(0.005)
                    store.add_message(conversation_id, "assistant", response_text)
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Handle CREATE intent
                if reminder_intent_type == ReminderIntentType.CREATE:
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
                                timezone=reminder_params.get("timezone", "America/New_York"),
                            )

                            display_time = reminder_params.get("display_time", "")
                            if not display_time and reminder.schedule_type == "cron":
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

                            for chunk in response_text:
                                yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.005)

                            routing_metadata = {
                                "sources": ["reminder"],
                                "reasoning": "Reminder created",
                                "created_reminder": {
                                    "id": reminder.id,
                                    "name": reminder.name,
                                },
                            }
                            store.add_message(
                                conversation_id, "assistant", response_text,
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
                        print("Could not extract reminder params, falling through to normal flow")

            elif action_intent and action_intent.category == "task_and_reminder":
                # ---- BOTH: create task AND linked reminder ----
                print("DETECTED TASK_AND_REMINDER INTENT — creating both")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['tasks', 'reminder'], 'reasoning': 'Task and reminder creation', 'latency_ms': 0})}\n\n"

                from api.services.task_manager import get_task_manager
                task_manager = get_task_manager()

                # Extract the underlying request from conversation context
                # The user may have said "Both" as a follow-up, so check history
                original_query = request.question
                if conversation_history and len(request.question.split()) <= 3:
                    # Short follow-up like "Both" — find the original request
                    for msg in reversed(conversation_history):
                        if msg.role == "user" and len(msg.content.split()) > 3:
                            original_query = msg.content
                            break

                task_params = await extract_task_params(original_query, conversation_history)
                if task_params:
                    try:
                        task = task_manager.create(
                            description=task_params["description"],
                            context=task_params.get("context", "Inbox"),
                            priority=task_params.get("priority", ""),
                            due_date=task_params.get("due_date"),
                            tags=task_params.get("tags", []),
                        )

                        # Create linked reminder
                        reminder_id = None
                        try:
                            from api.services.reminder_store import get_reminder_store
                            r_store = get_reminder_store()

                            reminder_time = task_params.get("reminder_time")
                            if not reminder_time:
                                # No time extracted — extract reminder params from original query
                                r_params = await extract_reminder_params(original_query, conversation_history)
                                if r_params:
                                    reminder = r_store.create(
                                        name=task_params["description"],
                                        schedule_type=r_params["schedule_type"],
                                        schedule_value=r_params["schedule_value"],
                                        message_type=r_params.get("message_type", "static"),
                                        message_content=task_params["description"],
                                        timezone=r_params.get("timezone", "America/New_York"),
                                    )
                                    reminder_id = reminder.id
                                else:
                                    # Default: remind tomorrow at 9am
                                    from zoneinfo import ZoneInfo
                                    eastern = ZoneInfo("America/New_York")
                                    tomorrow = (datetime.now(eastern) + timedelta(days=1)).replace(
                                        hour=9, minute=0, second=0, microsecond=0
                                    )
                                    reminder = r_store.create(
                                        name=task_params["description"],
                                        schedule_type="once",
                                        schedule_value=tomorrow.isoformat(),
                                        message_type="static",
                                        message_content=task_params["description"],
                                        timezone="America/New_York",
                                    )
                                    reminder_id = reminder.id
                            else:
                                reminder = r_store.create(
                                    name=task_params["description"],
                                    schedule_type="once",
                                    schedule_value=reminder_time,
                                    message_type="static",
                                    message_content=task_params["description"],
                                    timezone="America/New_York",
                                )
                                reminder_id = reminder.id

                            if reminder_id:
                                task_manager.update(task.id, reminder_id=reminder_id)
                        except Exception as e:
                            logger.warning(f"Failed to create linked reminder: {e}")

                        response_text = f"Done! I've created both:\n\n"
                        response_text += f"**Task:** {task.description}\n"
                        response_text += f"**Context:** {task.context}"
                        if task.tags:
                            response_text += f" | {' '.join('#' + t for t in task.tags)}"
                        response_text += "\n"
                        if task.due_date:
                            response_text += f"**Due:** {task.due_date}\n"
                        if reminder_id:
                            response_text += f"\n**Reminder** set to ping you about it."
                        else:
                            response_text += f"\nI added the task but couldn't create the reminder."

                        for chunk in response_text:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            await asyncio.sleep(0.005)
                        store.add_message(conversation_id, "assistant", response_text)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                    except Exception as e:
                        error_msg = f"Error creating task and reminder: {str(e)}"
                        logger.error(error_msg)
                        yield f"data: {json.dumps({'type': 'content', 'content': error_msg})}\n\n"
                        store.add_message(conversation_id, "assistant", error_msg)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                else:
                    print("Could not extract task params for task_and_reminder, falling through")

            elif action_intent and action_intent.category == "ambiguous_task_reminder":
                # ---- AMBIGUOUS: could be task or reminder ----
                print("DETECTED AMBIGUOUS TASK/REMINDER INTENT")
                response_text = "Should I add this as a **to-do** in your task list, or set a **timed reminder** to ping you about it, or both?"
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['clarification'], 'reasoning': 'Ambiguous task/reminder', 'latency_ms': 0})}\n\n"
                for chunk in response_text:
                    yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                    await asyncio.sleep(0.005)
                store.add_message(conversation_id, "assistant", response_text)
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            elif action_intent and action_intent.category == "code":
                # ---- CODE: requires Claude Code (terminal/filesystem/browser) ----
                print("DETECTED CODE INTENT - delegating to Claude Code")
                yield f"data: {json.dumps({'type': 'routing', 'sources': ['code'], 'reasoning': 'Action requires terminal/filesystem/browser access', 'latency_ms': 0})}\n\n"
                # Signal to caller (e.g., Telegram) that this needs Claude Code
                yield f"data: {json.dumps({'type': 'code_intent', 'task': request.question})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # =============================================================
            # Agentic synthesis path: Claude decides what to fetch
            # =============================================================
            from api.services.agent_loop import run_agent_loop
            from api.services.model_selector import classify_query_complexity

            # Expand follow-up queries with conversation context
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

            # Select model tier
            complexity = classify_query_complexity(effective_question)
            model_tier = complexity.recommended_model
            print(f"\n{'='*60}")
            print(f"QUERY: {request.question}")
            print(f"MODEL: {model_tier} ({complexity.reasoning})")
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

            yield f"data: {json.dumps({'type': 'routing', 'sources': ['agent'], 'reasoning': f'Agentic loop ({model_tier})', 'latency_ms': 0})}\n\n"

            # Consume the async generator from the agent loop
            agent_result = None
            async for event in run_agent_loop(
                question=effective_question,
                conversation_history=conversation_history,
                attachments=attachments_for_api,
                model_tier=model_tier,
                max_tool_rounds=5,
            ):
                if event["type"] == "text":
                    yield f"data: {json.dumps({'type': 'content', 'content': event['content']})}\n\n"
                elif event["type"] == "status":
                    yield f"data: {json.dumps({'type': 'status', 'message': event['message']})}\n\n"
                elif event["type"] == "self_correction":
                    yield f"data: {json.dumps({'type': 'self_correction'})}\n\n"
                elif event["type"] == "result":
                    agent_result = event["result"]

            if agent_result is None:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Agent loop returned no result'})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # Record usage
            if agent_result.total_input_tokens > 0:
                usage_store = get_usage_store()
                usage_store.record_usage(
                    model=agent_result.model,
                    input_tokens=agent_result.total_input_tokens,
                    output_tokens=agent_result.total_output_tokens,
                    cost_usd=agent_result.total_cost_usd,
                    conversation_id=conversation_id,
                )
                yield f"data: {json.dumps({'type': 'usage', 'input_tokens': agent_result.total_input_tokens, 'output_tokens': agent_result.total_output_tokens, 'cost_usd': agent_result.total_cost_usd, 'model': agent_result.model})}\n\n"

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
                yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

            # Save assistant response
            routing_metadata = {
                "sources": [tc["tool"] for tc in agent_result.tool_calls_log],
                "reasoning": f"agentic ({model_tier})",
                "tool_rounds": len(agent_result.tool_calls_log),
            }
            store.add_message(
                conversation_id,
                "assistant",
                agent_result.full_text,
                sources=sources,
                routing=routing_metadata,
            )
            print(f"Saved assistant response ({len(agent_result.full_text)} chars, {len(agent_result.tool_calls_log)} tool calls)")

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
