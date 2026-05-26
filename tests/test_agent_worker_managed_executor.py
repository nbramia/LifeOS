"""ManagedExecutor lifecycle tests — create → poll → terminal.

Driver is mocked at the class level (not via HTTP) so we exercise the
executor's orchestration logic — transcript mirroring, budget accounting,
state machine transitions — without depending on the API surface.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.managed_driver import ManagedSessionState
from api.services.agent_worker.managed_executor import ManagedExecutor
from api.services.agent_worker.session_store import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


AGENT_ID = "agent_test"
ENV_ID = "env_test"
VAULT_IDS = ["vlt_test"]


class _FakeDriver:
    """Records calls and returns scripted state objects."""

    def __init__(self, *, create_returns="sess_remote", state_responses=None, create_raises=None):
        self.create_returns = create_returns
        self.state_responses = list(state_responses or [])
        self.create_raises = create_raises
        self.kills: list[tuple[str, str]] = []
        self.created_with: dict | None = None
        self.poll_calls: list[tuple[str, str | None]] = []

    def create_session(self, **kwargs):
        self.created_with = kwargs
        if self.create_raises:
            raise self.create_raises
        return self.create_returns

    def get_session_state(self, session_id, since_event_id=None):
        self.poll_calls.append((session_id, since_event_id))
        if not self.state_responses:
            raise AssertionError("driver poll called with no scripted responses")
        return self.state_responses.pop(0)

    def kill_session(self, session_id, reason=""):
        self.kills.append((session_id, reason))


def _make_executor(store, transcript, driver, *, model="claude-sonnet-4-6"):
    return ManagedExecutor(
        session_store=store,
        transcript_store=transcript,
        driver=driver,
        agent_id=AGENT_ID,
        environment_id=ENV_ID,
        vault_ids=VAULT_IDS,
        model=model,
    )


@pytest.fixture
def stores(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "sessions.db")
    store.create(
        task_id="t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
        expected_output="text",
    )
    session = store.get("t1")
    transcript = TranscriptStore(transcripts_dir=tmp_path / "transcripts")
    return store, session, transcript


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_start_creates_remote_session_with_agent_and_environment_ids(stores):
    store, session, transcript = stores
    driver = _FakeDriver(create_returns="sess_remote_42")
    executor = _make_executor(store, transcript, driver)

    outcome = executor.start(session, {"id": "t1", "description": "say hi"})
    assert outcome.status == STATUS_RUNNING
    refreshed = store.get("t1")
    assert refreshed.managed_agent_session_id == "sess_remote_42"

    # Driver received agent_id + environment_id + vault_ids (no inline prompt/model/mcps)
    cw = driver.created_with
    assert cw["agent_id"] == AGENT_ID
    assert cw["environment_id"] == ENV_ID
    assert cw["vault_ids"] == VAULT_IDS
    assert cw["metadata"]["lifeos_session_id"] == session.session_id
    assert cw["metadata"]["task_id"] == "t1"
    # Initial message carries task description + soft budget. Budget is
    # framed as "soft" because Anthropic doesn't enforce it server-side;
    # the worker kills the session externally on breach.
    assert "say hi" in cw["initial_message"]
    assert "expected_output=text" in cw["initial_message"]
    assert "soft budget" in cw["initial_message"]
    assert "~$5.0" in cw["initial_message"]
    # Ambiguity policy NOT duplicated here — it lives in the agent preset's
    # system prompt (Anthropic console). User message stays task-specific.
    assert "ambiguous" not in cw["initial_message"]
    # session_id NOT in the user message — already in session metadata.
    assert session.session_id not in cw["initial_message"]
    assert "lifeos_session_id" not in cw["initial_message"]
    # NO inline system_prompt / model / mcp_servers / connectors
    assert "system_prompt" not in cw
    assert "model" not in cw
    assert "mcp_servers" not in cw
    assert "connectors" not in cw


@pytest.mark.unit
def test_start_uses_first_100_chars_of_description_as_title(stores):
    store, session, transcript = stores
    driver = _FakeDriver(create_returns="sess_remote")
    executor = _make_executor(store, transcript, driver)

    long_title = "x" * 200
    executor.start(session, {"id": "t1", "description": long_title})
    assert driver.created_with["title"] == "x" * 100


@pytest.mark.unit
def test_start_omits_title_when_description_empty(stores):
    store, session, transcript = stores
    driver = _FakeDriver(create_returns="sess_remote")
    executor = _make_executor(store, transcript, driver)
    executor.start(session, {"id": "t1", "description": ""})
    assert driver.created_with["title"] is None


@pytest.mark.unit
def test_start_handles_create_failure(stores):
    """create_session failure path masks the exception args (which may contain
    request details / API key) and surfaces only the exception type name."""
    store, session, transcript = stores
    driver = _FakeDriver(create_raises=RuntimeError("403"))
    executor = _make_executor(store, transcript, driver)
    outcome = executor.start(session, {"id": "t1", "description": "x"})
    assert outcome.status == STATUS_FAILED
    # Exception args (which may contain headers/API key) are NOT surfaced —
    # only the type name.
    assert "RuntimeError" in outcome.reason
    assert "403" not in outcome.reason
    assert store.get("t1").status == STATUS_FAILED


# ---------------------------------------------------------------------------
# poll() — running state
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_poll_returns_running_when_remote_still_running(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")

    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running",
            last_event_id="evt_1",
            new_events=[{"id": "evt_1", "type": "agent.message", "payload": {"text": "thinking"}}],
            total_input_tokens=10, total_output_tokens=5,
        ),
    ])
    executor = _make_executor(store, transcript, driver)

    outcome = executor.poll(session)
    assert outcome.status == STATUS_RUNNING

    # Tokens / cursor recorded
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 10
    assert refreshed.total_output_tokens == 5
    assert store.get_managed_last_event_id("t1") == "evt_1"

    events = [e["kind"] for e in transcript.read(session.session_id)]
    assert "managed_event_agent.message" in events


@pytest.mark.unit
def test_poll_uses_since_cursor_on_subsequent_calls(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_1",
            new_events=[{"id": "evt_1", "type": "x"}],
            total_input_tokens=0, total_output_tokens=0,
        ),
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_2",
            new_events=[{"id": "evt_2", "type": "y"}],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    executor.poll(session)
    # Reload session to pick up the cursor.
    session = store.get("t1")
    executor.poll(session)
    # First call: since=None; second call: since=evt_1.
    assert driver.poll_calls == [("sess_remote", None), ("sess_remote", "evt_1")]


# ---------------------------------------------------------------------------
# poll() — terminal states
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_poll_finalizes_completed(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="completed",
            last_event_id="evt_done",
            new_events=[{"id": "evt_done", "type": "session.status_idle", "payload": {}}],
            total_input_tokens=200, total_output_tokens=100,
            final_text="The answer is 42.",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "The answer is 42."
    assert store.get("t1").status == STATUS_COMPLETED


@pytest.mark.unit
def test_poll_propagates_init_failed_mcps_to_outcome(stores):
    """Managed executor must surface init_failed_mcps from the driver state
    through to the ExecutorOutcome so the worker's completion summary can
    include the footer."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="idle",
            last_event_id="evt_done",
            new_events=[{"id": "evt_done", "type": "session.status_idle"}],
            total_input_tokens=10, total_output_tokens=20,
            final_text="ok",
            init_failed_mcps=["gmail", "gcal"],
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_COMPLETED
    assert outcome.init_failed_mcps == ["gmail", "gcal"]


@pytest.mark.unit
def test_poll_finalizes_idle_status_as_completed(stores):
    """The Managed Agents API reports successful terminal as `"idle"` (not
    `"completed"`). Executor must treat both as STATUS_COMPLETED."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="idle",  # ← live API uses this verbatim
            last_event_id="evt_done",
            new_events=[{"id": "evt_done", "type": "session.status_idle"}],
            total_input_tokens=3, total_output_tokens=42,
            final_text="42 tasks.",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "42 tasks."
    assert store.get("t1").status == STATUS_COMPLETED


@pytest.mark.unit
def test_poll_carries_final_text_forward_across_cursor_batches(stores):
    """`get_session_state` uses an event cursor, so consecutive polls only
    return events since the last seen id. If the final `agent.message` lands
    in poll N-1 and `session.status_idle` lands alone in poll N, the latter's
    response has no text — the executor must fall back to the cached text
    from the earlier batch so the Telegram completion summary isn't empty."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        # Poll N-1: contains the agent's final message, but no idle event yet
        # (the API hasn't transitioned the session status).
        ManagedSessionState(
            session_id="sess_remote", status="running",
            last_event_id="evt_msg",
            new_events=[{"id": "evt_msg", "type": "agent.message"}],
            total_input_tokens=100, total_output_tokens=50,
            final_text="The carried-forward answer.",
        ),
        # Poll N: cursor advanced past evt_msg, so this batch has only the
        # idle event. final_text=None mirrors what `_extract_final_text`
        # returns when no agent.message event is in the batch.
        ManagedSessionState(
            session_id="sess_remote", status="completed",
            last_event_id="evt_idle",
            new_events=[{"id": "evt_idle", "type": "session.status_idle"}],
            total_input_tokens=100, total_output_tokens=50,
            final_text=None,
        ),
    ])
    executor = _make_executor(store, transcript, driver)

    # First poll: agent.message text gets cached.
    out1 = executor.poll(session)
    assert out1.status == STATUS_RUNNING
    assert store.get_managed_final_text("t1") == "The carried-forward answer."

    # Second poll: only idle event arrives. Executor must read cached text.
    session = store.get("t1")  # refresh cursor
    out2 = executor.poll(session)
    assert out2.status == STATUS_COMPLETED
    assert out2.final_text == "The carried-forward answer."


@pytest.mark.unit
def test_poll_finalizes_budget_exceeded(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="budget_exceeded",
            last_event_id="evt_bx", new_events=[],
            total_input_tokens=0, total_output_tokens=0,
            error_reason="hit max_tokens",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_tokens" in outcome.reason


@pytest.mark.unit
def test_poll_finalizes_failed_with_error_reason(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="failed",
            last_event_id="evt_fail", new_events=[],
            total_input_tokens=0, total_output_tokens=0,
            error_reason="tool dispatch crashed",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == "tool dispatch crashed"
    assert store.get("t1").status == STATUS_FAILED


@pytest.mark.unit
def test_poll_records_session_hour_overhead_on_completion(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="completed",
            last_event_id="evt_done", new_events=[],
            total_input_tokens=0, total_output_tokens=0,
            final_text="ok",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    executor.poll(session)
    # session ran for ~0 seconds in test, so overhead is ~$0 — but the path
    # exists. Verify total_dollars is a non-negative number (not None).
    assert store.get("t1").total_dollars >= 0


# ---------------------------------------------------------------------------
# poll() — budget breach mid-flight
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_poll_kills_remote_session_on_token_budget_breach(stores):
    store, session, transcript = stores
    # Tighten the budget so the first poll exceeds it.
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100, "max_dollars": 100.0},
        expected_output="text",
    )
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_x",
            new_events=[],
            total_input_tokens=120, total_output_tokens=0,  # exceeds 100-token cap
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_tokens" in outcome.reason
    # Remote session was killed.
    assert driver.kills, "expected driver.kill_session to be called"
    assert driver.kills[0][0] == "sess_remote"


@pytest.mark.unit
def test_poll_kills_remote_session_on_dollar_budget_breach(stores):
    store, session, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 1_000_000, "max_dollars": 0.001},
        expected_output="text",
    )
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    # 100 input tokens * Opus rate ($15/M) = $0.0015 — just over $0.001 cap.
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_x",
            new_events=[],
            total_input_tokens=100, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver, model="claude-opus-4-7")
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_dollars" in outcome.reason
    assert driver.kills


# ---------------------------------------------------------------------------
# poll() — error / cancellation edge cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cancelled_status_uses_distinct_telegram_reason(stores):
    """`cancelled` and `failed` both map to STATUS_FAILED internally but the
    reason text is distinct so the operator can tell them apart."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="cancelled",
            last_event_id="evt_c", new_events=[],
            total_input_tokens=0, total_output_tokens=0,
            error_reason="operator pressed cancel",
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_FAILED
    assert "cancelled" in outcome.reason
    assert "operator pressed cancel" in outcome.reason


@pytest.mark.unit
def test_poll_returns_running_on_transient_driver_error(stores):
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")

    import httpx

    class _CrashingDriver(_FakeDriver):
        def get_session_state(self, *a, **kw):
            raise httpx.TimeoutException("temporary network blip")

    executor = _make_executor(store, transcript, _CrashingDriver())
    outcome = executor.poll(session)
    # Transient errors don't kill the session — worker retries next tick.
    assert outcome.status == STATUS_RUNNING
    # Session not yet marked terminal.
    assert store.get("t1").status != STATUS_FAILED
