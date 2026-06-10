"""Date parsing utilities for vault notes."""
import re
from datetime import date, datetime, timedelta
from typing import Optional, Union

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


# Vague "recency" terms map to a generous trailing window. Wide enough not to
# drop a genuinely-relevant note that is a few weeks old, narrow enough to
# exclude last year's results (the actual complaint being fixed).
_RECENT_WINDOW_DAYS = 90


def resolve_relative_time(
    text: str, now: Union[date, datetime], include_vague: bool = True
) -> Optional[tuple[str, str]]:
    """Resolve a relative-time phrase in ``text`` to a concrete date range.

    Pure function of ``now`` — never reads the system clock — so callers stay
    deterministic and testable. ``now`` is the caller's notion of "today"
    (computed fresh per request, in the user's timezone), so no notion of the
    current date is hardcoded anywhere.

    Returns ``(date_from, date_to)`` as inclusive ``YYYY-MM-DD`` strings, or
    ``None`` when no recognized phrase is present. Recognized phrases (checked
    most-specific first):

    - "today", "yesterday"                                       (bounded)
    - "this week" / "last week" (Mon–Sun ISO weeks)              (bounded)
    - "this month" / "last month"                                (bounded)
    - "this year" / "last year"                                  (bounded)
    - "past/last/previous N days/weeks/months"                   (bounded)
    - "recent" / "recently" / "lately" → trailing 90-day window  (vague)

    Bounded phrases denote a specific interval and are safe to apply as a hard
    filter. The vague terms only express a *preference* for recency, which the
    recency boost already serves — so ``include_vague=False`` returns ``None``
    for them, letting callers (e.g. the search routes) avoid hard-filtering a
    query like "the most recent invoice" down to an empty window when the
    newest match happens to be older than 90 days.

    The order matters: bounded phrases ("last week") win over the vague
    "recent" so the more precise range is used when both could match.
    """
    if not text:
        return None

    today = now.date() if isinstance(now, datetime) else now
    t = text.lower()

    def fmt(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    # "today" / "yesterday"
    if re.search(r"\btoday\b", t):
        return fmt(today), fmt(today)
    if re.search(r"\byesterday\b", t):
        y = today - timedelta(days=1)
        return fmt(y), fmt(y)

    # "past/last/previous N days|weeks|months" (numeric window)
    m = re.search(r"\b(?:past|last|previous)\s+(\d{1,4})\s+(day|week|month)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 30}[unit] * n
        return fmt(today - timedelta(days=days)), fmt(today)

    # "this/last week" (ISO weeks: Monday start)
    monday = today - timedelta(days=today.weekday())
    if re.search(r"\bthis week\b", t):
        return fmt(monday), fmt(today)
    if re.search(r"\blast week\b", t):
        last_monday = monday - timedelta(days=7)
        return fmt(last_monday), fmt(last_monday + timedelta(days=6))

    # "this/last month"
    first_of_month = today.replace(day=1)
    if re.search(r"\bthis month\b", t):
        return fmt(first_of_month), fmt(today)
    if re.search(r"\blast month\b", t):
        last_month_end = first_of_month - timedelta(days=1)
        return fmt(last_month_end.replace(day=1)), fmt(last_month_end)

    # "this/last year"
    if re.search(r"\bthis year\b", t):
        return fmt(today.replace(month=1, day=1)), fmt(today)
    if re.search(r"\blast year\b", t):
        ly = today.year - 1
        return f"{ly:04d}-01-01", f"{ly:04d}-12-31"

    # Vague recency — generous trailing window (preference, not a hard bound)
    if include_vague and re.search(r"\b(recent(ly)?|lately)\b", t):
        return fmt(today - timedelta(days=_RECENT_WINDOW_DAYS)), fmt(today)

    return None


def resolve_effective_dates(
    query: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Decide the effective date window for a search request.

    Explicit ``date_from``/``date_to`` always win. When neither is given, try to
    infer a window from a relative-time phrase in ``query`` evaluated against the
    current date (in the operator's configured timezone). Returns
    ``(date_from, date_to)``, either of which may be ``None``.

    The "now" used for inference is computed fresh here, so callers never carry a
    hardcoded notion of today.

    Only *bounded* phrases ("last week", "yesterday", "past 30 days") are
    auto-applied as a hard filter (``include_vague=False``). Vague recency
    ("recent", "lately") is intentionally left to the recency boost so an
    auto-filter never empties an otherwise-good result set.
    """
    if date_from or date_to:
        return date_from, date_to

    from zoneinfo import ZoneInfo
    from config.settings import settings

    now = datetime.now(ZoneInfo(settings.timezone))
    resolved = resolve_relative_time(query, now, include_vague=False)
    if resolved:
        return resolved
    return None, None


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
