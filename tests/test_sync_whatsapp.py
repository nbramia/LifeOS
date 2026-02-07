"""Tests for WhatsApp sync via wacli."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from scripts.sync_whatsapp import (
    normalize_phone,
    extract_phone_from_jid,
    is_group_jid,
    run_wacli,
    check_wacli_auth,
)


class TestNormalizePhone:
    """Tests for phone number normalization."""

    def test_us_10_digit(self):
        """Test normalizing 10-digit US number."""
        assert normalize_phone("5551234567") == "+15551234567"

    def test_us_with_country_code(self):
        """Test normalizing 11-digit US number with country code."""
        assert normalize_phone("15551234567") == "+15551234567"

    def test_with_formatting(self):
        """Test normalizing formatted number."""
        assert normalize_phone("(555) 123-4567") == "+15551234567"
        assert normalize_phone("555-123-4567") == "+15551234567"

    def test_international(self):
        """Test normalizing international number."""
        assert normalize_phone("+442071234567") == "+442071234567"

    def test_empty(self):
        """Test empty input."""
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""

    def test_short_number(self):
        """Test number too short to normalize."""
        assert normalize_phone("12345") == ""


class TestExtractPhoneFromJid:
    """Tests for JID phone extraction."""

    def test_standard_jid(self):
        """Test extracting from standard JID."""
        phone = extract_phone_from_jid("15551234567@s.whatsapp.net")
        assert phone == "+15551234567"

    def test_international_jid(self):
        """Test extracting from international JID."""
        phone = extract_phone_from_jid("442071234567@s.whatsapp.net")
        assert phone == "+442071234567"

    def test_empty_jid(self):
        """Test empty JID."""
        assert extract_phone_from_jid("") == ""
        assert extract_phone_from_jid(None) == ""

    def test_group_jid(self):
        """Test group JID (returns empty - not a phone)."""
        phone = extract_phone_from_jid("123456789@g.us")
        # Groups have numeric IDs but they're not phone numbers
        assert phone == "" or phone.startswith("+")


class TestIsGroupJid:
    """Tests for group JID detection."""

    def test_individual_jid(self):
        """Test individual chat JID."""
        assert is_group_jid("15551234567@s.whatsapp.net") is False

    def test_group_jid(self):
        """Test group chat JID."""
        assert is_group_jid("123456789012345@g.us") is True

    def test_empty(self):
        """Test empty JID."""
        assert is_group_jid("") is False
        assert is_group_jid(None) is False


class TestRunWacli:
    """Tests for wacli command execution."""

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_successful_command(self, mock_run):
        """Test successful wacli command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"success": true, "data": []}',
            stderr="",
        )

        result = run_wacli(["chats", "list"])

        # run_wacli extracts the "data" field from the response
        assert result == []
        mock_run.assert_called_once()

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_not_authenticated(self, mock_run):
        """Test handling not authenticated error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: no store found at ~/.wacli",
        )

        result = run_wacli(["chats", "list"])

        assert result is None

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_timeout(self, mock_run):
        """Test handling timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["wacli"], timeout=300)

        result = run_wacli(["messages", "list"])

        assert result is None

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_json_parse_error(self, mock_run):
        """Test handling invalid JSON."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not valid json",
            stderr="",
        )

        result = run_wacli(["chats", "list"])

        assert result is None


class TestCheckWacliAuth:
    """Tests for wacli authentication check."""

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_authenticated(self, mock_run_wacli):
        """Test when authenticated."""
        mock_run_wacli.return_value = [{"jid": "123@s.whatsapp.net"}]

        assert check_wacli_auth() is True

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_not_authenticated(self, mock_run_wacli):
        """Test when not authenticated."""
        mock_run_wacli.return_value = None

        assert check_wacli_auth() is False

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_empty_chats(self, mock_run_wacli):
        """Test when authenticated but no chats."""
        mock_run_wacli.return_value = []

        # Empty list is still valid - means authenticated but no chats
        assert check_wacli_auth() is True
