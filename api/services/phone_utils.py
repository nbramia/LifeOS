"""
Phone number utilities for LifeOS.

Provides normalization to E.164 format and validation.
"""
import re
from typing import Optional

# Strip everything except digits and leading +
_NON_PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(raw: str, default_country: str = "US") -> Optional[str]:
    """
    Normalize phone number to E.164 format.

    Handles common US formats and international numbers with + prefix.
    Bare digit strings >11 digits are rejected (ambiguous without country code).

    Args:
        raw: Raw phone number in any common format
        default_country: ISO country code for numbers without country prefix.
            Currently only "US" is supported.

    Returns:
        E.164 formatted phone (+1XXXXXXXXXX) or None if invalid

    Examples:
        >>> normalize_phone("(212) 555-0173")
        '+12125550173'
        >>> normalize_phone("212-555-0173")
        '+12125550173'
        >>> normalize_phone("+1 212 555 0173")
        '+12125550173'
        >>> normalize_phone("2125550173")
        '+12125550173'
        >>> normalize_phone("+447700900123")
        '+447700900123'
        >>> normalize_phone("123")
    """
    if not raw:
        return None

    # Strip non-digit characters (keep leading +)
    cleaned = _NON_PHONE_RE.sub("", raw.strip())
    if not cleaned:
        return None

    # Already has + prefix — validate and return
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if digits.isdigit() and 10 <= len(digits) <= 15:
            return cleaned
        return None

    # All digits from here
    if not cleaned.isdigit():
        return None

    if default_country == "US":
        if len(cleaned) == 11 and cleaned.startswith("1"):
            return f"+{cleaned}"
        if len(cleaned) == 10:
            return f"+1{cleaned}"

    # Can't normalize without country code for non-standard lengths
    return None


def format_phone_display(phone: str) -> str:
    """
    Format E.164 phone number for display.

    Args:
        phone: E.164 formatted phone (+1XXXXXXXXXX)

    Returns:
        Display-friendly format: (XXX) XXX-XXXX for US numbers

    Examples:
        >>> format_phone_display("+12125550173")
        '(212) 555-0173'
        >>> format_phone_display("+447700900123")
        '+447700900123'
    """
    if not phone:
        return ""

    # US/Canada numbers (11 digits starting with +1)
    if phone.startswith("+1") and len(phone) == 12:
        area = phone[2:5]
        exchange = phone[5:8]
        subscriber = phone[8:12]
        return f"({area}) {exchange}-{subscriber}"

    # International: just return as-is
    return phone


def is_valid_phone(phone: str) -> bool:
    """
    Check if a string is a valid E.164 phone number.

    Args:
        phone: Phone number to validate

    Returns:
        True if valid E.164 format
    """
    if not phone:
        return False
    # E.164: starts with +, followed by 7-15 digits
    return bool(re.match(r'^\+[1-9]\d{6,14}$', phone))
