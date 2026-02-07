"""
Action Item extraction and tracking service for LifeOS.

Extracts action items from notes using various patterns and maintains
a registry for querying.
"""
import re
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ActionItem:
    """Represents an action item extracted from a note."""
    task: str
    status: str = "open"  # open | completed
    owner: Optional[str] = None
    due_date: Optional[str] = None
    source_file: str = ""
    source_date: str = ""
    extracted_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionItem":
        return cls(**data)


def extract_action_items(
    text: str,
    source_file: str,
    source_date: str
) -> list[ActionItem]:
    """
    Extract action items from text using multiple patterns.

    Patterns detected:
    - `- [ ]` checkbox syntax (open)
    - `- [x]` checkbox syntax (completed)
    - `Action: Owner → Task`
    - `TODO: Task`
    - Bullets under "Next Steps:" section

    Args:
        text: Text content to extract from
        source_file: Path to source file
        source_date: Date of the note

    Returns:
        List of ActionItem objects
    """
    actions = []

    # Pattern 1: Checkbox syntax - [ ] and - [x]
    checkbox_pattern = r'-\s*\[([ xX])\]\s*(.+?)(?:\n|$)'
    for match in re.finditer(checkbox_pattern, text):
        status = "completed" if match.group(1).lower() == "x" else "open"
        task_text = match.group(2).strip()

        # Try to extract owner and due date from task text
        owner, task, due_date = _parse_task_text(task_text)

        actions.append(ActionItem(
            task=task,
            status=status,
            owner=owner,
            due_date=due_date,
            source_file=source_file,
            source_date=source_date,
            extracted_text=match.group(0).strip()
        ))

    # Pattern 2: Action: Owner → Task
    action_pattern = r'Action:\s*([A-Za-z]+)\s*(?:→|->|:)\s*(.+?)(?:\n|$)'
    for match in re.finditer(action_pattern, text, re.IGNORECASE):
        owner = match.group(1).strip()
        task_text = match.group(2).strip()

        _, task, due_date = _parse_task_text(task_text)

        actions.append(ActionItem(
            task=task,
            status="open",
            owner=owner,
            due_date=due_date,
            source_file=source_file,
            source_date=source_date,
            extracted_text=match.group(0).strip()
        ))

    # Pattern 3: TODO: Task
    todo_pattern = r'TODO:\s*(.+?)(?:\n|$)'
    for match in re.finditer(todo_pattern, text, re.IGNORECASE):
        task_text = match.group(1).strip()
        owner, task, due_date = _parse_task_text(task_text)

        actions.append(ActionItem(
            task=task,
            status="open",
            owner=owner,
            due_date=due_date,
            source_file=source_file,
            source_date=source_date,
            extracted_text=match.group(0).strip()
        ))

    # Pattern 4: Next Steps section
    next_steps_pattern = r'(?:##?\s*)?Next\s*Steps?:?\s*\n((?:-\s+.+\n?)+)'
    next_steps_match = re.search(next_steps_pattern, text, re.IGNORECASE)
    if next_steps_match:
        steps_text = next_steps_match.group(1)
        bullet_pattern = r'-\s+(.+?)(?:\n|$)'
        for match in re.finditer(bullet_pattern, steps_text):
            task_text = match.group(1).strip()
            # Skip if already captured as checkbox
            if task_text.startswith('['):
                continue

            owner, task, due_date = _parse_task_text(task_text)

            actions.append(ActionItem(
                task=task,
                status="open",
                owner=owner,
                due_date=due_date,
                source_file=source_file,
                source_date=source_date,
                extracted_text=match.group(0).strip()
            ))

    return actions


def _parse_task_text(task_text: str) -> tuple[Optional[str], str, Optional[str]]:
    """
    Parse task text to extract owner and due date.

    Patterns for owner:
    - "Owner: Task" or "Owner → Task"
    - "@Owner Task"

    Patterns for due date:
    - "by Friday"
    - "by 2025-01-15"
    - "(due: Jan 20)"

    Returns:
        Tuple of (owner, task, due_date)
    """
    owner = None
    due_date = None
    task = task_text

    # Extract owner - Pattern: "Name:" or "Name →" or "@Name"
    owner_patterns = [
        r'^([A-Z][a-z]+):\s*(.+)$',  # "Nathan: Do something"
        r'^([A-Z][a-z]+)\s*(?:→|->)\s*(.+)$',  # "Nathan → Do something"
        r'^@([A-Za-z]+)\s+(.+)$',  # "@Nathan Do something"
    ]

    for pattern in owner_patterns:
        match = re.match(pattern, task_text)
        if match:
            owner = match.group(1)
            task = match.group(2).strip()
            break

    # Extract due date patterns
    due_patterns = [
        (r'\(due:\s*([^)]+)\)', 1),  # (due: Jan 20)
        (r'by\s+(\d{4}-\d{2}-\d{2})', 1),  # by 2025-01-15
        (r'by\s+(Friday|Monday|Tuesday|Wednesday|Thursday|Saturday|Sunday)', 1),  # by Friday
        (r'by\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})', 0),  # by Jan 20
    ]

    for pattern, group in due_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            if group == 0:
                # Full match for month patterns
                due_date = match.group(0).replace("by ", "").strip()
            else:
                due_date = match.group(group).strip()
            # Remove due date from task
            task = re.sub(pattern, '', task, flags=re.IGNORECASE).strip()
            break

    return owner, task, due_date


class ActionRegistry:
    """
    Registry for tracking action items from notes.

    Provides querying capabilities for:
    - All open action items
    - Action items by owner
    - Action items involving a person
    - Completed action items
    """

    def __init__(self, storage_path: str = "./data/action_registry.json"):
        """
        Initialize action registry.

        Args:
            storage_path: Path to JSON storage file
        """
        self.storage_path = Path(storage_path)
        self._actions: list[ActionItem] = []
        self._load()

    def _load(self) -> None:
        """Load registry from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    self._actions = [ActionItem.from_dict(d) for d in data]
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load action registry: {e}")
                self._actions = []

    def save(self) -> None:
        """Persist registry to disk."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.storage_path, "w") as f:
                json.dump([a.to_dict() for a in self._actions], f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save action registry: {e}")

    def add_action(self, action: ActionItem) -> None:
        """Add an action item to the registry."""
        self._actions.append(action)

    def clear_actions_from_file(self, source_file: str) -> None:
        """Remove all actions from a specific source file."""
        self._actions = [a for a in self._actions if a.source_file != source_file]

    def get_all_actions(self) -> list[ActionItem]:
        """Get all action items."""
        return self._actions.copy()

    def get_open_actions(self) -> list[ActionItem]:
        """Get only open action items."""
        return [a for a in self._actions if a.status == "open"]

    def get_completed_actions(self) -> list[ActionItem]:
        """Get only completed action items."""
        return [a for a in self._actions if a.status == "completed"]

    def get_actions_by_owner(self, owner: str) -> list[ActionItem]:
        """Get actions owned by a specific person."""
        return [a for a in self._actions if a.owner and a.owner.lower() == owner.lower()]

    def get_actions_involving_person(self, person: str) -> list[ActionItem]:
        """Get actions that involve a person (owner or mentioned in task)."""
        person_lower = person.lower()
        results = []
        for action in self._actions:
            if action.owner and action.owner.lower() == person_lower:
                results.append(action)
            elif person_lower in action.task.lower():
                results.append(action)
        return results

    def get_actions_by_source(self, source_file: str) -> list[ActionItem]:
        """Get actions from a specific source file."""
        return [a for a in self._actions if a.source_file == source_file]


# Singleton instance
_registry: ActionRegistry | None = None


def get_action_registry(storage_path: str = "./data/action_registry.json") -> ActionRegistry:
    """Get or create action registry singleton."""
    global _registry
    if _registry is None:
        _registry = ActionRegistry(storage_path)
    return _registry
