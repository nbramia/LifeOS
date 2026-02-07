"""
Tests for Action Item Extraction functionality.
P1.5 Acceptance Criteria:
- Detects `- [ ]` checkbox syntax
- Detects `Action:` pattern
- Detects `TODO:` pattern
- Extracts owner when specified
- Extracts due date when present
- Links action items to source note
- "What are my open action items" returns list
- "What did I commit to Alex" filters by person
- Completed items tracked separately
"""
import pytest

# All tests in this file are fast unit tests
pytestmark = pytest.mark.unit
import tempfile
from pathlib import Path
from datetime import datetime

from api.services.actions import (
    ActionItem,
    ActionRegistry,
    extract_action_items,
)


class TestActionItemDetection:
    """Test action item detection from text."""

    def test_detects_checkbox_syntax(self):
        """Should detect - [ ] checkbox syntax."""
        text = """
# Meeting Notes

## Action Items
- [ ] Send budget proposal to Sam
- [ ] Review Q1 targets
- [x] Complete the report
"""
        actions = extract_action_items(text, source_file="meeting.md", source_date="2025-01-05")

        open_actions = [a for a in actions if a.status == "open"]
        completed = [a for a in actions if a.status == "completed"]

        assert len(open_actions) == 2
        assert len(completed) == 1
        assert any("budget proposal" in a.task.lower() for a in open_actions)

    def test_detects_action_pattern(self):
        """Should detect Action: Owner → Task pattern."""
        text = """
# Meeting Summary

Action: John → Send the updated budget by Friday
Action: Alex → Review hiring pipeline
"""
        actions = extract_action_items(text, source_file="summary.md", source_date="2025-01-05")

        assert len(actions) >= 2
        john_action = next((a for a in actions if a.owner == "John"), None)
        assert john_action is not None
        assert "budget" in john_action.task.lower()

    def test_detects_todo_pattern(self):
        """Should detect TODO: pattern."""
        text = """
# Project Notes

TODO: Implement the new feature
TODO: Write documentation

Some other content here.
"""
        actions = extract_action_items(text, source_file="project.md", source_date="2025-01-05")

        assert len(actions) >= 2
        assert any("implement" in a.task.lower() for a in actions)

    def test_detects_next_steps_bullets(self):
        """Should detect items under Next Steps section."""
        text = """
# Meeting Notes

## Next Steps
- Schedule follow-up meeting
- Prepare presentation slides
- Review competitor analysis
"""
        actions = extract_action_items(text, source_file="meeting.md", source_date="2025-01-05")

        assert len(actions) >= 3

    def test_extracts_owner_from_checkbox(self):
        """Should extract owner from checkbox items."""
        text = """
- [ ] John: Send budget proposal
- [ ] @Alex Review the numbers
- [ ] Sam → Update spreadsheet
"""
        actions = extract_action_items(text, source_file="tasks.md", source_date="2025-01-05")

        assert len(actions) >= 3
        owners = [a.owner for a in actions if a.owner]
        assert "John" in owners
        assert "Alex" in owners
        assert "Sam" in owners

    def test_extracts_due_date(self):
        """Should extract due date when present."""
        text = """
- [ ] Send report by Friday
- [ ] Complete analysis by 2025-01-15
- [ ] Submit proposal (due: Jan 20)
"""
        actions = extract_action_items(text, source_file="tasks.md", source_date="2025-01-05")

        # At least one should have a due date
        actions_with_due = [a for a in actions if a.due_date]
        assert len(actions_with_due) >= 1

    def test_links_to_source_file(self):
        """Should link action items to source note."""
        text = "- [ ] Test action item"
        actions = extract_action_items(
            text,
            source_file="/vault/test.md",
            source_date="2025-01-05"
        )

        assert len(actions) == 1
        assert actions[0].source_file == "/vault/test.md"
        assert actions[0].source_date == "2025-01-05"

    def test_tracks_completed_items(self):
        """Should track completed items separately."""
        text = """
- [ ] Open task 1
- [x] Completed task 1
- [ ] Open task 2
- [x] Completed task 2
"""
        actions = extract_action_items(text, source_file="tasks.md", source_date="2025-01-05")

        open_tasks = [a for a in actions if a.status == "open"]
        completed = [a for a in actions if a.status == "completed"]

        assert len(open_tasks) == 2
        assert len(completed) == 2


class TestActionRegistry:
    """Test the Action Registry storage and queries."""

    @pytest.fixture
    def temp_registry_path(self):
        """Create temp path for registry storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "action_registry.json"

    @pytest.fixture
    def registry(self, temp_registry_path):
        """Create a fresh registry."""
        return ActionRegistry(storage_path=str(temp_registry_path))

    def test_adds_action_item(self, registry):
        """Should add action items to registry."""
        action = ActionItem(
            task="Send budget proposal",
            owner="John",
            status="open",
            source_file="/vault/meeting.md",
            source_date="2025-01-05"
        )
        registry.add_action(action)

        actions = registry.get_all_actions()
        assert len(actions) >= 1
        assert any("budget" in a.task.lower() for a in actions)

    def test_gets_open_actions(self, registry):
        """Should filter to only open actions."""
        registry.add_action(ActionItem(
            task="Open task",
            status="open",
            source_file="test.md",
            source_date="2025-01-05"
        ))
        registry.add_action(ActionItem(
            task="Completed task",
            status="completed",
            source_file="test.md",
            source_date="2025-01-05"
        ))

        open_actions = registry.get_open_actions()
        assert len(open_actions) == 1
        assert open_actions[0].task == "Open task"

    def test_filters_by_owner(self, registry):
        """Should filter actions by owner."""
        registry.add_action(ActionItem(
            task="John's task",
            owner="John",
            status="open",
            source_file="test.md",
            source_date="2025-01-05"
        ))
        registry.add_action(ActionItem(
            task="Alex's task",
            owner="Alex",
            status="open",
            source_file="test.md",
            source_date="2025-01-05"
        ))

        john_actions = registry.get_actions_by_owner("John")
        assert len(john_actions) == 1
        assert john_actions[0].owner == "John"

    def test_filters_by_person_involved(self, registry):
        """Should find actions involving a person (owner or mentioned)."""
        registry.add_action(ActionItem(
            task="Send report to Alex",
            owner="John",
            status="open",
            source_file="test.md",
            source_date="2025-01-05"
        ))

        # Should find this when filtering by Alex (mentioned in task)
        alex_actions = registry.get_actions_involving_person("Alex")
        assert len(alex_actions) >= 1

    def test_persists_registry(self, temp_registry_path):
        """Registry should persist across instances."""
        reg1 = ActionRegistry(storage_path=str(temp_registry_path))
        reg1.add_action(ActionItem(
            task="Persistent task",
            status="open",
            source_file="test.md",
            source_date="2025-01-05"
        ))
        reg1.save()

        reg2 = ActionRegistry(storage_path=str(temp_registry_path))
        actions = reg2.get_all_actions()
        assert len(actions) == 1
        assert actions[0].task == "Persistent task"

    def test_clears_actions_from_file(self, registry):
        """Should clear actions when re-indexing a file."""
        registry.add_action(ActionItem(
            task="Old task",
            source_file="/vault/test.md",
            source_date="2025-01-05",
            status="open"
        ))

        # Clear and re-add
        registry.clear_actions_from_file("/vault/test.md")
        registry.add_action(ActionItem(
            task="New task",
            source_file="/vault/test.md",
            source_date="2025-01-05",
            status="open"
        ))

        actions = registry.get_all_actions()
        assert len(actions) == 1
        assert actions[0].task == "New task"
