"""Tests for contacts CSV sync."""
import pytest
from scripts.sync_contacts_csv import normalize_phone


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
        assert normalize_phone("555.123.4567") == "+15551234567"

    def test_international(self):
        """Test normalizing international number."""
        assert normalize_phone("+442071234567") == "+442071234567"
        assert normalize_phone("442071234567") == "+442071234567"

    def test_empty(self):
        """Test empty input returns empty."""
        assert normalize_phone("") == ""

    def test_short_number(self):
        """Test number too short to normalize returns empty."""
        assert normalize_phone("12345") == ""
        assert normalize_phone("123") == ""

    def test_with_spaces(self):
        """Test number with spaces."""
        assert normalize_phone("555 123 4567") == "+15551234567"
        assert normalize_phone("+1 555 123 4567") == "+15551234567"

    def test_with_country_prefix(self):
        """Test number with + prefix."""
        assert normalize_phone("+15551234567") == "+15551234567"
