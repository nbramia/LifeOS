"""Tests for #787: when a chat turn's model call exhausts its retries, the
user-facing message must be fixed and generic — never the underlying
exception's own text. On a keyless or misconfigured install that text would
otherwise be a provider SDK's raw internal message, surfacing at exactly the
moment a person most needs a plain signal instead of a cryptic string. The
full exception is still logged server-side; only what reaches the user
changes. Covers the three call sites named in the issue: the tool-round
loop's fatal branch (no prior text and, separately, an already-produced
partial answer), and the final synthesis round's error branch.
"""
import pytest
from unittest.mock import patch

from api.services import agent_loop

pytestmark = pytest.mark.unit

_SENSITIVE = "sk-ant-totally-real-secret-should-never-reach-the-user"


class _RaisingClient:
    """An astream that raises a non-retryable error before yielding anything
    -- is_retryable_api_error(RuntimeError(...)) is False, so this fails the
    turn on the first attempt, no retries."""

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, timeout=None, temperature=None,
                       enable_thinking=None, reasoning_effort=None):
        if False:  # pragma: no cover -- keeps this an async generator
            yield {}
        raise RuntimeError(_SENSITIVE)


@pytest.mark.asyncio
async def test_round_loop_fatal_error_no_prior_text_is_generic():
    """Call site 1 (agent_loop.py, tool-round loop, `result.full_text` falsy):
    a fresh turn whose very first model call fails gets a fixed message, not
    str(exc)."""
    with patch.object(agent_loop, "_select_client", return_value=_RaisingClient()):
        events = [e async for e in agent_loop.run_agent_loop("find X")]

    text_events = [e["content"] for e in events if e["type"] == "text"]
    assert text_events, "expected a user-facing error message"
    joined = "".join(text_events)
    assert _SENSITIVE not in joined
    assert joined == "Sorry, I encountered an error and could not complete this request."


@pytest.mark.asyncio
async def test_round_loop_fatal_error_with_prior_text_is_generic(monkeypatch):
    """Call site 1's other branch (`result.full_text` truthy -- an answer
    already in progress when the fatal error hits): still no str(exc)."""

    class _PreSeededResult(agent_loop.AgentResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.full_text = "Here's what I found so far: "

    monkeypatch.setattr(agent_loop, "AgentResult", _PreSeededResult)

    with patch.object(agent_loop, "_select_client", return_value=_RaisingClient()):
        events = [e async for e in agent_loop.run_agent_loop("find X")]

    text_events = [e["content"] for e in events if e["type"] == "text"]
    joined = "".join(text_events)
    assert _SENSITIVE not in joined
    assert "\n\n(Search interrupted: the request could not be completed.)" in joined


@pytest.mark.asyncio
async def test_synthesis_round_error_is_generic():
    """Call site 2 (agent_loop.py, final synthesis round after tool rounds
    are exhausted -- max_tool_rounds=0 forces straight into the `for...else`
    synthesis branch): still no str(exc)."""
    with patch.object(agent_loop, "_select_client", return_value=_RaisingClient()):
        events = [
            e async for e in agent_loop.run_agent_loop("find X", max_tool_rounds=0)
        ]

    text_events = [e["content"] for e in events if e["type"] == "text"]
    joined = "".join(text_events)
    assert _SENSITIVE not in joined
    assert "\n\n(Error during synthesis: the request could not be completed.)" in joined


@pytest.mark.asyncio
async def test_round_loop_fatal_error_still_logs_full_exception(capsys):
    """The full exception detail must still reach the server-side log --
    only the user-facing yield changes."""
    with patch.object(agent_loop, "_select_client", return_value=_RaisingClient()):
        _ = [e async for e in agent_loop.run_agent_loop("find X")]

    captured = capsys.readouterr()
    assert _SENSITIVE in captured.out


@pytest.mark.asyncio
async def test_synthesis_round_still_logs_full_exception(capsys):
    """Same logging guarantee for the synthesis round's error branch."""
    with patch.object(agent_loop, "_select_client", return_value=_RaisingClient()):
        _ = [
            e async for e in agent_loop.run_agent_loop("find X", max_tool_rounds=0)
        ]

    captured = capsys.readouterr()
    assert _SENSITIVE in captured.out
