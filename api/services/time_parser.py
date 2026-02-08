"""
Smart time parsing for natural language reminder scheduling.

Provides context-aware defaults and natural language time parsing.
Handles expressions like "later today", "tomorrow morning", "next week", etc.
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo


# Default timezone for the user
DEFAULT_TIMEZONE = ZoneInfo("America/New_York")


def get_smart_default_time(now: Optional[datetime] = None) -> datetime:
    """
    Get a sensible default time for reminders when no time is specified.

    Default: tomorrow at 9am in user's timezone.

    Args:
        now: Current time (defaults to now in DEFAULT_TIMEZONE)

    Returns:
        datetime for tomorrow at 9:00 AM
    """
    if now is None:
        now = datetime.now(DEFAULT_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=DEFAULT_TIMEZONE)

    # Tomorrow at 9am
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)


def parse_contextual_time(
    expression: str,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Parse natural language time expressions with context awareness.

    Handles:
    - "later today" -> 5pm if before 5pm, 8pm if after
    - "tonight" -> 8pm today
    - "tomorrow" / "tomorrow morning" -> tomorrow 9am
    - "tomorrow afternoon" -> tomorrow 2pm
    - "tomorrow evening" -> tomorrow 6pm
    - "next week" -> next Monday 9am
    - "in X hours/minutes" -> now + X
    - "at X:XX" or "at Xpm" -> today/tomorrow at that time

    Args:
        expression: Natural language time expression
        now: Current time (defaults to now in DEFAULT_TIMEZONE)

    Returns:
        Parsed datetime or None if not recognized
    """
    if now is None:
        now = datetime.now(DEFAULT_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=DEFAULT_TIMEZONE)

    expr = expression.lower().strip()

    # "in X hours/minutes"
    in_time_match = re.search(r'in\s+(\d+)\s*(hour|hr|minute|min)s?', expr)
    if in_time_match:
        amount = int(in_time_match.group(1))
        unit = in_time_match.group(2)
        if 'hour' in unit or 'hr' in unit:
            return now + timedelta(hours=amount)
        else:
            return now + timedelta(minutes=amount)

    # "later today" - context-aware
    if 'later today' in expr:
        hour = now.hour
        if hour < 17:  # Before 5pm
            return now.replace(hour=17, minute=0, second=0, microsecond=0)
        else:  # After 5pm
            return now.replace(hour=20, minute=0, second=0, microsecond=0)

    # "tonight" - 8pm today
    if 'tonight' in expr:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)

    # "this evening" - 6pm today
    if 'this evening' in expr:
        return now.replace(hour=18, minute=0, second=0, microsecond=0)

    # "this afternoon" - 2pm today
    if 'this afternoon' in expr:
        return now.replace(hour=14, minute=0, second=0, microsecond=0)

    # "tomorrow" variants
    tomorrow = now + timedelta(days=1)
    if 'tomorrow' in expr:
        if 'morning' in expr:
            return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
        elif 'afternoon' in expr:
            return tomorrow.replace(hour=14, minute=0, second=0, microsecond=0)
        elif 'evening' in expr or 'night' in expr:
            return tomorrow.replace(hour=18, minute=0, second=0, microsecond=0)
        else:
            # Default tomorrow to 9am
            return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

    # "next week" - next Monday 9am
    if 'next week' in expr:
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7  # If today is Monday, go to next Monday
        next_monday = now + timedelta(days=days_until_monday)
        return next_monday.replace(hour=9, minute=0, second=0, microsecond=0)

    # "next monday/tuesday/etc"
    day_names = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }
    for day_name, day_num in day_names.items():
        if f'next {day_name}' in expr or f'on {day_name}' in expr:
            days_ahead = (day_num - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # If today, go to next week
            target_day = now + timedelta(days=days_ahead)
            return target_day.replace(hour=9, minute=0, second=0, microsecond=0)

    # "at X:XX" or "at Xpm/am" - parse specific time
    time_match = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', expr)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        ampm = time_match.group(3)

        if ampm:
            if ampm == 'pm' and hour != 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0

        # Determine if today or tomorrow
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # Move to tomorrow if time has passed
        return target

    # "X pm/am" without "at"
    bare_time_match = re.search(r'(\d{1,2})\s*(am|pm)', expr)
    if bare_time_match:
        hour = int(bare_time_match.group(1))
        ampm = bare_time_match.group(2)

        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0

        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


def format_time_for_display(
    dt: datetime,
    now: Optional[datetime] = None,
) -> str:
    """
    Format a datetime for user-friendly display.

    Examples:
    - "tomorrow at 9:00 AM"
    - "today at 5:00 PM"
    - "Monday at 9:00 AM"
    - "February 10 at 3:00 PM"

    Args:
        dt: datetime to format
        now: Current time for relative comparisons

    Returns:
        Human-readable string
    """
    if now is None:
        now = datetime.now(DEFAULT_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=DEFAULT_TIMEZONE)

    # Ensure dt has timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TIMEZONE)

    # Format time component
    time_str = dt.strftime("%-I:%M %p").lstrip("0")  # "9:00 AM" not "09:00 AM"

    # Determine date description
    today = now.date()
    target_date = dt.date()
    tomorrow = today + timedelta(days=1)

    if target_date == today:
        date_str = "today"
    elif target_date == tomorrow:
        date_str = "tomorrow"
    elif target_date < today + timedelta(days=7):
        date_str = dt.strftime("%A")  # "Monday", "Tuesday", etc.
    else:
        date_str = dt.strftime("%B %-d")  # "February 10"

    return f"{date_str} at {time_str}"


def extract_time_from_query(query: str) -> Optional[str]:
    """
    Extract time expression from a query for parsing.

    Finds time-related phrases in natural language queries.

    Args:
        query: Full user query

    Returns:
        Extracted time expression or None
    """
    query_lower = query.lower()

    # Patterns to extract time expressions
    patterns = [
        r'(in\s+\d+\s+(?:hour|hr|minute|min)s?)',
        r'(later today)',
        r'(tonight)',
        r'(this evening)',
        r'(this afternoon)',
        r'(tomorrow\s*(?:morning|afternoon|evening|night)?)',
        r'(next week)',
        r'(next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))',
        r'(on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))',
        r'(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
        r'(\d{1,2}\s*(?:am|pm))',
    ]

    for pattern in patterns:
        match = re.search(pattern, query_lower)
        if match:
            return match.group(1)

    return None
