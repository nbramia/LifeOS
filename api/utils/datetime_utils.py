"""
Datetime utilities for LifeOS API services.
"""
from datetime import datetime, timezone
from typing import Optional


def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Ensure datetime is timezone-aware (UTC if naive).

    Args:
        dt: A datetime object that may or may not have timezone info

    Returns:
        The same datetime with UTC timezone if it was naive,
        or the original timezone-aware datetime, or None if input was None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
