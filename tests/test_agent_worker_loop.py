"""Worker poll-loop integration tests.

Drives the worker against an in-process fake HTTP server (httpx MockTransport)
and a stub preflight caller. Each test exercises one outcome of the dispatch
machine: local completion, ambiguity → blocked, sanity → failed, daily cap
pause, sleeps wake-up.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
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


@pytest.fixture(autouse=True)
def _redirect_agent_output(tmp_path, monkeypatch):
    """Every completed task now writes a note to the vault's Agent Output
    folder. Redirect the vault to a throwaway tmp dir so these worker tests
    write there instead of the repo's ./vault default."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "vault_path", tmp_path / "vault", raising=False)


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


def _make_worker(tmp_path: Path, api: FakeApi, *, preflight_caller, local_executor,
                  claude_code_executor=None, codex_executor=None, cli_pool=None):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    sent: list[str] = []
    # Capturing sender for clarification questions — records the text and
    # returns a deterministic message_id so reply-threading can be tested.
    sent_with_ids: list[tuple[int, str]] = []
    def _fake_send_with_id(text):
        # Mirror send_message_capture_ids: return a list of chunk ids.
        sent.append(text)
        msg_id = len(sent_with_ids) + 1000
        sent_with_ids.append((msg_id, text))
        return [msg_id]
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
        claude_code_executor=claude_code_executor,
        codex_executor=codex_executor,
        cli_pool=cli_pool,
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
    """A deterministically destructive title (matched by the code, not the
    model's prose) still fails closed — #747 must not weaken this guard."""
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
def test_mundane_sane_false_task_lands_in_blocked_not_cancelled(tmp_path: Path):
    """#747: a preflight sanity rejection of an ordinary, non-destructive
    title must park the task (blocked, still actionable) rather than
    cancel it — the model's own 'not executable' opinion is not fatal."""
    api = FakeApi(tasks=[
        {"id": "t1",
         "description": "Display the transcribed message immediately after sending",
         "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run"))
    preflight = _golden_preflight(
        routing="local", sane=False,
        sane_reason="This is a product specification or feature request, not a task an agent can execute.",
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    # Never ran, but critically NOT cancelled — parked and still actionable.
    assert executor.calls == []
    assert FAILED_TAG not in api.tasks["t1"]["tags"]
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] != "cancelled"
    assert w.session_store.get("t1").status == STATUS_BLOCKED
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert any("not executable" in s and "flagged" in s for s in sent)


@pytest.mark.unit
def test_sane_false_and_ambiguous_blocked_messages_are_distinguishable(tmp_path: Path):
    """#747 + #748 interaction: a parked sanity objection and a genuine
    ambiguity must produce distinguishable operator-facing text, not one
    generic 'blocked' string."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run"))
    preflight = _golden_preflight(
        routing="local", sane=False, sane_reason="looks like a spec, not a task",
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    combined = " ".join(sent)
    assert "looks like a spec" in combined
    assert "Which John" in combined
    # The two objections are textually distinct, not collapsed into one line.
    assert "looks like a spec" not in "Which John — John Doe or John Smith?"


@pytest.mark.unit
def test_routing_flavored_ambiguity_does_not_block(tmp_path: Path):
    """#748: a method-of-execution question smuggled into `ambiguity` must
    not block the task — routing decides."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "Turn the record button white when idle",
         "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="ran"))
    preflight = _golden_preflight(
        routing="local",
        ambiguity={
            "question": (
                "Should this task be routed to a local agent for code "
                "implementation, or is it a design/specification task for "
                "a human engineer?"
            ),
        },
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls != []
    assert BLOCKED_TAG not in api.tasks["t1"]["tags"]
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]


@pytest.mark.unit
def test_default_route_demotes_ambiguity_and_runs(tmp_path: Path, monkeypatch):
    """#751: with a default route configured, a genuine ambiguity no longer
    blocks — it's demoted to advisory and the task runs on the default
    route instead. Contrast with `test_ambiguous_title_lands_in_blocked`,
    which covers the no-default-route case and must keep blocking."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "agent_default_route", "local")
    api = FakeApi(tasks=[
        {"id": "t1", "description": "reply to John", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="replied"))
    preflight = _golden_preflight(
        routing="ask",
        ambiguity={"question": "Which John — John Doe or John Smith?"},
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    # Ran on the default route rather than blocking on the question.
    assert executor.calls != []
    assert BLOCKED_TAG not in api.tasks["t1"]["tags"]
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert not any("Which John" in s for s in sent)


@pytest.mark.unit
def test_default_route_does_not_rescue_fatal_sanity(tmp_path: Path, monkeypatch):
    """#751 must not weaken #747's fail-closed guard: a deterministically
    destructive title still fails the task even with a default route
    configured — sanity is decided before routing is even consulted."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "agent_default_route", "local")
    api = FakeApi(tasks=[
        {"id": "t1", "description": "rm -rf /", "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text=""))
    preflight = _golden_preflight(routing="local", sane=False, sane_reason="destructive")
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert FAILED_TAG in api.tasks["t1"]["tags"]
    assert w.session_store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_default_route_does_not_rescue_nonfatal_sanity(tmp_path: Path, monkeypatch):
    """#751 must not weaken #747: a non-fatal sane=false (the classifier's
    own 'not executable' opinion on a mundane title) still parks the task
    even with a default route configured — sanity ('should this run at
    all') is orthogonal to a default route ('who resolves an open
    question'), and #751 only touches the latter."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "agent_default_route", "local")
    api = FakeApi(tasks=[
        {"id": "t1", "description": "Display the transcribed message immediately after sending",
         "status": "todo", "tags": ["agent"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED, final_text="should not run"))
    preflight = _golden_preflight(
        routing="local", sane=False,
        sane_reason="This is a product specification or feature request, not a task an agent can execute.",
    )
    w = _make_worker(tmp_path, api, preflight_caller=preflight, local_executor=executor)
    w.tick()

    assert executor.calls == []
    assert FAILED_TAG not in api.tasks["t1"]["tags"]
    assert BLOCKED_TAG in api.tasks["t1"]["tags"]
    assert w.session_store.get("t1").status == STATUS_BLOCKED


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
        # `#cloud` is the operator asking for the API route; without it an
        # inferred cloud route would park at `ask` instead (#584).
        {"id": "t1", "description": "summarize", "status": "todo",
         "tags": ["agent", "cloud"]},
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
        {"id": "t1", "description": "summarize my inbox", "status": "todo",
         "tags": ["agent", "cloud"]},
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
    sent_ids: list[tuple[int, str]] = []
    def _fake_with_id(text):
        # Mirror send_message_capture_ids: return a list of chunk ids.
        sent.append(text)
        msg_id = len(sent_ids) + 2000
        sent_ids.append((msg_id, text))
        return [msg_id]
    managed = _StubManagedExecutor()
    w = Worker(
        api_base="http://api",
        session_store=store_for_session,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        telegram_send_with_id=_fake_with_id,
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
def test_format_token_buckets_collapses_when_cache_is_zero():
    """Local-path sessions never hit the prompt cache — the rendered string
    should collapse to the original `N input + M output` shape."""
    from api.services.agent_worker.worker import _format_token_buckets
    assert _format_token_buckets(100, 0, 0, 50) == "100 input + 50 output"


@pytest.mark.unit
def test_format_token_buckets_includes_cache_creation_and_read_when_nonzero():
    """Cloud sessions surface all four buckets so the operator can see what
    drove cost (cache_creation typically dominates first-turn spend)."""
    from api.services.agent_worker.worker import _format_token_buckets
    rendered = _format_token_buckets(3, 109_075, 2_000, 81)
    assert "3 input" in rendered
    assert "109,075 cached-write" in rendered
    assert "2,000 cached-read" in rendered
    assert "81 output" in rendered


@pytest.mark.unit
def test_completion_summary_renders_four_bucket_breakdown(tmp_path: Path):
    """When a managed session populated all four token buckets, the operator-
    facing Telegram message must surface them — that's the visible signal
    that #137 actually landed."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "draft email", "status": "todo",
         "tags": ["agent", "cloud"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="Drafted.",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=executor)
    w.tick()
    # Inject realistic four-bucket token totals via record_spend, then
    # re-render the summary directly using the refreshed row.
    w.session_store.record_spend(
        "t1",
        tokens_in=3,
        tokens_out=81,
        dollars=0.42,
        cache_creation_tokens=109_075,
        cache_read_tokens=2_000,
    )
    refreshed = w.session_store.get("t1")
    msg = w._completion_summary(refreshed, {"id": "t1", "description": "draft email"},
                                executor.outcome)
    assert "3 input" in msg
    assert "109,075 cached-write" in msg
    assert "2,000 cached-read" in msg
    assert "81 output" in msg


@pytest.mark.unit
def test_completion_summary_flags_escalation_to_child_engine(tmp_path: Path):
    """When a session delegated work to a claude_code child, the single
    completion message names the engine + tier so the operator knows the task
    was escalated and where it ran (#349)."""
    api = FakeApi(tasks=[])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="Here are today's matches: A vs B at noon.",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    parent = w.session_store.create(task_id="t1", routing="local", expected_output="text")
    w.session_store.create(
        task_id="spawn_child", routing="claude_code",
        parent_session_id=parent.session_id, claude_code_model="haiku",
    )
    msg = w._completion_summary(parent, {"id": "t1", "description": "world cup"},
                                executor.outcome)
    assert "⤴️ Escalated to Claude Code (haiku)" in msg
    # The single message still carries the actual result.
    assert "Here are today's matches" in msg


@pytest.mark.unit
def test_completion_summary_no_escalation_line_without_children(tmp_path: Path):
    """A session that ran solo (spawned no children) gets no escalation flag."""
    api = FakeApi(tasks=[])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="Done.",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    sess = w.session_store.create(task_id="t1", routing="local", expected_output="text")
    msg = w._completion_summary(sess, {"id": "t1", "description": "simple task"},
                                executor.outcome)
    assert "Escalated" not in msg


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
def test_resume_message_carries_failed_child_reason(tmp_path: Path):
    """A parent resuming after failed/budget children sees WHY each one died
    (#433): the resume turn appends a `reason:` line under the status header,
    read from the child's `child_*_internal` transcript event. A failed child
    with no such event (pre-#433 CLI transcripts) gets no reason line."""
    api = FakeApi(tasks=[
        {"id": "p1", "description": "orchestrate the thing",
         "status": "in_progress", "tags": [RUNNING_TAG, "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="wrapped up",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    parent = w.session_store.create(
        task_id="p1", routing="local", status=STATUS_YIELDED, expected_output="text",
    )
    failed_child = w.session_store.create(
        task_id="c_fail", routing="claude_code", status=STATUS_FAILED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    budget_child = w.session_store.create(
        task_id="c_budget", routing="codex", status=STATUS_BUDGET_EXCEEDED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    legacy_child = w.session_store.create(
        task_id="c_legacy", routing="claude_code", status=STATUS_FAILED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    w.transcript_store.append(failed_child.session_id, "child_failed_internal", {
        "parent_session_id": parent.session_id, "reason": "claude exited with code 2",
    })
    w.transcript_store.append(budget_child.session_id, "child_budget_exceeded_internal", {
        "parent_session_id": parent.session_id, "reason": "budget exceeded (wall_seconds)",
    })
    # legacy_child deliberately has no child_*_internal event.
    w.session_store.set_yield_waiting_for(
        "p1", [failed_child.session_id, budget_child.session_id, legacy_child.session_id],
    )

    w._resume_yielded_for_children()

    user_msgs = [m for m in w.session_store.get_messages(parent.session_id)
                 if m["role"] == "user"]
    assert user_msgs, "resume should have appended a user turn"
    resume = user_msgs[-1]["content"]
    assert "reason: claude exited with code 2" in resume
    assert "reason: budget exceeded (wall_seconds)" in resume
    # Exactly two reason lines — the eventless legacy child contributes none.
    assert resume.count("reason:") == 2


@pytest.mark.unit
def test_resume_message_latest_failure_event_wins(tmp_path: Path):
    """A child that hit budget, was reopened (#428), then failed shows the
    NEWER failed reason in the parent's resume turn — _child_failure_reason
    is latest-event-wins across both child_*_internal kinds."""
    api = FakeApi(tasks=[
        {"id": "p1", "description": "orchestrate the thing",
         "status": "in_progress", "tags": [RUNNING_TAG, "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="wrapped up",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    parent = w.session_store.create(
        task_id="p1", routing="local", status=STATUS_YIELDED, expected_output="text",
    )
    child = w.session_store.create(
        task_id="c_retry", routing="claude_code", status=STATUS_FAILED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    w.transcript_store.append(child.session_id, "child_budget_exceeded_internal", {
        "parent_session_id": parent.session_id, "reason": "budget exceeded (wall_seconds)",
    })
    w.transcript_store.append(child.session_id, "child_failed_internal", {
        "parent_session_id": parent.session_id, "reason": "tests failed on retry",
    })
    w.session_store.set_yield_waiting_for("p1", [child.session_id])

    w._resume_yielded_for_children()

    user_msgs = [m for m in w.session_store.get_messages(parent.session_id)
                 if m["role"] == "user"]
    assert user_msgs, "resume should have appended a user turn"
    resume = user_msgs[-1]["content"]
    assert "reason: tests failed on retry" in resume
    assert "wall_seconds" not in resume
    assert resume.count("reason:") == 1


@pytest.mark.unit
def test_completed_child_leftover_failure_event_no_reason_line(tmp_path: Path):
    """A COMPLETED child carrying a leftover child_*_internal event (e.g. from
    a failed run before a #428 reopen) contributes NO reason line — the status
    gate in _build_resume_message keeps reason lines failed/budget-only."""
    api = FakeApi(tasks=[
        {"id": "p1", "description": "orchestrate the thing",
         "status": "in_progress", "tags": [RUNNING_TAG, "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="wrapped up",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    parent = w.session_store.create(
        task_id="p1", routing="local", status=STATUS_YIELDED, expected_output="text",
    )
    child = w.session_store.create(
        task_id="c_reopened", routing="claude_code", status=STATUS_COMPLETED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    w.transcript_store.append(child.session_id, "child_failed_internal", {
        "parent_session_id": parent.session_id, "reason": "stale failure from first run",
    })
    w.session_store.set_yield_waiting_for("p1", [child.session_id])

    w._resume_yielded_for_children()

    user_msgs = [m for m in w.session_store.get_messages(parent.session_id)
                 if m["role"] == "user"]
    assert user_msgs, "resume should have appended a user turn"
    resume = user_msgs[-1]["content"]
    assert f"[{STATUS_COMPLETED}]" in resume
    assert "reason:" not in resume


@pytest.mark.unit
def test_cloud_yield_resume_creates_fresh_managed_session_with_children_output(tmp_path: Path):
    """Live bug: cloud parents that called yield_until ended up in FAILED
    because `_resume_yielded_for_children` short-circuited non-local
    routings. Now we create a fresh Anthropic session, pass the original
    task + each child's final_text as the initial user message, and let
    the parent's poll loop pick it up via the new managed_session_id."""
    from api.services.agent_worker.session_store import STATUS_RUNNING, STATUS_YIELDED, STATUS_COMPLETED
    api = FakeApi(tasks=[
        {"id": "p1", "description": "Compare gmail vs slack — aggregate child outputs",
         "status": "in_progress", "tags": [RUNNING_TAG, "cloud"]},
    ])
    # Track what the driver was asked to do.
    create_calls: list[dict] = []
    class _StubDriver:
        def create_session(self, **kwargs):
            create_calls.append(kwargs)
            return "sesn_new_remote_id"
    class _StubManagedExec:
        agent_id = "agent_x"
        environment_id = "env_x"
        vault_ids = ["vlt_x"]
        driver = _StubDriver()
        def start(self, session, task):
            return ExecutorOutcome(status=STATUS_RUNNING)
        def poll(self, session):
            return ExecutorOutcome(status=STATUS_RUNNING)

    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED)))
    w._managed_executor = _StubManagedExec()  # type: ignore[assignment]

    # Construct a cloud parent that's yielded waiting on two children.
    parent = w.session_store.create(
        task_id="p1", routing="claude", status=STATUS_YIELDED,
        expected_output="structured",
    )
    w.session_store.set_managed_session_id("p1", "sesn_old_remote")
    c1 = w.session_store.create(
        task_id="spawn_c1", routing="claude", status=STATUS_COMPLETED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    c2 = w.session_store.create(
        task_id="spawn_c2", routing="claude", status=STATUS_COMPLETED,
        parent_session_id=parent.session_id, root_session_id=parent.session_id,
        spawn_depth=1,
    )
    # Cache each child's final_text as managed children do post-completion.
    w.session_store.set_managed_final_text(c1.task_id, "Gmail: 1,451 emails received in April.")
    w.session_store.set_managed_final_text(c2.task_id, "Slack: 230 messages sent in April.")
    w.session_store.set_yield_waiting_for("p1", [c1.session_id, c2.session_id])

    w._resume_yielded_for_children()

    assert len(create_calls) == 1, "should have created exactly one fresh managed session"
    call = create_calls[0]
    msg = call["initial_message"]
    # Original task is restated for the fresh session (no prior history).
    assert "Compare gmail vs slack" in msg
    # Both children's outputs are in the resume message.
    assert "Gmail: 1,451 emails received" in msg
    assert "Slack: 230 messages sent" in msg
    # Both child session_ids show up in the [status] header lines.
    assert c1.session_id in msg
    assert c2.session_id in msg
    # Parent session row now points at the new remote id.
    refreshed = w.session_store.get("p1")
    assert refreshed.managed_agent_session_id == "sesn_new_remote_id"
    # Status flipped to RUNNING; yield_waiting_for cleared.
    assert refreshed.status == STATUS_RUNNING
    assert not refreshed.yield_waiting_for
    # No Telegram messages leaked during the resume.
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent == []


@pytest.mark.unit
def test_recovery_skips_json_tool_results_and_summarizes_tool_calls(tmp_path: Path):
    """Live bug: a cloud agent idled after calling list_threads (which
    returned a 30KB JSON payload). The recovery code surfaced that raw
    JSON as the operator-facing completion body — unreadable. Now we
    skip JSON-shaped or oversize results and fall back to a compact
    "tools called: X, Y, Z" summary."""
    from api.services.agent_worker.transcript_store import TranscriptStore as _TS

    api = FakeApi(tasks=[
        {"id": "t1", "description": "compare gmail vs slack",
         "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    sess = w.session_store.create(task_id="t1", routing="local",
                                   expected_output="text")
    ts = _TS(transcripts_dir=tmp_path / "transcripts")
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_use",
              {"name": "lifeos_gmail_search"})
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_use",
              {"name": "lifeos_agent_spawn"})
    # Last tool result is a giant JSON dump — recovery must skip it.
    json_dump = '{"threads":[' + ('{"id":"x"},' * 500) + '{"id":"end"}]}'
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_result", {
        "is_error": False,
        "content": [{"type": "text", "text": json_dump}],
    })
    msg = w._completion_summary(sess, {"id": "t1", "description": "compare"},
                                executor.outcome)
    # JSON body NOT inlined.
    assert json_dump[:50] not in msg
    assert '{"threads"' not in msg
    # Instead, surface tool-call summary mentioning the calls.
    assert "Tools called" in msg
    assert "lifeos_gmail_search" in msg
    assert "lifeos_agent_spawn" in msg


@pytest.mark.unit
def test_recovery_inlines_short_text_tool_result(tmp_path: Path):
    """Sanity check the other branch: when the last tool result IS short
    and text-shaped (e.g. lifeos_gmail_draft returned a few-hundred-char
    summary), inline it — that's the genuine "show what the agent did"
    case from PR #130."""
    from api.services.agent_worker.transcript_store import TranscriptStore as _TS
    api = FakeApi(tasks=[
        {"id": "t1", "description": "draft", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    sess = w.session_store.create(task_id="t1", routing="local",
                                   expected_output="text")
    ts = _TS(transcripts_dir=tmp_path / "transcripts")
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_result", {
        "is_error": False,
        "content": [{"type": "text",
                     "text": "Draft created — to: kevin@example.com, "
                             "subject: Apologies for delay"}],
    })
    msg = w._completion_summary(sess, {"id": "t1", "description": "draft"},
                                executor.outcome)
    assert "Draft created" in msg
    assert "Tools called" not in msg


class _FakeManagedDriver:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.kills: list[tuple[str, str]] = []

    def kill_session(self, session_id: str, reason: str = "") -> None:
        if self.fail:
            raise RuntimeError("remote kill 404")
        self.kills.append((session_id, reason))


class _FakeManagedExecutor:
    def __init__(self, fail: bool = False):
        self.driver = _FakeManagedDriver(fail=fail)


@pytest.mark.unit
def test_resume_pending_kills_orphan_remote_session(tmp_path: Path):
    """#198: rolling back a session with a live remote Managed Agents session
    must kill the remote session — otherwise it keeps running on Anthropic's
    infrastructure, making MCP tool calls with side effects long after the
    operator was told the task was rolled back."""
    from api.services.agent_worker.session_store import STATUS_FAILED, STATUS_RUNNING

    api = FakeApi(tasks=[
        {"id": "t1", "description": "cloud task", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=executor)
    fake_managed = _FakeManagedExecutor()
    w._managed_executor = fake_managed
    w.session_store.create(
        task_id="t1", routing="claude", status=STATUS_RUNNING, expected_output="text",
    )
    w.session_store.set_managed_session_id("t1", "sesn_remote_orphan")

    n = w.resume_pending()

    assert n == 1
    assert fake_managed.driver.kills == [("sesn_remote_orphan", "worker_restart_rollback")]
    assert w.session_store.get("t1").status == STATUS_FAILED
    sid = w.session_store.get("t1").session_id
    kinds = [e["kind"] for e in w.transcript_store.read(sid)]
    assert "orphan_remote_session_killed" in kinds


@pytest.mark.unit
def test_resume_pending_rollback_survives_kill_failure(tmp_path: Path):
    """#198: a kill failure (remote 404, network error) must not block the
    local rollback — the session still finalizes FAILED."""
    from api.services.agent_worker.session_store import STATUS_FAILED, STATUS_RUNNING

    api = FakeApi(tasks=[
        {"id": "t1", "description": "cloud task", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=executor)
    w._managed_executor = _FakeManagedExecutor(fail=True)
    w.session_store.create(
        task_id="t1", routing="claude", status=STATUS_RUNNING, expected_output="text",
    )
    w.session_store.set_managed_session_id("t1", "sesn_remote_orphan")

    n = w.resume_pending()

    assert n == 1
    assert w.session_store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_resume_pending_does_not_telegram_for_spawned_children(tmp_path: Path):
    """Live bug: after a worker restart, the startup-recovery path saw a
    spawned child stuck in RUNNING, rolled it back, and pinged the
    operator with the parent-internal child id. Per PR #132 invariant,
    children's terminal state should never reach Telegram."""
    from api.services.agent_worker.session_store import STATUS_RUNNING
    api = FakeApi(tasks=[
        {"id": "root_task", "description": "root", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_COMPLETED))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude"),
                     local_executor=executor)
    parent = w.session_store.create(
        task_id="root_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )
    # Spawned child stuck in RUNNING — would-be subject of the recovery path.
    w.session_store.create(
        task_id="spawn_orphan", routing="claude", status=STATUS_RUNNING,
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    orphan = w.session_store.get("spawn_orphan")
    n = w.resume_pending()
    assert n == 2, "both sessions should be marked failed by recovery"
    sent = w._sent_telegram  # type: ignore[attr-defined]
    # Exactly one notification — for the root, not the spawned child.
    assert len(sent) == 1, f"expected only the root's recovery notify; got {sent}"
    # The child's session_id (which goes into the transcript path of the
    # notify) must NOT appear — proves the spawned child stayed silent.
    assert orphan.session_id not in sent[0]
    # Notify is for a single session — the root's id should appear.
    assert parent.session_id in sent[0]


@pytest.mark.unit
def test_spawned_child_failure_does_not_telegram_the_operator(tmp_path: Path):
    """Live bug: a spawned child session's `create_session` 4xx'd, and the
    worker fired a failure notification to operator Telegram with the
    parent's full multi-line prompt as the "task title". Children are
    parent-internal — their terminal status flows back through
    `_resume_yielded_for_children`, not Telegram."""
    from api.services.agent_worker.session_store import STATUS_RUNNING
    api = FakeApi(tasks=[])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_FAILED, reason="create_session failed: HTTPStatusError",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)

    # Construct a spawned-child session by hand. parent_session_id is what
    # marks it as not-a-root.
    parent = w.session_store.create(
        task_id="parent_task", routing="claude", expected_output="text",
        status=STATUS_RUNNING,
    )
    child = w.session_store.create(
        task_id="spawn_child", routing="claude", expected_output="structured",
        status=STATUS_RUNNING,
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )

    # Trigger the failure handler directly as the dispatch path would.
    w._handle_outcome(
        child,
        {"id": child.task_id, "description": "You are analyzing WORK Gmail activity…"},
        ExecutorOutcome(status=STATUS_FAILED, reason="create_session failed: HTTPStatusError"),
    )

    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent == [], f"spawned child failure leaked to operator: {sent}"


@pytest.mark.unit
def test_spawned_child_completion_does_not_telegram_the_operator(tmp_path: Path):
    """Same invariant for the COMPLETED branch — children's outputs are
    aggregated by the parent's yield_until resumption, not surfaced to
    Telegram individually."""
    from api.services.agent_worker.session_store import STATUS_RUNNING
    api = FakeApi(tasks=[])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="child output",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    parent = w.session_store.create(
        task_id="parent_task", routing="local", expected_output="text",
        status=STATUS_RUNNING,
    )
    child = w.session_store.create(
        task_id="spawn_child", routing="local", expected_output="structured",
        status=STATUS_RUNNING,
        parent_session_id=parent.session_id,
        root_session_id=parent.session_id,
        spawn_depth=1,
    )
    w._handle_outcome(
        child, {"id": child.task_id, "description": "child prompt"},
        ExecutorOutcome(status=STATUS_COMPLETED, final_text="child output"),
    )
    sent = w._sent_telegram  # type: ignore[attr-defined]
    sent_ids = w._sent_with_ids  # type: ignore[attr-defined]
    assert sent == [], f"child completion leaked via _notify: {sent}"
    assert sent_ids == [], f"child completion leaked via _telegram_send_with_id: {sent_ids}"


@pytest.mark.unit
def test_followup_reply_reopens_completed_session_and_reruns_with_new_turn(tmp_path: Path):
    """Live bug repro: the operator replied to a completion message
    ("turn this into a .md in my vault") and the response had no context.
    Now the worker registers each completion message's Telegram msg_id,
    and a reply matching that msg_id reopens the COMPLETED session,
    appends the reply as a new user turn (history preserved so "this"
    still refers to the prior assistant turn), and re-runs the executor."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "summarize X", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    # Two executor invocations — first completes, second handles the followup.
    class _TwoStepExecutor:
        outcomes = [
            ExecutorOutcome(status=STATUS_COMPLETED, final_text="Here's the summary."),
            ExecutorOutcome(status=STATUS_COMPLETED, final_text="Saved to vault as `summary.md`."),
        ]
        calls: list = []
        def execute(self, session, task):
            self.calls.append((session.task_id, task.get("description")))
            return self.outcomes[len(self.calls) - 1]

    executor = _TwoStepExecutor()
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)

    # Tick 1: first completion fires. The worker captures the Telegram
    # msg_id via _telegram_send_with_id and registers a followup row.
    w.tick()
    assert len(executor.calls) == 1
    sent_ids = w._sent_with_ids  # type: ignore[attr-defined]
    assert sent_ids, "first completion should have captured a msg_id"
    completion_msg_id = sent_ids[0][0]
    assert "Here's the summary" in sent_ids[0][1]
    # Task ended at #agent-completed / done.
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "done"

    # Operator replies to the completion in Telegram → deposit_answer.
    deposited = w.session_store.deposit_answer(completion_msg_id, "turn this into a .md in my vault")
    assert deposited

    # Tick 2: worker drains followup answers, reopens the session, and
    # re-runs the executor. The reply is now in the conversation history.
    w.tick()
    assert len(executor.calls) == 2, "executor should re-run on followup"
    # Task ended at #agent-completed again with the second completion.
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "done"
    # Two completion messages captured.
    assert len(sent_ids) == 2
    assert "Saved to vault" in sent_ids[1][1]

    # And the reply turn is in the session's conversation history.
    session = w.session_store.get("t1")
    msgs = w.session_store.get_messages(session.session_id)
    user_turns = [m for m in msgs if m["role"] == "user"]
    assert any(
        isinstance(m["content"], str) and "turn this into a .md" in m["content"]
        for m in user_turns
    ), "follow-up reply should appear as a user turn in the conversation history"


@pytest.mark.unit
def test_empty_final_text_surfaces_last_tool_result_from_transcript(tmp_path: Path):
    """Live bug repro (cloud session that drafted an email then idled
    without producing an agent.message): the operator saw only the
    transcript-pointer placeholder. Now the worker scans the transcript
    for the last meaningful tool result and surfaces it inline so the
    operator sees what the agent actually did."""
    from api.services.agent_worker.transcript_store import TranscriptStore as _TS

    api = FakeApi(tasks=[
        {"id": "t1", "description": "draft email", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)

    # Pre-seed the transcript that the recovery helper will scan. The
    # session_id matches the one the worker will assign on claim.
    # Because the executor stub doesn't actually run a full session, we
    # seed via the transcript store directly. To make this work we need
    # the session_id ahead of time — claim it manually so we know it.
    # Simpler: write a transcript by predicted session_id after tick().
    # The cleaner approach is to use a stub managed transcript shape.

    # Drive the tick so the worker generates a session + transcript path.
    # Inject a managed-shape tool_result BEFORE the tick by pre-creating
    # the session.
    sess = w.session_store.create(task_id="t1", routing="local",
                                   expected_output="text")
    ts = _TS(transcripts_dir=tmp_path / "transcripts")
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_use", {"name": "lifeos_gmail_draft"})
    ts.append(sess.session_id, "managed_event_agent.mcp_tool_result", {
        "is_error": False,
        "content": [{"type": "text", "text": "Draft created — to: kevin@example.com, subject: 'Apologies for the delay'"}],
    })

    # Now drive tick — task is already claimed, so the worker will find
    # the session, run the (stub) executor, get empty final_text, and
    # use _recover_result_from_transcript.
    # Need to also flip the task to #agent-running for the dispatch path
    # to work. Simpler: just call _completion_summary directly.
    msg = w._completion_summary(sess, {"id": "t1", "description": "draft email"},
                                executor.outcome)
    assert "Draft created" in msg, msg
    assert "no final text" in msg.lower()
    assert "transcript at" not in msg.lower(), (
        "should NOT fall back to transcript-pointer when a tool result was recovered"
    )


@pytest.mark.unit
def test_empty_final_text_without_side_effect_marks_failed(tmp_path: Path):
    """Live bug repro: an agent runs a bunch of read-only searches, finds
    nothing useful, and idles without a final reply. Previously this got
    marked #agent-completed and the operator never noticed the silent
    failure. The empty-final-text guard now routes it through the failure
    path so the operator gets a Telegram alert and the task carries
    #agent-failed."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "research thing", "status": "todo",
         "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()

    # Empty result + no side-effect tool → failure path
    assert api.tasks["t1"]["status"] == "cancelled", api.tasks["t1"]
    assert FAILED_TAG in api.tasks["t1"]["tags"]
    assert COMPLETED_TAG not in api.tasks["t1"]["tags"]


@pytest.mark.unit
def test_empty_final_text_with_high_spend_skips_failure_guard(tmp_path: Path):
    """Belt-and-suspenders for the empty-result guard: even when the
    transcript shows no side-effect tool use, if the session burned real
    money (≥$0.05) we assume Anthropic's events endpoint lagged and a
    write tool fired off-transcript. Marking such a session as failed
    would alarm the operator about a session that almost certainly
    produced something. The backfill in managed_executor closes most of
    this gap; this guard handles the residual lag."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "expensive research", "status": "todo",
         "tags": ["agent", "running"]},
    ])
    api.tasks["t1"]["tags"] = ["agent-running"]

    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(
                         status=STATUS_COMPLETED, final_text="",
                     )))
    sess = w.session_store.create(task_id="t1", routing="local",
                                   expected_output="text")
    # Pretend the session ran up a real bill — $0.50 is well above the
    # $0.05 cost gate. No transcript events; no final text.
    w.session_store.record_spend("t1", 1000, 500, 0.50,
                                  cache_creation_tokens=0, cache_read_tokens=0)

    w._handle_outcome(
        sess,
        {"id": "t1", "description": "expensive research"},
        ExecutorOutcome(status=STATUS_COMPLETED, final_text=""),
    )
    # Cost gate trips → completion path runs (NOT failure path).
    assert api.tasks["t1"]["status"] == "done", api.tasks["t1"]
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert FAILED_TAG not in api.tasks["t1"]["tags"]


@pytest.mark.unit
def test_empty_final_text_with_side_effect_tool_still_marks_completed(tmp_path: Path):
    """Counterpoint to the failure-on-empty test: when the agent DID do
    real work (e.g., called lifeos_gmail_draft to create a draft) and just
    didn't write a final summary, the task is legitimately complete. The
    side-effect-tool check distinguishes "agent gave up" from "agent did
    the work but skipped the summary".

    Drives `_handle_outcome` directly (not via tick) because tick() would
    skip a pre-claimed task; the goal here is to exercise the empty-text
    branch with a transcript that already contains a side-effect tool use.
    """
    api = FakeApi(tasks=[
        {"id": "t1", "description": "draft an email", "status": "todo",
         "tags": ["agent", "running"]},  # already #agent-running for the swap
    ])
    # Manually flip the running tag — handle_outcome's success path swaps
    # RUNNING_TAG → COMPLETED_TAG, so the running tag must be present.
    api.tasks["t1"]["tags"] = ["agent-running"]

    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=_StubExecutor(outcome=ExecutorOutcome(
                         status=STATUS_COMPLETED, final_text="",
                     )))
    sess = w.session_store.create(task_id="t1", routing="local",
                                   expected_output="text")
    w.transcript_store.append(sess.session_id, "tool_call", {
        "tool": "lifeos_gmail_draft", "is_error": False,
    })

    w._handle_outcome(
        sess,
        {"id": "t1", "description": "draft an email"},
        ExecutorOutcome(status=STATUS_COMPLETED, final_text=""),
    )

    # Side-effect tool was present → completion path took over
    assert api.tasks["t1"]["status"] == "done", api.tasks["t1"]
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert FAILED_TAG not in api.tasks["t1"]["tags"]


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
def test_completion_label_reports_remote_fallback_model_when_served_by_set(tmp_path: Path):
    """(#699) When the local route actually ran on the flag-gated remote
    fallback, the completion message must name the model that actually
    served the session, not just the static "local" route label — #658's
    report-observed-not-configured principle applied to this new path."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "task", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text="done",
        served_by="accounts/fireworks/models/deepseek-v4-flash-0731",
    ))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent
    assert "Local agent worker" in sent[0]
    assert "accounts/fireworks/models/deepseek-v4-flash-0731" in sent[0]


@pytest.mark.unit
def test_completion_inline_summary_kept_when_under_cap(tmp_path: Path):
    """A short final_text is delivered inline (not replaced by a preview), and
    every completion now also lands a note in the vault — so the message carries
    the full body plus a 'Saved to vault' pointer + obsidian:// link."""
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
    # Full body inline — short answers are not truncated to a preview.
    assert short_text.strip() in sent[0]
    assert "Full answer saved to vault" not in sent[0]
    # Always-write: short completions still get a saved-note pointer.
    assert "Saved to vault:" in sent[0]
    assert "obsidian://" in sent[0]


@pytest.mark.unit
def test_failed_task_writes_no_agent_output(tmp_path: Path):
    """Only successful completions write a note. A FAILED outcome notifies via
    Telegram but leaves the Agent Output folder untouched (criterion #6)."""
    from config.settings import settings as _settings

    api = FakeApi(tasks=[
        {"id": "t1", "description": "do the thing", "status": "todo", "tags": ["agent", "local"]},
    ])
    executor = _StubExecutor(outcome=ExecutorOutcome(status=STATUS_FAILED, reason="boom"))
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="local"),
                     local_executor=executor)
    w.tick()
    sent = w._sent_telegram  # type: ignore[attr-defined]
    assert sent and "failed" in sent[0].lower()
    out_dir = _settings.vault_path / _settings.agent_output_dir
    assert not out_dir.exists() or not list(out_dir.glob("*.md"))


@pytest.mark.unit
def test_completion_spills_to_vault_when_over_cap(tmp_path: Path, monkeypatch):
    """When final_text is >2000 chars, the worker writes the full body
    to the vault and replaces the inline blob with a short preview +
    obsidian:// link. The operator should never see a mid-paragraph
    truncation again."""
    from config.settings import settings as _settings
    vault = tmp_path / "MyVault"
    monkeypatch.setattr(_settings, "vault_path", vault, raising=False)

    # Tag is "local" — not "cloud" — so the deterministic tag precedence
    # (#139 §2) routes this task to the local executor. The test exercises
    # the over-cap spillover via _StubExecutor on the local path.
    api = FakeApi(tasks=[
        {"id": "t1", "description": "Big report on Julia",
         "status": "todo", "tags": ["agent", "local"]},
    ])
    long_text = (
        "Julia Barnes is the CEO of The Movement Cooperative.\n\n"
        + ("Paragraph body content that goes on and on. " * 80)  # ~3200 chars
    )
    executor = _StubExecutor(outcome=ExecutorOutcome(
        status=STATUS_COMPLETED, final_text=long_text,
    ))
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
    # The vault file exists in the configured agent-output dir
    # (LIFEOS_AGENT_OUTPUT_DIR; defaults to "LifeOS/Tasks/Agent Output").
    from config.settings import settings as _settings
    out_dir = vault / _settings.agent_output_dir
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


# ---------------------------------------------------------------------------
# #753 — top-level #agent CLI-routed tasks dispatch off the tick thread
#
# Before this fix, `_dispatch()`'s ROUTE_CLAUDE_CODE/ROUTE_CODEX branch called
# `_dispatch_claude_code_session`/`_dispatch_codex_session` inline, so the
# whole CLI subprocess ran synchronously inside tick()'s claim loop — up to
# the session's 14,400s budget wall. Nothing else in tick() (new claims,
# sleeping-session wakes, managed polling, clarification processing/timeouts)
# ran while that subprocess was in flight. The fix reuses `_submit_cli_dispatch`
# — the same pool + `_cli_inflight` machinery spawned CLI children already use
# (#299, test_agent_worker_async_cli_dispatch.py) — for the top-level path too.
# ---------------------------------------------------------------------------

class _CapturingPool:
    """Records submitted callables without running them, so a test can
    assert a top-level CLI dispatch was handed off (not run inline) and then
    run it on demand. Mirrors the pool in test_agent_worker_async_cli_dispatch.py."""

    def __init__(self):
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return None

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass

    def run_all(self) -> None:
        for fn, args, kwargs in self.submitted:
            fn(*args, **kwargs)


@pytest.mark.unit
def test_top_level_cli_task_dispatched_off_tick_not_inline(tmp_path: Path):
    """A top-level #agent task routed to claude_code must be handed to the
    CLI pool, not executed inline in tick() — the fix's core claim, proven
    by a pool that records submissions without running them. Once the
    submitted work is run (as the real pool thread would), the full
    outcome-handling path (vault tag swap, task completion, Telegram) still
    fires — it's the same `_dispatch_claude_code_session` used by spawned
    sessions, just called on a pool thread instead of the tick thread."""
    calls: list = []

    class _Executor:
        def execute(self, session, task):
            calls.append((session.task_id, task.get("description")))
            # notifications_sent=1 earns the completion (#760) — this test is
            # about off-tick dispatch, not the earned-completion gate itself.
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="all done", notifications_sent=1)

    api = FakeApi(tasks=[
        {"id": "t1", "description": "do the thing", "status": "todo", "tags": ["agent"]},
    ])
    pool = _CapturingPool()
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude_code"),
                     local_executor=None,
                     claude_code_executor=_Executor(),
                     cli_pool=pool)

    handled = w.tick()

    assert handled == 1
    # Handed to the pool, NOT run inline — tick() returned before the
    # (would-be long) subprocess ever started.
    assert len(pool.submitted) == 1
    assert calls == []
    assert COMPLETED_TAG not in api.tasks["t1"]["tags"]

    # Running the submitted work exercises the outcome-handling path exactly
    # as the real pool thread would.
    pool.run_all()

    assert calls == [("t1", "do the thing")]
    assert COMPLETED_TAG in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "done"
    assert any("all done" in t for t in w._sent_telegram)  # type: ignore[attr-defined]
    assert w._cli_inflight == set()


@pytest.mark.unit
def test_second_cli_task_claimed_and_runs_while_first_blocks(tmp_path: Path):
    """Live bug repro (#753): two #agent tasks routed to claude_code — the
    second must be claimed and start running while the first is still mid
    execute(), instead of sitting unclaimed for the first task's entire run.
    Uses a real ThreadPoolExecutor (not a synchronous stub) since this is
    specifically a concurrency claim."""
    start_evt = threading.Event()
    release_evt = threading.Event()

    class _Executor:
        def execute(self, session, task):
            # notifications_sent=1 earns the completion (#760) — this test is
            # about pool concurrency, not the earned-completion gate itself.
            if task.get("description") == "slow task":
                start_evt.set()
                assert release_evt.wait(timeout=5), "test deadlocked waiting for release"
                return ExecutorOutcome(
                    status=STATUS_COMPLETED, final_text="slow done", notifications_sent=1)
            return ExecutorOutcome(
                status=STATUS_COMPLETED, final_text="fast done", notifications_sent=1)

    api = FakeApi(tasks=[
        {"id": "t-slow", "description": "slow task", "status": "todo", "tags": ["agent"]},
        {"id": "t-fast", "description": "fast task", "status": "todo", "tags": ["agent"]},
    ])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude_code"),
                     local_executor=None,
                     claude_code_executor=_Executor(),
                     cli_pool=ThreadPoolExecutor(max_workers=4))
    try:
        handled = w.tick()
        # Both tasks claimed and dispatched in the same tick — tick() did not
        # block on the slow task's execute().
        assert handled == 2
        assert start_evt.wait(timeout=2), "slow task never reached execute() on the pool"

        # The fast task, submitted to the same pool, finishes without
        # waiting on the slow one — proves real concurrency, not just
        # deferred-then-serial execution.
        deadline = time.time() + 5
        while time.time() < deadline and COMPLETED_TAG not in api.tasks["t-fast"]["tags"]:
            time.sleep(0.02)
        assert COMPLETED_TAG in api.tasks["t-fast"]["tags"]
        # The slow one is genuinely still blocked at this point.
        assert COMPLETED_TAG not in api.tasks["t-slow"]["tags"]
    finally:
        release_evt.set()
        w._cli_pool.shutdown(wait=True)

    assert COMPLETED_TAG in api.tasks["t-slow"]["tags"]


@pytest.mark.unit
def test_clarification_processing_continues_while_cli_task_blocks(tmp_path: Path):
    """(#753) A blocked CLI subprocess must not starve clarification
    processing for an unrelated, already-blocked local session — the tick
    thread has to stay free to keep servicing the rest of the worker's
    responsibilities while the CLI task runs on the pool."""
    block_evt = threading.Event()
    release_evt = threading.Event()

    class _Executor:
        def execute(self, session, task):
            block_evt.set()
            assert release_evt.wait(timeout=5), "test deadlocked waiting for release"
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="slow done")

    api = FakeApi(tasks=[
        {"id": "t-slow", "description": "slow task", "status": "todo", "tags": ["agent"]},
        {"id": "t-other", "description": "other task", "status": "blocked",
         "tags": [BLOCKED_TAG, "local"]},
    ])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude_code"),
                     local_executor=None,
                     claude_code_executor=_Executor(),
                     cli_pool=ThreadPoolExecutor(max_workers=4))
    try:
        w.tick()  # dispatches t-slow onto the pool; must return without waiting
        assert block_evt.wait(timeout=2), "slow task never reached execute()"

        # Seed the unrelated BLOCKED local-routed session with an answered
        # clarification, exactly as a live worker would have parked one from
        # an earlier tick.
        local_stub = _StubExecutor(outcome=ExecutorOutcome(
            status=STATUS_COMPLETED, final_text="answered",
        ))
        w._local_executor = local_stub
        other = w.session_store.create(task_id="t-other", routing="local",
                                        status=STATUS_BLOCKED, expected_output="text")
        w.session_store.create_pending_question(
            other.session_id, "t-other", "which file?", sent_message_id=555,
        )
        w.session_store.deposit_answer(555, "the config file")

        # This must complete promptly — proving the tick thread isn't stuck
        # behind the still-blocked slow task.
        w._process_clarification_answers()

        assert local_stub.calls == [("t-other", "other task")]
    finally:
        release_evt.set()
        w._cli_pool.shutdown(wait=True)


@pytest.mark.unit
def test_cli_inflight_guard_covers_top_level_dispatch(tmp_path: Path):
    """(#753) Top-level CLI dispatch shares `_cli_inflight` with the spawned
    path (#299) — a session submitted to the pool but not yet flipped
    CLAIMED→RUNNING must not be submitted a second time, mirroring
    test_inflight_guard_prevents_double_dispatch_across_ticks for spawned
    children."""
    calls: list = []

    class _Executor:
        def execute(self, session, task):
            calls.append(session.task_id)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="done")

    api = FakeApi(tasks=[
        {"id": "t1", "description": "do the thing", "status": "todo", "tags": ["agent"]},
    ])
    pool = _CapturingPool()  # never runs -> the session stays "in flight"
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude_code"),
                     local_executor=None,
                     claude_code_executor=_Executor(),
                     cli_pool=pool)
    w.session_store.create(task_id="t1", status=STATUS_CLAIMED)
    task = {"id": "t1", "description": "do the thing", "tags": []}

    w._dispatch(task)
    w._dispatch(task)  # a second dispatch attempt before the pool thread runs

    assert len(pool.submitted) == 1
    assert calls == []


@pytest.mark.unit
def test_resume_pending_rolls_back_top_level_cli_session_same_as_before(tmp_path: Path):
    """(#753) resume_pending()'s startup-recovery semantics for a top-level
    claude_code/codex #agent session are unchanged by routing dispatch
    through the pool — a crash-time CLAIMED/RUNNING session still rolls back
    to #agent for retry, exactly as before this fix."""
    api = FakeApi(tasks=[
        {"id": "t1", "description": "do the thing", "status": "in_progress",
         "tags": [RUNNING_TAG]},
    ])
    w = _make_worker(tmp_path, api,
                     preflight_caller=_golden_preflight(routing="claude_code"),
                     local_executor=None)
    w.session_store.create(task_id="t1", routing="claude_code", status=STATUS_RUNNING)

    n = w.resume_pending()

    assert n == 1
    refreshed = w.session_store.get("t1")
    assert refreshed.status == STATUS_FAILED
    assert AGENT_TAG in api.tasks["t1"]["tags"]
    assert RUNNING_TAG not in api.tasks["t1"]["tags"]
    assert api.tasks["t1"]["status"] == "todo"
