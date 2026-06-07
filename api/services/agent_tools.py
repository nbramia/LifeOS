"""
Agent tool definitions and execution for LifeOS agentic chat.

Each tool wraps an existing service. Tool definitions follow the Anthropic
tool-use schema. execute_tool() dispatches by name and returns a string result.
"""
import asyncio
import contextvars
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from api.services.google_auth import resolve_account, get_configured_accounts
from config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Email send gate (draft → confirm → send)
# ---------------------------------------------------------------------------
# Per-turn set of draft IDs created during the current agent turn. The agent
# loop binds a fresh set at the start of each user turn (begin_email_send_turn);
# create_email_draft adds to it and send_email_draft refuses to send anything
# in it. This makes "draft first, get confirmation, then send" a STRUCTURAL
# guarantee rather than a prompt-only instruction — a freshly drafted email
# cannot be sent in the same turn even if the model tries to. A ContextVar
# (not a module global) keeps concurrent requests on the shared API isolated:
# each request runs in its own task/context with its own set.
_drafts_created_this_turn: contextvars.ContextVar = contextvars.ContextVar(
    "drafts_created_this_turn", default=None
)


def begin_email_send_turn() -> None:
    """Bind a fresh per-turn draft set. Call once at the start of each agent turn."""
    _drafts_created_this_turn.set(set())


def _mark_draft_created_this_turn(draft_id: str) -> None:
    drafts = _drafts_created_this_turn.get()
    if drafts is not None and draft_id:
        drafts.add(draft_id)


def _draft_created_this_turn(draft_id: str) -> bool:
    drafts = _drafts_created_this_turn.get()
    return bool(drafts) and draft_id in drafts

# The operator's local timezone, from `LIFEOS_TIMEZONE` (defaults to
# America/New_York). Used to format message timestamps in tool output.
LOCAL_TZ = ZoneInfo(settings.timezone)

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic schema)
# ---------------------------------------------------------------------------

_user = settings.user_name

TOOL_DEFINITIONS = [
    # -- Retrieval --
    {
        "name": "search_vault",
        "description": (
            f"Search {_user}'s Obsidian vault (notes, meeting transcripts, journals, project docs). "
            "Returns relevance-ranked text chunks with file names. "
            "Good for written records, decisions, project details. Returns chunks, not full files."
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
            "Returns event titles, dates, times, attendees, and locations. "
            f"Shows when {_user} met with someone or has upcoming meetings."
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
                    "description": "Number of days to search. With query: search ±N days (default 180). With date_ref: range from date (default 1).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_email",
        "description": (
            "Search Gmail across personal and work accounts. "
            "Returns sender, recipient, subject, date, and body preview. "
            "Use from_email/to_email for targeted searches (get email from person_info first)."
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
            "Search the web for current or real-time information — weather, news, prices, "
            "rankings, benchmarks, reviews, technical specs, documentation, or any public facts "
            "that may have changed. Use whenever the answer requires up-to-date information. "
            "You have full web access through this tool."
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
            "Get iMessage and WhatsApp chat logs with a specific person. "
            "Returns actual message content with timestamps — shows what was said and when. "
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
            "Use 'lookup' for any query mentioning a person — returns entity_id, emails, phones, "
            "relationship strength, days since last contact, interaction counts per channel (90 days), "
            "and known facts. Use 'briefing' for meeting prep or deep dives."
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
        "description": (
            "Manage Obsidian tasks. Actions: 'create' (new task — always lands in "
            "Inbox; do not pass a context), 'list' (filter tasks; supports the "
            "context filter for already-categorized tasks), 'complete' (mark task "
            "done), 'update' (edit any field on an existing task, including tags or "
            "moving the task to a different context), 'tags' (list every distinct "
            "tag across all tasks with usage counts — the same list is already in "
            "the system prompt; call this action only if the user explicitly asks "
            "'what tags do I have', or to double-check a stale cache). When "
            "assigning tags, reuse an existing tag from the system prompt list when "
            "it clearly matches the user's intent; otherwise follow the user's "
            "wording and create a new tag — don't collapse a distinct user-named "
            "tag onto a vaguely similar existing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "complete", "update", "tags"],
                    "description": "Action to perform.",
                },
                "description": {
                    "type": "string",
                    "description": "Task description (for create, or to rename on update).",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (required for complete and update).",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Context/category (e.g. 'Work', 'Personal', 'Inbox'). Used as "
                        "a filter for 'list' and as the target context for 'update'. "
                        "Ignored on 'create' — new tasks always land in Inbox."
                    ),
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
                    "description": (
                        "Full set of tags for the task (for create or update). On update, "
                        "this REPLACES the existing tag list — to add a tag to a task, "
                        "first 'list' or fetch the task, then send the union of existing "
                        "+ new tags. Strip leading '#'."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by status (for list): 'todo', 'done', 'in_progress', etc. "
                        "Also accepted on update to change status."
                    ),
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
        "description": "DEPRECATED — use manage_schedules. Manage timed reminders: create or list.",
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
        "name": "manage_schedules",
        "description": (
            "Manage schedules: create or list. A schedule binds a trigger "
            "(once/cron) to an action (notify/prompt/endpoint/agent). Use "
            "action='agent' to run autonomous work on a schedule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list"],
                    "description": "Operation to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Short schedule name/title (for create).",
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
                "schedule_action": {
                    "type": "string",
                    "enum": ["notify", "prompt", "endpoint", "agent"],
                    "description": "What fires: notify (static text), prompt (run chat), endpoint (call API), agent (hand off to the agent worker).",
                },
                "message_content": {
                    "type": "string",
                    "description": "Static text, natural-language prompt, or agent task description (for create).",
                },
                "executor": {
                    "type": "string",
                    "enum": ["local", "cloud", "cloud-haiku", "cloud-sonnet"],
                    "description": "For schedule_action='agent': which executor the spawned #agent task targets.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "read_vault_file",
        "description": (
            "Read the full content of a specific file from the Obsidian vault by name. "
            "Use after search_vault finds a relevant file but only returns partial chunks. "
            "Supports fuzzy matching — just provide the filename (e.g. 'Taylor.md' or 'Taylor')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "File name to read (e.g. 'Taylor.md', '2026-01-12'). Fuzzy matched.",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": "search_finances",
        "description": (
            "Query live financial data from Monarch Money. "
            "Actions: 'accounts' (current balances), 'transactions' (recent spending, filterable), "
            "'cashflow' (income/expenses/savings summary), 'budgets' (budget vs actual by category). "
            "For historical monthly summaries, use search_vault with 'finance' or 'spending'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accounts", "transactions", "cashflow", "budgets"],
                    "description": "What financial data to retrieve.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Transactions default to 30 days ago, cashflow/budgets to 1st of current month.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Defaults to today.",
                },
                "category": {
                    "type": "string",
                    "description": "Filter transactions by category name (e.g. 'Groceries', 'Dining').",
                },
                "search": {
                    "type": "string",
                    "description": "Search transactions by merchant name.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "create_email_draft",
        "description": (
            "Create a Gmail draft email. This NEVER sends — it only drafts. "
            "Always the FIRST step for any email request, even one phrased as "
            "\"send an email to X\": draft it, show it to the user, and wait for "
            "their explicit confirmation before sending with send_email_draft. "
            "Returns the draft_id you'll need to send it later."
        ),
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
    {
        "name": "send_email_draft",
        "description": (
            "Send a Gmail draft that was already created with create_email_draft, "
            "by its draft_id. SAFETY GATE: only use this AFTER you have shown the "
            "user the draft and they have EXPLICITLY confirmed — in a later message "
            "— that they want it sent. NEVER send a draft in the same turn you "
            "created it: draft first, ask for confirmation, then send only once the "
            "user says yes. A draft created in the current turn cannot be sent and "
            "will be rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "The draft_id returned by create_email_draft.",
                },
                "account": {
                    "type": "string",
                    "description": "'personal' or 'work'. Must match the account the draft was created in. Default: 'personal'.",
                    "enum": ["personal", "work"],
                },
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a Google Calendar event. Invite emails are automatically sent to attendees. "
            "IMPORTANT: Before calling this tool, present the event details to the user and wait for confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "start_time": {"type": "string", "description": "ISO datetime (e.g. 2026-02-14T14:00:00-05:00)."},
                "end_time": {"type": "string", "description": "ISO datetime."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses of attendees.",
                },
                "description": {"type": "string", "description": "Event description."},
                "location": {"type": "string", "description": "Event location."},
                "account": {
                    "type": "string",
                    "enum": ["personal", "work"],
                    "description": "'personal' or 'work'. Default: 'personal'.",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": (
            "Update an existing Google Calendar event. Requires event_id from search_calendar. "
            "Only provided fields are changed. Update emails are sent to attendees. "
            "IMPORTANT: Confirm changes with the user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID from search_calendar."},
                "title": {"type": "string", "description": "New title."},
                "start_time": {"type": "string", "description": "New start ISO datetime."},
                "end_time": {"type": "string", "description": "New end ISO datetime."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New attendee emails (replaces existing list).",
                },
                "description": {"type": "string", "description": "New description."},
                "location": {"type": "string", "description": "New location."},
                "account": {
                    "type": "string",
                    "enum": ["personal", "work"],
                    "description": "'personal' or 'work'. Default: 'personal'.",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete a Google Calendar event. Requires event_id from search_calendar. "
            "Cancellation emails are sent to attendees. "
            "IMPORTANT: Confirm deletion with the user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID from search_calendar."},
                "account": {
                    "type": "string",
                    "enum": ["personal", "work"],
                    "description": "'personal' or 'work'. Default: 'personal'.",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a persistent memory for future reference. Use when the user says "
            "'remember that...', 'don't forget...', 'note that...', or asks you to "
            "remember something across conversations. Memories persist across all sessions "
            "and are automatically surfaced when relevant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory to save (natural language).",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Search saved memories by keyword. Use to recall previously saved information, "
            "check if a memory already exists, or find specific remembered facts/preferences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for memories.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "manage_workouts",
        "description": (
            "Log and query the user's workout log and fitness metrics (the fitness "
            "bot's backend). Parse free-form training text and record it — do NOT "
            "ask the user to confirm before logging. Actions:\n"
            "- 'log': record a session. Pass `sets` (one entry per distinct "
            "exercise/load); use `count` for repeated identical sets ('3x5 @185' = "
            "count 3, reps 5, weight 185; 'bench 135x8' = count 1, reps 8, weight "
            "135). Omit `date` to log today; pass YYYY-MM-DD for an explicit day. "
            "After logging, tell the user what was recorded in normalized form.\n"
            "- 'update': correct a session. Defaults to the most recent session; "
            "to fix an OLDER one (e.g. the user threaded-replied to an earlier "
            "'Logged…' line), first 'list' recent sessions, find the matching "
            "`session_id`, and pass it. Pass `sets` to replace its sets, or "
            "date/kind/title/notes to amend.\n"
            "- 'list': recent sessions with their id, date, and summary — use to "
            "find the session a correction refers to before 'update'.\n"
            "- 'history': recent sets for one `exercise` (trend a lift).\n"
            "- 'summary': aggregate volume (sets/reps/tonnage) over a window, "
            "optionally for one `exercise` or `kind`.\n"
            "- 'log_metric': record a body metric, e.g. morning body weight "
            "(metric_type 'body_weight', value 178.4, unit 'lb').\n"
            "- 'metrics': list a metric over time (e.g. body_weight trend).\n"
            "- 'get_profile' / 'set_profile': read or set the training profile "
            "(keys: goals, injuries, equipment, experience, schedule, constraints, "
            "preferences).\n"
            "- 'readiness': one-call snapshot for recommendations — recent volume, "
            "available recovery signals (body weight now; sleep/HR via Apple "
            "Health later), and the training profile. Call this before giving "
            "trainer-style advice so it's grounded in the user's real data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["log", "update", "list", "history", "summary", "log_metric", "metrics", "get_profile", "set_profile", "readiness"],
                    "description": "Action to perform.",
                },
                "sets": {
                    "type": "array",
                    "description": "Exercises (for log/update). Each: {exercise, reps, weight, weight_unit, count, rpe, notes}. `count` = number of identical sets (default 1).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "exercise": {"type": "string", "description": "Exercise name (normalized server-side)."},
                            "reps": {"type": "integer", "description": "Reps per set."},
                            "weight": {"type": "number", "description": "Load."},
                            "weight_unit": {"type": "string", "description": "'lb' or 'kg' (default lb)."},
                            "count": {"type": "integer", "description": "Number of identical sets (default 1)."},
                            "rpe": {"type": "number", "description": "Rate of perceived exertion (optional)."},
                            "notes": {"type": "string", "description": "Per-exercise note, e.g. cardio distance/time."},
                        },
                    },
                },
                "date": {"type": "string", "description": "Session date YYYY-MM-DD (for log/update). Omit on log to use today."},
                "kind": {"type": "string", "description": "strength | cardio | mobility | sport | other (optional)."},
                "title": {"type": "string", "description": "Session title, e.g. 'Push day' (optional)."},
                "notes": {"type": "string", "description": "Session notes (optional)."},
                "session_id": {"type": "string", "description": "Target session for 'update' (defaults to most recent)."},
                "exercise": {"type": "string", "description": "Exercise name for 'history' / 'summary'."},
                "date_start": {"type": "string", "description": "Window start YYYY-MM-DD (for summary/metrics)."},
                "date_end": {"type": "string", "description": "Window end YYYY-MM-DD (for summary/metrics)."},
                "metric_type": {"type": "string", "description": "Metric name for log_metric/metrics, e.g. 'body_weight'."},
                "value": {"type": "string", "description": "Value: numeric for 'log_metric' (e.g. '178.4'), free text for 'set_profile'."},
                "unit": {"type": "string", "description": "Metric unit for 'log_metric', e.g. 'lb'."},
                "key": {"type": "string", "description": "Training-profile key for 'set_profile'."},
                "limit": {"type": "integer", "description": "Max rows for history/metrics (default 20/100)."},
            },
            "required": ["action"],
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

    Sync handlers are run in a thread pool so they don't block the event loop
    and can execute truly in parallel via asyncio.gather.
    """
    try:
        handler = _TOOL_HANDLERS.get(name)
        if not handler:
            return f"Error: Unknown tool '{name}'"
        result = handler(tool_input)
        if asyncio.iscoroutine(result):
            result = await result
        elif not isinstance(result, str):
            # Handler returned a coroutine wrapper (from consolidated dispatchers)
            result = await result if asyncio.iscoroutine(result) else result
        return result
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
        return f"Error: {e}"


# Sync handlers to wrap in to_thread for parallel execution
_SYNC_HANDLERS = {"search_vault", "read_vault_file", "search_slack", "get_message_history", "person_info", "manage_tasks", "manage_reminders", "manage_schedules", "create_calendar_event", "update_calendar_event", "delete_calendar_event", "search_memories"}


async def execute_tool_parallel(name: str, tool_input: dict) -> str:
    """Like execute_tool but runs sync handlers in a thread to avoid blocking the event loop."""
    try:
        handler = _TOOL_HANDLERS.get(name)
        if not handler:
            return f"Error: Unknown tool '{name}'"
        if name in _SYNC_HANDLERS:
            result = await asyncio.to_thread(handler, tool_input)
        else:
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
    days_range = inp.get("days_range")

    all_events = []
    for account in get_configured_accounts():
        try:
            cal = CalendarService(account)
            if query:
                search_range = days_range or 180
                events = cal.search_events(query=query, days_back=search_range, days_forward=search_range)
            elif date_ref:
                start = datetime.strptime(date_ref, "%Y-%m-%d")
                end = start + timedelta(days=days_range or 1)
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
    for account in get_configured_accounts():
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
                date_str = m.date.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p %Z")
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
    for account in get_configured_accounts():
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


def _format_whatsapp_interactions(interactions: list) -> str:
    """Format WhatsApp interactions into the same markdown style as iMessage."""
    if not interactions:
        return ""

    lines = []
    current_date = None

    for inter in interactions:
        msg_date = inter.timestamp.strftime("%Y-%m-%d")

        if msg_date != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"### {msg_date}")
            current_date = msg_date

        time_str = inter.timestamp.strftime("%H:%M")
        # Parse direction from title: "WhatsApp → Name" = sent, "WhatsApp ← Name" = received
        direction = "→ " if "→" in (inter.title or "") else "← "
        text = (inter.snippet or "").replace("\n", " ").strip()
        if len(text) > 300:
            text = text[:300] + "..."
        lines.append(f"- **{time_str}** {direction}{text}")

    return "\n".join(lines)


def _tool_get_message_history(inp: dict) -> str:
    from api.services.imessage import query_person_messages, resolve_entity_id
    from api.services.interaction_store import get_interaction_store

    entity_id = inp["entity_id"]
    resolved_id = resolve_entity_id(entity_id)
    if not resolved_id:
        return f"Could not resolve person '{entity_id}'. Use person_info first."

    start_date = inp.get("start_date")
    end_date = inp.get("end_date")
    search_term = inp.get("search_term")
    limit = inp.get("limit", 100)
    if not start_date and not end_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    # 1. iMessage/SMS
    imessage_result = query_person_messages(
        entity_id=resolved_id,
        search_term=search_term,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    # 2. WhatsApp from interaction store
    store = get_interaction_store()
    days_back = 30
    if start_date:
        try:
            delta = datetime.now() - datetime.strptime(start_date, "%Y-%m-%d")
            days_back = max(delta.days, 1)
        except (ValueError, TypeError):
            pass

    whatsapp_interactions = store.get_for_person(
        person_id=resolved_id,
        days_back=days_back,
        source_type="whatsapp",
        limit=limit,
    )

    # Filter WhatsApp by search_term
    if search_term and whatsapp_interactions:
        term_lower = search_term.lower()
        whatsapp_interactions = [
            i for i in whatsapp_interactions
            if i.snippet and term_lower in i.snippet.lower()
        ]

    # Filter WhatsApp by end_date
    if end_date and whatsapp_interactions:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            whatsapp_interactions = [
                i for i in whatsapp_interactions if i.timestamp <= end_dt
            ]
        except (ValueError, TypeError):
            pass

    # Sort WhatsApp chronologically (store returns most recent first)
    whatsapp_interactions.sort(key=lambda i: i.timestamp)

    imessage_count = imessage_result["count"]
    whatsapp_count = len(whatsapp_interactions)

    if imessage_count == 0 and whatsapp_count == 0:
        return "No messages found."

    # Build output
    parts = []

    if imessage_count > 0 and whatsapp_count > 0:
        # Both sources — label each section
        dr = imessage_result.get("date_range")
        date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})" if dr else ""
        parts.append(f"## iMessage ({imessage_count} messages{date_info})\n\n{imessage_result['formatted']}")
        parts.append(f"## WhatsApp ({whatsapp_count} messages)\n\n{_format_whatsapp_interactions(whatsapp_interactions)}")
        total = imessage_count + whatsapp_count
        return f"{total} messages from iMessage and WhatsApp:\n\n" + "\n\n".join(parts)
    elif imessage_count > 0:
        date_info = ""
        if imessage_result.get("date_range"):
            dr = imessage_result["date_range"]
            date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})"
        return f"{imessage_count} messages{date_info}:\n\n{imessage_result['formatted']}"
    else:
        return f"{whatsapp_count} WhatsApp messages:\n\n{_format_whatsapp_interactions(whatsapp_interactions)}"


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
    if entity.phone_numbers:
        parts.append(f"Phones: {', '.join(entity.phone_numbers)}")
    if entity.birthday:
        parts.append(f"Birthday: {entity.birthday}")
    if entity.company or entity.position:
        role = " — ".join(filter(None, [entity.position, entity.company]))
        parts.append(f"Role: {role}")

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
        priority=inp.get("priority", ""),
        due_date=inp.get("due_date"),
        tags=inp.get("tags"),
    )
    due = f", due {task.due_date}" if task.due_date else ""
    return f"Task created: \"{task.description}\" (id: {task.id}{due})"


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


_UPDATABLE_FIELDS = ("description", "status", "context", "priority", "due_date", "tags")


def _task_update(inp: dict) -> str:
    from api.services.task_manager import get_task_manager
    task_id = inp.get("task_id")
    if not task_id:
        return "Error: 'task_id' is required for update."
    tm = get_task_manager()
    updates = {k: inp[k] for k in _UPDATABLE_FIELDS if k in inp and inp[k] is not None}
    if not updates:
        return "Error: no updatable fields provided (description, status, context, priority, due_date, tags)."
    task = tm.update(task_id, **updates)
    if not task:
        return f"Error: Task '{task_id}' not found."
    changed = ", ".join(f"{k}={updates[k]!r}" for k in updates)
    return f"Task updated: \"{task.description}\" (id: {task.id}; changed: {changed})"


def _task_tags(_inp: dict) -> str:
    from api.services.task_manager import get_task_manager
    rows = get_task_manager().list_tags()
    if not rows:
        return "No tags defined yet."
    return "\n".join(f"#{row['tag']} ({row['count']})" for row in rows)


def _tool_manage_tasks(inp: dict):
    action = inp["action"]
    if action == "create":
        return _task_create(inp)
    elif action == "list":
        return _task_list(inp)
    elif action == "complete":
        return _task_complete(inp)
    elif action == "update":
        return _task_update(inp)
    elif action == "tags":
        return _task_tags(inp)
    return f"Error: Unknown manage_tasks action '{action}'"


# -- Reminder helpers --

def _reminder_create(inp: dict) -> str:
    from api.services.reminder_store import get_reminder_store
    store = get_reminder_store()
    reminder = store.create(
        name=inp["name"],
        schedule_type=inp["schedule_type"],
        schedule_value=inp["schedule_value"],
        message_type="static",
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
    # DEPRECATED alias for manage_schedules — kept for back-compat.
    action = inp["action"]
    if action == "create":
        return _reminder_create(inp)
    elif action == "list":
        return _reminder_list(inp)
    return f"Error: Unknown manage_reminders action '{action}'"


# -- Schedule helpers --

def _schedule_create(inp: dict) -> str:
    from api.services.scheduler_store import get_scheduler_store
    store = get_scheduler_store()
    # `action` is the manage_schedules operation (create/list); the schedule's
    # own action is passed as `schedule_action`.
    action = inp.get("schedule_action") or "notify"
    entry = store.create(
        name=inp["name"],
        schedule_type=inp["schedule_type"],
        schedule_value=inp["schedule_value"],
        action=action,
        message_type=inp.get("message_type", "static" if action == "notify" else action),
        message_content=inp.get("message_content", ""),
        executor=inp.get("executor", ""),
    )
    label = f"{action} (#{entry.executor})" if action == "agent" and entry.executor else action
    return (f"Schedule created: \"{entry.name}\" (id: {entry.id}, action: {label}, "
            f"next: {entry.next_trigger_at})")


def _schedule_list(inp: dict) -> str:
    from api.services.scheduler_store import get_scheduler_store
    store = get_scheduler_store()
    entries = store.list_all()
    if not entries:
        return "No active schedules."
    lines = []
    for e in entries:
        status = "enabled" if e.enabled else "disabled"
        label = f"{e.action} (#{e.executor})" if e.action == "agent" and e.executor else e.action
        lines.append(f"- \"{e.name}\" ({e.schedule_type}: {e.schedule_value}) "
                     f"[{label}] [{status}] [id:{e.id}]")
    return "\n".join(lines)


def _tool_manage_schedules(inp: dict):
    action = inp["action"]
    if action == "create":
        return _schedule_create(inp)
    elif action == "list":
        return _schedule_list(inp)
    return f"Error: Unknown manage_schedules action '{action}'"


async def _tool_create_email_draft(inp: dict) -> str:
    from api.services.gmail import GmailService
    account_str = inp.get("account", "personal")
    account = resolve_account(account_str)
    gmail = GmailService(account)
    draft = gmail.create_draft(
        to=inp["to"],
        subject=inp["subject"],
        body=inp["body"],
    )
    if not draft:
        return "Error: Failed to create email draft."
    # Record this draft as created in the current turn so the send gate refuses
    # to send it until the user confirms in a later turn.
    _mark_draft_created_this_turn(draft.draft_id)
    return (
        f"Draft created in {account_str} Gmail (draft_id={draft.draft_id}): "
        f"\"{inp['subject']}\" to {inp['to']}. "
        f"Do NOT send it yet — show the draft to the user and wait for their "
        f"explicit confirmation, then call send_email_draft with draft_id="
        f"{draft.draft_id} and account={account_str}."
    )


async def _tool_send_email_draft(inp: dict) -> str:
    from api.services.gmail import GmailService
    draft_id = (inp.get("draft_id") or "").strip()
    if not draft_id:
        return "Error: draft_id is required to send a draft. Create the draft first with create_email_draft."
    # SAFETY GATE: never send a draft that was created in this same turn. Sends
    # are only allowed for drafts created in a prior turn, giving the user a
    # chance to review and explicitly confirm before anything goes out.
    if _draft_created_this_turn(draft_id):
        return (
            "Error: This draft was just created in the current turn, so it cannot be sent yet. "
            "Show the draft to the user and ask them to confirm. Only call send_email_draft "
            "after they explicitly say yes in a later message."
        )
    account_str = inp.get("account", "personal")
    account = resolve_account(account_str)
    gmail = GmailService(account)
    message_id = gmail.send_draft(draft_id)
    if message_id:
        return f"Email sent from {account_str} Gmail (message_id={message_id})."
    return (
        "Error: Failed to send draft. Verify the draft_id is correct and that the "
        "account matches where the draft was created."
    )


def _tool_create_calendar_event(inp: dict) -> str:
    from api.services.calendar import CalendarService
    account_str = inp.get("account", "personal")
    account = resolve_account(account_str)
    cal = CalendarService(account)
    event = cal.create_event(
        title=inp["title"],
        start_time=inp["start_time"],
        end_time=inp["end_time"],
        attendees=inp.get("attendees"),
        description=inp.get("description"),
        location=inp.get("location"),
    )
    parts = [f"Event created: \"{event.title}\""]
    parts.append(f"When: {event.start_time.strftime('%Y-%m-%d %H:%M')} – {event.end_time.strftime('%H:%M')}")
    if event.attendees:
        parts.append(f"Attendees: {', '.join(event.attendees)}")
    if event.html_link:
        parts.append(f"Link: {event.html_link}")
    parts.append(f"Account: {account_str}")
    return "\n".join(parts)


def _tool_update_calendar_event(inp: dict) -> str:
    from api.services.calendar import CalendarService
    account_str = inp.get("account", "personal")
    account = resolve_account(account_str)
    cal = CalendarService(account)
    event = cal.update_event(
        event_id=inp["event_id"],
        title=inp.get("title"),
        start_time=inp.get("start_time"),
        end_time=inp.get("end_time"),
        attendees=inp.get("attendees"),
        description=inp.get("description"),
        location=inp.get("location"),
    )
    parts = [f"Event updated: \"{event.title}\""]
    parts.append(f"When: {event.start_time.strftime('%Y-%m-%d %H:%M')} – {event.end_time.strftime('%H:%M')}")
    if event.attendees:
        parts.append(f"Attendees: {', '.join(event.attendees)}")
    if event.html_link:
        parts.append(f"Link: {event.html_link}")
    return "\n".join(parts)


def _tool_delete_calendar_event(inp: dict) -> str:
    from api.services.calendar import CalendarService
    account_str = inp.get("account", "personal")
    account = resolve_account(account_str)
    cal = CalendarService(account)
    cal.delete_event(event_id=inp["event_id"])
    return f"Event deleted (id: {inp['event_id']}, account: {account_str})"


async def _tool_save_memory(inp: dict) -> str:
    from api.services.memory_store import get_memory_store
    from api.routes.memories import synthesize_memory

    content = await synthesize_memory(inp["content"])
    store = get_memory_store()
    memory = store.create_memory(content)
    return f"Memory saved: \"{memory.content}\" (id: {memory.id}, category: {memory.category})"


def _tool_search_memories(inp: dict) -> str:
    from api.services.memory_store import get_memory_store

    store = get_memory_store()
    memories = store.search_memories(inp["query"], limit=10)
    if not memories:
        return "No matching memories found."
    lines = []
    for m in memories:
        lines.append(f"- [{m.category}] {m.content}")
    return "\n".join(lines)


# -- Workout helpers (fitness bot backend) --

def _fmt_num(n) -> str:
    if n is None:
        return ""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _summarize_session(session) -> str:
    """Compact one-line summary, grouping consecutive identical sets."""
    groups: list[list] = []
    for s in session.sets:
        key = (s.exercise, s.reps, s.weight, s.weight_unit)
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
        else:
            groups.append([key, 1])
    parts = []
    for (exercise, reps, weight, unit), count in groups:
        if reps is None and weight is None:
            parts.append(exercise)
            continue
        rep_part = f"{count}×{reps}" if count > 1 else f"{_fmt_num(reps)}"
        w = f" @{_fmt_num(weight)} {unit}" if weight else ""
        parts.append(f"{exercise} {rep_part}{w}".strip())
    body = "; ".join(parts) if parts else "(no sets)"
    return f"{session.date}: {body}"


def _workout_log(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    store = get_fitness_store()
    sets = inp.get("sets") or []
    if not sets:
        return "Error: 'log' needs at least one entry in 'sets'."
    session = store.add_session(
        sets=sets,
        date=inp.get("date"),
        kind=inp.get("kind", ""),
        title=inp.get("title", ""),
        notes=inp.get("notes", ""),
        raw_ref=inp.get("raw_ref", ""),
    )
    return f"Logged — {_summarize_session(session)} (session id: {session.id})"


def _workout_update(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    store = get_fitness_store()
    session = store.update_session(
        session_id=inp.get("session_id"),
        target="latest",
        date=inp.get("date"),
        kind=inp.get("kind"),
        title=inp.get("title"),
        notes=inp.get("notes"),
        sets=inp.get("sets"),
    )
    if not session:
        return "Error: no session to update (none logged yet, or session_id not found)."
    return f"Updated — {_summarize_session(session)} (session id: {session.id})"


def _workout_list(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    store = get_fitness_store()
    sessions = store.list_sessions(
        date_start=inp.get("date_start"),
        date_end=inp.get("date_end"),
        kind=inp.get("kind"),
        limit=int(inp.get("limit", 10) or 10),
    )
    if not sessions:
        return "No sessions logged."
    lines = ["Recent sessions (newest first):"]
    for s in sessions:
        lines.append(f"  [{s.id}] {_summarize_session(s)}")
    return "\n".join(lines)


def _workout_history(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    exercise = inp.get("exercise")
    if not exercise:
        return "Error: 'history' needs an 'exercise'."
    store = get_fitness_store()
    rows = store.exercise_history(exercise, limit=int(inp.get("limit", 20) or 20))
    canonical = store.normalize_exercise(exercise)
    if not rows:
        return f"No history for {canonical}."
    lines = [f"{canonical} — recent sets:"]
    for r in rows:
        w = f" @{_fmt_num(r['weight'])} {r['weight_unit']}" if r["weight"] else ""
        rpe = f" RPE {_fmt_num(r['rpe'])}" if r["rpe"] else ""
        sid = f" [{r['session_id']}]" if r.get("session_id") else ""
        lines.append(f"  {r['date']}: {_fmt_num(r['reps'])} reps{w}{rpe}{sid}")
    return "\n".join(lines)


def _workout_summary(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    store = get_fitness_store()
    summary = store.volume_summary(
        exercise=inp.get("exercise"),
        kind=inp.get("kind"),
        date_start=inp.get("date_start"),
        date_end=inp.get("date_end"),
    )
    scope = store.normalize_exercise(inp["exercise"]) if inp.get("exercise") else (inp.get("kind") or "all")
    return (
        f"Volume ({scope}): {summary['sessions']} sessions, {summary['sets']} sets, "
        f"{summary['reps']} reps, tonnage {_fmt_num(summary['tonnage'])}."
    )


def _workout_log_metric(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    metric_type = inp.get("metric_type")
    value = inp.get("value")
    if not metric_type or value is None:
        return "Error: 'log_metric' needs 'metric_type' and 'value'."
    store = get_fitness_store()
    m = store.log_metric(metric_type, float(value), unit=inp.get("unit", ""), start_at=inp.get("date"))
    unit = f" {m.unit}" if m.unit else ""
    return f"Logged {metric_type.replace('_', ' ')}: {_fmt_num(m.value)}{unit}."


def _workout_metrics(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    metric_type = inp.get("metric_type")
    if not metric_type:
        return "Error: 'metrics' needs a 'metric_type'."
    store = get_fitness_store()
    rows = store.list_metrics(metric_type, start=inp.get("date_start"), end=inp.get("date_end"),
                              limit=int(inp.get("limit", 100) or 100))
    if not rows:
        return f"No {metric_type.replace('_', ' ')} recorded."
    lines = [f"{metric_type.replace('_', ' ')}:"]
    for m in rows:
        unit = f" {m.unit}" if m.unit else ""
        lines.append(f"  {m.start_at[:10]}: {_fmt_num(m.value)}{unit}")
    return "\n".join(lines)


def _workout_get_profile(_inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    profile = get_fitness_store().get_profile()
    if not profile:
        return "Training profile is empty."
    return "\n".join(f"{k}: {v}" for k, v in profile.items())


def _workout_set_profile(inp: dict) -> str:
    from api.services.fitness_store import get_fitness_store
    key, value = inp.get("key"), inp.get("value")
    if not key or value is None:
        return "Error: 'set_profile' needs 'key' and 'value'."
    get_fitness_store().set_profile(key, str(value))
    return f"Training profile updated: {key} = {value}"


# Recovery metrics worth citing for a readiness read (manual + Apple Health #323).
_RECOVERY_METRICS = ("body_weight", "resting_hr", "hrv", "sleep_hours")


def _workout_readiness(inp: dict) -> str:
    """One-call snapshot for trainer recommendations: recent volume + recovery
    signals + profile. Degrades gracefully when little data exists (e.g. before
    Apple Health #323 lands, only manual metrics like body weight are present).
    """
    from datetime import date, timedelta
    from api.services.fitness_store import get_fitness_store, _today
    store = get_fitness_store()

    today = date.fromisoformat(_today())
    since14 = (today - timedelta(days=14)).isoformat()
    since30 = (today - timedelta(days=30)).isoformat()

    recent = store.list_sessions(date_start=since14, limit=50)
    vol = store.volume_summary(date_start=since30)

    lines = ["Readiness snapshot:"]
    if recent:
        lines.append(f"- Sessions (14d): {len(recent)} — most recent {recent[0].date}")
    else:
        lines.append("- Sessions (14d): none logged")
    lines.append(
        f"- Volume (30d): {vol['sessions']} sessions, {vol['sets']} sets, "
        f"{vol['reps']} reps, tonnage {_fmt_num(vol['tonnage'])}"
    )

    have_recovery = []
    for metric in _RECOVERY_METRICS:
        latest = store.latest_metric(metric)
        if latest:
            have_recovery.append(metric)
            unit = f" {latest.unit}" if latest.unit else ""
            lines.append(f"- {metric.replace('_', ' ')}: {_fmt_num(latest.value)}{unit} (latest {latest.start_at[:10]})")
    if not have_recovery:
        lines.append("- Recovery metrics: none yet (sleep/HR/HRV arrive with Apple Health; log body weight to start)")

    profile = store.get_profile()
    if profile:
        lines.append("- Profile: " + "; ".join(f"{k}={v}" for k, v in profile.items()))
    else:
        lines.append("- Profile: not set (ask the user for goals/injuries/equipment)")
    return "\n".join(lines)


def _tool_manage_workouts(inp: dict) -> str:
    action = inp.get("action")
    handlers = {
        "log": _workout_log,
        "update": _workout_update,
        "list": _workout_list,
        "history": _workout_history,
        "summary": _workout_summary,
        "log_metric": _workout_log_metric,
        "metrics": _workout_metrics,
        "get_profile": _workout_get_profile,
        "set_profile": _workout_set_profile,
        "readiness": _workout_readiness,
    }
    handler = handlers.get(action)
    if not handler:
        return f"Error: Unknown manage_workouts action '{action}'"
    result = handler(inp)
    # Mirror to the Google Sheet (if configured) after a mutating action.
    # Non-blocking and best-effort — never let a mirror issue affect logging.
    if action in ("log", "update", "log_metric") and not result.startswith("Error"):
        try:
            from api.services.fitness_sheet_mirror import trigger_mirror
            trigger_mirror()
        except Exception as e:
            logger.debug(f"Fitness sheet mirror trigger skipped: {e}")
    return result


# Handler dispatch table
def _tool_read_vault_file(inp: dict) -> str:
    from pathlib import Path
    from config.settings import settings
    vault = Path(settings.vault_path)
    target = inp["filename"].strip()
    # Add .md extension if missing
    if not target.endswith(".md"):
        target_md = target + ".md"
    else:
        target_md = target
        target = target[:-3]  # name without extension

    # Try exact match first, then case-insensitive, then substring
    candidates = list(vault.rglob("*.md"))
    match = None
    for f in candidates:
        if f.name == target_md:
            match = f
            break
    if not match:
        target_lower = target_md.lower()
        for f in candidates:
            if f.name.lower() == target_lower:
                match = f
                break
    if not match:
        target_lower = target.lower()
        for f in candidates:
            if target_lower in f.stem.lower():
                match = f
                break
    if not match:
        return f"File '{inp['filename']}' not found in vault."
    try:
        content = match.read_text(encoding="utf-8")
        # Truncate if very long (keep first 6000 chars)
        if len(content) > 6000:
            content = content[:6000] + f"\n\n... (truncated, {len(content)} chars total)"
        return f"File: {match.relative_to(vault)}\n\n{content}"
    except Exception as e:
        return f"Error reading {match.name}: {e}"


async def _tool_search_finances(inp: dict) -> str:
    from api.services.monarch import get_monarch_client

    action = inp["action"]
    client = get_monarch_client()

    if action == "accounts":
        accounts = await client.get_accounts()
        if not accounts:
            return "No accounts found."
        lines = ["| Account | Type | Balance | Institution |", "|---------|------|---------|-------------|"]
        for a in sorted(accounts, key=lambda x: x["balance"], reverse=True):
            lines.append(f"| {a['name']} | {a['type']} | ${a['balance']:,.2f} | {a['institution']} |")
        return "\n".join(lines)

    elif action == "transactions":
        start = inp.get("start_date")
        end = inp.get("end_date")
        if not start:
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        txns = await client.get_transactions(
            start_date=start, end_date=end,
            search=inp.get("search", ""), category=inp.get("category"),
        )
        if not txns:
            return "No transactions found."
        lines = [f"{len(txns)} transactions:"]
        for t in sorted(txns, key=lambda x: x["date"], reverse=True)[:50]:
            sign = "" if t["amount"] >= 0 else "-"
            lines.append(f"- {t['date']} | {t['merchant']} | {t['category']} | {sign}${abs(t['amount']):,.2f}")
        if len(txns) > 50:
            lines.append(f"... and {len(txns) - 50} more")
        return "\n".join(lines)

    elif action == "cashflow":
        start = inp.get("start_date")
        end = inp.get("end_date")
        if not start:
            now = datetime.now()
            start = now.replace(day=1).strftime("%Y-%m-%d")
        cf = await client.get_cashflow_summary(start_date=start, end_date=end)
        cats = await client.get_cashflow_by_category(start_date=start, end_date=end)
        lines = [
            f"**Income**: ${cf['total_income']:,.2f}",
            f"**Expenses**: ${cf['total_expenses']:,.2f}",
            f"**Net Savings**: ${cf['total_income'] - cf['total_expenses']:,.2f}",
            f"**Savings Rate**: {cf['savings_rate'] * 100:.1f}%" if cf['savings_rate'] <= 1 else f"**Savings Rate**: {cf['savings_rate']:.1f}%",
        ]
        if cats:
            lines.append("\nTop categories:")
            for c in cats[:10]:
                lines.append(f"- {c['category']}: ${c['amount']:,.2f}")
        return "\n".join(lines)

    elif action == "budgets":
        start = inp.get("start_date")
        end = inp.get("end_date")
        if not start:
            now = datetime.now()
            start = now.replace(day=1).strftime("%Y-%m-%d")
        budgets = await client.get_budgets(start_date=start, end_date=end)
        if not budgets:
            return "No budgets found."
        lines = ["| Category | Budgeted | Actual | Remaining |", "|----------|----------|--------|-----------|"]
        for b in budgets:
            lines.append(f"| {b['category']} | ${b['budgeted']:,.2f} | ${b['actual']:,.2f} | ${b['remaining']:,.2f} |")
        return "\n".join(lines)

    return f"Error: Unknown search_finances action '{action}'"


_TOOL_HANDLERS = {
    "search_vault": _tool_search_vault,
    "read_vault_file": _tool_read_vault_file,
    "search_calendar": _tool_search_calendar,
    "search_email": _tool_search_email,
    "search_drive": _tool_search_drive,
    "search_slack": _tool_search_slack,
    "search_web": _tool_search_web,
    "get_message_history": _tool_get_message_history,
    "person_info": _tool_person_info,
    "manage_tasks": _tool_manage_tasks,
    "manage_reminders": _tool_manage_reminders,
    "manage_schedules": _tool_manage_schedules,
    "manage_workouts": _tool_manage_workouts,
    "search_finances": _tool_search_finances,
    "create_email_draft": _tool_create_email_draft,
    "send_email_draft": _tool_send_email_draft,
    "create_calendar_event": _tool_create_calendar_event,
    "update_calendar_event": _tool_update_calendar_event,
    "delete_calendar_event": _tool_delete_calendar_event,
    "save_memory": _tool_save_memory,
    "search_memories": _tool_search_memories,
}

# Status messages for UI feedback when tools execute
TOOL_STATUS_MESSAGES = {
    "search_vault": "Searching notes...",
    "read_vault_file": "Reading vault file...",
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
    "manage_tasks.update": "Updating task...",
    "manage_tasks.tags": "Loading tag list...",
    "manage_reminders": "Managing reminders...",
    "manage_reminders.create": "Setting reminder...",
    "manage_reminders.list": "Loading reminders...",
    "manage_schedules": "Managing schedules...",
    "manage_schedules.create": "Setting schedule...",
    "manage_schedules.list": "Loading schedules...",
    "manage_workouts": "Updating workout log...",
    "manage_workouts.log": "Logging workout...",
    "manage_workouts.update": "Correcting log...",
    "manage_workouts.list": "Loading recent sessions...",
    "manage_workouts.history": "Loading lift history...",
    "manage_workouts.summary": "Tallying volume...",
    "manage_workouts.log_metric": "Recording metric...",
    "manage_workouts.metrics": "Loading metrics...",
    "manage_workouts.readiness": "Checking training readiness...",
    "search_finances": "Checking finances...",
    "search_finances.accounts": "Loading account balances...",
    "search_finances.transactions": "Searching transactions...",
    "search_finances.cashflow": "Loading cashflow summary...",
    "search_finances.budgets": "Checking budgets...",
    "create_email_draft": "Drafting email...",
    "send_email_draft": "Sending email...",
    "create_calendar_event": "Creating calendar event...",
    "update_calendar_event": "Updating calendar event...",
    "delete_calendar_event": "Deleting calendar event...",
    "save_memory": "Saving memory...",
    "search_memories": "Searching memories...",
}
