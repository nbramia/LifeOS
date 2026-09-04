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


# ---------------------------------------------------------------------------
# build_turn_context (#591) — the per-turn context shared with the
# turn-context endpoint and the Hermes envelope. See
# tests/test_agent_system_prompt_golden.py for the byte-identical native
# prompt guarantee and tests/test_turn_context_api.py / test_hermes_proxy.py
# for the endpoint/envelope-level coverage.
# ---------------------------------------------------------------------------

def test_build_turn_context_shape(tm):
    turn = agent_system_prompt.build_turn_context()
    assert set(turn.keys()) == {
        "current_datetime", "current_datetime_iso", "timezone",
        "time_resolution_instruction", "personal_context",
        "existing_tags", "tags_instruction",
        "session_cost_usd", "session_turn_count",
        "session_input_tokens", "session_output_tokens",
        "session_cost_is_lower_bound",
    }
    assert turn["existing_tags"] == []
    assert turn["personal_context"] == ""
    # No conversation_id given -- a fresh/unscoped session reports zero
    # rather than omitting the fields or erroring (#610).
    assert turn["session_cost_usd"] == 0.0
    assert turn["session_turn_count"] == 0
    assert turn["session_input_tokens"] == 0
    assert turn["session_output_tokens"] == 0
    assert turn["session_cost_is_lower_bound"] is False


def test_build_turn_context_existing_tags_populated(tm):
    tm.create("a", tags=["work", "urgent"])
    turn = agent_system_prompt.build_turn_context()
    assert {"tag": "work", "count": 1} in turn["existing_tags"]
    assert {"tag": "urgent", "count": 1} in turn["existing_tags"]


def test_build_turn_context_degrades_on_task_manager_failure(monkeypatch):
    def boom():
        raise RuntimeError("task manager unavailable")

    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", boom)

    turn = agent_system_prompt.build_turn_context()
    assert turn["existing_tags"] == []  # degraded, not raised


def test_build_turn_context_personal_context_scoped_to_persona_id(tm, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "partner_name", "Sam")
    monkeypatch.setattr(settings, "therapist_patterns", "Dr. A")

    assert "Sam" in agent_system_prompt.build_turn_context("therapist")["personal_context"]
    assert agent_system_prompt.build_turn_context("primary")["personal_context"] == ""
    assert agent_system_prompt.build_turn_context(None)["personal_context"] == ""


# ---------------------------------------------------------------------------
# Session-to-date cost (#610) — a model that can't see what its own
# conversation has cost so far can't reason about its own expense (issue
# #610). `build_turn_context()`'s `session_*` fields expose the verbatim
# sum already recorded in the usage store (never recomputed), scoped to
# `conversation_id`, excluding the in-flight turn (its own usage isn't
# written until its stream finishes, after this context is built).
# ---------------------------------------------------------------------------

def test_build_turn_context_sums_prior_turns_for_the_conversation(tm):
    from api.services.usage_store import get_usage_store

    store = get_usage_store()  # the per-test isolated singleton (conftest)
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-a",
    )
    store.record_usage(
        model="deepseek-v3-fireworks", input_tokens=200, output_tokens=80,
        cost_usd=0.0009, conversation_id="conv-a",
    )
    # A different conversation's usage must never leak into this sum.
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=999, output_tokens=999,
        cost_usd=9.99, conversation_id="conv-b",
    )

    turn = agent_system_prompt.build_turn_context(conversation_id="conv-a")
    assert turn["session_cost_usd"] == pytest.approx(0.002 + 0.0009)
    assert turn["session_input_tokens"] == 300
    assert turn["session_output_tokens"] == 130
    assert turn["session_turn_count"] == 2
    assert turn["session_cost_is_lower_bound"] is False


def test_build_turn_context_unknown_conversation_id_reports_zero(tm):
    """A conversation_id that has never recorded usage (e.g. the very first
    turn under a freshly-minted id) is a normal state, not an error --
    present-and-zero rather than absent or raising."""
    turn = agent_system_prompt.build_turn_context(conversation_id="never-seen-before")
    assert turn["session_cost_usd"] == 0.0
    assert turn["session_turn_count"] == 0
    assert turn["session_input_tokens"] == 0
    assert turn["session_output_tokens"] == 0
    assert turn["session_cost_is_lower_bound"] is False


def test_build_turn_context_zero_cost_turn_still_reports_a_truthful_sum(tm):
    """A conversation containing a turn recorded with cost_usd=0.0 and
    unpriced=False (genuinely free, e.g. a local model) must still report
    a truthful sum and turn count, not error or silently drop it -- and
    must not be flagged as a lower bound, since nothing summed here is
    unpriced."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-mixed",
    )
    store.record_usage(
        model="some-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-mixed",
    )

    turn = agent_system_prompt.build_turn_context(conversation_id="conv-mixed")
    assert turn["session_cost_usd"] == pytest.approx(0.002)
    assert turn["session_input_tokens"] == 110
    assert turn["session_output_tokens"] == 60
    assert turn["session_turn_count"] == 2
    assert turn["session_cost_is_lower_bound"] is False


def test_build_turn_context_unpriced_turn_marks_session_cost_as_lower_bound(tm):
    """#613: a turn recorded `unpriced=True` (its provider reported no
    cost) must surface as `session_cost_is_lower_bound=True` -- the real
    distinction this field exists to carry, as opposed to #610's original
    unconditional-floor wording."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-unpriced", unpriced=False,
    )
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-unpriced", unpriced=True,
    )

    turn = agent_system_prompt.build_turn_context(conversation_id="conv-unpriced")
    assert turn["session_cost_usd"] == pytest.approx(0.002)
    assert turn["session_cost_is_lower_bound"] is True


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


def test_prompt_describes_human_queue_tool(tm):
    """#852: the orchestrator must know to file via manage_human_queue, use
    'list' for "what's waiting on me", and never file work it can do itself."""
    text = "\n".join(_text_blocks(build_system_prompt()))
    assert "manage_human_queue" in text
    assert "add/list/resolve" in text
    assert "never file work" in text.lower()
