"""Worker completion-path wiring for git worktree sessions: a fully-complete
session pushes its branch and opens a pull request (URL folded into the
completion notice); a question-pausing session pushes without opening one
(branch folded into the question); and a session that ends without
completing still gets its leftover uncommitted changes safety-netted and
pushed. Exercises `_dispatch_claude_code_session` and
`_dispatch_codex_session` directly with a stub CLI executor, a real
temporary git worktree, and a fake `gh` on PATH — no real GitHub call.
"""
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.execution import BillingClass, ExecutionConstraints, ExecutionSpec
from api.services.agent_worker.git_worktree import ensure_worktree
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path, name: str = "repo") -> Path:
    origin = root / f"{name}-origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    assert _git(repo, "push", "-q", "origin", "main").returncode == 0
    return repo


def _install_fake_gh(tmp_path: Path, monkeypatch, *, pr_url: str = "https://github.com/x/y/pull/9") -> Path:
    """A `gh` on PATH ahead of the real one: `pr view` always reports no
    existing PR, `pr create` prints a fake URL. Every invocation is logged
    so a test can assert whether `gh` was ever consulted at all."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "gh-calls.log"
    script = bin_dir / "gh"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo "no PR found" >&2
  exit 1
fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "{pr_url}"
  exit 0
fi
exit 1
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return log


def _spec_for(working_dir: str, executor: str) -> dict:
    return ExecutionSpec(
        executor=executor, provider=executor, runtime="cli",
        model_id=None, effort=None, host=None, working_dir=working_dir,
        persona_id=None, parent_session_id=None, root_session_id=None, reply_destination=None,
        budget=None, constraints=ExecutionConstraints(), billing=BillingClass.SUBSCRIPTION,
        resolved_at=datetime.now(timezone.utc),
    ).to_dict()


@dataclass
class _StubCliExecutor:
    outcome: ExecutorOutcome
    calls: list = field(default_factory=list)

    def execute(self, session, task):
        self.calls.append(task)
        return self.outcome


def _worker(tmp_path: Path, *, claude_code_executor=None, codex_executor=None):
    sent_texts: list[str] = []

    def _send_with_id(text):
        sent_texts.append(text)
        return [777]

    def _send(text, chat_id=None):
        sent_texts.append(text)
        return True

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=_send,
        telegram_send_with_id=_send_with_id,
        http_client=client,
        claude_code_executor=claude_code_executor,
        codex_executor=codex_executor,
    )
    return worker, sent_texts


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def test_claude_code_full_completion_pushes_and_opens_pr(tmp_path: Path, monkeypatch):
    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path)
    provisioned = ensure_worktree(str(repo), "task-complete", "fix the thing")
    (Path(provisioned.working_dir) / "change.txt").write_text("real work\n")
    _git(Path(provisioned.working_dir), "add", "change.txt")
    _git(Path(provisioned.working_dir), "commit", "-q", "-m", "did the work")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
    ))
    worker, sent = _worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-complete", routing="claude_code", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    assert any("https://github.com/x/y/pull/9" in t for t in sent)
    assert gh_log.exists() and "create" in gh_log.read_text()
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in ls_remote.stdout


def test_claude_code_question_pause_pushes_without_pr(tmp_path: Path, monkeypatch):
    from api.services.agent_worker.claude_code_executor import REASON_AWAITING_CLARIFICATION

    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo2")
    provisioned = ensure_worktree(str(repo), "task-blocked", "fix the thing")
    (Path(provisioned.working_dir) / "partial.txt").write_text("partial work\n")
    _git(Path(provisioned.working_dir), "add", "partial.txt")
    _git(Path(provisioned.working_dir), "commit", "-q", "-m", "partial")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION,
        final_text="Which environment should I target?",
    ))
    worker, sent = _worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-blocked", routing="claude_code", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    assert any(f"Branch: `{provisioned.branch}`" in t for t in sent)
    assert not any("http" in t for t in sent)  # no PR URL anywhere
    assert not gh_log.exists()  # gh was never consulted at all
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in ls_remote.stdout


def test_claude_code_failed_session_safety_nets_leftover_changes(tmp_path: Path, monkeypatch):
    from api.services.agent_worker.claude_code_executor import REASON_TIMEOUT

    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo3")
    provisioned = ensure_worktree(str(repo), "task-failed", "fix the thing")
    # Left uncommitted by the session before it died.
    (Path(provisioned.working_dir) / "leftover.txt").write_text("never committed\n")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason=REASON_TIMEOUT))
    worker, sent = _worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-failed", routing="claude_code", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    assert any(f"Branch: `{provisioned.branch}`" in t for t in sent)
    assert not gh_log.exists()
    status = _git(Path(provisioned.working_dir), "status", "--porcelain")
    assert status.stdout.strip() == ""  # the leftover change was committed
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in ls_remote.stdout


def test_claude_code_no_worktree_session_unaffected(tmp_path: Path, monkeypatch):
    """An operator /claude session with no worker-provisioned worktree
    (execution_spec has no working_dir at all) gets no git footer."""
    _install_fake_gh(tmp_path, monkeypatch)

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
    ))
    worker, sent = _worker(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(task_id="task-plain", routing="claude_code", origin="operator")

    worker._dispatch_claude_code_session(session, [{"content": "do a thing"}])

    assert sent and sent[0].startswith("📌 task-plain\n\nAll done.")
    assert not any("Branch:" in t or "PR:" in t for t in sent)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def test_codex_full_completion_pushes_and_opens_pr(tmp_path: Path, monkeypatch):
    gh_log = _install_fake_gh(tmp_path, monkeypatch, pr_url="https://github.com/x/y/pull/11")
    repo = _init_repo_with_origin(tmp_path, name="repo4")
    provisioned = ensure_worktree(str(repo), "task-codex-complete", "fix the thing")
    (Path(provisioned.working_dir) / "change.txt").write_text("real work\n")
    _git(Path(provisioned.working_dir), "add", "change.txt")
    _git(Path(provisioned.working_dir), "commit", "-q", "-m", "did the work")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
    ))
    worker, sent = _worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(
        task_id="task-codex-complete", routing="codex", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "codex"),
    )

    worker._dispatch_codex_session(session, [{"content": "fix the thing"}])

    assert any("https://github.com/x/y/pull/11" in t for t in sent)
    assert gh_log.exists() and "create" in gh_log.read_text()


def test_codex_failed_session_safety_nets_leftover_changes(tmp_path: Path, monkeypatch):
    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo5")
    provisioned = ensure_worktree(str(repo), "task-codex-failed", "fix the thing")
    (Path(provisioned.working_dir) / "leftover.txt").write_text("never committed\n")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="crashed"))
    worker, sent = _worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(
        task_id="task-codex-failed", routing="codex", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "codex"),
    )

    worker._dispatch_codex_session(session, [{"content": "fix the thing"}])

    assert any(f"Branch: `{provisioned.branch}`" in t for t in sent)
    assert not gh_log.exists()
    status = _git(Path(provisioned.working_dir), "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_codex_operator_killed_session_still_gets_safety_net_but_no_notice(tmp_path: Path, monkeypatch):
    """An operator kill suppresses the failure notice, but leftover work is
    still committed and pushed — the safety net doesn't depend on there
    being a notice to ride along in."""
    from api.services.agent_worker.codex_executor import REASON_KILLED

    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo6")
    provisioned = ensure_worktree(str(repo), "task-codex-killed", "fix the thing")
    (Path(provisioned.working_dir) / "leftover.txt").write_text("never committed\n")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason=REASON_KILLED))
    worker, sent = _worker(tmp_path, codex_executor=stub)
    session = worker.session_store.create(
        task_id="task-codex-killed", routing="codex", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "codex"),
    )

    worker._dispatch_codex_session(session, [{"content": "fix the thing"}])

    assert sent == []  # no post-kill notice, per existing convention
    assert not gh_log.exists()
    status = _git(Path(provisioned.working_dir), "status", "--porcelain")
    assert status.stdout.strip() == ""  # still safety-netted despite no notice
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in ls_remote.stdout


# ---------------------------------------------------------------------------
# Codex [CLARIFY] question-pause (F1), and card-state (F3): a question-pause
# must actually move the backing board task to the Human queue lane
# (agent-running -> agent-blocked, status -> blocked), and an operator
# answer must swap it back so the session stays resumable.
# ---------------------------------------------------------------------------

def _worker_with_http_capture(tmp_path: Path, *, claude_code_executor=None, codex_executor=None):
    sent_texts: list[str] = []
    http_calls: list[tuple[str, str, dict]] = []
    # Tracks each task's current lifecycle tag for real, so a fresh dispatch
    # sees "agent-running" but a later resume (after the block transition
    # actually swapped it) correctly sees "agent-blocked" — a bare stub
    # returning one fixed tag can't distinguish those two moments.
    tag_state: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        http_calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "POST" and req.url.path.endswith("/swap-tag"):
            task_id = req.url.path.split("/api/tasks/")[1].rsplit("/swap-tag", 1)[0]
            params = dict(req.url.params)
            current = tag_state.get(task_id, "agent-running")
            swapped = current == params.get("from")
            if swapped:
                tag_state[task_id] = params.get("to")
            return httpx.Response(200, json={"swapped": swapped})
        if req.method == "GET" and "/api/tasks/" in req.url.path:
            task_id = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": task_id, "tags": [tag_state.get(task_id, "agent-running")]})
        return httpx.Response(200, json={"tasks": []})

    def _send_with_id(text):
        sent_texts.append(text)
        return [777]

    def _send(text, chat_id=None):
        sent_texts.append(text)
        return True

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=_send,
        telegram_send_with_id=_send_with_id,
        http_client=client,
        claude_code_executor=claude_code_executor,
        codex_executor=codex_executor,
    )
    return worker, sent_texts, http_calls


def test_codex_clarify_question_pauses_pushes_no_pr_and_registers_answer_anchor(tmp_path: Path, monkeypatch):
    from api.services.agent_worker.codex_executor import REASON_AWAITING_CLARIFICATION

    gh_log = _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo7")
    provisioned = ensure_worktree(str(repo), "task-codex-clarify", "fix the thing")
    (Path(provisioned.working_dir) / "partial.txt").write_text("partial work\n")
    _git(Path(provisioned.working_dir), "add", "partial.txt")
    _git(Path(provisioned.working_dir), "commit", "-q", "-m", "partial")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION,
        final_text="Which deployment environment should I target?",
    ))
    worker, sent, _ = _worker_with_http_capture(tmp_path, codex_executor=stub)
    session = worker.session_store.create(
        task_id="task-codex-clarify", routing="codex", origin="operator",
        execution_spec=_spec_for(provisioned.working_dir, "codex"),
    )

    worker._dispatch_codex_session(session, [{"content": "fix the thing"}])

    assert any("Which deployment environment should I target?" in t for t in sent)
    assert any(f"Branch: `{provisioned.branch}`" in t for t in sent)
    assert not any("http" in t for t in sent)  # no PR URL anywhere
    assert not gh_log.exists()  # gh never consulted on a question pause
    ls_remote = _git(repo, "ls-remote", "--heads", "origin", provisioned.branch)
    assert provisioned.branch in ls_remote.stdout
    # An answer anchor was registered so a threaded reply can resume it.
    question = worker.session_store.get_open_question_by_session_id(session.session_id)
    assert question is not None
    assert question["task_id"] == "task-codex-clarify"


def test_claude_code_question_pause_moves_card_to_human_queue(tmp_path: Path, monkeypatch):
    from api.services.agent_worker.claude_code_executor import REASON_AWAITING_CLARIFICATION

    _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo8")
    provisioned = ensure_worktree(str(repo), "task-claude-humanq", "fix the thing")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION,
        final_text="Which environment?",
    ))
    worker, sent, http_calls = _worker_with_http_capture(tmp_path, claude_code_executor=stub)
    # No `origin="operator"` — a board-backed session, so vault reconciliation applies.
    session = worker.session_store.create(
        task_id="task-claude-humanq", routing="claude_code",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    swap_calls = [c for c in http_calls if c[1].endswith("/swap-tag")]
    assert any(c[2].get("from") == "agent-running" and c[2].get("to") == "agent-blocked" for c in swap_calls)
    status_calls = [c for c in http_calls if c[0] == "PUT" and c[1] == "/api/tasks/task-claude-humanq"]
    assert status_calls  # the vault checkbox status was updated too


def test_codex_clarify_question_moves_card_to_human_queue(tmp_path: Path, monkeypatch):
    from api.services.agent_worker.codex_executor import REASON_AWAITING_CLARIFICATION

    _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo9")
    provisioned = ensure_worktree(str(repo), "task-codex-humanq", "fix the thing")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION,
        final_text="Which environment?",
    ))
    worker, sent, http_calls = _worker_with_http_capture(tmp_path, codex_executor=stub)
    session = worker.session_store.create(
        task_id="task-codex-humanq", routing="codex",
        execution_spec=_spec_for(provisioned.working_dir, "codex"),
    )

    worker._dispatch_codex_session(session, [{"content": "fix the thing"}])

    swap_calls = [c for c in http_calls if c[1].endswith("/swap-tag")]
    assert any(c[2].get("from") == "agent-running" and c[2].get("to") == "agent-blocked" for c in swap_calls)


def test_claude_code_blocked_session_resume_swaps_card_back_to_running(tmp_path: Path, monkeypatch):
    """The answer must still resume the session in the same worktree, and
    the card must leave the Human queue lane once answered."""
    from api.services.agent_worker.claude_code_executor import REASON_AWAITING_CLARIFICATION

    _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo10")
    provisioned = ensure_worktree(str(repo), "task-claude-resume", "fix the thing")

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_BLOCKED, reason=REASON_AWAITING_CLARIFICATION,
        final_text="Which environment?",
    ))
    worker, sent, http_calls = _worker_with_http_capture(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-claude-resume", routing="claude_code",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )
    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    question = worker.session_store.get_open_question_by_session_id(session.session_id)
    assert question is not None
    worker.session_store.deposit_answer_by_id(question["id"], "use staging")
    claimed = worker.session_store.claim_answered_unprocessed_questions()
    q = next(item for item in claimed if item["id"] == question["id"])
    session = worker.session_store.get(session.task_id)

    http_calls.clear()
    worker._resume_as_followup(q, session, "use staging")

    swap_calls = [c for c in http_calls if c[1].endswith("/swap-tag")]
    assert any(c[2].get("from") == "agent-blocked" and c[2].get("to") == "agent-running" for c in swap_calls)
    # Still points at the same worktree — the worker never re-provisions on resume.
    reloaded = worker.session_store.get(session.task_id)
    from api.services.agent_worker.execution import ExecutionSpec
    assert ExecutionSpec.from_dict(reloaded.execution_spec).working_dir == provisioned.working_dir


def test_claude_code_completion_reports_push_failure_but_still_reaches_review(tmp_path: Path, monkeypatch):
    """A push/PR failure (or a branch with nothing to push) must be
    reported plainly in the completion notice — never silently swallowed
    as success — but the card still reaches the Review lane, same as any
    other completed session; git trouble doesn't strand the card."""
    _install_fake_gh(tmp_path, monkeypatch)
    repo = _init_repo_with_origin(tmp_path, name="repo11")
    provisioned = ensure_worktree(str(repo), "task-claude-pushfail", "fix the thing")
    (Path(provisioned.working_dir) / "change.txt").write_text("work\n")
    _git(Path(provisioned.working_dir), "add", "change.txt")
    _git(Path(provisioned.working_dir), "commit", "-q", "-m", "did the work")
    # Break the push so finalize reports an honest error instead of a PR.
    _git(Path(provisioned.working_dir), "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
    ))
    worker, sent, http_calls = _worker_with_http_capture(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-claude-pushfail", routing="claude_code",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    assert any("⚠️ git:" in t for t in sent)  # honest, not pretending success
    swap_calls = [c for c in http_calls if c[1].endswith("/swap-tag")]
    assert any(c[2].get("from") == "agent-running" and c[2].get("to") == "agent-completed" for c in swap_calls)


def test_claude_code_completion_threads_session_host_into_finalize(tmp_path: Path, monkeypatch):
    """`Worker._finalize_worktree_for_session` must pass the session's own
    assigned host through to `finalize_worktree_session` — otherwise a
    remote-host session's worktree would silently be finalized (or
    silently skipped) against this worker's own filesystem instead."""
    from api.services.agent_worker import git_worktree as git_worktree_module

    repo = _init_repo_with_origin(tmp_path, name="repo12")
    provisioned = ensure_worktree(str(repo), "task-claude-hostthread", "fix the thing")

    captured: dict = {}
    real_finalize = git_worktree_module.finalize_worktree_session

    def spying_finalize(working_dir, **kwargs):
        captured["host"] = kwargs.get("host")
        return real_finalize(working_dir, **kwargs)

    monkeypatch.setattr(git_worktree_module, "finalize_worktree_session", spying_finalize)

    stub = _StubCliExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="All done.", notifications_sent=1,
    ))
    worker, sent, _ = _worker_with_http_capture(tmp_path, claude_code_executor=stub)
    session = worker.session_store.create(
        task_id="task-claude-hostthread", routing="claude_code", host="studio",
        execution_spec=_spec_for(provisioned.working_dir, "claude_code"),
    )

    worker._dispatch_claude_code_session(session, [{"content": "fix the thing"}])

    assert captured.get("host") == "studio"
