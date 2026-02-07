"""
Phone number utilities for LifeOS.

Provides normalization to E.164 format and validation.
"""
import re
from typing import Optional


def normalize_phone(raw: str) -> Optional[str]:
    """
    Normalize phone number to E.164 format.

    Args:
        raw: Raw phone number in any common format

    Returns:
        E.164 formatted phone (+1XXXXXXXXXX) or None if invalid

    Examples:
        >>> normalize_phone("(901) 229-5017")
        '+19012295017'
        >>> normalize_phone("901-229-5017")
        '+19012295017'
        >>> normalize_phone("+1 901 229 5017")
        '+19012295017'
        >>> normalize_phone("9012295017")
        '+19012295017'
        >>> normalize_phone("123")
        None
    """
    if not raw:
        return None

    # Strip all non-digit characters
    digits = re.sub(r'\D', '', raw)

    if len(digits) == 10:
        # US number without country code
        return f"+1{digits}"
    elif len(digits) == 11 and digits[0] == '1':
        # US number with country code
        return f"+{digits}"
    elif len(digits) > 11:
        # International number
        return f"+{digits}"
    else:
        # Invalid (too short)
        return None


def format_phone_display(phone: str) -> str:
    """
    Format E.164 phone number for display.

    Args:
        phone: E.164 formatted phone (+1XXXXXXXXXX)

    Returns:
        Display-friendly format: (XXX) XXX-XXXX for US numbers

    Examples:
        >>> format_phone_display("+19012295017")
        '(901) 229-5017'
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
