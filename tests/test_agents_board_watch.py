"""Watcher -> board publish path for the /agents Kanban board (#850).

Acceptance criterion: "The board updates within three seconds of an external
vault edit without a page reload." The task watcher's debounce is 2.0s (see
`api/services/task_watcher.py`); this test proves an externally-written tag
change is visible via the stream's cached read (`_get_board_cached()`, TTL
`_BOARD_CACHE_TTL = 0.25s`) well inside the remaining budget, using the REAL
production debounce (not sped up) so the 3s claim is actually exercised end
to end: real filesystem watcher -> TaskManager.reindex_file ->
_get_board_cached() reading the same TaskManager instance through the same
cache the stream's own tick reads. This exercises the debounce leg and the
cache leg (round-2 finding 7) — it does NOT add the stream's separate
`_BOARD_STREAM_INTERVAL` (0.5s) tick delay, which only applies to an actual
SSE connection (covered by `TestBoardStream` in
tests/test_agents_board_api.py) and is accounted for arithmetically in the
2.75s worst case documented in docs/specs/technical/agent-viz.md.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import api.services.task_manager as task_manager_module
import api.services.scheduler_store as scheduler_store_module
from api.services.task_manager import TaskManager
from api.services.scheduler_store import SchedulerStore
from api.services.task_watcher import TaskWatcher, _DEBOUNCE_SECONDS
from api.routes import agents as agents_route

pytestmark = pytest.mark.unit


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    task_manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json",
    )
    monkeypatch.setattr(task_manager_module, "_task_manager", task_manager)

    scheduler_store = SchedulerStore(
        vault_path=tmp_path / "vault", index_path=tmp_path / "scheduler_index.json",
    )
    monkeypatch.setattr(scheduler_store_module, "_scheduler_store", scheduler_store)

    # Board building also touches the session store / snapshot — point those
    # at empty temp fixtures so the board build doesn't fall over.
    from api.services.agent_worker.session_store import SessionStore
    from api.services.agent_worker.transcript_store import TranscriptStore
    monkeypatch.setattr(agents_route, "_session_store", SessionStore(db_path=tmp_path / "sessions.db"))
    monkeypatch.setattr(agents_route, "_transcript_store", TranscriptStore(transcripts_dir=tmp_path / "transcripts"))
    monkeypatch.setattr(agents_route, "_claude_code_snapshot", lambda: ([], []))
    monkeypatch.setattr(agents_route, "_codex_snapshot", lambda: ([], []))
    # The board cache (round-2 finding 6) is module-level and TTL'd — reset
    # it so an earlier test's cached board can't leak into this one.
    monkeypatch.setattr(agents_route, "_board_cache", None)

    return task_manager, scheduler_store


class TestBoardReflectsExternalVaultEditWithinThreeSeconds:
    async def test_external_tag_edit_moves_lane_within_three_seconds(self, stores):
        task_manager, _scheduler_store = stores
        task = task_manager.create("Handle the ticket")
        first_board = await agents_route._get_board_cached()
        assert first_board["lanes"]["unassigned"][0]["id"] == task.id

        # Uses the REAL debounce (api/services/task_watcher.py's
        # _DEBOUNCE_SECONDS = 2.0), not a sped-up test value, so this proves
        # the actual production timing budget, not an idealized one.
        watcher = TaskWatcher(tasks_dir=task_manager.tasks_dir, on_change=task_manager.reindex_file)
        watcher.start()
        try:
            inbox = task_manager.tasks_dir / "Inbox.md"
            content = inbox.read_text(encoding="utf-8")
            assert task.id in content
            # Tag the task #me from outside the process — like an operator
            # editing the vault directly in Obsidian.
            patched = content.replace(f"<!-- id:{task.id} -->", f"#me <!-- id:{task.id} -->")
            edit_started_at = time.monotonic()
            inbox.write_text(patched, encoding="utf-8")

            deadline = edit_started_at + 3.0
            lane = None
            while time.monotonic() < deadline:
                # Read through the same cached path the stream's own tick
                # uses, not the raw builder — this is the leg round-2
                # finding 7 asked the test to actually exercise.
                board = await agents_route._get_board_cached()
                assigned_ids = [c["id"] for c in board["lanes"]["assigned"]]
                if task.id in assigned_ids:
                    lane = "assigned"
                    break
                await asyncio.sleep(0.05)

            elapsed = time.monotonic() - edit_started_at
            assert lane == "assigned", (
                f"external edit not reflected in the board within 3s "
                f"(elapsed={elapsed:.2f}s, debounce={_DEBOUNCE_SECONDS}s)"
            )
            assert elapsed <= 3.0
        finally:
            watcher.stop()
