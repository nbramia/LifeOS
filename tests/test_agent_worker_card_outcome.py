"""The Review-card outcome a completed agent run records: the agent's own
completion summary and, for a coding session, its branch and any pull
request it opened.

Kept out of the vault task's `notes` field on purpose — the drawer renders
it as its own read-only section (`session_store.card_outcomes`), not
markdown appended to the editable notes textarea, so nothing here ever
needs the notes sanitization `task_manager._validate_text_fields` enforces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.git_worktree import FinalizeResult
from api.services.agent_worker.session_store import STATUS_BLOCKED, SessionStore
from tests.test_agent_worker_card_completion_ordering import (
    EARNED_FINAL_TEXT,
    THIN_FINAL_TEXT,
    _claude_code_executor,
    _codex_executor,
    _make_real_route_worker,
    _seed_claimed_task,
)

pytestmark = pytest.mark.unit

PR_URL = "https://github.com/nbramia/LifeOS/pull/1234"


# ---------------------------------------------------------------------------
# session_store: card_outcomes / pr_status_cache
# ---------------------------------------------------------------------------


def test_record_and_get_card_outcome_round_trips(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.record_card_outcome(
        "task-1", session_id="sess-1", engine_label="Claude Code",
        summary="Fixed the bug.", branch="feat/x", pr_urls=[PR_URL],
    )
    outcome = store.get_card_outcome("task-1")
    assert outcome["session_id"] == "sess-1"
    assert outcome["engine_label"] == "Claude Code"
    assert outcome["summary"] == "Fixed the bug."
    assert outcome["branch"] == "feat/x"
    assert outcome["pr_urls"] == [PR_URL]


def test_get_card_outcome_missing_returns_none(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    assert store.get_card_outcome("no-such-task") is None


def test_record_card_outcome_replaces_the_prior_run_not_appends(tmp_path: Path):
    """A resumed session that completes again overwrites the earlier run's
    record — a card never carries more than one outcome."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.record_card_outcome(
        "task-1", session_id="sess-1", engine_label="Claude Code",
        summary="First run.", branch="feat/x", pr_urls=[],
    )
    store.record_card_outcome(
        "task-1", session_id="sess-1", engine_label="Claude Code",
        summary="Second run, after a resume.", branch="feat/x", pr_urls=[PR_URL],
    )
    outcome = store.get_card_outcome("task-1")
    assert outcome["summary"] == "Second run, after a resume."
    assert outcome["pr_urls"] == [PR_URL]


def test_record_card_outcome_tolerates_notes_hostile_characters(tmp_path: Path):
    """The outcome record has no notes-field sanitization constraints —
    it never touches `notes` — so carriage returns and an HTML comment
    opener pass straight through and round-trip exactly."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    hostile = "did <!-- a thing -->\r\nline two"
    store.record_card_outcome(
        "task-1", session_id="sess-1", engine_label="Claude Code",
        summary=hostile, branch=None, pr_urls=[],
    )
    assert store.get_card_outcome("task-1")["summary"] == hostile


def test_list_outcome_pr_urls_dedupes_across_tasks(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=[PR_URL],
    )
    store.record_card_outcome(
        "task-2", session_id="s2", engine_label="Codex",
        summary="b", branch=None, pr_urls=[PR_URL, "https://github.com/o/r/pull/9"],
    )
    assert sorted(store.list_outcome_pr_urls()) == sorted(
        [PR_URL, "https://github.com/o/r/pull/9"]
    )


def test_list_stale_pr_urls_includes_never_checked_and_excludes_fresh(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=[PR_URL],
    )
    # Never refreshed at all — always stale.
    assert store.list_stale_pr_urls(ttl_s=300) == [PR_URL]

    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None})
    assert store.list_stale_pr_urls(ttl_s=300) == []

    # A checked_at far enough in the past falls outside the TTL again.
    import time
    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None},
                            checked_at=int(time.time()) - 1000)
    assert store.list_stale_pr_urls(ttl_s=300) == [PR_URL]


def test_upsert_pr_status_failed_refresh_keeps_last_known_value_but_flags_stale(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None})
    assert store.get_pr_status(PR_URL) == {
        "url": PR_URL, "number": 1234, "title": "t", "state": "OPEN",
        "merged_at": None, "checked_at": store.get_pr_status(PR_URL)["checked_at"], "stale": False,
    }
    # A failed refresh attempt (viewer returned None) never clears the
    # previously observed state — only flips `stale` on.
    store.upsert_pr_status(PR_URL, None)
    status = store.get_pr_status(PR_URL)
    assert status["state"] == "OPEN" and status["number"] == 1234
    assert status["stale"] is True


def test_pr_status_reflects_a_merge_that_happened_after_completion(tmp_path: Path):
    """The acceptance case: a PR open at completion time later merges, and
    a later refresh (not the write at completion) is what makes the
    cached status catch up."""
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None})
    assert store.get_pr_status(PR_URL)["state"] == "OPEN"
    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "MERGED", "merged_at": "2026-09-18T04:00:00Z"})
    status = store.get_pr_status(PR_URL)
    assert status["state"] == "MERGED" and status["stale"] is False


# ---------------------------------------------------------------------------
# worker completion paths
# ---------------------------------------------------------------------------


def test_claude_code_completion_records_outcome(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text=EARNED_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "ship it"}])

    outcome = sessions.get_card_outcome(task_id)
    assert outcome is not None
    assert outcome["engine_label"] == "Claude Code"
    assert outcome["summary"] == EARNED_FINAL_TEXT
    # No worker-provisioned worktree in this fixture (no execution_spec) —
    # FinalizeResult.applicable is False, so no branch/PR is fabricated.
    assert outcome["branch"] is None
    assert outcome["pr_urls"] == []
    # The notes field is untouched — the outcome never gets written there.
    assert not (manager.get(task_id).notes or "")


def test_codex_completion_records_outcome(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="codex")
    manager.update(task_id, notes="Original brief.")
    worker._codex_executor = _codex_executor(
        tmp_path, sessions, worker.transcript_store, final_text=EARNED_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="codex")

    worker._dispatch_codex_session(session, [{"content": "ship it"}])

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["engine_label"] == "Codex"
    assert outcome["summary"] == EARNED_FINAL_TEXT
    # Pre-existing notes are never touched by recording the outcome.
    assert manager.get(task_id).notes == "Original brief."


def test_parked_completion_records_no_outcome(tmp_path: Path, monkeypatch):
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text=THIN_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="claude_code")

    worker._dispatch_claude_code_session(session, [{"content": "ship it"}])

    assert sessions.get(task_id).status == STATUS_BLOCKED
    assert sessions.get_card_outcome(task_id) is None


def test_operator_origin_completion_records_no_outcome(tmp_path: Path, monkeypatch):
    """A `/claude`-style operator spawn has no backing vault card — nothing
    to attach an outcome to, matching the same gate the completion
    notification path already uses."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    worker._claude_code_executor = _claude_code_executor(
        sessions, worker.transcript_store, final_text=EARNED_FINAL_TEXT,
    )
    session = sessions.create(task_id=task_id, routing="claude_code", origin="operator")

    worker._dispatch_claude_code_session(session, [{"content": "ship it"}])

    assert sessions.get_card_outcome(task_id) is None


def test_record_card_outcome_uses_finalize_result_branch_and_pr_over_transcript_grep(tmp_path: Path, monkeypatch):
    """Branch/PR come from the authoritative `FinalizeResult`, not
    `_discover_wip_branch`'s transcript grep, whenever a result is given."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    session = sessions.create(task_id=task_id, routing="claude_code")
    # A transcript branch that would be found by the grep-based fallback —
    # must be ignored in favor of the FinalizeResult's own branch.
    worker.transcript_store.append(session.session_id, "claude_code_tool_use", {
        "name": "Bash", "input": {"command": "git checkout -b grepped-branch"},
    })
    git_result = FinalizeResult(applicable=True, branch="authoritative-branch", pushed=True, pr_url=PR_URL, pr_opened=True)

    worker._record_card_outcome(session, "Did the thing.", git_result=git_result)

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["branch"] == "authoritative-branch"
    assert outcome["pr_urls"] == [PR_URL]


def test_record_card_outcome_never_falls_back_to_transcript_grep_without_a_finalize_result(tmp_path: Path, monkeypatch):
    """No `FinalizeResult` at all (the generic local/remote/Hermes/Managed
    Agents path never provisions a worktree) records no branch — a
    transcript command that merely looks like a branch checkout is never
    trusted as one."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    session = sessions.create(task_id=task_id, routing="claude_code")
    worker.transcript_store.append(session.session_id, "claude_code_tool_use", {
        "name": "Bash", "input": {"command": "git checkout -b grepped-branch"},
    })

    worker._record_card_outcome(session, "Did the thing.", git_result=None)

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["branch"] is None
    assert outcome["pr_urls"] == []


def test_record_card_outcome_records_a_finalize_results_absent_branch_as_is(tmp_path: Path, monkeypatch):
    """A `FinalizeResult` that is applicable but never determined a branch
    (e.g. an unresolvable host, per `resolve_runner_for_host`'s own
    `FinalizeResult(applicable=True, error=...)`) records that absence —
    never papered over by a transcript grep that would fabricate one."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    session = sessions.create(task_id=task_id, routing="claude_code")
    worker.transcript_store.append(session.session_id, "claude_code_tool_use", {
        "name": "Bash", "input": {"command": "git checkout -b grepped-branch"},
    })
    git_result = FinalizeResult(applicable=True, branch=None, error="cannot resolve host")

    worker._record_card_outcome(session, "Did the thing.", git_result=git_result)

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["branch"] is None
    assert outcome["pr_urls"] == []


def test_record_card_outcome_records_no_branch_when_finalize_result_is_not_applicable(tmp_path: Path, monkeypatch):
    """`applicable=False` (no worker-provisioned worktree at all) records
    no branch either, even with a matching transcript command — the same
    "use the FinalizeResult's own absence, never grep" rule."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    session = sessions.create(task_id=task_id, routing="claude_code")
    worker.transcript_store.append(session.session_id, "claude_code_tool_use", {
        "name": "Bash", "input": {"command": "git checkout -b grepped-branch"},
    })
    git_result = FinalizeResult(applicable=False)

    worker._record_card_outcome(session, "Did the thing.", git_result=git_result)

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["branch"] is None
    assert outcome["pr_urls"] == []


def test_generic_cloud_completion_records_outcome_with_no_branch_or_pr(tmp_path: Path, monkeypatch):
    """A non-coding completion (local/Hermes/Managed Agents) records the
    agent's summary with no branch/PR lines rather than a blank or
    misleading claim."""
    worker, manager, sessions = _make_real_route_worker(tmp_path, monkeypatch)
    task_id = _seed_claimed_task(manager, assignee="claude")
    session = sessions.create(task_id=task_id, routing="claude")

    worker._record_card_outcome(session, "Drafted the reply to the synthetic vendor.",
                                 engine_label="Cloud agent worker")

    outcome = sessions.get_card_outcome(task_id)
    assert outcome["engine_label"] == "Cloud agent worker"
    assert outcome["summary"] == "Drafted the reply to the synthetic vendor."
    assert outcome["branch"] is None
    assert outcome["pr_urls"] == []
