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
                    "description": (
                        "Number of days to search. With query: search ±N days — omit "
                        "to auto-widen through ±180 → ±365 → ±1095 days, stopping at "
                        "the first span with events; setting it disables widening. "
                        "With date_ref: range from date (default 1)."
                    ),
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
            "Use from_email/to_email for targeted searches (get email from person_info first). "
            "Scope by time with after/before; with neither, all of history is searched."
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
                "after": {
                    "type": "string",
                    "description": (
                        "Only emails on or after this date (YYYY-MM-DD). Omit to "
                        "search back to the beginning of the mailbox."
                    ),
                },
                "before": {
                    "type": "string",
                    "description": (
                        "Only emails on or before this date (YYYY-MM-DD). Omit for "
                        "no upper bound."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Max emails to return per account (default 15). Raise it when "
                        "the result says it was capped and you need the rest."
                    ),
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
        "description": (
            "Search Slack messages across DMs and channels. Semantic search over "
            "indexed messages only — an empty result means nothing indexed matches, "
            "not a sign the search broke. Optionally scope by date with after/before."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results (default 20).",
                },
                "after": {
                    "type": "string",
                    "description": (
                        "Only messages on or after this date (YYYY-MM-DD). Applied "
                        "after ranking, so a dated search can return fewer than top_k."
                    ),
                },
                "before": {
                    "type": "string",
                    "description": "Only messages on or before this date (YYYY-MM-DD).",
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
            "Requires entity_id from person_info. Can filter by date range or search term. "
            "With no date range it searches the last 90 days, then the last year, then all "
            "history, stopping at the first window with matches — so omit dates when you "
            "don't know when something was said. A 'no messages found' result from this "
            "tool means the history genuinely does not contain them, not that retrieval "
            "failed; do not report it as a sync or permissions problem."
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
                    "description": (
                        "Start date (YYYY-MM-DD). Omit to auto-widen through "
                        "90 days → 1 year → all history. Set it only to pin a "
                        "specific period; doing so disables auto-widening."
                    ),
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
            "Manage schedules: create, list, update, or delete. A schedule binds "
            "a trigger (once/cron) to an action (notify/prompt/endpoint/agent). Use "
            "action='agent' to run autonomous work on a schedule. For update/delete, "
            "pass schedule_id from a prior list; update changes only the fields you supply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "delete"],
                    "description": "Operation to perform.",
                },
                "schedule_id": {
                    "type": "string",
                    "description": "ID of the schedule to update or delete (from action='list').",
                },
                "name": {
                    "type": "string",
                    "description": "Short schedule name/title (for create; optional for update).",
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
                "enabled": {
                    "type": "boolean",
                    "description": "Enable (true) or pause (false) the schedule (for update).",
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
            "Query live financial data. "
            "Actions: 'accounts' (current balances), 'transactions' (recent spending, filterable), "
            "'cashflow' (income/expenses/savings summary), 'budgets' (budget vs actual by category), "
            "'investments' (Nathan's full portfolio: Schwab + Guideline 401k + TSP — totals, tax "
            "buckets, per-position holdings with cost basis, savings by year, wealth trend, taxable "
            "unrealized gains; aggregated nightly by the investments pipeline). "
            "'movers' (which held positions moved most today — live day-change % for the snapshot's "
            "tickers, past an optional 'threshold' percent, default 5). "
            "For historical monthly summaries, use search_vault with 'finance' or 'spending'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accounts", "transactions", "cashflow", "budgets", "investments", "movers"],
                    "description": "What financial data to retrieve.",
                },
                "start_date": {
                    "type": "string",
                    "description": (
                        "Start date (YYYY-MM-DD). For transactions, omit to auto-widen "
                        "through 90 days → 1 year → all history, stopping at the first "
                        "window with matches; setting it disables widening. "
                        "cashflow/budgets default to the 1st of the current month."
                    ),
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
                "threshold": {
                    "type": "number",
                    "description": "For action 'movers': only positions whose absolute day change exceeds this percent (default 5).",
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
            "Search saved memories by wording and meaning. Use to recall previously saved "
            "information, check if a memory already exists, or find specific remembered "
            "facts/preferences. A relevance threshold applies, so an empty result can mean "
            "the query wording missed rather than nothing being saved — the result says "
            "which. When it says candidates came close, retry with different wording or a "
            "higher limit before telling the user nothing is saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for memories.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max memories to return (default 10). Raise it when the result "
                        "says it was capped, or when widening a search that came up short."
                    ),
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
            "135). For timed/cardio work put the count (steps, meters, strokes) in "
            "`reps` and the elapsed time in `duration_seconds` ('500 stairs in "
            "7:01' = reps 500, duration_seconds 421; 'ran 4mi 32:10' = "
            "duration_seconds 1930 with the distance in `notes`). Omit `date` to "
            "log today; pass YYYY-MM-DD for an explicit day. "
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
            "- 'metrics': list a metric over time (e.g. body_weight trend). "
            "Cumulative metrics ('steps', 'active_energy') are returned as daily "
            "totals rather than raw intraday samples.\n"
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
                    "description": "Exercises (for log/update). Each: {exercise, reps, weight, unit, count, rpe, duration_seconds, notes}. `count` = number of identical sets (default 1).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "exercise": {"type": "string", "description": "Exercise name (normalized server-side)."},
                            "reps": {"type": "integer", "description": "Reps per set — also the count for timed work (steps climbed, meters rowed)."},
                            "weight": {"type": "number", "description": "Load."},
                            "unit": {"type": "string", "description": "'lb'/'kg' for weighted sets (defaults to lb when weight is set); for counted work, what reps counts ('steps', 'm'). Omit otherwise."},
                            "count": {"type": "integer", "description": "Number of identical sets (default 1)."},
                            "rpe": {"type": "number", "description": "Rate of perceived exertion (optional)."},
                            "duration_seconds": {"type": "integer", "description": "Elapsed time in seconds for timed work (stairs, runs, planks, hangs) — '7:01' = 421."},
                            "notes": {"type": "string", "description": "Per-exercise note, e.g. cardio distance ('4 mi')."},
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


# Widening ladder for a calendar keyword search with no caller-supplied range.
# ±180d covers "did we meet recently"; the wider rungs catch anniversaries and
# one-off events from previous years that the old fixed ±180d silently hid.
_CALENDAR_LADDER_DAYS = (180, 365, 1095)

# Ceiling for a caller-supplied days_range — a century either side of today,
# far beyond any real calendar and well short of the OverflowError that
# timedelta(days=...) raises on absurd values.
_CALENDAR_MAX_DAYS = 36500


async def _tool_search_calendar(inp: dict) -> str:
    from api.services.calendar import CalendarService

    query = inp.get("query")
    date_ref = inp.get("date_ref")
    # A non-int or non-positive days_range would raise inside
    # timedelta(days=...) per account, get swallowed by the handler below, and
    # come back as "No calendar events found. Searched ±30d" — a scoped-looking
    # empty for a search that never validly ran. Treat it as unstated (so the
    # ladder applies) and say the value was dropped, rather than clamping it to
    # some nearby number the caller never asked for.
    # The upper bound matters as much as the lower one: search_events does
    # `now - timedelta(days=days_back)`, which raises OverflowError past a few
    # million days. That would land in the per-account handler below and get
    # reported as an unreachable account needing re-authorisation — a bad
    # argument blamed on expired credentials, which is this same misdiagnosis
    # in a new costume.
    raw_range = inp.get("days_range")
    days_range = None
    if raw_range is not None:
        try:
            days_range = int(raw_range)
        except (TypeError, ValueError):
            days_range = None
        if days_range is not None and not 1 <= days_range <= _CALENDAR_MAX_DAYS:
            days_range = None
    bad_range = (
        f" [Ignored days_range={raw_range!r} — must be a whole number of days "
        f"between 1 and {_CALENDAR_MAX_DAYS}, so this result is NOT scoped to "
        "it.]"
        if raw_range is not None and days_range is None
        else ""
    )

    # Accounts that errored during the search. An expired token used to be
    # logged and then reported as "no events" — a real fault dressed up as an
    # empty result, which is the misdiagnosis this whole change exists to stop.
    failed_accounts: list[str] = []

    def _fetch(search_range: int | None) -> list:
        """Collect events from every configured account for one window."""
        events = []
        failed_accounts.clear()
        for account in get_configured_accounts():
            try:
                cal = CalendarService(account)
                if query:
                    events.extend(cal.search_events(query=query, days_back=search_range, days_forward=search_range))
                elif date_ref:
                    start = datetime.strptime(date_ref, "%Y-%m-%d")
                    end = start + timedelta(days=days_range or 1)
                    events.extend(cal.get_events_in_range(start, end))
                else:
                    events.extend(cal.get_upcoming_events(days=7, max_results=15))
            except Exception as e:
                logger.warning(f"Calendar {account.value} error: {e}")
                failed_accounts.append(account.value)
        return events

    # An explicit days_range is an intentional constraint — answer exactly that
    # span. Only a keyword search with no stated range walks the ladder.
    windows_tried: list[str] = []
    if query and days_range:
        all_events = _fetch(days_range)
        windows_tried.append(f"±{days_range}d")
    elif query:
        for days in _CALENDAR_LADDER_DAYS:
            all_events = _fetch(days)
            windows_tried.append(f"±{days}d")
            if all_events:
                break
    else:
        # date_ref / upcoming branches set their own range; search_range unused.
        all_events = _fetch(None)

    failed_note = ""
    if failed_accounts:
        failed_note = (
            f" [Could not reach {', '.join(failed_accounts)} — that account "
            "errored, so this is an incomplete answer, not necessarily an empty "
            "calendar. It may need re-authorising.]"
        )

    if not all_events:
        if windows_tried:
            hint = f"Nothing on the calendar matches {query!r} in that span."
            return (
                _exhausted_note("calendar events", windows_tried, hint)
                + bad_range
                + failed_note
            )
        return "No calendar events found." + bad_range + failed_note

    all_events.sort(key=lambda e: e.start_time or datetime.min)
    lines = []
    for e in all_events:
        start = e.start_time.strftime("%Y-%m-%d %H:%M") if e.start_time else "TBD"
        acct = f"[{e.source_account}]" if e.source_account else ""
        attendees = f" with {', '.join(e.attendees[:5])}" if e.attendees else ""
        loc = f" @ {e.location}" if e.location else ""
        lines.append(f"- {e.title} ({start}) {acct}{attendees}{loc}")
    body = "\n".join(lines)
    note = _ladder_note(windows_tried) + bad_range + failed_note
    return f"{len(all_events)} events{note}:\n{body}" if note else body


async def _tool_search_email(inp: dict) -> str:
    from api.services.gmail import GmailService

    # Normalised, not passed through: max_results is also the yardstick for the
    # truncation check below, so a None or 0 from the model would both confuse
    # Gmail and silently disable that disclosure.
    max_results = _positive_int(inp.get("max_results", 15), 15, 100)
    after = _parse_ymd(inp.get("after"))
    before = _parse_ymd(inp.get("before"))
    ignored = [
        f"{k}={inp[k]!r}"
        for k, parsed in (("after", after), ("before", before))
        if inp.get(k) and not parsed
    ]
    ignored_note = (
        f"\n\n[Ignored unparseable {', '.join(ignored)} — dates must be YYYY-MM-DD, "
        "so these results are NOT scoped to that range.]"
        if ignored
        else ""
    )

    all_messages = []
    truncated = False
    for account in get_configured_accounts():
        try:
            gmail = GmailService(account)
            messages = gmail.search(
                keywords=inp.get("keywords"),
                from_email=inp.get("from_email"),
                to_email=inp.get("to_email"),
                after=after,
                before=before,
                max_results=max_results,
                include_body=True,
            )
            all_messages.extend(messages)
            # A full page back is the only signal Gmail gives that it had more.
            if len(messages) == max_results:
                truncated = True
        except Exception as e:
            logger.warning(f"Gmail {account.value} error: {e}")

    if not all_messages:
        # Name the filters actually applied. A keyword that matches nothing
        # matches nothing at any max_results, so there is no retry to make —
        # but the model needs to see what was searched to pick a better filter.
        applied = [
            f"{label}={inp[key]!r}"
            for key, label in (
                ("keywords", "keywords"),
                ("from_email", "from"),
                ("to_email", "to"),
            )
            if inp.get(key)
        ]
        if after or before:
            span = " to ".join(
                d.strftime("%Y-%m-%d") for d in (after, before) if d
            )
            applied.append(f"dates {span}")
        else:
            applied.append("no date filter")
        return f"No emails found. Searched with {', '.join(applied)}.{ignored_note}"

    trunc_note = ""
    if truncated:
        trunc_note = (
            f"\n\n[Capped at {max_results} per account — more may exist. Raise "
            "max_results, or narrow with after/before, to see the rest.]"
        )

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
    return "\n\n---\n".join(lines) + ignored_note + trunc_note


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
    # Normalised for the same reason as email's max_results: it doubles as the
    # truncation yardstick, so None/0 would quietly disable that disclosure.
    top_k = _positive_int(inp.get("top_k", 20), 20, 200)
    query = inp["query"]
    results = indexer.search(query=query, top_k=top_k)

    # A full page back means the ranking was cut off, not that Slack has no more.
    truncated = len(results) == top_k

    # after/before are post-filters: the indexed metadata carries an ISO
    # `timestamp` per message, but the vector search itself can't range-filter,
    # so dates are applied to the already-ranked page. A dated search can
    # therefore return fewer than top_k even when more matches exist.
    after = _parse_ymd(inp.get("after"))
    before = _parse_ymd(inp.get("before"))
    ignored = [
        f"{k}={inp[k]!r}"
        for k, parsed in (("after", after), ("before", before))
        if inp.get(k) and not parsed
    ]
    if after:
        after = after.replace(tzinfo=timezone.utc)
    if before:
        before = before.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    ranked_count = len(results)
    undateable = 0
    if after or before:
        dated = []
        for msg in results:
            try:
                when = datetime.fromisoformat(msg.get("timestamp") or "")
            except (ValueError, TypeError):
                # Excluded because we can't place it in time, which is NOT the
                # same as falling outside the range — counted separately so the
                # empty-result text doesn't claim something it hasn't shown.
                undateable += 1
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if after and when < after:
                continue
            if before and when > before:
                continue
            dated.append(msg)
        results = dated

    notes = ""
    if ignored:
        notes += (
            f"\n\n[Ignored unparseable {', '.join(ignored)} — dates must be "
            "YYYY-MM-DD, so these results are NOT date-scoped.]"
        )
    if truncated:
        notes += (
            f"\n\n[Ranked top {top_k} only — more may match. Raise top_k to see "
            "further; note after/before filter this page after ranking, so a "
            "dated search may return fewer than top_k.]"
        )
    if undateable:
        notes += (
            f"\n\n[{undateable} matching message(s) had no readable timestamp and "
            "were excluded from the date filter — they may well be in range.]"
        )

    if not results:
        span = " to ".join(d.strftime("%Y-%m-%d") for d in (after, before) if d)
        if span and ranked_count:
            # A bigger top_k genuinely helps here: the query did match, but no
            # match on the ranked page could be shown for this range. Attribute
            # the exclusions accurately — "outside the range" and "couldn't be
            # dated" are different facts and only the first is established.
            dated_out = ranked_count - undateable
            reasons = []
            if dated_out:
                reasons.append(f"{dated_out} outside that range")
            if undateable:
                reasons.append(f"{undateable} with no readable timestamp")
            return (
                f"No Slack messages for {query!r} between {span} — of "
                f"{ranked_count} top-ranked matches, {' and '.join(reasons)}. "
                "Dates are applied after ranking, so raising top_k may surface "
                "in-range matches." + notes
            )
        # Straight top-k nearest-neighbour with no score threshold: zero results
        # means nothing indexed matches, and a larger top_k cannot change that.
        return (
            f"No Slack messages found for {query!r}"
            + (f" between {span}" if span else "")
            + ". Slack coverage is limited to the channels and DMs that have been "
            "indexed, so this means nothing indexed matches — not a sign the "
            "search broke." + notes
        )

    lines = []
    for msg in results:
        channel = msg.get("channel_name", "Unknown")
        user = msg.get("user_name", "Unknown")
        ts = msg.get("timestamp", "")[:16]
        content = msg.get("content", "")[:500]
        lines.append(f"**{channel}** - {user} ({ts}):\n{content}")
    return "\n\n".join(lines) + notes


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


def _ladder_note(attempts: list[str]) -> str:
    """Note which rungs of a widening ladder came up empty.

    Lets the model see the search was already widened rather than assuming the
    narrow window was the only one tried. Empty when the first rung hit.
    """
    if len(attempts) < 2:
        return ""
    return f" (nothing in {', '.join(attempts[:-1])}; widened to {attempts[-1]})"


def _exhausted_note(noun: str, attempts: list[str], hint: str = "") -> str:
    """Empty-result text that names every window searched.

    A bare "No X found." reads as a broken backend; naming the windows makes the
    emptiness a fact about the data instead.
    """
    searched = f" Searched {', then '.join(attempts)}." if attempts else ""
    return f"No {noun} found.{searched}" + (f" {hint}" if hint else "")


def _positive_int(raw, default: int, maximum: int) -> int:
    """Coerce a model-supplied count to a usable bound.

    The LLM fills these in, so None, "20", 0 and -5 all turn up. A bad value
    must not reach the service layer: `timedelta(days="30")` raises, and a 0 or
    None cap silently defeats the truncation checks that compare against it.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _parse_ymd(raw) -> datetime | None:
    """Parse a 'YYYY-MM-DD' filter value. None if absent or unparseable.

    Callers drop the filter on None rather than raising: a malformed date
    shouldn't cost the model its whole search, but it must be told the filter
    was ignored so it doesn't read the results as scoped.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# Widening ladder for message history when the caller gave no date range.
# Each step is tried in order and the first one with any hits wins. Widening is
# cheap by construction: it only happens on threads too sparse to fill the
# narrow window, so the wider queries scan very little.
_MSG_HISTORY_LADDER_DAYS = (90, 365, None)  # None = all history

# Payload ceiling for message history, in characters. Message *count* is a poor
# cost proxy — a year of a quiet thread is ~250 tokens while 1000 messages of a
# busy one is ~28k — so the budget is applied to formatted output instead.
_MSG_HISTORY_CHAR_BUDGET = 24000


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
    limit = inp.get("limit", 1000)

    # An explicit date range is an intentional constraint — answer exactly that
    # window. Only auto-widen when the caller expressed no preference.
    caller_set_dates = bool(start_date or end_date)

    store = get_interaction_store()

    def _fetch(window_start: str | None) -> tuple[dict, list]:
        """Fetch both sources for one window. Returns (imessage_result, whatsapp)."""
        imsg = query_person_messages(
            entity_id=resolved_id,
            search_term=search_term,
            start_date=window_start,
            end_date=end_date,
            limit=limit,
        )

        # The interaction store takes a lookback in days rather than a date.
        days_back = 36500  # ~all history
        if window_start:
            try:
                delta = datetime.now() - datetime.strptime(window_start, "%Y-%m-%d")
                days_back = max(delta.days, 1)
            except (ValueError, TypeError):
                pass

        wa = store.get_for_person(
            person_id=resolved_id,
            days_back=days_back,
            source_type="whatsapp",
            limit=limit,
        )

        if search_term and wa:
            term_lower = search_term.lower()
            wa = [i for i in wa if i.snippet and term_lower in i.snippet.lower()]

        if end_date and wa:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
                wa = [i for i in wa if i.timestamp <= end_dt]
            except (ValueError, TypeError):
                pass

        # Store returns most recent first; present chronologically.
        wa.sort(key=lambda i: i.timestamp)
        return imsg, wa

    # Walk the widening ladder until either source has hits. Breaking on either
    # (not just iMessage) keeps a recent iMessage from hiding an older WhatsApp
    # thread, and vice versa.
    windows_tried: list[str] = []
    if caller_set_dates:
        imessage_result, whatsapp_interactions = _fetch(start_date)
    else:
        for days in _MSG_HISTORY_LADDER_DAYS:
            window_start = (
                (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                if days
                else None
            )
            imessage_result, whatsapp_interactions = _fetch(window_start)
            windows_tried.append(f"last {days}d" if days else "all history")
            if imessage_result["count"] > 0 or whatsapp_interactions:
                break

    imessage_count = imessage_result["count"]
    whatsapp_count = len(whatsapp_interactions)

    if imessage_count == 0 and whatsapp_count == 0:
        if windows_tried:
            return (
                "No messages found. Searched "
                + ", then ".join(windows_tried)
                + (f" for {search_term!r}" if search_term else "")
                + ". There is no iMessage or WhatsApp history on record for this "
                "person"
                + (
                    " matching that term — try again without search_term."
                    if search_term
                    else "."
                )
            )
        window_desc = " to ".join(x for x in (start_date, end_date) if x)
        return (
            f"No messages found in the requested window ({window_desc})"
            + (f" for {search_term!r}" if search_term else "")
            + ". Retry without start_date/end_date to search all history."
        )

    widened_note = _ladder_note(windows_tried)

    # Build output. The budget is split across sources rather than applied to the
    # assembled string: the sections are grouped by source, not interleaved by
    # time, so trimming the tail of the whole document would drop all of iMessage
    # before touching WhatsApp regardless of recency.
    imsg_text = imessage_result["formatted"]
    wa_text = _format_whatsapp_interactions(whatsapp_interactions) if whatsapp_count else ""
    imsg_text, wa_text, trimmed = _split_to_budget(imsg_text, wa_text)

    trim_note = ""
    if trimmed:
        trim_note = (
            "\n\n[Oldest messages trimmed to fit context — the most recent are "
            "shown. Narrow with search_term or start_date/end_date for a "
            "specific period.]"
        )

    if imessage_count > 0 and whatsapp_count > 0:
        # Both sources — label each section
        dr = imessage_result.get("date_range")
        date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})" if dr else ""
        parts = [
            f"## iMessage ({imessage_count} messages{date_info})\n\n{imsg_text}",
            f"## WhatsApp ({whatsapp_count} messages)\n\n{wa_text}",
        ]
        total = imessage_count + whatsapp_count
        return (
            f"{total} messages from iMessage and WhatsApp{widened_note}{trim_note}:\n\n"
            + "\n\n".join(parts)
        )
    elif imessage_count > 0:
        date_info = ""
        if imessage_result.get("date_range"):
            dr = imessage_result["date_range"]
            date_info = f" ({dr['start'][:10]} to {dr['end'][:10]})"
        return f"{imessage_count} messages{date_info}{widened_note}{trim_note}:\n\n{imsg_text}"
    else:
        return f"{whatsapp_count} WhatsApp messages{widened_note}{trim_note}:\n\n{wa_text}"


def _trim_section(formatted: str, budget: int) -> str:
    """Trim one formatted message block to budget, keeping the most recent.

    Blocks are chronological (oldest first), so the tail is kept. The cut is
    aligned to a `### date` header so no message is left without the date it
    belongs under — a shown message with a wrong or missing date is worse than
    a dropped one.
    """
    if len(formatted) <= budget:
        return formatted
    kept = formatted[-budget:]
    idx = kept.find("\n### ")
    if idx != -1:
        return kept[idx + 1:]
    # No header in range — fall back to a clean line boundary.
    return kept.split("\n", 1)[1] if "\n" in kept else kept


def _split_to_budget(imsg_text: str, wa_text: str) -> tuple[str, str, bool]:
    """Fit both source blocks into the payload budget, keeping recent messages.

    Each source gets an equal share, but a block smaller than its share donates
    the unused remainder to the other — so one quiet source never wastes budget
    a busy one could use.
    """
    budget = _MSG_HISTORY_CHAR_BUDGET
    if len(imsg_text) + len(wa_text) <= budget:
        return imsg_text, wa_text, False

    if not wa_text:
        return _trim_section(imsg_text, budget), "", True
    if not imsg_text:
        return "", _trim_section(wa_text, budget), True

    share = budget // 2
    imsg_share = share + max(0, share - len(wa_text))
    wa_share = share + max(0, share - len(imsg_text))
    return (
        _trim_section(imsg_text, imsg_share),
        _trim_section(wa_text, wa_share),
        True,
    )


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


def _schedule_update(inp: dict) -> str:
    from api.services.scheduler_store import get_scheduler_store
    schedule_id = inp.get("schedule_id")
    if not schedule_id:
        return "Error: schedule_id is required for update (use action='list' to find it)."
    # Only forward fields the caller actually supplied (partial update). The
    # schedule's own action is passed as `schedule_action`, not `action`.
    fields = {
        key: inp[key]
        for key in ("name", "schedule_type", "schedule_value", "message_content", "executor", "enabled")
        if inp.get(key) is not None
    }
    if inp.get("schedule_action") is not None:
        fields["action"] = inp["schedule_action"]
    store = get_scheduler_store()
    entry = store.update(schedule_id, **fields)
    if entry is None:
        return f"Error: No schedule found with id '{schedule_id}'."
    label = f"{entry.action} (#{entry.executor})" if entry.action == "agent" and entry.executor else entry.action
    return (f"Schedule updated: \"{entry.name}\" (id: {entry.id}, action: {label}, "
            f"next: {entry.next_trigger_at})")


def _schedule_delete(inp: dict) -> str:
    from api.services.scheduler_store import get_scheduler_store
    schedule_id = inp.get("schedule_id")
    if not schedule_id:
        return "Error: schedule_id is required for delete (use action='list' to find it)."
    store = get_scheduler_store()
    entry = store.get(schedule_id)
    name = entry.name if entry else ""
    if store.delete(schedule_id):
        return f"Schedule deleted: \"{name}\" (id: {schedule_id})"
    return f"Error: No schedule found with id '{schedule_id}'."


def _tool_manage_schedules(inp: dict):
    action = inp["action"]
    if action == "create":
        return _schedule_create(inp)
    elif action == "list":
        return _schedule_list(inp)
    elif action == "update":
        return _schedule_update(inp)
    elif action == "delete":
        return _schedule_delete(inp)
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


# Result cap for memory search. Exposed to the caller so a memory that missed on
# wording can be reached by widening; also the truncation yardstick, so it has to
# be a usable positive int however the model fills it in.
_MEMORY_LIMIT_DEFAULT = 10
_MEMORY_LIMIT_MAX = 200


def _tool_search_memories(inp: dict) -> str:
    from api.services.memory_store import get_memory_store

    query = (inp.get("query") or "").strip()
    if not query:
        return "search_memories needs a non-empty query."

    store = get_memory_store()
    limit = _positive_int(inp.get("limit", _MEMORY_LIMIT_DEFAULT), _MEMORY_LIMIT_DEFAULT, _MEMORY_LIMIT_MAX)
    memories, stats = store.search_memories_detailed(query, limit=limit)

    notes = ""
    # The corpus bound is a fact about what was checked, never about what exists:
    # a memory saved before the newest N was not scored at all.
    if stats.total_saved > stats.searched:
        notes += (
            f"\n\n[Scored the {stats.searched} most recently saved memories of "
            f"{stats.total_saved} saved in total — anything older was not checked, "
            "so this is not a complete look at everything saved.]"
        )
    # stats.matched counts matches before the cap, so this fires only when the
    # cap actually hid something.
    if stats.matched > limit:
        notes += (
            f"\n\n[Showing {limit} of {stats.matched} matching memories — raise "
            "`limit` to see the rest.]"
        )
    if not stats.semantic_available:
        notes += (
            "\n\n[Meaning-based recall is offline for this search, so only word "
            "overlap was scored — a memory phrased differently from the query may "
            "have been missed. Retrying in the memory's likely wording helps.]"
        )

    if not memories:
        if stats.near_misses:
            # Candidates existed and were scored; only the relevance floors kept
            # them out. Rewording is the fix, not concluding the memory is gone.
            return (
                f"No saved memory cleared the relevance threshold for {query!r}, but "
                f"{stats.near_misses} came close. Something relevant is likely saved: "
                "retry with the wording the memory itself probably uses, or with "
                "fewer, more specific terms." + notes
            )
        if stats.total_saved > stats.searched:
            # The corpus bound cut the search short, so absence was never established.
            return (
                f"No match for {query!r} among the {stats.searched} most recently "
                f"saved memories, of {stats.total_saved} saved in total. Older "
                "memories were not scored, so this does not establish that the "
                "thing was never saved." + notes
            )
        if stats.total_saved == 0:
            return (
                f"Nothing saved matches {query!r} — no memories have been saved yet, "
                "so there was nothing to search."
            )
        if stats.semantic_available:
            return (
                f"Nothing saved matches {query!r}. All {stats.total_saved} saved "
                "memories were scored on both wording and meaning, and none "
                "matched." + notes
            )
        # Meaning was never scored, so only a wording miss was established.
        return (
            f"No saved memory matches the wording of {query!r}. All "
            f"{stats.total_saved} saved memories were checked for word overlap."
            + notes
        )

    lines = []
    for m in memories:
        lines.append(f"- [{m.category}] {m.content}")
    return "\n".join(lines) + notes


# -- Workout helpers (fitness bot backend) --

def _fmt_num(n) -> str:
    if n is None:
        return ""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _fmt_duration(seconds) -> str:
    from api.services.fitness_store import format_duration
    return format_duration(seconds)


def _summarize_session(session) -> str:
    """Compact one-line summary, grouping consecutive identical sets."""
    groups: list[list] = []
    for s in session.sets:
        key = (s.exercise, s.reps, s.weight, s.unit, s.duration_seconds)
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
        else:
            groups.append([key, 1])
    parts = []
    for (exercise, reps, weight, unit, duration), count in groups:
        dur = f" in {_fmt_duration(duration)}" if duration else ""
        if reps is None and weight is None:
            parts.append(f"{exercise}{dur}")
            continue
        rep_part = f"{count}×{reps}" if count > 1 else f"{_fmt_num(reps)}"
        if unit and weight is None:
            rep_part += f" {unit}"   # counted work: "500 steps"
        w = f" @{_fmt_num(weight)} {unit or 'lb'}" if weight else ""
        parts.append(f"{exercise} {rep_part}{w}{dur}".strip())
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
        w = f" @{_fmt_num(r['weight'])} {r['unit']}" if r["weight"] else ""
        rpe = f" RPE {_fmt_num(r['rpe'])}" if r["rpe"] else ""
        dur = f" in {_fmt_duration(r.get('duration_seconds'))}" if r.get("duration_seconds") else ""
        sid = f" [{r['session_id']}]" if r.get("session_id") else ""
        lines.append(f"  {r['date']}: {_fmt_num(r['reps'])} reps{w}{rpe}{dur}{sid}")
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
    label = metric_type.replace("_", " ")
    limit = int(inp.get("limit", 100) or 100)

    # Cumulative metrics (steps, active energy) arrive from Apple Health as many
    # intraday buckets — sum them to one daily total so the trend is readable.
    if metric_type in _CUMULATIVE_METRICS:
        days = store.daily_metric_totals(
            metric_type, start=inp.get("date_start"), end=inp.get("date_end"), limit=limit,
        )
        if not days:
            return f"No {label} recorded."
        lines = [f"{label} (daily total):"]
        for d in days:
            unit = f" {d['unit']}" if d["unit"] else ""
            lines.append(f"  {d['date']}: {_fmt_num(d['value'])}{unit}")
        return "\n".join(lines)

    rows = store.list_metrics(metric_type, start=inp.get("date_start"), end=inp.get("date_end"), limit=limit)
    if not rows:
        return f"No {label} recorded."
    lines = [f"{label}:"]
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

# Cumulative metrics: Apple Health emits many intraday buckets per day, so the
# meaningful view is a daily SUM, not the raw per-sample list (see #333).
_CUMULATIVE_METRICS = frozenset({"steps", "active_energy"})


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


# Widening ladder for transactions when the caller gave no start_date.
# None = all history (the Monarch client omits start_date entirely).
_TXN_LADDER_DAYS = (90, 365, None)

# Row cap for a transactions fetch, passed explicitly so the number is known
# here and can be disclosed. Monarch's own default is also 500, so before this
# was passed a widened window silently topped out with no indication. Ordering
# is newest-first (the client hardcodes orderBy="date", verified descending),
# so hitting the cap drops the OLDEST rows, not the most recent.
_TXN_ROW_CAP = 500


async def _tool_search_finances(inp: dict) -> str:
    from api.services.monarch import get_monarch_client

    action = inp["action"]

    if action == "investments":
        # Schwab pipeline snapshot, synced from nathan-macbook via Syncthing
        # (see api/routes/investments.py). Read from disk — no client needed.
        import json as _json
        import os as _os
        path = _os.path.expanduser("~/Code/Sync/investments/summary.json")
        if not _os.path.exists(path):
            return "Investments snapshot not synced yet (macbook refresh hasn't run)."
        with open(path) as f:
            inv = _json.load(f)
        synced = datetime.fromtimestamp(_os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        t = inv["totals"]
        lines = [
            f"Investments (as of {inv['as_of']}, synced {synced}):",
            f"- Total: ${t['all_investments']:,.0f} (Schwab ${t['schwab']:,.0f} + external retirement ${t['external_retirement']:,.0f})",
            f"- Tax buckets: pre-tax ${t['tax_buckets']['pretax']:,.0f} · Roth ${t['tax_buckets']['roth']:,.0f} · taxable ${t['tax_buckets']['taxable']:,.0f}",
            "", "Accounts:",
        ]
        for a in sorted(inv["accounts"], key=lambda x: -x["value"]):
            tag = " (external)" if a["external"] else ""
            lines.append(f"- {a['name']} [{a['key']}]{tag}: ${a['value']:,.0f}")
        lines += ["", f"Positions ({len(inv['positions'])}):"]
        for pos in inv["positions"]:
            unrl = f", unrealized ${pos['unrealized']:+,.0f}" if pos.get("unrealized") is not None else ""
            # Include the security name alongside the ticker: the model's
            # world knowledge can be stale (a recently-IPO'd company reads as
            # "private, can't be in a portfolio"), so a bare ticker it doesn't
            # recognize gets overridden by that prior. The desc makes
            # "do I own <company>?" a literal text match.
            desc = (pos.get("desc") or "").strip()
            label = f"{pos['symbol']} — {desc}" if desc else pos["symbol"]
            lines.append(f"- {label}: ${pos['value']:,.0f} ({pos['weight_pct']}%{unrl})")
        tu = inv["taxable_unrealized"]
        lines += [
            "",
            f"Taxable unrealized: LT ${tu['long_term']:,.0f} · ST ${tu['short_term']:,.0f} · harvestable ${tu['harvestable_losses']:,.0f}",
            f"Net saved by year: {inv['savings_net_by_year']}",
            "Full detail: GET /api/investments/portfolio (positions incl. lots/flows, wealth history).",
        ]
        return "\n".join(lines)

    if action == "movers":
        # On-demand "which of my positions moved most today?" — reuses the noon
        # big-mover check (live day-change via yfinance for the snapshot's tickers).
        from api.routes.investments import MOVER_THRESHOLD_PCT, _held_tickers, investments_movers
        threshold = inp.get("threshold")
        if not isinstance(threshold, (int, float)) or threshold <= 0:
            threshold = MOVER_THRESHOLD_PCT
        if not _held_tickers():
            # Distinguish "couldn't check" from a genuinely quiet day — on an
            # on-demand ask, an empty count from a missing snapshot shouldn't read
            # as "the market was flat."
            return "Couldn't check movers right now — the investments snapshot isn't available."
        result = await investments_movers(threshold=threshold)
        if result.get("count"):
            return result["scheduler_message"]
        return f"No held position moved more than {threshold:g}% today."

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
        # Normalise before use: these come from the model, and an unparseable
        # date passed through to Monarch would either error or be ignored
        # server-side, leaving results that silently aren't scoped as asked.
        raw_start, raw_end = inp.get("start_date"), inp.get("end_date")
        start = raw_start if _parse_ymd(raw_start) else None
        end = raw_end if _parse_ymd(raw_end) else None
        dropped = [
            f"{label}={raw!r}"
            for raw, label in ((raw_start, "start_date"), (raw_end, "end_date"))
            if raw and not _parse_ymd(raw)
        ]
        dropped_note = (
            f" [Ignored unparseable {', '.join(dropped)} — dates must be "
            "YYYY-MM-DD, so this result is NOT scoped to them.]"
            if dropped
            else ""
        )
        search = inp.get("search", "")
        category = inp.get("category")

        async def _fetch(window_start: str | None) -> list:
            # Monarch rejects a one-sided range ("You must specify both a
            # startDate and endDate"), so fill in whichever half is missing.
            # Unbounded is expressed as *neither* bound, not as a null start.
            window_end = end
            if window_start and not window_end:
                window_end = datetime.now().strftime("%Y-%m-%d")
            elif window_end and not window_start:
                window_start = "1900-01-01"
            # `category` is deliberately NOT passed down. The client applies it
            # client-side *after* the row cap (monarch.py), so a capped fetch
            # would hand back a filtered handful and hide the fact that the cap
            # bound at all. Filtering here keeps the cap and the filter on the
            # same side, so the pre-filter row count stays knowable.
            return await client.get_transactions(
                start_date=window_start, end_date=window_end,
                search=search, limit=_TXN_ROW_CAP,
            )

        def _by_category(rows: list) -> list:
            if not category:
                return rows
            wanted = category.lower()
            return [r for r in rows if (r.get("category") or "").lower() == wanted]

        # An explicit start_date is an intentional constraint — answer exactly
        # that window. With none, walk the ladder rather than silently clamping
        # to 30 days: a merchant last charged 8 months ago is not "no data".
        #
        # The ladder counts back from end_date when one was given, not from
        # today. Counting from today would build an inverted start>end window
        # for any past end_date — a range that can only return zero, which the
        # ladder note would then misreport as "nothing in the last 90d".
        anchor = _parse_ymd(end) or datetime.now()
        anchored = " before " + anchor.strftime("%Y-%m-%d") if _parse_ymd(end) else ""
        windows_tried: list[str] = []
        capped = False
        if start:
            fetched = await _fetch(start)
            capped = len(fetched) >= _TXN_ROW_CAP
            txns = _by_category(fetched)
        else:
            for days in _TXN_LADDER_DAYS:
                window_start = (
                    (anchor - timedelta(days=days)).strftime("%Y-%m-%d")
                    if days
                    else None
                )
                fetched = await _fetch(window_start)
                capped = len(fetched) >= _TXN_ROW_CAP
                txns = _by_category(fetched)
                windows_tried.append(
                    f"last {days}d{anchored}" if days else f"all history{anchored}"
                )
                if txns:
                    break
                # Rows come back newest-first, so once a window fills the cap the
                # wider rungs return that same newest page — they can only add
                # older rows the cap already excluded. Continuing would burn API
                # calls and, worse, let the note claim "all history" was searched
                # when it never got past the cap.
                if capped:
                    break

        if not txns:
            # A capped fetch means absence was never established: the filter ran
            # over one page, not the window. Claiming "no transactions on record"
            # here is the original misdiagnosis wearing a different hat.
            if capped:
                scope = f" in category {category!r}" if category else ""
                return (
                    _exhausted_note("transactions", windows_tried)
                    + f" Nothing{scope} in the {_TXN_ROW_CAP} most recent rows of "
                    "that window, but the window holds more than that — this is "
                    "NOT a confirmed absence. Narrow with start_date/end_date, or "
                    "use a search term, to look past the cap." + dropped_note
                )
            if search:
                hint = f"Nothing matched {search!r} — retry without the search term."
            elif start:
                hint = "Retry without start_date to search all history."
            else:
                hint = "There are no transactions on record" + (
                    f" in category {category!r}." if category else "."
                )
            return _exhausted_note("transactions", windows_tried, hint) + dropped_note

        capped_note = ""
        if capped:
            capped_note = (
                f" [Fetched the {_TXN_ROW_CAP} most recent rows in this window and "
                "older ones were dropped, so this may be incomplete. Narrow with "
                "start_date/end_date or a search term to see past the cap.]"
            )
        lines = [
            f"{len(txns)} transactions{_ladder_note(windows_tried)}:"
            f"{dropped_note}{capped_note}"
        ]
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
    "manage_schedules.update": "Updating schedule...",
    "manage_schedules.delete": "Removing schedule...",
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
    "search_finances.movers": "Checking today's movers...",
    "create_email_draft": "Drafting email...",
    "send_email_draft": "Sending email...",
    "create_calendar_event": "Creating calendar event...",
    "update_calendar_event": "Updating calendar event...",
    "delete_calendar_event": "Deleting calendar event...",
    "save_memory": "Saving memory...",
    "search_memories": "Searching memories...",
}
