"""Preflight unit tests — parsing, defaults, error handling.

These exercise the parser and the public `run_preflight` entry point against
canned LLM replies. The real Haiku call is mocked via the `caller` parameter.
"""
from __future__ import annotations

import json

import pytest

from api.services.agent_worker import preflight as pf


def _stub(reply: str):
    """Build a caller that always returns `reply`."""
    return lambda prompt: reply


def _golden_reply(**overrides) -> str:
    base = {
        "budget": {"wall_seconds": 14400, "max_tokens": 500000, "max_dollars": 5.0},
        "routing": "local",
        "routing_reason": "#local tag present",
        "expected_output": "text",
        "ambiguity": None,
        "sane": True,
        "sane_reason": "",
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.unit
def test_run_preflight_returns_parsed_result():
    result = pf.run_preflight(title="echo the date", tags=["agent", "local"], caller=_stub(_golden_reply()))
    assert result.sane is True
    assert result.routing == pf.ROUTE_LOCAL
    assert result.budget.max_dollars == pytest.approx(5.0)
    assert result.expected_output == "text"
    assert result.ambiguity is None


@pytest.mark.unit
def test_preflight_routing_claude():
    reply = _golden_reply(routing="claude", routing_reason="title says 'use claude opus'")
    result = pf.run_preflight(title="use claude opus to summarize", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_CLAUDE


@pytest.mark.unit
def test_preflight_routing_ask_when_unspecified():
    reply = _golden_reply(routing="ask", routing_reason="no tag and no title cue")
    result = pf.run_preflight(title="research dolphins", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_ambiguity_populated():
    reply = _golden_reply(
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    result = pf.run_preflight(title="reply to John", tags=["agent", "local"], caller=_stub(reply))
    assert result.ambiguity is not None
    assert "John" in result.ambiguity.question


@pytest.mark.unit
def test_preflight_sanity_failure_passed_through():
    reply = _golden_reply(sane=False, sane_reason="destructive: 'rm -rf /'")
    result = pf.run_preflight(title="rm -rf /", tags=["agent"], caller=_stub(reply))
    assert result.sane is False
    assert "destructive" in result.sane_reason


@pytest.mark.unit
def test_preflight_handles_json_in_code_fence():
    """Some models like to wrap output in ```json fences."""
    inner = _golden_reply()
    reply = f"Here you go:\n```json\n{inner}\n```\n"
    result = pf.run_preflight(title="something", tags=["agent", "local"], caller=_stub(reply))
    assert result.sane is True
    assert result.routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_preflight_unparseable_reply_defaults_to_unsafe_ask():
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub("totally not json"))
    assert result.sane is False
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_caller_exception_defaults_to_unsafe_ask():
    def boom(prompt):
        raise RuntimeError("haiku is down")

    result = pf.run_preflight(title="x", tags=["agent"], caller=boom)
    assert result.sane is False
    assert "haiku is down" in result.sane_reason
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_empty_title_short_circuits_without_llm_call():
    """An empty title is always unsafe — don't bother spending a Haiku call."""
    calls = []

    def fail(prompt):
        calls.append(prompt)
        raise AssertionError("LLM should not have been called for empty title")

    result = pf.run_preflight(title="   ", tags=["agent"], caller=fail)
    assert result.sane is False
    assert calls == []


@pytest.mark.unit
def test_preflight_invalid_routing_value_falls_back_to_ask():
    reply = _golden_reply(routing="invalid-value")
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_invalid_expected_output_falls_back_to_text():
    reply = _golden_reply(expected_output="hologram")
    result = pf.run_preflight(title="x", tags=["agent", "local"], caller=_stub(reply))
    assert result.expected_output == "text"


@pytest.mark.unit
def test_preflight_budget_partial_uses_defaults():
    """Missing budget fields fall back to settings defaults — not zero."""
    reply = json.dumps({"budget": {"wall_seconds": 60}, "routing": "local",
                        "routing_reason": "x", "expected_output": "text",
                        "ambiguity": None, "sane": True, "sane_reason": ""})
    result = pf.run_preflight(title="x", tags=["agent", "local"], caller=_stub(reply))
    assert result.budget.wall_seconds == 60
    # max_tokens / max_dollars come from defaults — strictly positive
    assert result.budget.max_tokens > 0
    assert result.budget.max_dollars > 0
