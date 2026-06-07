"""
Tests for outbound Telegram bot routing (issue #318).

Verifies that send functions, the adhoc endpoint, the MCP tool schema,
and the scheduler all route proactive sends from the correct bot account
when a bot name is supplied, and fall back to the primary bot otherwise.
"""
from unittest.mock import patch, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _token_for_bot resolution
# ---------------------------------------------------------------------------

class TestTokenForBot:
    def _bots(self):
        from config.settings import TelegramBotConfig
        return [
            TelegramBotConfig(name="fitness", token="FIT-TOK", chat_id="1"),
            TelegramBotConfig(name="therapy", token="THER-TOK", chat_id="1"),
        ]

    @patch("api.services.telegram.settings")
    def test_none_returns_none(self, ms):
        ms.telegram_bot_token = "PRIMARY"
        ms.telegram_bots = self._bots()
        from api.services.telegram import _token_for_bot
        assert _token_for_bot(None) is None

    @patch("api.services.telegram.settings")
    def test_empty_string_returns_none(self, ms):
        ms.telegram_bot_token = "PRIMARY"
        ms.telegram_bots = self._bots()
        from api.services.telegram import _token_for_bot
        assert _token_for_bot("") is None

    @patch("api.services.telegram.settings")
    def test_primary_returns_primary_token(self, ms):
        ms.telegram_bot_token = "PRIMARY-TOK"
        ms.telegram_bots = self._bots()
        from api.services.telegram import _token_for_bot
        assert _token_for_bot("primary") == "PRIMARY-TOK"

    @patch("api.services.telegram.settings")
    def test_named_bots_resolved(self, ms):
        ms.telegram_bot_token = "PRIMARY"
        ms.telegram_bots = self._bots()
        from api.services.telegram import _token_for_bot
        assert _token_for_bot("fitness") == "FIT-TOK"
        assert _token_for_bot("therapy") == "THER-TOK"

    @patch("api.services.telegram.settings")
    def test_unknown_name_returns_none_with_warning(self, ms, caplog):
        import logging
        ms.telegram_bot_token = "PRIMARY"
        ms.telegram_bots = self._bots()
        from api.services.telegram import _token_for_bot
        with caplog.at_level(logging.WARNING, logger="api.services.telegram"):
            result = _token_for_bot("unknown-bot")
        assert result is None
        assert "not found in registry" in caplog.text


# ---------------------------------------------------------------------------
# send_message uses resolved token
# ---------------------------------------------------------------------------

class TestSendMessageBotParam:
    @patch("api.services.telegram.httpx.post")
    @patch("api.services.telegram.settings")
    def test_send_message_uses_named_bot_token(self, mock_settings, mock_post):
        from api.services.telegram import send_message
        from config.settings import TelegramBotConfig

        mock_settings.telegram_enabled = True
        mock_settings.telegram_chat_id = "123"
        mock_settings.telegram_bot_token = "PRIMARY"
        mock_settings.telegram_bots = [
            TelegramBotConfig(name="fitness", token="FIT-TOK", chat_id="123")
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_message("Workout logged", bot="fitness")

        url_used = mock_post.call_args[0][0]
        assert "FIT-TOK" in url_used
        assert "PRIMARY" not in url_used

    @patch("api.services.telegram.httpx.post")
    @patch("api.services.telegram.settings")
    def test_send_message_no_bot_uses_primary(self, mock_settings, mock_post):
        from api.services.telegram import send_message

        mock_settings.telegram_enabled = True
        mock_settings.telegram_chat_id = "123"
        mock_settings.telegram_bot_token = "PRIMARY"
        mock_settings.telegram_bots = []

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_message("General alert")

        url_used = mock_post.call_args[0][0]
        assert "PRIMARY" in url_used

    @patch("api.services.telegram.httpx.post")
    @patch("api.services.telegram.settings")
    def test_unknown_bot_falls_back_to_primary(self, mock_settings, mock_post):
        from api.services.telegram import send_message

        mock_settings.telegram_enabled = True
        mock_settings.telegram_chat_id = "123"
        mock_settings.telegram_bot_token = "PRIMARY"
        mock_settings.telegram_bots = []

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_message("Alert", bot="nonexistent")

        url_used = mock_post.call_args[0][0]
        assert "PRIMARY" in url_used


# ---------------------------------------------------------------------------
# send_message_async uses resolved token
# ---------------------------------------------------------------------------

class TestSendMessageAsyncBotParam:
    @pytest.mark.asyncio
    @patch("api.services.telegram.settings")
    async def test_async_send_named_bot_token(self, mock_settings):
        from api.services.telegram import send_message_async
        from config.settings import TelegramBotConfig

        mock_settings.telegram_enabled = True
        mock_settings.telegram_chat_id = "123"
        mock_settings.telegram_bot_token = "PRIMARY"
        mock_settings.telegram_bots = [
            TelegramBotConfig(name="fitness", token="FIT-TOK", chat_id="123")
        ]

        captured_url = []

        class MockResp:
            status_code = 200

        class MockClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def post(self, url, **kwargs):
                captured_url.append(url)
                return MockResp()

        with patch("api.services.telegram.httpx.AsyncClient", MockClient):
            await send_message_async("Logged workout", bot="fitness")

        assert captured_url and "FIT-TOK" in captured_url[0]


# ---------------------------------------------------------------------------
# Adhoc endpoint passes bot through
# ---------------------------------------------------------------------------

class TestAdhocEndpointBotParam:
    def test_bot_field_accepted_on_request(self):
        from api.routes.reminders import SendMessageRequest

        request = SendMessageRequest(text="Feeling better today", bot="therapy")
        assert request.bot == "therapy"

    def test_no_bot_defaults_to_none(self):
        from api.routes.reminders import SendMessageRequest
        req = SendMessageRequest(text="hello")
        assert req.bot is None


# ---------------------------------------------------------------------------
# MCP schema includes bot field
# ---------------------------------------------------------------------------

class TestMcpSchemaBotField:
    def test_telegram_send_schema_has_bot_property(self):
        from mcp_server import LifeOSMCPServer
        server = LifeOSMCPServer.__new__(LifeOSMCPServer)
        schemas = server._fallback_schemas()
        tg_schema = schemas["lifeos_telegram_send"]
        assert "bot" in tg_schema["properties"]
        assert "text" in tg_schema.get("required", [])
        assert "bot" not in tg_schema.get("required", [])


# ---------------------------------------------------------------------------
# ScheduleEntry bot field: parse and format
# ---------------------------------------------------------------------------

class TestScheduleEntryBotField:
    def test_default_bot_is_empty(self):
        from api.services.scheduler_store import ScheduleEntry
        entry = ScheduleEntry(
            id="abc", name="test",
            schedule_type="cron", schedule_value="0 9 * * *",
        )
        assert entry.bot == ""

    def test_format_includes_bot_when_set(self):
        from api.services.scheduler_store import ScheduleEntry, _format_entry_line
        entry = ScheduleEntry(
            id="abc", name="Morning log",
            schedule_type="cron", schedule_value="0 8 * * *",
            bot="fitness",
        )
        line = _format_entry_line(entry)
        assert "[bot:: fitness]" in line

    def test_format_omits_bot_when_empty(self):
        from api.services.scheduler_store import ScheduleEntry, _format_entry_line
        entry = ScheduleEntry(
            id="abc", name="Morning log",
            schedule_type="cron", schedule_value="0 8 * * *",
        )
        line = _format_entry_line(entry)
        assert "bot::" not in line

    def test_parse_reads_bot_field(self):
        from api.services.scheduler_store import _parse_entry_line
        line = "- [ ] Morning log [cron:: 0 8 * * *] [action:: notify] [mtype:: static] [bot:: fitness] <!-- id:abc123 -->"
        entry = _parse_entry_line(line)
        assert entry is not None
        assert entry.bot == "fitness"

    def test_parse_missing_bot_defaults_to_empty(self):
        from api.services.scheduler_store import _parse_entry_line
        line = "- [ ] Morning log [cron:: 0 8 * * *] [action:: notify] [mtype:: static] <!-- id:abc123 -->"
        entry = _parse_entry_line(line)
        assert entry is not None
        assert entry.bot == ""

    def test_from_dict_roundtrip(self):
        from api.services.scheduler_store import ScheduleEntry
        data = {
            "id": "abc", "name": "Workout", "schedule_type": "cron",
            "schedule_value": "0 8 * * *", "bot": "fitness",
        }
        entry = ScheduleEntry.from_dict(data)
        assert entry.bot == "fitness"

    def test_from_dict_no_bot_defaults(self):
        from api.services.scheduler_store import ScheduleEntry
        data = {
            "id": "abc", "name": "Workout", "schedule_type": "cron",
            "schedule_value": "0 8 * * *",
        }
        entry = ScheduleEntry.from_dict(data)
        assert entry.bot == ""
