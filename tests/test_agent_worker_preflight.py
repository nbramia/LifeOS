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
        "routing_explicit": False,
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
    reply = _golden_reply(routing="claude", routing_reason="title says 'use claude opus'",
                          routing_explicit=True)
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
def test_preflight_prompt_says_method_questions_are_not_ambiguity():
    """Repro of the live bug: preflight was flagging "summarize Julia's
    background" as ambiguous because the agent could use either local
    docs or web search. The prompt now tells the classifier that
    method-of-execution choices are NEVER ambiguity — the agent picks
    one and adapts. Without this guidance Haiku over-blocks."""
    prompt = pf.build_preflight_prompt(title="anything", tags=["agent"])
    lowered = prompt.lower()
    assert "method-of-execution" in lowered or "method of execution" in lowered
    # The classifier must be told these aren't ambiguity (NOT / NEVER).
    assert "not ambiguity" in lowered or "never ambiguity" in lowered
    # And the prefer-null guidance must be in there too
    assert "leave null" in lowered or "prefer null" in lowered


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


# ---------------------------------------------------------------------------
# Tag precedence (#139 §2)
# ---------------------------------------------------------------------------

def _stub_caller(routing="claude", routing_explicit=False):
    """Build a fake caller returning a minimal preflight JSON.

    `routing_explicit` mirrors the classifier's own flag: true only when the
    operator named the engine themselves. Cloud routes without it are
    downgraded to `ask` (#584), so tests that want a real cloud dispatch either
    set it (with a title that names the engine) or use a `#cloud*` tag.
    """
    import json
    def call(_prompt):
        return json.dumps({
            "budget": {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 0.50},
            "routing": routing,
            "routing_reason": "stub",
            "routing_explicit": routing_explicit,
            "expected_output": "text",
            "ambiguity": None,
            "sane": True,
            "sane_reason": "",
        })
    return call


@pytest.mark.unit
def test_cloud_haiku_tag_forces_haiku_routing():
    """`#cloud-haiku` always picks Haiku, regardless of preflight's choice."""
    result = pf.run_preflight("ambiguous task", tags=["agent", "cloud-haiku"],
                              caller=_stub_caller(routing="local"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.model == pf.MODEL_HAIKU
    assert "cloud-haiku" in result.routing_reason


@pytest.mark.unit
def test_cloud_sonnet_tag_forces_sonnet_routing():
    """`#cloud-sonnet` always picks Sonnet."""
    result = pf.run_preflight("anything", tags=["agent", "cloud-sonnet"],
                              caller=_stub_caller(routing="local"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.model == pf.MODEL_SONNET
    assert "cloud-sonnet" in result.routing_reason


@pytest.mark.unit
def test_local_tag_overrides_to_local_model():
    """`#local` forces local regardless of preflight's choice."""
    result = pf.run_preflight("task", tags=["agent", "local"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.model == pf.MODEL_LOCAL


@pytest.mark.unit
def test_cloud_tag_keeps_preflight_routing_but_defaults_model_to_sonnet():
    """`#cloud` says cloud but leaves the model open — defaults to Sonnet
    when preflight didn't pick a specific model."""
    result = pf.run_preflight("anything", tags=["agent", "cloud"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.model == pf.MODEL_SONNET


@pytest.mark.unit
def test_untagged_cloud_route_is_downgraded_to_ask():
    """An inferred cloud route never dispatches on its own (#584).

    Replaces the former "untagged cloud → Sonnet default" case: without a
    `#cloud*` tag or an operator who named the engine, a `claude` route from
    the classifier is a guess, and guessing costs API credits. It becomes
    `ask`, and the model is left unset for the answer to decide.
    """
    result = pf.run_preflight("draft an email", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_ASK
    assert result.routing_explicit is False
    assert result.model is None
    assert "not explicitly requested" in result.routing_reason


@pytest.mark.unit
def test_untagged_local_route_sets_model_to_local():
    """Local routing without override gets MODEL_LOCAL."""
    result = pf.run_preflight("quick lookup", tags=["agent"],
                              caller=_stub_caller(routing="local"))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.model == pf.MODEL_LOCAL


@pytest.mark.unit
def test_tag_precedence_haiku_wins_over_sonnet_tag():
    """If both #cloud-haiku and #cloud-sonnet are present, the first match
    in the precedence ladder wins (haiku checked before sonnet)."""
    result = pf.run_preflight("anything",
                              tags=["agent", "cloud-haiku", "cloud-sonnet"],
                              caller=_stub_caller(routing="claude"))
    # Per the ladder docstring: cloud-haiku is checked before cloud-sonnet.
    assert result.model == pf.MODEL_HAIKU


@pytest.mark.unit
def test_tag_precedence_accepts_hash_prefix_form():
    """Operators sometimes write the leading `#`; the parser must accept it."""
    result = pf.run_preflight("anything", tags=["#agent", "#cloud-haiku"],
                              caller=_stub_caller(routing="claude"))
    assert result.model == pf.MODEL_HAIKU


@pytest.mark.unit
def test_claude_tag_routes_to_claude_code_cli():
    """`#claude` forces routing=claude_code (Claude Code CLI, subscription-billed)."""
    result = pf.run_preflight("clean up the logs",
                              tags=["agent", "claude"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert "#claude tag present" in result.routing_reason
    # No per-token cost gating for CLI routes — billed via subscription.
    assert result.estimated_cost_dollars == 0.0
    assert result.needs_cost_confirmation is False


@pytest.mark.unit
def test_codex_tag_routes_to_codex_cli():
    """`#codex` forces routing=codex (Codex CLI, subscription-billed)."""
    result = pf.run_preflight("rename a class",
                              tags=["agent", "codex"],
                              caller=_stub_caller(routing="local"))
    assert result.routing == pf.ROUTE_CODEX
    assert "#codex tag present" in result.routing_reason
    assert result.estimated_cost_dollars == 0.0
    assert result.needs_cost_confirmation is False


@pytest.mark.unit
def test_local_tag_beats_claude_tag():
    """`#local` is checked first; combining with `#claude` keeps the task
    on Gemma. Documents the precedence ladder."""
    result = pf.run_preflight("anything",
                              tags=["agent", "claude", "local"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_claude_tag_beats_codex_tag():
    """`#claude` is checked before `#codex`; if both are present, Claude wins.
    Documents the precedence ladder."""
    result = pf.run_preflight("anything",
                              tags=["agent", "codex", "claude"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE_CODE


# ---------------------------------------------------------------------------
# Preset class tag detection (#139 §3 wiring)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_preset_class_tag_sets_preset_class():
    """An explicit class tag (#research / #crm / etc.) sets preset_class."""
    result = pf.run_preflight("dig into Q4 numbers", tags=["agent", "research"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "research"


@pytest.mark.unit
def test_each_preset_class_tag_recognized():
    """All six class tags map to the right preset_class string."""
    for tag in ("personal-comm", "work-comm", "research", "financial", "crm", "fullstack"):
        result = pf.run_preflight("any task", tags=["agent", tag],
                                  caller=_stub_caller(routing="claude"))
        assert result.preset_class == tag, f"tag #{tag} should set preset_class={tag}"


@pytest.mark.unit
def test_preset_class_accepts_hash_prefix_form():
    """Operators may write `#research` or `research` — both map to the class."""
    result = pf.run_preflight("any task", tags=["#agent", "#crm"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_first_preset_class_tag_wins_on_conflict():
    """If two class tags slip in, the first one in the tag list wins."""
    result = pf.run_preflight("any task", tags=["agent", "crm", "research"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_no_class_tag_leaves_preset_class_none():
    """Without an explicit class tag, preset_class stays None — the worker
    defaults to fullstack (no filter)."""
    result = pf.run_preflight("ambiguous task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class is None


@pytest.mark.unit
def test_preset_class_set_on_empty_title_short_circuit():
    """The empty-title short-circuit path also honors a class tag."""
    result = pf.run_preflight("", tags=["agent", "financial"])
    assert result.preset_class == "financial"
    # And the sane flag is still false (empty title is unsafe regardless of tags).
    assert result.sane is False


# ---------------------------------------------------------------------------
# Cost gates: fail-fast budget check (#139 §6) + cost preview (#139 §7)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cloud_route_emits_cost_estimate():
    """Cloud-routed tasks get a non-zero cache-cold cost estimate so the
    orchestrator can preview cost before dispatch."""
    result = pf.run_preflight("research task", tags=["agent", "research", "cloud"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.estimated_cost_dollars > 0


@pytest.mark.unit
def test_local_route_emits_zero_estimate():
    """Local routing is free compute (operator paid for the hardware)."""
    result = pf.run_preflight("anything", tags=["agent", "local"],
                              caller=_stub_caller(routing="local"))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.estimated_cost_dollars == 0.0
    assert result.needs_cost_confirmation is False


@pytest.mark.unit
def test_fullstack_estimate_higher_than_research_estimate():
    """Larger preset classes are estimated more expensively — that's the
    whole point of per-class filtering."""
    full = pf.run_preflight("any task", tags=["agent", "fullstack", "cloud"],
                            caller=_stub_caller(routing="claude"))
    research = pf.run_preflight("any task", tags=["agent", "research", "cloud"],
                                caller=_stub_caller(routing="claude"))
    assert full.estimated_cost_dollars > research.estimated_cost_dollars


@pytest.mark.unit
def test_fail_fast_refuses_when_estimate_exceeds_2x_max_dollars():
    """§6 acceptance: refuse dispatch when cache-cold estimate exceeds
    2× max_dollars (refuse only when even the cheap path can't fit)."""
    # fullstack on Sonnet = 100k × $3/M × 1.25 ≈ $0.375. 2× = $0.75.
    # Set max_dollars below that floor (so 2× margin still doesn't fit).
    import json
    def stub_caller(_p):
        return json.dumps({
            "budget": {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 0.10},
            "routing": "claude",
            "routing_reason": "stub",
            "expected_output": "text",
            "ambiguity": None,
            "sane": True,
            "sane_reason": "",
        })
    result = pf.run_preflight("expensive task", tags=["agent", "fullstack", "cloud"],
                              caller=stub_caller)
    assert result.sane is False
    assert "budget_too_small" in result.sane_reason


@pytest.mark.unit
def test_fail_fast_does_not_refuse_when_2x_margin_fits():
    """Cache-warm tasks aren't over-refused: when 2× max_dollars covers
    the estimate, dispatch is allowed (real cost may be 10× cheaper from
    cache_read on a warm cache)."""
    import json
    def stub_caller(_p):
        return json.dumps({
            "budget": {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 5.0},
            "routing": "claude",
            "routing_reason": "stub",
            "expected_output": "text",
            "ambiguity": None,
            "sane": True,
            "sane_reason": "",
        })
    result = pf.run_preflight("normal task", tags=["agent", "research"],
                              caller=stub_caller)
    assert result.sane is True
    assert "budget_too_small" not in result.sane_reason


@pytest.mark.unit
def test_cost_confirmation_triggers_above_threshold(monkeypatch):
    """§7 acceptance: estimate > threshold sets needs_cost_confirmation."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_cost_confirm_threshold_dollars", 0.01)
    result = pf.run_preflight("any task", tags=["agent", "fullstack", "cloud"],
                              caller=_stub_caller(routing="claude"))
    # fullstack estimate is well over a penny.
    assert result.needs_cost_confirmation is True


@pytest.mark.unit
def test_cost_confirmation_not_triggered_below_threshold(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_cost_confirm_threshold_dollars", 1000.0)
    result = pf.run_preflight("any task", tags=["agent", "fullstack"],
                              caller=_stub_caller(routing="claude"))
    assert result.needs_cost_confirmation is False


@pytest.mark.unit
def test_cost_confirmation_disabled_when_threshold_zero(monkeypatch):
    """Threshold=0 disables confirmation entirely (auto-dispatch all)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_cost_confirm_threshold_dollars", 0.0)
    result = pf.run_preflight("any task", tags=["agent", "fullstack"],
                              caller=_stub_caller(routing="claude"))
    assert result.needs_cost_confirmation is False


# ---------------------------------------------------------------------------
# API-spend gate (#584): an inferred cloud route never dispatches on its own
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("title", [
    "search my gmail for the invoice",   # rule 4 capability inference
    "check my calendar for tomorrow",
    "summarize my slack threads",
])
def test_capability_inference_never_reaches_the_api_by_itself(title):
    """Rule 4 is the classifier guessing that a task needs cloud connectors.
    A guess may not spend API credits — it asks instead."""
    result = pf.run_preflight(title, tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_classifier_explicit_flag_alone_is_not_enough():
    """`routing_explicit` is corroborated, not trusted.

    The flag comes from an LLM, so it can be wrong. Unless the title actually
    names an engine or model, a `true` is treated as the guess it probably is —
    the direction where a mistake costs a question rather than credits.
    """
    result = pf.run_preflight("draft an email to the team", tags=["agent"],
                              caller=_stub_caller(routing="claude", routing_explicit=True))
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
@pytest.mark.parametrize("title", [
    "use claude to draft the email",
    "summarize this with opus",
    "run it on the cloud model",
])
def test_operator_naming_the_engine_dispatches_without_a_question(title):
    """The other half of the rule: an operator who asked for it gets it.
    Same principle as the `#cloud` tag — explicit intent is consent."""
    result = pf.run_preflight(title, tags=["agent"],
                              caller=_stub_caller(routing="claude", routing_explicit=True))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.routing_explicit is True


@pytest.mark.unit
@pytest.mark.parametrize("tag,expected_model", [
    ("cloud", pf.MODEL_SONNET),
    ("cloud-haiku", pf.MODEL_HAIKU),
    ("cloud-sonnet", pf.MODEL_SONNET),
])
def test_cloud_tags_are_consent_and_still_dispatch(tag, expected_model):
    """`#cloud*` tasks were explicitly tagged by the operator, so they keep
    dispatching straight to the API — the gate is about inference only."""
    result = pf.run_preflight("any task", tags=["agent", tag],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.routing_explicit is True
    assert result.model == expected_model


@pytest.mark.unit
def test_subscription_routes_are_unaffected_by_the_gate():
    """The gate targets per-token API spend. The CLI routes bill a flat
    subscription, so they dispatch without a question, as before."""
    for tag, route in (("claude", pf.ROUTE_CLAUDE_CODE), ("codex", pf.ROUTE_CODEX),
                       ("local", pf.ROUTE_LOCAL)):
        result = pf.run_preflight("do the thing", tags=["agent", tag],
                                  caller=_stub_caller(routing="claude"))
        assert result.routing == route, tag


# ---------------------------------------------------------------------------
# #704 — `_default_llm_caller` client-selection fallback order
# ---------------------------------------------------------------------------

class _FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text


@pytest.mark.unit
def test_default_llm_caller_uses_anthropic_when_key_set_no_probe(monkeypatch):
    """Order 1: an Anthropic key selects AnthropicLLMClient with
    agent_preflight_model, exactly as before #704 — and never touches
    LocalLLMClient.is_available (no reachability probe on this branch)."""
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)

    captured = {}

    def fake_init(self, api_key=None, model=None):
        captured["model"] = model
        captured["api_key"] = api_key

    def fake_create(self, messages, *, system=None, max_tokens=4096, tools=None, temperature=None):
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        captured["temperature"] = temperature
        return _FakeLLMResponse("anthropic reply")

    monkeypatch.setattr(AnthropicLLMClient, "__init__", fake_init)
    monkeypatch.setattr(AnthropicLLMClient, "create", fake_create)

    def _forbidden_probe(self):
        raise AssertionError("Anthropic branch must not probe the local llama-server")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    result = pf._default_llm_caller("some prompt")

    assert result == "anthropic reply"
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["messages"] == [{"role": "user", "content": "some prompt"}]
    assert captured["max_tokens"] == 1024
    assert captured["temperature"] == 0.0


@pytest.mark.unit
def test_default_llm_caller_falls_back_to_local_when_reachable(monkeypatch):
    """Order 2: no Anthropic key, local llama-server reachable → runs on it."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    captured = {}
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: True)

    def fake_create(self, messages, *, system=None, max_tokens=4096, tools=None, temperature=None):
        captured["base_url"] = self.base_url
        captured["model"] = self.model
        return _FakeLLMResponse("local reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "local reply"
    assert captured["model"] == "local"
    assert captured["base_url"] == LocalLLMClient().base_url


@pytest.mark.unit
def test_default_llm_caller_falls_back_to_remote_when_local_unreachable(monkeypatch):
    """Order 3: no Anthropic key, local unreachable, #699 remote provider
    configured + enabled → runs on the remote OpenAI-compatible provider."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", True, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/deepseek-v4-flash-0731", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "fw_test_key", raising=False)
    monkeypatch.setattr(settings, "remote_llm_timeout", 42, raising=False)

    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: False)

    captured = {}

    def fake_create(self, messages, *, system=None, max_tokens=4096, tools=None, temperature=None):
        captured["base_url"] = self.base_url
        captured["model"] = self.model
        captured["timeout"] = self.timeout
        captured["auth"] = self._auth_headers()
        return _FakeLLMResponse("remote reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "remote reply"
    assert captured["base_url"] == "https://remote.example/v1"
    assert captured["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert captured["timeout"] == 42
    assert captured["auth"] == {"Authorization": "Bearer fw_test_key"}


@pytest.mark.unit
def test_default_llm_caller_raises_when_no_client_usable(monkeypatch):
    """Order 4: no key, no reachable local server, no remote provider ⇒
    raise. `run_preflight`'s existing except-clause degrades this to
    sane=False/routing=ask, unchanged by #704."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: False)

    with pytest.raises(RuntimeError):
        pf._default_llm_caller("some prompt")

    # And the run_preflight integration point: the raise degrades to the
    # existing safe path rather than propagating.
    result = pf.run_preflight("do the thing", tags=["agent"], caller=None)
    assert result.sane is False
    assert result.routing == pf.ROUTE_ASK
