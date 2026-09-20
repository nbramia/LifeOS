"""Opening-prompt coverage for durable-project guidance."""
from __future__ import annotations

import pytest

from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
from api.services.agent_worker.codex_executor import _delegation_header
from api.services.agent_worker.delegation import PROJECT_TASK_GUIDANCE
from api.services.agent_worker.local_executor import _system_prompt
from api.services.agent_worker.managed_executor import _user_message_for
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.transcript_store import TranscriptStore


pytestmark = pytest.mark.unit


_BUDGET = {"wall_seconds": 60, "max_tokens": 1000, "max_dollars": 1.0}


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
