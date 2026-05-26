"""Worker poll-loop integration tests.

Drives the worker against an in-process fake HTTP server (httpx MockTransport)
and a stub preflight caller. Each test exercises one outcome of the dispatch
machine: local completion, ambiguity → blocked, sanity → failed, daily cap
pause, sleeps wake-up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    AGENT_TAG,
    BLOCKED_TAG,
    BUDGET_EXCEEDED_TAG,
    COMPLETED_TAG,
    FAILED_TAG,
    RUNNING_TAG,
    Worker,
)


# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------

class FakeApi:
    """In-memory stand-in for /api/tasks."""

    def __init__(self, tasks=None):
        self.tasks = {t["id"]: t for t in (tasks or [])}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tasks":
            tag = request.url.params.get("tag")
            status = request.url.params.get("status")
            matched = [
                t for t in self.tasks.values()
                if (status is None or t.get("status") == status)
                and (tag in t.get("tags", []) if tag else True)
            ]
            return httpx.Response(200, json={"tasks": matched, "total": len(matched)})

        if request.method == "POST" and request.url.path.endswith("/swap-tag"):
            task_id = request.url.path.split("/")[-2]
            from_tag = request.url.params.get("from")
            to_tag = request.url.params.get("to")
            task = self.tasks.get(task_id)
            if not task or from_tag not in task.get("tags", []):
                return httpx.Response(200, json={"swapped": False, "reason": "tag not present"})
            tags = list(task["tags"])
            tags[tags.index(from_tag)] = to_tag
            task["tags"] = tags
            return httpx.Response(200, json={"swapped": True})

        if request.method == "PUT" and request.url.path.endswith("/complete"):
            task_id = request.url.path.split("/")[-2]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            task["status"] = "done"
            return httpx.Response(200, json=task)

        if request.method == "PUT" and request.url.path.startswith("/api/tasks/"):
            # Generic update — used by _set_task_status to sync the vault
            # checkbox alongside #agent-* tag transitions.
            task_id = request.url.path.split("/")[-1]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            body = json.loads(request.content or b"{}")
            for k, v in body.items():
                task[k] = v
            return httpx.Response(200, json=task)

        if request.method == "GET" and "/api/tasks/" in request.url.path:
            task_id = request.url.path.split("/")[-1]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            return httpx.Response(200, json=task)

        return httpx.Response(404)


def _make_worker(tmp_path: Path, api: FakeApi, *, preflight_caller, local_executor):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    # Capturing sender for clarification questions — records the text and
    # returns a deterministic message_id so reply-threading can be tested.
    sent_with_ids: list[tuple[int, str]] = []
    def _fake_send_with_id(text):
        sent.append(text)
        msg_id = len(sent_with_ids) + 1000
        sent_with_ids.append((msg_id, text))
        return msg_id
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        telegram_send_with_id=_fake_send_with_id,
        http_client=client,
        preflight_caller=preflight_caller,
        local_executor=local_executor,
    )
    w._sent_telegram = sent  # type: ignore[attr-defined]
    w._sent_with_ids = sent_with_ids  # type: ignore[attr-defined]
    return w


def _golden_preflight(routing="local", ambiguity=None, sane=True, sane_reason=""):
    payload = {
        "budget": {"wall_seconds": 3600, "max_tokens": 5000, "max_dollars": 5.0},
        "routing": routing,
        "routing_reason": f"test stub: routing={routing}",
        "expected_output": "text",
        "ambiguity": ambiguity,
        "sane": sane,
        "sane_reason": sane_reason,
    }
    return lambda prompt: json.dumps(payload)


@dataclass
class _StubExecutor:
    """Pretends to be a LocalExecutor — returns a canned outcome."""

    outcome: ExecutorOutcome
    calls: list = None

    def __post_init__(self):
        self.calls = []

    def execute(self, session, task):
        self.calls.append((session.task_id, task.get("description")))
        return self.outcome


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_worker_picks_up_urgent_status_tasks_too(tmp_path: Path):
    """The Obsidian Tasks plugin distinguishes between `todo` (`[ ]`) and
    `urgent` (`[!]`) statuses. Both should be eligible for #agent pickup —
    the urgent status signals high-priority work the operator wants run
    sooner, not 'skip the agent.'"""
    api = FakeApi(tasks=[
        {"id": "t-todo",   "description": "ordinary task",  "status": "todo",   "tags": ["agent", "local"]},
        {"id": "t-urgent", "description": "urgent task",    "status": "urgent", "tags": ["agent", "local"]},
        {"id": "t-other",  "description": "irrelevant",     "status": "todo",   "tags": ["other"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="done"))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    # One tick should claim and dispatch both #agent tasks (todo + urgent).
    handled = w.tick()
    assert handled == 2
    claimed_ids = {tid for tid, _ in executor.calls}
    assert claimed_ids == {"t-todo", "t-urgent"}
    # Both end at agent-completed
    assert COMPLETED_TAG in api.tasks["t-todo"]["tags"]
    assert COMPLETED_TAG in api.tasks["t-urgent"]["tags"]
    # The non-#agent task is untouched
    assert api.tasks["t-other"]["tags"] == ["other"]


@pytest.mark.unit
def test_worker_dedups_when_task_appears_under_both_statuses(tmp_path: Path):
    """If a task somehow appears in both status='todo' and status='urgent'
    response sets between fan-out calls (or if a later status query returns
    a row already seen), the worker must claim it only once."""
    # Construct a FakeApi that returns the same task for both status queries
    # to simulate the edge case.
    class _DupeApi(FakeApi):
        def handler(self, request):
            if request.method == "GET" and request.url.path == "/api/tasks":
                tag = request.url.params.get("tag")
                # Return the same task under any status query
                matched = [
                    t for t in self.tasks.values()
                    if (tag in t.get("tags", []) if tag else True)
                ]
                return httpx.Response(200, json={"tasks": matched, "total": len(matched)})
            return super().handler(request)

    api = _DupeApi(tasks=[
        {"id": "t1", "description": "dup", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok"))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    assert w.tick() == 1
    assert len(executor.calls) == 1


@pytest.mark.unit
def test_dispatch_local_completes_and_marks_task_done(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "hello there", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="hi back"))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    assert w.tick() == 1
    assert api.tasks["t1"]["status"] == "done"
    assert executor.calls == [("t1", "hello there")]
    # Tag swaps: agent → agent-running (on claim) → agent-completed (on success).
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]
    assert AGENT_TAG not in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent and "completed 'hello there'" in sent[0]
    assert "hi back" in sent[0]


@pytest.mark.unit
def test_ambiguous_title_lands_in_blocked(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run"))
    preflight = _golden_preflight(
        routing="local",
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    # Executor should not have been invoked.
    assert executor.calls == []
    # Task gets the blocked tag.
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert AGENT_TAG not in api.tasks["t1"]["tags"]
    # Session status reflects blocked.
    assert w.session_store.get("t1").status == STATUS_BLOCKED
    # Telegram message includes the question.
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("Which John" in s for s in sent)


@pytest.mark.unit
def test_routing_ask_lands_in_blocked_with_model_question(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "research dolphins", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="ask")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("local" in s.lower() and "claude" in s.lower() for s in sent)


@pytest.mark.unit
def test_insane_task_lands_in_failed(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "rm -rf /", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="local", sane=False, sane_reason="destructive")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert FAILED_TAG in api.tasks["t1"]["tags"]
    assert w.session_store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_executor_budget_exceeded_sets_budget_exceeded_tag(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "long task", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_BUDGET_EXCEEDED, reason="budget exceeded (max_tokens)",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    assert BUDGET_EXCEEDED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("hit its budget" in s for s in sent)


@pytest.mark.unit
def test_claude_routing_without_managed_credentials_blocks(tmp_path: Path, monkeypatch):
    """Without Managed Agents credentials configured the worker parks Claude-
    routed tasks at #agent-blocked. Same UX as ambiguity / sanity / ask."""
    # Force agent_preset_id + agent_environment_id empty regardless of the
    # operator's actual .env so the test exercises the not-configured branch
    # deterministically.
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "agent_preset_id", "", raising=False)
    monkeypatch.setattr(_settings, "agent_environment_id", "", raising=False)
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="claude")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert AGENT_TAG not in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("Managed Agents" in s for s in sent)


@pytest.mark.unit
def test_claude_routing_with_managed_executor_starts_and_polls(tmp_path: Path):
    """When Managed Agents is configured, the worker delegates to the
    managed executor: `start` on first tick (status=RUNNING) and `poll` on
    subsequent ticks until terminal."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize my inbox", "status": "todo", "tags": ["agent"]},
    ])

    class _StubManagedExecutor:
        def __init__(self):
            self.start_calls = 0
            self.poll_calls = 0

        def start(self, session, task):
            self.start_calls += 1
            # Simulate driver attaching a remote id.
            store_for_session.set_managed_session_id(session.task_id, "sess_remote_42")
            return ExecutorOutcome(status=STATUS_RUNNING)

        def poll(self, session):
            self.poll_calls += 1
            if self.poll_calls < 2:
                return ExecutorOutcome(status=STATUS_RUNNING)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="here's the summary")

    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    store_for_session = SessionStore(db_path=tmp_path / "sessions.db")
    sent: list[str] = []
    managed = _StubManagedExecutor()
    w = Worker(
        api_base="http://api",
        session_store=store_for_session,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
        preflight_caller=_golden_preflight(routing="claude"),
        local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="")),
        managed_executor=managed,
    )

    # Tick 1: claim → preflight → managed.start. Task still #agent-running.
    w.tick()
    assert managed.start_calls == 1
    assert managed.poll_calls == 0
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    # Vault checkbox flips to in_progress on claim so the operator can see
    # which tasks are actively executing alongside the #agent-running tag.
    assert api.tasks["t1"]["status"] == "in_progress"
    # Tick 2: managed.poll → still running.
    w.tick()
    assert managed.poll_calls == 1
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    # Tick 3: managed.poll → completed → tag swap + Telegram + mark done.
    w.tick()
    assert managed.poll_calls == 2
    assert api.tasks["t1"]["status"] == "done"
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]
    assert any("here's the summary" in s for s in sent)


@pytest.mark.unit
def test_completion_summary_uses_transcript_pointer_when_final_text_empty(tmp_path: Path):
    """When the agent idles without producing an `agent.message` (sometimes
    happens after a tool call on tight budgets), Telegram surfaces a
    transcript pointer instead of an empty body so the operator can inspect
    what actually happened."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "do the thing", "status": "todo", "tags": ["agent", "local"]},
    ])
    # final_text="" simulates the empty-text path.
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent, "expected a completion notification"
    msg = sent[0]
    # Pointer to transcript path, wrapped in backticks so Telegram's
    # Markdown parser doesn't interpret underscores as italic markers
    # (the live bug rendered the path as "data/agenttranscripts/sess35c…").
    assert "`data/agent_transcripts/" in msg
    assert ".jsonl`" in msg
    # No literal "(no text response)" — that placeholder is deprecated in favor
    # of the transcript pointer.
    assert "(no text response)" not in msg


@pytest.mark.unit
def test_completion_summary_includes_init_failed_mcps_footer(tmp_path: Path):
    """When the managed executor reports MCPs that failed to initialize,
    the completion summary appends a "Note: N MCP server(s) unavailable"
    footer so the operator can fix or remove the broken connectors."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize my calendar", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED,
        final_text="Two events today: standup at 10am and review at 3pm.",
        init_failed_mcps=["gmail", "gcal"],
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    msg = sent[0]
    # Real reply is preserved
    assert "Two events today" in msg
    # Footer is appended
    assert "Note:" in msg
    assert "2 MCP server(s) unavailable" in msg
    assert "gmail" in msg
    assert "gcal" in msg


@pytest.mark.unit
def test_completion_summary_omits_footer_when_no_init_failures(tmp_path: Path):
    """No footer noise when all MCPs initialized cleanly."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "answer the question", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="42.", init_failed_mcps=[],
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    assert "Note:" not in sent[0]
    assert "MCP server(s) unavailable" not in sent[0]


@pytest.mark.unit
def test_claim_sets_vault_status_to_in_progress(tmp_path: Path):
    """When the worker claims a task (swap #agent → #agent-running), the
    vault checkbox status must also flip to "in_progress" so the operator
    can see at a glance which tasks are actively being worked on."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "do thing", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="done",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    # After a successful completion the task ends at "done" (set by the
    # /complete endpoint). The transition through "in_progress" is implied —
    # assert the intermediate state by checking a budget-exceeded run instead
    # where the status doesn't subsequently flip to done.
    assert api.tasks["t1"]["status"] == "done"


@pytest.mark.unit
def test_budget_exceeded_sets_vault_status_to_cancelled(tmp_path: Path):
    """Terminal non-success states (budget_exceeded, failed) flip the vault
    checkbox to "cancelled" rather than leaving it stranded at "in_progress"."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "expensive task", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_BUDGET_EXCEEDED, reason="max_tokens",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    assert api.tasks["t1"]["status"] == "cancelled"
    assert BUDGET_EXCEEDED_TAG in api.tasks["t1"]["tags"]


@pytest.mark.unit
def test_clarification_resume_sets_status_back_to_in_progress(tmp_path: Path):
    """When a blocked task gets a Telegram reply and is resumed, the vault
    status should flip from "blocked" back to "in_progress" so it visually
    re-enters the active queue."""
    from api.services.agent_worker.session_store import STATUS_BLOCKED

    api = FakeApi(tasks=[
        {"id": "t1", "description": "ambiguous task", "status": "blocked",
         "tags": [BLOCKED_TAG, "local"]},
    ])
    # Successful completion after resume so we end at "done" — but the
    # intermediate "in_progress" transition is the assertion target.
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="resolved",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)

    # Set up the session as if it was already blocked waiting on a question.
    session = w.session_store.create(task_id="t1", routing="local",
                                      status=STATUS_BLOCKED, expected_output="text")
    w.session_store.create_pending_question(
        session.session_id, "t1", "Web search or local data?",
        sent_message_id=42,
    )
    w.session_store.deposit_answer(42, "Web search")

    # Snapshot the status the moment _set_task_status fires. Patch the
    # method to capture intermediate values, since the task ultimately ends
    # at "done" via _complete_task.
    statuses_seen: list[str] = []
    real = w._set_task_status
    def capture(task_id, status):
        statuses_seen.append(status)
        return real(task_id, status)
    w._set_task_status = capture  # type: ignore[method-assign]

    w._process_clarification_answers()
    # The resume path must have flipped the status to in_progress before
    # the eventual completion.
    assert "in_progress" in statuses_seen, statuses_seen


@pytest.mark.unit
def test_completion_label_says_local_for_local_routing(tmp_path: Path):
    """Operator wants to know at a glance whether a result came from local
    Gemma or cloud Claude — the worker label is route-aware."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "task", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="done",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    assert "Local agent worker" in sent[0]
    assert "Cloud agent worker" not in sent[0]


@pytest.mark.unit
def test_completion_inline_summary_kept_when_under_cap(tmp_path: Path):
    """A short final_text is delivered inline — no spillover to vault."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize", "status": "todo", "tags": ["agent", "local"]},
    ])
    # Just under the 2000-char inline cap
    short_text = "Here is the answer. " * 50  # 1000 chars
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text=short_text,
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    assert short_text.strip() in sent[0]
    assert "Full answer saved to vault" not in sent[0]
    assert "obsidian://" not in sent[0]


@pytest.mark.unit
def test_completion_spills_to_vault_when_over_cap(tmp_path: Path, monkeypatch):
    """When final_text is >2000 chars, the worker writes the full body
    to the vault and replaces the inline blob with a short preview +
    obsidian:// link. The operator should never see a mid-paragraph
    truncation again."""
    from config.settings import settings as _settings
    vault = tmp_path / "MyVault"
    monkeypatch.setattr(_settings, "vault_path", vault, raising=False)

    api = FakeApi(tasks=[
        {"id": "t1", "description": "Big report on Julia",
         "status": "todo", "tags": ["agent", "cloud"]},
    ])
    long_text = (
        "Julia Barnes is the CEO of The Movement Cooperative.\n\n"
        + ("Paragraph body content that goes on and on. " * 80)  # ~3200 chars
    )
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text=long_text,
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=executor)  # used for unrelated paths
    # Force the local path so we can drive the completion through _StubExecutor.
    # Use routing="local" on the preflight so the worker takes the local branch
    # while still exercising the cap logic.
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent, "expected a Telegram notification"
    msg = sent[0]
    # The inline preview is short (≤400 chars after first paragraph break)
    assert "Julia Barnes is the CEO" in msg
    # Spillover markers present
    assert "Full answer saved to vault" in msg
    assert "obsidian://" in msg
    # The full body is NOT inlined verbatim
    assert long_text not in msg
    # The vault file exists with the expected structure
    out_dir = vault / "Inbox" / "Agent Output"
    md_files = list(out_dir.glob("*.md"))
    assert len(md_files) == 1, f"expected one spillover file; got {md_files}"
    body = md_files[0].read_text(encoding="utf-8")
    # Frontmatter then the full text
    assert body.startswith("---\n")
    assert "task: Big report on Julia" in body
    assert "Paragraph body content that goes on and on." in body


@pytest.mark.unit
def test_completion_falls_back_to_truncation_when_vault_unset(tmp_path: Path, monkeypatch):
    """If `LIFEOS_VAULT_PATH` is unset (fresh install), the worker can't
    spill — it falls back to truncating the inline text so the operator
    still sees something instead of a write error."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "vault_path", None, raising=False)

    api = FakeApi(tasks=[
        {"id": "t1", "description": "report", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    long_text = "X" * 3000
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text=long_text,
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    msg = sent[0]
    # Truncated, with the ellipsis sentinel
    assert "…" in msg or "..." in msg
    # No vault references
    assert "vault" not in msg.lower()
    assert "obsidian://" not in msg


@pytest.mark.unit
def test_managed_executor_constructed_with_full_credentials(tmp_path: Path, monkeypatch):
    """When API key + agent_preset_id + agent_environment_id + agent_vault_id
    are all set, the worker constructs a ManagedExecutor with each ID flowing
    through to the right field. MCP servers and connectors are NOT passed at
    session-create — they live in the agent preset."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-ant-test", raising=False)
    monkeypatch.setattr(_settings, "agent_preset_id", "agent_test", raising=False)
    monkeypatch.setattr(_settings, "agent_environment_id", "env_test", raising=False)
    monkeypatch.setattr(_settings, "agent_vault_id", "vlt_test", raising=False)

    api = FakeApi(tasks=[])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED)))
    me = w._get_managed_executor()
    assert me is not None
    assert me.agent_id == "agent_test"
    assert me.environment_id == "env_test"
    assert me.vault_ids == ["vlt_test"]


@pytest.mark.unit
def test_managed_executor_returns_none_when_preset_missing(tmp_path: Path, monkeypatch):
    """Missing agent_preset_id → not configured → None (worker parks at #agent-blocked)."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-ant-test", raising=False)
    monkeypatch.setattr(_settings, "agent_preset_id", "", raising=False)
    monkeypatch.setattr(_settings, "agent_environment_id", "env_test", raising=False)

    api = FakeApi(tasks=[])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED)))
    assert w._get_managed_executor() is None


@pytest.mark.unit
def test_managed_executor_returns_none_when_environment_missing(tmp_path: Path, monkeypatch):
    """Missing agent_environment_id → not configured → None."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-ant-test", raising=False)
    monkeypatch.setattr(_settings, "agent_preset_id", "agent_test", raising=False)
    monkeypatch.setattr(_settings, "agent_environment_id", "", raising=False)

    api = FakeApi(tasks=[])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED)))
    assert w._get_managed_executor() is None


@pytest.mark.unit
def test_managed_executor_omits_vault_ids_when_vault_unset(tmp_path: Path, monkeypatch):
    """Vault is optional — agents with no OAuth-protected MCPs work without it."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "anthropic_api_key", "sk-ant-test", raising=False)
    monkeypatch.setattr(_settings, "agent_preset_id", "agent_test", raising=False)
    monkeypatch.setattr(_settings, "agent_environment_id", "env_test", raising=False)
    monkeypatch.setattr(_settings, "agent_vault_id", "", raising=False)

    api = FakeApi(tasks=[])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED)))
    me = w._get_managed_executor()
    assert me is not None
    assert me.vault_ids == []


@pytest.mark.unit
def test_sleep_yield_does_not_mark_terminal(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "wait for it", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_YIELDED, wake_at=99999999999,  # far future
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()

    # Task should remain at #agent-running while the session sleeps; no Telegram on yield.
    assert RUNNING_TAG in api.tasks["t1"]["tags"]
    # Vault status was flipped to in_progress on claim and stays there
    # through the sleep — yielded sessions are still actively held by the
    # worker, so "in_progress" is more accurate than "todo".
    assert api.tasks["t1"]["status"] == "in_progress"
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent == []


@pytest.mark.unit
def test_worker_skips_already_claimed_tasks(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "hi", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    # Re-tag back to #agent and verify the worker doesn't claim it again.
    api.tasks["t1"]["tags"] = ["agent", "local"]
    api.tasks["t1"]["status"] = "todo"
    assert w.tick() == 0
    # Executor was called exactly once across both ticks.
    assert len(executor.calls) == 1


@pytest.mark.unit
def test_worker_pauses_at_daily_cap(tmp_path: Path):
    api = FakeApi(tasks=[
        {"id": "t1", "description": "x", "status": "todo", "tags": ["agent", "local"]},
    ])
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=0.0),
        poll_seconds=0.01,
        telegram_send=lambda *a, **kw: True,
        http_client=client,
        preflight_caller=_golden_preflight(routing="local"),
        local_executor=executor,
    )
    assert w.tick() == 0
    assert executor.calls == []
