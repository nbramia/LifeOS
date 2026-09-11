"""On a journal-persona turn, `ask_stream` skips the engine-handoff directive
and the intent classifier: journal is a filing surface, not an
orchestrator, so a "remind me to…" fragment must reach the journal filing
policy rather than being intercepted by the generic task-vs-reminder
clarification, and a code-action phrasing must not emit `claude_intent` from
here. Every other persona keeps both interceptors unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.routes.chat as chat
from api.services.chat_helpers import ActionIntent
from config.settings import settings

pytestmark = pytest.mark.unit

_FRAGMENT = "call the synthetic vendor about the invoice"


@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    import api.services.journal_capture as journal_capture_mod
    import config.settings as settings_mod
    from api.routes import vault as vault_route_mod

    root = tmp_path / "vault"
    root.mkdir()
    for obj in (settings_mod.settings, journal_capture_mod.settings, vault_route_mod.settings):
        monkeypatch.setattr(obj, "vault_path", root)
    return root


@pytest.fixture
def journal_persona(tmp_path, monkeypatch) -> str:
    reg = tmp_path / "bots.json"
    reg.write_text(json.dumps([{
        "name": "journal",
        "token_env": "TG_JOURNAL_TEST",
        "persona_file": "config/personas/journal.md",
    }]))
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_JOURNAL_TEST", "tok")
    preamble = settings.resolve_persona("journal")
    assert preamble, "journal persona did not resolve"
    return preamble


@pytest.fixture
def fake_agent_loop(monkeypatch):
    import api.services.agent_loop as agent_loop_mod

    async def fake_loop(**kwargs):
        yield {"type": "text", "content": "Logged."}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="fake", tool_calls_log=[], full_text="Logged.",
        )}

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)


async def _run_turn(**request_kwargs) -> list[dict]:
    response = await chat.ask_stream(chat.AskStreamRequest(**request_kwargs))
    events: list[dict] = []
    async for raw in response.body_iterator:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


class _CountingClassifier:
    def __init__(self, intent: ActionIntent | None):
        self.calls = 0
        self._intent = intent

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        return self._intent


class TestIntentClassifierSkippedForJournal:
    async def test_journal_turn_never_calls_the_classifier(
        self, vault, journal_persona, fake_agent_loop, monkeypatch,
    ):
        classifier = _CountingClassifier(ActionIntent(category="ambiguous_task_reminder"))
        monkeypatch.setattr(chat, "classify_action_intent", classifier)

        events = await _run_turn(question=_FRAGMENT, persona_id="journal")
        full_reply = "".join(e.get("content", "") for e in events if e.get("type") == "content")

        assert classifier.calls == 0
        # The generic clarification never fires -- the turn reaches the
        # (faked) agent loop instead.
        assert "to-do" not in full_reply and "timed reminder" not in full_reply
        assert full_reply == "Logged."

    async def test_non_journal_turn_still_calls_the_classifier(self, tmp_path, monkeypatch):
        classifier = _CountingClassifier(ActionIntent(category="ambiguous_task_reminder"))
        monkeypatch.setattr(chat, "classify_action_intent", classifier)

        events = await _run_turn(question=_FRAGMENT)
        full_reply = "".join(e.get("content", "") for e in events if e.get("type") == "content")

        assert classifier.calls == 1
        assert "to-do" in full_reply and "timed reminder" in full_reply


class TestEngineDirectiveSkippedForJournal:
    async def test_journal_turn_does_not_hand_off_to_an_engine(
        self, vault, journal_persona, fake_agent_loop, monkeypatch,
    ):
        import api.services.agent_loop as agent_loop_mod

        calls = {"n": 0}
        original = agent_loop_mod.parse_engine_directive

        def counting_directive(question: str):
            calls["n"] += 1
            return original(question)

        monkeypatch.setattr(agent_loop_mod, "parse_engine_directive", counting_directive)

        events = await _run_turn(question="use codex to fix the widget", persona_id="journal")

        assert calls["n"] == 0
        assert not any(e.get("type") == "claude_intent" for e in events)

    async def test_non_journal_turn_still_hands_off_to_an_engine(self, tmp_path, monkeypatch):
        async def fake_classify(*_a, **_k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        events = await _run_turn(question="use codex to fix the widget")

        assert any(e.get("type") == "claude_intent" and e.get("engine") == "codex" for e in events)
