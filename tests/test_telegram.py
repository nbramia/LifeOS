"""
Tests for the Telegram service.

Tests message sending, splitting, markdown cleaning, bot listener,
update offset persistence, and message deduplication.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.unit


class TestMessageSplitting:
    """Tests for message splitting logic."""

    def test_short_message_no_split(self):
        from api.services.telegram import _split_message
        parts = _split_message("Hello world")
        assert len(parts) == 1
        assert parts[0] == "Hello world"

    def test_long_message_splits_at_newline(self):
        from api.services.telegram import _split_message
        # Create a message longer than 4096 chars with newlines
        lines = [f"Line {i}: {'x' * 50}" for i in range(100)]
        text = "\n".join(lines)
        assert len(text) > 4096

        parts = _split_message(text)
        assert len(parts) > 1
        for part in parts:
            assert len(part) <= 4096

    def test_long_message_without_newlines(self):
        from api.services.telegram import _split_message
        text = "x" * 8000
        parts = _split_message(text)
        assert len(parts) == 2
        assert len(parts[0]) == 4096
        assert len(parts[1]) == 8000 - 4096

    def test_exact_limit_no_split(self):
        from api.services.telegram import _split_message
        text = "x" * 4096
        parts = _split_message(text)
        assert len(parts) == 1


class TestMarkdownCleaning:
    """Tests for Telegram markdown cleaning."""

    def test_headers_to_bold(self):
        from api.services.telegram import _clean_markdown_for_telegram
        assert _clean_markdown_for_telegram("## Header") == "*Header*"
        assert _clean_markdown_for_telegram("### Sub Header") == "*Sub Header*"

    def test_removes_horizontal_rules(self):
        from api.services.telegram import _clean_markdown_for_telegram
        text = "before\n---\nafter"
        result = _clean_markdown_for_telegram(text)
        assert "---" not in result
        assert "before" in result
        assert "after" in result

    def test_removes_image_syntax(self):
        from api.services.telegram import _clean_markdown_for_telegram
        text = "Check ![alt text](http://example.com/img.png) here"
        result = _clean_markdown_for_telegram(text)
        assert "![" not in result
        assert "alt text" in result


class TestSendMessage:
    """Tests for message sending."""

    @patch("api.services.telegram.settings")
    @patch("api.services.telegram.httpx.post")
    def test_send_message_success(self, mock_post, mock_settings):
        from api.services.telegram import send_message

        mock_settings.telegram_enabled = True
        mock_settings.telegram_bot_token = "test-token"
        mock_settings.telegram_chat_id = "12345"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = send_message("Hello")
        assert result is True
        mock_post.assert_called_once()

    @patch("api.services.telegram.settings")
    def test_send_message_not_configured(self, mock_settings):
        from api.services.telegram import send_message

        mock_settings.telegram_enabled = False
        result = send_message("Hello")
        assert result is False

    @patch("api.services.telegram.settings")
    @patch("api.services.telegram.httpx.post")
    def test_send_message_markdown_fallback(self, mock_post, mock_settings):
        from api.services.telegram import send_message

        mock_settings.telegram_enabled = True
        mock_settings.telegram_bot_token = "test-token"
        mock_settings.telegram_chat_id = "12345"

        # First call fails (Markdown), second succeeds (plain text)
        fail_response = MagicMock()
        fail_response.status_code = 400
        fail_response.text = "Bad Request"

        ok_response = MagicMock()
        ok_response.status_code = 200

        mock_post.side_effect = [fail_response, ok_response]

        result = send_message("Hello *world*")
        assert result is True
        assert mock_post.call_count == 2


class TestChatViaApi:
    """Tests for the internal chat client."""

    @pytest.mark.asyncio
    @patch("api.services.telegram.settings")
    async def test_chat_via_api_collects_content(self, mock_settings):
        from api.services.telegram import chat_via_api

        mock_settings.port = 8000

        # Mock the SSE stream
        events = [
            'data: {"type": "conversation_id", "conversation_id": "conv-123"}',
            'data: {"type": "content", "content": "Hello "}',
            'data: {"type": "content", "content": "world"}',
            'data: {"type": "done"}',
        ]

        class MockStream:
            status_code = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def aiter_lines(self):
                for event in events:
                    yield event

        class MockAsyncClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, method, url, **kwargs):
                return MockStream()

        with patch("api.services.telegram.httpx.AsyncClient", MockAsyncClient):
            result = await chat_via_api("test question")
            assert result["answer"] == "Hello world"
            assert result["conversation_id"] == "conv-123"


# =============================================================================
# Update Offset Persistence Tests
# =============================================================================


class TestUpdateOffsetPersistence:
    """Tests for persisting _last_update_id across restarts."""

    def test_load_from_state_file(self, tmp_path):
        """Loads persisted update_id from state file on init."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"
        state_file.write_text(json.dumps({"last_update_id": 42}))

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()
            assert listener._last_update_id == 42

    def test_default_zero_when_no_file(self, tmp_path):
        """Defaults to 0 when state file doesn't exist."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "nonexistent.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()
            assert listener._last_update_id == 0

    def test_default_zero_on_corrupt_file(self, tmp_path):
        """Defaults to 0 when state file is corrupt."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"
        state_file.write_text("not valid json{{{")

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()
            assert listener._last_update_id == 0

    def test_save_persists_to_file(self, tmp_path):
        """_save_last_update_id writes to state file."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()
            listener._last_update_id = 99
            listener._save_last_update_id()

        data = json.loads(state_file.read_text())
        assert data["last_update_id"] == 99

    def test_save_creates_parent_directories(self, tmp_path):
        """_save_last_update_id creates missing parent dirs."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "subdir" / "telegram_state.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()
            listener._last_update_id = 50
            listener._save_last_update_id()

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["last_update_id"] == 50

    def test_roundtrip_persistence(self, tmp_path):
        """Update ID survives save → new instance → load cycle."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            # First instance saves
            listener1 = TelegramBotListener()
            listener1._last_update_id = 12345
            listener1._save_last_update_id()

            # Second instance loads
            listener2 = TelegramBotListener()
            assert listener2._last_update_id == 12345


# =============================================================================
# Message Deduplication Tests
# =============================================================================


class TestMessageDedup:
    """Tests for message-level deduplication safety net."""

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self, tmp_path):
        """Same message_id is not processed twice."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()

        # First call — should process (but we mock the rest to avoid side effects)
        update = {"message": {"message_id": 1, "text": "hi", "chat": {"id": "123"}}}
        with patch.object(listener, "_check_agent_approval", return_value=False), \
             patch.object(listener, "_check_agent_clarification", return_value=False), \
             patch.object(listener, "_check_code_followup", return_value=False), \
             patch("api.services.telegram.settings") as mock_settings, \
             patch("api.services.telegram.send_typing_indicator"), \
             patch("api.services.telegram.chat_via_api", new_callable=AsyncMock) as mock_chat, \
             patch("api.services.telegram.send_message_async", new_callable=AsyncMock):
            mock_settings.telegram_chat_id = "123"
            mock_chat.return_value = {"answer": "response", "conversation_id": "c1", "code_intent": False}

            await listener._handle_update(update)
            assert mock_chat.call_count == 1

            # Second call with same message_id — should be skipped
            await listener._handle_update(update)
            assert mock_chat.call_count == 1  # Not called again

    def test_dedup_window_rolls(self, tmp_path):
        """Dedup window evicts oldest entries when full."""
        from api.services.telegram import TelegramBotListener

        state_file = tmp_path / "telegram_state.json"

        with patch.object(TelegramBotListener, "_STATE_FILE", state_file):
            listener = TelegramBotListener()

        # Fill the dedup window
        for i in range(TelegramBotListener._DEDUP_WINDOW):
            listener._processed_ids.append(i)

        assert 0 in listener._processed_ids
        # Add one more — oldest (0) should be evicted
        listener._processed_ids.append(TelegramBotListener._DEDUP_WINDOW)
        assert 0 not in listener._processed_ids
        assert TelegramBotListener._DEDUP_WINDOW in listener._processed_ids
