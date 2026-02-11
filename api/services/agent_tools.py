"""
Agent tool definitions and execution for LifeOS agentic chat.

Each tool wraps an existing service. Tool definitions follow the Anthropic
tool-use schema. execute_tool() dispatches by name and returns a string result.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from api.services.google_auth import GoogleAccount

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    # -- Retrieval --
    {
        "name": "search_vault",
        "description": (
            "Search Nathan's Obsidian vault (notes, meeting transcripts, journals, project docs). "
            "Use for past events, decisions, project details, or anything written down."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 10)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_calendar",
        "description": (
            "Search Google Calendar events across personal and work accounts. "
            "Returns upcoming events or events matching a query/date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (event title, attendee name). Optional if using date_ref.",
                },
                "date_ref": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD) to center the search on. If omitted, returns upcoming events.",
                },
                "days_range": {
                    "type": "integer",
                    "description": "Number of days around date_ref to search (default 1).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_email",
        "description": (
            "Search Gmail across personal and work accounts. "
            "Can filter by sender, recipient, keywords, or date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "Search keywords for email subject/body.",
                },
                "from_email": {
                    "type": "string",
                    "description": "Filter by sender email address.",
                },
                "to_email": {
                    "type": "string",
                    "description": "Filter by recipient email address.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max emails to return per account (default 5).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_drive",
        "description": (
            "Search Google Drive files (docs, sheets, presentations) across personal and work accounts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (matches file names and content).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return per account (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_slack",
        "description": "Search Slack messages across DMs and channels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results (default 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_web",
        "description": (
            "Search the web for current information (weather, news, prices, public facts). "
            "Only use for information that wouldn't be in Nathan's personal data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Web search query.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_message_history",
        "description": (
            "Get iMessage/WhatsApp conversation history with a specific person. "
            "Requires entity_id from person_info. Can filter by date range or search term."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Person entity ID (from person_info).",
                },
                "search_term": {
                    "type": "string",
                    "description": "Optional text to search within messages.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Defaults to last 30 days.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default 100).",
                },
            },
            "required": ["entity_id"],
        },
    },
    # -- People (consolidated) --
    {
        "name": "person_info",
        "description": (
            "Look up a person or generate a comprehensive briefing. "
            "Always start with 'lookup' for person queries; use 'briefing' for meeting prep or deep dives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["lookup", "briefing"],
                    "description": "'lookup' to get entity_id/context, 'briefing' for comprehensive profile.",
                },
                "name": {
                    "type": "string",
                    "description": "Person's name.",
                },
                "email": {
                    "type": "string",
                    "description": "Person's email (optional, improves briefing accuracy).",
                },
            },
            "required": ["action", "name"],
        },
    },
    # -- Actions (consolidated) --
    {
        "name": "manage_tasks",
        "description": "Manage Obsidian tasks: create, list, or complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "complete"],
                    "description": "Action to perform.",
                },
                "description": {
                    "type": "string",
                    "description": "Task description (for create).",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (for complete).",
                },
                "context": {
                    "type": "string",
                    "description": "Context/category (e.g. 'Work', 'Personal', 'Inbox'). Default: 'Inbox'.",
                },
                "priority": {
                    "type": "string",
                    "description": "Priority: 'high', 'medium', 'low', or '' (none).",
                    "enum": ["high", "medium", "low", ""],
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date (YYYY-MM-DD). Optional.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for the task. Optional.",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status (for list): 'todo', 'done', 'in_progress', etc.",
                },
                "query": {
                    "type": "string",
                    "description": "Search within task descriptions (for list).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_reminders",
        "description": "Manage timed reminders: create or list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list"],
                    "description": "Action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Short reminder name/title (for create).",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["once", "cron"],
                    "description": "'once' for one-time, 'cron' for recurring (for create).",
                },
                "schedule_value": {
                    "type": "string",
                    "description": "ISO datetime for 'once', or cron expression for 'cron' (for create).",
                },
                "message_content": {
                    "type": "string",
                    "description": "The reminder message to send (for create).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "create_email_draft",
        "description": "Create a Gmail draft email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Email body text.",
                },
                "account": {
                    "type": "string",
                    "description": "'personal' or 'work'. Default: 'personal'.",
                    "enum": ["personal", "work"],
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
]

# Cache breakpoint on last tool — everything up to here gets cached
TOOL_DEFINITIONS[-1]["cache_control"] = {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def execute_tool(name: str, tool_input: dict) -> str:
    """
    Execute a tool by name and return the formatted result string.

    Returns a string suitable for a tool_result content block.
    On error, returns a string prefixed with "Error: " (caller sets is_error).
    """
    try:
        handler = _TOOL_HANDLERS.get(name)
        if not handler:
            return f"Error: Unknown tool '{name}'"
        result = handler(tool_input)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------

def _tool_search_vault(inp: dict) -> str:
    from api.services.hybrid_search import HybridSearch
    hs = HybridSearch()
    top_k = inp.get("top_k", 10)
    results = hs.search(inp["query"], top_k=top_k)
    if not results:
        return "No vault results found."
    lines = []
    for i, r in enumerate(results, 1):
        fn = r.get("file_name", "unknown")
        content = r.get("content", "")[:800]
        score = r.get("hybrid_score", 0)
        lines.append(f"[{i}] {fn} (score={score:.2f})\n{content}")
    return "\n\n---\n".join(lines)


async def _tool_search_calendar(inp: dict) -> str:
    from api.services.calendar import CalendarService

    query = inp.get("query")
    date_ref = inp.get("date_ref")
    days_range = inp.get("days_range", 1)

    all_events = []
    for account in (GoogleAccount.PERSONAL, GoogleAccount.WORK):
        try:
            cal = CalendarService(account)
            if query:
                events = cal.search_events(query=query, days_back=30, days_forward=30)
            elif date_ref:
                start = datetime.strptime(date_ref, "%Y-%m-%d")
                end = start + timedelta(days=days_range)
                events = cal.get_events_in_range(start, end)
            else:
                events = cal.get_upcoming_events(days=7, max_results=15)
            all_events.extend(events)
        except Exception as e:
            logger.warning(f"Calendar {account.value} error: {e}")

    if not all_events:
        return "No calendar events found."

    all_events.sort(key=lambda e: e.start_time or datetime.min)
    lines = []
    for e in all_events:
        start = e.start_time.strftime("%Y-%m-%d %H:%M") if e.start_time else "TBD"
        acct = f"[{e.source_account}]" if e.source_account else ""
        attendees = f" with {', '.join(e.attendees[:5])}" if e.attendees else ""
        loc = f" @ {e.location}" if e.location else ""
        lines.append(f"- {e.title} ({start}) {acct}{attendees}{loc}")
    return "\n".join(lines)


async def _tool_search_email(inp: dict) -> str:
    from api.services.gmail import GmailService

    max_results = inp.get("max_results", 5)
    all_messages = []
    for account in (GoogleAccount.PERSONAL, GoogleAccount.WORK):
        try:
            gmail = GmailService(account)
            messages = gmail.search(
                keywords=inp.get("keywords"),
                from_email=inp.get("from_email"),
                to_email=inp.get("to_email"),
                max_results=max_results,
                include_body=True,
            )
            all_messages.extend(messages)
        except Exception as e:
            logger.warning(f"Gmail {account.value} error: {e}")

    if not all_messages:
        return "No emails found."

    lines = []
    for m in all_messages:
        date_str = ""
        if m.date:
            try:
                date_str = m.date.astimezone(EASTERN).strftime("%Y-%m-%d %I:%M %p ET")
            except Exception:
                date_str = str(m.date)[:16]
        acct = f"[{m.source_account}]" if m.source_account else ""
        body_preview = (m.body or m.snippet or "")[:600]
        lines.append(
            f"From: {m.sender} {acct}\n"
            f"To: {m.to or ''}\n"
            f"Subject: {m.subject}\n"
            f"Date: {date_str}\n"
            f"{body_preview}"
        )
    return "\n\n---\n".join(lines)


async def _tool_search_drive(inp: dict) -> str:
    from api.services.drive import DriveService

    max_results = inp.get("max_results", 5)
    all_files = []
    for account in (GoogleAccount.PERSONAL, GoogleAccount.WORK):
        try:
            drive = DriveService(account)
            files = drive.search(full_text=inp["query"], max_results=max_results)
            all_files.extend(files)
        except Exception as e:
            logger.warning(f"Drive {account.value} error: {e}")

    if not all_files:
        return "No drive files found."

    lines = []
    for f in all_files:
        acct = f"[{f.source_account}]" if f.source_account else ""
        content_preview = ""
        if f.content:
            content_preview = f"\n{f.content[:800]}"
        lines.append(f"**{f.name}** {acct} ({f.mime_type}){content_preview}")
    return "\n\n---\n".join(lines)


def _tool_search_slack(inp: dict) -> str:
    from api.services.slack_indexer import get_slack_indexer
    from api.services.slack_integration import is_slack_enabled

    if not is_slack_enabled():
        return "Slack is not configured."

    indexer = get_slack_indexer()
    top_k = inp.get("top_k", 10)
    results = indexer.search(query=inp["query"], top_k=top_k)
    if not results:
        return "No Slack messages found."

    lines = []
    for msg in results:
        channel = msg.get("channel_name", "Unknown")
        user = msg.get("user_name", "Unknown")
        ts = msg.get("timestamp", "")[:16]
        content = msg.get("content", "")[:500]
        lines.append(f"**{channel}** - {user} ({ts}):\n{content}")
    return "\n\n".join(lines)


async def _tool_search_web(inp: dict) -> str:
    from api.services.web_search import search_web_with_synthesis
    synthesized, _raw = await search_web_with_synthesis(inp["query"])
    return synthesized or "No web results found."


def _tool_get_message_history(inp: dict) -> str:
    from api.services.imessage import query_person_messages

    start_date = inp.get("start_date")
    end_date = inp.get("end_date")
    if not start_date and not end_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    result = query_person_messages(
        entity_id=inp["entity_id"],
        search_term=inp.get("search_term"),
        start_date=start_date,
        end_date=end_date,
        limit=inp.get("limit", 100),
    )
    if result["count"] == 0:
        return "No messages found."

    date_info = ""
    if result.get("date_range"):
        dr = result["date_range"]
        date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})"
    return f"{result['count']} messages{date_info}:\n\n{result['formatted']}"


# -- People helpers --

def _lookup_person(inp: dict) -> str:
    from api.services.entity_resolver import get_entity_resolver
    from api.services.relationship_summary import get_relationship_summary, format_relationship_context
    from api.services.person_facts import get_person_fact_store

    resolver = get_entity_resolver()
    result = resolver.resolve(name=inp["name"])
    if not result or not result.entity:
        return f"No person found matching '{inp['name']}'."

    entity = result.entity
    parts = [f"**{entity.canonical_name}** (entity_id: {entity.id})"]

    if entity.emails:
        parts.append(f"Emails: {', '.join(entity.emails)}")
    if entity.phones:
        parts.append(f"Phones: {', '.join(entity.phones)}")

    # Relationship summary
    rel = get_relationship_summary(entity.id)
    if rel:
        parts.append(format_relationship_context(rel))

    # Person facts
    fact_store = get_person_fact_store()
    facts = fact_store.get_for_person(entity.id)
    if facts:
        fact_lines = [f"- {f.category}: {f.key} = {f.value}" for f in facts[:15]]
        parts.append("Known facts:\n" + "\n".join(fact_lines))

    return "\n\n".join(parts)


async def _briefing_person(inp: dict) -> str:
    from api.services.briefings import get_briefings_service
    svc = get_briefings_service()
    result = await svc.generate_briefing(inp["name"], email=inp.get("email"))
    if result.get("status") == "success":
        return result.get("briefing", "Briefing generated but empty.")
    return f"Briefing failed: {result.get('message', 'unknown error')}"


def _tool_person_info(inp: dict):
    action = inp["action"]
    if action == "lookup":
        return _lookup_person(inp)
    elif action == "briefing":
        return _briefing_person(inp)
    return f"Error: Unknown person_info action '{action}'"


# -- Task helpers --

def _task_create(inp: dict) -> str:
    from api.services.task_manager import get_task_manager
    tm = get_task_manager()
    task = tm.create(
        description=inp["description"],
        context=inp.get("context", "Inbox"),
        priority=inp.get("priority", ""),
        due_date=inp.get("due_date"),
        tags=inp.get("tags"),
    )
    due = f", due {task.due_date}" if task.due_date else ""
    return f"Task created: \"{task.description}\" (id: {task.id}, context: {task.context}{due})"


def _task_list(inp: dict) -> str:
    from api.services.task_manager import get_task_manager
    tm = get_task_manager()
    tasks = tm.list_tasks(
        status=inp.get("status"),
        context=inp.get("context"),
        query=inp.get("query"),
    )
    if not tasks:
        return "No tasks found."
    lines = []
    for t in tasks:
        status_icon = {"todo": "[ ]", "done": "[x]", "in_progress": "[/]"}.get(t.status, f"[{t.status}]")
        due = f" (due {t.due_date})" if t.due_date else ""
        lines.append(f"{status_icon} {t.description}{due} [id:{t.id}]")
    return "\n".join(lines)


def _task_complete(inp: dict) -> str:
    from api.services.task_manager import get_task_manager
    tm = get_task_manager()
    task = tm.complete(inp["task_id"])
    if not task:
        return f"Error: Task '{inp['task_id']}' not found."
    return f"Task completed: \"{task.description}\""


def _tool_manage_tasks(inp: dict):
    action = inp["action"]
    if action == "create":
        return _task_create(inp)
    elif action == "list":
        return _task_list(inp)
    elif action == "complete":
        return _task_complete(inp)
    return f"Error: Unknown manage_tasks action '{action}'"


# -- Reminder helpers --

def _reminder_create(inp: dict) -> str:
    from api.services.reminder_store import get_reminder_store
    store = get_reminder_store()
    reminder = store.create(
        name=inp["name"],
        schedule_type=inp["schedule_type"],
        schedule_value=inp["schedule_value"],
        message_type="telegram",
        message_content=inp["message_content"],
    )
    return f"Reminder created: \"{reminder.name}\" (id: {reminder.id}, next: {reminder.next_trigger_at})"


def _reminder_list(inp: dict) -> str:
    from api.services.reminder_store import get_reminder_store
    store = get_reminder_store()
    reminders = store.list_all()
    if not reminders:
        return "No active reminders."
    lines = []
    for r in reminders:
        status = "enabled" if r.enabled else "disabled"
        lines.append(f"- \"{r.name}\" ({r.schedule_type}: {r.schedule_value}) [{status}] [id:{r.id}]")
    return "\n".join(lines)


def _tool_manage_reminders(inp: dict):
    action = inp["action"]
    if action == "create":
        return _reminder_create(inp)
    elif action == "list":
        return _reminder_list(inp)
    return f"Error: Unknown manage_reminders action '{action}'"


async def _tool_create_email_draft(inp: dict) -> str:
    from api.services.gmail import GmailService
    account_str = inp.get("account", "personal")
    account = GoogleAccount.WORK if account_str == "work" else GoogleAccount.PERSONAL
    gmail = GmailService(account)
    draft = gmail.create_draft(
        to=inp["to"],
        subject=inp["subject"],
        body=inp["body"],
    )
    if draft:
        return f"Draft created in {account_str} Gmail: \"{inp['subject']}\" to {inp['to']}"
    return "Error: Failed to create email draft."


# Handler dispatch table
_TOOL_HANDLERS = {
    "search_vault": _tool_search_vault,
    "search_calendar": _tool_search_calendar,
    "search_email": _tool_search_email,
    "search_drive": _tool_search_drive,
    "search_slack": _tool_search_slack,
    "search_web": _tool_search_web,
    "get_message_history": _tool_get_message_history,
    "person_info": _tool_person_info,
    "manage_tasks": _tool_manage_tasks,
    "manage_reminders": _tool_manage_reminders,
    "create_email_draft": _tool_create_email_draft,
}

# Status messages for UI feedback when tools execute
TOOL_STATUS_MESSAGES = {
    "search_vault": "Searching notes...",
    "search_calendar": "Checking calendar...",
    "search_email": "Searching email...",
    "search_drive": "Searching Drive...",
    "search_slack": "Searching Slack...",
    "search_web": "Searching the web...",
    "get_message_history": "Loading messages...",
    "person_info": "Looking up person...",
    "person_info.lookup": "Looking up person...",
    "person_info.briefing": "Generating briefing...",
    "manage_tasks": "Managing tasks...",
    "manage_tasks.create": "Creating task...",
    "manage_tasks.list": "Loading tasks...",
    "manage_tasks.complete": "Completing task...",
    "manage_reminders": "Managing reminders...",
    "manage_reminders.create": "Setting reminder...",
    "manage_reminders.list": "Loading reminders...",
    "create_email_draft": "Drafting email...",
}
