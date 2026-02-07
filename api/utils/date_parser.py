"""Date parsing utilities for vault notes."""
import re
from datetime import date
from typing import Optional

MONTH_NAMES = {
    'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
    'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
    'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
    'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}


def parse_note_date(text: str) -> Optional[str]:
    """
    Parse date from text, returning YYYY-MM-DD or None.

    Requirements:
    - Must have year, month, AND day (no partial dates)
    - Future dates (after today) are rejected

    Supported formats:
    - ISO: 2024-12-19, 2024/12/19, 2022-3-6
    - US: 1-15-24, 3/15/19, 12/25/2024
    - Long: October 11, 2018, Oct 11 2018
    - Compact: jan12 2017, 20241219
    - EU: 11 October 2018
    """
    if not text:
        return None

    text = text.strip()
    today = date.today()

    # 1. ISO-like: YYYY-MM-DD or YYYY/MM/DD (with 1 or 2 digit month/day)
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
    if match:
        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        result = _validate_and_format(y, m, d, today)
        if result:
            return result

    # 2. US format: M-DD-YY, M/DD/YY, MM/DD/YYYY
    match = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})(?:\D|$)', text)
    if match:
        m, d, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if y < 100:  # 2-digit year
            y = 2000 + y if y < 50 else 1900 + y
        result = _validate_and_format(y, m, d, today)
        if result:
            return result

    # 3. Long month with space: "October 11, 2018", "Oct 11 2018"
    match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
    if match:
        month_str, day, year = match.group(1).lower(), int(match.group(2)), int(match.group(3))
        if month_str in MONTH_NAMES:
            result = _validate_and_format(year, MONTH_NAMES[month_str], day, today)
            if result:
                return result

    # 4. Compact month: "jan12 2017", "dec25 2020"
    match = re.search(r'([A-Za-z]{3,})(\d{1,2})\s+(\d{4})', text, re.IGNORECASE)
    if match:
        month_str, day, year = match.group(1).lower(), int(match.group(2)), int(match.group(3))
        if month_str in MONTH_NAMES:
            result = _validate_and_format(year, MONTH_NAMES[month_str], day, today)
            if result:
                return result

    # 5. Day Month Year: "11 October 2018"
    match = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', text)
    if match:
        day, month_str, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
        if month_str in MONTH_NAMES:
            result = _validate_and_format(year, MONTH_NAMES[month_str], day, today)
            if result:
                return result

    # 6. Compact numeric: 20241219
    match = re.match(r'^(\d{4})(\d{2})(\d{2})$', text)
    if match:
        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        result = _validate_and_format(y, m, d, today)
        if result:
            return result

    return None


def _validate_and_format(year: int, month: int, day: int, today: date) -> Optional[str]:
    """Validate date and return YYYY-MM-DD format, or None if invalid."""
    # Basic range check
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None

    try:
        d = date(year, month, day)
        # Reject future dates
        if d > today:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None
