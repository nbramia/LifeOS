"""Tests for codex_spawn + CodexExecutor."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from api.services.agent_worker import codex_spawn
from api.services.agent_worker.codex_executor import (
    CodexExecutor,
    REASON_BINARY_NOT_FOUND,
)
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(tmp_path: Path):
    """Isolated session/transcript stores backed by tmp_path."""
    session_db = tmp_path / "sessions.db"
    transcripts_dir = tmp_path / "transcripts"
    return SessionStore(db_path=str(session_db)), TranscriptStore(transcripts_dir=transcripts_dir)


@pytest.mark.unit
def test_spawn_codex_session_creates_row(stores):
    sess_store, _ = stores
    result = codex_spawn.spawn_codex_session(
        sess_store, "do a thing", working_dir="/tmp", chat_id="42",
    )
    assert result["ok"] is True
    session = sess_store.get_by_session_id(result["session_id"])
    assert session.routing == "codex"
    assert session.origin == "operator"
    assert session.parent_session_id is None
    # Pending message holds the JSON payload.
    msgs = sess_store.drain_pending_messages(session.session_id)
    assert len(msgs) == 1
    payload = json.loads(msgs[0]["content"])
    assert payload["prompt"] == "do a thing"
    assert payload["working_dir"] == "/tmp"
    assert payload["chat_id"] == "42"


@pytest.mark.unit
def test_spawn_codex_session_rejects_empty(stores):
    sess_store, _ = stores
    result = codex_spawn.spawn_codex_session(sess_store, "")
    assert result["ok"] is False
    assert "required" in result["error"]


@pytest.mark.unit
def test_parse_codex_spawn_payload_round_trips():
    encoded = json.dumps({"prompt": "p", "working_dir": "/w", "chat_id": "c"})
    decoded = codex_spawn.parse_codex_spawn_payload(encoded)
    assert decoded == {"prompt": "p", "working_dir": "/w", "chat_id": "c"}


@pytest.mark.unit
def test_parse_codex_spawn_payload_falls_back_to_string():
    decoded = codex_spawn.parse_codex_spawn_payload("just a string")
    assert decoded == {"prompt": "just a string", "working_dir": None, "chat_id": None}


# ---------------------------------------------------------------------------
# executor — fake spawn that returns a canned JSONL stream
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal subprocess.Popen substitute for stream-parsing tests."""
    def __init__(self, lines: list[dict], returncode: int = 0, stderr_text: str = ""):
        self.stdout = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n")
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.pid = 12345

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.mark.unit
def test_executor_handles_full_stream(stores, monkeypatch, tmp_path):
    """End-to-end stream parse → session marked completed, final_text captured."""
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t1",
        session_id="sess_codex_1",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )

    lines = [
        {"type": "thread.started", "thread_id": "thread-abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100, "cached_input_tokens": 0,
                "output_tokens": 10, "reasoning_output_tokens": 0,
            },
        },
    ]

    fake_proc = _FakeProc(lines, returncode=0)

    def fake_spawn(cmd, **kwargs):
        # Write the "final message" file the executor expects.
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("Hello world\n")
        return fake_proc

    notifications: list[str] = []
    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        notification_callback=notifications.append,
        spawn_fn=fake_spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,  # don't fire in tests
    )

    outcome = executor.execute(session, {"description": "say hi", "working_dir": str(tmp_path)})
    assert outcome.status == STATUS_COMPLETED
    assert outcome.final_text == "Hello world"
    # The executor no longer streams agent messages to Telegram — the worker
    # sends the final message once on completion. Only heartbeats (suppressed
    # here) would reach the callback mid-run, so it stays empty.
    assert notifications == []
    # Thread id persisted for future resume.
    reloaded = sess_store.get_by_session_id(session.session_id)
    assert reloaded.claude_code_session_id == "thread-abc"


@pytest.mark.unit
def test_high_cost_does_not_cap_subscription_route(stores, monkeypatch, tmp_path):
    """Codex is subscription-billed — a high reported cost must NOT cap the task.
    Only the managed/API route enforces a dollar cap. Here the turn reports
    2M+2M tokens (~$70 rolled up) against a $0.001 cap; under the old behavior
    that was BUDGET_EXCEEDED, now it completes (cost is tracked, not enforced)."""
    monkeypatch.setattr(
        "api.services.agent_worker.codex_executor.settings.claude_max_cost_usd",
        0.001,
    )
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t_cost",
        session_id="sess_codex_cost",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )

    lines = [
        {"type": "thread.started", "thread_id": "thread-cost"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 2_000_000, "cached_input_tokens": 0,
                "output_tokens": 2_000_000, "reasoning_output_tokens": 0,
            },
        },
    ]
    fake_proc = _FakeProc(lines, returncode=0)

    def fake_spawn(cmd, **kwargs):
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("done\n")
        return fake_proc

    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        notification_callback=lambda _m: None,
        spawn_fn=fake_spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )

    outcome = executor.execute(session, {"description": "expensive", "working_dir": str(tmp_path)})
    assert outcome.status == STATUS_COMPLETED
    assert sess_store.get(session.task_id).status == STATUS_COMPLETED


@pytest.mark.unit
def test_executor_prepends_capabilities_preamble(stores, monkeypatch, tmp_path):
    """A fresh /codex turn carries the LifeOS capabilities briefing so the
    agent has the same situational awareness as the managed/local routes."""
    from api.services.agent_worker.capabilities_preamble import CAPABILITIES_PREAMBLE

    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t_pre", session_id="sess_pre", status="claimed", routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text", origin="operator",
    )

    captured: dict = {}

    def fake_spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("ok\n")
        return _FakeProc([{"type": "session.completed"}], returncode=0)

    executor = CodexExecutor(
        session_store=sess_store, transcript_store=tr_store,
        spawn_fn=fake_spawn, binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    executor.execute(session, {"description": "what's on my calendar?", "working_dir": str(tmp_path)})

    # The prompt is the last positional arg of the codex command.
    prompt_arg = captured["cmd"][-1]
    assert "=== LIFEOS BRIEFING" in prompt_arg
    assert prompt_arg.endswith("what's on my calendar?")
    assert CAPABILITIES_PREAMBLE in prompt_arg


@pytest.mark.unit
def test_executor_injects_delegation_header_with_session_id(stores, tmp_path):
    """The opening Codex turn tells the agent its session id and how to
    delegate work it can't do (e.g. browser) to a claude_code child."""
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t_del", session_id="sess_del", status="claimed", routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text", origin="operator",
    )

    captured: dict = {}

    def fake_spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("ok\n")
        return _FakeProc([{"type": "session.completed"}], returncode=0)

    executor = CodexExecutor(
        session_store=sess_store, transcript_store=tr_store,
        spawn_fn=fake_spawn, binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    executor.execute(session, {"description": "do a thing", "working_dir": str(tmp_path)})

    prompt_arg = captured["cmd"][-1]
    assert "sess_del" in prompt_arg
    assert "lifeos_agent_spawn" in prompt_arg
    assert 'model="claude_code"' in prompt_arg


@pytest.mark.unit
def test_resume_does_not_prepend_preamble(stores, tmp_path):
    """Resume reloads the Codex thread (which already holds the preamble from
    the opening turn), so the follow-up message must NOT re-inject it."""
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t_res", session_id="sess_res", status="claimed", routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text", origin="operator",
    )
    sess_store.set_claude_code_session_id("t_res", "thread-xyz")
    session = sess_store.get_by_session_id("sess_res")

    captured: dict = {}

    def fake_spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("ok\n")
        return _FakeProc([{"type": "session.completed"}], returncode=0)

    executor = CodexExecutor(
        session_store=sess_store, transcript_store=tr_store,
        spawn_fn=fake_spawn, binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    executor.resume(session, "and tomorrow?", working_dir=str(tmp_path))

    prompt_arg = captured["cmd"][-1]
    assert "=== LIFEOS BRIEFING" not in prompt_arg
    assert prompt_arg == "and tomorrow?"
    assert "resume" in captured["cmd"]


@pytest.mark.unit
def test_executor_does_not_stream_intermediate_messages(stores, tmp_path):
    """Codex narrates before each tool call; those agent messages must not be
    forwarded to Telegram (the flood the issue describes). The callback only
    ever sees heartbeats, which are suppressed in tests — so it stays empty,
    while final_text tracks the last message for the worker to send once."""
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t_flood", session_id="sess_flood", status="claimed", routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text", origin="operator",
    )

    lines = [
        {"type": "thread.started", "thread_id": "thread-f"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Let me search."}},
        {"type": "item.completed", "item": {"type": "command_executed", "command": "grep"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Now I'll check email."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer: 3 events."}},
        {"type": "session.completed"},
    ]

    def fake_spawn(cmd, **kwargs):
        for i, tok in enumerate(cmd):
            if tok == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w") as f:
                    f.write("Final answer: 3 events.\n")
        return _FakeProc(lines, returncode=0)

    notifications: list[str] = []
    executor = CodexExecutor(
        session_store=sess_store, transcript_store=tr_store,
        notification_callback=notifications.append,
        spawn_fn=fake_spawn, binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    outcome = executor.execute(session, {"description": "events?", "working_dir": str(tmp_path)})

    assert notifications == []  # no intermediate flooding
    assert outcome.final_text == "Final answer: 3 events."


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_body, expect_warning",
    [
        ("", True),  # no config at all
        ("model = \"gpt-5.5\"\n", True),  # config but no MCP servers
        ('[mcp_servers.other]\ncommand = "x"\n', True),  # a different MCP server only
        ('[mcp_servers.lifeos]\ncommand = "py"\n', False),  # lifeos configured
    ],
)
def test_warn_if_mcp_missing_keys_on_lifeos(
    stores, tmp_path, monkeypatch, caplog, config_body, expect_warning
):
    """The dispatch warning fires unless the *lifeos* MCP server specifically
    is configured — an unrelated server leaves LifeOS just as unreachable."""
    import logging

    sess_store, tr_store = stores
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(config_body)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    executor = CodexExecutor(
        session_store=sess_store, transcript_store=tr_store,
        binary_resolver=lambda: "/usr/bin/true", heartbeat_interval=9999,
    )
    with caplog.at_level(logging.WARNING):
        executor._warn_if_mcp_missing()

    warned = any("no [mcp_servers.lifeos]" in r.getMessage() for r in caplog.records)
    assert warned is expect_warning
    # Gated to once per process — a second call never re-warns.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        executor._warn_if_mcp_missing()
    assert not any("mcp_servers.lifeos" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_executor_binary_not_found(stores, tmp_path):
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t2",
        session_id="sess_codex_2",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )

    def fail_spawn(*args, **kwargs):
        raise FileNotFoundError("nope")

    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        spawn_fn=fail_spawn,
        binary_resolver=lambda: "/nonexistent/codex",
        heartbeat_interval=9999,
    )
    outcome = executor.execute(session, {"description": "x", "working_dir": str(tmp_path)})
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == REASON_BINARY_NOT_FOUND


@pytest.mark.unit
def test_executor_rejects_empty_prompt(stores, tmp_path):
    sess_store, tr_store = stores
    session = sess_store.create(
        task_id="t3",
        session_id="sess_codex_3",
        status="claimed",
        routing="codex",
        budget={"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0},
        expected_output="text",
        origin="operator",
    )
    executor = CodexExecutor(
        session_store=sess_store,
        transcript_store=tr_store,
        spawn_fn=lambda *a, **k: _FakeProc([]),
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=9999,
    )
    outcome = executor.execute(session, {"description": "  "})
    assert outcome.status == STATUS_FAILED
    assert outcome.reason == "empty prompt"
