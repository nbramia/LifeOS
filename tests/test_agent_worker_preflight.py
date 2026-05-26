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


# ---------------------------------------------------------------------------
# Prompt-content tests — verify routing rules visible to Haiku (issue #119)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_preflight_prompt_recognizes_cloud_tag_as_mirror_of_local():
    """`#cloud` tag should route to claude — symmetric with `#local` → local.
    The rule lives in the prompt text Haiku consumes."""
    prompt = pf.build_preflight_prompt(title="anything", tags=["agent", "cloud"])
    # Strong assertions: the literal rule text Haiku reads.
    assert 'tag list contains "cloud"' in prompt
    assert '#cloud tag present' in prompt
    # Both directions documented in the rules
    assert '"local"' in prompt
    assert '"cloud"' in prompt


@pytest.mark.unit
def test_preflight_prompt_infers_claude_from_capability_phrases():
    """Capability-implying phrases (gmail/calendar/drive/slack/etc.) should
    cue claude routing without an explicit 'use claude' phrase. Live testing
    today saw 3 tasks go to `ask` because of strict literal matching; this
    rule fixes that."""
    prompt = pf.build_preflight_prompt(title="search my gmail for ...", tags=["agent"])
    # The capability-inference rule must mention each major surface
    for keyword in ("gmail", "calendar", "drive", "slack"):
        assert keyword in prompt.lower(), f"missing capability inference for: {keyword}"
    # And explicitly state the routing decision
    assert "claude" in prompt.lower()


@pytest.mark.unit
def test_preflight_prompt_includes_ordered_precedence():
    """Routing precedence must be clearly ordered (tags first, explicit cues
    second, capability inference third, ask as final fallback)."""
    prompt = pf.build_preflight_prompt(title="x", tags=["agent"])
    assert "precedence" in prompt.lower() or "apply in order" in prompt.lower() or "first match wins" in prompt.lower()
    # All four ladder steps present
    assert "#local" in prompt or "tag list contains \"local\"" in prompt
    assert "use claude" in prompt
    assert "capability" in prompt.lower() or "infer from capability" in prompt.lower()
    assert "ask" in prompt.lower()
