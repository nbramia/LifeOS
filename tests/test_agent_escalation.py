"""Orchestrator escalation (#303).

When the prior assistant turn refused / claimed something impossible and the
user's new message pushes back, the chat orchestrator retries on a stronger
model. These tests cover the detection (`should_escalate`), the model decision
(`resolve_orchestrator_model`), and the per-turn client selection
(`_select_client`).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.services.agent_loop import (
    parse_engine_directive,
    resolve_orchestrator_model,
    should_escalate,
    _select_client,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeMessage:
    role: str
    content: str


# A realistic refusal (the World Cup failure shape) and a pushback.
_REFUSAL = "I found the tournament dates, but FIFA hasn't released the specific USA match schedule yet."
_PUSHBACK = "yes fifa has released dates, do research"


# ---------------------------------------------------------------------------
# should_escalate
# ---------------------------------------------------------------------------

def test_escalates_on_refusal_then_pushback():
    history = [
        FakeMessage("user", "add the world cup games to my calendar"),
        FakeMessage("assistant", _REFUSAL),
    ]
    assert should_escalate(history, _PUSHBACK) is True


def test_no_escalation_when_prior_turn_did_not_refuse():
    history = [FakeMessage("assistant", "Done — I added all three games to your calendar.")]
    assert should_escalate(history, _PUSHBACK) is False


def test_no_escalation_without_pushback():
    history = [FakeMessage("assistant", _REFUSAL)]
    assert should_escalate(history, "thanks, sounds good") is False


def test_no_escalation_on_empty_history():
    assert should_escalate([], _PUSHBACK) is False
    assert should_escalate(None, _PUSHBACK) is False


def test_only_assistant_refusal_counts_not_user_text():
    # A user message containing refusal-like words must not trigger escalation —
    # only the model's own prior refusal does.
    history = [FakeMessage("user", "isn't it true the schedule hasn't been released?")]
    assert should_escalate(history, _PUSHBACK) is False


def test_uses_most_recent_assistant_turn():
    history = [
        FakeMessage("assistant", _REFUSAL),
        FakeMessage("user", "ok"),
        FakeMessage("assistant", "Here is the answer you wanted."),
    ]
    # Most recent assistant turn didn't refuse → no escalation.
    assert should_escalate(history, _PUSHBACK) is False


def test_works_with_dict_shaped_messages():
    history = [{"role": "assistant", "content": _REFUSAL}]
    assert should_escalate(history, _PUSHBACK) is True


@pytest.mark.parametrize("pushback", [
    "you're wrong, look it up",
    "do research",
    "that's not true, it has been released",
    "I'm telling you it should be possible",
    "try again",
])
def test_various_pushback_phrasings(pushback):
    history = [FakeMessage("assistant", _REFUSAL)]
    assert should_escalate(history, pushback) is True


@pytest.mark.parametrize("correct_negative", [
    "I couldn't find any emails from Sarah in your inbox.",
    "There's no such contact named Bob in your CRM.",
    "That file doesn't exist in your vault.",
    "I can't find a calendar event matching that.",
])
def test_correct_data_lookup_negatives_do_not_escalate(correct_negative):
    """A true 'not in your data' answer must NOT escalate on pushback — a
    stronger model can't find data that isn't there (only wastes the spend)."""
    history = [FakeMessage("assistant", correct_negative)]
    assert should_escalate(history, "you're wrong, look it up again") is False


@pytest.mark.parametrize("giveup", [
    "I can't access live data, so I don't know the current standings.",
    "Based on my knowledge cutoff, I can't provide real-time results.",
    "I don't have access to up-to-date information on that.",
])
def test_giveup_phrases_count_as_refusal(giveup):
    """Knowledge-cutoff / can't-access-live phrasing is a stale-knowledge refusal
    that escalation should also catch."""
    history = [FakeMessage("assistant", giveup)]
    assert should_escalate(history, "do research") is True


# ---------------------------------------------------------------------------
# resolve_orchestrator_model
# ---------------------------------------------------------------------------

def test_resolve_escalates_when_configured_and_triggered():
    history = [FakeMessage("assistant", _REFUSAL)]
    model, escalated = resolve_orchestrator_model(
        history, _PUSHBACK, base_model="claude-haiku-4-5", escalation_model="claude-opus-4-8"
    )
    assert (model, escalated) == ("claude-opus-4-8", True)


def test_resolve_no_escalation_when_model_unset():
    history = [FakeMessage("assistant", _REFUSAL)]
    model, escalated = resolve_orchestrator_model(
        history, _PUSHBACK, base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_resolve_no_escalation_when_model_equals_base():
    history = [FakeMessage("assistant", _REFUSAL)]
    model, escalated = resolve_orchestrator_model(
        history, _PUSHBACK, base_model="claude-haiku-4-5", escalation_model="claude-haiku-4-5"
    )
    assert escalated is False


def test_resolve_no_escalation_when_not_triggered():
    history = [FakeMessage("assistant", "Here are your three games.")]
    model, escalated = resolve_orchestrator_model(
        history, "thanks", base_model="claude-haiku-4-5", escalation_model="claude-opus-4-8"
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


# ---------------------------------------------------------------------------
# user-directed escalation (#305)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("escalate to opus", "claude-opus-4-8"),
    ("use opus please", "claude-opus-4-8"),
    ("with claude opus", "claude-opus-4-8"),
    ("retry on opus", "claude-opus-4-8"),
    ("use sonnet", "claude-sonnet-4-6"),
    ("switch to sonnet", "claude-sonnet-4-6"),
    ("use haiku for this", "claude-haiku-4-5"),
])
def test_named_tier_directive_selects_that_model(question, expected):
    # No history / no refusal — the directive alone drives the choice. Base is a
    # model different from the target so escalation is observable.
    base = "claude-haiku-4-5" if expected != "claude-haiku-4-5" else "claude-sonnet-4-6"
    model, escalated = resolve_orchestrator_model([], question, base_model=base, escalation_model="")
    assert (model, escalated) == (expected, True)


def test_named_tier_works_without_escalation_model_configured():
    """An explicit 'use opus' must work even when auto-escalation is unconfigured."""
    model, escalated = resolve_orchestrator_model(
        [], "use opus", base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-opus-4-8", True)


@pytest.mark.parametrize("question", [
    "use a smarter model",
    "try a stronger model",
    "use a more capable model",
    "escalate to a stronger model",
    "escalate the model",
])
def test_generic_directive_falls_back_to_configured_model(question):
    model, escalated = resolve_orchestrator_model(
        [], question, base_model="claude-haiku-4-5", escalation_model="claude-sonnet-4-6"
    )
    assert (model, escalated) == ("claude-sonnet-4-6", True)


@pytest.mark.parametrize("question", [
    "don't use opus, stick with haiku",
    "I didn't ask you to use opus",
    "why did you use sonnet?",
    "can you use opus or sonnet?",
    "should I use opus for this?",
    "escalate to codex",                   # deferred engine — must NOT become sonnet
    "use codex instead",
    "escalate this ticket to the team",    # 'escalate' about a ticket, not the model
])
def test_negated_question_and_engine_directives_do_not_escalate(question):
    """Negations, meta-questions, unsupported-engine names, and non-model uses of
    'escalate' must not trigger a model swap."""
    model, escalated = resolve_orchestrator_model(
        [], question, base_model="claude-haiku-4-5", escalation_model="claude-sonnet-4-6"
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_contrastive_directive_still_honors_named_model():
    """'instead of'/'rather than' contrast options — the named model is desired."""
    model, escalated = resolve_orchestrator_model(
        [], "use sonnet instead of opus", base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-sonnet-4-6", True)


def test_generic_directive_noops_when_unconfigured():
    model, escalated = resolve_orchestrator_model(
        [], "use a smarter model", base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_directive_to_base_model_is_noop():
    model, escalated = resolve_orchestrator_model(
        [], "use haiku", base_model="claude-haiku-4-5", escalation_model="claude-opus-4-8"
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_directive_beats_auto_heuristic_without_refusal():
    """A named directive escalates even with no refusal+pushback in history."""
    model, escalated = resolve_orchestrator_model(
        [FakeMessage("assistant", "Here are your three games.")],
        "actually, use opus",
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-4-6",
    )
    assert (model, escalated) == ("claude-opus-4-8", True)


@pytest.mark.parametrize("question", [
    "summarize my notes about the opus project",
    "find the sonnet I wrote for Taylor",
    "what's on my calendar today",
])
def test_non_directive_mentions_do_not_escalate(question):
    model, escalated = resolve_orchestrator_model(
        [], question, base_model="claude-haiku-4-5", escalation_model="claude-opus-4-8"
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


# ---------------------------------------------------------------------------
# engine handoff directives (#305 part b)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question, engine, task", [
    # Leading imperative.
    ("use codex to add the world cup games", "codex", "add the world cup games"),
    ("use claude code to refactor the parser", "claude_code", "refactor the parser"),
    ("with codex, summarize the repo", "codex", "summarize the repo"),
    ("hand this to codex: fix the failing test", "codex", "fix the failing test"),
    ("please use codex to deploy", "codex", "deploy"),
    # Trailing imperative ("<task> using codex").
    ("add the games using codex", "codex", "add the games"),
    ("fix the bug with claude code", "claude_code", "fix the bug"),
    ("deploy the app via codex", "codex", "deploy the app"),
    ("run the report with codex", "codex", "run the report"),
])
def test_engine_directive_routes_and_cleans_task(question, engine, task):
    # A command — leading or trailing — routes; the directive phrase is stripped.
    assert parse_engine_directive(question) == (engine, task)


def test_engine_directive_distinguishes_codex_from_claude_code():
    assert parse_engine_directive("use codex")[0] == "codex"
    assert parse_engine_directive("use claude code")[0] == "claude_code"


def test_engine_directive_strips_trailing_model_token():
    # "use codex with opus" must not leave the bare "with opus" as the task.
    engine, task = parse_engine_directive("use codex with opus")
    assert engine == "codex"
    assert task != "with opus"


@pytest.mark.parametrize("question", [
    # Incidental mid-sentence mentions must NOT spawn a worker subprocess.
    "remind me to use codex tomorrow",
    "what time do I usually use codex at night",
    "I want to use codex eventually",
    "summarize my notes about how to use codex effectively",
    # Statements ending in "with codex" — not commands.
    "I have been working with codex",
    "the report should run with codex",
    # Negations / questions / non-engine.
    "don't use codex",
    "why use codex?",
    "can you use claude code?",
    "summarize my codex integration notes",
    "use opus",                                 # a model, not an engine
    "what's on my calendar today",
])
def test_non_engine_directives_return_no_engine(question):
    engine, task = parse_engine_directive(question)
    assert engine == ""
    assert task == question  # task falls back to the original question


def test_engine_directive_with_no_task_falls_back_to_question():
    # The directive is the whole message — nothing to clean to, so the spawned
    # session gets the original text rather than an empty prompt.
    engine, task = parse_engine_directive("use codex")
    assert engine == "codex"
    assert task == "use codex"


# ---------------------------------------------------------------------------
# _select_client
# ---------------------------------------------------------------------------

def test_select_client_uses_escalation_model_on_anthropic_backend(monkeypatch):
    monkeypatch.setattr("api.services.agent_loop.settings.llm_backend", "anthropic", raising=False)
    client = _select_client("claude-opus-4-8")
    from api.services.llm_client import AnthropicLLMClient
    assert isinstance(client, AnthropicLLMClient)
    assert client._model == "claude-opus-4-8"


def test_select_client_falls_back_to_singleton_without_model(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("api.services.agent_loop.get_local_llm", lambda: sentinel)
    assert _select_client("") is sentinel


def test_select_client_ignores_model_on_local_backend(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("api.services.agent_loop.settings.llm_backend", "local", raising=False)
    monkeypatch.setattr("api.services.agent_loop.get_local_llm", lambda: sentinel)
    # Even with a model requested, the local backend uses the singleton.
    assert _select_client("claude-opus-4-8") is sentinel
