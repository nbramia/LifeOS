"""
Tests for api/services/task_manager.py

Tests task CRUD operations, status transitions, context changes, fuzzy search,
parse/format round-trip, reindexing, and persistence.
"""
import json
import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from api.services.task_manager import (
    Task,
    TaskManager,
    STATUS_TO_SYMBOL,
    SYMBOL_TO_STATUS,
    VALID_STATUSES,
    _format_task_line,
    _parse_task_line,
    _fuzzy_filter,
)

pytestmark = pytest.mark.unit


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def tmp_vault(tmp_path):
    """Create a temporary vault directory."""
    return tmp_path / "vault"


@pytest.fixture
def tmp_index(tmp_path):
    """Create a temporary index path."""
    return tmp_path / "index" / "task_index.json"


@pytest.fixture
def task_manager(tmp_vault, tmp_index):
    """Create a TaskManager with temporary paths."""
    return TaskManager(vault_path=tmp_vault, index_path=tmp_index)


@pytest.fixture
def populated_manager(task_manager):
    """Create a TaskManager with several tasks."""
    task_manager.create("Write documentation", context="Work", tags=["docs", "writing"])
    task_manager.create("Pull 1099 from Schwab", context="Personal", tags=["taxes", "finance"])
    task_manager.create("Review PR", context="Work", priority="high", tags=["code-review"])
    task_manager.create("Buy groceries", context="Personal", due_date="2025-02-15")
    task_manager.create("Schedule meeting", context="Work", due_date="2025-02-10", priority="urgent")
    return task_manager


# =============================================================================
# Task Dataclass Tests
# =============================================================================

class TestTaskDataclass:
    """Tests for Task dataclass."""

    def test_task_creation(self):
        """Test creating a Task instance."""
        task = Task(
            id="test123",
            description="Test task",
            status="todo",
            context="Inbox",
            priority="high",
            due_date="2025-02-15",
            created_date="2025-02-08",
            tags=["test", "sample"],
        )
        assert task.id == "test123"
        assert task.description == "Test task"
        assert task.status == "todo"
        assert task.context == "Inbox"
        assert task.priority == "high"
        assert task.due_date == "2025-02-15"
        assert task.tags == ["test", "sample"]

    def test_task_to_dict(self):
        """Test converting Task to dictionary."""
        task = Task(
            id="test123",
            description="Test task",
            status="todo",
            context="Inbox",
            created_date="2025-02-08",
        )
        result = task.to_dict()

        assert result["id"] == "test123"
        assert result["description"] == "Test task"
        assert result["status"] == "todo"
        assert result["context"] == "Inbox"
        assert "created_date" in result

    def test_task_from_dict(self):
        """Test creating Task from dictionary."""
        data = {
            "id": "test123",
            "description": "Test task",
            "status": "todo",
            "context": "Inbox",
            "created_date": "2025-02-08",
            "tags": ["test"],
        }
        task = Task.from_dict(data)

        assert task.id == "test123"
        assert task.description == "Test task"
        assert task.tags == ["test"]

    def test_task_from_dict_handles_non_list_tags(self):
        """Test that from_dict handles non-list tags gracefully."""
        data = {
            "id": "test123",
            "description": "Test task",
            "status": "todo",
            "context": "Inbox",
            "created_date": "2025-02-08",
            "tags": "not-a-list",
        }
        task = Task.from_dict(data)
        assert task.tags == []


# =============================================================================
# Status and Symbol Mapping Tests
# =============================================================================

class TestStatusMappings:
    """Tests for status to symbol mappings."""

    def test_all_statuses_have_symbols(self):
        """Test that all valid statuses have symbol mappings."""
        for status in VALID_STATUSES:
            assert status in STATUS_TO_SYMBOL
            symbol = STATUS_TO_SYMBOL[status]
            assert symbol in SYMBOL_TO_STATUS

    def test_specific_status_mappings(self):
        """Test specific status to symbol mappings."""
        assert STATUS_TO_SYMBOL["todo"] == " "
        assert STATUS_TO_SYMBOL["done"] == "x"
        assert STATUS_TO_SYMBOL["in_progress"] == "/"
        assert STATUS_TO_SYMBOL["cancelled"] == "-"
        assert STATUS_TO_SYMBOL["deferred"] == ">"
        assert STATUS_TO_SYMBOL["blocked"] == "?"
        assert STATUS_TO_SYMBOL["urgent"] == "!"

    def test_symbol_to_status_reverse_mapping(self):
        """Test reverse mapping from symbol to status."""
        assert SYMBOL_TO_STATUS["x"] == "done"
        assert SYMBOL_TO_STATUS["/"] == "in_progress"
        assert SYMBOL_TO_STATUS["!"] == "urgent"


# =============================================================================
# TaskManager Initialization Tests
# =============================================================================

class TestTaskManagerInit:
    """Tests for TaskManager initialization."""

    def test_init_creates_directories(self, tmp_vault, tmp_index):
        """Test initialization creates necessary directories."""
        manager = TaskManager(vault_path=tmp_vault, index_path=tmp_index)

        assert manager.tasks_dir.exists()
        assert manager.index_path.parent.exists()
        assert manager.tasks_dir == tmp_vault / "LifeOS/Tasks"

    def test_init_creates_dashboard(self, tmp_vault, tmp_index):
        """Test initialization creates Dashboard.md."""
        manager = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        dashboard = manager.tasks_dir / "Dashboard.md"

        assert dashboard.exists()
        content = dashboard.read_text()
        assert "# Task Dashboard" in content
        assert "## All Open" in content

    def test_init_loads_empty_index(self, tmp_vault, tmp_index):
        """Test initialization with no existing index."""
        manager = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        tasks = manager.list_tasks()

        assert tasks == []

    def test_init_loads_existing_index(self, tmp_vault, tmp_index):
        """Test initialization loads existing index."""
        # Create manager and add task
        manager1 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        task = manager1.create("Test task", context="Inbox")
        task_id = task.id

        # Create new manager and verify task exists
        manager2 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        retrieved = manager2.get(task_id)

        assert retrieved is not None
        assert retrieved.description == "Test task"


# =============================================================================
# CRUD Operation Tests
# =============================================================================

class TestCreate:
    """Tests for create method."""

    def test_create_basic_task(self, task_manager):
        """Test creating a basic task."""
        task = task_manager.create("Test task")

        assert task.id
        assert len(task.id) == 8  # UUID hex[:8]
        assert task.description == "Test task"
        assert task.status == "todo"
        assert task.context == "Inbox"
        assert task.created_date == date.today().isoformat()

    def test_create_with_context(self, task_manager):
        """Test creating task with specific context."""
        task = task_manager.create("Work task", context="Work")

        assert task.context == "Work"

    def test_create_with_priority(self, task_manager):
        """Test creating task with priority."""
        task = task_manager.create("Important task", priority="high")

        assert task.priority == "high"

    def test_create_with_due_date(self, task_manager):
        """Test creating task with due date."""
        task = task_manager.create("Deadline task", due_date="2025-02-15")

        assert task.due_date == "2025-02-15"

    def test_create_with_tags(self, task_manager):
        """Test creating task with tags."""
        task = task_manager.create("Tagged task", tags=["urgent", "important"])

        assert "urgent" in task.tags
        assert "important" in task.tags

    def test_create_with_reminder_id(self, task_manager):
        """Test creating task with reminder ID."""
        task = task_manager.create("Reminder task", reminder_id="reminder123")

        assert task.reminder_id == "reminder123"

    def test_create_appends_to_file(self, task_manager):
        """Test that create appends task to markdown file."""
        task = task_manager.create("File test", context="TestContext")

        file_path = task_manager.tasks_dir / "TestContext.md"
        assert file_path.exists()

        content = file_path.read_text()
        assert "File test" in content
        assert "- [ ] TODO" in content

    def test_create_updates_index(self, task_manager):
        """Test that create updates the index file."""
        task = task_manager.create("Index test")

        assert task_manager.index_path.exists()
        data = json.loads(task_manager.index_path.read_text())

        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["description"] == "Index test"

    def test_create_records_source_info(self, task_manager):
        """Test that create records source file and line number."""
        task = task_manager.create("Source test", context="SourceTest")

        assert task.source_file
        assert task.line_number > 0
        assert Path(task.source_file).exists()


class TestGet:
    """Tests for get method."""

    def test_get_existing_task(self, task_manager):
        """Test getting an existing task."""
        created = task_manager.create("Get test")
        retrieved = task_manager.get(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.description == created.description

    def test_get_nonexistent_task(self, task_manager):
        """Test getting a non-existent task."""
        result = task_manager.get("nonexistent")
        assert result is None


class TestComplete:
    """Tests for complete method."""

    def test_complete_task(self, task_manager):
        """Test marking a task as done."""
        task = task_manager.create("Complete me")
        completed = task_manager.complete(task.id)

        assert completed is not None
        assert completed.status == "done"
        assert completed.done_date == date.today().isoformat()

    def test_complete_updates_file(self, task_manager):
        """Test that complete updates the markdown file."""
        task = task_manager.create("File complete", context="Complete")
        task_manager.complete(task.id)

        file_path = task_manager.tasks_dir / "Complete.md"
        content = file_path.read_text()

        assert "- [x] TODO File complete" in content

    def test_complete_nonexistent_task(self, task_manager):
        """Test completing a non-existent task."""
        result = task_manager.complete("nonexistent")
        assert result is None


class TestUpdate:
    """Tests for update method."""

    def test_update_description(self, task_manager):
        """Test updating task description."""
        task = task_manager.create("Original description")
        updated = task_manager.update(task.id, description="New description")

        assert updated.description == "New description"

    def test_update_status(self, task_manager):
        """Test updating task status."""
        task = task_manager.create("Status test")
        updated = task_manager.update(task.id, status="in_progress")

        assert updated.status == "in_progress"

    def test_update_priority(self, task_manager):
        """Test updating task priority."""
        task = task_manager.create("Priority test")
        updated = task_manager.update(task.id, priority="high")

        assert updated.priority == "high"

    def test_update_due_date(self, task_manager):
        """Test updating task due date."""
        task = task_manager.create("Due date test")
        updated = task_manager.update(task.id, due_date="2025-03-01")

        assert updated.due_date == "2025-03-01"

    def test_update_tags(self, task_manager):
        """Test updating task tags."""
        task = task_manager.create("Tags test", tags=["old"])
        updated = task_manager.update(task.id, tags=["new", "tags"])

        assert "new" in updated.tags
        assert "tags" in updated.tags
        assert "old" not in updated.tags

    def test_update_status_to_done_sets_done_date(self, task_manager):
        """Test that updating status to done sets done_date."""
        task = task_manager.create("Done test")
        updated = task_manager.update(task.id, status="done")

        assert updated.done_date == date.today().isoformat()

    def test_update_status_to_cancelled_sets_cancelled_date(self, task_manager):
        """Test that updating status to cancelled sets cancelled_date."""
        task = task_manager.create("Cancel test")
        updated = task_manager.update(task.id, status="cancelled")

        assert updated.cancelled_date == date.today().isoformat()

    def test_update_nonexistent_task(self, task_manager):
        """Test updating a non-existent task."""
        result = task_manager.update("nonexistent", description="New")
        assert result is None

    def test_update_rewrites_line_in_file(self, task_manager):
        """Test that update rewrites the line in the file."""
        task = task_manager.create("Update file test", context="UpdateTest")
        original_line_num = task.line_number

        task_manager.update(task.id, description="Updated description")

        file_path = task_manager.tasks_dir / "UpdateTest.md"
        content = file_path.read_text()

        assert "Updated description" in content
        assert "Update file test" not in content


class TestDelete:
    """Tests for delete method."""

    def test_delete_task(self, task_manager):
        """Test deleting a task."""
        task = task_manager.create("Delete me")
        result = task_manager.delete(task.id)

        assert result is True
        assert task_manager.get(task.id) is None

    def test_delete_removes_from_file(self, task_manager):
        """Test that delete removes line from file."""
        task = task_manager.create("Remove from file", context="DeleteTest")
        file_path = task_manager.tasks_dir / "DeleteTest.md"

        # Verify task is in file
        content_before = file_path.read_text()
        assert "Remove from file" in content_before

        task_manager.delete(task.id)

        # Verify task is removed
        content_after = file_path.read_text()
        assert "Remove from file" not in content_after

    def test_delete_updates_index(self, task_manager):
        """Test that delete updates the index."""
        task = task_manager.create("Index delete")
        task_manager.delete(task.id)

        data = json.loads(task_manager.index_path.read_text())
        task_ids = [t["id"] for t in data["tasks"]]

        assert task.id not in task_ids

    def test_delete_adjusts_line_numbers(self, task_manager):
        """Test that delete adjusts line numbers for tasks below deleted line."""
        task1 = task_manager.create("First task", context="LineAdjust")
        task2 = task_manager.create("Second task", context="LineAdjust")
        task3 = task_manager.create("Third task", context="LineAdjust")

        line2_before = task2.line_number
        line3_before = task3.line_number

        # Delete first task
        task_manager.delete(task1.id)

        # Check that task2 and task3 line numbers decreased
        task2_after = task_manager.get(task2.id)
        task3_after = task_manager.get(task3.id)

        assert task2_after.line_number == line2_before - 1
        assert task3_after.line_number == line3_before - 1

    def test_delete_nonexistent_task(self, task_manager):
        """Test deleting a non-existent task."""
        result = task_manager.delete("nonexistent")
        assert result is False


# =============================================================================
# List and Filter Tests
# =============================================================================

class TestListTasks:
    """Tests for list_tasks method."""

    def test_list_all_tasks(self, populated_manager):
        """Test listing all tasks."""
        tasks = populated_manager.list_tasks()
        assert len(tasks) == 5

    def test_list_by_status(self, populated_manager):
        """Test filtering by status."""
        # Complete one task
        tasks = populated_manager.list_tasks()
        populated_manager.complete(tasks[0].id)

        todo_tasks = populated_manager.list_tasks(status="todo")
        done_tasks = populated_manager.list_tasks(status="done")

        assert len(todo_tasks) == 4
        assert len(done_tasks) == 1

    def test_list_by_context(self, populated_manager):
        """Test filtering by context."""
        work_tasks = populated_manager.list_tasks(context="Work")
        personal_tasks = populated_manager.list_tasks(context="Personal")

        assert len(work_tasks) == 3
        assert len(personal_tasks) == 2

    def test_list_by_tag(self, populated_manager):
        """Test filtering by tag."""
        finance_tasks = populated_manager.list_tasks(tag="finance")

        assert len(finance_tasks) >= 1
        assert any("1099" in t.description for t in finance_tasks)

    def test_list_by_tag_without_hash(self, populated_manager):
        """Test filtering by tag works with or without # prefix."""
        with_hash = populated_manager.list_tasks(tag="#taxes")
        without_hash = populated_manager.list_tasks(tag="taxes")

        assert len(with_hash) == len(without_hash)

    def test_list_by_due_before(self, populated_manager):
        """Test filtering by due date."""
        tasks = populated_manager.list_tasks(due_before="2025-02-12")

        # Should include task due on 2025-02-10
        assert len(tasks) >= 1
        for task in tasks:
            assert task.due_date is not None
            assert task.due_date <= "2025-02-12"

    def test_list_with_query_exact_match(self, populated_manager):
        """Test fuzzy query matching with exact substring."""
        tasks = populated_manager.list_tasks(query="documentation")

        assert len(tasks) >= 1
        assert any("documentation" in t.description.lower() for t in tasks)

    def test_list_with_query_fuzzy_match(self, populated_manager):
        """Test fuzzy query matching."""
        # "1099" should find "Pull 1099 from Schwab" via exact match
        # "Schwab" should find it via exact match
        # For true fuzzy matching test, we search for similar word
        tasks = populated_manager.list_tasks(query="1099")

        # Should match via exact substring in description
        assert len(tasks) >= 1
        assert any("1099" in t.description for t in tasks)

    def test_list_empty(self, task_manager):
        """Test listing with no tasks."""
        tasks = task_manager.list_tasks()
        assert tasks == []

    def test_list_multiple_filters(self, populated_manager):
        """Test combining multiple filters."""
        tasks = populated_manager.list_tasks(
            status="todo",
            context="Work",
        )

        for task in tasks:
            assert task.status == "todo"
            assert task.context == "Work"


# =============================================================================
# Fuzzy Search Tests
# =============================================================================

class TestFuzzyFilter:
    """Tests for fuzzy matching functionality."""

    def test_fuzzy_filter_exact_substring(self):
        """Test fuzzy filter with exact substring match."""
        tasks = [
            Task(id="1", description="Pull 1099 from Schwab", status="todo", context="Personal"),
            Task(id="2", description="Buy groceries", status="todo", context="Personal"),
        ]

        results = _fuzzy_filter(tasks, "1099")
        assert len(results) == 1
        assert results[0].id == "1"

    def test_fuzzy_filter_partial_match(self):
        """Test fuzzy filter with partial match."""
        tasks = [
            Task(id="1", description="Pull 1099 from Schwab", status="todo", context="Personal"),
            Task(id="2", description="Review tax documents", status="todo", context="Personal"),
        ]

        results = _fuzzy_filter(tasks, "tax")
        assert len(results) >= 1

    def test_fuzzy_filter_case_insensitive(self):
        """Test fuzzy filter is case insensitive."""
        tasks = [
            Task(id="1", description="Write Documentation", status="todo", context="Work"),
        ]

        results = _fuzzy_filter(tasks, "documentation")
        assert len(results) == 1

    @pytest.mark.skipif(
        not pytest.importorskip("rapidfuzz", reason="rapidfuzz not installed"),
        reason="rapidfuzz required for fuzzy matching"
    )
    def test_fuzzy_filter_with_rapidfuzz(self):
        """Test fuzzy filter uses rapidfuzz when available."""
        tasks = [
            Task(id="1", description="Pull 1099 from Schwab", status="todo", context="Personal"),
        ]

        # "taxes" should match via fuzzy scoring
        results = _fuzzy_filter(tasks, "Schwab")
        assert len(results) >= 1


# =============================================================================
# Format and Parse Tests
# =============================================================================

class TestFormatTaskLine:
    """Tests for _format_task_line function."""

    def test_format_basic_task(self):
        """Test formatting a basic task."""
        task = Task(
            id="test123",
            description="Test task",
            status="todo",
            context="Inbox",
            created_date="2025-02-08",
        )

        line = _format_task_line(task)

        assert "- [ ] TODO Test task" in line
        assert "[created:: 2025-02-08]" in line
        assert "<!-- id:test123 -->" in line

    def test_format_task_with_status(self):
        """Test formatting task with different status."""
        task = Task(
            id="test123",
            description="Done task",
            status="done",
            context="Inbox",
            created_date="2025-02-08",
            done_date="2025-02-09",
        )

        line = _format_task_line(task)

        assert "- [x] TODO Done task" in line
        assert "[done:: 2025-02-09]" in line

    def test_format_task_with_all_fields(self):
        """Test formatting task with all fields."""
        task = Task(
            id="test123",
            description="Complete task",
            status="in_progress",
            context="Work",
            priority="high",
            due_date="2025-02-15",
            created_date="2025-02-08",
            tags=["urgent", "important"],
        )

        line = _format_task_line(task)

        assert "- [/] TODO Complete task" in line
        assert "[due:: 2025-02-15]" in line
        assert "[priority:: high]" in line
        assert "#urgent" in line
        assert "#important" in line

    def test_format_task_with_tags_without_hash(self):
        """Test that tags without # get # added."""
        task = Task(
            id="test123",
            description="Tagged task",
            status="todo",
            context="Inbox",
            created_date="2025-02-08",
            tags=["tag1", "tag2"],
        )

        line = _format_task_line(task)

        assert "#tag1" in line
        assert "#tag2" in line


class TestParseTaskLine:
    """Tests for _parse_task_line function."""

    def test_parse_basic_task(self):
        """Test parsing a basic task line."""
        line = "- [ ] TODO Test task [created:: 2025-02-08] <!-- id:test123 -->"

        task = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert task is not None
        assert task.id == "test123"
        assert task.description == "Test task"
        assert task.status == "todo"
        assert task.created_date == "2025-02-08"

    def test_parse_task_with_status(self):
        """Test parsing task with different status."""
        line = "- [x] TODO Done task [created:: 2025-02-08] [done:: 2025-02-09] <!-- id:test123 -->"

        task = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert task.status == "done"
        assert task.done_date == "2025-02-09"

    def test_parse_task_with_all_fields(self):
        """Test parsing task with all fields."""
        line = "- [/] TODO Complete task [due:: 2025-02-15] [priority:: high] [created:: 2025-02-08] #urgent #important <!-- id:test123 -->"

        task = _parse_task_line(line, "/path/to/Work.md", 5)

        assert task.status == "in_progress"
        assert task.description == "Complete task"
        assert task.due_date == "2025-02-15"
        assert task.priority == "high"
        assert "urgent" in task.tags
        assert "important" in task.tags
        assert task.source_file == "/path/to/Work.md"
        assert task.line_number == 5

    def test_parse_task_infers_context_from_filename(self):
        """Test that context is inferred from filename."""
        line = "- [ ] TODO Test [created:: 2025-02-08] <!-- id:test123 -->"

        task = _parse_task_line(line, "/path/to/ProjectX.md", 1)

        assert task.context == "ProjectX"

    def test_parse_task_generates_id_if_missing(self):
        """Test that ID is generated if missing."""
        line = "- [ ] TODO Test [created:: 2025-02-08]"

        task = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert task is not None
        assert task.id
        assert len(task.id) == 8

    def test_parse_non_task_line_returns_none(self):
        """Test that non-task lines return None."""
        line = "This is just regular text"

        task = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert task is None

    def test_parse_checkbox_without_todo_keyword_returns_none(self):
        """Test that checkbox without TODO keyword returns None."""
        line = "- [ ] Regular checklist item"

        task = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert task is None


class TestParseFormatRoundTrip:
    """Tests for parse/format round-trip."""

    def test_format_parse_roundtrip_basic(self):
        """Test that formatting and parsing a task preserves data."""
        original = Task(
            id="test123",
            description="Test task",
            status="todo",
            context="Inbox",
            created_date="2025-02-08",
        )

        line = _format_task_line(original)
        parsed = _parse_task_line(line, "/path/to/Inbox.md", 1)

        assert parsed.id == original.id
        assert parsed.description == original.description
        assert parsed.status == original.status
        assert parsed.created_date == original.created_date

    def test_format_parse_roundtrip_complete(self):
        """Test round-trip with all fields."""
        original = Task(
            id="test123",
            description="Complete task",
            status="done",
            context="Work",
            priority="high",
            due_date="2025-02-15",
            created_date="2025-02-08",
            done_date="2025-02-09",
            tags=["urgent", "important"],
        )

        line = _format_task_line(original)
        parsed = _parse_task_line(line, "/path/to/Work.md", 1)

        assert parsed.id == original.id
        assert parsed.description == original.description
        assert parsed.status == original.status
        assert parsed.priority == original.priority
        assert parsed.due_date == original.due_date
        assert parsed.created_date == original.created_date
        assert parsed.done_date == original.done_date
        assert set(parsed.tags) == set(original.tags)


# =============================================================================
# Status Transition Tests
# =============================================================================

class TestStatusTransitions:
    """Tests for all 7 status types."""

    def test_status_todo(self, task_manager):
        """Test todo status."""
        task = task_manager.create("Todo task")
        assert task.status == "todo"

        file_path = Path(task.source_file)
        content = file_path.read_text()
        assert "- [ ] TODO" in content

    def test_status_done(self, task_manager):
        """Test done status."""
        task = task_manager.create("Done task")
        task_manager.update(task.id, status="done")

        updated = task_manager.get(task.id)
        assert updated.status == "done"
        assert updated.done_date is not None

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [x] TODO" in content

    def test_status_in_progress(self, task_manager):
        """Test in_progress status."""
        task = task_manager.create("In progress task")
        task_manager.update(task.id, status="in_progress")

        updated = task_manager.get(task.id)
        assert updated.status == "in_progress"

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [/] TODO" in content

    def test_status_cancelled(self, task_manager):
        """Test cancelled status."""
        task = task_manager.create("Cancelled task")
        task_manager.update(task.id, status="cancelled")

        updated = task_manager.get(task.id)
        assert updated.status == "cancelled"
        assert updated.cancelled_date is not None

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [-] TODO" in content

    def test_status_deferred(self, task_manager):
        """Test deferred status."""
        task = task_manager.create("Deferred task")
        task_manager.update(task.id, status="deferred")

        updated = task_manager.get(task.id)
        assert updated.status == "deferred"

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [>] TODO" in content

    def test_status_blocked(self, task_manager):
        """Test blocked status."""
        task = task_manager.create("Blocked task")
        task_manager.update(task.id, status="blocked")

        updated = task_manager.get(task.id)
        assert updated.status == "blocked"

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [?] TODO" in content

    def test_status_urgent(self, task_manager):
        """Test urgent status."""
        task = task_manager.create("Urgent task")
        task_manager.update(task.id, status="urgent")

        updated = task_manager.get(task.id)
        assert updated.status == "urgent"

        file_path = Path(updated.source_file)
        content = file_path.read_text()
        assert "- [!] TODO" in content


# =============================================================================
# Context Change Tests
# =============================================================================

class TestContextChange:
    """Tests for moving tasks between contexts."""

    def test_context_change_moves_between_files(self, task_manager):
        """Test that changing context moves task between files."""
        task = task_manager.create("Move me", context="ContextA")
        old_file = task_manager.tasks_dir / "ContextA.md"
        new_file = task_manager.tasks_dir / "ContextB.md"

        # Verify task is in old file
        assert "Move me" in old_file.read_text()

        # Change context
        task_manager.update(task.id, context="ContextB")

        # Verify task moved
        assert "Move me" not in old_file.read_text()
        assert "Move me" in new_file.read_text()

    def test_context_change_updates_source_file(self, task_manager):
        """Test that context change updates source_file field."""
        task = task_manager.create("Move me", context="ContextA")
        original_source = task.source_file

        task_manager.update(task.id, context="ContextB")
        updated = task_manager.get(task.id)

        assert updated.source_file != original_source
        assert "ContextB.md" in updated.source_file

    def test_context_change_adjusts_line_numbers(self, task_manager):
        """Test that context change adjusts line numbers in old file."""
        task1 = task_manager.create("Stay here", context="ContextA")
        task2 = task_manager.create("Move me", context="ContextA")
        task3 = task_manager.create("Also stay", context="ContextA")

        line1_before = task1.line_number
        line3_before = task3.line_number

        # Move task2 to different context
        task_manager.update(task2.id, context="ContextB")

        # task1 should keep same line number
        # task3 should have line number decreased
        task1_after = task_manager.get(task1.id)
        task3_after = task_manager.get(task3.id)

        assert task1_after.line_number == line1_before
        assert task3_after.line_number == line3_before - 1


# =============================================================================
# Reindex Tests
# =============================================================================

class TestReindexFile:
    """Tests for reindex_file method."""

    def test_reindex_after_external_edit(self, task_manager):
        """Test reindexing after manually editing file."""
        task = task_manager.create("Original task", context="ReindexTest")
        file_path = task_manager.tasks_dir / "ReindexTest.md"

        # Manually edit the file
        content = file_path.read_text()
        new_content = content.replace("Original task", "Externally edited task")
        file_path.write_text(new_content)

        # Reindex
        task_manager.reindex_file(str(file_path))

        # Verify index updated
        retrieved = task_manager.get(task.id)
        assert retrieved.description == "Externally edited task"

    def test_reindex_adds_new_tasks(self, task_manager):
        """Test that reindex adds new tasks found in file."""
        # Create file with task manually
        file_path = task_manager.tasks_dir / "ManualTest.md"
        file_path.write_text(
            "# Manual Tasks\n\n"
            "- [ ] TODO Manually added task [created:: 2025-02-08] <!-- id:manual123 -->\n"
        )

        # Reindex
        task_manager.reindex_file(str(file_path))

        # Verify task added to index
        task = task_manager.get("manual123")
        assert task is not None
        assert task.description == "Manually added task"

    def test_reindex_removes_deleted_tasks(self, task_manager):
        """Test that reindex removes tasks deleted from file."""
        task = task_manager.create("Delete from file", context="DeleteTest")
        file_path = task_manager.tasks_dir / "DeleteTest.md"

        # Manually delete task from file
        lines = file_path.read_text().splitlines()
        filtered_lines = [l for l in lines if "Delete from file" not in l]
        file_path.write_text("\n".join(filtered_lines) + "\n")

        # Reindex
        task_manager.reindex_file(str(file_path))

        # Verify task removed from index
        retrieved = task_manager.get(task.id)
        assert retrieved is None

    def test_reindex_handles_nonexistent_file(self, task_manager):
        """Test that reindex handles deleted file gracefully."""
        task = task_manager.create("File will be deleted", context="DeletedFile")
        file_path = task_manager.tasks_dir / "DeletedFile.md"

        # Delete the file
        file_path.unlink()

        # Reindex should remove tasks from that file
        task_manager.reindex_file(str(file_path))

        retrieved = task_manager.get(task.id)
        assert retrieved is None


class TestRebuildIndex:
    """Tests for rebuild_index method."""

    def test_rebuild_index_rescans_all_files(self, task_manager):
        """Test that rebuild_index rescans all task files."""
        # Create tasks in different contexts
        task1 = task_manager.create("Task 1", context="Context1")
        task2 = task_manager.create("Task 2", context="Context2")
        task3 = task_manager.create("Task 3", context="Context3")

        # Clear index
        task_manager._tasks.clear()
        task_manager._save_index()

        # Rebuild
        task_manager.rebuild_index()

        # Verify all tasks restored
        assert task_manager.get(task1.id) is not None
        assert task_manager.get(task2.id) is not None
        assert task_manager.get(task3.id) is not None

    def test_rebuild_index_skips_dashboard(self, task_manager):
        """Test that rebuild_index skips Dashboard.md."""
        # Verify Dashboard.md exists
        dashboard = task_manager.tasks_dir / "Dashboard.md"
        assert dashboard.exists()

        # Add a task-like line to Dashboard
        content = dashboard.read_text()
        content += "- [ ] TODO This should be ignored [created:: 2025-02-08] <!-- id:dashboard123 -->\n"
        dashboard.write_text(content)

        # Rebuild
        task_manager.rebuild_index()

        # Verify dashboard task not added to index
        task = task_manager.get("dashboard123")
        assert task is None


# =============================================================================
# Persistence Tests
# =============================================================================

class TestPersistence:
    """Tests for data persistence."""

    def test_persistence_across_instances(self, tmp_vault, tmp_index):
        """Test that tasks persist across manager instances."""
        # Create manager and add tasks
        manager1 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        task1 = manager1.create("Persistent task 1", context="Persist")
        task2 = manager1.create("Persistent task 2", context="Persist", priority="high")

        # Create new manager instance
        manager2 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)

        # Verify tasks loaded
        retrieved1 = manager2.get(task1.id)
        retrieved2 = manager2.get(task2.id)

        assert retrieved1 is not None
        assert retrieved1.description == "Persistent task 1"
        assert retrieved2 is not None
        assert retrieved2.priority == "high"

    def test_persistence_maintains_file_state(self, tmp_vault, tmp_index):
        """Test that markdown files persist correctly."""
        # Create manager and add task
        manager1 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        task = manager1.create("File persistence test", context="FileTest")

        # Read file directly
        file_path = manager1.tasks_dir / "FileTest.md"
        original_content = file_path.read_text()

        # Create new manager instance
        manager2 = TaskManager(vault_path=tmp_vault, index_path=tmp_index)

        # Verify file unchanged
        new_content = file_path.read_text()
        assert new_content == original_content

    def test_persistence_index_and_files_in_sync(self, tmp_vault, tmp_index):
        """Test that index and files stay synchronized."""
        # Create manager and add tasks
        manager = TaskManager(vault_path=tmp_vault, index_path=tmp_index)
        task1 = manager.create("Sync test 1", context="Sync")
        task2 = manager.create("Sync test 2", context="Sync")

        # Read index
        index_data = json.loads(tmp_index.read_text())
        index_tasks = {t["id"]: t for t in index_data["tasks"]}

        # Read file
        file_path = manager.tasks_dir / "Sync.md"
        file_content = file_path.read_text()

        # Verify both tasks in index
        assert task1.id in index_tasks
        assert task2.id in index_tasks

        # Verify both tasks in file
        assert "Sync test 1" in file_content
        assert "Sync test 2" in file_content


# =============================================================================
# Threading Tests
# =============================================================================

class TestThreading:
    """Tests for thread safety."""

    def test_concurrent_creates_are_threadsafe(self, task_manager):
        """Test that concurrent creates don't corrupt data."""
        import threading

        results = []

        def create_task(i):
            task = task_manager.create(f"Task {i}", context="Concurrent")
            results.append(task)

        threads = [threading.Thread(target=create_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify all tasks created
        assert len(results) == 10
        assert len(set(t.id for t in results)) == 10  # All unique IDs

    def test_concurrent_updates_are_threadsafe(self, task_manager):
        """Test that concurrent updates don't corrupt data."""
        import threading

        task = task_manager.create("Update test", context="Concurrent")

        def update_task(i):
            task_manager.update(task.id, priority=f"priority-{i}")

        threads = [threading.Thread(target=update_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify task still exists and is valid
        updated = task_manager.get(task.id)
        assert updated is not None
        assert updated.priority.startswith("priority-")


# =============================================================================
# Edge Cases Tests
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_create_with_empty_description(self, task_manager):
        """Test creating task with empty description."""
        task = task_manager.create("", context="Empty")
        assert task.description == ""

    def test_create_with_special_characters(self, task_manager):
        """Test creating task with special characters."""
        task = task_manager.create(
            "Task with 'quotes' and \"double quotes\" and #hashtags",
            context="Special"
        )
        assert task.description == "Task with 'quotes' and \"double quotes\" and #hashtags"

    def test_update_with_none_values_ignored(self, task_manager):
        """Test that update ignores None values."""
        task = task_manager.create("Test", priority="high")
        original_priority = task.priority

        task_manager.update(task.id, priority=None)
        updated = task_manager.get(task.id)

        # Priority should not change when set to None
        assert updated.priority == original_priority

    def test_list_with_invalid_status(self, task_manager):
        """Test listing with invalid status."""
        task_manager.create("Task 1")
        tasks = task_manager.list_tasks(status="invalid_status")

        # Should return empty list for invalid status
        assert tasks == []

    def test_context_file_creation_with_special_chars(self, task_manager):
        """Test that context files handle special characters."""
        task = task_manager.create("Test", context="Context-With-Dashes")
        file_path = task_manager.tasks_dir / "Context-With-Dashes.md"

        assert file_path.exists()

    def test_multiple_tasks_same_description(self, task_manager):
        """Test creating multiple tasks with same description."""
        task1 = task_manager.create("Duplicate", context="Dup")
        task2 = task_manager.create("Duplicate", context="Dup")

        assert task1.id != task2.id
        assert task1.description == task2.description

        tasks = task_manager.list_tasks()
        assert len(tasks) == 2
