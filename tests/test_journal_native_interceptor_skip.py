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
from unittest.mock import patch

import pytest

import api.routes.chat as chat
from api.services.chat_helpers import ActionIntent
from api.services.llm_client import LLMUsage
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


@pytest.fixture
def capturing_agent_loop(monkeypatch):
    """Like `fake_agent_loop`, but records the kwargs `chat.ask_stream`
    actually passed to `run_agent_loop` — so a test can assert on them
    directly rather than only on their downstream effect."""
    import api.services.agent_loop as agent_loop_mod

    captured: dict = {}

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        yield {"type": "text", "content": "Logged."}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="fake", tool_calls_log=[], full_text="Logged.",
        )}

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
    return captured


class _ToolCallingClient:
    """A fake model client for driving the REAL `run_agent_loop` (unlike
    `fake_agent_loop`/`capturing_agent_loop`, which replace it entirely).
    Its first call emits a tool call for `tool_name`; its next call answers
    with text so the loop terminates. Records the `tools` kwarg it was
    actually called with, so a test can assert on the advertised tool list
    `run_agent_loop` built from `tools_for_persona`."""

    def __init__(self, tool_name: str):
        self.model = "fake"
        self._tool_name = tool_name
        self.seen_tools = None
        self._calls = 0

    async def astream(self, messages, *, system=None, max_tokens=4096,
                       tools=None, temperature=None, enable_thinking=None,
                       reasoning_effort=None):
        if self.seen_tools is None:
            self.seen_tools = tools
        if self._calls == 0:
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": self._tool_name, "arguments": "{}"},
                }],
            }
            finish_reason = "tool_calls"
        else:
            yield {"type": "text", "content": "done"}
            finish_reason = "end_turn"
        yield {"type": "done", "usage": LLMUsage(), "finish_reason": finish_reason}
        self._calls += 1


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


@pytest.fixture
def real_agent_loop_capture(monkeypatch):
    """Let the REAL `run_agent_loop` execute (unlike `fake_agent_loop`/
    `capturing_agent_loop`, which replace it) and capture its final
    `AgentResult` — including `tool_calls_log` — for assertions."""
    import api.services.agent_loop as agent_loop_mod

    original = agent_loop_mod.run_agent_loop
    captured: dict = {}

    async def wrapper(**kwargs):
        async for event in original(**kwargs):
            if event.get("type") == "result":
                captured["result"] = event["result"]
            yield event

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", wrapper)
    return captured


class TestJournalWiringIsNotUnpinned:
    """Pins the wiring that carries a journal turn's `persona_id` and
    `user_message` from `chat.py`'s call into `run_agent_loop`, and from
    there into `tools_for_persona` and `execute_tool_parallel`: without it,
    the journal bot would be advertised (and able to call) tools like
    `create_calendar_event`. These assert on the real wiring directly rather
    than only on a downstream effect a different bug could also produce."""

    async def test_journal_turn_passes_persona_id_and_user_message(
        self, vault, journal_persona, capturing_agent_loop,
    ):
        await _run_turn(question=_FRAGMENT, persona_id="journal")
        assert capturing_agent_loop["persona_id"] == "journal"
        assert capturing_agent_loop["user_message"] == _FRAGMENT

    async def test_raw_preamble_turn_also_resolves_persona_id_and_user_message(
        self, vault, journal_persona, capturing_agent_loop,
    ):
        # The Telegram bot's real path (cf. test_journal_capture.py's
        # `test_fragment_lands_via_raw_persona_preamble`): no persona_id
        # field, just the resolved preamble text.
        await _run_turn(question=_FRAGMENT, persona=journal_persona)
        assert capturing_agent_loop["persona_id"] == "journal"
        assert capturing_agent_loop["user_message"] == _FRAGMENT

    async def _assert_excludes_and_refuses(self, real_agent_loop_capture, monkeypatch, **request_kwargs):
        import api.services.agent_tools as agent_tools_mod

        async def stub_handler(inp):
            return "STUB: calendar event created — the gate should never let this run"
        monkeypatch.setitem(agent_tools_mod._TOOL_HANDLERS, "create_calendar_event", stub_handler)

        client = _ToolCallingClient("create_calendar_event")
        with patch("api.services.agent_loop._select_client", return_value=client):
            await _run_turn(**request_kwargs)

        tool_names = {t["name"] for t in (client.seen_tools or [])}
        assert "create_calendar_event" not in tool_names

        result = real_agent_loop_capture["result"]
        assert len(result.tool_calls_log) == 1
        call = result.tool_calls_log[0]
        assert call["is_error"] is True
        assert "STUB" not in call["result_preview"]

    async def test_journal_turn_excludes_and_refuses_an_orchestration_tool(
        self, vault, journal_persona, real_agent_loop_capture, monkeypatch,
    ):
        await self._assert_excludes_and_refuses(
            real_agent_loop_capture, monkeypatch,
            question=_FRAGMENT, persona_id="journal",
        )

    async def test_raw_preamble_turn_excludes_and_refuses_an_orchestration_tool(
        self, vault, journal_persona, real_agent_loop_capture, monkeypatch,
    ):
        await self._assert_excludes_and_refuses(
            real_agent_loop_capture, monkeypatch,
            question=_FRAGMENT, persona=journal_persona,
        )
