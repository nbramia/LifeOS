"""
Task Manager for LifeOS.

Manages tasks as Obsidian Tasks plugin-compatible markdown in the vault.
Markdown files are source of truth; JSON index is a query cache.

Task line format (Dataview inline fields):
  - [ ] TODO Ask Zoe about HR issue [created:: 2025-02-07] #work #hr <!-- id:abc123 -->

Storage: LifeOS/Tasks/{Context}.md files in the vault
Index:   data/task_index.json for fast API queries
"""
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path("data/task_index.json")

# Status ↔ checkbox symbol mapping
STATUS_TO_SYMBOL = {
    "todo": " ",
    "done": "x",
    "in_progress": "/",
    "cancelled": "-",
    "deferred": ">",
    "blocked": "?",
    "urgent": "!",
}
SYMBOL_TO_STATUS = {v: k for k, v in STATUS_TO_SYMBOL.items()}

VALID_STATUSES = set(STATUS_TO_SYMBOL.keys())


@dataclass
class Task:
    """A task stored in the vault."""
    id: str
    description: str
    status: str = "todo"
    context: str = "Inbox"
    priority: str = ""  # high, medium, low, or ""
    due_date: Optional[str] = None  # YYYY-MM-DD
    created_date: str = ""  # YYYY-MM-DD
    done_date: Optional[str] = None  # YYYY-MM-DD
    cancelled_date: Optional[str] = None  # YYYY-MM-DD
    tags: list[str] = field(default_factory=list)
    reminder_id: Optional[str] = None
    source_file: str = ""
    line_number: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # Handle tags being stored as non-list
        if "tags" in data and not isinstance(data.get("tags"), list):
            data["tags"] = []
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def _today() -> str:
    return date.today().isoformat()


class TaskManager:
    """
    CRUD manager for tasks stored as Obsidian-compatible markdown.

    Markdown files in LifeOS/Tasks/ are the source of truth.
    data/task_index.json is a query cache rebuilt from markdown.
    """

    TASKS_FOLDER = "LifeOS/Tasks"

    def __init__(self, vault_path: Optional[Path] = None, index_path: Optional[Path] = None):
        self.vault_path = Path(vault_path) if vault_path else Path(settings.vault_path)
        self.index_path = Path(index_path) if index_path else DEFAULT_INDEX_PATH
        self.tasks_dir = self.vault_path / self.TASKS_FOLDER
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        self._load_index()
        self._ensure_dashboard()

    # ------------------------------------------------------------------
    # Index persistence
    # ------------------------------------------------------------------

    def _load_index(self):
        """Load index from disk, rebuild if missing or stale."""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text())
                for item in data.get("tasks", []):
                    task = Task.from_dict(item)
                    self._tasks[task.id] = task
                logger.info(f"Loaded {len(self._tasks)} tasks from index")
                return
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Error loading task index: {e}. Rebuilding.")
        self.rebuild_index()

    def _save_index(self):
        """Persist index to disk."""
        data = {
            "description": "LifeOS Task Index (cache — regenerated from vault markdown)",
            "last_updated": datetime.utcnow().isoformat(),
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        self.index_path.write_text(json.dumps(data, indent=2, default=str))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        description: str,
        context: str = "Inbox",
        priority: str = "",
        due_date: Optional[str] = None,
        tags: Optional[list[str]] = None,
        reminder_id: Optional[str] = None,
    ) -> Task:
        """Create a new task, append to context file, update index."""
        with self._lock:
            task = Task(
                id=uuid.uuid4().hex[:8],
                description=description,
                status="todo",
                context=context,
                priority=priority,
                due_date=due_date,
                created_date=_today(),
                tags=tags or [],
                reminder_id=reminder_id,
            )
            file_path = self._get_context_file(context)
            line = _format_task_line(task)
            _append_to_file(file_path, line)

            # Record source info
            task.source_file = str(file_path)
            task.line_number = _count_lines(file_path)

            self._tasks[task.id] = task
            self._save_index()
            logger.info(f"Created task {task.id}: {description}")
            return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def complete(self, task_id: str) -> Optional[Task]:
        """Mark a task as done."""
        return self.update(task_id, status="done")

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
        """Update a task. Supports: description, status, context, priority, due_date, tags."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            old_context = task.context
            new_context = kwargs.get("context", old_context)

            # Apply updates
            for key, value in kwargs.items():
                if key == "status" and value == "done" and task.status != "done":
                    task.done_date = _today()
                elif key == "status" and value == "cancelled" and task.status != "cancelled":
                    task.cancelled_date = _today()
                if hasattr(task, key) and value is not None:
                    setattr(task, key, value)

            # Context change = move between files
            if new_context != old_context:
                self._move_task_between_files(task, old_context, new_context)
            else:
                # Rewrite line in place
                new_line = _format_task_line(task)
                _replace_line_in_file(Path(task.source_file), task.line_number, new_line)

            self._save_index()
            return task

    def delete(self, task_id: str) -> bool:
        """Remove a task from its file and index."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            _remove_line_from_file(Path(task.source_file), task.line_number)

            # Adjust line numbers for tasks in same file after deleted line
            for t in self._tasks.values():
                if t.source_file == task.source_file and t.line_number > task.line_number:
                    t.line_number -= 1

            del self._tasks[task_id]
            self._save_index()
            logger.info(f"Deleted task {task_id}")
            return True

    def list_tasks(
        self,
        status: Optional[str] = None,
        context: Optional[str] = None,
        tag: Optional[str] = None,
        due_before: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[Task]:
        """Filter and return tasks. `query` does fuzzy matching on description."""
        results = list(self._tasks.values())

        if status:
            results = [t for t in results if t.status == status]
        if context:
            results = [t for t in results if t.context.lower() == context.lower()]
        if tag:
            tag_lower = tag.lower().lstrip("#")
            results = [t for t in results if any(tg.lower().lstrip("#") == tag_lower for tg in t.tags)]
        if due_before:
            results = [t for t in results if t.due_date and t.due_date <= due_before]

        if query:
            results = _fuzzy_filter(results, query)

        return results

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    def reindex_file(self, file_path: str):
        """Parse a single task file and update index entries for it."""
        path = Path(file_path)
        if not path.exists():
            # File was deleted — remove tasks from index
            with self._lock:
                to_remove = [tid for tid, t in self._tasks.items() if t.source_file == str(path)]
                for tid in to_remove:
                    del self._tasks[tid]
                if to_remove:
                    self._save_index()
            return

        with self._lock:
            # Remove old entries for this file
            to_remove = [tid for tid, t in self._tasks.items() if t.source_file == str(path)]
            for tid in to_remove:
                del self._tasks[tid]

            # Parse file
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                logger.warning(f"Could not read {file_path}: {e}")
                return

            for line_num, line in enumerate(lines, start=1):
                task = _parse_task_line(line, str(path), line_num)
                if task:
                    self._tasks[task.id] = task

            self._save_index()

    def rebuild_index(self):
        """Full re-parse of all LifeOS/Tasks/*.md files."""
        self._tasks.clear()
        if not self.tasks_dir.exists():
            self._save_index()
            return

        for md_file in sorted(self.tasks_dir.glob("*.md")):
            if md_file.name == "Dashboard.md":
                continue
            try:
                lines = md_file.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                logger.warning(f"Could not read {md_file}: {e}")
                continue
            for line_num, line in enumerate(lines, start=1):
                task = _parse_task_line(line, str(md_file), line_num)
                if task:
                    self._tasks[task.id] = task

        self._save_index()
        logger.info(f"Rebuilt task index: {len(self._tasks)} tasks")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_context_file(self, context: str) -> Path:
        """Return path to context file, creating with template if missing."""
        file_path = self.tasks_dir / f"{context}.md"
        if not file_path.exists():
            template = (
                f"---\ntype: tasks\ncontext: {context.lower()}\n---\n"
                f"# {context} Tasks\n\n"
            )
            file_path.write_text(template, encoding="utf-8")
        return file_path

    def _move_task_between_files(self, task: Task, old_context: str, new_context: str):
        """Remove from old file, append to new file."""
        old_path = Path(task.source_file)
        _remove_line_from_file(old_path, task.line_number)

        # Adjust line numbers for tasks in old file after removed line
        for t in self._tasks.values():
            if t.source_file == str(old_path) and t.line_number > task.line_number:
                t.line_number -= 1

        new_path = self._get_context_file(new_context)
        new_line = _format_task_line(task)
        _append_to_file(new_path, new_line)

        task.source_file = str(new_path)
        task.line_number = _count_lines(new_path)
        task.context = new_context

    def _ensure_dashboard(self):
        """Create Dashboard.md if missing."""
        dashboard = self.tasks_dir / "Dashboard.md"
        if dashboard.exists():
            return
        content = """---
type: dashboard
---
# Task Dashboard

## All Open
```tasks
not done
path includes LifeOS/Tasks
sort by due
group by filename
```

## Due This Week
```tasks
not done
path includes LifeOS/Tasks
due before next week
sort by due
```

## In Progress
```tasks
status.name includes In Progress
path includes LifeOS/Tasks
```

## Blocked
```tasks
status.name includes Blocked
path includes LifeOS/Tasks
```

## Recently Completed
```tasks
done
path includes LifeOS/Tasks
done after 7 days ago
sort by done reverse
```
"""
        dashboard.write_text(content, encoding="utf-8")
        logger.info("Created Tasks Dashboard.md")


# ======================================================================
# Module-level helpers (pure functions)
# ======================================================================

# Regex for parsing a task line
_TASK_LINE_RE = re.compile(
    r'^- \[(.)\] TODO\s+'   # checkbox + TODO keyword
    r'(.+?)'                # description (non-greedy)
    r'\s*$'                  # end of line
)

_INLINE_FIELD_RE = re.compile(r'\[(\w+)::\s*([^\]]*)\]')
_TAG_RE = re.compile(r'#([\w-]+)')
_ID_RE = re.compile(r'<!--\s*id:(\w+)\s*-->')


def _format_task_line(task: Task) -> str:
    """Task → Dataview format markdown line."""
    symbol = STATUS_TO_SYMBOL.get(task.status, " ")
    parts = [f"- [{symbol}] TODO {task.description}"]

    # Inline fields
    if task.due_date:
        parts.append(f"[due:: {task.due_date}]")
    if task.priority:
        parts.append(f"[priority:: {task.priority}]")
    parts.append(f"[created:: {task.created_date}]")
    if task.done_date:
        parts.append(f"[done:: {task.done_date}]")
    if task.cancelled_date:
        parts.append(f"[cancelled:: {task.cancelled_date}]")

    # Tags
    for tag in task.tags:
        t = tag if tag.startswith("#") else f"#{tag}"
        parts.append(t)

    # ID comment
    parts.append(f"<!-- id:{task.id} -->")

    return " ".join(parts)


def _parse_task_line(line: str, file_path: str, line_num: int) -> Optional[Task]:
    """Parse one checkbox line into a Task, or None if not a task line."""
    # Must be a checkbox line with TODO keyword
    m = re.match(r'^- \[(.)\]\s+TODO\s+', line)
    if not m:
        return None

    symbol = m.group(1)
    status = SYMBOL_TO_STATUS.get(symbol, "todo")

    # Extract the rest after "- [x] TODO "
    rest = line[m.end():]

    # Extract ID
    id_match = _ID_RE.search(rest)
    task_id = id_match.group(1) if id_match else uuid.uuid4().hex[:8]

    # Extract inline fields
    fields = {}
    for fm in _INLINE_FIELD_RE.finditer(rest):
        fields[fm.group(1)] = fm.group(2).strip()

    # Extract tags
    tags = _TAG_RE.findall(rest)

    # Description = everything minus inline fields, tags, and ID comment
    desc = rest
    desc = _ID_RE.sub("", desc)
    desc = _INLINE_FIELD_RE.sub("", desc)
    desc = re.sub(r'#[\w-]+', '', desc)
    desc = desc.strip()

    # Infer context from filename
    context = Path(file_path).stem

    return Task(
        id=task_id,
        description=desc,
        status=status,
        context=context,
        priority=fields.get("priority", ""),
        due_date=fields.get("due") or None,
        created_date=fields.get("created", ""),
        done_date=fields.get("done") or None,
        cancelled_date=fields.get("cancelled") or None,
        tags=tags,
        source_file=file_path,
        line_number=line_num,
    )


def _append_to_file(path: Path, line: str):
    """Append a task line to file, ensuring newline before if needed."""
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if content and not content.endswith("\n"):
        content += "\n"
    content += line + "\n"
    path.write_text(content, encoding="utf-8")


def _replace_line_in_file(path: Path, line_num: int, new_line: str):
    """Replace a specific line (1-indexed) in a file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if 1 <= line_num <= len(lines):
        lines[line_num - 1] = new_line
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_line_from_file(path: Path, line_num: int):
    """Remove a specific line (1-indexed) from a file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if 1 <= line_num <= len(lines):
        del lines[line_num - 1]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _count_lines(path: Path) -> int:
    """Count lines in a file."""
    return len(path.read_text(encoding="utf-8").splitlines())


def _fuzzy_filter(tasks: list[Task], query: str) -> list[Task]:
    """Filter tasks by fuzzy matching on description."""
    query_lower = query.lower()

    # First: exact substring matches (always include)
    exact = [t for t in tasks if query_lower in t.description.lower()]
    exact_ids = {t.id for t in exact}

    # Second: fuzzy matches via rapidfuzz
    try:
        from rapidfuzz.fuzz import partial_ratio
        fuzzy = []
        for t in tasks:
            if t.id in exact_ids:
                continue
            score = partial_ratio(query_lower, t.description.lower())
            if score >= 60:
                fuzzy.append((t, score))
        fuzzy.sort(key=lambda x: x[1], reverse=True)
        return exact + [t for t, _ in fuzzy]
    except ImportError:
        return exact


# ======================================================================
# Singleton
# ======================================================================

_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """Get or create TaskManager singleton."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
