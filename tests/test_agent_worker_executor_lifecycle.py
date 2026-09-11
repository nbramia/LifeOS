"""Synthetic executor lifecycle contract tests."""
from __future__ import annotations

from types import SimpleNamespace
import time

import httpx
import pytest

from api.services.agent_worker.executor_lifecycle import (
    CancelResult,
    ExecutorCapabilities,
    ExecutorRegistry,
    adapter_for,
    route_supports_resume_after_children,
)
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, yield_until
from api.services.conversation_store import ConversationStore
from api.services.agent_worker.local_executor import LocalExecutor
from api.services.agent_worker.worker import Worker


pytestmark = pytest.mark.unit


class _FakeEngine:
    def __init__(self, route: str):
        self.route = route
        self.calls: list[tuple[str, str]] = []

    def execute(self, session, request):
        self.calls.append(("start", str(request)))
        return ExecutorOutcome(
            status=STATUS_COMPLETED,
            final_text="synthetic result",
            termination_evidence={"terminal_success": True},
        )

    def resume(self, session, message):
        self.calls.append(("resume", message))
        return ExecutorOutcome(
            status=STATUS_COMPLETED,
            final_text="synthetic resumed",
            termination_evidence={"terminal_success": True},
        )


def _session(route: str):
    return SimpleNamespace(
        task_id=f"task-{route}",
        session_id=f"session-{route}",
        routing=route,
        claude_code_session_id=f"cli-{route}",
        managed_agent_session_id=f"managed-{route}",
        conversation_id=f"conversation-{route}",
    )


@pytest.mark.parametrize(
    "route", ["local", "remote", "claude", "hermes", "claude_code", "codex"]
)
def test_all_supported_routes_resume_natively(route):
    engine = _FakeEngine(route)
    adapter = adapter_for(route, engine)
    outcome = adapter.resume_after_children(_session(route), "children are complete", [])

    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "synthetic resumed"
    assert outcome.session_id == f"session-{route}"
    assert outcome.attempt_id == f"legacy:session-{route}"
    assert outcome.executor == route
    assert outcome.continuation_id in {
        f"cli-{route}", f"managed-{route}", f"conversation-{route}", None,
    }
    assert engine.calls == [("resume", "children are complete")]


def test_managed_resume_can_use_documented_recreated_remote_session():
    engine = _FakeEngine("claude")
    recreated: list[str] = []

    def resume_after_children(session, message, children):
        recreated.append(message)
        return ExecutorOutcome(
            status=STATUS_COMPLETED,
            final_text="managed aggregate",
            termination_evidence={"terminal_success": True},
        )

    adapter = adapter_for("claude", engine, child_resume=resume_after_children)
    outcome = adapter.resume_after_children(_session("claude"), "aggregate", [])
    assert outcome.status == STATUS_COMPLETED
    assert recreated == ["aggregate"]
    assert engine.calls == []


def test_registry_guard_is_idempotent_and_no_local_fallback():
    registry = ExecutorRegistry()
    engine = _FakeEngine("codex")
    registry.register("codex", adapter_for("codex", engine))
    session = _session("codex")
    assert registry.begin(session, "resume_after_children") is True
    assert registry.begin(session, "resume_after_children") is False
    registry.finish(session, "resume_after_children")
    assert registry.begin(session, "resume_after_children") is True

    # Unknown routes have no adapter and never acquire a LocalExecutor path.
    assert registry.get("old_backend") is None
    assert registry.capabilities("old_backend") == ExecutorCapabilities()
    assert route_supports_resume_after_children("old_backend") is False


def test_attempt_and_turn_ids_are_distinct_and_persist_across_reopen(tmp_path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    first = store.create("identity", routing="local")
    first_turn = store.begin_executor_turn("identity", "execute")
    second_turn = store.begin_executor_turn("identity", "resume")

    assert first.attempt_id
    assert first_turn.attempt_id == first.attempt_id
    assert first_turn.turn_id
    assert second_turn.turn_id != first_turn.turn_id
    assert second_turn.attempt_id == first_turn.attempt_id

    store.update_status("identity", STATUS_COMPLETED)
    store.begin_new_execution("identity", request={"executor": "local"})
    reopened = store.get("identity")
    assert reopened.attempt_id
    assert reopened.attempt_id != first.attempt_id
    assert reopened.attempt_number == first.attempt_number + 1
    assert reopened.turn_id is None
    reopened_turn = store.begin_executor_turn("identity", "retry")
    assert reopened_turn.attempt_id == reopened.attempt_id
    assert reopened_turn.turn_id not in {first_turn.turn_id, second_turn.turn_id}

    with store._connect() as conn:
        attempts = conn.execute(
            "SELECT attempt_id FROM execution_attempts WHERE session_id = ? ORDER BY attempt_number",
            (first.session_id,),
        ).fetchall()
        turns = conn.execute(
            "SELECT turn_id FROM execution_turns WHERE session_id = ? ORDER BY started_at, turn_number",
            (first.session_id,),
        ).fetchall()
    assert [row[0] for row in attempts] == [first.attempt_id, reopened.attempt_id]
    assert len({row[0] for row in turns}) == 3


def test_late_attempt_writes_cannot_clobber_newer_turn(tmp_path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create("late-write", routing="hermes")
    old_turn = store.begin_executor_turn("late-write", "execute")
    newer_turn = store.begin_executor_turn("late-write", "resume")

    assert store.set_conversation_id(
        "late-write", "old-conversation",
        attempt_id=old_turn.attempt_id, turn_id=old_turn.turn_id,
    ) is False
    assert store.set_hermes_model(
        "late-write", "old-model",
        attempt_id=old_turn.attempt_id, turn_id=old_turn.turn_id,
    ) is False
    current = store.get("late-write")
    assert current.attempt_id == newer_turn.attempt_id
    assert current.turn_id == newer_turn.turn_id
    assert current.conversation_id is None
    assert current.hermes_model is None


def test_adapter_returns_persisted_turn_snapshot_after_executor_begins(tmp_path):
    store = SessionStore(db_path=tmp_path / "sessions.db")

    class Engine(_FakeEngine):
        def execute(self, session, request):
            store.begin_executor_turn(session.task_id, "execute", session=session)
            return ExecutorOutcome(status=STATUS_COMPLETED, final_text="ok")

    session = store.create("adapter-snapshot", routing="local")
    adapter = adapter_for("local", Engine("local"), session_store=store)
    outcome = adapter.start(session, {"description": "synthetic"})
    persisted = store.get("adapter-snapshot")
    assert outcome.attempt_id == persisted.attempt_id
    assert outcome.turn_id == persisted.turn_id
    assert outcome.turn_id != session.turn_id


def test_cancellation_is_idempotent_and_targets_one_attempt():
    registry = ExecutorRegistry()
    engine = _FakeEngine("codex")
    calls: list[tuple[str, str]] = []

    def cancel(session, reason):
        calls.append((session.session_id, reason))
        return CancelResult(
            cancelled=True,
            reason=reason,
            session_id=session.session_id,
            attempt_id=session.session_id,
            continuation_id=session.claude_code_session_id,
        )

    registry.register("codex", adapter_for("codex", engine, cancel_fn=cancel))
    session = _session("codex")
    first = registry.cancel_once(session, "operator requested")
    second = registry.cancel_once(session, "duplicate event")
    assert first.cancelled is True
    assert second.idempotent is True
    assert calls == [(session.session_id, "operator requested")]


def test_cancellation_uses_persisted_attempt_identity(tmp_path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    session = store.create("cancel-identity", routing="codex", status="running")
    registry = ExecutorRegistry()

    def cancel(_session, _reason):
        return CancelResult(cancelled=True)

    registry.register(
        "codex", adapter_for("codex", _FakeEngine("codex"), cancel_fn=cancel),
    )
    first = registry.cancel_once(session, "operator requested")
    second = registry.cancel_once(session, "duplicate event")
    assert first.cancelled is True
    assert first.attempt_id == session.attempt_id
    assert first.attempt_id != session.session_id
    assert second.idempotent is True


def test_cancellation_guard_keeps_exact_turn_failed_and_does_not_fence_reopen(tmp_path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    session = store.create("sticky-cancel", routing="codex", status="running")
    turn = store.begin_executor_turn("sticky-cancel", "execute", session=session)

    assert store.mark_cancelled(
        session.task_id,
        attempt_id=turn.attempt_id,
        turn_id=turn.turn_id,
        reason="operator requested",
    )
    assert store.is_cancelled(session.task_id, turn.attempt_id, turn.turn_id)
    assert not store.update_status(
        session.task_id, STATUS_COMPLETED,
        attempt_id=turn.attempt_id, turn_id=turn.turn_id,
    )
    assert store.get(session.task_id).status == STATUS_FAILED

    reopened = store.begin_new_execution(session.task_id)
    assert not store.is_cancelled(
        reopened.task_id, reopened.attempt_id, reopened.turn_id,
    )


def test_hermes_start_race_cannot_resurrect_cancelled_turn(tmp_path, monkeypatch):
    """A cancellation between turn allocation and upstream start wins."""
    from api.services.agent_worker.hermes_executor import HermesExecutor
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://synthetic-hermes")
    store = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    stream_calls: list[bool] = []

    session = store.create("hermes-start-race", routing="hermes")
    allocated = store.begin_executor_turn(
        session.task_id, "execute", session=session,
    )
    executor = HermesExecutor(
        session_store=store,
        transcript_store=transcripts,
        http_client_factory=lambda: stream_calls.append(True),
    )
    registry = ExecutorRegistry(session_store=store)
    registry.register(
        "hermes", adapter_for("hermes", executor, session_store=store),
    )

    cancelled = registry.cancel_once(allocated, "operator requested")
    outcome = executor.execute(allocated, {"description": "must not stream"})

    assert cancelled.cancelled is True
    assert outcome.status == STATUS_FAILED
    assert outcome.termination_evidence["cancelled"] is True
    assert stream_calls == []
    persisted = store.get(session.task_id)
    assert persisted.status == STATUS_FAILED
    assert persisted.attempt_id == allocated.attempt_id
    assert persisted.turn_id == allocated.turn_id
    assert all(event["kind"] != "hermes_completed" for event in transcripts.read(session.session_id))


def test_hermes_cancel_reports_conversation_identity(tmp_path):
    from api.services.agent_worker.hermes_executor import HermesExecutor

    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    executor = HermesExecutor(
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=transcripts,
        http_client_factory=lambda: None,
    )
    session = _session("hermes")
    result = adapter_for("hermes", executor).cancel(session, "operator requested")
    assert result.cancelled is True
    assert result.continuation_id == session.conversation_id
    assert transcripts.read(session.session_id)[-1]["kind"] == "cancel_requested"


def test_unsupported_resume_is_rejected_before_yield_mutation(tmp_path):
    sessions = SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    caller = sessions.create("caller", routing="legacy_backend")
    child = sessions.create(
        "child", routing="legacy_backend", parent_session_id=caller.session_id,
        root_session_id=caller.root_session_id,
    )
    result = yield_until(
        InterAgentContext(
            session_store=sessions,
            transcript_store=transcripts,
            caller_session_id=caller.session_id,
            caps=Caps(),
        ),
        {"children": [child.session_id]},
    )
    assert result["ok"] is False
    assert result["error"] == "unsupported_resume"
    assert result["message"] == "executor cannot resume after children"
    refreshed = sessions.get("caller")
    assert refreshed.status == STATUS_CLAIMED
    assert refreshed.yield_waiting_for is None


def test_hermes_blocked_fake_stream_honors_absolute_deadline(tmp_path, monkeypatch):
    """A blocked reader is bounded even when the read-idle timeout is longer."""
    from api.routes import hermes_proxy as hp
    from api.services.agent_worker.hermes_executor import HermesExecutor
    from api.services.conversation_store import ConversationStore
    from api.services.usage_store import UsageStore
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://synthetic-hermes")
    monkeypatch.setattr(settings, "hermes_backend_token", "synthetic-token")
    monkeypatch.setattr(settings, "claude_timeout_seconds", 3600)
    monkeypatch.setattr(hp, "get_store", lambda: ConversationStore(db_path=str(tmp_path / "conv.db")))
    monkeypatch.setattr(hp, "get_usage_store", lambda: UsageStore(db_path=str(tmp_path / "usage.db")))
    monkeypatch.setattr(hp, "schedule_retitle", lambda _conversation_id: None)

    class Response:
        def raise_for_status(self):
            return None

        def iter_bytes(self):
            while not client.closed:
                time.sleep(0.005)
                yield b""

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    class Client:
        closed = False

        def stream(self, *_args, **_kwargs):
            return Stream()

        def close(self):
            self.closed = True

    client = Client()
    sessions = SessionStore(db_path=tmp_path / "sessions.db")
    session = sessions.create(
        "hermes-deadline", routing="hermes", budget={"wall_seconds": 0.03},
    )
    executor = HermesExecutor(
        session_store=sessions,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        http_client_factory=lambda: client,
    )
    started = time.monotonic()
    outcome = executor.execute(session, {"description": "synthetic blocked turn"})
    elapsed = time.monotonic() - started
    # The bound separates the two deadlines this test tells apart: the
    # session's 0.03s wall budget and the 3600s read-idle timeout the
    # blocked reader would otherwise sit on. Any figure between them
    # proves the executor stopped on the budget, so this one is set far
    # enough above 0.03 to absorb scheduler delay on a loaded parallel
    # run -- a bound a few hundred milliseconds wide measures the host,
    # not the executor.
    assert elapsed < 10, f"executor took {elapsed:.2f}s; budget was 0.03s"
    assert outcome.status == STATUS_FAILED
    assert "absolute turn deadline" in outcome.reason
    assert outcome.termination_evidence["absolute_deadline"] is True


@pytest.mark.parametrize("route", ["local", "remote"])
def test_worker_ignores_late_clean_completion_after_exact_turn_cancellation(
    tmp_path, route,
):
    """A clean in-process result cannot complete or notify a cancelled turn."""
    sessions = SessionStore(db_path=tmp_path / f"{route}.db")
    transcripts = TranscriptStore(transcripts_dir=tmp_path / f"{route}-transcripts")
    session = sessions.create(
        f"late-{route}", routing=route, status="running",
    )
    turn = sessions.begin_executor_turn(
        session.task_id, "execute", session=session,
    )
    sessions.mark_executor_turn_running(
        session.task_id, turn.attempt_id, turn.turn_id,
    )
    sessions.mark_cancelled(
        session.task_id, attempt_id=turn.attempt_id, turn_id=turn.turn_id,
        reason="operator requested",
    )
    worker = Worker(
        session_store=sessions,
        transcript_store=transcripts,
        conversation_store=ConversationStore(db_path=tmp_path / f"{route}-conversations.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(404))),
    )
    effects: list[str] = []
    worker._complete_task = lambda *_args: effects.append("complete")
    worker._notify_terminal = lambda *_args, **_kwargs: effects.append("notify")
    outcome = ExecutorOutcome(
        status=STATUS_COMPLETED,
        final_text="late clean result",
        session_id=turn.session_id,
        attempt_id=turn.attempt_id,
        turn_id=turn.turn_id,
        executor=route,
        termination_evidence={"terminal_success": True},
    )

    worker._handle_outcome(session, {"description": "cancelled task"}, outcome)

    assert effects == []
    assert sessions.get(session.task_id).status == STATUS_FAILED
    assert transcripts.read(session.session_id)[-1]["kind"] == "late_terminal_ignored"
    worker._http.close()


def test_message_writes_are_fenced_across_cancel_and_reopen(tmp_path):
    sessions = SessionStore(db_path=tmp_path / "sessions.db")
    session = sessions.create("message-fence", routing="local", status="running")
    old_turn = sessions.begin_executor_turn(
        session.task_id, "execute", session=session,
    )
    assert sessions.append_message(
        session.session_id, "user", "old message",
        attempt_id=old_turn.attempt_id, turn_id=old_turn.turn_id,
    ) is not None
    sessions.mark_cancelled(
        session.task_id, attempt_id=old_turn.attempt_id, turn_id=old_turn.turn_id,
        reason="operator requested",
    )
    reopened = sessions.begin_new_execution(session.task_id)
    new_turn = sessions.begin_executor_turn(
        reopened.task_id, "execute", session=reopened,
    )

    assert sessions.append_message(
        session.session_id, "assistant", "late old message",
        attempt_id=old_turn.attempt_id, turn_id=old_turn.turn_id,
    ) is None
    assert sessions.append_message(
        session.session_id, "assistant", "new message",
        attempt_id=new_turn.attempt_id, turn_id=new_turn.turn_id,
    ) is not None
    messages = sessions.get_messages(session.session_id)
    assert [message["content"] for message in messages] == [
        "old message", "new message",
    ]


@pytest.mark.parametrize("route,is_remote", [("local", False), ("remote", True)])
def test_local_executor_persists_normal_message_turn_with_fence(
    tmp_path, route, is_remote,
):
    class Response:
        text = "normal result"
        tool_calls = []
        usage = SimpleNamespace(input_tokens=2, output_tokens=3)

    class Llm:
        def create(self, **_kwargs):
            return Response()

    sessions = SessionStore(db_path=tmp_path / f"{route}.db")
    session = sessions.create(
        f"normal-{route}", routing=route, status="claimed",
    )
    executor = LocalExecutor(
        session_store=sessions,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / f"{route}-transcripts"),
        llm_client=Llm(),
        is_remote=is_remote,
    )

    outcome = executor.execute(session, {"description": "normal persistence"})

    assert outcome.status == STATUS_COMPLETED
    persisted = sessions.get(session.task_id)
    assert persisted.status == STATUS_COMPLETED
    assert [message["role"] for message in sessions.get_messages(session.session_id)] == [
        "system", "user", "assistant",
    ]
