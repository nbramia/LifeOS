"""Per-class tool filter helper (#139 §3, partial).

The full per-class session tool filtering flow is:
  1. Preflight picks `preset_class` (personal-comm / work-comm / research /
     financial / crm / fullstack)
  2. Worker creates the session as usual
  3. Worker calls `driver.update_session()` with the filtered tool list
  4. Filtered tools scope the cache_creation cost on the first user turn

This module owns step 3's input: given a class name, return the right
`{tools: [...], mcp_servers: [...]}` payload for the UPDATE call. The
worker-side wiring (steps 2 + 3) is a follow-up; this helper lands now
so the rest can compose against a tested, documented surface.

Cross-cutting tools are auto-merged into every class's filter — every
agent needs at minimum: telegram_send, agent_* coordination, search,
ask, health, task_*, reminder_*, and calendar reads.
"""
from __future__ import annotations


# Preset classes. Preflight picks one; "fullstack" means "use the entire
# preset, no filtering" — the operator's default fallback.
PRESET_CLASS_PERSONAL_COMM = "personal-comm"
PRESET_CLASS_WORK_COMM = "work-comm"
PRESET_CLASS_RESEARCH = "research"
PRESET_CLASS_FINANCIAL = "financial"
PRESET_CLASS_CRM = "crm"
PRESET_CLASS_FULLSTACK = "fullstack"

ALL_PRESET_CLASSES = (
    PRESET_CLASS_PERSONAL_COMM,
    PRESET_CLASS_WORK_COMM,
    PRESET_CLASS_RESEARCH,
    PRESET_CLASS_FINANCIAL,
    PRESET_CLASS_CRM,
    PRESET_CLASS_FULLSTACK,
)


# Cross-cutting tools every agent needs regardless of routing class.
# Without these, a research agent can't message back to the operator,
# a financial agent can't spawn a research child, etc.
CROSS_CUTTING_LIFEOS_TOOLS = (
    # Operator messaging
    "lifeos_telegram_send",
    # Inter-agent coordination
    "lifeos_agent_spawn",
    "lifeos_agent_check",
    "lifeos_agent_send",
    "lifeos_agent_yield_until",
    "lifeos_agent_kill",
    "lifeos_agent_transcript_read",
    "lifeos_agent_sessions_list",
    "lifeos_agent_user_ask",
    # General vault search — often the first move on any task
    "lifeos_search",
    "lifeos_ask",
    # Self-diagnosis
    "lifeos_health",
    # Follow-up creation
    "lifeos_task_create",
    "lifeos_task_list",
    "lifeos_task_update",
    "lifeos_task_complete",
    "lifeos_reminder_create",
    "lifeos_reminder_list",
    # Calendar reads are common across most classes; writes stay class-specific.
    "lifeos_calendar_upcoming",
    "lifeos_calendar_search",
)


# Per-class specialty tools. Cross-cutting tools are merged in by
# `class_to_tool_filter` so each class's tuple only lists what's unique.
_CLASS_SPECIALTIES: dict[str, tuple[str, ...]] = {
    PRESET_CLASS_PERSONAL_COMM: (
        "lifeos_gmail_search",
        "lifeos_gmail_draft",
        "lifeos_calendar_create",
        "lifeos_calendar_update",
        "lifeos_calendar_delete",
        "lifeos_imessage_search",
    ),
    PRESET_CLASS_WORK_COMM: (
        "lifeos_slack_search",
        "lifeos_gmail_search",
        "lifeos_gmail_draft",
        "lifeos_calendar_create",
        "lifeos_calendar_update",
        "lifeos_calendar_delete",
        "lifeos_drive_search",
    ),
    PRESET_CLASS_RESEARCH: (
        "lifeos_drive_search",
        "lifeos_memories_create",
        "lifeos_memories_search",
        "lifeos_people_search",
    ),
    PRESET_CLASS_FINANCIAL: (
        "lifeos_monarch_accounts",
        "lifeos_monarch_transactions",
        "lifeos_monarch_cashflow",
        "lifeos_monarch_budgets",
    ),
    PRESET_CLASS_CRM: (
        "lifeos_people_search",
        "lifeos_person_profile",
        "lifeos_person_timeline",
        "lifeos_person_connections",
        "lifeos_person_facts",
        "lifeos_person_fact_update",
        "lifeos_person_fact_confirm",
        "lifeos_person_fact_delete",
        "lifeos_person_update",
        "lifeos_relationship_insights",
        "lifeos_communication_gaps",
        "lifeos_meeting_prep",
        "lifeos_photos_person",
        "lifeos_photos_shared",
        "lifeos_photos_stats",
        "lifeos_memories_search",
    ),
    # `fullstack` is the no-filter fallback — handled by class_to_tool_filter
    # by returning None instead of a payload.
    PRESET_CLASS_FULLSTACK: (),
}


def class_to_tool_filter(preset_class: str) -> dict | None:
    """Build the `agent.tools` payload for `driver.update_session()`.

    Returns:
      - A `{"tools": [...]}` dict when filtering applies. The list is the
        union of the class's specialty tools and `CROSS_CUTTING_LIFEOS_TOOLS`,
        de-duplicated and sorted for stability across calls.
      - `None` for the `fullstack` class or any unknown class — meaning
        "no filter, use the agent preset as-is". The worker should skip
        the UPDATE call in this case.

    The `mcp_servers` key is intentionally NOT emitted here. The MCP server
    list lives on the preset (Anthropic console); filtering individual
    tools within those servers is the per-class lever. If a class needs
    a different set of MCP servers entirely, that's a separate operator
    decision and should be made by swapping the preset, not the filter.
    """
    if preset_class not in _CLASS_SPECIALTIES or preset_class == PRESET_CLASS_FULLSTACK:
        return None
    specialty = _CLASS_SPECIALTIES[preset_class]
    merged = sorted(set(specialty) | set(CROSS_CUTTING_LIFEOS_TOOLS))
    return {"tools": merged}
