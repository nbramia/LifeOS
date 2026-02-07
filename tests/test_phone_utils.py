"""
Tests for phone number utilities.
"""
import pytest

from api.services.phone_utils import (
    normalize_phone,
    format_phone_display,
    is_valid_phone,
)


class TestNormalizePhone:
    """Tests for normalize_phone function."""

    def test_normalize_10_digit_us(self):
        """Test normalizing 10-digit US numbers."""
        assert normalize_phone("9012295017") == "+19012295017"
        assert normalize_phone("5551234567") == "+15551234567"

    def test_normalize_with_parentheses(self):
        """Test normalizing numbers with parentheses."""
        assert normalize_phone("(901) 229-5017") == "+19012295017"
        assert normalize_phone("(555) 123-4567") == "+15551234567"

    def test_normalize_with_dashes(self):
        """Test normalizing numbers with dashes."""
        assert normalize_phone("901-229-5017") == "+19012295017"
        assert normalize_phone("555-123-4567") == "+15551234567"

    def test_normalize_with_spaces(self):
        """Test normalizing numbers with spaces."""
        assert normalize_phone("901 229 5017") == "+19012295017"
        assert normalize_phone("+1 901 229 5017") == "+19012295017"

    def test_normalize_with_country_code(self):
        """Test normalizing numbers with +1 country code."""
        assert normalize_phone("+19012295017") == "+19012295017"
        assert normalize_phone("19012295017") == "+19012295017"
        assert normalize_phone("1-901-229-5017") == "+19012295017"

    def test_normalize_international(self):
        """Test normalizing international numbers (>11 digits)."""
        # UK number
        assert normalize_phone("+447700900123") == "+447700900123"
        assert normalize_phone("447700900123") == "+447700900123"

    def test_normalize_invalid_too_short(self):
        """Test that short numbers return None."""
        assert normalize_phone("123") is None
        assert normalize_phone("1234567") is None
        assert normalize_phone("123456789") is None

    def test_normalize_empty(self):
        """Test empty string returns None."""
        assert normalize_phone("") is None
        assert normalize_phone(None) is None

    def test_normalize_mixed_formats(self):
        """Test various mixed format inputs."""
        assert normalize_phone("(901) 229.5017") == "+19012295017"
        assert normalize_phone("901.229.5017") == "+19012295017"
        assert normalize_phone("  901-229-5017  ") == "+19012295017"


class TestFormatPhoneDisplay:
    """Tests for format_phone_display function."""

    def test_format_us_number(self):
        """Test formatting US numbers."""
        assert format_phone_display("+19012295017") == "(901) 229-5017"
        assert format_phone_display("+15551234567") == "(555) 123-4567"

    def test_format_international_number(self):
        """Test that international numbers are returned as-is."""
        assert format_phone_display("+447700900123") == "+447700900123"

    def test_format_empty(self):
        """Test empty string returns empty."""
        assert format_phone_display("") == ""
        assert format_phone_display(None) == ""


class TestIsValidPhone:
    """Tests for is_valid_phone function."""

    def test_valid_e164_numbers(self):
        """Test valid E.164 format numbers."""
        assert is_valid_phone("+19012295017") is True
        assert is_valid_phone("+15551234567") is True
        assert is_valid_phone("+447700900123") is True
        assert is_valid_phone("+861234567890") is True

    def test_invalid_missing_plus(self):
        """Test numbers without + are invalid."""
        assert is_valid_phone("19012295017") is False
        assert is_valid_phone("9012295017") is False

    def test_invalid_too_short(self):
        """Test numbers that are too short."""
        assert is_valid_phone("+12345") is False
        assert is_valid_phone("+123456") is False

    def test_invalid_too_long(self):
        """Test numbers that are too long."""
        assert is_valid_phone("+1234567890123456") is False

    def test_invalid_starts_with_zero(self):
        """Test numbers starting with zero after + are invalid."""
        assert is_valid_phone("+0123456789") is False

    def test_invalid_non_numeric(self):
        """Test numbers with non-numeric characters."""
        assert is_valid_phone("+1-901-229-5017") is False
        assert is_valid_phone("+1 901 229 5017") is False
        assert is_valid_phone("+(901) 229-5017") is False

    def test_empty(self):
        """Test empty values return False."""
        assert is_valid_phone("") is False
        assert is_valid_phone(None) is False
