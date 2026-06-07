"""
Tests for multi-bot Telegram support (issue #316).

Covers the registry loader (settings.telegram_bots), per-bot listener wiring,
token routing via the active-bot ContextVar, and persona forwarding through the
chat client. Specialized bots route to the shared orchestrator with a domain
persona while keeping full tool access.
"""
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Registry loader (settings.telegram_bots)
# ---------------------------------------------------------------------------

class TestRegistryLoader:
    def test_missing_registry_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "nope.json")
        from config.settings import settings
        assert settings.telegram_bots == []

    def test_bot_with_unset_token_is_skipped(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([{"name": "fitness", "token_env": "TG_FIT_TOKEN"}]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.delenv("TG_FIT_TOKEN", raising=False)
        from config.settings import settings
        assert settings.telegram_bots == []

    def test_bot_loaded_with_token_persona_and_chat(self, tmp_path, monkeypatch):
        persona_file = tmp_path / "fitness.md"
        persona_file.write_text("FIT PERSONA")
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([{
            "name": "fitness",
            "token_env": "TG_FIT_TOKEN",
            "chat_id_env": "TG_FIT_CHAT",
            "persona_file": str(persona_file),
        }]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT_TOKEN", "fit-token-123")
        monkeypatch.setenv("TG_FIT_CHAT", "555")
        from config.settings import settings
        bots = settings.telegram_bots
        assert len(bots) == 1
        bot = bots[0]
        assert (bot.name, bot.token, bot.chat_id, bot.persona) == (
            "fitness", "fit-token-123", "555", "FIT PERSONA"
        )

    def test_chat_id_defaults_to_primary(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([{"name": "fitness", "token_env": "TG_FIT_TOKEN"}]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT_TOKEN", "x")
        from config.settings import settings
        assert settings.telegram_bots[0].chat_id == settings.telegram_chat_id

    def test_reserved_and_invalid_names_skipped(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "primary", "token_env": "TG_A"},      # reserved
            {"name": "bad name!", "token_env": "TG_B"},    # invalid chars
            {"name": "fitness", "token_env": "TG_C"},      # ok
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_A", "a")
        monkeypatch.setenv("TG_B", "b")
        monkeypatch.setenv("TG_C", "c")
        from config.settings import settings
        names = [b.name for b in settings.telegram_bots]
        assert names == ["fitness"]

    def test_duplicate_names_deduped(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "fitness", "token_env": "TG_C"},
            {"name": "fitness", "token_env": "TG_C"},
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_C", "c")
        from config.settings import settings
        assert len(settings.telegram_bots) == 1


# ---------------------------------------------------------------------------
# Token routing via the active-bot ContextVar
# ---------------------------------------------------------------------------

class TestTokenRouting:
    def test_active_bot_token_used(self):
        from api.services.telegram import _telegram_url, _active_bot_token
        tok = _active_bot_token.set("BOTX")
        try:
            assert "/botBOTX/" in _telegram_url("sendMessage")
        finally:
            _active_bot_token.reset(tok)

    def test_explicit_token_overrides_active(self):
        from api.services.telegram import _telegram_url, _active_bot_token
        tok = _active_bot_token.set("BOTX")
        try:
            assert "/botEXPLICIT/" in _telegram_url("sendMessage", "EXPLICIT")
        finally:
            _active_bot_token.reset(tok)

    def test_defaults_to_primary_when_unset(self):
        import api.services.telegram as tg
        from api.services.telegram import _telegram_url, _active_bot_token
        # No active token set and no explicit token -> primary settings token.
        assert _active_bot_token.get() is None
        assert _telegram_url("getMe") == f"{tg.TELEGRAM_API}/bot{tg.settings.telegram_bot_token}/getMe"


# ---------------------------------------------------------------------------
# Per-bot listener wiring
# ---------------------------------------------------------------------------

class TestListenerWiring:
    def test_primary_uses_legacy_state_file(self, tmp_path):
        from api.services.telegram import TelegramBotListener
        state = tmp_path / "telegram_state.json"
        with patch.object(TelegramBotListener, "_STATE_FILE", state):
            listener = TelegramBotListener()  # None -> primary
            assert listener._is_primary
            assert listener._state_file == state

    def test_named_bot_has_isolated_state_file_and_config(self):
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig
        bot = TelegramBotConfig(name="fitness", token="T", chat_id="C", persona="P")
        listener = TelegramBotListener(bot)
        assert not listener._is_primary
        assert listener._state_file == Path("data/telegram_state_fitness.json")
        assert listener._token == "T"
        assert listener._chat_id == "C"
        assert listener._persona == "P"


# ---------------------------------------------------------------------------
# Persona forwarding + agent-reply gating in _handle_update
# ---------------------------------------------------------------------------

class _DummyTyping:
    """async-context-manager stand-in for TypingIndicator."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestHandleUpdate:
    def _listener(self, name, chat_id, persona=""):
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig
        bot = TelegramBotConfig(name=name, token="TOK", chat_id=chat_id, persona=persona)
        return TelegramBotListener(bot)

    @pytest.mark.asyncio
    async def test_specialized_bot_forwards_persona_and_skips_agent_hooks(self):
        listener = self._listener("fitness", "999", persona="FIT PERSONA")
        update = {"message": {
            "text": "squats 5x5 @185",
            "chat": {"id": 999},
            "message_id": 1,
            "reply_to_message": {"message_id": 42},  # would trigger hooks on primary
        }}
        with patch.object(listener, "_maybe_deposit_agent_answer") as mock_dep, \
             patch.object(listener, "_maybe_handle_claude_code_reply", new_callable=AsyncMock) as mock_claude, \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock), \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"answer": "Logged", "conversation_id": "c1"}
            await listener._handle_update(update)

        # Specialized bots are pure chat: agent/Claude reply hooks never run.
        mock_dep.assert_not_called()
        mock_claude.assert_not_called()
        # Persona forwarded to the orchestrator.
        mock_chat.assert_awaited_once()
        assert mock_chat.call_args.kwargs.get("persona") == "FIT PERSONA"

    @pytest.mark.asyncio
    async def test_primary_bot_still_runs_agent_reply_hook(self):
        listener = self._listener("primary", "999")
        update = {"message": {
            "text": "the answer",
            "chat": {"id": 999},
            "message_id": 2,
            "reply_to_message": {"message_id": 42},
        }}
        with patch.object(listener, "_maybe_handle_claude_code_reply", new_callable=AsyncMock, return_value=False) as mock_claude, \
             patch.object(listener, "_maybe_deposit_agent_answer", return_value=True) as mock_dep, \
             patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)

        # Primary owns the reply threads; deposit short-circuits before chat.
        mock_claude.assert_awaited_once()
        mock_dep.assert_called_once()
        mock_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_chat_ignored_per_bot(self):
        listener = self._listener("fitness", "999", persona="P")
        update = {"message": {"text": "hi", "chat": {"id": 111}, "message_id": 3}}
        with patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat:
            await listener._handle_update(update)
        mock_chat.assert_not_called()


class TestPrimaryOnlyCommands:
    """Agent/Claude-Code/Codex commands belong to the primary bot only."""

    def _listener(self, name, chat_id):
        from api.services.telegram import TelegramBotListener
        from config.settings import TelegramBotConfig
        return TelegramBotListener(TelegramBotConfig(name=name, token="TOK", chat_id=chat_id))

    @pytest.mark.asyncio
    async def test_specialized_bot_redirects_agent_command(self):
        listener = self._listener("fitness", "999")
        with patch("api.services.telegram.send_message_async", new_callable=AsyncMock) as mock_send, \
             patch.object(listener, "_handle_agent_spawn", new_callable=AsyncMock) as mock_spawn:
            handled = await listener._handle_command("/agent do a thing", "999")
        assert handled is True
        mock_spawn.assert_not_called()
        mock_send.assert_awaited_once()
        assert "main LifeOS bot" in mock_send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_primary_bot_allows_agent_command(self):
        listener = self._listener("primary", "999")
        with patch.object(listener, "_handle_agent_spawn", new_callable=AsyncMock) as mock_spawn:
            handled = await listener._handle_command("/agent do a thing", "999")
        assert handled is True
        mock_spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_specialized_bot_redirects_on_engine_handoff(self):
        listener = self._listener("fitness", "999")
        update = {"message": {"text": "use claude code to fix x", "chat": {"id": 999}, "message_id": 5}}
        with patch("api.services.telegram.send_typing_indicator", new_callable=AsyncMock), \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock) as mock_send, \
             patch("api.services.telegram.TypingIndicator", _DummyTyping), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat, \
             patch.object(listener, "_handle_claude_command", new_callable=AsyncMock) as mock_claude:
            mock_chat.return_value = {
                "answer": "", "conversation_id": "c1",
                "claude_intent": True, "engine": "claude_code", "task": "fix x",
            }
            await listener._handle_update(update)
        # No background session spawned; a redirect was sent instead.
        mock_claude.assert_not_called()
        assert any(
            c.args and "main" in c.args[0].lower() for c in mock_send.call_args_list
        )


class TestPersonaValidation:
    def test_persona_within_cap_accepted(self):
        from api.routes.chat import AskStreamRequest
        req = AskStreamRequest(question="hi", persona="x" * 8000)
        assert len(req.persona) == 8000

    def test_persona_over_cap_rejected(self):
        from api.routes.chat import AskStreamRequest
        with pytest.raises(ValueError):
            AskStreamRequest(question="hi", persona="x" * 8001)


# ---------------------------------------------------------------------------
# Persona threads through chat_via_api into the request body
# ---------------------------------------------------------------------------

class TestChatViaApiPersona:
    @pytest.mark.asyncio
    @patch("api.services.telegram.settings")
    async def test_persona_added_to_request_body(self, mock_settings):
        from api.services.telegram import chat_via_api
        mock_settings.port = 8000
        captured = {}

        class MockStream:
            status_code = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def aiter_lines(self):
                for ev in ['data: {"type": "content", "content": "ok"}', 'data: {"type": "done"}']:
                    yield ev

        class MockAsyncClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, method, url, **kwargs):
                captured["json"] = kwargs.get("json")
                return MockStream()

        with patch("api.services.telegram.httpx.AsyncClient", MockAsyncClient):
            await chat_via_api("hi", persona="FIT PERSONA")
        assert captured["json"]["persona"] == "FIT PERSONA"

    @pytest.mark.asyncio
    @patch("api.services.telegram.settings")
    async def test_no_persona_key_when_absent(self, mock_settings):
        from api.services.telegram import chat_via_api
        mock_settings.port = 8000
        captured = {}

        class MockStream:
            status_code = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def aiter_lines(self):
                for ev in ['data: {"type": "done"}']:
                    yield ev

        class MockAsyncClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, method, url, **kwargs):
                captured["json"] = kwargs.get("json")
                return MockStream()

        with patch("api.services.telegram.httpx.AsyncClient", MockAsyncClient):
            await chat_via_api("hi")
        assert "persona" not in captured["json"]
