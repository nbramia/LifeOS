"""
Tests for the reminders.json → Scheduler store migration (#247).
"""
import json

import pytest

from api.services.scheduler_store import SchedulerStore
from scripts.migrate_reminders_to_scheduler import migrate

pytestmark = pytest.mark.unit


def _write_legacy(json_path):
    json_path.write_text(json.dumps({
        "reminders": [
            {
                "id": "r1", "name": "Morning Briefing", "schedule_type": "cron",
                "schedule_value": "0 9 * * *", "message_type": "prompt",
                "message_content": "Summarize my day", "enabled": True,
                "timezone": "America/New_York",
                "last_triggered_at": "2026-05-01T09:00:00+00:00",
            },
            {
                "id": "r2", "name": "Water Plants", "schedule_type": "cron",
                "schedule_value": "0 18 * * *", "message_type": "static",
                "message_content": "hydrate the ferns", "enabled": True,
            },
            {
                "id": "r3", "name": "Status Poll", "schedule_type": "cron",
                "schedule_value": "0 8 * * *", "message_type": "endpoint",
                "message_content": "", "enabled": False,
                "endpoint_config": {"endpoint": "/health", "method": "GET"},
            },
        ]
    }), encoding="utf-8")


def _store(tmp_path):
    return SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "idx.json")


def test_migration_creates_schedules_with_mapped_actions(tmp_path):
    json_path = tmp_path / "reminders.json"
    _write_legacy(json_path)
    store = _store(tmp_path)

    n = migrate(json_path=json_path, store=store)
    assert n == 3

    by_name = {e.name: e for e in store.list_all()}
    assert by_name["Morning Briefing"].action == "prompt"
    assert by_name["Morning Briefing"].timezone == "America/New_York"
    assert by_name["Morning Briefing"].last_triggered_at == "2026-05-01T09:00:00+00:00"
    assert by_name["Water Plants"].action == "notify"  # static → notify
    assert by_name["Status Poll"].action == "endpoint"
    assert by_name["Status Poll"].enabled is False
    assert by_name["Status Poll"].endpoint_config == {"endpoint": "/health", "method": "GET"}


def test_migrated_entries_are_editable_lines_in_inbox(tmp_path):
    json_path = tmp_path / "reminders.json"
    _write_legacy(json_path)
    store = _store(tmp_path)
    migrate(json_path=json_path, store=store)

    inbox = store.inbox_path.read_text(encoding="utf-8")
    assert "Morning Briefing" in inbox
    assert "[cron:: 0 9 * * *]" in inbox
    assert "[action:: prompt]" in inbox
    # Each line carries a stable id comment.
    assert inbox.count("<!-- id:") == 3


def test_migration_keeps_backup_and_is_idempotent(tmp_path):
    json_path = tmp_path / "reminders.json"
    _write_legacy(json_path)
    store = _store(tmp_path)

    first = migrate(json_path=json_path, store=store)
    assert first == 3
    # Backup retained, marker written.
    assert json_path.exists()
    assert json_path.with_suffix(".json.migrated").exists()

    # Second run is a no-op (marker guards it) — no duplicates.
    second = migrate(json_path=json_path, store=store)
    assert second == 0
    assert len(store.list_all()) == 3


def test_migration_force_reruns(tmp_path):
    json_path = tmp_path / "reminders.json"
    _write_legacy(json_path)
    store = _store(tmp_path)
    migrate(json_path=json_path, store=store)
    again = migrate(json_path=json_path, store=store, force=True)
    assert again == 3


def test_migration_no_source_is_noop(tmp_path):
    store = _store(tmp_path)
    assert migrate(json_path=tmp_path / "missing.json", store=store) == 0
    assert store.list_all() == []
