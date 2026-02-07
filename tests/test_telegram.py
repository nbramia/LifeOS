"""
Tests for the Telegram service.

Tests message sending, splitting, markdown cleaning, and bot listener.
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
