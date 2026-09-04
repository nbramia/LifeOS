"""Tests for `api/services/agent_worker/assignment.py` (#851): the
task-fields extractor and the per-engine effort mapping."""
from __future__ import annotations

import pytest

from api.services.agent_worker.assignment import (
    ENGINE_CLAUDE_CODE,
    ENGINE_CODEX,
    ENGINE_HERMES,
    ENGINE_LOCAL,
    extract_assignment,
    local_thinking_for_effort,
    map_effort_for_engine,
)


pytestmark = pytest.mark.unit


def test_extract_assignment_pulls_all_four_fields():
    a = extract_assignment({
        "model": "opus", "effort": "high", "host": "studio", "assigned_by": "board",
    })
    assert a.model == "opus"
    assert a.effort == "high"
    assert a.host == "studio"
    assert a.assigned_by == "board"
    assert a.is_board_assigned is True


def test_extract_assignment_defaults_on_missing_fields():
    a = extract_assignment(None)
    assert a.model is None and a.effort is None and a.host is None
    assert a.assigned_by is None
    assert a.is_board_assigned is False


def test_extract_assignment_ignores_unrecognized_effort():
    a = extract_assignment({"effort": "extreme"})
    assert a.effort is None


def test_extract_assignment_strips_whitespace_to_none():
    a = extract_assignment({"model": "  ", "host": ""})
    assert a.model is None
    assert a.host is None


@pytest.mark.parametrize("effort,expected", [
    ("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "max"),
    (None, None), ("bogus", None),
])
def test_claude_code_effort_map(effort, expected):
    assert map_effort_for_engine(ENGINE_CLAUDE_CODE, effort) == expected


@pytest.mark.parametrize("effort,expected", [
    ("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "xhigh"),
    (None, None),
])
def test_codex_effort_map(effort, expected):
    assert map_effort_for_engine(ENGINE_CODEX, effort) == expected


def test_engines_without_effort_flags_return_none():
    assert map_effort_for_engine(ENGINE_LOCAL, "high") is None
    assert map_effort_for_engine(ENGINE_HERMES, "high") is None


@pytest.mark.parametrize("effort,expected", [
    ("high", True), ("max", True), ("low", False), ("medium", False),
    (None, None), ("", None), ("bogus", None),
])
def test_local_thinking_for_effort(effort, expected):
    assert local_thinking_for_effort(effort) is expected
