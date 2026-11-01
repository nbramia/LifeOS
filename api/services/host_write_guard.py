"""Content checks for host-tool writes to the task and schedule files.

`lifeos_fs_write` replaces whole files, so a write to a task file or the
schedule inbox would otherwise skip every check the task and schedule tools
run. `check_vault_write` compares the file before and after the write and
refuses one that:

- in a `LifeOS/Tasks/*.md` file: adds an engine, assignee, execution or `#human` tag
  anywhere, moves one between tasks, removes a task id, changes a task's
  `parent_id` or gives a new task one, or changes any line of a task an
  engine, the worker or the operator queue owns. Plain tasks can be added and
  the operator's, an external agent's and unassigned tasks edited; claiming
  and assigning go through `lifeos_task_update`.
- in `LifeOS/Scheduler/Inbox.md`: removes a schedule, changes a schedule that
  runs a prompt, an endpoint or an agent, or adds or leaves a schedule that is
  anything but a static notification.

Pure: it parses text with the stores' own parsers and touches no file.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import PurePosixPath

from api.services import agent_board
from api.services import scheduler_store as ss
from api.services import task_manager as tm

TASKS_PARTS = PurePosixPath(tm.TaskManager.TASKS_FOLDER).parts
SCHEDULE_INBOX_PARTS = PurePosixPath(ss.SCHEDULER_FOLDER, ss.INBOX_FILE).parts


def _forbidden(tag: str) -> bool:
    from api.services.mcp_tool_tier import _forbidden_tag

    return _forbidden_tag(tag)


def _forbidden_counts(text: str) -> Counter:
    counts: Counter = Counter()
    for variant in (text, unicodedata.normalize("NFKC", text)):
        found = Counter(t.lower() for t in tm._TAG_RE.findall(variant) if _forbidden(t))
        counts |= found  # per tag, the larger of the two readings
    return counts


def _task_blocks(text: str) -> dict[str, tuple[tm.Task, str]]:
    """Task id -> (parsed task, its line plus notes body) for every id'd task."""
    lines = text.splitlines()
    blocks: dict[str, tuple[tm.Task, str]] = {}
    i = 0
    while i < len(lines):
        task = tm._parse_task_line(lines[i], "check.md", i + 1)
        if task is None:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and tm._BODY_LINE_RE.match(lines[j]):
            j += 1
        if tm._ID_RE.search(lines[i]):
            blocks[task.id] = (task, "\n".join(lines[i:j]))
        else:
            blocks[f"new:{i}"] = (task, "\n".join(lines[i:j]))
        i = j
    return blocks


def _check_task_file(old: str, new: str) -> str | None:
    old_counts, new_counts = _forbidden_counts(old), _forbidden_counts(new)
    added = sorted(t for t in new_counts if new_counts[t] > old_counts.get(t, 0))
    if added:
        return (f"task files cannot gain #{added[0]} through a file write; "
                "use lifeos_task_update or the board to assign or route a task")
    before, after = _task_blocks(old), _task_blocks(new)
    for task_id, (task, block) in before.items():
        if task_id.startswith("new:"):
            continue
        if task_id not in after:
            return f"task {task_id} would be removed; deleting a task is not available here"
        new_task, new_block = after[task_id]
        old_owned = agent_board.normalize_tags(task.tags) & agent_board.EXTERNAL_AGENT_HANDS_OFF_TAGS
        if old_owned and new_block != block:
            return (f"task {task_id} is owned by an engine, the worker or the operator queue "
                    f"(#{min(old_owned)}); leave its lines unchanged")
        old_tags = {t.lower() for t in task.tags if _forbidden(t)}
        new_tags = {t.lower() for t in new_task.tags if _forbidden(t)}
        if old_tags != new_tags:
            return f"task {task_id} would change its assignee or routing tags; use lifeos_task_update"
        if task.fields.get("parent_id") != new_task.fields.get("parent_id"):
            return f"task {task_id} would change its parent_id; reparent through lifeos_task_update"
    for task_id, (task, _) in after.items():
        if task_id in before and not task_id.startswith("new:"):
            continue
        if task.fields.get("parent_id"):
            return "a new task cannot set parent_id through a file write"
    return None


def _schedule_blocks(text: str) -> dict[str, tuple[ss.ScheduleEntry, str]]:
    lines = text.splitlines()
    blocks = {}
    for start, end, entry in ss._iter_entry_blocks(lines):
        key = entry.id if ss._ID_RE.search(lines[start]) else f"new:{start}"
        blocks[key] = (entry, "\n".join(lines[start:end]))
    return blocks


def _static_notification(entry: ss.ScheduleEntry) -> bool:
    return (
        entry.action == "notify" and entry.message_type == "static"
        and not entry.endpoint_config and not entry.executor
        and not (entry.persona_id or entry.model_id or entry.working_dir)
    )


def _check_schedule_inbox(old: str, new: str) -> str | None:
    before, after = _schedule_blocks(old), _schedule_blocks(new)
    for key, (entry, block) in before.items():
        if key.startswith("new:"):
            continue
        if key not in after:
            return f"schedule {key} would be removed; deleting a schedule is not available here"
        if not _static_notification(entry) and after[key][1] != block:
            return (f"schedule {key} runs a prompt, an endpoint or an agent; "
                    "leave its lines unchanged")
    for key, (entry, block) in after.items():
        if key in before and before[key][1] == block:
            continue
        if not _static_notification(entry):
            return ("a file write can add or change only static notification schedules "
                    "(no action other than notify, no prompt, endpoint, executor tag, "
                    "persona, model or working_dir)")
    return None


def _is_task_file(rel: PurePosixPath) -> bool:
    parts = rel.parts
    # The task store reads only files directly in the tasks folder.
    return parts[:len(TASKS_PARTS)] == TASKS_PARTS and len(parts) == len(TASKS_PARTS) + 1


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
