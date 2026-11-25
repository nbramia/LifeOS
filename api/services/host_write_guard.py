"""Content checks for host-tool writes to the task and schedule files.

`lifeos_fs_write` replaces whole files, so a write to a task file or the
schedule inbox would otherwise skip every check the task and schedule tools
run. `check_vault_write` compares the file before and after the write and
refuses one that:

- in a `LifeOS/Tasks/*.md` file: gives a new task an engine, consent,
  protected-lifecycle or `#human` tag (line or notes), adds one to an existing
  task's notes, changes the routing tags on an existing task's line, adds such
  a tag anywhere outside a task, repeats a task id, removes a task id, adds,
  repeats or changes any `[key:: value]` field other than due, priority and
  the dates (so `parent_id`, `operation_key` and the execution fields stay as
  they are), changes any line of a task an engine, the worker or the operator
  queue owns, or changes or removes a task that has no id yet but carries one
  of those tags or a field. Plain tasks can be added and the operator's, an
  external agent's and unassigned tasks edited; claiming and assigning go
  through `lifeos_task_update`.
- in `LifeOS/Scheduler/Inbox.md`: removes a schedule (with or without an id),
  repeats a schedule id, changes a schedule that runs a prompt, an endpoint
  or an agent, or adds or leaves a schedule that is anything but a static
  notification. A static notification without an id may be edited in place.

Ownership comes from the tags on the task line, as the task store reads
them; a tag in a task's notes is prose and routes nothing.

Tasks are read the way the task store reads them: fenced code blocks hold no
tasks, every checkbox line is a task whether or not it has an id yet, and the
first block carrying an id owns it. Tags are read from the raw text and from
its NFKC form, each compared on its own.

Pure: it parses text with the stores' own parsers and touches no file.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from api.services import agent_board
from api.services import scheduler_store as ss
from api.services import task_manager as tm

TASKS_PARTS = PurePosixPath(tm.TaskManager.TASKS_FOLDER).parts
SCHEDULE_INBOX_PARTS = PurePosixPath(ss.SCHEDULER_FOLDER, ss.INBOX_FILE).parts

_ASSIGN_HINT = "use lifeos_task_update or the board to assign or route a task"


def _forbidden(tag: str) -> bool:
    from api.services.mcp_tool_tier import _forbidden_tag

    return _forbidden_tag(tag)


def _forbidden_tags(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Forbidden tags in `text`, as (raw reading, NFKC reading), lowercased."""
    return tuple(  # type: ignore[return-value]
        frozenset(t.lower() for t in tm._TAG_RE.findall(variant) if _forbidden(t))
        for variant in (text, unicodedata.normalize("NFKC", text))
    )


def _forbidden_counts(lines: list[str]) -> tuple[Counter, Counter]:
    raw: Counter = Counter()
    nfkc: Counter = Counter()
    for line in lines:
        for counter, variant in ((raw, line), (nfkc, unicodedata.normalize("NFKC", line))):
            counter.update(t.lower() for t in tm._TAG_RE.findall(variant) if _forbidden(t))
    return raw, nfkc


@dataclass
class _TaskBlock:
    task: tm.Task
    text: str           # the task line plus its notes body
    line: str           # the task line alone
    had_id: bool        # carries an id the store keeps (first block with it)

    @property
    def line_tags(self) -> tuple[frozenset[str], frozenset[str]]:
        """Forbidden tags on the task line, read raw and NFKC."""
        return _forbidden_tags(self.line)

    @property
    def body_tags(self) -> tuple[frozenset[str], frozenset[str]]:
        """Forbidden tags in the notes body, read raw and NFKC."""
        return _forbidden_tags(self.text[len(self.line):])

    @property
    def fields(self) -> list[tuple[str, str]]:
        """Every `[key:: value]` on the line other than the dates, due and
        priority, in order and with repeats (the store keeps the last)."""
        return [(m.group(1), m.group(2).strip()) for m in tm._INLINE_FIELD_RE.finditer(self.line)
                if m.group(1) not in tm._KNOWN_FIELD_KEYS]

    @property
    def owned(self) -> set[str]:
        """The task's own hands-off tags, as the task store reads them."""
        return agent_board.normalize_tags(self.task.tags) & agent_board.EXTERNAL_AGENT_HANDS_OFF_TAGS


def _task_blocks(text: str) -> tuple[list[_TaskBlock], list[str], list[str]]:
    """(task blocks in file order, non-task lines, ids repeated in the file).

    Follows `TaskManager._reparse_lines`: lines inside a fence are not tasks,
    and a later block repeating an id is a task without one (the store gives
    it a fresh id).
    """
    lines = text.splitlines()
    fence = dict(tm._iter_lines_with_fence_state(lines))
    blocks: list[_TaskBlock] = []
    other: list[str] = []
    seen: set[str] = set()
    repeated: list[str] = []
    i = 0
    while i < len(lines):
        block = None if fence.get(i) else tm._match_task_block(lines, i, "check.md")
        if block is None:
            other.append(lines[i])
            i += 1
            continue
        end, task, had_id = block
        if had_id:
            if task.id in seen:
                repeated.append(task.id)
                had_id = False
            seen.add(task.id)
        blocks.append(_TaskBlock(task, "\n".join(lines[i:end]), lines[i], had_id))
        i = end
    return blocks, other, repeated


def _check_task_file(old: str, new: str) -> str | None:
    old_blocks, old_other, _ = _task_blocks(old)
    new_blocks, new_other, repeated = _task_blocks(new)
    if repeated:
        return f"task id {repeated[0]} appears more than once; each task keeps its own id"

    for before, after in zip(_forbidden_counts(old_other), _forbidden_counts(new_other)):
        added = sorted(t for t in after if after[t] > before.get(t, 0))
        if added:
            return f"task files cannot gain #{added[0]} through a file write; {_ASSIGN_HINT}"

    old_by_id = {b.task.id: b for b in old_blocks if b.had_id}
    new_by_id = {b.task.id: b for b in new_blocks if b.had_id}
    old_unnamed = Counter(b.text for b in old_blocks if not b.had_id)
    new_unnamed = Counter(b.text for b in new_blocks if not b.had_id)

    for task_id, block in old_by_id.items():
        after = new_by_id.get(task_id)
        if after is None:
            return f"task {task_id} would be removed; deleting a task is not available here"
        if block.owned and after.text != block.text:
            return (f"task {task_id} is owned by an engine, the worker or the operator queue "
                    f"(#{min(block.owned)}); leave its lines unchanged")
        if after.line_tags != block.line_tags:
            gained = sorted(set().union(*after.line_tags) - set().union(*block.line_tags))
            detail = f" (#{gained[0]})" if gained else ""
            return (f"task {task_id} would change its assignee or routing tags{detail}; "
                    "use lifeos_task_update")
        for old_reading, new_reading in zip(block.body_tags, after.body_tags):
            if new_reading - old_reading:
                return (f"task files cannot gain #{min(new_reading - old_reading)} through a file "
                        f"write; {_ASSIGN_HINT}")
        if after.fields != block.fields:
            changed = sorted({*block.fields} ^ {*after.fields}) or sorted(after.fields)
            return (f"task {task_id} would change its [{changed[0][0]}::] field; parent, operation "
                    "and execution fields change only through the task tools")

    for block in old_blocks:
        if block.had_id or new_unnamed[block.text] >= old_unnamed[block.text]:
            continue
        if block.owned or any(block.line_tags) or block.fields:
            return ("a task without an id yet carries a routing tag or field; leave it "
                    "unchanged until the task store gives it an id")

    unchanged = Counter(old_unnamed)
    for block in new_blocks:
        if block.had_id and block.task.id in old_by_id:
            continue
        if not block.had_id and unchanged[block.text] > 0:
            unchanged[block.text] -= 1
            continue
        tags = sorted({t for reading in (*block.line_tags, *block.body_tags) for t in reading})
        if tags:
            return f"task files cannot gain #{tags[0]} through a file write; {_ASSIGN_HINT}"
        if block.fields:
            key = block.fields[0][0]
            return (f"a new task cannot set [{key}::] through a file write "
                    "(parent, operation and execution fields go through the task tools)")
    return None


def _schedule_blocks(text: str) -> tuple[dict[str, tuple[ss.ScheduleEntry, str]], list, list[str]]:
    """(blocks by id, blocks without an id, ids repeated in the file)."""
    lines = text.splitlines()
    by_id: dict[str, tuple[ss.ScheduleEntry, str]] = {}
    unnamed: list[tuple[ss.ScheduleEntry, str]] = []
    repeated: list[str] = []
    for start, end, entry in ss._iter_entry_blocks(lines):
        block = (entry, "\n".join(lines[start:end]))
        if not ss._ID_RE.search(lines[start]):
            unnamed.append(block)
            continue
        if entry.id in by_id:
            repeated.append(entry.id)
        by_id[entry.id] = block  # the store keeps the last block for an id
    return by_id, unnamed, repeated


def _static_notification(entry: ss.ScheduleEntry) -> bool:
    return (
        entry.action == "notify" and entry.message_type == "static"
        and not entry.endpoint_config and not entry.executor
        and not (entry.persona_id or entry.model_id or entry.working_dir)
    )


_STATIC_ONLY = ("a file write can add or change only static notification schedules "
                "(no action other than notify, no prompt, endpoint, executor tag, "
                "persona, model or working_dir)")


def _check_schedule_inbox(old: str, new: str) -> str | None:
    before, before_unnamed, _ = _schedule_blocks(old)
    after, after_unnamed, repeated = _schedule_blocks(new)
    if repeated:
        return f"schedule id {repeated[0]} appears more than once; each schedule keeps its own id"
    for key, (entry, block) in before.items():
        if key not in after:
            return f"schedule {key} would be removed; deleting a schedule is not available here"
        if not _static_notification(entry) and after[key][1] != block:
            return (f"schedule {key} runs a prompt, an endpoint or an agent; "
                    "leave its lines unchanged")
    if len(after_unnamed) < len(before_unnamed):
        return ("a schedule without an id would be removed; "
                "deleting a schedule is not available here")
    remaining = Counter(block for _, block in after_unnamed)
    for entry, block in before_unnamed:
        if _static_notification(entry):
            continue  # an id-less static notification may be edited in place
        if remaining[block] <= 0:
            return ("a schedule without an id runs a prompt, an endpoint or an agent; "
                    "leave its lines unchanged")
        remaining[block] -= 1
    for key, (entry, block) in after.items():
        if key in before and before[key][1] == block:
            continue
        if not _static_notification(entry):
            return _STATIC_ONLY
    unchanged = Counter(block for _, block in before_unnamed)
    for entry, block in after_unnamed:
        if unchanged[block] > 0:
            unchanged[block] -= 1
            continue
        if not _static_notification(entry):
            return _STATIC_ONLY
    return None


def _is_task_file(rel: PurePosixPath) -> bool:
    parts = rel.parts
    # The task store reads only files directly in the tasks folder.
    return (parts[:len(TASKS_PARTS)] == TASKS_PARTS and len(parts) == len(TASKS_PARTS) + 1
            and rel.suffix == ".md")


def guarded_path(rel: PurePosixPath) -> bool:
    """Whether a vault write to `rel` goes through `check_vault_write`."""
    return _is_task_file(rel) or rel.parts == SCHEDULE_INBOX_PARTS


def check_vault_write(rel: PurePosixPath, old: str, new: str) -> str | None:
    """Refusal for writing `new` over `old` at vault path `rel`, or None."""
    if _is_task_file(rel):
        return _check_task_file(old, new)
    if rel.parts == SCHEDULE_INBOX_PARTS:
        return _check_schedule_inbox(old, new)
    return None
