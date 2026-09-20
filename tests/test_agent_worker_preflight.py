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


# ---------------------------------------------------------------------------
# A sanity rejection must park, not cancel, unless the code itself can
# confirm the title is empty, deterministically destructive, or preflight
# itself failed. `sane_fatal` is the signal the worker uses to distinguish.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_sane_false_on_mundane_title_is_not_fatal():
    """The model may reject a routine UI task as
    'not executable' even though the title itself is ordinary — the
    classifier's own inferred opinion must not be treated as fatal."""
    reply = _golden_reply(
        sane=False,
        sane_reason="This is a product specification or feature request, not a task an agent can execute.",
    )
    result = pf.run_preflight(
        title="Display the user's transcribed message immediately after sending",
        tags=["agent"], caller=_stub(reply),
    )
    assert result.sane is False
    assert result.sane_fatal is False


@pytest.mark.unit
def test_sane_false_destructive_title_is_fatal():
    """The model's own sane=false on an actually-destructive title is
    corroborated by the deterministic check — stays fatal."""
    reply = _golden_reply(sane=False, sane_reason="destructive: 'rm -rf /'")
    result = pf.run_preflight(title="rm -rf /", tags=["agent"], caller=_stub(reply))
    assert result.sane is False
    assert result.sane_fatal is True


@pytest.mark.unit
def test_destructive_title_is_fatal_even_if_model_claims_sane():
    """The destructive-shape guard must not be weakened by (or dependent on)
    model compliance: even a model that wrongly says sane=true on an
    obviously destructive title gets overridden by the deterministic check."""
    reply = _golden_reply(sane=True, sane_reason="")
    result = pf.run_preflight(title="delete all my data", tags=["agent"], caller=_stub(reply))
    assert result.sane is False
    assert result.sane_fatal is True


@pytest.mark.unit
def test_empty_title_sanity_failure_is_fatal():
    def fail(prompt):
        raise AssertionError("LLM should not have been called for empty title")

    result = pf.run_preflight(title="   ", tags=["agent"], caller=fail)
    assert result.sane is False
    assert result.sane_fatal is True


@pytest.mark.unit
def test_preflight_llm_error_sanity_is_not_fatal():
    def boom(prompt):
        raise RuntimeError("haiku is down")

    result = pf.run_preflight(title="x", tags=["agent"], caller=boom)
    assert result.sane is True
    assert result.sane_fatal is False
    assert "haiku is down" in result.preflight_error


@pytest.mark.unit
def test_preflight_unparseable_reply_sanity_is_not_fatal():
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub("totally not json"))
    assert result.sane is True
    assert result.sane_fatal is False
    assert "no JSON object" in result.preflight_error


# ---------------------------------------------------------------------------
# A routing/method-of-execution question smuggled into `ambiguity`
# must not block; routing (including the default route) owns that decision.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_routing_flavored_ambiguity_is_suppressed():
    """The model may raise 'should this go to a
    local agent or is it a design task for a human engineer' as ambiguity.
    That's a routing question, not a blocking ambiguity."""
    reply = _golden_reply(
        routing="ask",
        routing_reason="No explicit model cue or capability inference.",
        ambiguity={
            "question": (
                "Should this task be routed to a local agent for code "
                "implementation, or is it a design/specification task for "
                "a human engineer?"
            ),
        },
    )
    result = pf.run_preflight(
        title="Turn the inactive record button white when not recording",
        tags=["agent"], caller=_stub(reply),
    )
    assert result.ambiguity is None


@pytest.mark.unit
def test_genuine_missing_referent_ambiguity_still_blocks():
    """A real missing-referent ambiguity must not be swallowed by the
    routing-flavored matcher."""
    reply = _golden_reply(
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    result = pf.run_preflight(title="reply to John", tags=["agent"], caller=_stub(reply))
    assert result.ambiguity is not None
    assert "John" in result.ambiguity.question


@pytest.mark.unit
def test_default_route_applies_when_ambiguity_is_routing_flavored(monkeypatch):
    """The default route must not be defeated by a spurious routing-
    flavored ambiguity."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(
        routing="ask",
        routing_reason="No explicit model cue or capability inference.",
        ambiguity={
            "question": (
                "Should this task be routed to a local agent for code "
                "implementation, or is it a design/specification task for "
                "a human engineer?"
            ),
        },
    )
    result = pf.run_preflight(
        title="Turn the inactive record button white when not recording",
        tags=["agent"], caller=_stub(reply),
    )
    assert result.ambiguity is None
    assert result.routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_preflight_handles_json_in_code_fence():
    """Some models like to wrap output in ```json fences."""
    inner = _golden_reply()
    reply = f"Here you go:\n```json\n{inner}\n```\n"
    result = pf.run_preflight(title="something", tags=["agent", "local"], caller=_stub(reply))
    assert result.sane is True
    assert result.routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_preflight_unparseable_reply_defaults_to_ask():
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub("totally not json"))
    assert result.sane is True
    assert result.sane_fatal is False
    assert "no JSON object" in result.preflight_error
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_caller_exception_defaults_to_ask():
    def boom(prompt):
        raise RuntimeError("haiku is down")

    result = pf.run_preflight(title="x", tags=["agent"], caller=boom)
    assert result.sane is True
    assert result.sane_fatal is False
    assert "haiku is down" in result.preflight_error
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
# Prompt-content tests — verify routing rules visible to Haiku
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
    docs or web search. The prompt tells the classifier that
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
# Tag precedence
# ---------------------------------------------------------------------------

def _stub_caller(routing="claude", routing_explicit=False):
    """Build a fake caller returning a minimal preflight JSON.

    `routing_explicit` mirrors the classifier's own flag: true only when the
    operator named the engine themselves. Cloud routes without it are
    downgraded to `ask`, so tests that want a real cloud dispatch either
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
def test_cloud_tag_routes_to_remote_provider_not_anthropic():
    """`#cloud` routes to the configured remote OpenAI-compatible
    provider, never the Anthropic API — regardless of what preflight itself
    returned. `model` is left empty: the remote model id comes from
    `settings.remote_llm_model` at dispatch time, not from `ALLOWED_MODELS`."""
    result = pf.run_preflight("anything", tags=["agent", "cloud"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_REMOTE
    assert result.routing_explicit is True
    assert result.model == ""


@pytest.mark.unit
def test_untagged_cloud_route_is_downgraded_to_ask():
    """An inferred cloud route never dispatches on its own.

    Without a `#cloud*` tag or an operator who named the engine, a `claude` route from
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
# Preset class tag detection
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
# Cost gates: fail-fast budget check + cost preview
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cloud_route_emits_cost_estimate():
    """Cloud-routed (Anthropic API) tasks get a non-zero cache-cold cost
    estimate so the orchestrator can preview cost before dispatch."""
    result = pf.run_preflight("research task", tags=["agent", "research", "cloud-sonnet"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.estimated_cost_dollars > 0


@pytest.mark.unit
def test_remote_route_emits_zero_estimate():
    """`#cloud` (the remote provider) is treated like local/CLI for
    preflight cost-preview purposes — the §6/§7 confirmation ceremony is
    specifically for the Anthropic-API 'expensive exception', not third-party
    spend in general. Real spend still records correctly at execution time
    (see `LocalExecutor._record_spend`'s `is_remote` branch)."""
    result = pf.run_preflight("anything", tags=["agent", "cloud"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_REMOTE
    assert result.estimated_cost_dollars == 0.0
    assert result.needs_cost_confirmation is False


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
    full = pf.run_preflight("any task", tags=["agent", "fullstack", "cloud-sonnet"],
                            caller=_stub_caller(routing="claude"))
    research = pf.run_preflight("any task", tags=["agent", "research", "cloud-sonnet"],
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
    result = pf.run_preflight("expensive task", tags=["agent", "fullstack", "cloud-sonnet"],
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
    result = pf.run_preflight("any task", tags=["agent", "fullstack", "cloud-sonnet"],
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
# API-spend gate: an inferred cloud route never dispatches on its own
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
    "run it on the anthropic api",
])
def test_operator_naming_the_engine_dispatches_without_a_question(title):
    """The other half of the rule: an operator who asked for it gets it.
    Same principle as the `#cloud-haiku`/`#cloud-sonnet` tags — explicit
    intent is consent."""
    result = pf.run_preflight(title, tags=["agent"],
                              caller=_stub_caller(routing="claude", routing_explicit=True))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.routing_explicit is True


@pytest.mark.unit
def test_bare_cloud_in_title_no_longer_corroborates_anthropic():
    """A title merely containing the bare word "cloud" must not count as
    corroboration for a model-claimed `routing="claude"`: `#cloud` the tag
    means the configured remote provider, not the Anthropic API, so this
    title must NOT dispatch straight to the API — it falls through
    to the downgrade-to-`ask` path instead, same as any other
    unconfirmed cloud inference."""
    result = pf.run_preflight("run it on the cloud model", tags=["agent"],
                              caller=_stub_caller(routing="claude", routing_explicit=True))
    assert result.routing == pf.ROUTE_ASK
    assert result.routing_explicit is False


@pytest.mark.unit
@pytest.mark.parametrize("tag,expected_model", [
    ("cloud-haiku", pf.MODEL_HAIKU),
    ("cloud-sonnet", pf.MODEL_SONNET),
])
def test_cloud_tags_are_consent_and_still_dispatch(tag, expected_model):
    """`#cloud-haiku`/`#cloud-sonnet` tasks were explicitly tagged by the
    operator, so they keep dispatching straight to the API — the gate is
    about inference only. (Bare `#cloud` is covered separately — it routes
    to the remote provider, not the API.)"""
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


@pytest.mark.unit
def test_preflight_max_tokens_floor():
    """`_PREFLIGHT_MAX_TOKENS` must stay large enough for a reasoning model
    to emit its hidden reasoning before the JSON reply: at 1024 tokens the
    configured remote classifier's reply was truncated
    (finish_reason='length') on 5 of 8 trials against a real task title,
    and the worst observed successful completion used 1621 output tokens."""
    assert pf._PREFLIGHT_MAX_TOKENS >= 4096


# ---------------------------------------------------------------------------
# `_default_llm_caller` client-selection fallback order
# ---------------------------------------------------------------------------

class _FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text


@pytest.mark.unit
def test_default_llm_caller_uses_anthropic_when_key_set_no_probe(monkeypatch):
    """Order 1: an Anthropic key selects AnthropicLLMClient with
    agent_preflight_model, and never touches
    LocalLLMClient.is_available (no reachability probe on this branch)."""
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    # Host .env may force LIFEOS_AGENT_PREFLIGHT_ENGINE=remote; pin auto so
    # this exercises the Anthropic-first default-caller path.
    monkeypatch.setattr(settings, "agent_preflight_engine", "auto", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    captured = {}

    def fake_init(self, api_key=None, model=None):
        captured["model"] = model
        captured["api_key"] = api_key

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
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
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS
    assert captured["temperature"] == 0.0


@pytest.mark.unit
def test_default_llm_caller_falls_back_to_local_when_reachable(monkeypatch):
    """Order 2: no Anthropic key, local llama-server reachable → runs on it."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "auto", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    captured = {}
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: True)

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["base_url"] = self.base_url
        captured["model"] = self.model
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("local reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "local reply"
    assert captured["model"] == "local"
    assert captured["base_url"] == LocalLLMClient().base_url
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


@pytest.mark.unit
def test_default_llm_caller_falls_back_to_remote_when_local_unreachable(monkeypatch):
    """Order 3: no Anthropic key, local unreachable, remote provider
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

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["base_url"] = self.base_url
        captured["model"] = self.model
        captured["timeout"] = self.timeout
        captured["auth"] = self._auth_headers()
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("remote reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "remote reply"
    # LocalLLMClient strips one trailing /v1 segment so the wire
    # path is always {base}/v1/chat/completions, never .../v1/v1/....
    assert captured["base_url"] == "https://remote.example"
    assert captured["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert captured["timeout"] == 42
    assert captured["auth"] == {"Authorization": "Bearer fw_test_key"}
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


@pytest.mark.unit
def test_default_llm_caller_raises_when_no_client_usable(monkeypatch):
    """Order 4: no key, no reachable local server, no remote provider ⇒
    raise. `run_preflight`'s existing except-clause degrades this to a
    non-fatal `preflight_error`/routing=ask result rather than propagating."""
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
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.preflight_error
    assert result.routing == pf.ROUTE_ASK


# ---------------------------------------------------------------------------
# LIFEOS_AGENT_PREFLIGHT_ENGINE: which client `_default_llm_caller`
# builds for the preflight classifier call, independent of the auto
# order. Every test below monkeypatches anthropic_api_key, the full
# remote_llm_* block, and agent_remote_executor explicitly (in addition to
# agent_preflight_engine) — a host .env can set any of these ambiently, and
# an un-monkeypatched one would silently change which branch a test
# exercises.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_preflight_engine_auto_explicit_matches_704_order(monkeypatch):
    """Explicit engine="auto" reproduces the Anthropic-first order —
    no probe of the local llama-server, same client construction. Proves
    "auto" is a real branch, not just the unset-default case the other
    tests already cover."""
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "auto", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    captured = {}

    def fake_init(self, api_key=None, model=None):
        captured["model"] = model

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("anthropic reply")

    monkeypatch.setattr(AnthropicLLMClient, "__init__", fake_init)
    monkeypatch.setattr(AnthropicLLMClient, "create", fake_create)

    def _forbidden_probe(self):
        raise AssertionError("auto+key must not probe the local llama-server")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    result = pf._default_llm_caller("some prompt")

    assert result == "anthropic reply"
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


@pytest.mark.unit
def test_preflight_engine_remote_configured_builds_remote_client(monkeypatch):
    """engine="remote" + remote_llm_configured dispatches to the remote
    provider FIRST and unprobed — even with a usable Anthropic key AND a
    reachable local server also available, proving it isn't merely falling
    into the auto chain's own remote fallback."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "remote", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "unused-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)  # must not matter for "remote"
    monkeypatch.setattr(settings, "remote_llm_base_url", "https://remote.example/v1", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "accounts/fireworks/models/deepseek-v4-flash-0731", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "fw_test_key", raising=False)
    monkeypatch.setattr(settings, "remote_llm_timeout", 42, raising=False)

    def _forbidden_probe(self):
        raise AssertionError("remote engine must not probe reachability (#706: unprobed by design)")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    captured = {}

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["base_url"] = self.base_url
        captured["model"] = self.model
        captured["timeout"] = self.timeout
        captured["auth"] = self._auth_headers()
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        captured["temperature"] = temperature
        return _FakeLLMResponse("remote reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "remote reply"
    # LocalLLMClient strips one trailing /v1 segment.
    assert captured["base_url"] == "https://remote.example"
    assert captured["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert captured["timeout"] == 42
    assert captured["auth"] == {"Authorization": "Bearer fw_test_key"}
    assert captured["messages"] == [{"role": "user", "content": "some prompt"}]
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS
    assert captured["temperature"] == 0.0


@pytest.mark.unit
def test_preflight_engine_remote_unconfigured_raises_instead_of_falling_back(monkeypatch):
    """engine="remote" but the provider isn't configured -> raise (fail
    closed). A forced engine must never silently revert to the `auto`
    chain — with an Anthropic key present that would mean API spend the
    operator explicitly opted out of. `run_preflight` degrades the raise
    to routing=ask, which is the visible outcome the operator should get."""
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "remote", raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)

    def _forbidden_anthropic(self, *a, **kw):
        raise AssertionError("forced remote engine must never construct the Anthropic client")

    def _forbidden_probe(self):
        raise AssertionError("forced remote engine must never fall back to local")

    monkeypatch.setattr(AnthropicLLMClient, "__init__", _forbidden_anthropic)
    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    with pytest.raises(RuntimeError, match="remote provider is not configured"):
        pf._default_llm_caller("some prompt")


@pytest.mark.unit
def test_preflight_engine_anthropic_forced(monkeypatch):
    """engine="anthropic" forces the Anthropic branch when a key is present."""
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "anthropic", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)

    captured = {}

    def fake_init(self, api_key=None, model=None):
        captured["model"] = model

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("anthropic reply")

    monkeypatch.setattr(AnthropicLLMClient, "__init__", fake_init)
    monkeypatch.setattr(AnthropicLLMClient, "create", fake_create)

    def _forbidden_probe(self):
        raise AssertionError("forced anthropic engine must not probe the local llama-server")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    result = pf._default_llm_caller("some prompt")

    assert result == "anthropic reply"
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


@pytest.mark.unit
def test_preflight_engine_anthropic_unconfigured_raises_instead_of_falling_back(monkeypatch):
    """engine="anthropic" but no key configured -> raise (fail closed),
    never a silent hop to local or the remote provider."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "anthropic", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    def _forbidden_probe(self):
        raise AssertionError("forced anthropic engine must never fall back to local")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    with pytest.raises(RuntimeError, match="no Anthropic API key"):
        pf._default_llm_caller("some prompt")


@pytest.mark.unit
def test_preflight_engine_local_forced_uses_probe(monkeypatch):
    """engine="local" forces the local client and still probes
    is_available() — even with an Anthropic key present, proving it isn't
    falling into the auto chain's Anthropic-first branch."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "local", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "unused-anthropic-key", raising=False)

    probed = {"called": False}

    def fake_is_available(self):
        probed["called"] = True
        return True

    monkeypatch.setattr(LocalLLMClient, "is_available", fake_is_available)

    captured = {}

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["model"] = self.model
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("local reply")

    monkeypatch.setattr(LocalLLMClient, "create", fake_create)

    result = pf._default_llm_caller("some prompt")

    assert result == "local reply"
    assert probed["called"] is True
    assert captured["model"] == "local"
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


@pytest.mark.unit
def test_preflight_engine_local_forced_raises_when_unreachable(monkeypatch):
    """engine="local" forced with an unreachable server raises rather than
    silently falling back to another engine — there's no further engine to
    fall back to for a forced value. `run_preflight`'s existing except-
    clause still degrades this to a non-fatal `preflight_error`/ask
    result, unchanged."""
    from config.settings import settings
    from api.services.llm_client import LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "local", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(LocalLLMClient, "is_available", lambda self: False)

    with pytest.raises(RuntimeError):
        pf._default_llm_caller("some prompt")

    result = pf.run_preflight("do the thing", tags=["agent"], caller=None)
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.preflight_error
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_preflight_engine_invalid_value_falls_back_to_auto(monkeypatch, caplog):
    """An unrecognized LIFEOS_AGENT_PREFLIGHT_ENGINE value never crashes —
    it's treated as `auto` (with a logged warning), mirroring
    `_apply_default_route`'s own invalid-value handling."""
    import logging
    from config.settings import settings
    from api.services.llm_client import AnthropicLLMClient, LocalLLMClient

    monkeypatch.setattr(settings, "agent_preflight_engine", "bogus-value", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-anthropic-key", raising=False)
    monkeypatch.setattr(settings, "agent_preflight_model", "claude-haiku-4-5", raising=False)
    monkeypatch.setattr(settings, "agent_remote_executor", False, raising=False)
    monkeypatch.setattr(settings, "remote_llm_base_url", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_model", "", raising=False)
    monkeypatch.setattr(settings, "remote_llm_api_key", "", raising=False)

    def fake_init(self, api_key=None, model=None):
        pass

    captured = {}

    def fake_create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        captured["max_tokens"] = max_tokens
        return _FakeLLMResponse("anthropic reply")

    monkeypatch.setattr(AnthropicLLMClient, "__init__", fake_init)
    monkeypatch.setattr(AnthropicLLMClient, "create", fake_create)

    def _forbidden_probe(self):
        raise AssertionError("invalid engine treated as auto+key must not probe local")

    monkeypatch.setattr(LocalLLMClient, "is_available", _forbidden_probe)

    with caplog.at_level(logging.WARNING, logger="api.services.agent_worker.preflight"):
        result = pf._default_llm_caller("some prompt")

    assert result == "anthropic reply"
    assert any("invalid" in rec.message.lower() for rec in caplog.records)
    assert captured["max_tokens"] == pf._PREFLIGHT_MAX_TOKENS


# ---------------------------------------------------------------------------
# LIFEOS_AGENT_DEFAULT_ROUTE: route "ask for lack of cues" outcomes
# instead of blocking, on installs with exactly one executor.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_default_route_unset_is_byte_identical_to_today():
    """Empty setting (the default) must not touch the no-cues ask path."""
    reply = _golden_reply(routing="ask", routing_reason="no tag and no title cue")
    result = pf.run_preflight(title="research dolphins", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK
    assert result.routing_reason == "no tag and no title cue"


@pytest.mark.unit
def test_default_route_applies_when_no_cues(monkeypatch):
    """Set + no-cues ask -> routes without blocking, and routing_reason
    names the setting."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(routing="ask", routing_reason="no tag and no title cue")
    result = pf.run_preflight(title="research dolphins", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert "LIFEOS_AGENT_DEFAULT_ROUTE=local" in result.routing_reason
    assert result.model == pf.MODEL_LOCAL


@pytest.mark.unit
def test_default_route_applies_to_llm_omitted_routing(monkeypatch):
    """The deterministic no-cues path (invalid/missing `routing` from the
    model) is also routed by the default — same "nothing to route on"
    outcome as the LLM's own explicit `ask`."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(routing="not-a-real-route")
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_default_route_demotes_ambiguity_and_applies(monkeypatch):
    """With a default route configured, a non-null ambiguity on an
    otherwise-runnable task is demoted to advisory (not discarded — it's
    preserved on `demoted_ambiguity` for the session log) and the default
    route applies, rather than the task blocking on the question.

    A configured default route is a standing "run untagged tasks without
    asking me" instruction that a cheap classifier's hedging must not
    override, and string-matching the hedge's prose proved to be
    whack-a-mole once the model rephrased around the pattern.
    """
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(
        routing="ask", routing_reason="no tag and no title cue",
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    result = pf.run_preflight(title="reply to John", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.ambiguity is None
    assert result.demoted_ambiguity == "Which John — John Doe or John Smith?"


@pytest.mark.unit
def test_default_route_does_not_apply_on_sanity_failure(monkeypatch):
    """Set + sane=False -> still ask."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(routing="ask", sane=False, sane_reason="destructive: 'rm -rf /'")
    result = pf.run_preflight(title="rm -rf /", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK
    assert result.sane is False


@pytest.mark.unit
def test_default_route_does_not_apply_on_empty_title(monkeypatch):
    """Set + empty title (sanity short-circuit) -> still ask, no LLM call."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")

    def fail(prompt):
        raise AssertionError("LLM should not have been called for empty title")

    result = pf.run_preflight(title="   ", tags=["agent"], caller=fail)
    assert result.routing == pf.ROUTE_ASK
    assert result.sane is False


@pytest.mark.unit
def test_default_route_does_not_apply_on_llm_error(monkeypatch):
    """Set + preflight LLM call failed -> still ask, not substituted onto
    the configured default route, because the classifier never obtained a
    verdict (`preflight_error` set)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")

    def boom(prompt):
        raise RuntimeError("no client available")

    result = pf.run_preflight(title="do the thing", tags=["agent"], caller=boom)
    assert result.routing == pf.ROUTE_ASK
    assert result.sane is True
    assert result.preflight_error


@pytest.mark.unit
def test_default_route_does_not_apply_to_unconfirmed_cloud_inference(monkeypatch):
    """Set + a cloud-inference downgrade (a cue nobody confirmed, not a
    *lack* of cues) -> still ask. This is the case the plain
    `routing == ask and sane and ambiguity is None` gate would wrongly
    catch without the original-routing check."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    result = pf.run_preflight("draft an email", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.routing == pf.ROUTE_ASK
    assert "not explicitly requested" in result.routing_reason


@pytest.mark.unit
def test_default_route_does_not_rescue_unconfirmed_cloud_even_with_ambiguity(monkeypatch):
    """Demoting ambiguity for a configured default route must not weaken
    the cloud-inference downgrade: it must not also rescue an inferred
    (unconfirmed) cloud route from the downgrade — never auto-spend on
    inferred cloud routing, ambiguity or not."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(
        routing="claude", routing_reason="implies email capability", routing_explicit=False,
        ambiguity={"question": "Which recipient — no name given in the title?"},
    )
    result = pf.run_preflight(title="draft an email", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK
    assert "not explicitly requested" in result.routing_reason
    # The ambiguity is still demoted (advisory) — it just doesn't change the
    # ask outcome, since routing==ask blocks independently either way.
    assert result.ambiguity is None
    assert result.demoted_ambiguity == "Which recipient — no name given in the title?"


@pytest.mark.unit
def test_default_route_invalid_value_logs_error_and_falls_back_to_ask(monkeypatch, caplog):
    """AC: an invalid value surfaces a clear error rather than silently
    asking (or crashing the worker loop) — falls back to `ask` with a
    logged ERROR."""
    import logging
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "bogus-route")
    reply = _golden_reply(routing="ask", routing_reason="no tag and no title cue")
    with caplog.at_level(logging.ERROR, logger="api.services.agent_worker.preflight"):
        result = pf.run_preflight(title="research dolphins", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_ASK
    assert any("LIFEOS_AGENT_DEFAULT_ROUTE" in rec.message for rec in caplog.records)
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


@pytest.mark.unit
def test_default_route_invalid_value_does_not_demote_ambiguity(monkeypatch):
    """Ambiguity demotion is gated on the setting being non-empty AND
    valid — a typo'd value is not a standing instruction the operator
    successfully gave, so ambiguity must still block exactly like the
    no-default-route case."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "bogus-route")
    reply = _golden_reply(
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    result = pf.run_preflight(title="reply to John", tags=["agent"], caller=_stub(reply))
    assert result.ambiguity is not None
    assert result.demoted_ambiguity is None


@pytest.mark.unit
def test_default_route_tags_still_win(monkeypatch):
    """Tag overrides beat the default route — precedence: tags > explicit
    LLM route > default route > ask."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude")
    reply = _golden_reply(routing="ask", routing_reason="no tag and no title cue")
    result = pf.run_preflight(title="research dolphins", tags=["agent", "local"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.routing_reason == "#local tag present"


# ---------------------------------------------------------------------------
# A non-fatal sanity opinion must not gate feature requests when a
# default route is configured: the operator's standing "run untagged tasks
# without asking me" instruction outranks a cheap classifier's "this isn't
# executable" hedge, the same way default-route demotion already works for
# `ambiguity`. Every test here monkeypatches `settings.agent_default_route`
# explicitly (never relies on it being unset) since a host `.env` can leak
# the setting in.
# ---------------------------------------------------------------------------

# Two real field verdicts: the classifier calling an
# ordinary feature request "a product specification or feature request, not
# a task an agent can execute" — once on a voice-UI task, once on a
# macOS setup-script parity task.
_FIELD_VERDICT_SANE_REASON = (
    "This is a product specification or feature request, not a task an "
    "agent can execute."
)
_FIELD_VERDICT_TITLES = [
    "Display the user's transcribed message immediately after sending",
    "Bring the macOS setup script's service catalog up to parity with "
    "Linux for the agent worker and MCP-HTTP bridge",
]


@pytest.mark.unit
@pytest.mark.parametrize("title", _FIELD_VERDICT_TITLES)
def test_default_route_demotes_field_verdict_sanity_and_runs(monkeypatch, title):
    """Given a default route and one of the real field
    verdicts, the task routes and runs — the opinion is demoted to
    `demoted_sanity` and logged, not surfaced as a block."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(
        routing="ask", routing_reason="no tag and no title cue",
        sane=False, sane_reason=_FIELD_VERDICT_SANE_REASON,
    )
    result = pf.run_preflight(title=title, tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.demoted_sanity == _FIELD_VERDICT_SANE_REASON


@pytest.mark.unit
def test_no_default_route_field_verdict_sanity_still_parks(monkeypatch):
    """With no default route configured, the sanity-opinion park
    behavior is unchanged — explicit empty setting, not ambient default,
    per the host-.env leak risk."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "")
    reply = _golden_reply(
        routing="local", sane=False, sane_reason=_FIELD_VERDICT_SANE_REASON,
    )
    result = pf.run_preflight(
        title=_FIELD_VERDICT_TITLES[0], tags=["agent"], caller=_stub(reply),
    )
    assert result.sane is False
    assert result.sane_fatal is False
    assert result.demoted_sanity is None


@pytest.mark.unit
def test_default_route_does_not_demote_fatal_sanity_empty_title(monkeypatch):
    """Fatal verdicts stay fail-closed regardless of default route: empty
    title short-circuit."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")

    def fail(prompt):
        raise AssertionError("LLM should not have been called for empty title")

    result = pf.run_preflight(title="   ", tags=["agent"], caller=fail)
    assert result.sane is False
    assert result.sane_fatal is True
    assert result.demoted_sanity is None
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_default_route_does_not_demote_fatal_sanity_destructive_title(monkeypatch):
    """Fatal verdicts stay fail-closed regardless of default route: the
    deterministic destructive-title regex."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(routing="ask", sane=False, sane_reason="destructive: 'rm -rf /'")
    result = pf.run_preflight(title="rm -rf /", tags=["agent"], caller=_stub(reply))
    assert result.sane is False
    assert result.sane_fatal is True
    assert result.demoted_sanity is None
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_default_route_does_not_substitute_on_llm_error(monkeypatch):
    """The preflight LLM call itself failing carries no verdict to demote —
    `preflight_error` is set and routing stays `ask`, not substituted onto
    the configured default route."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")

    def boom(prompt):
        raise RuntimeError("no client available")

    result = pf.run_preflight(title="do the thing", tags=["agent"], caller=boom)
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.preflight_error
    assert result.demoted_sanity is None
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_default_route_does_not_substitute_on_unparseable_reply(monkeypatch):
    """An unparseable preflight reply carries no verdict to demote —
    `preflight_error` is set and routing stays `ask`, not substituted onto
    the configured default route."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    result = pf.run_preflight(title="x", tags=["agent"], caller=_stub("totally not json"))
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.preflight_error
    assert result.demoted_sanity is None
    assert result.routing == pf.ROUTE_ASK


@pytest.mark.unit
def test_default_route_invalid_value_does_not_demote_sanity(monkeypatch):
    """Sanity demotion is gated on the setting being non-empty AND valid — a
    typo'd value is not a standing instruction the operator successfully
    gave, so a non-fatal sanity objection must still park exactly like the
    no-default-route case."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "bogus-route")
    reply = _golden_reply(
        routing="local", sane=False, sane_reason=_FIELD_VERDICT_SANE_REASON,
    )
    result = pf.run_preflight(
        title=_FIELD_VERDICT_TITLES[0], tags=["agent"], caller=_stub(reply),
    )
    assert result.sane is False
    assert result.sane_fatal is False
    assert result.demoted_sanity is None


# ---------------------------------------------------------------------------
# An uncorroborated LLM-invented route must not bypass
# LIFEOS_AGENT_DEFAULT_ROUTE. All tests explicitly monkeypatch
# `settings.agent_default_route` rather than relying on ambient env (a
# freshly-filed issue exists about the host's real .env leaking into tests).
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_field_evidence_uncorroborated_local_route_demoted_to_default(monkeypatch):
    """A real field payload: LIFEOS_AGENT_DEFAULT_ROUTE
    configured, the model returns routing="local" with a plausible-sounding
    but non-cue reason, and the title has no rule-3 cue at all. The default
    route must win, and the demotion must be recorded on `demoted_routing`
    (and logged into the preflight transcript event by worker.py)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    reply = _golden_reply(
        routing="local",
        routing_reason="Software implementation task suitable for local execution.",
        routing_explicit=True,
    )
    title = (
        "Implement in web/chat: in voice mode, render the user's transcribed "
        "message as it streams in"
    )
    result = pf.run_preflight(title=title, tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert result.demoted_routing == pf.ROUTE_LOCAL
    assert "LIFEOS_AGENT_DEFAULT_ROUTE=claude_code" in result.routing_reason


@pytest.mark.unit
def test_default_route_ignores_explicit_flag_without_title_corroboration(monkeypatch):
    """`routing_explicit=true` alone is not corroboration — same principle
    the cloud-inference downgrade already applies to cloud, extended here
    to local/claude_code/codex. A `true` with no matching title cue still
    gets demoted."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    reply = _golden_reply(
        routing="local", routing_explicit=True, routing_reason="operator wants local",
    )
    result = pf.run_preflight(title="Refactor the sync pipeline", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert result.demoted_routing == pf.ROUTE_LOCAL


@pytest.mark.unit
def test_title_names_local_engine_corroborates_and_wins_over_default(monkeypatch):
    """Title "using gemma" + default configured -> local wins (the operator
    actually named the engine, via the prompt's own rule-3 cue)."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    reply = _golden_reply(
        routing="local", routing_explicit=True, routing_reason="title says 'using gemma'",
    )
    result = pf.run_preflight(title="Summarize this using gemma", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.demoted_routing is None


@pytest.mark.unit
def test_title_names_claude_code_corroborates_and_wins_over_default(monkeypatch):
    """Title "use claude code" -> claude_code wins over a configured
    default, even though preflight's own schema never asks the model to
    emit `claude_code` directly — KNOWN_ROUTES accepts it defensively, so a
    noncompliant model emitting it anyway still gets a corroboration check."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    reply = _golden_reply(
        routing="claude_code", routing_explicit=True,
        routing_reason="operator asked for claude code",
    )
    result = pf.run_preflight(
        title="Use claude code to refactor this module", tags=["agent"], caller=_stub(reply),
    )
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert result.demoted_routing is None


@pytest.mark.unit
def test_corroborated_cloud_route_stands_even_with_default_route_configured(monkeypatch):
    """A title that genuinely names the engine
    (rule 3) still dispatches straight to Claude even though a default
    route is configured for something else. The route-corroboration gate
    is out of scope for ROUTE_CLAUDE entirely — the cloud-inference
    downgrade's own corroboration check inside `_apply_tag_overrides`
    already governs it — so a corroborated cloud route is untouched by
    route corroboration either way."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    result = pf.run_preflight(
        "use claude to draft the email", tags=["agent"],
        caller=_stub_caller(routing="claude", routing_explicit=True),
    )
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.routing_explicit is True
    assert result.demoted_routing is None


@pytest.mark.unit
def test_uncorroborated_cloud_inference_still_confirms_with_default_route_configured(monkeypatch):
    """The other half of the combined case: an *inferred* (not corroborated)
    cloud route must still go through the cloud-inference downgrade's
    confirmation flow — `ask`, not silently redirected to the configured
    default and not silently dispatched to the API. This mirrors
    `test_default_route_does_not_apply_to_unconfirmed_cloud_inference`
    deliberately, right next to the route-corroboration tests, as explicit
    proof that route corroboration does not weaken the cloud-inference
    downgrade."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    result = pf.run_preflight(
        "search my gmail for the invoice", tags=["agent"],
        caller=_stub_caller(routing="claude", routing_explicit=False),
    )
    assert result.routing == pf.ROUTE_ASK
    assert "not explicitly requested" in result.routing_reason
    assert result.demoted_routing is None


@pytest.mark.unit
def test_tag_override_bypasses_route_corroboration_check(monkeypatch):
    """A `#cloud-sonnet` tag is direct operator corroboration — the route-
    corroboration title-cue check must not run on it at all, even though
    the model's own uncorroborated route (local) would otherwise have been
    demoted."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    reply = _golden_reply(routing="local", routing_explicit=False, routing_reason="stub")
    result = pf.run_preflight(title="Refactor auth", tags=["agent", "cloud-sonnet"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_CLAUDE
    assert result.demoted_routing is None


@pytest.mark.unit
def test_bare_cloud_tag_also_bypasses_route_corroboration_check(monkeypatch):
    """Same bypass, for the bare `#cloud` tag routing to `remote` —
    it's still direct operator corroboration, just for a different route."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    reply = _golden_reply(routing="local", routing_explicit=False, routing_reason="stub")
    result = pf.run_preflight(title="Refactor auth", tags=["agent", "cloud"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_REMOTE
    assert result.demoted_routing is None


@pytest.mark.unit
def test_no_default_route_uncorroborated_local_route_is_unaffected():
    """With no default route configured, route corroboration is a complete
    no-op: the field-evidence payload's routing stands unchanged,
    byte-identical for every fresh clone."""
    reply = _golden_reply(
        routing="local", routing_explicit=True,
        routing_reason="Software implementation task suitable for local execution.",
    )
    title = (
        "Implement in web/chat: in voice mode, render the user's transcribed "
        "message as it streams in"
    )
    result = pf.run_preflight(title=title, tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_LOCAL
    assert result.demoted_routing is None


# ---------------------------------------------------------------------------
# A failed/unparseable preflight call must never cancel a task that carries
# an explicit routing tag — the operator's own tag already fully determines
# routing, independent of the classifier's advisory opinion.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_unparseable_reply_with_explicit_tag_still_runs():
    """The reported live failure: a reasoning-model reply truncated before
    it emitted JSON must not cancel a `#claude`-tagged task."""
    result = pf.run_preflight(
        title="Fix the login bug", tags=["claude"], caller=_stub("totally not json"),
    )
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.preflight_error


@pytest.mark.unit
def test_llm_call_failure_with_explicit_tag_still_runs():
    def boom(prompt):
        raise RuntimeError("preflight provider unreachable")

    result = pf.run_preflight(title="Refactor the parser", tags=["local"], caller=boom)
    assert result.routing == pf.ROUTE_LOCAL
    assert result.sane is True


@pytest.mark.unit
def test_unparseable_reply_untagged_with_default_route_stays_ask(monkeypatch):
    """A default route must not rescue a failed classifier call onto it —
    without a routing tag, the task stays `ask` so the operator is asked,
    rather than being silently auto-dispatched."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local")
    result = pf.run_preflight(
        title="Do the thing", tags=["agent"], caller=_stub("totally not json"),
    )
    assert result.routing == pf.ROUTE_ASK
    assert result.preflight_error


@pytest.mark.unit
def test_truncated_json_reply_is_not_fatal():
    """A real captured provider payload: a reasoning model's JSON reply cut
    off mid-object by an exhausted output-token budget. Untagged, this
    parks; tagged, it must still run on the tagged route."""
    truncated = (
        '{\n  "budget": {\n    "wall_seconds": 14400,\n    "max_tokens": 500000,\n'
        '    "max_dollars": 5.0\n  },\n  "routing": "claude",\n'
        '  "routing_reason": "explicit #claude tag",\n  "routing_explicit": true,'
    )
    untagged = pf.run_preflight(title="Fix the login bug", tags=["agent"], caller=_stub(truncated))
    assert untagged.sane is True
    assert untagged.sane_fatal is False
    assert untagged.preflight_error
    assert untagged.routing == pf.ROUTE_ASK

    tagged = pf.run_preflight(title="Fix the login bug", tags=["claude"], caller=_stub(truncated))
    assert tagged.routing == pf.ROUTE_CLAUDE_CODE
    assert tagged.sane is True
    assert tagged.sane_fatal is False
    assert tagged.preflight_error


# ---------------------------------------------------------------------------
# Jev destructiveness judgment (`_apply_destructive_judgment` /
# `LIFEOS_AGENT_JEV_DESTRUCTIVE_GATE`). The Jev call itself is stubbed via
# monkeypatching `JevClient.ask`; the LLM preflight `caller` stays a normal
# `_golden_reply()` stub throughout, since these tests exercise the gate
# layered on top of preflight, not preflight's own JSON parsing.
# ---------------------------------------------------------------------------

from api.services.jev_client import JevClient, JevError  # noqa: E402


def _stub_jev_ask(score: float, probability: float):
    def _ask(self, state, questions, *, model=None):
        return {
            "harm": {"score": score, "confidence": 0.9},
            "irreversible": {"noul": probability},
        }
    return _ask


@pytest.mark.unit
def test_destructive_judgment_no_key_never_constructs_client(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "shadow")

    def _boom_init(self, *a, **kw):
        raise AssertionError("JevClient must not be constructed with no key configured")

    monkeypatch.setattr(JevClient, "__init__", _boom_init)

    result = pf.run_preflight(title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()))
    assert result.destructive_score is None
    assert result.destructive_probability is None


@pytest.mark.unit
def test_destructive_judgment_gate_off_with_key_never_constructs_client(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "off")

    def _boom_init(self, *a, **kw):
        raise AssertionError("JevClient must not be constructed when the gate is off")

    monkeypatch.setattr(JevClient, "__init__", _boom_init)

    result = pf.run_preflight(title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()))
    assert result.destructive_score is None
    assert result.destructive_probability is None


@pytest.mark.unit
def test_destructive_judgment_shadow_records_fields_without_mutating_sane(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "shadow")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=4.0, probability=0.99))

    result = pf.run_preflight(
        title="Purge all email older than 2020", tags=["agent"], caller=_stub(_golden_reply()),
    )
    assert result.destructive_score == pytest.approx(4.0)
    assert result.destructive_probability == pytest.approx(0.99)
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.destructive_block is False


@pytest.mark.unit
def test_destructive_judgment_block_score_alone_parks(monkeypatch):
    """Harm score above threshold, irreversible probability below —
    the OR must still park. (Mutation check: swapping the gate's OR for an
    AND makes this test fail.)"""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=3.0, probability=0.1))

    result = pf.run_preflight(
        title="Nuke the chromadb data dir and start fresh", tags=["agent"], caller=_stub(_golden_reply()),
    )
    assert result.sane is False
    assert result.sane_fatal is False  # non-fatal — parks, never cancels
    assert result.destructive_block is True
    assert "Jev destructive judgment" in result.sane_reason


@pytest.mark.unit
def test_destructive_judgment_block_probability_alone_parks(monkeypatch):
    """Irreversible probability above threshold, harm score below — the OR
    must still park. (Mutation check: swapping the gate's OR for an AND
    makes this test fail.)"""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=1.0, probability=0.9))

    result = pf.run_preflight(
        title="Force-push main and delete the old branches", tags=["agent"], caller=_stub(_golden_reply()),
    )
    assert result.sane is False
    assert result.sane_fatal is False
    assert result.destructive_block is True
    assert "Jev destructive judgment" in result.sane_reason


@pytest.mark.unit
def test_destructive_judgment_block_below_both_thresholds_untouched(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=0.5, probability=0.1))

    result = pf.run_preflight(title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()))
    assert result.destructive_score == pytest.approx(0.5)
    assert result.destructive_probability == pytest.approx(0.1)
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.destructive_block is False


@pytest.mark.unit
def test_destructive_judgment_block_regex_title_stays_fatal(monkeypatch):
    """The regex sanity gate runs first and sets sane_fatal=True on a
    regex-matching title — even a high-confidence Jev verdict must not
    overwrite it (the regex wins in every gate mode)."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=4.0, probability=0.99))

    result = pf.run_preflight(title="rm -rf /", tags=["agent"], caller=_stub(_golden_reply()))
    assert result.sane is False
    assert result.sane_fatal is True
    # The judgment still ran and recorded its answer even though it didn't
    # get to act on it.
    assert result.destructive_score == pytest.approx(4.0)
    assert result.destructive_probability == pytest.approx(0.99)
    assert result.destructive_block is False  # regex verdict, not the Jev gate, set sane=False


@pytest.mark.unit
def test_destructive_judgment_jev_error_leaves_fields_none(monkeypatch, caplog):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")

    def _boom(self, state, questions, *, model=None):
        raise JevError("status 500")

    monkeypatch.setattr(JevClient, "ask", _boom)

    with caplog.at_level("WARNING"):
        result = pf.run_preflight(
            title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()),
        )
    assert result.destructive_score is None
    assert result.destructive_probability is None
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.routing == pf.ROUTE_LOCAL  # golden reply's routing, untouched
    assert "JevError" in caplog.text
    # Never the title or transcript in the warning.
    assert "Fix the login bug" not in caplog.text


@pytest.mark.unit
def test_destructive_judgment_nan_answer_is_invalid(monkeypatch, caplog):
    """A NaN harm score is a real float (passes the type check) but not a
    finite in-range number — must be rejected the same as a failed call.

    Mutation check: dropping the `math.isfinite` check from
    `_validate_destructive_answer` makes this test fail."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=float("nan"), probability=0.99))

    with caplog.at_level("WARNING"):
        result = pf.run_preflight(
            title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()),
        )
    assert result.destructive_score is None
    assert result.destructive_probability is None
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.destructive_block is False
    assert "invalid answer" in caplog.text


@pytest.mark.unit
def test_destructive_judgment_bool_answer_is_invalid(monkeypatch, caplog):
    """`bool` is an `int` subclass — `float(True) == 1.0` must not be
    silently accepted as a valid harm score."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=True, probability=0.99))

    with caplog.at_level("WARNING"):
        result = pf.run_preflight(
            title="Fix the login bug", tags=["agent"], caller=_stub(_golden_reply()),
        )
    assert result.destructive_score is None
    assert result.destructive_probability is None
    assert result.sane is True
    assert result.sane_fatal is False
    assert result.destructive_block is False
    assert "invalid answer" in caplog.text


@pytest.mark.unit
def test_destructive_judgment_unknown_gate_value_behaves_as_shadow(monkeypatch, caplog):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "bogus-value")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=4.0, probability=0.99))

    with caplog.at_level("WARNING"):
        result = pf.run_preflight(
            title="Purge all email older than 2020", tags=["agent"], caller=_stub(_golden_reply()),
        )
    # Shadow behavior: fields recorded, sane untouched even though the
    # thresholds are crossed — an unknown value must not silently behave
    # like `block`.
    assert result.destructive_score == pytest.approx(4.0)
    assert result.destructive_probability == pytest.approx(0.99)
    assert result.sane is True
    assert result.sane_fatal is False
    assert "LIFEOS_AGENT_JEV_DESTRUCTIVE_GATE" in caplog.text


@pytest.mark.unit
def test_destructive_judgment_block_survives_default_route_demotion(monkeypatch):
    """`_apply_default_route`'s sanity demotion exists for the classifier's
    own free-form "not executable" opinion, not a code-thresholded Jev
    verdict — a `destructive_block` park must stay parked even when a
    default route is configured, unlike an ordinary non-fatal sanity
    objection (see `test_default_route_demotes_field_verdict_sanity_and_runs`,
    which pins that the ordinary case IS still demoted).

    Mutation check: removing the `destructive_block` guard from
    `_apply_default_route`'s sanity-demotion gate makes this test fail
    (sane flips back to True and demoted_sanity gets set)."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    monkeypatch.setattr(settings, "agent_default_route", "claude_code")
    monkeypatch.setattr(JevClient, "ask", _stub_jev_ask(score=3.0, probability=0.95))

    result = pf.run_preflight(
        title="Nuke the chromadb data dir and start fresh", tags=["agent"], caller=_stub(_golden_reply()),
    )
    assert result.sane is False
    assert result.sane_fatal is False
    assert result.destructive_block is True
    assert result.demoted_sanity is None


@pytest.mark.unit
def test_destructive_judgment_uses_separate_execution_safety_context(monkeypatch):
    """Inherited execution instructions reach only the Jev safety judgment.

    Parent text can change the destructive verdict, but engine words in that
    text must not become route corroboration or cloud consent.
    """
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_jev_destructive_gate", "block")
    classifier_prompts: list[str] = []
    jev_states: list[dict] = []

    def classifier(prompt: str) -> str:
        classifier_prompts.append(prompt)
        return _golden_reply(
            routing="claude",
            routing_reason="inferred from context",
            routing_explicit=True,
        )

    def destructive(self, state, questions, *, model=None):
        jev_states.append(state)
        return {
            "harm": {"score": 3.0, "confidence": 0.9},
            "irreversible": {"noul": 0.95},
        }

    monkeypatch.setattr(JevClient, "ask", destructive)
    safety_context = (
        "Child instructions:\nUpdate the synthetic release.\n\n"
        "Parent objective:\nUse Claude to permanently delete the synthetic archive."
    )

    result = pf.run_preflight(
        title="Implement phase two",
        tags=["codex"],
        caller=classifier,
        safety_context=safety_context,
    )

    assert result.routing == pf.ROUTE_CODEX
    assert result.destructive_block is True
    assert jev_states[0] == {
        "task_title": "Implement phase two",
        "context": pf._DESTRUCTIVE_CONTEXT,
        "execution_instructions": safety_context,
    }
    assert all("execution_instructions" not in state for state in jev_states[1:])
    assert len(classifier_prompts) == 1
    assert "Use Claude" not in classifier_prompts[0]
    assert "synthetic archive" not in classifier_prompts[0]


# ---------------------------------------------------------------------------
# Preset class — Jev fan-out judgment (jev_task_routing.judge_task)
# ---------------------------------------------------------------------------

def _preset_judgment(choice: str, confidence: float, software_noul: float = 0.1):
    """`software_noul` defaults to 0.1 (not software) so tests that aren't
    specifically about the software_work guard exercise only the
    confidence axis."""
    from api.services.jev_task_routing import JevAnswer, TaskJudgment

    return TaskJudgment(
        location=None, difficulty=None,
        preset_class=JevAnswer(choice=choice, confidence=confidence),
        software_work=JevAnswer(noul=software_noul),
    )


@pytest.mark.unit
def test_preset_class_tag_wins_over_jev_judgment(monkeypatch):
    """An explicit class tag always wins over the Jev judgment — even a
    high-confidence one naming a different class. Mutation check: removing
    the tag-override precedence in `_apply_preset_class` fails this test."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("financial", 0.95),
    )
    result = pf.run_preflight("any task", tags=["agent", "crm"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_preset_class_tag_wins_over_a_pre_populated_result_value():
    """An explicit class tag beats even an already-populated
    `result.preset_class` (an LLM/caller pre-set value; no classifier
    emits one today, but the precedence must hold regardless) — the tag
    check runs before that early-return, not after it."""
    result = pf.PreflightResult(
        budget=pf._defaults(), routing=pf.ROUTE_CLAUDE, routing_reason="test",
        expected_output="text", preset_class="financial",
    )
    result = pf._apply_preset_class(result, tags=["agent", "crm"], title="any task")
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_preset_class_jev_accepts_confident_non_software_class(monkeypatch):
    """No tag; a Jev judgment at or above the 0.7 confidence floor, on a
    task the judgment itself doesn't flag as software work, sets
    `preset_class` to its choice."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("research", 0.75, software_noul=0.1),
    )
    result = pf.run_preflight("dig into last quarter's numbers", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "research"


@pytest.mark.unit
def test_preset_class_jev_confidence_boundary_at_exactly_point_seven(monkeypatch):
    """Exactly 0.7 must be accepted (>=, not >)."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("crm", 0.7, software_noul=0.1),
    )
    result = pf.run_preflight("any task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_preset_class_jev_low_confidence_leaves_unset(monkeypatch):
    """Below the 0.7 floor, `preset_class` stays unset (today's default),
    not the Jev choice."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("crm", 0.69, software_noul=0.1),
    )
    result = pf.run_preflight("any task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class is None


@pytest.mark.unit
def test_preset_class_software_work_guard_blocks_even_high_confidence(monkeypatch):
    """A wrong narrow class is worse than the unfiltered default, since it
    removes tools from the session — so a task the judgment itself flags
    as likely software work (noul >= 0.5) never gets narrowed, no matter
    how confident the class choice is. Mutation check: dropping the
    software_work guard from `_apply_preset_class` fails this test."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("crm", 0.9, software_noul=0.9),
    )
    result = pf.run_preflight(
        "Fix the granola watcher so finance meetings stop landing in Meetings",
        tags=["agent"], caller=_stub_caller(routing="claude"),
    )
    assert result.preset_class is None


@pytest.mark.unit
def test_preset_class_software_work_guard_allows_non_software(monkeypatch):
    """High confidence and a low software_work probability together set
    the class."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("crm", 0.9, software_noul=0.1),
    )
    result = pf.run_preflight("any task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class == "crm"


@pytest.mark.unit
def test_preset_class_software_work_guard_and_low_confidence_both_unset(monkeypatch):
    """Confidence below the floor leaves the class unset even when the
    software_work guard alone would have passed."""
    monkeypatch.setattr(
        "api.services.jev_task_routing.judge_task",
        lambda title: _preset_judgment("crm", 0.65, software_noul=0.1),
    )
    result = pf.run_preflight("any task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class is None


@pytest.mark.unit
def test_preset_class_no_key_leaves_todays_default(monkeypatch):
    """Without a Jev judgment at all (unconfigured or failed call, modeled
    here by `judge_task` returning None), `preset_class` stays unset — the
    worker's own no-filter default, exactly as it is with no explicit
    class tag."""
    monkeypatch.setattr("api.services.jev_task_routing.judge_task", lambda title: None)
    result = pf.run_preflight("any task", tags=["agent"],
                              caller=_stub_caller(routing="claude"))
    assert result.preset_class is None
