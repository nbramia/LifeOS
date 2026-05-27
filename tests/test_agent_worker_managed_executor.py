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

    def __init__(self, *, create_returns="sess_remote", state_responses=None,
                 create_raises=None, post_raises=None, update_raises=None):
        self.create_returns = create_returns
        self.state_responses = list(state_responses or [])
        self.create_raises = create_raises
        self.post_raises = post_raises
        self.update_raises = update_raises
        self.kills: list[tuple[str, str]] = []
        self.created_with: dict | None = None
        self.posted_messages: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict]] = []
        self.poll_calls: list[tuple[str, str | None]] = []

    def create_session(self, **kwargs):
        self.created_with = kwargs
        if self.create_raises:
            raise self.create_raises
        return self.create_returns

    def post_user_message(self, session_id, content):
        self.posted_messages.append((session_id, content))
        if self.post_raises:
            raise self.post_raises

    def update_session(self, session_id, agent_payload):
        self.updates.append((session_id, agent_payload))
        if self.update_raises:
            raise self.update_raises

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
    # Per #139 §3 the initial message is NOT in the create_session kwargs —
    # it's posted as a separate user.message AFTER an optional update_session.
    assert "initial_message" not in cw
    # The initial user message landed via post_user_message.
    assert len(driver.posted_messages) == 1
    posted_sid, posted_msg = driver.posted_messages[0]
    assert posted_sid == "sess_remote_42"
    # Posted message carries task description + soft budget. Budget is
    # framed as "soft" because Anthropic doesn't enforce it server-side;
    # the worker kills the session externally on breach.
    assert "say hi" in posted_msg
    assert "expected_output=text" in posted_msg
    assert "soft budget" in posted_msg
    assert "~$5.0" in posted_msg
    # Today's date is injected for day-relative reasoning.
    import re
    assert re.search(r"today=\d{4}-\d{2}-\d{2} \([A-Z][a-z]+day\)", posted_msg), \
        posted_msg[-300:]
    # Ambiguity policy NOT duplicated here — it lives in the agent preset.
    assert "ambiguous" not in posted_msg
    assert f"lifeos_session_id={session.session_id}" in posted_msg
    # NO inline system_prompt / model / mcp_servers / connectors / update call
    # (no preset_class set on the session → no filter applied).
    assert "system_prompt" not in cw
    assert "model" not in cw
    assert driver.updates == [], "no preset_class set → no update_session call"
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
def test_start_applies_tool_filter_when_preset_class_set(stores):
    """When session.preset_class is set, the executor calls update_session
    between create and the first user message — scoping cache_creation
    to the class's filtered tool list (#139 §3)."""
    store, _, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
        expected_output="text",
        preset_class="research",
    )
    session = store.get("t1")
    driver = _FakeDriver(create_returns="sess_remote")
    executor = _make_executor(store, transcript, driver)
    outcome = executor.start(session, {"id": "t1", "description": "look it up"})
    assert outcome.status == STATUS_RUNNING
    # update_session called once with the research-class filter.
    assert len(driver.updates) == 1
    upd_sid, upd_payload = driver.updates[0]
    assert upd_sid == "sess_remote"
    assert "tools" in upd_payload
    assert "lifeos_drive_search" in upd_payload["tools"]  # research-class specialty
    assert "lifeos_search" in upd_payload["tools"]        # cross-cutting
    # The order matters: update_session must happen BEFORE the user message
    # or cache_creation locks in on the unfiltered preset.
    assert driver.posted_messages, "user message should still be posted"


@pytest.mark.unit
def test_start_skips_filter_for_fullstack_preset_class(stores):
    """preset_class=fullstack means no filter — update_session is skipped."""
    store, _, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
        expected_output="text",
        preset_class="fullstack",
    )
    session = store.get("t1")
    driver = _FakeDriver(create_returns="sess_remote")
    executor = _make_executor(store, transcript, driver)
    executor.start(session, {"id": "t1", "description": "anything"})
    assert driver.updates == []


@pytest.mark.unit
def test_start_falls_back_to_full_preset_on_update_failure(stores):
    """If update_session fails (4xx, schema mismatch), the session still
    proceeds with the full preset — we don't want to abort a billable
    session over a non-fatal filter issue."""
    import httpx
    store, _, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 100_000, "max_dollars": 5.0},
        expected_output="text",
        preset_class="research",
    )
    session = store.get("t1")
    driver = _FakeDriver(
        create_returns="sess_remote",
        update_raises=httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(400),
        ),
    )
    executor = _make_executor(store, transcript, driver)
    outcome = executor.start(session, {"id": "t1", "description": "research X"})
    # Session continues despite the filter failure.
    assert outcome.status == STATUS_RUNNING
    assert driver.posted_messages, "initial message still posted after filter failure"


@pytest.mark.unit
def test_start_marks_failed_when_post_user_message_fails(stores):
    """If posting the initial user message fails, the session is marked
    failed — we can't proceed without it."""
    store, _, transcript = stores
    driver = _FakeDriver(
        create_returns="sess_remote",
        post_raises=RuntimeError("network blip"),
    )
    session = store.get("t1")
    executor = _make_executor(store, transcript, driver)
    outcome = executor.start(session, {"id": "t1", "description": "x"})
    assert outcome.status == STATUS_FAILED
    assert "post_user_message failed" in outcome.reason


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
def test_poll_persists_cache_creation_and_cache_read_deltas(stores):
    """All four token buckets (uncached input, output, cache_creation,
    cache_read) must flow from driver state through record_spend into the
    sessions row. Confirms the dollar total reflects cache costs."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_1",
            new_events=[{"id": "evt_1", "type": "x"}],
            total_input_tokens=10,
            total_output_tokens=5,
            total_cache_creation_tokens=100_000,
            total_cache_read_tokens=2_000,
        ),
    ])
    executor = _make_executor(store, transcript, driver, model="claude-sonnet-4-6")
    executor.poll(session)
    refreshed = store.get("t1")
    assert refreshed.total_input_tokens == 10
    assert refreshed.total_output_tokens == 5
    assert refreshed.total_cache_creation_tokens == 100_000
    assert refreshed.total_cache_read_tokens == 2_000
    # Sonnet input $3/M:
    # 10 input = $0.00003, 5 output ($15/M) = $0.000075,
    # 100k cache_creation (×1.25) = $0.375, 2k cache_read (×0.10) = $0.0006.
    # The dollar total also accrues session-hour overhead from test wall time
    # ($0.08/hr) so we tolerate a small positive delta above the token cost.
    expected_token_dollars = (
        10 * 3.0e-6
        + 5 * 15.0e-6
        + 100_000 * 3.0e-6 * 1.25
        + 2_000 * 3.0e-6 * 0.10
    )
    overhead_upper_bound = 60.0 / 3600.0 * 0.08  # 60s wall = generous
    assert expected_token_dollars <= refreshed.total_dollars <= expected_token_dollars + overhead_upper_bound


@pytest.mark.unit
def test_poll_computes_cache_token_deltas_against_prior_state(stores):
    """Token totals are absolute remote counts; record_spend gets deltas only."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_1",
            new_events=[{"id": "evt_1", "type": "x"}],
            total_input_tokens=5,
            total_output_tokens=3,
            total_cache_creation_tokens=50_000,
            total_cache_read_tokens=1_000,
        ),
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_2",
            new_events=[{"id": "evt_2", "type": "y"}],
            total_input_tokens=12,
            total_output_tokens=8,
            total_cache_creation_tokens=50_000,  # no new cache_creation
            total_cache_read_tokens=4_500,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    executor.poll(session)
    session = store.get("t1")
    executor.poll(session)
    refreshed = store.get("t1")
    # Cumulative absolute totals — not double-counted.
    assert refreshed.total_input_tokens == 12
    assert refreshed.total_output_tokens == 8
    assert refreshed.total_cache_creation_tokens == 50_000
    assert refreshed.total_cache_read_tokens == 4_500


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


@pytest.mark.unit
def test_poll_dollar_breach_fires_from_cache_creation_cost_alone(stores):
    """The whole point of #137: cache_creation-heavy sessions must trip
    max_dollars even when uncached input + output are negligible. Previously
    the dollar count missed cache cost entirely, so this didn't fire."""
    store, session, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        budget={"wall_seconds": 3600, "max_tokens": 1_000_000, "max_dollars": 0.10},
        expected_output="text",
    )
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    # 100k cache_creation at Sonnet ($3/M × 1.25) = $0.375 — over the $0.10 cap.
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_x",
            new_events=[],
            total_input_tokens=3,
            total_output_tokens=81,
            total_cache_creation_tokens=100_000,
            total_cache_read_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver, model="claude-sonnet-4-6")
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_dollars" in outcome.reason


@pytest.mark.unit
def test_poll_dollar_breach_takes_precedence_over_token_breach(stores):
    """When both budgets would breach, max_dollars wins. Dollars-first
    enforcement means runaway cache_creation cost can't be masked by a
    generous token cap."""
    store, session, transcript = stores
    store.set_routing_and_budget(
        "t1",
        routing="claude",
        # Both caps fail: 200 tokens > 100-cap AND $0.003 > $0.001-cap.
        budget={"wall_seconds": 3600, "max_tokens": 100, "max_dollars": 0.001},
        expected_output="text",
    )
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_x",
            new_events=[],
            total_input_tokens=200, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver, model="claude-opus-4-7")
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "max_dollars" in outcome.reason
    assert "max_tokens" not in outcome.reason


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
def test_sanitize_title_strips_newlines_and_control_chars():
    """Live repro: spawn prompt contained `\\n` newlines, Anthropic 400'd
    with `"title: must not contain Unicode control or format characters"`.
    The sanitizer must collapse whitespace control chars (newlines, tabs)
    to single spaces and drop other control / format codepoints."""
    from api.services.agent_worker.managed_executor import _sanitize_title

    # Multi-line spawn prompt (common from cloud parent agents).
    multiline = "You are analyzing WORK Gmail.\n\nStrategy:\n1. Foo\n2. Bar"
    out = _sanitize_title(multiline)
    assert out is not None
    assert "\n" not in out
    assert "\r" not in out
    assert "\t" not in out
    assert out.startswith("You are analyzing WORK Gmail.")
    # Collapsed double-newline becomes a single space, not two.
    assert "  " not in out

    # Empty / whitespace-only → None so the API uses its default.
    assert _sanitize_title("") is None
    assert _sanitize_title(None) is None
    assert _sanitize_title("\n\n\t\r") is None

    # Length cap at 100 chars.
    long = "x" * 500
    assert len(_sanitize_title(long)) == 100

    # Non-control unicode is preserved (em-dashes, curly quotes, etc.).
    assert _sanitize_title("Compare — last month's data") == "Compare — last month's data"


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


# ---------------------------------------------------------------------------
# Runaway detection (#139 Section 5)
# ---------------------------------------------------------------------------

def _tool_use(tool_name: str, args: dict, event_id: str = "evt") -> dict:
    """Helper: build a `tool_use` event in the shape the driver returns."""
    return {"id": event_id, "type": "tool_use", "name": tool_name, "input": args}


def _agent_message(text: str = "thinking", event_id: str = "evt") -> dict:
    return {"id": event_id, "type": "agent.message",
            "content": [{"type": "text", "text": text}]}


def _tool_result(payload: str, event_id: str = "evt") -> dict:
    return {"id": event_id, "type": "tool_result", "content": payload}


@pytest.mark.unit
def test_runaway_tool_loop_kill_after_four_identical_calls(stores):
    """Same tool + identical args 4× consecutively → kill."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    def same(i):
        return _tool_use("lifeos_search", {"q": "X"}, f"evt_{i}")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_4",
            new_events=[same(1), same(2), same(3), same(4)],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "tool_loop_detected" in outcome.reason
    assert driver.kills  # remote session was killed


@pytest.mark.unit
def test_runaway_tool_loop_not_triggered_by_three_identical_calls(stores):
    """Threshold is 4 — three identical calls is still tolerated (could
    be a transient retry sequence)."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    def same(i):
        return _tool_use("lifeos_search", {"q": "X"}, f"evt_{i}")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_3",
            new_events=[same(1), same(2), same(3)],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_RUNNING
    assert not driver.kills


@pytest.mark.unit
def test_runaway_tool_loop_resets_on_different_tool(stores):
    """A different tool resets the loop counter so a legitimate retry path
    (toolA, toolA, toolB, toolA, toolA, toolA, toolA) doesn't trip."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    # 3× toolA, then a different tool, then 3× toolA again — none of those
    # streaks reach the 4-call threshold individually.
    events = [
        _tool_use("toolA", {"q": "X"}, "evt_1"),
        _tool_use("toolA", {"q": "X"}, "evt_2"),
        _tool_use("toolA", {"q": "X"}, "evt_3"),
        _tool_use("toolB", {"q": "Y"}, "evt_4"),
        _tool_use("toolA", {"q": "X"}, "evt_5"),
        _tool_use("toolA", {"q": "X"}, "evt_6"),
    ]
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_6",
            new_events=events,
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_RUNNING
    assert not driver.kills


@pytest.mark.unit
def test_runaway_tool_loop_persists_across_polls(stores):
    """Counter survives across polls: 2 identical calls in poll 1, 2 more
    identical in poll 2 → 4 consecutive → kill on poll 2."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    def same(i):
        return _tool_use("lifeos_search", {"q": "X"}, f"evt_{i}")
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_2",
            new_events=[same(1), same(2)],
            total_input_tokens=0, total_output_tokens=0,
        ),
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_4",
            new_events=[same(3), same(4)],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome1 = executor.poll(session)
    assert outcome1.status == STATUS_RUNNING
    session = store.get("t1")
    outcome2 = executor.poll(session)
    assert outcome2.status == STATUS_BUDGET_EXCEEDED
    assert "tool_loop_detected" in outcome2.reason


@pytest.mark.unit
def test_runaway_no_progress_kill_after_fifteen_tools_no_message(stores):
    """15 tool calls with no agent.message → kill."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    # 15 different tools so tool-loop doesn't fire first.
    events = [
        _tool_use(f"tool_{i}", {"i": i}, f"evt_{i}") for i in range(15)
    ]
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_14",
            new_events=events,
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert "no_text_in_15_tool_calls" in outcome.reason


@pytest.mark.unit
def test_runaway_no_progress_resets_on_agent_message(stores):
    """An intervening agent.message resets the no-progress counter."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    # 10 tools, then a message, then 10 more tools — neither streak
    # reaches 15, so no kill.
    events = (
        [_tool_use(f"tool_{i}", {"i": i}, f"evt_a_{i}") for i in range(10)]
        + [_agent_message("here's an update", "evt_msg")]
        + [_tool_use(f"tool_{i}", {"i": i}, f"evt_b_{i}") for i in range(10)]
    )
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_b_9",
            new_events=events,
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    outcome = executor.poll(session)
    assert outcome.status == STATUS_RUNNING


@pytest.mark.unit
def test_oversized_tool_result_truncated_in_transcript(stores):
    """A >20KB tool_result is truncated when mirrored to the transcript."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    big_payload = "X" * 30_000
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_1",
            new_events=[_tool_result(big_payload, "evt_1")],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    executor.poll(session)
    # The transcript should hold a truncated copy, not the full payload.
    entries = transcript.read(session.session_id)
    tool_results = [e for e in entries if e["kind"] == "managed_event_tool_result"]
    assert tool_results
    stored = tool_results[0]["payload"]
    assert len(stored["content"]) < 30_000
    assert "[…truncated]" in stored["content"]
    assert stored.get("_truncated_from_chars") == 30_000


@pytest.mark.unit
def test_undersized_tool_result_not_truncated(stores):
    """Tool results under the threshold pass through unchanged."""
    store, session, transcript = stores
    store.set_managed_session_id("t1", "sess_remote")
    session = store.get("t1")
    payload = "ok"
    driver = _FakeDriver(state_responses=[
        ManagedSessionState(
            session_id="sess_remote", status="running", last_event_id="evt_1",
            new_events=[_tool_result(payload, "evt_1")],
            total_input_tokens=0, total_output_tokens=0,
        ),
    ])
    executor = _make_executor(store, transcript, driver)
    executor.poll(session)
    entries = transcript.read(session.session_id)
    tool_results = [e for e in entries if e["kind"] == "managed_event_tool_result"]
    assert tool_results[0]["payload"]["content"] == "ok"
    assert "_truncated_from_chars" not in tool_results[0]["payload"]
