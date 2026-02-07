"""
Nickname lookup for entity resolution.

Provides bidirectional lookup between formal names and nicknames/diminutives.
E.g., "Benjamin" <-> "Ben", "Michael" <-> "Mike", "Katherine" <-> "Kate"

Data source: https://github.com/carltonnorthern/nicknames
"""

import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Path to the nicknames CSV file
NICKNAMES_CSV = Path(__file__).parent / "nicknames.csv"

# Global lookup tables (loaded lazily)
_nickname_to_formal: dict[str, set[str]] = {}
_formal_to_nicknames: dict[str, set[str]] = {}
_all_variants: dict[str, set[str]] = {}  # bidirectional: any name -> all variants
_loaded = False


def _load_nicknames() -> None:
    """Load the nicknames CSV into memory."""
    global _nickname_to_formal, _formal_to_nicknames, _all_variants, _loaded

    if _loaded:
        return

    _nickname_to_formal = defaultdict(set)
    _formal_to_nicknames = defaultdict(set)
    _all_variants = defaultdict(set)

    if not NICKNAMES_CSV.exists():
        _loaded = True
        return

    with open(NICKNAMES_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            formal = row.get("name1", "").strip().lower()
            nickname = row.get("name2", "").strip().lower()
            relationship = row.get("relationship", "")

            if not formal or not nickname:
                continue

            if relationship == "has_nickname":
                _formal_to_nicknames[formal].add(nickname)
                _nickname_to_formal[nickname].add(formal)

                # Build bidirectional variant map (direct relationships only)
                _all_variants[formal].add(nickname)
                _all_variants[nickname].add(formal)

    # Add one level of sibling relationships (if Ben -> Benjamin and Benji -> Benjamin,
    # then Ben and Benji are siblings via Benjamin)
    # This allows Ben to match Benji without cascading to unrelated names
    for formal, nicknames in list(_formal_to_nicknames.items()):
        # All nicknames of the same formal name are siblings
        for nick in nicknames:
            _all_variants[nick].update(nicknames)
            _all_variants[nick].discard(nick)  # Don't include self

    _loaded = True


def get_name_variants(name: str) -> set[str]:
    """
    Get all known variants of a name (nicknames and formal forms).

    Args:
        name: A first name to look up

    Returns:
        Set of variant names (may be empty if name not found)

    Examples:
        get_name_variants("benjamin") -> {"ben", "bennie", "benny", "benji"}
        get_name_variants("ben") -> {"benjamin", "benedict", "bennie", "benny"}
        get_name_variants("mike") -> {"michael", "micah", "mick", "mickey"}
    """
    _load_nicknames()
    return _all_variants.get(name.lower(), set())


def get_nicknames(formal_name: str) -> set[str]:
    """
    Get nicknames for a formal name.

    Args:
        formal_name: The formal/full first name

    Returns:
        Set of nicknames (may be empty)

    Examples:
        get_nicknames("benjamin") -> {"ben", "bennie", "benny", "benji"}
        get_nicknames("michael") -> {"mike", "mick", "mickey", "mikey"}
    """
    _load_nicknames()
    return _formal_to_nicknames.get(formal_name.lower(), set())


def get_formal_names(nickname: str) -> set[str]:
    """
    Get formal names for a nickname.

    Args:
        nickname: A nickname/diminutive

    Returns:
        Set of formal names (may be empty)

    Examples:
        get_formal_names("ben") -> {"benjamin", "benedict", "benson"}
        get_formal_names("mike") -> {"michael", "micah"}
    """
    _load_nicknames()
    return _nickname_to_formal.get(nickname.lower(), set())


def are_name_variants(name1: str, name2: str) -> bool:
    """
    Check if two names are variants of each other.

    Args:
        name1: First name
        name2: Second name

    Returns:
        True if the names are known variants

    Examples:
        are_name_variants("Ben", "Benjamin") -> True
        are_name_variants("Mike", "Michael") -> True
        are_name_variants("John", "Michael") -> False
    """
    if name1.lower() == name2.lower():
        return True

    _load_nicknames()
    name1_lower = name1.lower()
    name2_lower = name2.lower()

    variants = _all_variants.get(name1_lower, set())
    return name2_lower in variants


def get_stats() -> dict:
    """Get statistics about the loaded nickname data."""
    _load_nicknames()
    return {
        "formal_names": len(_formal_to_nicknames),
        "nicknames": len(_nickname_to_formal),
        "total_variants": len(_all_variants),
        "total_relationships": sum(len(v) for v in _formal_to_nicknames.values()),
    }
