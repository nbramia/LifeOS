"""
Tests for the email send gate in the agentic chat orchestrator.

The core invariant: an email draft created during the current turn can NEVER be
sent in that same turn. Sending is only allowed for drafts created in a prior
turn — enforcing "draft → confirm → send" structurally, not just via the prompt.
"""
from unittest.mock import patch, MagicMock

import pytest

from api.services.agent_tools import (
    begin_email_send_turn,
    _tool_create_email_draft,
    _tool_send_email_draft,
)
from api.services.gmail import DraftMessage

pytestmark = pytest.mark.unit


def _mock_gmail(draft_id="d1", message_id="sent-1"):
    mock = MagicMock()
    mock.create_draft.return_value = DraftMessage(
        draft_id=draft_id,
        message_id="m1",
        subject="test",
        to="recipient@example.com",
        body="test body",
        source_account="personal",
    )
    mock.send_draft.return_value = message_id
    return mock


async def test_cannot_send_draft_created_this_turn():
    """A draft created in the current turn must be refused by the send gate."""
    begin_email_send_turn()
    mock = _mock_gmail(draft_id="d1")

    with patch("api.services.gmail.GmailService", return_value=mock):
        create_result = await _tool_create_email_draft(
            {"to": "recipient@example.com", "subject": "test", "body": "test body"}
        )
        assert "d1" in create_result  # draft_id surfaced for later sending

        send_result = await _tool_send_email_draft({"draft_id": "d1"})

    assert send_result.startswith("Error")
    assert "current turn" in send_result
    mock.send_draft.assert_not_called()


async def test_in_process_gate_refuses_same_turn_before_send_call():
    """The agent-loop gate stays in-process and refuses before Gmail send."""
    begin_email_send_turn()
    mock = _mock_gmail(draft_id="d1")

    with patch("api.services.gmail.GmailService", return_value=mock):
        await _tool_create_email_draft(
            {"to": "recipient@example.com", "subject": "test", "body": "test body"}
        )
        result = await _tool_send_email_draft({"draft_id": "d1"})

    assert result.startswith("Error")
    assert "current turn" in result
    mock.send_draft.assert_not_called()


async def test_can_send_draft_from_prior_turn():
    """A draft NOT created in the current turn (i.e. from a prior turn) can be sent."""
    begin_email_send_turn()  # fresh turn — no drafts created within it
    mock = _mock_gmail(message_id="sent-1")

    with patch("api.services.gmail.GmailService", return_value=mock):
        result = await _tool_send_email_draft({"draft_id": "old-draft-from-last-turn"})

    assert "sent" in result.lower()
    mock.send_draft.assert_called_once_with("old-draft-from-last-turn")


async def test_new_turn_does_not_inherit_prior_turn_drafts():
    """begin_email_send_turn() resets the per-turn set, so a draft from the
    previous turn becomes sendable in the next turn."""
    # Turn 1: create a draft.
    begin_email_send_turn()
    mock = _mock_gmail(draft_id="d1", message_id="sent-1")
    with patch("api.services.gmail.GmailService", return_value=mock):
        await _tool_create_email_draft(
            {"to": "recipient@example.com", "subject": "test", "body": "test body"}
        )
        # Same turn: cannot send.
        blocked = await _tool_send_email_draft({"draft_id": "d1"})
    assert blocked.startswith("Error")

    # Turn 2: user confirmed; the draft is now sendable.
    begin_email_send_turn()
    with patch("api.services.gmail.GmailService", return_value=mock):
        sent = await _tool_send_email_draft({"draft_id": "d1"})
    assert "sent" in sent.lower()
    mock.send_draft.assert_called_once_with("d1")


async def test_send_requires_draft_id():
    """Sending without a draft_id is an error and never touches Gmail."""
    begin_email_send_turn()
    result = await _tool_send_email_draft({})
    assert result.startswith("Error")
    assert "draft_id" in result


async def test_gate_holds_across_gather_child_tasks():
    """Mirrors the real agent loop: the per-turn set is bound in the parent task
    and tools run in asyncio.gather child tasks across rounds. The child tasks
    inherit (a copy of) the parent context referencing the SAME set object, so a
    draft created in one child task is visible to the send in a later child task.
    This guards the ContextVar-sharing assumption the gate relies on."""
    import asyncio

    begin_email_send_turn()  # bound in this (parent) task
    mock = _mock_gmail(draft_id="d1")

    with patch("api.services.gmail.GmailService", return_value=mock):
        # Round 1: create runs in a gather child task.
        (create_result,) = await asyncio.gather(
            _tool_create_email_draft(
                {"to": "recipient@example.com", "subject": "test", "body": "test body"}
            )
        )
        assert "d1" in create_result

        # Round 2: send runs in a separate gather child task and must still see
        # that d1 was created this turn → blocked.
        (send_result,) = await asyncio.gather(_tool_send_email_draft({"draft_id": "d1"}))

    assert send_result.startswith("Error")
    assert "current turn" in send_result
    mock.send_draft.assert_not_called()
