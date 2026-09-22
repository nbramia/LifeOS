"""End-to-end coverage of the journal persona's per-turn parent-attestation
set: `run_agent_loop` binds one fresh `set()` of task IDs created by a
journal `manage_tasks` create this turn and threads it through
`execute_tool_parallel` on every tool call, so a sub-task created later in
the same turn can name a parent the model just created without a spoken
`parent_id:<id>` attestation. Tool calls within one round run in parallel,
so a parent and its children always span rounds -- this drives the REAL
`run_agent_loop` (unlike tests that replace it) across two rounds with a
fake model client, and a real (isolated) `TaskManager`. All task data below
is synthetic.
"""
from __future__ import annotations

import json
import re

import pytest

from api.services.llm_client import LLMUsage
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit

_ID_RE = re.compile(r"\[id:([\w-]+)\]")


@pytest.fixture
def tm(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    index = tmp_path / "task_index.json"
    manager = TaskManager(vault_path=vault, index_path=index)
    monkeypatch.setattr("api.services.agent_tools.get_task_manager", lambda: manager, raising=False)
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


class _CreateParentThenChildClient:
    """The first model turn creates a parent task with no `parent_id`. The
    second turn creates a child naming, as its `parent_id`, the id
    `_format_native_task` printed in the first turn's tool result -- read
    straight out of the appended tool result message rather than assumed,
    since a real TaskManager assigns it. The third turn answers with plain
    text so the loop terminates.
    """

    def __init__(self, child_description: str = "Clear the synthetic shelves"):
        self.model = "fake"
        self._round = 0
        self._child_description = child_description

    async def astream(self, messages, *, system=None, max_tokens=4096, tools=None, **_kwargs):
        self._round += 1
        if self._round == 1:
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "c1",
                    "function": {
                        "name": "manage_tasks",
                        "arguments": json.dumps({
                            "action": "create",
                            "description": "Renovate the synthetic garage",
                        }),
                    },
                }],
            }
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
        elif self._round == 2:
            last_message = messages[-1]
            tool_result = last_message["content"][0]["content"]
            parent_id = _ID_RE.search(tool_result).group(1)
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "c2",
                    "function": {
                        "name": "manage_tasks",
                        "arguments": json.dumps({
                            "action": "create",
                            "description": self._child_description,
                            "parent_id": parent_id,
                        }),
                    },
                }],
            }
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
        else:
            yield {"type": "text", "content": "Filed."}
            yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}


async def _run(fake, question, **kwargs):
    from unittest.mock import patch
    from api.services import agent_loop
    with patch.object(agent_loop, "_select_client", return_value=fake):
        return [e async for e in agent_loop.run_agent_loop(
            question, persona_id="journal", user_message=question, **kwargs,
        )]


@pytest.mark.asyncio
async def test_same_turn_child_keeps_the_parents_id_end_to_end(tm):
    question = (
        "Make a project to renovate the synthetic garage with subtasks "
        "clear the synthetic shelves"
    )
    events = await _run(_CreateParentThenChildClient(), question)

    assert not any(e["type"] == "self_correction" for e in events)
    result = next(e for e in events if e["type"] == "result")["result"]
    assert [tc["tool"] for tc in result.tool_calls_log] == ["manage_tasks", "manage_tasks"]
    assert all(not tc["is_error"] for tc in result.tool_calls_log)

    tasks = tm.list_tasks()
    parent = next(t for t in tasks if t.description == "Renovate the synthetic garage")
    child = next(t for t in tasks if t.description == "Clear the synthetic shelves")
    assert child.fields.get("parent_id") == parent.id


@pytest.mark.asyncio
async def test_a_second_run_agent_loop_call_does_not_inherit_the_set(tm):
    # First turn: create the parent alone (no child), establishing a task id
    # that a later, separate turn could try to reuse as an unattested parent.
    class _CreateParentOnly:
        model = "fake"

        def __init__(self):
            self._round = 0

        async def astream(self, messages, *, system=None, max_tokens=4096, tools=None, **_kwargs):
            self._round += 1
            if self._round == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "c1",
                        "function": {
                            "name": "manage_tasks",
                            "arguments": json.dumps({
                                "action": "create",
                                "description": "Renovate the synthetic garage",
                            }),
                        },
                    }],
                }
                yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
            else:
                yield {"type": "text", "content": "Filed."}
                yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}

    await _run(_CreateParentOnly(), "Make a project to renovate the synthetic garage")
    parent = tm.list_tasks()[0]

    # Second, separate run_agent_loop call: the model tries to attach a
    # child to that same parent id without a spoken attestation. A fresh
    # per-turn set means this turn never saw that id get created, so it
    # must still be stripped.
    class _ChildOnlyNamingPriorParent:
        model = "fake"

        def __init__(self):
            self._round = 0

        async def astream(self, messages, *, system=None, max_tokens=4096, tools=None, **_kwargs):
            self._round += 1
            if self._round == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "c1",
                        "function": {
                            "name": "manage_tasks",
                            "arguments": json.dumps({
                                "action": "create",
                                "description": "Clear the synthetic shelves",
                                "parent_id": parent.id,
                            }),
                        },
                    }],
                }
                yield {"type": "done", "usage": LLMUsage(), "finish_reason": "tool_calls"}
            else:
                yield {"type": "text", "content": "Filed."}
                yield {"type": "done", "usage": LLMUsage(), "finish_reason": "end_turn"}

    await _run(_ChildOnlyNamingPriorParent(), "Clear the synthetic shelves")

    child = next(t for t in tm.list_tasks() if t.description == "Clear the synthetic shelves")
    assert child.fields.get("parent_id") is None
