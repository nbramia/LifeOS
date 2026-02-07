"""
People tracking service for LifeOS.

Extracts person names from notes, handles aliases/misspellings,
and maintains a registry for person-scoped queries.
"""
import re
import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Path to people dictionary config file
PEOPLE_DICTIONARY_PATH = Path(__file__).parent.parent.parent / "config" / "people_dictionary.json"


def _load_people_dictionary() -> dict:
    """
    Load people dictionary from config file.

    Returns empty dict if file doesn't exist, allowing the system to work
    without personal configuration.
    """
    if PEOPLE_DICTIONARY_PATH.exists():
        try:
            with open(PEOPLE_DICTIONARY_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load people dictionary: {e}")
            return {}
    return {}


# People Dictionary - loaded from config file
PEOPLE_DICTIONARY = _load_people_dictionary()

# Build reverse lookup for aliases
ALIAS_MAP = {}
for name, info in PEOPLE_DICTIONARY.items():
    ALIAS_MAP[name.lower()] = name
    for alias in info.get("aliases", []):
        ALIAS_MAP[alias.lower()] = name

# Known names to look for (case-insensitive patterns)
KNOWN_NAMES = set(PEOPLE_DICTIONARY.keys())
for info in PEOPLE_DICTIONARY.values():
    KNOWN_NAMES.update(info.get("aliases", []))


def extract_people_from_text(text: str) -> list[str]:
    """
    Extract person names from text.

    Uses multiple strategies:
    1. Bold names (**Name**)
    2. Known names from dictionary
    3. Common patterns (Attendees:, Met with, etc.)

    Args:
        text: Text content to extract names from

    Returns:
        List of unique person names (excluding self)
    """
    people = set()

    # Strategy 1: Bold names
    bold_pattern = r'\*\*([A-Z][a-z]+)\*\*'
    for match in re.finditer(bold_pattern, text):
        name = match.group(1)
        resolved = resolve_person_name(name)
        if not _is_excluded(resolved):
            people.add(resolved)

    # Strategy 2: Known names from dictionary
    for name in KNOWN_NAMES:
        if len(name) > 2:  # Skip short aliases
            # Word boundary match
            pattern = r'\b' + re.escape(name) + r'(?:\'s)?\b'
            if re.search(pattern, text, re.IGNORECASE):
                resolved = resolve_person_name(name)
                if not _is_excluded(resolved):
                    people.add(resolved)

    # Strategy 3: Common patterns
    patterns = [
        r'(?:with|met|called|email(?:ed)?)\s+([A-Z][a-z]+)',
        r'(?:Attendees?|Participants?):\s*([A-Z][a-z]+(?:\s*,\s*[A-Z][a-z]+)*)',
        r'1-1\s+(?:with\s+)?([A-Z][a-z]+)',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            names_str = match.group(1)
            # Split on commas for attendee lists
            for name in re.split(r'\s*,\s*', names_str):
                name = name.strip()
                if name and len(name) > 1:
                    resolved = resolve_person_name(name)
                    if not _is_excluded(resolved):
                        people.add(resolved)

    return list(people)


def resolve_person_name(name: str) -> str:
    """
    Resolve a name to its canonical form.

    Handles:
    - Known aliases
    - Common misspellings
    - Email addresses

    Args:
        name: Name or alias to resolve

    Returns:
        Canonical name or original if not found
    """
    # Check alias map
    lookup = name.lower().strip()

    # Handle possessives
    if lookup.endswith("'s"):
        lookup = lookup[:-2]

    if lookup in ALIAS_MAP:
        return ALIAS_MAP[lookup]

    # Handle email addresses
    if "@" in name:
        email_name = name.split("@")[0].lower()
        if email_name in ALIAS_MAP:
            return ALIAS_MAP[email_name]

    return name


def _is_excluded(name: str) -> bool:
    """Check if a name should be excluded (e.g., self-references)."""
    info = PEOPLE_DICTIONARY.get(name)
    return info is not None and info.get("exclude", False)


def get_person_category(name: str) -> str:
    """Get the category for a person (work/personal/family)."""
    info = PEOPLE_DICTIONARY.get(name)
    if info:
        return info.get("category", "unknown")
    return "unknown"


class PeopleRegistry:
    """
    Registry for tracking people mentioned in notes.

    Stores:
    - Canonical name
    - Aliases
    - Category (work/personal/family)
    - Last mention date
    - Mention count
    - Related notes
    """

    def __init__(self, storage_path: str = "./data/people_registry.json"):
        """
        Initialize people registry.

        Args:
            storage_path: Path to JSON storage file
        """
        self.storage_path = Path(storage_path)
        self._registry: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r") as f:
                    self._registry = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load people registry: {e}")
                self._registry = {}

    def save(self) -> None:
        """Persist registry to disk."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.storage_path, "w") as f:
                json.dump(self._registry, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save people registry: {e}")

    def record_mention(
        self,
        name: str,
        source_file: str,
        mention_date: str
    ) -> None:
        """
        Record a person mention from a note.

        Args:
            name: Person name (will be resolved to canonical)
            source_file: Path to the source file
            mention_date: Date of the mention (YYYY-MM-DD)
        """
        canonical = resolve_person_name(name)

        if _is_excluded(canonical):
            return

        if canonical not in self._registry:
            self._registry[canonical] = {
                "canonical_name": canonical,
                "aliases": [],
                "category": get_person_category(canonical),
                "last_mention_date": mention_date,
                "mention_count": 0,
                "related_notes": []
            }

        person = self._registry[canonical]

        # Update mention count
        person["mention_count"] += 1

        # Update last mention date (keep most recent)
        if mention_date > person["last_mention_date"]:
            person["last_mention_date"] = mention_date

        # Add source file if not already present
        if source_file not in person["related_notes"]:
            person["related_notes"].append(source_file)

    def get_person(self, name: str) -> Optional[dict]:
        """
        Get person info by name.

        Args:
            name: Person name (will be resolved)

        Returns:
            Person dict or None if not found
        """
        canonical = resolve_person_name(name)
        return self._registry.get(canonical)

    def get_related_notes(self, name: str) -> list[str]:
        """
        Get list of notes mentioning a person.

        Args:
            name: Person name

        Returns:
            List of file paths
        """
        person = self.get_person(name)
        if person:
            return person.get("related_notes", [])
        return []

    def get_all_people(self) -> list[dict]:
        """Get all people in the registry."""
        return list(self._registry.values())

    def search_people(self, query: str) -> list[dict]:
        """
        Search for people by name.

        Args:
            query: Search query

        Returns:
            List of matching people
        """
        query_lower = query.lower()
        results = []

        for name, info in self._registry.items():
            if query_lower in name.lower():
                results.append(info)
            elif any(query_lower in alias.lower() for alias in info.get("aliases", [])):
                results.append(info)

        return results


# Singleton instance
_registry: PeopleRegistry | None = None


def get_people_registry(storage_path: str = "./data/people_registry.json") -> PeopleRegistry:
    """Get or create people registry singleton."""
    global _registry
    if _registry is None:
        _registry = PeopleRegistry(storage_path)
    return _registry
