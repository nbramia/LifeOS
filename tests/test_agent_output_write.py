"""Tests for the worker's always-write Agent Output behavior.

Every completed root #agent task lands a Markdown note in the vault's Agent
Output folder. One-off tasks get a new dated note; recurring (cron-scheduled)
tasks — recognised by a `sched-<id>` tag — append each fire to one shared note
per schedule, newest run on top.

These exercise `Worker._write_agent_output` directly (no poll loop), building a
bare Worker via __new__ since the method only needs settings + the schedule-name
resolver, which we stub.
"""
from __future__ import annotations

import re

import pytest

from api.services.agent_worker.session_store import Session
from api.services.agent_worker.worker import (
    Worker,
    _slugify,
    _split_frontmatter,
)

pytestmark = pytest.mark.unit


def _session(session_id="sess_abcdef123456", task_id="t1", routing="local"):
    return Session(
        task_id=task_id,
        session_id=session_id,
        status="completed",
        started_at=0,
        last_activity_at=0,
        routing=routing,
    )


@pytest.fixture
def worker_and_vault(tmp_path, monkeypatch):
    from config.settings import settings

    vault = tmp_path / "vault"
    monkeypatch.setattr(settings, "vault_path", vault)
    monkeypatch.setattr(settings, "agent_output_dir", "Agent Output")
    return Worker.__new__(Worker), vault


def _out_dir(vault):
    return vault / "Agent Output"


# ---------------------------------------------------------------------------
# One-off tasks
# ---------------------------------------------------------------------------

def test_one_off_writes_new_dated_file(worker_and_vault):
    w, vault = worker_and_vault
    task = {"id": "t1", "description": "Summarize my inbox", "tags": ["agent", "local"]}

    res = w._write_agent_output(_session(), task, "Here is the summary.")

    assert res is not None
    rel_path, url = res
    files = list(_out_dir(vault).glob("*.md"))
    assert len(files) == 1
    f = files[0]
    assert re.match(r"\d{4}-\d{2}-\d{2}-summarize-my-inbox-\w+\.md$", f.name)
    text = f.read_text(encoding="utf-8")
    assert "source: agent-worker" in text
    assert "Here is the summary." in text
    assert rel_path == f"Agent Output/{f.name}"
    assert url.startswith("obsidian://")


def test_one_off_same_slug_same_day_does_not_clobber(worker_and_vault):
    """Two one-off tasks with the same description on the same day must not
    overwrite each other — the session-id suffix keeps them distinct."""
    w, vault = worker_and_vault
    task = {"id": "t1", "description": "Daily digest", "tags": ["agent"]}

    w._write_agent_output(_session(session_id="sess_aaaaaa111111"), task, "run A")
    w._write_agent_output(_session(session_id="sess_bbbbbb222222"), task, "run B")

    files = sorted(p.name for p in _out_dir(vault).glob("*.md"))
    assert len(files) == 2


def test_returns_none_when_vault_unset(worker_and_vault, monkeypatch):
    w, vault = worker_and_vault
    from config.settings import settings

    monkeypatch.setattr(settings, "vault_path", None)
    res = w._write_agent_output(_session(), {"id": "t1", "description": "x"}, "body")
    assert res is None


# ---------------------------------------------------------------------------
# Recurring (cron) tasks
# ---------------------------------------------------------------------------

def test_recurring_first_run_creates_named_file(worker_and_vault, monkeypatch):
    w, vault = worker_and_vault
    monkeypatch.setattr(w, "_resolve_schedule_name", lambda sid: "Weekly Review")
    task = {
        "id": "t1",
        "description": "Draft my weekly review",
        "tags": ["agent", "cloud", "sched-ab12cd34"],
    }

    res = w._write_agent_output(_session(), task, "First run body.")

    assert res is not None
    # Filename is <schedule-slug>-<id>.md so distinct schedules sharing a name
    # never collide.
    f = _out_dir(vault) / "weekly-review-ab12cd34.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "schedule_id: ab12cd34" in text
    assert "source: agent-worker-recurring" in text
    assert "First run body." in text
    assert len(re.findall(r"^## ", text, re.M)) == 1


def test_recurring_second_run_prepends_newest_on_top(worker_and_vault, monkeypatch):
    w, vault = worker_and_vault
    monkeypatch.setattr(w, "_resolve_schedule_name", lambda sid: "Weekly Review")
    task = {"id": "t1", "description": "Draft my weekly review", "tags": ["sched-ab12cd34", "agent"]}

    w._write_agent_output(_session(session_id="sess_run1_000000"), task, "OLDER run body.")
    w._write_agent_output(_session(session_id="sess_run2_000000"), task, "NEWER run body.")

    files = list(_out_dir(vault).glob("*.md"))
    assert len(files) == 1, "all fires of one schedule share a single note"
    text = files[0].read_text(encoding="utf-8")
    # Both runs present, newest above oldest.
    assert "OLDER run body." in text
    assert "NEWER run body." in text
    assert text.index("NEWER run body.") < text.index("OLDER run body.")
    # Exactly one frontmatter block and two dated run headings, each closed by
    # its own `---` rule (criterion #4: prior runs sit below the rule).
    assert text.count("source: agent-worker-recurring") == 1
    assert len(re.findall(r"^## ", text, re.M)) == 2
    assert len(re.findall(r"^---$", _split_frontmatter(text)[1], re.M)) == 2


def test_recurring_preserves_created_date_bumps_updated(worker_and_vault, monkeypatch):
    """A new fire keeps the note's original `created` date and bumps `updated`."""
    w, vault = worker_and_vault
    monkeypatch.setattr(w, "_resolve_schedule_name", lambda sid: "Weekly Review")
    out = _out_dir(vault)
    out.mkdir(parents=True, exist_ok=True)
    # Seed an existing note from an earlier run with an old created date.
    (out / "weekly-review-ab12cd34.md").write_text(
        "---\n"
        "schedule: Weekly Review\n"
        "schedule_id: ab12cd34\n"
        "created: 2020-01-01\n"
        "updated: 2020-01-01\n"
        "source: agent-worker-recurring\n"
        "---\n\n"
        "## 2020-01-01 09:00\n\nancient run\n\n---\n",
        encoding="utf-8",
    )
    task = {"id": "t1", "description": "Draft my weekly review", "tags": ["sched-ab12cd34"]}

    w._write_agent_output(_session(), task, "fresh run")

    text = (out / "weekly-review-ab12cd34.md").read_text(encoding="utf-8")
    fm, _body = _split_frontmatter(text)
    assert "created: 2020-01-01" in fm
    assert "updated: 2020-01-01" not in fm  # bumped to today
    assert re.search(r"^updated: \d{4}-\d{2}-\d{2}$", fm, re.M)
    assert "ancient run" in text  # prior run retained


def test_recurring_name_resolution_failure_uses_id_filename(worker_and_vault, monkeypatch):
    """When the schedule name can't be resolved (deleted schedule / API error),
    fall back to a stable id-keyed filename so fires still group together."""
    w, vault = worker_and_vault
    monkeypatch.setattr(w, "_resolve_schedule_name", lambda sid: None)
    task = {"id": "t1", "description": "Nightly thing", "tags": ["sched-zz99zz99", "agent"]}

    res = w._write_agent_output(_session(), task, "body")

    assert res is not None
    assert (_out_dir(vault) / "recurring-zz99zz99.md").exists()


def test_schedule_id_from_task_detects_tag(worker_and_vault):
    w, _ = worker_and_vault
    assert w._schedule_id_from_task({"tags": ["agent", "sched-ab12cd34"]}) == "ab12cd34"
    assert w._schedule_id_from_task({"tags": ["#sched-ab12cd34"]}) == "ab12cd34"
    assert w._schedule_id_from_task({"tags": ["agent", "local"]}) is None
    assert w._schedule_id_from_task({}) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_split_frontmatter_roundtrip():
    doc = "---\nschedule: X\nupdated: 2026-06-21\n---\n\n## 2026-06-21 09:00\n\nbody\n"
    fm, body = _split_frontmatter(doc)
    assert fm.startswith("---\n") and fm.rstrip().endswith("---")
    assert body.lstrip("\n").startswith("## 2026-06-21 09:00")
    # No frontmatter → everything is body.
    assert _split_frontmatter("## just a heading\n") == ("", "## just a heading\n")


def test_slugify():
    assert _slugify("Draft my Weekly Review!") == "draft-my-weekly-review"
    assert _slugify("   ") == ""
    assert len(_slugify("x" * 200)) == 60
