#!/usr/bin/env python3
"""
Dynamic MCP Server for LifeOS API.

Automatically discovers endpoints from the LifeOS OpenAPI spec and exposes them
as Claude Code tools. No manual updates needed when the API changes.

Usage:
    python mcp_server.py

Register with Claude Code:
    claude mcp add lifeos -s user -- python /path/to/mcp_server.py
"""
import json
import sys
import httpx
import logging
import os
from pathlib import Path

# Configure logging to stderr (stdout is for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Repo-anchored agent-worker store locations. This server is spawned as a
# stdio MCP child by CLI agents (codex / claude_code) with the agent's `-C`
# working dir as cwd — NOT the repo — so the stores' default cwd-relative
# paths would open a phantom empty DB and every inter-agent call would fail
# with "no_caller". Anchoring to this file's directory (the repo root) keeps
# inter-agent tools pointed at the real worker store regardless of cwd. Tests
# monkeypatch these to sandbox.
_REPO_ROOT = Path(__file__).resolve().parent
AGENT_SESSIONS_DB = _REPO_ROOT / "data" / "agent_sessions.db"
AGENT_TRANSCRIPTS_DIR = _REPO_ROOT / "data" / "agent_transcripts"

API_BASE = os.environ.get("LIFEOS_API_URL", "http://localhost:8000")
OPENAPI_URL = f"{API_BASE}/openapi.json"

# Curated list of endpoints to expose as tools (path -> tool config)
# This allows us to control which endpoints are exposed and how they're described
CURATED_ENDPOINTS = {
    "/api/ask": {
        "name": "lifeos_ask",
        "description": "RAG synthesis over the vault. Returns a natural-language answer with citations. Use for open-ended questions ('what did we discuss about X?'). For raw chunks, use lifeos_search.",
        "method": "POST"
    },
    "/api/search": {
        "name": "lifeos_search",
        "description": "Search the vault without synthesis. Returns raw document chunks with relevance scores. Use when you need specific documents or want to process results yourself. For synthesized answers, use lifeos_ask instead.",
        "method": "POST"
    },
    "/api/calendar/upcoming": {
        "name": "lifeos_calendar_upcoming",
        "description": "Get upcoming calendar events for the next N days. Use for 'what's on my calendar?' or 'what meetings do I have this week?'. For searching past events, use lifeos_calendar_search instead.",
        "method": "GET"
    },
    "/api/calendar/search": {
        "name": "lifeos_calendar_search",
        "description": "Search past and future calendar events by keyword ('when did I meet with X?'). For upcoming events only, use lifeos_calendar_upcoming.",
        "method": "GET"
    },
    "/api/gmail/search": {
        "name": "lifeos_gmail_search",
        "description": "Search Gmail. Returns metadata + full body for top 5 results. Filter by account (personal/work).",
        "method": "GET"
    },
    "/api/drive/search": {
        "name": "lifeos_drive_search",
        "description": "Search files in Google Drive by name or content. Returns file metadata with links. Use for 'find the document about X' or 'what files do I have about Y?'.",
        "method": "GET"
    },
    "/api/vault/write": {
        "name": "lifeos_vault_write",
        "description": "Write a text file into the Obsidian vault. Use for any task needing a `.md` deliverable. `path` is vault-relative; parents auto-created. `mode`: `create` (default), `overwrite`, `append`.",
        "method": "POST"
    },
    "/api/conversations": {
        "name": "lifeos_conversations_list",
        "description": "List recent LifeOS conversations. Returns conversation IDs and titles for continuing previous chats.",
        "method": "GET"
    },
    "/api/memories": {
        "name": "lifeos_memories_create",
        "description": "Save a memory for future reference. Use when user says 'remember that...' or wants to store information for later. Memories persist across conversations.",
        "method": "POST"
    },
    "/api/memories/search/{query}": {
        "name": "lifeos_memories_search",
        "description": "Search saved memories. Use when user asks 'what did I tell you about X?' or wants to recall previously saved information.",
        "method": "GET"
    },
    "/api/people/search": {
        "name": "lifeos_people_search",
        "description": "Find people by name/email. Returns entity_id (required by lifeos_person_*), relationship_strength, active_channels. Always call first to get entity_id.",
        "method": "GET"
    },
    "/api/crm/people/{entity_id}:PATCH": {
        "name": "lifeos_person_update",
        "description": "Update a person's CRM profile (notes, tags, category, birthday MM-DD). Requires entity_id from lifeos_people_search; only provided fields change.",
        "method": "PATCH",
        "path": "/api/crm/people/{entity_id}"
    },
    "/health/services": {
        "name": "lifeos_health",
        "description": "Check if all LifeOS services are healthy. Use for debugging connection issues.",
        "method": "GET"
    },
    "/api/imessage/search": {
        "name": "lifeos_imessage_search",
        "description": "Search iMessage/SMS history. Filter by phone, entity_id, date range, or direction (sent/received).",
        "method": "GET"
    },
    "/api/gmail/drafts": {
        "name": "lifeos_gmail_draft",
        "description": "Create a Gmail draft (NOT sent — user reviews). Returns draft ID + URL. Required: to, subject, body. Optional: cc, bcc, account.",
        "method": "POST"
    },
    "/api/gmail/send": {
        "name": "lifeos_gmail_send",
        "description": "Send an existing Gmail draft (from lifeos_gmail_draft) by draft_id. SAFETY: only after the user reviews and confirms; never send a draft created this same turn. Required: draft_id. Optional: account.",
        "method": "POST"
    },
    "/api/slack/search": {
        "name": "lifeos_slack_search",
        "description": "Semantic search across Slack DMs, group DMs, and channels. Returns channel, user, content.",
        "method": "POST"
    },
    "/api/crm/people/{person_id}/facts": {
        "name": "lifeos_person_facts",
        "description": "Get extracted facts about a person (family, interests, work, dates, travel) with confidence scores. Requires entity_id. Use before drafting personalized messages.",
        "method": "GET"
    },
    "/api/crm/people/{entity_id}/facts/{fact_id}:PUT": {
        "name": "lifeos_person_fact_update",
        "description": "Update an extracted fact about a person. Use lifeos_person_facts first to get fact IDs. FOLLOW-UP TOOLS: Use lifeos_person_facts to verify the update.",
        "method": "PUT",
        "path": "/api/crm/people/{entity_id}/facts/{fact_id}"
    },
    "/api/crm/people/{entity_id}/facts/{fact_id}/confirm:POST": {
        "name": "lifeos_person_fact_confirm",
        "description": "Confirm an extracted fact as accurate. Confirmed facts are weighted higher in person profiles. Use lifeos_person_facts to find fact IDs.",
        "method": "POST",
        "path": "/api/crm/people/{entity_id}/facts/{fact_id}/confirm"
    },
    "/api/crm/people/{entity_id}/facts/{fact_id}:DELETE": {
        "name": "lifeos_person_fact_delete",
        "description": "Delete an incorrect or outdated fact about a person. Use lifeos_person_facts to find fact IDs.",
        "method": "DELETE",
        "path": "/api/crm/people/{entity_id}/facts/{fact_id}"
    },
    "/api/crm/people/{person_id}": {
        "name": "lifeos_person_profile",
        "description": "Full CRM profile: emails, phones, company, relationship_strength, tags, notes, facts. Requires entity_id. Use for 'tell me about X' or full contact details.",
        "method": "GET"
    },
    "/api/crm/people/{person_id}/timeline": {
        "name": "lifeos_person_timeline",
        "description": "Chronological interactions for a person (emails, messages, meetings, vault). Requires entity_id. Use for 'catch me up on X'.",
        "method": "GET"
    },
    "/api/calendar/meeting-prep": {
        "name": "lifeos_meeting_prep",
        "description": "Meeting prep for a date: each event with related notes, past meetings with same attendees, attachments. Defaults to today.",
        "method": "GET"
    },
    "/api/crm/family/communication-gaps": {
        "name": "lifeos_communication_gaps",
        "description": "Find people not contacted recently. Required: comma-separated person_ids (entity_ids). Returns days since last contact + gap stats.",
        "method": "GET"
    },
    "/api/crm/people/{person_id}/connections": {
        "name": "lifeos_person_connections",
        "description": "People connected to a person via shared meetings, emails, messages, LinkedIn. Requires entity_id. Use for 'who does X work with?'.",
        "method": "GET"
    },
    "/api/crm/relationship/insights": {
        "name": "lifeos_relationship_insights",
        "description": "Relationship insights (communication_patterns, emotional_needs, conflict_areas, growth_areas) extracted from notes. Optional person_id; defaults to primary relationship.",
        "method": "GET"
    },
    "/api/photos/person/{person_id}": {
        "name": "lifeos_photos_person",
        "description": "Photos of a person (Apple Photos face recognition). Requires entity_id. Use for 'show me photos of X'.",
        "method": "GET"
    },
    "/api/photos/shared/{person_a_id}/{person_b_id}": {
        "name": "lifeos_photos_shared",
        "description": "Photos where two people appear together. Required: person_a_id + person_b_id (entity_ids). Use for 'pictures of X and Y together'.",
        "method": "GET"
    },
    "/api/photos/stats": {
        "name": "lifeos_photos_stats",
        "description": "Apple Photos face-recognition stats: named_people, face_detections, multi_person_photos, integration status.",
        "method": "GET"
    },
    "/api/reminders:POST": {
        "name": "lifeos_reminder_create",
        "description": "Schedule a Telegram reminder. schedule_type=once|cron; message_type=static|prompt|endpoint (prompt runs full LifeOS pipeline).",
        "method": "POST",
        "path": "/api/reminders"
    },
    "/api/reminders:GET": {
        "name": "lifeos_reminder_list",
        "description": "List all scheduled reminders with their status, next trigger time, and configuration.",
        "method": "GET",
        "path": "/api/reminders"
    },
    "/api/reminders/{reminder_id}:PUT": {
        "name": "lifeos_reminder_update",
        "description": "Update a reminder by reminder_id (from lifeos_reminder_list). Only provided fields change.",
        "method": "PUT",
        "path": "/api/reminders/{reminder_id}"
    },
    "/api/reminders/{reminder_id}:DELETE": {
        "name": "lifeos_reminder_delete",
        "description": "Delete a scheduled reminder by ID. Use lifeos_reminder_list first to find the ID.",
        "method": "DELETE",
        "path": "/api/reminders/{reminder_id}"
    },
    "/api/scheduler:POST": {
        "name": "lifeos_schedule_create",
        "description": "Create a schedule. schedule_type=once|cron; action=notify|prompt|endpoint|agent (agent writes an #agent task, executor=local|cloud|cloud-haiku|cloud-sonnet).",
        "method": "POST",
        "path": "/api/scheduler"
    },
    "/api/scheduler:GET": {
        "name": "lifeos_schedule_list",
        "description": "List all schedules with their action, status, next trigger time, and last outcome.",
        "method": "GET",
        "path": "/api/scheduler"
    },
    "/api/scheduler/{schedule_id}:PUT": {
        "name": "lifeos_schedule_update",
        "description": "Update a schedule by schedule_id (from lifeos_schedule_list). Only provided fields change.",
        "method": "PUT",
        "path": "/api/scheduler/{schedule_id}"
    },
    "/api/scheduler/{schedule_id}:DELETE": {
        "name": "lifeos_schedule_delete",
        "description": "Delete a schedule by ID. Use lifeos_schedule_list first to find the ID.",
        "method": "DELETE",
        "path": "/api/scheduler/{schedule_id}"
    },
    "/api/reminders/send": {
        "name": "lifeos_telegram_send",
        "description": "Send an immediate message via Telegram. Use for ad-hoc notifications or testing. Requires Telegram to be configured.",
        "method": "POST"
    },
    "/api/admin/sync:POST": {
        "name": "lifeos_sync_trigger",
        "description": "Trigger a background data sync. source: vault, calendar, contacts, slack, photos, gmail, imessage, phone, facetime, linkedin.",
        "method": "POST",
        "path": "/api/admin/sync",
        "custom_handler": True
    },
    "/api/monarch/accounts": {
        "name": "lifeos_monarch_accounts",
        "description": "List all financial accounts with current balances from Monarch Money. Returns account name, type (checking, savings, credit card, investment), balance, and institution.",
        "method": "GET"
    },
    "/api/monarch/transactions": {
        "name": "lifeos_monarch_transactions",
        "description": "Search recent financial transactions from Monarch Money. Filter by date range, category, or merchant name. Returns date, merchant, category, amount, and account. Defaults to last 30 days.",
        "method": "GET"
    },
    "/api/monarch/cashflow": {
        "name": "lifeos_monarch_cashflow",
        "description": "Get cashflow summary from Monarch Money for a date range. Returns total income, expenses, savings rate, and spending breakdown by category. Defaults to current month.",
        "method": "GET"
    },
    "/api/monarch/budgets": {
        "name": "lifeos_monarch_budgets",
        "description": "Get current budget status from Monarch Money. Returns each budget with budgeted amount, actual spending, and remaining balance. Defaults to current month.",
        "method": "GET"
    },
    "/api/tasks:POST": {
        "name": "lifeos_task_create",
        "description": "Create a task (Obsidian markdown). Supports context, priority, due_date, tags. With dry_run=true + #agent tag, returns preflight routing + cost estimate instead of creating.",
        "method": "POST",
        "path": "/api/tasks"
    },
    "/api/tasks:GET": {
        "name": "lifeos_task_list",
        "description": "List and filter tasks. Supports filtering by status (todo/done/in_progress/cancelled/deferred/blocked/urgent), context, tag, due_before date, and fuzzy text query (e.g., query='taxes' finds '1099').",
        "method": "GET",
        "path": "/api/tasks"
    },
    "/api/tasks/{task_id}:PUT": {
        "name": "lifeos_task_update",
        "description": "Update a task's description, status, context, priority, due_date, or tags. Use lifeos_task_list first to find the task ID.",
        "method": "PUT",
        "path": "/api/tasks/{task_id}"
    },
    "/api/tasks/{task_id}/complete:PUT": {
        "name": "lifeos_task_complete",
        "description": "Mark a task as done. Shortcut for updating status to 'done'. Sets done_date automatically.",
        "method": "PUT",
        "path": "/api/tasks/{task_id}/complete"
    },
    "/api/tasks/{task_id}:DELETE": {
        "name": "lifeos_task_delete",
        "description": "Delete a task by ID. Removes it from the vault markdown file and index.",
        "method": "DELETE",
        "path": "/api/tasks/{task_id}"
    },
    "/api/calendar/events:POST": {
        "name": "lifeos_calendar_create",
        "description": "Create a Google Calendar event. Required: title, start_time, end_time (ISO). Optional: attendees, description, location, account. Invites are auto-sent. [CLARIFY] before adding attendees.",
        "method": "POST",
        "path": "/api/calendar/events"
    },
    "/api/calendar/events/{event_id}:PUT": {
        "name": "lifeos_calendar_update",
        "description": "Update a calendar event by event_id (from lifeos_calendar_search). Only provided fields change. Update emails are sent. [CLARIFY] first.",
        "method": "PUT",
        "path": "/api/calendar/events/{event_id}"
    },
    "/api/calendar/events/{event_id}:DELETE": {
        "name": "lifeos_calendar_delete",
        "description": "Delete a calendar event. Requires event_id from lifeos_calendar_search. Optional: account (personal/work). Cancellation emails are sent to attendees. [CLARIFY] before deleting events.",
        "method": "DELETE",
        "path": "/api/calendar/events/{event_id}"
    },
}


class LifeOSMCPServer:
    """MCP Server that dynamically discovers LifeOS API endpoints."""

    def __init__(self):
        self.client = httpx.Client(timeout=30.0)
        self.openapi_spec: dict | None = None
        self.tools: list[dict] = []
        # Per-session tool-result cache (#139 §4). Bypassed when the caller
        # doesn't supply a session id (which is the common case for local-CLI
        # tool calls; cache hits matter most on managed-agent HTTP calls).
        try:
            from api.services.agent_worker.tool_result_cache import (
                SessionToolResultCache,
            )
            self._result_cache = SessionToolResultCache()
        except Exception:  # pragma: no cover — keep server bootable without agent worker
            self._result_cache = None
        self._load_openapi_spec()
        self._register_inter_agent_tools()

    def _register_inter_agent_tools(self) -> None:
        """Expose the `lifeos_agent_*` family to remote agents over MCP.

        Each tool takes a required `caller_session_id` argument so the
        worker knows which session is acting (the local executor injects
        this automatically; remote managed agents pass it explicitly via
        the system prompt instruction). No sandboxing per the project's
        no-sandbox design — operators trust their own agents.
        """
        try:
            from api.services.agent_worker.inter_agent import INTER_AGENT_TOOL_SCHEMAS
        except Exception as e:  # pragma: no cover — keep MCP server bootable without agent worker
            logger.warning(f"inter-agent tools not registered: {e}")
            return
        for schema in INTER_AGENT_TOOL_SCHEMAS:
            tool = dict(schema)
            # Inject caller_session_id into the published input schema. The
            # tool definitions don't include it (the local registry sets it
            # automatically) but remote callers must pass it explicitly.
            input_schema = dict(tool["input_schema"])
            props = dict(input_schema.get("properties", {}))
            props["caller_session_id"] = {
                "type": "string",
                "description": "Your own session_id — included in your system prompt.",
            }
            input_schema["properties"] = props
            required = list(input_schema.get("required", []))
            if "caller_session_id" not in required:
                required = ["caller_session_id"] + required
            input_schema["required"] = required
            tool["input_schema"] = input_schema
            # MCP schema field name in this server is "inputSchema" — match
            # what _build_tools_from_spec produces.
            tool["inputSchema"] = tool.pop("input_schema")
            self.tools.append(tool)

    def _load_openapi_spec(self):
        """Load OpenAPI spec from LifeOS API."""
        try:
            resp = self.client.get(OPENAPI_URL)
            resp.raise_for_status()
            self.openapi_spec = resp.json()
            self._build_tools_from_spec()
            logger.info(f"Loaded OpenAPI spec: {len(self.tools)} tools available")
        except Exception as e:
            logger.warning(f"Could not load OpenAPI spec: {e}. Using curated endpoints only.")
            self._build_tools_fallback()

    def _build_tools_from_spec(self):
        """Build tool definitions from OpenAPI spec."""
        if not self.openapi_spec:
            return

        paths = self.openapi_spec.get("paths", {})
        schemas = self.openapi_spec.get("components", {}).get("schemas", {})

        for path_key, config in CURATED_ENDPOINTS.items():
            # Use explicit path if provided, otherwise strip method suffix
            actual_path = config.get("path", path_key.split(":")[0])
            # Find matching path in OpenAPI spec (handle path parameters)
            spec_path = self._find_spec_path(actual_path, paths)
            if not spec_path:
                if config.get("custom_handler"):
                    # Custom handler tools use fallback schemas
                    self.tools.append({
                        "name": config["name"],
                        "description": config["description"],
                        "inputSchema": self._get_fallback_schema(config["name"])
                    })
                else:
                    logger.debug(f"Path {actual_path} not found in OpenAPI spec")
                continue

            method = config["method"].lower()
            endpoint_spec = paths.get(spec_path, {}).get(method, {})

            tool = {
                "name": config["name"],
                "description": config["description"],
                "inputSchema": self._build_input_schema(endpoint_spec, schemas, method, actual_path)
            }
            self.tools.append(tool)

    def _find_spec_path(self, curated_path: str, paths: dict) -> str | None:
        """Find the matching OpenAPI spec path for a curated path."""
        # Direct match
        if curated_path in paths:
            return curated_path

        # Handle path parameters (e.g., /api/memories/search/{query})
        for spec_path in paths:
            # Convert OpenAPI path params to regex-like pattern
            pattern = spec_path.replace("{", "(?P<").replace("}", ">[^/]+)")
            import re
            if re.fullmatch(pattern, curated_path):
                return spec_path

        return None

    def _build_input_schema(self, endpoint_spec: dict, schemas: dict, method: str, path: str) -> dict:
        """Build JSON Schema for tool input from OpenAPI endpoint spec."""
        properties = {}
        required = []

        # Handle query parameters (GET requests)
        for param in endpoint_spec.get("parameters", []):
            if param.get("in") == "query":
                name = param["name"]
                param_schema = param.get("schema", {"type": "string"})
                properties[name] = {
                    "type": param_schema.get("type", "string"),
                    "description": param.get("description", f"Query parameter: {name}")
                }
                if param.get("required"):
                    required.append(name)

        # Handle path parameters
        if "{" in path:
            import re
            path_params = re.findall(r"\{(\w+)\}", path)
            for param_name in path_params:
                properties[param_name] = {
                    "type": "string",
                    "description": f"Path parameter: {param_name}"
                }
                required.append(param_name)

        # Handle request body (POST/PUT/PATCH requests)
        if method in ("post", "put", "patch"):
            request_body = endpoint_spec.get("requestBody", {})
            content = request_body.get("content", {})
            json_content = content.get("application/json", {})
            body_schema = json_content.get("schema", {})

            # Resolve $ref if present
            if "$ref" in body_schema:
                ref_name = body_schema["$ref"].split("/")[-1]
                body_schema = schemas.get(ref_name, {})

            # Merge body properties into tool schema
            for prop_name, prop_schema in body_schema.get("properties", {}).items():
                properties[prop_name] = {
                    "type": prop_schema.get("type", "string"),
                    "description": prop_schema.get("description", f"Request field: {prop_name}")
                }
                if prop_schema.get("default") is not None:
                    properties[prop_name]["default"] = prop_schema["default"]

            # Add required fields
            for req_field in body_schema.get("required", []):
                if req_field not in required:
                    required.append(req_field)

        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _get_fallback_schema(self, tool_name: str) -> dict:
        """Get fallback input schema for a tool by name."""
        return self._fallback_schemas().get(tool_name, {"type": "object", "properties": {}})

    def _fallback_schemas(self) -> dict:
        """Return fallback schemas for all tools."""
        return {
            "lifeos_ask": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask"},
                    "include_sources": {"type": "boolean", "description": "Include source citations", "default": True}
                },
                "required": ["question"]
            },
            "lifeos_search": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results (1-100)", "default": 10}
                },
                "required": ["query"]
            },
            "lifeos_calendar_upcoming": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Days to look ahead", "default": 7}
                }
            },
            "lifeos_calendar_search": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query"}
                },
                "required": ["q"]
            },
            "lifeos_gmail_search": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query"}
                },
                "required": ["q"]
            },
            "lifeos_drive_search": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query (name or content)"},
                    "account": {"type": "string", "description": "Account: personal or work", "default": "personal"}
                },
                "required": ["q"]
            },
            "lifeos_conversations_list": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results", "default": 10}
                }
            },
            "lifeos_memories_create": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Memory content"},
                    "category": {"type": "string", "description": "Category", "default": "facts"}
                },
                "required": ["content"]
            },
            "lifeos_memories_search": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            },
            "lifeos_people_search": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Name or email to search"}
                },
                "required": ["q"]
            },
            "lifeos_person_update": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Person entity ID from lifeos_people_search"},
                    "notes": {"type": "string", "description": "Free-text notes (replaces existing)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Classification tags (replaces existing)"},
                    "category": {"type": "string", "description": "Category: work, personal, family, or other"},
                    "birthday": {"type": "string", "description": "Birthday in MM-DD format, empty string to clear"}
                },
                "required": ["entity_id"]
            },
            "lifeos_health": {
                "type": "object",
                "properties": {}
            },
            "lifeos_imessage_search": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query for message text (case-insensitive)"},
                    "phone": {"type": "string", "description": "Filter by phone number (E.164 format, e.g., +15551234567)"},
                    "entity_id": {"type": "string", "description": "Filter by PersonEntity ID"},
                    "after": {"type": "string", "description": "Messages after date (YYYY-MM-DD)"},
                    "before": {"type": "string", "description": "Messages before date (YYYY-MM-DD)"},
                    "direction": {"type": "string", "description": "Filter by direction: 'sent' or 'received'"},
                    "max_results": {"type": "integer", "description": "Maximum results (1-200)", "default": 50}
                }
            },
            "lifeos_slack_search": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query for Slack messages (semantic search)"},
                    "top_k": {"type": "integer", "description": "Number of results to return (1-50)", "default": 20},
                    "channel_id": {"type": "string", "description": "Filter by specific channel ID"},
                    "user_id": {"type": "string", "description": "Filter by specific user ID"}
                },
                "required": ["query"]
            },
            "lifeos_person_facts": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The person's entity_id from lifeos_people_search"}
                },
                "required": ["person_id"]
            },
            "lifeos_person_profile": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The person's entity_id from lifeos_people_search"}
                },
                "required": ["person_id"]
            },
            "lifeos_person_timeline": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The person's entity_id from lifeos_people_search"},
                    "days_back": {"type": "integer", "description": "Days of history to include (default: 365)", "default": 365},
                    "source_type": {"type": "string", "description": "Filter by source type (e.g., 'imessage', 'gmail,slack')"},
                    "limit": {"type": "integer", "description": "Max results (default: 50)", "default": 50}
                },
                "required": ["person_id"]
            },
            "lifeos_meeting_prep": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format (defaults to today)"},
                    "include_all_day": {"type": "boolean", "description": "Include all-day events", "default": False},
                    "max_related_notes": {"type": "integer", "description": "Max related notes per meeting (1-10)", "default": 4}
                }
            },
            "lifeos_communication_gaps": {
                "type": "object",
                "properties": {
                    "person_ids": {"type": "string", "description": "Comma-separated person IDs to analyze"},
                    "days_back": {"type": "integer", "description": "Days of history to analyze (default: 365)", "default": 365},
                    "min_gap_days": {"type": "integer", "description": "Minimum gap to report in days (default: 14)", "default": 14}
                },
                "required": ["person_ids"]
            },
            "lifeos_person_connections": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The person's entity_id from lifeos_people_search"},
                    "relationship_type": {"type": "string", "description": "Filter by type (e.g., 'colleague', 'friend')"},
                    "limit": {"type": "integer", "description": "Max results (default: 50)", "default": 50}
                },
                "required": ["person_id"]
            },
            "lifeos_relationship_insights": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "Optional: Focus on specific person's insights"}
                }
            },
            "lifeos_reminder_create": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name for the reminder"},
                    "schedule_type": {"type": "string", "description": "'once' (ISO datetime) or 'cron' (cron expression)"},
                    "schedule_value": {"type": "string", "description": "ISO datetime (e.g., 2026-02-07T14:00:00) or cron expression (e.g., 30 7 * * 1-5)"},
                    "message_type": {"type": "string", "description": "'static' (send text as-is), 'prompt' (run through chat pipeline), or 'endpoint' (call API)"},
                    "message_content": {"type": "string", "description": "Static text or natural language prompt for Claude"},
                    "endpoint_config": {"type": "object", "description": "For endpoint type: {endpoint, method, params}"},
                    "enabled": {"type": "boolean", "description": "Whether the reminder is active", "default": True}
                },
                "required": ["name", "schedule_type", "schedule_value", "message_type"]
            },
            "lifeos_reminder_list": {
                "type": "object",
                "properties": {}
            },
            "lifeos_reminder_update": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string", "description": "Reminder ID from lifeos_reminder_list"},
                    "name": {"type": "string", "description": "Display name"},
                    "schedule_type": {"type": "string", "description": "'once' or 'cron'"},
                    "schedule_value": {"type": "string", "description": "ISO datetime or cron expression"},
                    "message_type": {"type": "string", "description": "'static', 'prompt', or 'endpoint'"},
                    "message_content": {"type": "string", "description": "Message text or prompt"},
                    "timezone": {"type": "string", "description": "IANA timezone (e.g., 'America/New_York')"},
                    "enabled": {"type": "boolean", "description": "Whether the reminder is active"}
                },
                "required": ["reminder_id"]
            },
            "lifeos_reminder_delete": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string", "description": "ID of the reminder to delete (from lifeos_reminder_list)"}
                },
                "required": ["reminder_id"]
            },
            "lifeos_schedule_create": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name for the schedule"},
                    "schedule_type": {"type": "string", "description": "'once' (ISO datetime) or 'cron' (cron expression)"},
                    "schedule_value": {"type": "string", "description": "ISO datetime (e.g., 2026-06-03T15:05:00) or cron expression (e.g., 0 9 * * 6)"},
                    "action": {"type": "string", "description": "'notify' (static text), 'prompt' (run chat pipeline), 'endpoint' (call API), or 'agent' (hand off to the agent worker)"},
                    "message_content": {"type": "string", "description": "Static text, natural-language prompt, or agent task description"},
                    "endpoint_config": {"type": "object", "description": "For action=endpoint: {endpoint, method, params}"},
                    "executor": {"type": "string", "description": "For action=agent: 'local', 'cloud', 'cloud-haiku', or 'cloud-sonnet'"},
                    "enabled": {"type": "boolean", "description": "Whether the schedule is active", "default": True}
                },
                "required": ["name", "schedule_type", "schedule_value", "action"]
            },
            "lifeos_schedule_list": {
                "type": "object",
                "properties": {}
            },
            "lifeos_schedule_update": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID from lifeos_schedule_list"},
                    "name": {"type": "string", "description": "Display name"},
                    "schedule_type": {"type": "string", "description": "'once' or 'cron'"},
                    "schedule_value": {"type": "string", "description": "ISO datetime or cron expression"},
                    "action": {"type": "string", "description": "'notify', 'prompt', 'endpoint', or 'agent'"},
                    "message_content": {"type": "string", "description": "Message text, prompt, or task description"},
                    "executor": {"type": "string", "description": "For action=agent: local | cloud | cloud-haiku | cloud-sonnet"},
                    "timezone": {"type": "string", "description": "IANA timezone (e.g., 'America/New_York')"},
                    "enabled": {"type": "boolean", "description": "Whether the schedule is active"}
                },
                "required": ["schedule_id"]
            },
            "lifeos_schedule_delete": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "ID of the schedule to delete (from lifeos_schedule_list)"}
                },
                "required": ["schedule_id"]
            },
            "lifeos_sync_trigger": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Sync source: vault, calendar, contacts, slack, photos, gmail, imessage, phone, facetime, linkedin"}
                },
                "required": ["source"]
            },
            "lifeos_person_fact_update": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Person entity ID from lifeos_people_search"},
                    "fact_id": {"type": "string", "description": "Fact ID from lifeos_person_facts"},
                    "value": {"type": "string", "description": "Updated fact value"},
                    "category": {"type": "string", "description": "Fact category (family, work, interests, dates, etc.)"}
                },
                "required": ["entity_id", "fact_id"]
            },
            "lifeos_person_fact_confirm": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Person entity ID from lifeos_people_search"},
                    "fact_id": {"type": "string", "description": "Fact ID from lifeos_person_facts"}
                },
                "required": ["entity_id", "fact_id"]
            },
            "lifeos_person_fact_delete": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Person entity ID from lifeos_people_search"},
                    "fact_id": {"type": "string", "description": "Fact ID from lifeos_person_facts"}
                },
                "required": ["entity_id", "fact_id"]
            },
            "lifeos_telegram_send": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message text to send via Telegram"}
                },
                "required": ["text"]
            },
            "lifeos_monarch_accounts": {
                "type": "object",
                "properties": {}
            },
            "lifeos_monarch_transactions": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD), defaults to 30 days ago"},
                    "end_date": {"type": "string", "description": "End date (YYYY-MM-DD), defaults to today"},
                    "category": {"type": "string", "description": "Filter by category name"},
                    "search": {"type": "string", "description": "Search by merchant name"},
                    "limit": {"type": "integer", "description": "Max results (default: 100)", "default": 100},
                    "account_id": {"type": "string", "description": "Filter by Monarch account ID"}
                }
            },
            "lifeos_monarch_cashflow": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD), defaults to first of current month"},
                    "end_date": {"type": "string", "description": "End date (YYYY-MM-DD), defaults to today"}
                }
            },
            "lifeos_monarch_budgets": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD), defaults to first of current month"},
                    "end_date": {"type": "string", "description": "End date (YYYY-MM-DD), defaults to today"}
                }
            },
            "lifeos_task_create": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Task description"},
                    "context": {"type": "string", "description": "Context/category (Work, Personal, Finance, etc.)", "default": "Inbox"},
                    "priority": {"type": "string", "description": "Priority: high, medium, low"},
                    "due_date": {"type": "string", "description": "Due date in YYYY-MM-DD format"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for classification"},
                    "reminder_id": {"type": "string", "description": "Linked reminder ID"},
                    "dry_run": {"type": "boolean", "description": "When true with an #agent tag, returns preflight routing + cost estimate without creating the task. Used for prompt-engineering iteration. Default: false."}
                },
                "required": ["description"]
            },
            "lifeos_task_list": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (todo, done, in_progress, cancelled, deferred, blocked, urgent)"},
                    "context": {"type": "string", "description": "Filter by context (Work, Personal, etc.)"},
                    "tag": {"type": "string", "description": "Filter by tag"},
                    "due_before": {"type": "string", "description": "Tasks due before date (YYYY-MM-DD)"},
                    "query": {"type": "string", "description": "Fuzzy text search across task descriptions"}
                }
            },
            "lifeos_task_update": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID from lifeos_task_list"},
                    "description": {"type": "string", "description": "New task description"},
                    "status": {"type": "string", "description": "New status (todo, done, in_progress, cancelled, deferred, blocked, urgent)"},
                    "context": {"type": "string", "description": "New context"},
                    "priority": {"type": "string", "description": "New priority (high, medium, low)"},
                    "due_date": {"type": "string", "description": "New due date (YYYY-MM-DD)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "New tags"}
                },
                "required": ["task_id"]
            },
            "lifeos_task_complete": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to mark as done"}
                },
                "required": ["task_id"]
            },
            "lifeos_task_delete": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to delete"}
                },
                "required": ["task_id"]
            },
            "lifeos_calendar_create": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "start_time": {"type": "string", "description": "Start time (ISO datetime, e.g. 2026-02-14T14:00:00-05:00)"},
                    "end_time": {"type": "string", "description": "End time (ISO datetime)"},
                    "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee email addresses"},
                    "description": {"type": "string", "description": "Event description"},
                    "location": {"type": "string", "description": "Event location"},
                    "account": {"type": "string", "description": "Account: personal or work", "default": "personal"}
                },
                "required": ["title", "start_time", "end_time"]
            },
            "lifeos_calendar_update": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID from lifeos_calendar_search"},
                    "title": {"type": "string", "description": "New title"},
                    "start_time": {"type": "string", "description": "New start time (ISO datetime)"},
                    "end_time": {"type": "string", "description": "New end time (ISO datetime)"},
                    "attendees": {"type": "array", "items": {"type": "string"}, "description": "New attendee emails (replaces existing)"},
                    "description": {"type": "string", "description": "New description"},
                    "location": {"type": "string", "description": "New location"},
                    "account": {"type": "string", "description": "Account: personal or work", "default": "personal"}
                },
                "required": ["event_id"]
            },
            "lifeos_calendar_delete": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID from lifeos_calendar_search"},
                    "account": {"type": "string", "description": "Account: personal or work", "default": "personal"}
                },
                "required": ["event_id"]
            }
        }

    def _build_tools_fallback(self):
        """Build tools from curated list without OpenAPI spec."""
        fallback_schemas = self._fallback_schemas()
        for config in CURATED_ENDPOINTS.values():
            tool = {
                "name": config["name"],
                "description": config["description"],
                "inputSchema": fallback_schemas.get(config["name"], {"type": "object", "properties": {}})
            }
            self.tools.append(tool)

    def _fetch_email_body(self, message_id: str, account: str = "personal") -> str | None:
        """Fetch full email body for a specific message."""
        try:
            url = f"{API_BASE}/api/gmail/message/{message_id}"
            resp = self.client.get(url, params={"account": account, "include_body": True})
            resp.raise_for_status()
            data = resp.json()
            return data.get("body")
        except Exception as e:
            logger.warning(f"Failed to fetch email body for {message_id}: {e}")
            return None

    def _handle_inter_agent(self, tool_name: str, arguments: dict) -> dict:
        """Dispatch a lifeos_agent_* call through the worker's session store.

        The remote caller passes `caller_session_id` explicitly (per the
        system prompt). We build a fresh InterAgentContext per call and run
        the tool — no shared mutable state.
        """
        caller_session_id = (arguments.pop("caller_session_id", None) or "").strip()
        if not caller_session_id:
            return {"error": "caller_session_id is required for inter-agent tools"}
        try:
            from api.services.agent_worker.inter_agent import (
                Caps,
                InterAgentContext,
                dispatch as inter_dispatch,
            )
            from api.services.agent_worker.session_store import SessionStore
            from api.services.agent_worker.transcript_store import TranscriptStore
            from config.settings import settings as _settings
        except Exception as e:
            return {"error": f"inter-agent stack unavailable: {e}"}

        # Wire a ManagedAgentsDriver into the context so `yield_until`
        # from a cloud caller can kill the remote session immediately
        # (otherwise Anthropic's session keeps generating post-yield).
        # Best-effort: if credentials aren't set, leave the driver None
        # and the handler logs a warning rather than failing.
        managed_driver = None
        try:
            if _settings.anthropic_api_key:
                from api.services.agent_worker.managed_driver import (
                    ManagedAgentsDriver,
                )
                managed_driver = ManagedAgentsDriver(
                    api_key=_settings.anthropic_api_key,
                )
        except Exception as exc:
            logger.warning("inter-agent: managed_driver init failed: %s", exc)

        # Anchor the stores to the repo's data/ dir (see AGENT_SESSIONS_DB) so
        # inter-agent tools hit the real worker store regardless of the cwd this
        # MCP server was spawned with.
        ctx = InterAgentContext(
            session_store=SessionStore(db_path=AGENT_SESSIONS_DB),
            transcript_store=TranscriptStore(transcripts_dir=AGENT_TRANSCRIPTS_DIR),
            caller_session_id=caller_session_id,
            caps=Caps(
                max_spawn_depth=_settings.agent_max_spawn_depth,
                max_descendants_per_root=_settings.agent_max_descendants_per_root,
                max_concurrent_local=_settings.agent_max_concurrent_local,
                max_concurrent_managed=_settings.agent_max_concurrent_managed,
            ),
            managed_driver=managed_driver,
        )
        return inter_dispatch(ctx, tool_name, arguments)

    def _handle_sync_trigger(self, arguments: dict) -> dict:
        """Route sync trigger to the correct endpoint based on source param."""
        source = arguments.get("source", "").lower().strip()
        if not source:
            return {"error": "Missing required parameter: source"}

        route_map = {
            "vault": "/api/admin/reindex",
            "calendar": "/api/admin/calendar/sync",
            "contacts": "/api/crm/contacts/sync",
            "slack": "/api/crm/slack/sync",
            "photos": "/api/photos/sync",
        }

        valid_sources = list(route_map.keys()) + [
            "gmail", "imessage", "phone", "facetime", "linkedin"
        ]

        if source in route_map:
            url = f"{API_BASE}{route_map[source]}"
        elif source in valid_sources:
            url = f"{API_BASE}/api/crm/sources/{source}/sync"
        else:
            return {"error": f"Invalid source: '{source}'. Valid sources: {', '.join(valid_sources)}"}

        try:
            resp = self.client.post(url)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"API error {e.response.status_code}: {e.response.text[:200]}"}
        except httpx.RequestError as e:
            return {"error": f"Request failed: {e}"}

    @staticmethod
    def _cache_eligible(tool_name: str) -> bool:
        """Tools whose results are safe to cache for 60s within a session.

        Only GET-shaped tools (read-only). Writes (drafts, calendar events,
        tasks, memories), inter-agent dispatch, and the sync trigger are
        excluded — they're either idempotent in the wrong direction or
        sensitive to fresh state.
        """
        cfg = next(
            (c for c in CURATED_ENDPOINTS.values() if c.get("name") == tool_name),
            None,
        )
        if cfg is None:
            return False
        return cfg.get("method", "").upper() == "GET"

    def _call_api(self, tool_name: str, arguments: dict, session_id: str | None = None) -> dict:
        """Call the LifeOS API based on tool name and arguments.

        `session_id` (optional) enables the per-session result cache (#139 §4):
        when supplied, identical GET-style tool calls within the same session
        are served from a 60s LRU instead of round-tripping to the API. Writes
        (POST/PUT/DELETE) are never cached. The cache also skips inter-agent
        tools and the sync trigger (both sensitive to fresh state).
        """
        # Custom handlers for tools that don't map 1:1 to endpoints
        if tool_name == "lifeos_sync_trigger":
            return self._handle_sync_trigger(arguments)

        # Inter-agent tools dispatch through the agent worker's session store
        # (no HTTP round-trip). The caller_session_id arg identifies which
        # session is acting — local executor injects it automatically; remote
        # managed agents pass it explicitly per the system prompt.
        if tool_name.startswith("lifeos_agent_"):
            return self._handle_inter_agent(tool_name, dict(arguments))

        # Cache check for read-only tools when the caller is session-aware.
        cache_key_args = arguments
        if self._cache_eligible(tool_name) and session_id and self._result_cache is not None:
            cached = self._result_cache.get(session_id, tool_name, cache_key_args)
            if cached is not None:
                return cached

        # Find the endpoint config
        endpoint_config = None
        endpoint_path = None
        for path, config in CURATED_ENDPOINTS.items():
            if config["name"] == tool_name:
                endpoint_config = config
                # Use explicit path if provided, otherwise strip method suffix
                endpoint_path = config.get("path", path.split(":")[0])
                break

        if not endpoint_config:
            return {"error": f"Unknown tool: {tool_name}"}

        method = endpoint_config["method"]
        url = f"{API_BASE}{endpoint_path}"

        # Handle path parameters
        if "{" in endpoint_path:
            import re
            path_params = re.findall(r"\{(\w+)\}", endpoint_path)
            for param in path_params:
                if param in arguments:
                    url = url.replace(f"{{{param}}}", str(arguments.pop(param)))

        try:
            if method == "GET":
                resp = self.client.get(url, params=arguments)
            elif method == "DELETE":
                resp = self.client.delete(url, params=arguments)
            elif method in ("PUT", "PATCH"):
                resp = self.client.request(method, url, json=arguments)
            else:  # POST
                resp = self.client.post(url, json=arguments)

            resp.raise_for_status()
            result = resp.json()

            # For gmail search, fetch bodies for top 5 results
            if tool_name == "lifeos_gmail_search":
                messages = result.get("messages", [])
                account = arguments.get("account", "personal")
                for msg in messages[:5]:
                    if msg.get("message_id"):
                        body = self._fetch_email_body(msg["message_id"], account)
                        if body:
                            msg["body"] = body

            # Store in the per-session cache for read-only tools (#139 §4).
            if self._cache_eligible(tool_name) and session_id and self._result_cache is not None:
                self._result_cache.put(session_id, tool_name, cache_key_args, result)

            return result
        except httpx.HTTPStatusError as e:
            return {"error": f"API error {e.response.status_code}: {e.response.text[:200]}"}
        except httpx.RequestError as e:
            return {"error": f"Request failed: {e}"}
        except Exception as e:
            return {"error": f"Unexpected error: {e}"}

    def _format_response(self, tool_name: str, data: dict) -> str:
        """Format API response for human readability."""
        if "error" in data:
            return f"Error: {data['error']}"

        # Tool-specific formatting
        if tool_name == "lifeos_ask":
            text = data.get("answer", "No answer returned")
            if sources := data.get("sources"):
                text += "\n\n**Sources:**\n"
                for s in sources[:5]:
                    text += f"- {s.get('file_name', 'Unknown')} (relevance: {s.get('relevance', 0):.2f})\n"
            return text

        elif tool_name == "lifeos_search":
            results = data.get("results", [])
            if not results:
                return "No results found."
            text = f"Found {len(results)} results:\n\n"
            for r in results[:10]:
                text += f"**{r.get('file_name', 'Unknown')}** (score: {r.get('score', 0):.2f})\n"
                content = r.get('content', '')[:150]
                text += f"{content}...\n\n"
            return text

        elif tool_name in ("lifeos_calendar_upcoming", "lifeos_calendar_search"):
            events = data.get("events", [])
            if not events:
                return "No events found."
            text = f"Found {len(events)} events:\n\n"
            for e in events[:10]:
                title = e.get("title") or e.get("summary") or "Untitled"
                text += f"- **{title}**\n"
                # Prefer the human-formatted strings when present; fall back
                # to raw ISO timestamps so consumers always get something.
                start = e.get("start_formatted") or e.get("start_time") or e.get("start") or "No time"
                end = e.get("end_formatted") or e.get("end_time") or e.get("end")
                if end and end != start:
                    text += f"  When: {start} – {end}\n"
                else:
                    text += f"  When: {start}\n"
                if location := e.get("location"):
                    text += f"  Where: {location}\n"
                if attendees := e.get("attendees"):
                    text += f"  With: {', '.join(attendees[:3])}\n"
            return text

        elif tool_name == "lifeos_calendar_create":
            title = data.get("title", "Untitled")
            start = data.get("start_time", "")
            end = data.get("end_time", "")
            text = f"Event created: **{title}**\nWhen: {start} – {end}"
            if attendees := data.get("attendees"):
                text += f"\nAttendees: {', '.join(attendees)}"
            text += f"\nAccount: {data.get('source_account', 'personal')}"
            return text

        elif tool_name == "lifeos_calendar_update":
            title = data.get("title", "Untitled")
            start = data.get("start_time", "")
            end = data.get("end_time", "")
            text = f"Event updated: **{title}**\nWhen: {start} – {end}"
            if attendees := data.get("attendees"):
                text += f"\nAttendees: {', '.join(attendees)}"
            return text

        elif tool_name == "lifeos_calendar_delete":
            return f"Event deleted (ID: {data.get('event_id', 'unknown')})"

        elif tool_name == "lifeos_gmail_search":
            emails = data.get("emails", data.get("messages", []))
            if not emails:
                return "No emails found."
            text = f"Found {len(emails)} emails:\n\n"
            for i, e in enumerate(emails[:10]):
                text += f"- **{e.get('subject', 'No subject')}**\n"
                # Show sender or recipient depending on what's available
                if sender := e.get('sender_name') or e.get('sender') or e.get('from'):
                    text += f"  From: {sender}\n"
                if to := e.get('to'):
                    text += f"  To: {to}\n"
                text += f"  Date: {e.get('date', 'Unknown')}\n"
                # Show body for first 5 emails if available
                if i < 5 and (body := e.get('body')):
                    # Truncate long bodies
                    body_preview = body[:2000] + "..." if len(body) > 2000 else body
                    text += f"  Body:\n{body_preview}\n"
                text += "\n"
            return text

        elif tool_name == "lifeos_drive_search":
            files = data.get("files", [])
            if not files:
                return "No files found."
            text = f"Found {len(files)} files:\n\n"
            for f in files[:10]:
                text += f"- **{f.get('name', 'Untitled')}**\n"
                text += f"  Type: {f.get('mime_type', 'Unknown')}\n"
                text += f"  Modified: {f.get('modified_time', 'Unknown')}\n"
                if f.get('web_link'):
                    text += f"  Link: {f.get('web_link')}\n"
                text += f"  Account: {f.get('source_account', 'Unknown')}\n\n"
            return text

        elif tool_name == "lifeos_conversations_list":
            convs = data.get("conversations", [])
            if not convs:
                return "No conversations found."
            text = f"Found {len(convs)} conversations:\n\n"
            for c in convs[:10]:
                text += f"- **{c.get('title', 'Untitled')}** (ID: {c.get('id', '')})\n"
            return text

        elif tool_name == "lifeos_memories_create":
            return f"Memory saved with ID: {data.get('id', 'unknown')}"

        elif tool_name == "lifeos_memories_search":
            memories = data.get("memories", [])
            if not memories:
                return "No memories found."
            text = f"Found {len(memories)} memories:\n\n"
            for m in memories[:10]:
                text += f"- {m.get('content', '')[:100]}...\n"
            return text

        elif tool_name == "lifeos_people_search":
            people = data.get("people", data.get("results", []))
            if not people:
                return "No people found."
            text = f"Found {len(people)} people:\n\n"
            for p in people[:10]:
                name = p.get("name", p.get("canonical_name", "Unknown"))
                text += f"- **{name}**"
                if email := p.get("email"):
                    text += f" ({email})"
                text += "\n"
                # Show relationship context for routing decisions
                strength = p.get("relationship_strength", 0)
                days = p.get("days_since_contact", 999)
                active = p.get("active_channels", [])
                entity_id = p.get("entity_id", "")
                text += f"  Strength: {strength:.0f}/100 | Last contact: {days} days ago\n"
                if active:
                    text += f"  Active channels: {', '.join(active)}\n"
                else:
                    text += "  Active channels: none recently\n"
                if entity_id:
                    text += f"  Entity ID: {entity_id}\n"
                text += "\n"
            return text

        elif tool_name == "lifeos_health":
            overall = data.get("overall_status", data.get("status", "unknown"))
            text = f"LifeOS Status: {overall}\n\nServices:\n"
            services = data.get("services", {})
            for svc_name, svc_info in services.items():
                if isinstance(svc_info, dict):
                    svc_status = svc_info.get("status", "unknown")
                    text += f"  {svc_name}: {svc_status}"
                    if svc_info.get("using_fallback"):
                        text += f" (fallback: {svc_info.get('fallback_name', 'yes')})"
                    if svc_info.get("last_error"):
                        text += f" - {svc_info['last_error']}"
                    text += "\n"
                else:
                    text += f"  {svc_name}: {svc_info}\n"
            deg_count = data.get("degradation_count_24h", 0)
            text += f"\nDegradation Events (24h): {deg_count}"
            critical = data.get("critical_issues", [])
            if critical:
                text += "\nCritical Issues:"
                for issue in critical:
                    text += f"\n  - {issue.get('service', 'unknown')}: {issue.get('error', '')}"
            else:
                text += "\nCritical Issues: none"
            return text

        elif tool_name == "lifeos_imessage_search":
            messages = data.get("messages", [])
            if not messages:
                return "No messages found."
            text = f"Found {len(messages)} messages:\n\n"
            for m in messages[:30]:
                direction = "→" if m.get("is_from_me") else "←"
                timestamp = m.get("timestamp", "")[:16].replace("T", " ")
                msg_text = m.get("text", "")
                # Truncate long messages
                if len(msg_text) > 150:
                    msg_text = msg_text[:150] + "..."
                msg_text = msg_text.replace("\n", " ").strip()
                text += f"- **{timestamp}** {direction} {msg_text}\n"
            return text

        elif tool_name == "lifeos_slack_search":
            results = data.get("results", [])
            if not results:
                return "No Slack messages found."
            text = f"Found {len(results)} Slack messages:\n\n"
            for r in results[:20]:
                channel = r.get("channel_name", "Unknown channel")
                user = r.get("user_name", "Unknown user")
                timestamp = r.get("timestamp", "")[:16].replace("T", " ")
                content = r.get("content", "")
                # Truncate long messages
                if len(content) > 200:
                    content = content[:200] + "..."
                content = content.replace("\n", " ").strip()
                text += f"- **{timestamp}** in {channel}\n"
                text += f"  {user}: {content}\n\n"
            return text

        elif tool_name == "lifeos_person_facts":
            facts = data.get("facts", [])
            if not facts:
                return "No facts extracted for this person yet."
            by_category = data.get("by_category", {})
            text = f"Found {len(facts)} facts:\n\n"
            for cat, cat_facts in by_category.items():
                text += f"**{cat.title()}:**\n"
                for f in cat_facts:
                    key = f.get("key", "")
                    value = f.get("value", "")
                    confidence = f.get("confidence", 0)
                    confirmed = "✓" if f.get("confirmed_by_user") else ""
                    text += f"  - {key}: {value} (conf: {confidence:.0%}) {confirmed}\n"
                text += "\n"
            return text

        elif tool_name == "lifeos_person_profile":
            name = data.get("display_name", data.get("canonical_name", "Unknown"))
            text = f"**{name}**\n\n"
            if emails := data.get("emails"):
                text += f"**Emails:** {', '.join(emails)}\n"
            if phones := data.get("phone_numbers"):
                text += f"**Phones:** {', '.join(phones)}\n"
            if company := data.get("company"):
                text += f"**Company:** {company}\n"
            if position := data.get("position"):
                text += f"**Position:** {position}\n"
            if linkedin := data.get("linkedin_url"):
                text += f"**LinkedIn:** {linkedin}\n"
            text += f"**Relationship Strength:** {data.get('relationship_strength', 0):.0f}/100\n"
            text += f"**Category:** {data.get('category', 'unknown')}\n"
            if sources := data.get("sources"):
                text += f"**Data Sources:** {', '.join(sources)}\n"
            if tags := data.get("tags"):
                text += f"**Tags:** {', '.join(tags)}\n"
            if notes := data.get("notes"):
                text += f"\n**Notes:**\n{notes}\n"
            # Interaction counts
            meeting_count = data.get("meeting_count", 0)
            email_count = data.get("email_count", 0)
            mention_count = data.get("mention_count", 0)
            if meeting_count or email_count or mention_count:
                text += f"\n**Interactions:** {meeting_count} meetings, {email_count} emails, {mention_count} mentions\n"
            return text

        elif tool_name == "lifeos_person_timeline":
            items = data.get("items", [])
            total = data.get("total_count", len(items))
            if not items:
                return "No interactions found for this person."
            text = f"Found {total} interactions:\n\n"
            for item in items[:30]:  # Limit display
                source = item.get("source_type", "unknown")
                timestamp = item.get("timestamp", "")[:16].replace("T", " ")
                summary = item.get("summary", "")[:200]
                # Use emoji for source type
                emoji = {
                    "gmail": "📧",
                    "imessage": "💬",
                    "whatsapp": "💬",
                    "calendar": "📅",
                    "slack": "💼",
                    "vault": "📝",
                    "granola": "📝",
                }.get(source, "•")
                text += f"{emoji} **{timestamp}** [{source}]\n"
                text += f"   {summary}\n\n"
            if total > 30:
                text += f"\n_... and {total - 30} more interactions_\n"
            return text

        elif tool_name == "lifeos_meeting_prep":
            meetings = data.get("meetings", [])
            date = data.get("date", "")
            if not meetings:
                return f"No meetings found for {date}."
            text = f"**Meeting Prep for {date}** ({len(meetings)} meetings)\n\n"
            for m in meetings:
                text += f"### {m.get('title', 'Untitled')}\n"
                text += f"**Time:** {m.get('start_time', '')} - {m.get('end_time', '')}\n"
                if attendees := m.get("attendees"):
                    text += f"**With:** {', '.join(attendees[:5])}"
                    if len(attendees) > 5:
                        text += f" (+{len(attendees) - 5} more)"
                    text += "\n"
                if location := m.get("location"):
                    text += f"**Location:** {location}\n"
                if description := m.get("description"):
                    text += f"**Description:** {description}\n"
                # Related notes
                if related := m.get("related_notes"):
                    text += "\n**Related Notes:**\n"
                    for note in related:
                        relevance = note.get("relevance", "")
                        title = note.get("title", "")
                        relevance_emoji = {
                            "attendee": "👤",
                            "past_meeting": "📅",
                            "topic": "📄",
                        }.get(relevance, "•")
                        text += f"  {relevance_emoji} {title}"
                        if note.get("date"):
                            text += f" ({note['date']})"
                        text += "\n"
                # Attachments
                if attachments := m.get("attachments"):
                    text += "\n**Attachments:**\n"
                    for att in attachments:
                        text += f"  📎 [{att.get('title', 'File')}]({att.get('url', '')})\n"
                text += "\n---\n\n"
            return text

        elif tool_name == "lifeos_communication_gaps":
            gaps = data.get("gaps", [])
            summaries = data.get("person_summaries", [])
            if not summaries:
                return "No communication data found for these people."
            text = "## Communication Gap Analysis\n\n"
            # Show person summaries first
            text += "### Overview\n"
            for s in summaries:
                name = s.get("person_name", "Unknown")
                days = s.get("days_since_last_contact", 999)
                avg = s.get("average_gap_days", 0)
                current = s.get("current_gap_days", 0)
                # Flag if current gap is significantly longer than average
                alert = "⚠️ " if current > avg * 1.5 and current > 14 else ""
                text += f"- **{name}**: {alert}{days} days since contact"
                if avg:
                    text += f" (avg gap: {avg:.0f} days)"
                text += "\n"
            # Show significant gaps
            if gaps:
                text += "\n### Significant Gaps\n"
                for g in gaps[:10]:
                    name = g.get("person_name", "Unknown")
                    gap_days = g.get("gap_days", 0)
                    start = g.get("gap_start", "")[:10]
                    end = g.get("gap_end", "")[:10]
                    text += f"- **{name}**: {gap_days} days ({start} to {end})\n"
            return text

        elif tool_name == "lifeos_person_connections":
            connections = data.get("connections", [])
            count = data.get("count", len(connections))
            if not connections:
                return "No connections found for this person."
            text = f"Found {count} connections:\n\n"
            for c in connections[:20]:
                name = c.get("name", "Unknown")
                company = c.get("company", "")
                rel_type = c.get("relationship_type", "")
                strength = c.get("relationship_strength", 0)
                # Calculate total shared interactions
                shared = (
                    c.get("shared_events_count", 0) +
                    c.get("shared_threads_count", 0) +
                    c.get("shared_messages_count", 0) +
                    c.get("shared_slack_count", 0) +
                    c.get("shared_whatsapp_count", 0)
                )
                text += f"- **{name}**"
                if company:
                    text += f" ({company})"
                text += "\n"
                text += f"  Shared interactions: {shared}"
                if rel_type:
                    text += f" | Type: {rel_type}"
                text += f" | Strength: {strength:.0f}/100\n"
                # Breakdown of shared items
                details = []
                if c.get("shared_events_count"):
                    details.append(f"{c['shared_events_count']} meetings")
                if c.get("shared_threads_count"):
                    details.append(f"{c['shared_threads_count']} email threads")
                if c.get("shared_messages_count"):
                    details.append(f"{c['shared_messages_count']} messages")
                if c.get("shared_slack_count"):
                    details.append(f"{c['shared_slack_count']} Slack msgs")
                if details:
                    text += f"  ({', '.join(details)})\n"
                if c.get("last_seen_together"):
                    text += f"  Last seen together: {c['last_seen_together'][:10]}\n"
                text += "\n"
            return text

        elif tool_name == "lifeos_relationship_insights":
            insights = data.get("insights", [])
            confirmed_count = data.get("confirmed_count", 0)
            unconfirmed_count = data.get("unconfirmed_count", 0)
            if not insights:
                return "No relationship insights found."
            text = f"## Relationship Insights ({confirmed_count} confirmed, {unconfirmed_count} unconfirmed)\n\n"
            # Group by category
            by_category = {}
            for i in insights:
                cat = i.get("category", "other")
                if cat not in by_category:
                    by_category[cat] = []
                by_category[cat].append(i)
            for cat, cat_insights in by_category.items():
                icon = cat_insights[0].get("category_icon", "")
                text += f"### {icon} {cat.replace('_', ' ').title()}\n"
                for i in cat_insights:
                    confirmed = "✓" if i.get("confirmed") else ""
                    text += f"- {i.get('text', '')} {confirmed}\n"
                    if i.get("source_title"):
                        text += f"  _Source: {i['source_title']}_\n"
                text += "\n"
            return text

        elif tool_name == "lifeos_monarch_accounts":
            accounts = data.get("accounts", [])
            if not accounts:
                return "No accounts found."
            text = f"Found {len(accounts)} accounts:\n\n"
            text += "| Account | Type | Balance | Institution | ID |\n"
            text += "|---------|------|---------|-------------|-----|\n"
            for a in accounts:
                bal = a.get("balance", 0)
                text += f"| {a.get('name', '')} | {a.get('type', '')} | ${bal:,.2f} | {a.get('institution', '')} | {a.get('id', '')} |\n"
            return text

        elif tool_name == "lifeos_monarch_transactions":
            txns = data.get("transactions", [])
            if not txns:
                return "No transactions found."
            text = f"Found {len(txns)} transactions ({data.get('start_date', '')} to {data.get('end_date', '')}):\n\n"
            text += "| Date | Merchant | Category | Amount | Account |\n"
            text += "|------|----------|----------|--------|---------|\n"
            for t in txns[:50]:
                amount = t.get("amount", 0)
                sign = "" if amount >= 0 else "-"
                text += f"| {t.get('date', '')} | {t.get('merchant', '')} | {t.get('category', '')} | {sign}${abs(amount):,.2f} | {t.get('account', '')} |\n"
            if len(txns) > 50:
                text += f"\n_... and {len(txns) - 50} more transactions_\n"
            return text

        elif tool_name == "lifeos_monarch_cashflow":
            income = data.get("total_income", 0)
            expenses = data.get("total_expenses", 0)
            savings = income - expenses
            rate = data.get("savings_rate", 0)
            text = f"**Cashflow Summary** ({data.get('start_date', '')} to {data.get('end_date', '')})\n\n"
            text += f"- **Income**: ${income:,.2f}\n"
            text += f"- **Expenses**: ${expenses:,.2f}\n"
            text += f"- **Net Savings**: ${savings:,.2f}\n"
            text += f"- **Savings Rate**: {rate * 100:.1f}%\n"
            categories = data.get("categories", [])
            if categories:
                text += "\n**Spending by Category:**\n"
                text += "| Category | Amount |\n"
                text += "|----------|--------|\n"
                for c in categories[:15]:
                    text += f"| {c.get('category', '')} | ${c.get('amount', 0):,.2f} |\n"
            return text

        elif tool_name == "lifeos_monarch_budgets":
            budgets = data.get("budgets", [])
            if not budgets:
                return "No budgets found."
            text = f"Found {len(budgets)} budgets ({data.get('start_date', '')} to {data.get('end_date', '')}):\n\n"
            text += "| Budget | Budgeted | Actual | Remaining |\n"
            text += "|--------|----------|--------|----------|\n"
            for b in budgets:
                text += f"| {b.get('category', '')} | ${b.get('budgeted', 0):,.2f} | ${b.get('actual', 0):,.2f} | ${b.get('remaining', 0):,.2f} |\n"
            return text

        elif tool_name == "lifeos_reminder_create":
            name = data.get("name", "")
            next_trigger = data.get("next_trigger_at", "not scheduled")
            return f"Reminder created: **{name}** (ID: {data.get('id', '')})\nNext trigger: {next_trigger}"

        elif tool_name == "lifeos_reminder_list":
            reminders = data.get("reminders", [])
            if not reminders:
                return "No reminders configured."
            text = f"Found {len(reminders)} reminders:\n\n"
            for r in reminders:
                status = "enabled" if r.get("enabled") else "disabled"
                emoji = "🔔" if r.get("enabled") else "🔕"
                text += f"{emoji} **{r.get('name', 'Untitled')}** ({status})\n"
                text += f"  Type: {r.get('message_type', '')} | Schedule: {r.get('schedule_type', '')} `{r.get('schedule_value', '')}`\n"
                if r.get("next_trigger_at"):
                    text += f"  Next: {r['next_trigger_at']}\n"
                if r.get("last_triggered_at"):
                    text += f"  Last: {r['last_triggered_at']}\n"
                text += f"  ID: {r.get('id', '')}\n\n"
            return text

        elif tool_name == "lifeos_reminder_update":
            name = data.get("name", "")
            next_trigger = data.get("next_trigger_at", "not scheduled")
            return f"Reminder updated: **{name}** (ID: {data.get('id', '')})\nNext trigger: {next_trigger}"

        elif tool_name == "lifeos_reminder_delete":
            return f"Reminder deleted: {data.get('id', 'unknown')}"

        elif tool_name in ("lifeos_schedule_create", "lifeos_schedule_update"):
            name = data.get("name", "")
            verb = "created" if tool_name.endswith("create") else "updated"
            action = data.get("action", "")
            executor = data.get("executor", "")
            label = f"{action} (#{executor})" if action == "agent" and executor else action
            next_trigger = data.get("next_trigger_at", "not scheduled")
            return (f"Schedule {verb}: **{name}** (ID: {data.get('id', '')})\n"
                    f"Action: {label} | Next: {next_trigger}")

        elif tool_name == "lifeos_schedule_delete":
            return f"Schedule deleted: {data.get('id', 'unknown')}"

        elif tool_name == "lifeos_schedule_list":
            schedules = data.get("schedules", [])
            if not schedules:
                return "No schedules configured."
            text = f"Found {len(schedules)} schedules:\n\n"
            for s in schedules:
                status = "enabled" if s.get("enabled") else "disabled"
                emoji = "🗓️" if s.get("enabled") else "🔕"
                action = s.get("action", "")
                executor = s.get("executor", "")
                label = f"{action} (#{executor})" if action == "agent" and executor else action
                text += f"{emoji} **{s.get('name', 'Untitled')}** ({status})\n"
                text += f"  Action: {label} | Schedule: {s.get('schedule_type', '')} `{s.get('schedule_value', '')}`\n"
                if s.get("next_trigger_at"):
                    text += f"  Next: {s['next_trigger_at']}\n"
                if s.get("last_status"):
                    text += f"  Last outcome: {s['last_status']}\n"
                text += f"  ID: {s.get('id', '')}\n\n"
            return text

        elif tool_name == "lifeos_sync_trigger":
            return f"Sync triggered: {data.get('message', data.get('status', 'started'))}"

        elif tool_name == "lifeos_person_update":
            name = data.get("display_name", data.get("canonical_name", "Unknown"))
            text = f"Person updated: **{name}** (ID: {data.get('entity_id', '')})"
            if data.get("notes"):
                text += f"\nNotes: {data['notes'][:100]}"
            if data.get("tags"):
                text += f"\nTags: {', '.join(data['tags'])}"
            if data.get("category"):
                text += f"\nCategory: {data['category']}"
            return text

        elif tool_name == "lifeos_person_fact_update":
            return f"Fact updated (ID: {data.get('id', data.get('fact_id', 'unknown'))})"

        elif tool_name == "lifeos_person_fact_confirm":
            return f"Fact confirmed (ID: {data.get('id', data.get('fact_id', 'unknown'))})"

        elif tool_name == "lifeos_person_fact_delete":
            return f"Fact deleted (ID: {data.get('id', data.get('fact_id', 'unknown'))})"

        elif tool_name == "lifeos_telegram_send":
            return "Message sent to Telegram."

        elif tool_name == "lifeos_task_create":
            return f"Task created: **{data.get('description', '')}** (ID: {data.get('id', '')})\nContext: {data.get('context', 'Inbox')} | File: {data.get('source_file', '')}"

        elif tool_name == "lifeos_task_list":
            tasks = data.get("tasks", [])
            if not tasks:
                return "No tasks found."
            text = f"Found {data.get('total', len(tasks))} tasks:\n\n"
            for t in tasks:
                symbol = {"todo": "[ ]", "done": "[x]", "in_progress": "[/]", "cancelled": "[-]", "deferred": "[>]", "blocked": "[?]", "urgent": "[!]"}.get(t.get("status", "todo"), "[ ]")
                text += f"- {symbol} **{t.get('description', '')}**"
                if t.get("due_date"):
                    text += f" (due: {t['due_date']})"
                if t.get("priority"):
                    text += f" [{t['priority']}]"
                text += f"\n  Context: {t.get('context', 'Inbox')}"
                if t.get("tags"):
                    text += f" | Tags: {', '.join('#' + tag for tag in t['tags'])}"
                text += f" | ID: {t.get('id', '')}\n"
            return text

        elif tool_name == "lifeos_task_update":
            return f"Task updated: **{data.get('description', '')}** (ID: {data.get('id', '')})\nStatus: {data.get('status', '')} | Context: {data.get('context', '')}"

        elif tool_name == "lifeos_task_complete":
            return f"Task completed: **{data.get('description', '')}** (ID: {data.get('id', '')})"

        elif tool_name == "lifeos_task_delete":
            return f"Task deleted (ID: {data.get('id', data.get('task_id', 'unknown'))})"

        # Default: return formatted JSON
        return json.dumps(data, indent=2)


def _ok(request_id, result: dict) -> dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id, message: str, code: int = -32000) -> dict:
    """Build a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(server: "LifeOSMCPServer", request: dict) -> dict | None:
    """Handle a single JSON-RPC request.

    Returns the response dict, or None for notifications (requests with no id).
    Used by both the stdio and HTTP transports so behavior stays identical.
    """
    method = request.get("method")
    request_id = request.get("id")
    is_notification = request_id is None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lifeos", "version": "1.0.0"},
            }
            return None if is_notification else _ok(request_id, result)

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            return None if is_notification else _ok(request_id, {"tools": server.tools})

        if method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            data = server._call_api(tool_name, arguments)
            formatted = server._format_response(tool_name, data)
            result = {"content": [{"type": "text", "text": formatted}]}
            return None if is_notification else _ok(request_id, result)

        # Unknown method
        if is_notification:
            return None
        return _err(request_id, f"Method not found: {method}", code=-32601)

    except Exception as e:
        logger.error(f"Error handling {method}: {e}")
        if is_notification:
            return None
        return _err(request_id, str(e))


def run_stdio(server: "LifeOSMCPServer") -> None:
    """Run the stdio transport (line-delimited JSON-RPC on stdin/stdout)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            continue
        response = dispatch(server, request)
        if response is not None:
            print(json.dumps(response), flush=True)


def build_http_app(server: "LifeOSMCPServer", bearer_token: str):
    """Build a FastAPI app exposing the MCP server over HTTP.

    A non-empty `bearer_token` is required — the HTTP transport is intended for
    public exposure (e.g., behind a Cloudflare Tunnel for Anthropic Managed
    Agents), so unauthenticated mode is not allowed. Local Claude Code keeps
    using the stdio transport, which has no bearer check.
    """
    if not bearer_token:
        raise ValueError(
            "HTTP transport requires a bearer token "
            "(set LIFEOS_MCP_BEARER_TOKEN in the environment)"
        )

    import hmac

    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse

    app = FastAPI(title="LifeOS MCP", docs_url=None, redoc_url=None, openapi_url=None)

    def _check_auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not hmac.compare_digest(token, bearer_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    def _parse_error_response() -> JSONResponse:
        """JSON-RPC -32700 Parse error per the spec — id is null when unparseable."""
        envelope = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }
        return JSONResponse(envelope, status_code=400)

    async def _handle(request: Request) -> Response:
        _check_auth(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _parse_error_response()

        if isinstance(body, list):
            responses = [r for r in (dispatch(server, req) for req in body) if r is not None]
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses)

        response = dispatch(server, body)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        return await _handle(request)

    @app.post("/")
    async def mcp_root(request: Request) -> Response:
        return await _handle(request)

    return app


def run_http(server: "LifeOSMCPServer", host: str, port: int, bearer_token: str) -> None:
    """Run the HTTP transport via uvicorn."""
    import uvicorn  # local import — only needed for HTTP mode

    app = build_http_app(server, bearer_token=bearer_token)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    """Entrypoint: parse --transport flag and run the chosen transport."""
    import argparse

    parser = argparse.ArgumentParser(description="LifeOS MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("LIFEOS_MCP_TRANSPORT", "stdio"),
        help="Transport to expose (default: stdio, suitable for Claude Code)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LIFEOS_MCP_HTTP_HOST", "127.0.0.1"),
        help="HTTP bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LIFEOS_MCP_HTTP_PORT", "8765")),
        help="HTTP bind port (default: 8765)",
    )
    args = parser.parse_args()

    server = LifeOSMCPServer()

    if args.transport == "stdio":
        run_stdio(server)
        return

    bearer_token = os.environ.get("LIFEOS_MCP_BEARER_TOKEN", "")
    if not bearer_token:
        logger.error(
            "HTTP transport requires LIFEOS_MCP_BEARER_TOKEN. "
            "Generate one with: openssl rand -hex 32"
        )
        sys.exit(2)

    logger.info(f"LifeOS MCP HTTP transport listening on {args.host}:{args.port}")
    run_http(server, host=args.host, port=args.port, bearer_token=bearer_token)


if __name__ == "__main__":
    main()
