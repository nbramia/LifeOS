"""
Tests for the agentic chat system prompt builder.

Specifically that the existing-task-tags block is injected so the LLM
can reuse tags it has seen before without needing an extra tool round.
"""
import pytest

from api.services import agent_system_prompt
from api.services.agent_system_prompt import build_system_prompt
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path, monkeypatch):
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


def _text_blocks(prompt):
    return [b["text"] for b in prompt if b.get("type") == "text"]


def test_no_tags_block_when_empty(tm):
    prompt = build_system_prompt()
    joined = "\n".join(_text_blocks(prompt))
    assert "Existing task tags" not in joined


def test_tags_block_lists_existing_tags_with_counts(tm):
    tm.create("a", tags=["work", "urgent"])
    tm.create("b", tags=["work"])
    tm.create("c", tags=["personal"])

    prompt = build_system_prompt()
    text = "\n".join(_text_blocks(prompt))

    assert "Existing task tags" in text
    assert "work (2)" in text
    assert "urgent (1)" in text
    assert "personal (1)" in text


def test_tags_block_warns_against_collapsing_to_similar(tm):
    tm.create("a", tags=["ai-agent-tag"])
    prompt = build_system_prompt()
    text = "\n".join(_text_blocks(prompt))
    assert "follow the user's wording" in text


def test_static_block_still_cached(tm):
    tm.create("a", tags=["work"])
    prompt = build_system_prompt()
    # First block stays static + cached so we don't pay re-encoding cost
    assert prompt[0].get("cache_control") == {"type": "ephemeral"}
    # Dynamic blocks should NOT carry cache_control
    for block in prompt[1:]:
        assert "cache_control" not in block


def test_max_tool_rounds_templated_not_hardcoded(tm):
    """The tool-round budget is rendered from the arg into an uncached dynamic
    block — never a hard-coded literal in the cached static prompt — so the
    prompt and the loop's actual budget can't drift."""
    prompt = build_system_prompt(max_tool_rounds=7)
    text = "\n".join(_text_blocks(prompt))
    assert "7 tool rounds" in text
    # The count must not live in the cached static block (it must stay
    # byte-stable across turns regardless of the per-turn budget).
    assert "7 tool rounds" not in prompt[0]["text"]
    assert "5 tool rounds" not in prompt[0]["text"]


def test_task_manager_failure_is_silent(monkeypatch):
    def boom():
        raise RuntimeError("task manager unavailable")

    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", boom)

    prompt = build_system_prompt()
    text = "\n".join(_text_blocks(prompt))
    # Graceful: prompt still builds, just without the tags block
    assert "Existing task tags" not in text
    assert "LifeOS" in text  # core prompt content still present


def test_existing_tags_block_helper_returns_none_when_empty(tm):
    assert agent_system_prompt._existing_tags_block() is None


def test_no_persona_block_by_default(tm):
    prompt = build_system_prompt()
    assert "FITNESS-PERSONA-MARKER" not in "\n".join(_text_blocks(prompt))


def test_persona_injected_after_static_block(tm):
    persona = "FITNESS-PERSONA-MARKER: you are the fitness bot."
    prompt = build_system_prompt(persona=persona)
    texts = _text_blocks(prompt)
    # Persona present...
    assert any("FITNESS-PERSONA-MARKER" in t for t in texts)
    # ...and placed AFTER the static block so the static cache prefix is shared.
    assert prompt[0].get("cache_control") == {"type": "ephemeral"}
    assert "FITNESS-PERSONA-MARKER" not in prompt[0]["text"]
    assert "FITNESS-PERSONA-MARKER" in prompt[1]["text"]
    # Persona block itself is not cached.
    assert "cache_control" not in prompt[1]


def test_blank_persona_adds_no_block(tm):
    base = build_system_prompt()
    spaced = build_system_prompt(persona="   ")
    assert len(spaced) == len(base)


def test_search_finances_prompt_advertises_investments(tm):
    """The orchestrator prompt must list the 'investments' action so agents
    prefer the portfolio snapshot over Monarch for net-worth questions (#447)."""
    text = "\n".join(_text_blocks(build_system_prompt()))
    assert "accounts/transactions/cashflow/budgets/investments" in text
    assert "'investments'" in text
