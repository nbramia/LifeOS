"""Tests for the per-class tool filter helper (#139 §3, partial)."""
from __future__ import annotations

import pytest

from api.services.agent_worker.tool_filter import (
    ALL_PRESET_CLASSES,
    CROSS_CUTTING_LIFEOS_TOOLS,
    PRESET_CLASS_CRM,
    PRESET_CLASS_FINANCIAL,
    PRESET_CLASS_FULLSTACK,
    PRESET_CLASS_PERSONAL_COMM,
    PRESET_CLASS_RESEARCH,
    PRESET_CLASS_WORK_COMM,
    class_to_tool_filter,
)


pytestmark = pytest.mark.unit


def test_fullstack_class_returns_none():
    """fullstack means 'no filter' — worker should skip the UPDATE call."""
    assert class_to_tool_filter(PRESET_CLASS_FULLSTACK) is None


def test_unknown_class_returns_none():
    """Defensive: a typo or future class returns None instead of an empty
    filter (which would lock the agent out of all tools)."""
    assert class_to_tool_filter("not-a-real-class") is None


@pytest.mark.parametrize("preset_class", [
    PRESET_CLASS_PERSONAL_COMM,
    PRESET_CLASS_WORK_COMM,
    PRESET_CLASS_RESEARCH,
    PRESET_CLASS_FINANCIAL,
    PRESET_CLASS_CRM,
])
def test_every_class_includes_all_cross_cutting_tools(preset_class):
    """Cross-cutting tools (telegram, agent_*, search, ask, etc.) must
    appear in every non-fullstack class's filter so the agent can always
    message back, spawn children, and search."""
    payload = class_to_tool_filter(preset_class)
    assert payload is not None
    tools = set(payload["tools"])
    missing = set(CROSS_CUTTING_LIFEOS_TOOLS) - tools
    assert not missing, f"{preset_class} missing cross-cutting tools: {missing}"


def test_personal_comm_class_has_gmail_and_imessage():
    """personal-comm specializes in personal messaging."""
    tools = set(class_to_tool_filter(PRESET_CLASS_PERSONAL_COMM)["tools"])
    assert "lifeos_gmail_search" in tools
    assert "lifeos_gmail_draft" in tools
    assert "lifeos_imessage_search" in tools
    # And it gets calendar writes (comms classes need to schedule).
    assert "lifeos_calendar_create" in tools


def test_work_comm_class_has_slack_and_drive():
    """work-comm specializes in workplace systems."""
    tools = set(class_to_tool_filter(PRESET_CLASS_WORK_COMM)["tools"])
    assert "lifeos_slack_search" in tools
    assert "lifeos_drive_search" in tools


def test_research_class_excludes_calendar_writes():
    """research is read-only — no calendar create/update/delete.
    Calendar READS (upcoming, search) are cross-cutting and ARE included."""
    tools = set(class_to_tool_filter(PRESET_CLASS_RESEARCH)["tools"])
    assert "lifeos_calendar_upcoming" in tools  # cross-cutting read
    assert "lifeos_calendar_create" not in tools  # write — excluded
    assert "lifeos_calendar_delete" not in tools


def test_financial_class_has_monarch_tools():
    """financial specializes in money tools."""
    tools = set(class_to_tool_filter(PRESET_CLASS_FINANCIAL)["tools"])
    assert "lifeos_monarch_accounts" in tools
    assert "lifeos_monarch_transactions" in tools
    assert "lifeos_monarch_cashflow" in tools
    assert "lifeos_monarch_budgets" in tools


def test_crm_class_has_full_person_family():
    """crm specializes in the people/person/photos tools."""
    tools = set(class_to_tool_filter(PRESET_CLASS_CRM)["tools"])
    assert "lifeos_people_search" in tools
    assert "lifeos_person_profile" in tools
    assert "lifeos_person_facts" in tools
    assert "lifeos_photos_person" in tools
    assert "lifeos_meeting_prep" in tools
    assert "lifeos_communication_gaps" in tools


def test_filter_payload_does_not_include_mcp_servers_key():
    """MCP server list lives on the preset, not in per-session filtering.
    Emitting an mcp_servers key here would risk overriding the preset's
    list with an empty array."""
    payload = class_to_tool_filter(PRESET_CLASS_RESEARCH)
    assert payload is not None
    assert "mcp_servers" not in payload
    assert set(payload.keys()) == {"tools"}


def test_filter_payload_is_deduplicated_and_sorted():
    """Output order is stable across calls so transcript diffs are clean."""
    payload = class_to_tool_filter(PRESET_CLASS_PERSONAL_COMM)
    assert payload is not None
    tools = payload["tools"]
    assert tools == sorted(set(tools)), "tool list must be deduped + sorted"
    # And calling twice yields identical output.
    assert class_to_tool_filter(PRESET_CLASS_PERSONAL_COMM) == payload


def test_all_classes_constant_matches_handled_classes():
    """The ALL_PRESET_CLASSES tuple matches what class_to_tool_filter
    actually handles — drift between the two would silently break the
    operator's view of available classes."""
    assert PRESET_CLASS_FULLSTACK in ALL_PRESET_CLASSES
    for cls in ALL_PRESET_CLASSES:
        # Either None (fullstack) or a real payload — never raises.
        result = class_to_tool_filter(cls)
        assert result is None or "tools" in result
