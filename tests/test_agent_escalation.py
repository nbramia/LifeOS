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
