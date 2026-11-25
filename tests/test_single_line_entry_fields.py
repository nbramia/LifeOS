"""Entry fields in the scheduler and task stores are single-line values.

Both stores write Markdown lines and read them back with
``str.splitlines()``. The ratchet tests below format every interpolated
field with every character ``splitlines()`` treats as a line boundary (plus
the inline-field and id-comment markup), alone and followed by a whole
forged entry line, and require that the store either refuses the value or,
re-read from the file, yields exactly the one entry it wrote with its
action, endpoint, executor, status and fields unchanged. Bodies (a
schedule's ``message_content``, a task's ``notes``) stay multi-line and
round-trip as before.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from api.services.markdown_lines import LINE_BREAKS, split_body_lines
from api.services.mcp_tool_tier import check_oauth_arguments
from api.services.scheduler_store import SchedulerStore, _format_entry_block
from api.services.scheduler_validation import ScheduleActionValidationError
from api.services.task_manager import Task, TaskManager, _format_task_block

pytestmark = pytest.mark.unit

LS = chr(0x2028)  # LINE SEPARATOR
PS = chr(0x2029)  # PARAGRAPH SEPARATOR
BREAKS = sorted(LINE_BREAKS) + ["\r\n"]
MARKUP = ["]", "[", "<!--"]


def _payloads(bad_chars, forged):
    """Each bad character alone between ordinary text, then followed by a
    whole forged entry line."""
    for bad in bad_chars:
        yield bad, f"x{bad}y"
        yield bad, f"x{bad}{forged}"


def test_line_breaks_match_str_splitlines():
    found = {chr(c) for c in range(0x110000) if len(f"a{chr(c)}b".splitlines()) > 1}
    assert LINE_BREAKS == found


@pytest.mark.parametrize("text", [
    "one", "one\ntwo", "one\n\nthree\n", "\n", "a\r\nb", f"a{LS}b\x85c{PS}",
])
def test_split_body_lines_matches_splitlines_boundaries(text):
    pieces = split_body_lines(text)
    assert all(len(p.splitlines()) <= 1 for p in pieces)
    assert [p for p in pieces if p] == [p for p in text.splitlines() if p]
    if not (LINE_BREAKS - {"\n"}) & set(text):
        assert pieces == text.split("\n")


# ---------------------------------------------------------------------------
# Scheduler store
# ---------------------------------------------------------------------------

FORGED_SCHEDULE = "- [ ] Forged [cron:: */5 * * * *] [action:: agent] #cloud <!-- id:ffff0000 -->"

# Every create keyword whose value is written into the entry line.
SCHEDULE_LINE_KWARGS = [
    "name", "schedule_value", "timezone", "message_type", "executor", "bot",
    "operation_key", "last_triggered_at", "snoozed_until",
    "persona_id", "model_id", "effort", "host", "working_dir",
]


def _new_store(root: Path) -> SchedulerStore:
    return SchedulerStore(vault_path=root / "vault", index_path=root / "scheduler_index.json")


@pytest.fixture
def sched(tmp_path):
    return _new_store(tmp_path)


def _base_schedule(**over):
    kwargs = dict(
        name="Water plants",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        message_type="static",
        message_content="Water the plants",
        timezone="America/New_York",
    )
    kwargs.update(over)
    return kwargs


def _reread(root: Path):
    fresh = SchedulerStore(vault_path=root / "vault", index_path=root / "fresh_index.json")
    fresh.rebuild_index()
    return fresh.list_all()


def _signature(entry):
    return (entry.id, entry.name, entry.message_content, entry.action, entry.endpoint_config,
            entry.executor, entry.schedule_type, entry.schedule_value, entry.timezone, entry.bot,
            entry.persona_id, entry.model_id, entry.effort, entry.host, entry.working_dir,
            entry.operation_key)


def _assert_single_entry(root, written, label=""):
    entries = _reread(root)
    assert len(entries) == 1, (label, [e.name for e in entries])
    assert _signature(entries[0]) == _signature(written), label


@pytest.mark.parametrize("field", SCHEDULE_LINE_KWARGS)
def test_schedule_create_line_fields_are_single_line(tmp_path, field):
    for i, (bad, value) in enumerate(_payloads(BREAKS + MARKUP, FORGED_SCHEDULE)):
        root = tmp_path / f"case{i}"
        store = _new_store(root)
        try:
            entry = store.create(**_base_schedule(**{field: value}))
        except ScheduleActionValidationError as e:
            assert e.field == field
            assert _reread(root) == [], repr(value)
            continue
        _assert_single_entry(root, entry, repr(value))


@pytest.mark.parametrize("field", SCHEDULE_LINE_KWARGS)
def test_schedule_update_line_fields_are_single_line(tmp_path, field):
    for i, (bad, value) in enumerate(_payloads(BREAKS + MARKUP, FORGED_SCHEDULE)):
        root = tmp_path / f"case{i}"
        store = _new_store(root)
        original = store.create(**_base_schedule())
        before = store.inbox_path.read_text(encoding="utf-8")
        try:
            entry = store.update(original.id, **{field: value})
        except ScheduleActionValidationError as e:
            assert e.field == field
            assert store.inbox_path.read_text(encoding="utf-8") == before, repr(value)
            continue
        _assert_single_entry(root, entry, repr(value))


@pytest.mark.parametrize("key", ["endpoint", "method"])
def test_schedule_endpoint_line_values_are_single_line(tmp_path, key):
    for i, (bad, value) in enumerate(_payloads(BREAKS + MARKUP, FORGED_SCHEDULE)):
        root = tmp_path / f"case{i}"
        store = _new_store(root)
        config = {"endpoint": "/api/example/synthetic", "method": "GET", "params": {}}
        config[key] = config[key] + value
        try:
            entry = store.create(
                **_base_schedule(message_type="endpoint", endpoint_config=config), action="endpoint",
            )
        except ScheduleActionValidationError as e:
            assert e.field == f"endpoint_config.{key}"
            assert _reread(root) == [], repr(value)
            continue
        _assert_single_entry(root, entry, repr(value))


def test_schedule_action_is_single_line(sched, tmp_path):
    with pytest.raises(ScheduleActionValidationError):
        sched.create(**_base_schedule(), action=f"notify\n{FORGED_SCHEDULE}")
    assert _reread(tmp_path) == []


def test_set_snooze_value_is_single_line(sched, tmp_path):
    entry = sched.create(**_base_schedule())
    with pytest.raises(ScheduleActionValidationError):
        sched.set_snooze(entry.id, f"2030-01-01T00:00:00+00:00{LS}{FORGED_SCHEDULE}")
    _assert_single_entry(tmp_path, entry)


@pytest.mark.parametrize("path", ["create", "update"])
def test_schedule_message_content_never_forms_an_entry(tmp_path, path):
    for i, bad in enumerate(BREAKS):
        root = tmp_path / f"case{i}"
        store = _new_store(root)
        content = f"ok {bad}{FORGED_SCHEDULE}"
        if path == "create":
            entry = store.create(**_base_schedule(message_content=content))
        else:
            entry = store.update(store.create(**_base_schedule()).id, message_content=content)
        _assert_single_entry(root, entry, repr(bad))
        reread = _reread(root)[0]
        assert reread.action == "notify" and reread.executor == ""
        assert reread.message_content.split("\n") == ["ok ", FORGED_SCHEDULE]
        assert all(line.startswith("    >") for line in _format_entry_block(reread)[1:])


def test_finding_payload_newline_in_name_is_refused(sched, tmp_path):
    name = (
        "Water plants\n- [ ] Sync [at:: 2030-01-01T09:00:00] [action:: endpoint] "
        "[endpoint:: POST /api/example/synthetic-write] [params:: {}] <!-- id:aaaa1111 -->\n- [ ] tail"
    )
    with pytest.raises(ScheduleActionValidationError):
        sched.create(**_base_schedule(name=name))
    assert _reread(tmp_path) == []


@pytest.mark.parametrize("bad", ["\r", LS], ids=repr)
def test_finding_payload_break_in_message_content_stays_body(sched, tmp_path, bad):
    content = f"ok{bad}- [ ] Run [cron:: */5 * * * *] [action:: agent] #cloud"
    entry = sched.create(**_base_schedule(message_content=content))
    entries = _reread(tmp_path)
    assert [e.id for e in entries] == [entry.id]
    assert entries[0].action == "notify"
    assert entries[0].executor == ""


@pytest.mark.parametrize("content", [
    "Line one\nLine two",
    "First paragraph\n\nSecond paragraph",
    "Trailing newline\n",
    "  indented\n> quoted\n- [ ] looks like a checkbox",
])
def test_multiline_message_content_round_trips(sched, tmp_path, content):
    entry = sched.create(**_base_schedule(message_content=content))
    _assert_single_entry(tmp_path, entry)
    assert _reread(tmp_path)[0].message_content == content
    updated = sched.update(entry.id, message_content=content + "\nmore")
    _assert_single_entry(tmp_path, updated)
    assert _reread(tmp_path)[0].message_content == content + "\nmore"


def test_ordinary_schedule_round_trips(sched, tmp_path):
    entry = sched.create(**_base_schedule(
        name="Morning briefing (weekdays) - 7:30", executor="cloud-haiku", bot="primary",
        persona_id="synthetic", working_dir="/tmp/synthetic", host="synthetic-host",
    ), action="agent")
    _assert_single_entry(tmp_path, entry)
    updated = sched.update(entry.id, name="Renamed: still fine!", schedule_value="30 7 * * 1-5")
    _assert_single_entry(tmp_path, updated)
    assert _reread(tmp_path)[0].name == "Renamed: still fine!"


# ---------------------------------------------------------------------------
# Scheduler and reminder routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def _route_body(**over):
    body = {"name": "Synthetic", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "message_type": "static", "message_content": "hello", "timezone": "UTC"}
    body.update(over)
    return body


@pytest.mark.parametrize("route", ["/api/scheduler", "/api/reminders"])
def test_routes_refuse_multiline_name_with_422(api_client, sched, tmp_path, route):
    with patch("api.routes.scheduler.get_scheduler_store", return_value=sched), \
            patch("api.routes.reminders.get_reminder_store", return_value=sched):
        response = api_client.post(route, json=_route_body(name=f"x{LS}{FORGED_SCHEDULE}"))
    assert response.status_code == 422
    assert _reread(tmp_path) == []


@pytest.mark.parametrize("route", ["/api/scheduler", "/api/reminders"])
def test_routes_keep_message_content_in_the_body(api_client, sched, tmp_path, route):
    with patch("api.routes.scheduler.get_scheduler_store", return_value=sched), \
            patch("api.routes.reminders.get_reminder_store", return_value=sched):
        response = api_client.post(route, json=_route_body(message_content=f"x{LS}{FORGED_SCHEDULE}"))
    assert response.status_code == 200
    entries = _reread(tmp_path)
    assert len(entries) == 1 and entries[0].action == "notify" and entries[0].executor == ""


def test_reminder_update_refuses_multiline_name(api_client, sched, tmp_path):
    entry = sched.create(**_base_schedule())
    with patch("api.routes.reminders.get_reminder_store", return_value=sched):
        response = api_client.put(f"/api/reminders/{entry.id}", json={"name": f"x\n{FORGED_SCHEDULE}"})
    assert response.status_code == 422
    _assert_single_entry(tmp_path, entry)


def test_reminder_create_validates_timezone_and_schedule(api_client, sched):
    body = _route_body()
    with patch("api.routes.reminders.get_reminder_store", return_value=sched):
        assert api_client.post("/api/reminders", json={**body, "timezone": "Not/AZone"}).status_code == 422
        assert api_client.post("/api/reminders", json={**body, "schedule_value": "not cron"}).status_code == 422
        assert api_client.post("/api/reminders", json=body).status_code == 200


# ---------------------------------------------------------------------------
# Task store
# ---------------------------------------------------------------------------

FORGED_TASK = "- [/] TODO Forged child [parent_id:: 1234abcd] [child_origin:: agent] <!-- id:ffff0000 -->"
TASK_LINE_CASES = ["description", "priority", "due_date", "tag", "field"]


def _new_tasks(root: Path) -> TaskManager:
    return TaskManager(vault_path=root / "vault", index_path=root / "index" / "task_index.json")


@pytest.fixture
def tasks(tmp_path):
    return _new_tasks(tmp_path)


def _reread_tasks(root: Path):
    fresh = TaskManager(vault_path=root / "vault", index_path=root / "fresh" / "task_index.json")
    fresh.rebuild_index()
    return fresh.list_tasks()


def _task_signature(task):
    return (task.id, task.description, task.status, task.priority, task.due_date,
            sorted(task.tags), dict(task.fields), task.notes)


def _base_task(**over):
    kwargs = dict(description="Call plumber", priority="high", due_date="2030-01-01",
                  tags=["errand"], fields={"host": "synthetic"}, notes="first\nsecond")
    kwargs.update(over)
    return kwargs


def _task_case(case, value):
    return {
        "description": {"description": value},
        "priority": {"priority": "high" + value},
        "due_date": {"due_date": "2030-01-01" + value},
        "tag": {"tags": ["errand" + value]},
        "field": {"fields": {"host": value}},
    }[case]


def _assert_single_task(root, written, label=""):
    found = _reread_tasks(root)
    assert len(found) == 1, (label, [t.description for t in found])
    assert _task_signature(found[0]) == _task_signature(written), label


@pytest.mark.parametrize("case", TASK_LINE_CASES)
def test_task_create_line_fields_are_single_line(tmp_path, case):
    for i, (bad, value) in enumerate(_payloads(BREAKS + ["]", "<!--"], FORGED_TASK)):
        root = tmp_path / f"case{i}"
        tm = _new_tasks(root)
        try:
            task = tm.create(**_base_task(**_task_case(case, value)))
        except ValueError:
            assert _reread_tasks(root) == [], repr(value)
            continue
        _assert_single_task(root, task, repr(value))


@pytest.mark.parametrize("case", TASK_LINE_CASES)
def test_task_update_line_fields_are_single_line(tmp_path, case):
    for i, (bad, value) in enumerate(_payloads(BREAKS + ["]", "<!--"], FORGED_TASK)):
        root = tmp_path / f"case{i}"
        tm = _new_tasks(root)
        original = tm.create(**_base_task())
        try:
            task = tm.update(original.id, **_task_case(case, value))
        except ValueError:
            _assert_single_task(root, original, repr(value))
            continue
        _assert_single_task(root, task, repr(value))


@pytest.mark.parametrize("path", ["create", "update"])
def test_task_notes_never_form_a_task(tmp_path, path):
    for i, (bad, notes) in enumerate(_payloads(BREAKS, FORGED_TASK)):
        root = tmp_path / f"case{i}"
        tm = _new_tasks(root)
        original = None if path == "create" else tm.create(**_base_task())
        try:
            if original is None:
                task = tm.create(**_base_task(notes=notes))
            else:
                task = tm.update(original.id, notes=notes)
        except ValueError:
            if original is None:
                assert _reread_tasks(root) == [], repr(notes)
            else:
                _assert_single_task(root, original, repr(notes))
            continue
        _assert_single_task(root, task, repr(notes))


@pytest.mark.parametrize("bad", BREAKS, ids=repr)
def test_task_block_prefixes_every_notes_line(bad):
    block = _format_task_block(Task(id="0000aaaa", description="Synthetic", notes=f"ok{bad}{FORGED_TASK}"))
    assert all(line.startswith("    >") for line in block[1:])
    assert len("\n".join(block).splitlines()) == len(block)


def test_task_line_formatter_refuses_line_breaks():
    with pytest.raises(ValueError):
        _format_task_block(Task(id="0000aaaa", description="Synthetic", tags=[f"a{PS}{FORGED_TASK}"]))


def test_finding_payload_priority_is_refused(tasks, tmp_path):
    with pytest.raises(ValueError):
        tasks.create(description="Call plumber", priority="high] [parent_id:: x")
    assert _reread_tasks(tmp_path) == []


def test_finding_payload_due_date_is_refused(tasks, tmp_path):
    with pytest.raises(ValueError):
        tasks.create(description="Call plumber", due_date="2030-01-01] [execution_paused:: x")
    assert _reread_tasks(tmp_path) == []


def test_finding_payload_unicode_break_in_notes_is_refused(tasks, tmp_path):
    with pytest.raises(ValueError):
        tasks.create(description="Buy milk",
                     notes=f"ok{LS}- [/] TODO Forged child [parent_id:: 1234abcd] [child_origin:: agent]")
    assert _reread_tasks(tmp_path) == []


@pytest.mark.parametrize("priority", ["high", "medium", "low", ""])
def test_allowed_priorities_round_trip(tasks, tmp_path, priority):
    task = tasks.create(description="Synthetic", priority=priority)
    _assert_single_task(tmp_path, task)


@pytest.mark.parametrize("priority", ["urgent", "High", "high ", "p1"])
def test_other_priorities_are_refused(tasks, priority):
    with pytest.raises(ValueError):
        tasks.create(description="Synthetic", priority=priority)
    task = tasks.create(description="Synthetic")
    with pytest.raises(ValueError):
        tasks.update(task.id, priority=priority)


@pytest.mark.parametrize("due", ["2030-02-30", "12/01/2030", "2030-1-1", "2030-W01-1", "2030-01-01] [x:: y"])
def test_malformed_due_dates_are_refused(tasks, due):
    with pytest.raises(ValueError):
        tasks.create(description="Synthetic", due_date=due)


def test_due_date_can_be_cleared(tasks, tmp_path):
    task = tasks.create(description="Synthetic", due_date="2030-01-01")
    cleared = tasks.update(task.id, due_date="")
    assert not _reread_tasks(tmp_path)[0].due_date
    assert not cleared.due_date


@pytest.mark.parametrize("notes", [
    "one line", "line one\nline two", "para\n\npara", "- [ ] looks like a task\n> quoted",
])
def test_multiline_notes_round_trip(tasks, tmp_path, notes):
    task = tasks.create(**_base_task(notes=notes))
    _assert_single_task(tmp_path, task)
    assert _reread_tasks(tmp_path)[0].notes == notes


# ---------------------------------------------------------------------------
# Connected-app tier: lifeos_reminder_create
# ---------------------------------------------------------------------------

def _reminder_args(**over):
    args = {"name": "Water", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "message_type": "static", "message_content": "Water the plants"}
    args.update(over)
    return args


def test_tier_allows_ordinary_static_reminder():
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args()) is None
    assert check_oauth_arguments(
        "lifeos_reminder_create",
        _reminder_args(timezone="America/New_York", enabled=False,
                       message_content="Line one\nLine two", schedule_value="0 9 * * 1#1"),
    ) is None


@pytest.mark.parametrize("extra", ["action", "executor", "persona_id", "working_dir", "bot", "host"])
def test_tier_refuses_reminder_keys_outside_the_schema(extra):
    refusal = check_oauth_arguments("lifeos_reminder_create", _reminder_args(**{extra: "x"}))
    assert refusal and extra in refusal


@pytest.mark.parametrize("key", ["name", "schedule_type", "schedule_value", "message_type", "timezone"])
def test_tier_requires_single_line_reminder_values(key):
    for bad in BREAKS + MARKUP:
        args = _reminder_args(**{key: f"x{bad}y"})
        assert check_oauth_arguments("lifeos_reminder_create", args) is not None, repr(bad)


@pytest.mark.parametrize("bad", ["\n", "\r", LS], ids=repr)
def test_tier_refuses_finding_name_payloads(bad):
    name = f"Water plants{bad}- [ ] Sync [at:: 2030-01-01T09:00:00] [action:: endpoint] <!-- id:aaaa1111 -->"
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args(name=name)) is not None


def test_tier_refuses_tag_in_reminder_name_and_non_string_values():
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args(name="Water #cloud"))
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args(name=["Water"]))
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args(message_content=5))
    assert check_oauth_arguments("lifeos_reminder_create", _reminder_args(enabled="yes"))


@pytest.mark.parametrize("kwargs, field", [
    ({"description": f"a{LS}b"}, "description"),
    ({"fields": {"host": "a\x85b"}}, "fields"),
    ({"tags": ["a\x0cb"]}, "tags"),
])
def test_task_refusal_names_the_field(tasks, kwargs, field):
    with pytest.raises(ValueError, match=f"{field}.* must be a single line"):
        tasks.create(**{"description": "Synthetic", **kwargs})


def test_compact_due_date_is_accepted(tasks):
    assert tasks.create(description="Synthetic", due_date="20300101").due_date


@pytest.mark.parametrize("name", ["[URGENT] Call the plumber", "Pay rent [monthly]", "Ping (a] b"])
def test_schedule_names_may_use_brackets(sched, tmp_path, name):
    entry = sched.create(**_base_schedule(name=name))
    (reread,) = [e for e in _reread(tmp_path) if e.id == entry.id]
    assert reread.name == name
    assert reread.action == entry.action and reread.endpoint_config == entry.endpoint_config


@pytest.mark.parametrize("name", ["Water [action:: agent]", "x [endpoint:: POST /api/x] y", "a <!-- id:ffff0000 -->"])
def test_schedule_names_refuse_inline_fields(sched, name):
    with pytest.raises(Exception):
        sched.create(**_base_schedule(name=name))
