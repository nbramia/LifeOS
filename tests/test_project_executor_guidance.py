"""Opening-prompt coverage for durable-project guidance."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import CodexExecutor, _delegation_header
from api.services.agent_worker.delegation import PROJECT_TASK_GUIDANCE
from api.services.agent_worker.execution import (
    BillingClass,
    ExecutionConstraints,
    ExecutionSpec,
)
from api.services.agent_worker.hermes_executor import HermesExecutor
from api.services.agent_worker.inter_agent import (
    Caps,
    InterAgentContext,
    caller_turn_proof_for_session,
)
from api.services.agent_worker.local_executor import LocalExecutor, _system_prompt
from api.services.agent_worker.managed_executor import _user_message_for
from api.services.agent_worker.session_store import STATUS_CLAIMED, STATUS_YIELDED, SessionStore
from api.services.agent_worker.tools import ToolRegistry
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.conversation_store import ConversationStore
from api.services.task_manager import TaskManager
from api.services.usage_store import UsageStore


pytestmark = pytest.mark.unit


_BUDGET = {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0}


class _CliProcess:
    """Small subprocess double matching both CLI executors' stream shape."""

    def __init__(self, events: list[dict]):
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO("")
        self.pid = 12345
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class _ScriptedLLM:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, messages, *, system=None, max_tokens, tools=None, temperature=None):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        return self.responses.pop(0)


class _LLMResponse:
    def __init__(self, tool_calls: list[dict]):
        self.text = ""
        self.tool_calls = tool_calls
        self.usage = type("Usage", (), {"input_tokens": 1, "output_tokens": 1})()
        self.model = "synthetic-local"
        self.finish_reason = "tool_use"


class _HermesResponse:
    def __init__(self, body: bytes):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield self.body


class _HermesClient:
    def __init__(self, captured: dict):
        self.captured = captured

    def stream(self, method, url, content=None, headers=None):
        self.captured.update(method=method, url=url, content=content, headers=headers)
        return self

    def __enter__(self):
        return _HermesResponse(
            b'data: {"type":"conversation_id","conversation_id":"conv-synthetic"}\n\n'
            b'data: {"type":"content","content":"done"}\n\n'
            b'data: {"type":"done"}\n\n',
        )

    def __exit__(self, *_args):
        return False

    def close(self):
        pass


def _execution_spec(executor: str) -> dict:
    return ExecutionSpec(
        executor=executor,
        provider=executor,
        runtime="in_process",
        model_id=None,
        effort=None,
        host=None,
        working_dir=None,
        persona_id=None,
        parent_session_id=None,
        root_session_id=None,
        reply_destination=None,
        budget=None,
        constraints=ExecutionConstraints(),
        billing=BillingClass.LOCAL_FREE,
        resolved_at=datetime.now(timezone.utc),
    ).to_dict()


@pytest.mark.parametrize("routing", ["local", "remote"])
def test_local_executor_system_prompt_includes_project_guidance_for_both_routes(routing):
    """LocalExecutor owns both the local and configured-remote opening paths."""
    prompt = _system_prompt(
        session_id=f"sess_{routing}",
        expected_output="text",
        budget=_BUDGET,
        attempt_id="attempt_1",
        turn_id="turn_1",
    )

    assert PROJECT_TASK_GUIDANCE in prompt


def test_managed_opening_message_includes_project_guidance():
    prompt = _user_message_for(
        {"description": "Coordinate synthetic launch work"},
        "sess_managed",
        "text",
        _BUDGET,
        attempt_id="attempt_1",
        turn_id="turn_1",
    )

    assert PROJECT_TASK_GUIDANCE in prompt


def test_claude_code_opening_system_prompt_includes_project_guidance(tmp_path):
    executor = ClaudeCodeExecutor(
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        binary_resolver=lambda: "claude",
    )
    command = executor._build_command(
        "Coordinate synthetic launch work",
        resume_session_id=None,
        session_id="sess_claude_code",
        attempt_id="attempt_1",
        turn_id="turn_1",
    )
    prompt = command[command.index("--append-system-prompt") + 1]

    assert PROJECT_TASK_GUIDANCE in prompt


def test_codex_opening_header_includes_project_guidance():
    prompt = _delegation_header("sess_codex", "attempt_1", "turn_1")

    assert PROJECT_TASK_GUIDANCE in prompt


def test_claude_code_spawn_environment_uses_current_executor_turn(tmp_path: Path, monkeypatch):
    from api.services.agent_worker import claude_code_executor

    monkeypatch.setattr(claude_code_executor, "_git_discipline_block", lambda _path: "")
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    captured: dict = {}

    def spawn(_cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _CliProcess([
            {"type": "system", "subtype": "init", "session_id": "cli-synthetic"},
            {"type": "result", "session_id": "cli-synthetic", "result": "done"},
        ])

    executor = ClaudeCodeExecutor(
        session_store=store,
        transcript_store=transcripts,
        spawn_fn=spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=3600,
    )
    source = store.create("claude-task", routing="claude_code", origin="operator")

    outcome = executor.execute(source, {
        "description": "Synthetic CLI work", "working_dir": str(tmp_path),
    })

    assert captured["env"]["LIFEOS_AGENT_SESSION_ID"] == outcome.session_id
    assert captured["env"]["LIFEOS_AGENT_ATTEMPT_ID"] == outcome.attempt_id
    assert captured["env"]["LIFEOS_AGENT_TURN_ID"] == outcome.turn_id


def test_codex_spawn_environment_uses_current_executor_turn(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    captured: dict = {}

    def spawn(command, **kwargs):
        captured["env"] = kwargs["env"]
        output_index = command.index("-o") + 1
        Path(command[output_index]).write_text("done\n", encoding="utf-8")
        return _CliProcess([
            {"type": "thread.started", "thread_id": "codex-synthetic"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ])

    executor = CodexExecutor(
        session_store=store,
        transcript_store=transcripts,
        spawn_fn=spawn,
        binary_resolver=lambda: "/usr/bin/true",
        heartbeat_interval=3600,
    )
    source = store.create("codex-task", routing="codex", origin="operator")

    outcome = executor.execute(source, {
        "description": "Synthetic CLI work", "working_dir": str(tmp_path),
    })

    assert captured["env"]["LIFEOS_AGENT_SESSION_ID"] == outcome.session_id
    assert captured["env"]["LIFEOS_AGENT_ATTEMPT_ID"] == outcome.attempt_id
    assert captured["env"]["LIFEOS_AGENT_TURN_ID"] == outcome.turn_id


@pytest.mark.parametrize("routing", ["local", "remote"])
def test_local_executor_handoff_overrides_model_identity_and_stops_remaining_work(
    tmp_path: Path, routing: str,
):
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "index" / "tasks.json",
        live_session_checker=lambda *_args: False,
    )
    parent = manager.create(
        "Coordinate synthetic local handoff",
        status="in_progress",
        tags=[routing, "agent-running"],
    )
    source = store.create(
        parent.id,
        status=STATUS_CLAIMED,
        routing=routing,
        execution_spec=_execution_spec(routing),
    )
    context = InterAgentContext(
        store,
        transcripts,
        source.session_id,
        Caps(),
        caller_attempt_id="model-forged-attempt",
        caller_turn_id="model-forged-turn",
        task_manager=manager,
    )
    tools = ToolRegistry(
        lifeos_mcp_server=type("MCP", (), {"tools": []})(),
        inter_agent_context=context,
    )
    handoff = {
        "operation_id": f"synthetic-{routing}-handoff",
        "children": [{
            "key": "local-child",
            "description": "Complete bounded synthetic child work",
            "execution": {"executor": "local"},
        }],
        "caller_session_id": "model-forged-session",
        "caller_attempt_id": "model-forged-attempt",
        "caller_turn_id": "model-forged-turn",
    }
    llm = _ScriptedLLM([_LLMResponse([
        {"id": "handoff", "name": "lifeos_agent_project_handoff", "input": handoff},
        {"id": "must-not-run", "name": "sleep", "input": {"seconds": 1}},
    ])])
    executor = LocalExecutor(
        session_store=store,
        transcript_store=transcripts,
        tool_registry=tools,
        llm_client=llm,
        model_name="synthetic-remote" if routing == "remote" else "synthetic-local",
        is_remote=routing == "remote",
    )

    outcome = executor.execute(source, {"id": parent.id, "description": parent.description})
    current = store.get(parent.id)

    assert outcome.status == STATUS_YIELDED
    assert context.caller_attempt_id == current.attempt_id
    assert context.caller_turn_id == current.turn_id
    assert len(llm.calls) == 1
    assert [event["payload"]["tool"] for event in transcripts.read(source.session_id)
            if event["kind"] == "tool_call"] == ["lifeos_agent_project_handoff"]
    assert len(manager.list_tasks()) == 2


def test_tool_registry_handoff_strips_forged_turn_identity_without_bound_context(
    tmp_path: Path, monkeypatch,
):
    """Test an unbound local context cannot forward model-supplied turn identity."""
    from api.services.agent_worker import inter_agent

    captured = {}

    def dispatch(context, name, arguments):
        captured.update(context=context, name=name, arguments=arguments)
        return {"ok": True}

    monkeypatch.setattr(inter_agent, "dispatch", dispatch)
    registry = ToolRegistry(
        lifeos_mcp_server=type("MCP", (), {"tools": []})(),
        inter_agent_context=InterAgentContext(
            SessionStore(tmp_path / "sessions.db"),
            TranscriptStore(tmp_path / "transcripts"),
            "trusted-source-session",
            Caps(),
        ),
    )

    result = registry.dispatch("lifeos_agent_project_handoff", {
        "caller_session_id": "model-forged-session",
        "caller_attempt_id": "model-forged-attempt",
        "caller_turn_id": "model-forged-turn",
    })

    assert not result.is_error
    assert captured["name"] == "lifeos_agent_project_handoff"
    assert captured["arguments"]["caller_session_id"] == "trusted-source-session"
    assert captured["arguments"]["caller_attempt_id"] is None
    assert captured["arguments"]["caller_turn_id"] is None


def test_hermes_opening_request_carries_guidance_and_current_turn_proof(tmp_path: Path, monkeypatch):
    from api.routes import hermes_proxy
    import api.services.task_manager as task_manager_module
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_url", "http://hermes.example", raising=False)
    monkeypatch.setattr(settings, "hermes_backend_token", "synthetic-backend-token", raising=False)
    monkeypatch.setattr(settings, "mcp_bearer_token", "synthetic-turn-secret", raising=False)
    monkeypatch.setattr(
        task_manager_module,
        "_task_manager",
        TaskManager(
            vault_path=tmp_path / "vault",
            index_path=tmp_path / "index" / "tasks.json",
        ),
    )
    conversations = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    usage = UsageStore(db_path=str(tmp_path / "usage.db"))
    monkeypatch.setattr(hermes_proxy, "get_store", lambda: conversations)
    monkeypatch.setattr(hermes_proxy, "get_usage_store", lambda: usage)
    monkeypatch.setattr(hermes_proxy, "schedule_retitle", lambda _conversation_id: None)
    store = SessionStore(tmp_path / "sessions.db")
    transcripts = TranscriptStore(tmp_path / "transcripts")
    captured: dict = {}
    executor = HermesExecutor(
        session_store=store,
        transcript_store=transcripts,
        http_client_factory=lambda: _HermesClient(captured),
    )
    source = store.create("hermes-task", routing="hermes")

    outcome = executor.execute(source, {"description": "Coordinate synthetic Hermes work"})
    request = json.loads(captured["content"])
    turn = request["lifeos_context"]["turn"]

    assert outcome.status == "completed"
    assert request["question"].startswith(PROJECT_TASK_GUIDANCE)
    assert turn["caller_session_id"] == outcome.session_id
    assert turn["caller_attempt_id"] == outcome.attempt_id
    assert turn["caller_turn_id"] == outcome.turn_id
    assert turn["caller_turn_proof"] == caller_turn_proof_for_session(
        outcome.session_id,
        outcome.attempt_id,
        outcome.turn_id,
        "synthetic-turn-secret",
    )
