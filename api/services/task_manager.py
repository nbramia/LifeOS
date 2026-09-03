"""
Task Manager for LifeOS.

Manages tasks as Obsidian Tasks plugin-compatible markdown in the vault.
Markdown files are source of truth; JSON index is a query cache.

Task line format (Dataview inline fields):
  - [ ] TODO Ask Zoe about HR issue [created:: 2025-02-07] #work #hr <!-- id:abc123 -->

Storage: LifeOS/Tasks/{Context}.md files in the vault
Index:   data/task_index.json for fast API queries

Tasks are addressed by their ``<!-- id:xxxx -->`` comment, not by cached line
number — a write locates its task's block by id on every mutation, exactly
like ``scheduler_store.py`` locates schedule blocks. This is what lets a task
survive an external edit that inserts lines above it before the file watcher
reindexes. ``line_number``/``source_file`` on ``Task`` remain informational
(refreshed after every write) but are never used to address a write.

See docs/specs/technical/task-management.md for the full design.
"""
import copy
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from config.settings import settings
from api.services.atomic_write import atomic_write_text, atomic_write_lines

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "task_index.json"

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

# Inline fields with a dedicated Task attribute — everything else parsed from
# `[key:: value]` lands in `Task.fields` and round-trips untouched, which is
# how operator fields (host, effort, model, key) and any future field survive
# without parser changes.
_KNOWN_FIELD_KEYS = {"due", "priority", "created", "done", "cancelled", "updated"}

# Field keys that already address a dedicated Task attribute (or the id
# comment itself) — accepting them through the free-form `fields` dict would
# let a caller write a second, conflicting `[key:: value]` onto the line, or
# forge `[id:: ...]`/`[updated:: ...]` outright. Rejected by
# `_validate_text_fields`.
_RESERVED_FIELD_KEYS = _KNOWN_FIELD_KEYS | {"id"}
_FIELD_KEY_RE = re.compile(r"^\w+$")

# Retries after an initial write attempt that loses a compare-and-swap race
# against a concurrent external edit (see TaskManager._cas_rewrite).
_CAS_MAX_RETRIES = 3


def _validate_text_fields(
    description: Optional[str] = None,
    notes: Optional[str] = None,
    fields: Optional[dict] = None,
) -> None:
    """Reject description/notes/fields content that would corrupt the task
    line's format or hijack another task's id comment when written back.

    A newline in `description` or a `fields` value would split the single
    checkbox line; a `]` would truncate an inline field and leak the rest
    into the description; an HTML comment opener (`<!--`) could forge a new
    `<!-- id:.. -->`, hijacking another task's id on the next reindex. Notes
    lines are allowed to be multi-line (that's their whole point) but not
    `\\r` (would desync from the `\\n`-joined body) or `<!--`. A `fields` key
    must be a bare word (`_format_task_line` interpolates it directly as
    `[key:: value]`) and must not shadow a reserved key (see
    `_RESERVED_FIELD_KEYS`).

    Raises `ValueError` — never silently truncates or strips, since that
    would surprise the caller by saving something other than what was sent.
    The route layer maps `ValueError` to HTTP 422.
    """
    if description is not None:
        for bad in ("\n", "\r", "]", "<!--"):
            if bad in description:
                raise ValueError(f"description must not contain {bad!r}")
    if notes is not None:
        for line in notes.split("\n"):
            for bad in ("\r", "<!--"):
                if bad in line:
                    raise ValueError(f"notes must not contain {bad!r}")
    if fields:
        for key, value in fields.items():
            if not _FIELD_KEY_RE.match(key):
                raise ValueError(f"invalid fields key {key!r}: must match ^\\w+$")
            if key in _RESERVED_FIELD_KEYS:
                raise ValueError(f"'{key}' is a reserved field and cannot be set via fields")
            if value is None:
                continue
            for bad in ("\n", "\r", "]", "<!--"):
                if bad in value:
                    raise ValueError(f"fields[{key!r}] must not contain {bad!r}")


class TaskConflictError(Exception):
    """Raised when a write loses the compare-and-swap race against a
    concurrent external edit `_CAS_MAX_RETRIES` times in a row. The route
    layer maps this to HTTP 409."""


class _TagAbsentError(Exception):
    """Internal signal: swap_tag's `from_tag` is no longer present after a
    CAS retry re-read the task (e.g. someone else already swapped it)."""


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
    updated_at: Optional[str] = None  # ISO-8601 with UTC offset
    tags: list[str] = field(default_factory=list)
    reminder_id: Optional[str] = None
    notes: Optional[str] = None
    fields: dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    line_number: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # Handle tags being stored as non-list
        if "tags" in data and not isinstance(data.get("tags"), list):
            data["tags"] = []
        if "fields" in data and not isinstance(data.get("fields"), dict):
            data["fields"] = {}
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def _today() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    """Current time as ISO-8601 with an explicit UTC offset."""
    return datetime.now(timezone.utc).isoformat()


def _mtime_or_none(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _read_lines(path: Path) -> list[str]:
    if path.exists():
        return path.read_text(encoding="utf-8").splitlines()
    return []


def _read_lines_with_terminator(path: Path) -> tuple[list[str], str, bool]:
    """Read `path` split into lines (terminators stripped) plus its detected
    line terminator and whether it ended with a trailing terminator — both
    needed to preserve a file's original formatting across a rewrite that
    only touches a few lines. Reads raw bytes (not `Path.read_text`, whose
    text-mode universal-newline translation would silently turn CRLF into
    LF before we ever got a look at it). Missing/empty file → ([], "\n",
    True), matching the append-a-trailing-newline default most vault files
    already have."""
    if not path.exists():
        return [], "\n", True
    raw = path.read_bytes()
    if not raw:
        return [], "\n", True
    newline = "\r\n" if b"\r\n" in raw else "\n"
    trailing_newline = raw.endswith(newline.encode("ascii"))
    return raw.decode("utf-8").splitlines(), newline, trailing_newline


# Syncthing conflict copies and temp files: never indexed as a task source,
# never trigger or survive a reindex. Exposed read-only via list_conflicts().
# Matches the substring regardless of the timestamp/suffix that follows it,
# per the criterion "*.sync-conflict-*" (not just Syncthing's own format).
_CONFLICT_RE = re.compile(r"\.sync-conflict-")


def is_conflict_file(path: Path) -> bool:
    name = path.name
    return bool(_CONFLICT_RE.search(name)) or name.startswith(".syncthing.")


class TaskManager:
    """
    CRUD manager for tasks stored as Obsidian-compatible markdown.

    Markdown files in LifeOS/Tasks/ are the source of truth.
    data/task_index.json is a query cache rebuilt from markdown.

    Writes locate their task's block by id (`_find_task_block_span`) rather
    than by cached line number, so a concurrent external edit that shifts
    line numbers can't misdirect a write — mirroring `SchedulerStore`. Each
    write is protected by a compare-and-swap on the file's mtime
    (`_cas_rewrite`): if the mtime changes between our read and our rename,
    someone else wrote to the file in between, so we reindex (absorbing
    their change) and retry, up to `_CAS_MAX_RETRIES` times, before raising
    `TaskConflictError`.
    """

    TASKS_FOLDER = "LifeOS/Tasks"

    def __init__(self, vault_path: Optional[Path] = None, index_path: Optional[Path] = None):
        self.vault_path = Path(vault_path) if vault_path else Path(settings.vault_path)
        self.index_path = Path(index_path) if index_path else DEFAULT_INDEX_PATH
        self.tasks_dir = self.vault_path / self.TASKS_FOLDER
        self._tasks: dict[str, Task] = {}
        # Exact raw main-line text last written or seen for each task id,
        # this process's lifetime only (never persisted — see docs/specs/
        # technical/task-management.md "External-edit detection" for why a
        # sidecar file isn't needed). Used to tell a genuine external edit
        # apart from our own prior write echoing back through the watcher;
        # comparing against this exact string (rather than reformatting the
        # prior Task and hoping it matches) is what keeps a hand-authored
        # line's exact formatting — no "TODO", no created date — stable
        # across repeated reindexes instead of getting rewritten every time.
        self._last_written_line: dict[str, str] = {}
        # Reentrant: a CAS retry inside a mutating call (create/update/
        # swap_tag/delete, all lock-held) invokes reindex_file(), which also
        # takes this lock — a plain Lock would self-deadlock on that retry.
        self._lock = threading.RLock()

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        self._load_index()
        self._write_dashboard()

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
            "last_updated": _now_iso(),
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        atomic_write_text(self.index_path, json.dumps(data, indent=2, default=str))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        description: str,
        context: str = "Inbox",
        status: str = "todo",
        priority: str = "",
        due_date: Optional[str] = None,
        tags: Optional[list[str]] = None,
        reminder_id: Optional[str] = None,
        notes: Optional[str] = None,
        fields: Optional[dict[str, str]] = None,
    ) -> Task:
        """Create a new task at the top of its context file, update index.

        Raises `ValueError` (-> HTTP 422 at the route layer) for an
        unrecognized `status`, or for `description`/`notes`/`fields` content
        that would corrupt the task line or hijack another task's id — see
        `_validate_text_fields`.
        """
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            )
        _validate_text_fields(description=description, notes=notes, fields=fields)
        with self._lock:
            task = Task(
                id=uuid.uuid4().hex[:8],
                description=description,
                status=status or "todo",
                context=context,
                priority=priority,
                due_date=due_date,
                created_date=_today(),
                updated_at=_now_iso(),
                tags=tags or [],
                reminder_id=reminder_id,
                notes=notes,
                fields=dict(fields) if fields else {},
            )
            if task.status == "done" and not task.done_date:
                task.done_date = _today()
            if task.status == "cancelled" and not task.cancelled_date:
                task.cancelled_date = _today()

            file_path = self._get_context_file(context)
            block = _format_task_block(task)
            start_line = self._cas_insert_at_top(file_path, block)

            task.source_file = str(file_path)
            task.line_number = start_line

            self._tasks[task.id] = task
            self._last_written_line[task.id] = block[0]
            self._reposition_file(file_path)
            self._save_index()
            self._write_dashboard()
            logger.info(f"Created task {task.id}: {description}")
            return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def complete(self, task_id: str) -> Optional[Task]:
        """Mark a task as done."""
        return self.update(task_id, status="done")

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
        """Update a task. Supports: description, status, context, priority,
        due_date, tags, notes, fields. `fields={"k": None}` removes key `k`;
        `fields={"k": "v"}` sets it. Raises `TaskConflictError` (-> HTTP 409)
        if the write keeps losing the CAS race against a concurrent edit;
        `ValueError` (-> HTTP 422) for an unrecognized `status` or hostile
        `description`/`notes`/`fields` content — see `_validate_text_fields`.
        """
        fields_patch = kwargs.pop("fields", None)
        if "status" in kwargs and kwargs["status"] is not None and kwargs["status"] not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{kwargs['status']}'. "
                f"Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            )
        _validate_text_fields(
            description=kwargs.get("description"), notes=kwargs.get("notes"), fields=fields_patch
        )
        with self._lock:
            current = self._tasks.get(task_id)
            if not current:
                return None

            old_context = current.context
            new_context = kwargs.get("context", old_context)

            def apply(t: Task) -> Task:
                # Copy first — `t` is `self._tasks[task_id]` itself, and this
                # closure is invoked fresh on every CAS retry via `compute()`
                # (see `_cas_rewrite`). Mutating it in place would make a
                # losing attempt's edits visible through `self.get(task_id)`
                # immediately, even though nothing was ever persisted; only
                # the CAS success branch below may rebind `self._tasks`.
                t = copy.copy(t)
                for key, value in kwargs.items():
                    if key == "status" and value == "done" and t.status != "done":
                        t.done_date = _today()
                    elif key == "status" and value == "cancelled" and t.status != "cancelled":
                        t.cancelled_date = _today()
                    if hasattr(t, key) and value is not None:
                        setattr(t, key, value)
                if fields_patch:
                    merged = dict(t.fields)
                    for k, v in fields_patch.items():
                        if v is None:
                            merged.pop(k, None)
                        else:
                            merged[k] = v
                    t.fields = merged
                t.updated_at = _now_iso()
                return t

            if new_context != old_context:
                task = apply(current)
                moved = self._move_task_between_files(task, old_context, new_context)
                if not moved:
                    # Task's block is no longer in its source file (an
                    # external delete raced us) — reconcile like the
                    # same-context branch does for `found=False` below,
                    # rather than raising.
                    self._tasks.pop(task_id, None)
                    self._last_written_line.pop(task_id, None)
                    self._save_index()
                    self._write_dashboard()
                    return None
                self._tasks[task_id] = task
            else:
                path = Path(current.source_file)

                def compute() -> Task:
                    return apply(self._tasks[task_id])

                found, task = self._cas_rewrite(path, task_id, compute)
                if not found:
                    self._tasks.pop(task_id, None)
                    self._last_written_line.pop(task_id, None)
                    self._save_index()
                    self._write_dashboard()
                    return None
                self._reposition_file(path)

            self._save_index()
            self._write_dashboard()
            return task

    def swap_tag(self, task_id: str, from_tag: str, to_tag: str) -> bool:
        """Atomically replace `from_tag` with `to_tag` on a task.

        Returns True if the swap happened, False if either the task is gone
        or `from_tag` is not present (already claimed / re-tagged).

        Tags are compared with the leading `#` stripped, case-insensitively, to
        match the rest of the codebase. The stored representation follows the
        existing convention (no `#` prefix in `Task.tags`).
        """
        from_norm = from_tag.lstrip("#").lower()
        to_norm = to_tag.lstrip("#")  # preserve operator-provided case
        with self._lock:
            current = self._tasks.get(task_id)
            if not current:
                return False
            if not any(t.lstrip("#").lower() == from_norm for t in current.tags):
                return False

            path = Path(current.source_file)

            def compute() -> Task:
                t = self._tasks[task_id]
                try:
                    idx = next(
                        i for i, tag in enumerate(t.tags)
                        if tag.lstrip("#").lower() == from_norm
                    )
                except StopIteration:
                    raise _TagAbsentError()
                # Copy first, same reasoning as `update.apply` — `t` is
                # `self._tasks[task_id]` itself, and this closure runs fresh
                # on every CAS retry; only the success branch in
                # `_cas_rewrite` may rebind `self._tasks`.
                new_task = copy.copy(t)
                new_tags = list(t.tags)
                new_tags[idx] = to_norm
                new_task.tags = new_tags
                new_task.updated_at = _now_iso()
                return new_task

            try:
                found, task = self._cas_rewrite(path, task_id, compute)
            except _TagAbsentError:
                return False
            if not found:
                self._tasks.pop(task_id, None)
                self._last_written_line.pop(task_id, None)
                self._save_index()
                self._write_dashboard()
                return False

            self._reposition_file(path)
            self._save_index()
            self._write_dashboard()
            logger.info(f"swap_tag {task_id}: {from_norm} → {to_norm}")
            return True

    def delete(self, task_id: str) -> bool:
        """Remove a task (and any notes body) from its file and index."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            path = Path(task.source_file)
            self._cas_rewrite(path, task_id, lambda: None)
            self._tasks.pop(task_id, None)
            self._last_written_line.pop(task_id, None)
            self._reposition_file(path)
            self._save_index()
            self._write_dashboard()
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

    def list_tags(self) -> list[dict]:
        """Return distinct tags across all tasks with usage counts, sorted by count desc then name."""
        counts: dict[str, int] = {}
        for task in self._tasks.values():
            for tag in task.tags:
                normalized = tag.lstrip("#")
                if not normalized:
                    continue
                counts[normalized] = counts.get(normalized, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        ]

    def list_conflicts(self) -> list[dict]:
        """List Syncthing conflict/temp files sitting in the tasks folder.

        Never indexed as tasks and never reindexed — surfaced here so a
        client (the board) can warn the operator to resolve them by hand.
        """
        if not self.tasks_dir.exists():
            return []
        results = []
        for p in self.tasks_dir.iterdir():
            if p.is_file() and is_conflict_file(p):
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
                except OSError:
                    continue
                results.append({"name": p.name, "mtime": mtime})
        results.sort(key=lambda r: r["mtime"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    def reindex_file(self, file_path: str):
        """Parse a single task file and update index entries for it.

        Also performs id write-back (a task line lacking `<!-- id:.. -->`
        gets one appended, minimally, with every other byte of the line and
        every non-task line untouched) and external-edit detection (a task
        line that no longer matches what the API last wrote gets a fresh
        `[updated::]` stamp; every other line is left alone). The write-back
        (if any) is itself CAS-protected on the file's mtime, bounded to
        `_CAS_MAX_RETRIES` re-read-and-reparse attempts; on persistent
        conflict it logs a warning and skips the write for this pass rather
        than raising — the watcher will fire again for whatever caused the
        conflict.
        """
        path = Path(file_path)
        # Dashboard.md is auto-generated; never index it as a task source.
        # (Also prevents a watcher feedback loop when we regenerate it below.)
        if path.name == "Dashboard.md":
            return
        if is_conflict_file(path):
            return

        if not path.exists():
            # File was deleted — remove tasks from index
            with self._lock:
                to_remove = [tid for tid, t in self._tasks.items() if t.source_file == str(path)]
                for tid in to_remove:
                    del self._tasks[tid]
                    self._last_written_line.pop(tid, None)
                if to_remove:
                    self._save_index()
                    self._write_dashboard()
            return

        with self._lock:
            file_tasks: dict[str, Task] = {}
            for attempt in range(_CAS_MAX_RETRIES + 1):
                mtime_before = _mtime_or_none(path)
                try:
                    lines, newline, trailing_newline = _read_lines_with_terminator(path)
                except Exception as e:
                    logger.warning(f"Could not read {file_path}: {e}")
                    return

                new_lines, file_tasks, rewrite_needed = self._reparse_lines(
                    lines, str(path), self._tasks
                )
                if not rewrite_needed:
                    break

                mtime_now = _mtime_or_none(path)
                if mtime_now == mtime_before:
                    atomic_write_lines(path, new_lines, newline=newline, trailing_newline=trailing_newline)
                    break
                if attempt >= _CAS_MAX_RETRIES:
                    logger.warning(
                        f"reindex_file: persistent write conflict on {file_path}; "
                        "skipping the id/restamp write-back this pass"
                    )
                    break
                # Someone else wrote in between — re-read and re-parse
                # against the freshest content on the next attempt.

            # Only tasks NOT present in this parse are gone from this file —
            # everything `_reparse_lines` just repopulated must survive, or
            # every reindex would immediately forget the external edit it
            # was meant to detect (a task restamped this pass would vanish
            # from the index a moment after being restamped).
            to_remove = [
                tid for tid, t in self._tasks.items()
                if t.source_file == str(path) and tid not in file_tasks
            ]
            for tid in to_remove:
                del self._tasks[tid]
                self._last_written_line.pop(tid, None)
            self._tasks.update(file_tasks)

            self._save_index()
            self._write_dashboard()

    def rebuild_index(self):
        """Full re-parse of all LifeOS/Tasks/*.md files."""
        prior = self._tasks
        rebuilt: dict[str, Task] = {}
        if self.tasks_dir.exists():
            for md_file in sorted(self.tasks_dir.glob("*.md")):
                if md_file.name == "Dashboard.md" or is_conflict_file(md_file):
                    continue
                try:
                    lines, newline, trailing_newline = _read_lines_with_terminator(md_file)
                except Exception as e:
                    logger.warning(f"Could not read {md_file}: {e}")
                    continue
                new_lines, file_tasks, rewrite_needed = self._reparse_lines(lines, str(md_file), prior)
                if rewrite_needed:
                    atomic_write_lines(md_file, new_lines, newline=newline, trailing_newline=trailing_newline)
                rebuilt.update(file_tasks)

        self._tasks = rebuilt
        self._last_written_line = {
            tid: line for tid, line in self._last_written_line.items() if tid in rebuilt
        }
        self._save_index()
        self._write_dashboard()
        logger.info(f"Rebuilt task index: {len(self._tasks)} tasks")

    def _reparse_lines(
        self, lines: list[str], file_path: str, prior: dict[str, Task]
    ) -> tuple[list[str], dict[str, Task], bool]:
        """Parse `lines` (belonging to `file_path`) into fresh tasks.

        Returns `(new_lines, tasks_for_this_file, rewrite_needed)`. Must be
        called with `self._lock` held (reads `prior` for reminder_id merge-
        forward, and `self._last_written_line` for external-edit detection).
        A task lacking an id comment gets one appended to its raw line only
        — nothing else about that line changes. A task whose raw line
        differs from `self._last_written_line[task.id]` (the exact text we
        last wrote or saw for it) is treated as an external edit: the parsed
        values win and the line is rewritten with a fresh `[updated::]`
        stamp. A task id with no prior record at all (first time this
        process has seen it) is taken as-is, no restamp — there is nothing
        to compare against. Every other line — task or not — is copied
        through byte-for-byte. A checkbox line inside a fenced (``` or ~~~)
        code block is never a task line (see `_iter_lines_with_fence_state`).
        A second block in this same file carrying an id already claimed
        earlier in this parse is treated as if it had no id at all — its
        stale/duplicated comment is replaced with a freshly minted one,
        rather than left to fight the first occurrence for the same id on
        every future parse.
        """
        out_lines: list[str] = []
        file_tasks: dict[str, Task] = {}
        rewrite_needed = False
        fence = dict(_iter_lines_with_fence_state(lines))
        idx, n = 0, len(lines)
        while idx < n:
            if fence.get(idx):
                out_lines.append(lines[idx])
                idx += 1
                continue
            block = _match_task_block(lines, idx, file_path)
            if block is None:
                out_lines.append(lines[idx])
                idx += 1
                continue
            end, task, had_id = block
            raw_main_line = lines[idx]
            body_lines = lines[idx + 1:end]

            if had_id and task.id in file_tasks:
                had_id = False
                task.id = uuid.uuid4().hex[:8]
                raw_main_line = _ID_RE.sub("", raw_main_line).rstrip()

            if had_id:
                prior_task = prior.get(task.id)
                if prior_task is not None and prior_task.reminder_id is not None and task.reminder_id is None:
                    task.reminder_id = prior_task.reminder_id
                last_written = self._last_written_line.get(task.id)
                if last_written is not None and last_written != raw_main_line:
                    task.updated_at = _now_iso()
                    new_main_line = _format_task_line(task)
                    out_lines.append(new_main_line)
                    out_lines.extend(body_lines)
                    rewrite_needed = True
                    self._last_written_line[task.id] = new_main_line
                else:
                    out_lines.append(raw_main_line)
                    out_lines.extend(body_lines)
                    self._last_written_line[task.id] = raw_main_line
            else:
                new_main_line = raw_main_line.rstrip("\n") + f" <!-- id:{task.id} -->"
                out_lines.append(new_main_line)
                out_lines.extend(body_lines)
                rewrite_needed = True
                self._last_written_line[task.id] = new_main_line

            file_tasks[task.id] = task
            idx = end

        return out_lines, file_tasks, rewrite_needed

    # ------------------------------------------------------------------
    # Compare-and-swap file writes (id-addressed)
    # ------------------------------------------------------------------

    def _cas_rewrite(
        self, path: Path, task_id: str, compute: Callable[[], Optional[Task]]
    ) -> tuple[bool, Optional[Task]]:
        """Rewrite (or delete) the block for `task_id` in `path`.

        `compute()` is invoked fresh on every attempt against the current
        `self._tasks[task_id]` and returns the `Task` to write, or `None` to
        delete the block. Protected by compare-and-swap on the file's mtime:
        read mtime, read+locate the block, compute the new content, then
        check the mtime again right before writing. A mismatch means a
        concurrent external writer touched the file in between — reindex
        (absorbing their change) and retry, up to `_CAS_MAX_RETRIES` times,
        then raise `TaskConflictError`.

        Returns `(found, task_or_none)`. `found=False` means the task's block
        was not present in the file at all (e.g. externally deleted) — not a
        CAS conflict, so the caller should reconcile rather than retry.

        Before computing the replacement, also checks whether the on-disk
        block reflects an edit `reindex_file` hasn't absorbed yet — the raw
        line differs from the last line the API wrote/saw, or the on-disk
        notes body differs from the in-memory task's. Under the watcher's 2s
        debounce this is the normal case for an edit that just landed, not a
        rare race: without this check `compute()` would build its
        replacement from stale in-memory state and silently overwrite the
        edit. On either mismatch, absorb it via `reindex_file` and retry
        (counts toward `_CAS_MAX_RETRIES`) instead of computing against
        stale state.
        """
        for attempt in range(_CAS_MAX_RETRIES + 1):
            mtime_before = _mtime_or_none(path)
            lines, newline, trailing_newline = _read_lines_with_terminator(path)
            span = _find_task_block_span(lines, task_id)
            if span is None:
                return False, None
            start, end = span

            if self._external_edit_pending(lines, start, task_id):
                if attempt < _CAS_MAX_RETRIES:
                    self.reindex_file(str(path))
                    continue
                raise TaskConflictError(f"Too many conflicting writes to {path}")

            result = compute()
            new_block = _format_task_block(result) if result is not None else []
            new_lines = lines[:start] + new_block + lines[end:]
            mtime_now = _mtime_or_none(path)
            if mtime_now == mtime_before:
                atomic_write_lines(path, new_lines, newline=newline, trailing_newline=trailing_newline)
                if result is not None:
                    self._tasks[task_id] = result
                    self._last_written_line[task_id] = new_block[0]
                else:
                    self._last_written_line.pop(task_id, None)
                return True, result
            if attempt < _CAS_MAX_RETRIES:
                self.reindex_file(str(path))
                continue
            raise TaskConflictError(f"Too many conflicting writes to {path}")
        raise TaskConflictError(f"Too many conflicting writes to {path}")

    def _external_edit_pending(self, lines: list[str], start: int, task_id: str) -> bool:
        """True if the on-disk block at `lines[start]` carries an edit
        `_cas_rewrite` hasn't absorbed into `self._tasks` yet: the raw line
        text no longer matches the last line the API wrote or saw for this
        id, or the on-disk notes body no longer matches the in-memory
        task's. Must be called with `self._lock` held."""
        last_written = self._last_written_line.get(task_id)
        if last_written is not None and lines[start] != last_written:
            return True
        in_memory = self._tasks.get(task_id)
        if in_memory is not None:
            on_disk_block = _match_task_block(lines, start)
            if on_disk_block is not None:
                _, on_disk_task, _ = on_disk_block
                if on_disk_task.notes != in_memory.notes:
                    return True
        return False

    def _cas_insert_at_top(self, path: Path, block: list[str]) -> int:
        """Insert `block` above the first existing task block. Returns its
        1-indexed start line. CAS-protected: re-reads the file and retries
        on a concurrent external write, up to `_CAS_MAX_RETRIES` times."""
        for attempt in range(_CAS_MAX_RETRIES + 1):
            mtime_before = _mtime_or_none(path)
            lines, newline, trailing_newline = _read_lines_with_terminator(path)
            fence = dict(_iter_lines_with_fence_state(lines))

            first_idx = None
            idx, n = 0, len(lines)
            while idx < n:
                if fence.get(idx):
                    idx += 1
                    continue
                b = _match_task_block(lines, idx, str(path))
                if b is not None:
                    first_idx = idx
                    break
                idx += 1

            if first_idx is None:
                new_lines = lines + block if lines else list(block)
                insert_at = len(lines) + 1
            else:
                new_lines = lines[:first_idx] + block + lines[first_idx:]
                insert_at = first_idx + 1

            mtime_now = _mtime_or_none(path)
            if mtime_now == mtime_before:
                atomic_write_lines(path, new_lines, newline=newline, trailing_newline=trailing_newline)
                return insert_at
            if attempt >= _CAS_MAX_RETRIES:
                raise TaskConflictError(f"Too many conflicting writes to {path}")
        raise TaskConflictError(f"Too many conflicting writes to {path}")

    def _reposition_file(self, path: Path):
        """Refresh `source_file`/`line_number` for every known task in `path`
        from its current on-disk content. Purely informational bookkeeping —
        writes never address by these fields, only by id."""
        if not path.exists():
            return
        lines = _read_lines(path)
        fence = dict(_iter_lines_with_fence_state(lines))
        idx, n = 0, len(lines)
        while idx < n:
            if fence.get(idx):
                idx += 1
                continue
            block = _match_task_block(lines, idx, str(path))
            if block is None:
                idx += 1
                continue
            end, task, had_id = block
            if had_id:
                existing = self._tasks.get(task.id)
                if existing is not None:
                    existing.source_file = str(path)
                    existing.line_number = idx + 1
            idx = end

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
            atomic_write_text(file_path, template)
        return file_path

    def _move_task_between_files(self, task: Task, old_context: str, new_context: str) -> bool:
        """Move `task`'s block from its old context file to `new_context`'s.

        Returns False if the task's block is no longer present in the
        source file (externally deleted) — the caller should reconcile like
        `update` does for `found=False`, not treat it as a conflict.

        Inserts into the destination BEFORE removing from the source, so a
        destination-side conflict can never lose the task: if the insert
        raises, the source is untouched. If the source removal itself then
        raises (a source-side conflict, after the insert already
        succeeded), best-effort removes the just-inserted destination block
        before re-raising, so the task isn't left duplicated in both files.
        """
        old_path = Path(task.source_file)
        old_lines = _read_lines(old_path)
        if _find_task_block_span(old_lines, task.id) is None:
            return False

        new_path = self._get_context_file(new_context)
        block = _format_task_block(task)
        start_line = self._cas_insert_at_top(new_path, block)

        try:
            self._cas_rewrite(old_path, task.id, lambda: None)
        except TaskConflictError:
            try:
                self._cas_rewrite(new_path, task.id, lambda: None)
            except TaskConflictError:
                pass
            raise
        self._reposition_file(old_path)

        task.source_file = str(new_path)
        task.line_number = start_line
        task.context = new_context
        self._last_written_line[task.id] = block[0]
        self._reposition_file(new_path)
        return True

    def _write_dashboard(self):
        """Regenerate Dashboard.md from current task state.

        The dashboard is fully auto-generated. Manual edits are overwritten
        the next time any task changes.
        """
        try:
            content = self._build_dashboard_content()
        except Exception as e:
            logger.warning(f"Dashboard generation failed: {e}")
            return
        dashboard = self.tasks_dir / "Dashboard.md"
        try:
            if dashboard.exists() and dashboard.read_text(encoding="utf-8") == content:
                return  # No-op write avoids triggering watchers
        except Exception:
            pass
        atomic_write_text(dashboard, content)

    def _build_dashboard_content(self) -> str:
        tasks = list(self._tasks.values())
        open_tasks = [t for t in tasks if t.status not in ("done", "cancelled")]

        today = date.today()
        today_iso = today.isoformat()
        in_seven_iso = (today + timedelta(days=7)).isoformat()
        in_progress_count = sum(1 for t in tasks if t.status == "in_progress")
        overdue_count = sum(1 for t in open_tasks if t.due_date and t.due_date < today_iso)
        due_this_week_count = sum(
            1 for t in open_tasks
            if t.due_date and today_iso <= t.due_date <= in_seven_iso
        )
        done_last_7_count = sum(
            1 for t in tasks
            if t.status == "done" and t.done_date
            and 0 <= (today - date.fromisoformat(t.done_date)).days <= 7
        )

        # Tags with at least one open task
        tag_counts: dict[str, int] = {}
        untagged_open = 0
        for t in open_tasks:
            normalized = [tg.lstrip("#") for tg in t.tags if tg.lstrip("#")]
            if normalized:
                for tag in normalized:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            else:
                untagged_open += 1

        lines: list[str] = [
            "---",
            "type: dashboard",
            "---",
            "<!-- AUTO-GENERATED by LifeOS task manager. Manual edits are overwritten on the next task change. -->",
            "# Task Dashboard",
            "",
            f"> **{len(open_tasks)} open** · {due_this_week_count} due this week · "
            f"{overdue_count} overdue · {in_progress_count} in progress · "
            f"{done_last_7_count} done in last 7 days",
            f"> _Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
            "",
        ]

        if overdue_count:
            lines += [
                "## Overdue",
                "```tasks",
                "not done",
                "path includes LifeOS/Tasks",
                "due before today",
                "sort by due",
                "```",
                "",
            ]

        lines += [
            "## Urgent",
            "```tasks",
            "status.name includes Urgent",
            "path includes LifeOS/Tasks",
            "sort by created reverse",
            "```",
            "",
            "## In Progress",
            "```tasks",
            "status.name includes In Progress",
            "path includes LifeOS/Tasks",
            "sort by created reverse",
            "```",
            "",
            "## Due This Week",
            "```tasks",
            "not done",
            "path includes LifeOS/Tasks",
            "due after yesterday",
            "due before in 8 days",
            "sort by due",
            "```",
            "",
            "## By Tag",
            "",
        ]

        for tag in sorted(tag_counts.keys(), key=lambda x: (-tag_counts[x], x.lower())):
            lines += [
                f"### #{tag}",
                "```tasks",
                "not done",
                "path includes LifeOS/Tasks",
                f"tag includes #{tag}",
                "sort by created reverse",
                "```",
                "",
            ]

        if untagged_open:
            lines += [
                "### No tag",
                "```tasks",
                "not done",
                "path includes LifeOS/Tasks",
                "no tags",
                "sort by created reverse",
                "```",
                "",
            ]

        lines += [
            "## All Open",
            "```tasks",
            "not done",
            "path includes LifeOS/Tasks",
            "sort by created reverse",
            "```",
            "",
            "## Stale — open 30+ days",
            "```tasks",
            "not done",
            "path includes LifeOS/Tasks",
            "created before 30 days ago",
            "sort by created",
            "```",
            "",
            "## Completed",
            "```tasks",
            "done",
            "path includes LifeOS/Tasks",
            "sort by done reverse",
            "```",
            "",
        ]

        return "\n".join(lines)


# ======================================================================
# Module-level helpers (pure functions)
# ======================================================================

_INLINE_FIELD_RE = re.compile(r'\[(\w+)::\s*([^\]]*)\]')
_TAG_RE = re.compile(r'#([\w-]+)')
_ID_RE = re.compile(r'<!--\s*id:(\w+)\s*-->')
# Any checkbox line counts as a task line — the literal "TODO" keyword is no
# longer required to parse (kept only because _format_task_line still emits
# it, for backward compatibility with existing vault content and the
# Obsidian Tasks dashboard queries in Dashboard.md).
_CHECKBOX_RE = re.compile(r'^- \[(.)\]\s+(?:TODO\s+)?')
# Notes body: an indented blockquote line directly beneath a task line,
# exactly like a scheduler entry's message_content body.
_BODY_LINE_RE = re.compile(r'^\s+>\s?(.*)$')
_BODY_INDENT = "    "

_FENCE_RE = re.compile(r'^(```|~~~)')


def _iter_lines_with_fence_state(lines: list[str]):
    """Yield `(idx, in_fence)` for every line in `lines`. `in_fence` is True
    for a line inside — or opening/closing — a ``` or ~~~ fenced code block.
    A checkbox line inside a fence is documentation/example text, never a
    real task: every scanner that walks task lines (`_reparse_lines`,
    `_cas_insert_at_top`, `_find_task_block_span`, `_reposition_file`) skips
    a line this marks `True` instead of handing it to `_match_task_block`.
    """
    in_fence = False
    marker = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if in_fence:
            yield idx, True
            if stripped.startswith(marker):
                in_fence = False
                marker = None
            continue
        m = _FENCE_RE.match(stripped)
        if m:
            marker = m.group(1)
            in_fence = True
            yield idx, True
        else:
            yield idx, False


def _format_task_line(task: Task) -> str:
    """Task → Dataview format markdown line."""
    symbol = STATUS_TO_SYMBOL.get(task.status, " ")
    parts = [f"- [{symbol}] TODO {task.description}"]

    # Inline fields
    if task.due_date:
        parts.append(f"[due:: {task.due_date}]")
    if task.priority:
        parts.append(f"[priority:: {task.priority}]")
    # Omitted (not emitted as an empty field) when unset, e.g. a hand-
    # authored line that only just got its id minted on reindex — a task
    # created through the API always has one (create() sets it to today).
    if task.created_date:
        parts.append(f"[created:: {task.created_date}]")
    if task.done_date:
        parts.append(f"[done:: {task.done_date}]")
    if task.cancelled_date:
        parts.append(f"[cancelled:: {task.cancelled_date}]")
    if task.updated_at:
        parts.append(f"[updated:: {task.updated_at}]")

    # Operator + unknown fields round-trip in their original order.
    for key, value in task.fields.items():
        parts.append(f"[{key}:: {value}]")

    # Tags
    for tag in task.tags:
        t = tag if tag.startswith("#") else f"#{tag}"
        parts.append(t)

    # ID comment
    parts.append(f"<!-- id:{task.id} -->")

    return " ".join(parts)


def _format_task_block(task: Task) -> list[str]:
    """Task → its markdown checkbox line plus an indented ``> `` blockquote
    body carrying `notes` (one line per content line). Empty notes emit no
    body lines."""
    lines = [_format_task_line(task)]
    if task.notes:
        for content_line in task.notes.split("\n"):
            lines.append(f"{_BODY_INDENT}> {content_line}" if content_line else f"{_BODY_INDENT}>")
    return lines


def _parse_task_line(line: str, file_path: str, line_num: int) -> Optional[Task]:
    """Parse one checkbox line into a Task, or None if not a checkbox line.

    The literal "TODO" keyword is optional — any `- [.] ...` checkbox line
    counts as a task (see module docstring / docs/specs/technical/
    task-management.md). A missing id gets a fresh one minted here; the
    caller (reindex) is responsible for writing that id back to disk so it's
    stable on the next parse.
    """
    m = _CHECKBOX_RE.match(line)
    if not m:
        return None

    symbol = m.group(1)
    status = SYMBOL_TO_STATUS.get(symbol, "todo")

    rest = line[m.end():]

    # Extract ID
    id_match = _ID_RE.search(rest)
    task_id = id_match.group(1) if id_match else uuid.uuid4().hex[:8]

    # Extract inline fields
    raw_fields = {}
    for fm in _INLINE_FIELD_RE.finditer(rest):
        raw_fields[fm.group(1)] = fm.group(2).strip()
    extra_fields = {k: v for k, v in raw_fields.items() if k not in _KNOWN_FIELD_KEYS}

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
        priority=raw_fields.get("priority", ""),
        due_date=raw_fields.get("due") or None,
        created_date=raw_fields.get("created", ""),
        done_date=raw_fields.get("done") or None,
        cancelled_date=raw_fields.get("cancelled") or None,
        updated_at=raw_fields.get("updated") or None,
        tags=tags,
        fields=extra_fields,
        source_file=file_path,
        line_number=line_num,
    )


def _match_task_block(
    lines: list[str], idx: int, file_path: str = ""
) -> Optional[tuple[int, Task, bool]]:
    """If `lines[idx]` is a task line, parse it (and any body lines
    immediately following) and return `(end, task, had_id)` — `end` is the
    exclusive index just past the block, `had_id` says whether the raw line
    already carried an `<!-- id:.. -->` comment. Returns None if `lines[idx]`
    is not a task line."""
    task = _parse_task_line(lines[idx], file_path, idx + 1)
    if task is None:
        return None
    had_id = bool(_ID_RE.search(lines[idx]))
    j, n = idx + 1, len(lines)
    body = []
    while j < n:
        m = _BODY_LINE_RE.match(lines[j])
        if not m:
            break
        body.append(m.group(1))
        j += 1
    if body:
        task.notes = "\n".join(body)
    return j, task, had_id


def _find_task_block_span(lines: list[str], task_id: str) -> Optional[tuple[int, int]]:
    """Return the `(start, end)` line span of the block carrying `task_id`'s
    id comment, or None if not found. Skips lines inside a fenced code
    block — an id comment there is example text, not a real task."""
    fence = dict(_iter_lines_with_fence_state(lines))
    idx, n = 0, len(lines)
    while idx < n:
        if fence.get(idx):
            idx += 1
            continue
        block = _match_task_block(lines, idx)
        if block is None:
            idx += 1
            continue
        end, task, had_id = block
        if had_id and task.id == task_id:
            return idx, end
        idx = end
    return None


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
