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
    resolve_model_alias,
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
    """Escalation still fires — it just no longer climbs onto the API (#584).

    `escalation_model` now only says "escalation is configured"; the rung it
    lands on is the first non-API engine, not the model named here.
    """
    history = [FakeMessage("assistant", _REFUSAL)]
    model, escalated = resolve_orchestrator_model(
        history, _PUSHBACK, base_model="claude-haiku-4-5", escalation_model="claude-opus-4-8"
    )
    assert (model, escalated) == ("claude_code", True)


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
    ("escalate to opus", "claude-opus-5"),
    ("use opus please", "claude-opus-5"),
    ("with claude opus", "claude-opus-5"),
    ("retry on opus", "claude-opus-5"),
    ("use sonnet", "claude-sonnet-5"),
    ("switch to sonnet", "claude-sonnet-5"),
    ("use haiku for this", "claude-haiku-4-5"),
])
def test_named_tier_directive_selects_that_model(question, expected):
    # No history / no refusal — the directive alone drives the choice. Base is a
    # model different from the target so escalation is observable.
    base = "claude-haiku-4-5" if expected != "claude-haiku-4-5" else "claude-sonnet-5"
    model, escalated = resolve_orchestrator_model([], question, base_model=base, escalation_model="")
    assert (model, escalated) == (expected, True)


def test_named_tier_works_without_escalation_model_configured():
    """An explicit 'use opus' must work even when auto-escalation is unconfigured."""
    model, escalated = resolve_orchestrator_model(
        [], "use opus", base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-opus-5", True)


@pytest.mark.parametrize("question", [
    "use a smarter model",
    "try a stronger model",
    "use a more capable model",
    "escalate to a stronger model",
    "escalate the model",
])
def test_generic_directive_falls_back_to_configured_model(question):
    model, escalated = resolve_orchestrator_model(
        [], question, base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5"
    )
    assert (model, escalated) == ("claude-sonnet-5", True)


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
        [], question, base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5"
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_contrastive_directive_still_honors_named_model():
    """'instead of'/'rather than' contrast options — the named model is desired."""
    model, escalated = resolve_orchestrator_model(
        [], "use sonnet instead of opus", base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-sonnet-5", True)


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
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("claude-opus-5", True)


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
# multi-tier escalation ladder (#305 part c)
# ---------------------------------------------------------------------------

def _refusal_history(n):
    """The history the store holds when the user is about to send their n-th
    pushback: n refusals with (n-1) interleaved pushbacks, ending in a refusal.
    The n-th pushback itself is the (separate) current `question`."""
    h = []
    for i in range(n):
        if i > 0:
            h.append(FakeMessage("user", _PUSHBACK))
        h.append(FakeMessage("assistant", _REFUSAL))
    return h


@pytest.mark.parametrize("n_refusals, expected", [
    (1, "claude_code"),   # rung 0 — the strongest subscription engine
    (2, "codex"),         # rung 1 — the other subscription engine
    (3, "codex"),         # capped at the top rung
])
def test_ladder_climbs_with_each_refusal(n_refusals, expected):
    model, escalated = resolve_orchestrator_model(
        _refusal_history(n_refusals), _PUSHBACK,
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == (expected, True)


def test_ladder_disabled_when_escalation_model_unset():
    model, escalated = resolve_orchestrator_model(
        _refusal_history(3), _PUSHBACK, base_model="claude-haiku-4-5", escalation_model=""
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_user_directive_overrides_ladder_rung():
    # Even three deep into the ladder, an explicit "use sonnet" wins.
    model, escalated = resolve_orchestrator_model(
        _refusal_history(3), "use sonnet",
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("claude-sonnet-5", True)


def test_escalation_cycles_breaks_on_normal_exchange():
    from api.services.agent_loop import _count_escalation_cycles
    # An earlier refusal the user never pushed back on (a normal question follows)
    # must NOT inflate the count — the chain breaks at that normal exchange.
    history = [
        FakeMessage("assistant", _REFUSAL),               # earlier topic refusal
        FakeMessage("user", "ok, different question"),    # NOT a pushback → breaks chain
        FakeMessage("assistant", _REFUSAL),               # fresh refusal
    ]
    assert _count_escalation_cycles(history) == 0  # only the fresh refusal, no prior cycle


def test_escalation_cycles_counts_pushback_chain():
    from api.services.agent_loop import _count_escalation_cycles
    # R, P, R, P, R  → two completed cycles (two prior pushbacks).
    assert _count_escalation_cycles(_refusal_history(3)) == 2


def test_stale_refusals_do_not_advance_the_rung():
    """Regression (#309 review): refusals on an earlier topic the user never
    pushed back on must not catapult the first fresh pushback up the ladder.

    (Formerly "...do_not_jump_to_engine_rung" — since #584 every rung is an
    engine, so the invariant is about the rung *index*, not its kind.)"""
    history = [
        FakeMessage("user", "question one"),
        FakeMessage("assistant", _REFUSAL),
        FakeMessage("user", "question two"),    # moved on — no pushback
        FakeMessage("assistant", _REFUSAL),     # fresh refusal, user about to push back
    ]
    model, escalated = resolve_orchestrator_model(
        history, _PUSHBACK, base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5"
    )
    assert (model, escalated) == ("claude_code", True)  # rung 0, not rung 1


def test_engine_handoff_recovers_original_request():
    from api.services.agent_loop import _original_request
    history = [
        FakeMessage("user", "add the world cup games to my calendar"),
        FakeMessage("assistant", _REFUSAL),
        FakeMessage("user", _PUSHBACK),
        FakeMessage("assistant", _REFUSAL),
    ]
    assert _original_request(history, "fallback") == "add the world cup games to my calendar"


def test_base_model_filtered_from_ladder(monkeypatch):
    # Explicit 3-rung ladder with base=opus mid-ladder; filtering opus keeps the
    # climb going instead of stalling on the rung that equals base.
    monkeypatch.setattr(
        "api.services.agent_loop.settings.agent_escalation_ladder",
        "claude-sonnet-5,claude-opus-4-8,claude_code", raising=False,
    )
    model, escalated = resolve_orchestrator_model(
        _refusal_history(2), _PUSHBACK,
        base_model="claude-opus-4-8", escalation_model="claude-sonnet-5",
    )
    # ladder after filtering opus = [sonnet, claude_code]; cycles=1 → rung 1.
    assert (model, escalated) == ("claude_code", True)


def test_explicit_ladder_setting_overrides_default(monkeypatch):
    """A configured ladder is honored, order and all."""
    monkeypatch.setattr(
        "api.services.agent_loop.settings.agent_escalation_ladder",
        "codex,claude_code", raising=False,
    )
    model, escalated = resolve_orchestrator_model(
        _refusal_history(1), _PUSHBACK,
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("codex", True)   # the setting's order, not the default's


def test_api_rungs_are_filtered_out_of_a_configured_ladder(monkeypatch):
    """An all-API ladder leaves nothing to climb, so the turn does not escalate.

    The operator can still reach these models by asking ("escalate to opus") —
    what's gone is LifeOS deciding to spend API credits by itself (#584).
    """
    monkeypatch.setattr(
        "api.services.agent_loop.settings.agent_escalation_ladder",
        "claude-sonnet-5,claude-opus-4-8", raising=False,
    )
    model, escalated = resolve_orchestrator_model(
        _refusal_history(3), _PUSHBACK,
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("claude-haiku-4-5", False)


def test_api_rungs_are_filtered_but_engine_rungs_survive(monkeypatch):
    """The filter is surgical: a mixed ladder keeps its non-API rungs."""
    monkeypatch.setattr(
        "api.services.agent_loop.settings.agent_escalation_ladder",
        "claude-opus-4-8,claude_code", raising=False,
    )
    model, escalated = resolve_orchestrator_model(
        _refusal_history(1), _PUSHBACK,
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("claude_code", True)


def test_local_is_a_legal_rung(monkeypatch):
    """Gemma is an available escalation target — it costs nothing to run."""
    monkeypatch.setattr(
        "api.services.agent_loop.settings.agent_escalation_ladder",
        "local,claude_code", raising=False,
    )
    model, escalated = resolve_orchestrator_model(
        _refusal_history(1), _PUSHBACK,
        base_model="claude-haiku-4-5", escalation_model="claude-sonnet-5",
    )
    assert (model, escalated) == ("local", True)


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


# ---------------------------------------------------------------------------
# Model picker: resolve_model_alias + per-turn local ("Gemma")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("haiku", "claude-haiku-4-5"),
    ("sonnet", "claude-sonnet-5"),
    ("opus", "claude-opus-5"),
    ("Opus", "claude-opus-5"),              # case-insensitive
    ("claude-opus-4-8", "claude-opus-4-8"),  # a full id passes through unchanged, even a superseded one
    ("", ""),
])
def test_resolve_model_alias(name, expected):
    assert resolve_model_alias(name) == expected


def test_force_local_builds_local_client_on_anthropic_backend(monkeypatch):
    # The "Gemma" picker option runs a turn on the local llama-server even though
    # the global backend is Anthropic.
    monkeypatch.setattr("api.services.agent_loop.settings.llm_backend", "anthropic", raising=False)
    client = _select_client("", force_local=True)
    from api.services.llm_client import LocalLLMClient
    assert isinstance(client, LocalLLMClient)


def test_force_local_reuses_singleton_on_local_backend(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("api.services.agent_loop.settings.llm_backend", "local", raising=False)
    monkeypatch.setattr("api.services.agent_loop.get_local_llm", lambda: sentinel)
    # Already local — reuse the singleton rather than build another client.
    assert _select_client("", force_local=True) is sentinel
