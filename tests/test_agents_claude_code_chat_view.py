"""Tests for the /chat thread-view reconstruction of routing='code' sessions."""
from __future__ import annotations

import pytest

from api.routes.agents import _reconstruct_code_conversation, _reconstruct_conversation


pytestmark = pytest.mark.unit


def _ev(kind: str, **payload):
    return {"kind": kind, "payload": payload}


def test_dispatcher_picks_code_branch_when_events_have_code_prefix():
    events = [
        _ev("code_init", code_session_id="cli-1"),
        _ev("code_user_prompt", text="hi", resume=False),
        _ev("code_notify", body="hello back!", body_chars=11),
    ]
    turns = _reconstruct_conversation(messages=[], events=events)
    assert turns == [
        {"role": "user", "text": "hi", "tools": []},
        {"role": "assistant", "text": "hello back!", "tools": []},
    ]


def test_tool_use_attaches_to_current_assistant_turn():
    events = [
        _ev("code_user_prompt", text="edit foo.py", resume=False),
        _ev("code_tool_use", name="Read", input={"file_path": "foo.py"}),
        _ev("code_notify", body="Read it.", body_chars=8),
        _ev("code_tool_use", name="Edit", input={"file_path": "foo.py"}),
        _ev("code_notify", body="Made the edit.", body_chars=14),
    ]
    turns = _reconstruct_code_conversation(events)
    assert len(turns) == 2
    assert turns[0] == {"role": "user", "text": "edit foo.py", "tools": []}
    assert turns[1]["role"] == "assistant"
    assert "Read it." in turns[1]["text"]
    assert "Made the edit." in turns[1]["text"]
    assert [t["name"] for t in turns[1]["tools"]] == ["Read", "Edit"]


def test_resume_starts_new_user_turn():
    events = [
        _ev("code_user_prompt", text="first ask", resume=False),
        _ev("code_notify", body="first answer", body_chars=12),
        _ev("code_user_prompt", text="follow up", resume=True),
        _ev("code_notify", body="second answer", body_chars=13),
    ]
    turns = _reconstruct_code_conversation(events)
    roles = [t["role"] for t in turns]
    texts = [t["text"] for t in turns]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert texts == ["first ask", "first answer", "follow up", "second answer"]


def test_clarify_without_body_still_renders_a_marker():
    events = [
        _ev("code_user_prompt", text="ambiguous task", resume=False),
        _ev("code_clarify", body_chars=20),  # legacy event missing 'body'
    ]
    turns = _reconstruct_code_conversation(events)
    assert turns[-1]["role"] == "assistant"
    assert "clarification" in turns[-1]["text"]
