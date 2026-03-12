"""Phone number normalization utilities."""
import re

# Strip everything except digits and leading +
_NON_PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(raw: str, default_country: str = "US") -> str | None:
    """Normalize a phone number to E.164 format.

    Handles common US formats:
        (703) 798-6709   → +17037986709
        4102591307       → +14102591307
        +15551234567     → +15551234567 (already E.164)
        703-798-6709     → +17037986709

    Args:
        raw: Raw phone string in any format.
        default_country: ISO country code for numbers without country prefix.
            Currently only "US" is supported.

    Returns:
        E.164 formatted string, or None if the input cannot be normalized.
    """
    if not raw:
        return None

    # Strip non-digit characters (keep leading +)
    cleaned = _NON_PHONE_RE.sub("", raw.strip())
    if not cleaned:
        return None

    # Already has + prefix
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
