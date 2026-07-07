"""Tests for the shared inter-agent delegation source (#383 Phase 3).

All three worker executors (claude_code, codex, local) build their delegation
guidance from api.services.agent_worker.delegation, so renaming a lifeos_agent_*
tool touches exactly one file.
"""
import pytest

from api.services.agent_worker import delegation

pytestmark = pytest.mark.unit


def test_preamble_contains_session_id_and_core_mechanic():
    p = delegation.delegation_preamble("SID-1", trigger="To do X,", model='"local"')
    assert "SID-1" in p
    assert "caller_session_id=SID-1" in p
    assert "To do X," in p
    assert 'model="local"' in p
    for tool in (delegation.SPAWN, delegation.CHECK, delegation.TRANSCRIPT_READ):
        assert tool in p


def test_preamble_explains_child_clarification_reopen():
    """A parent must learn from its prompt what a child's
    '[needs clarification]' output means and how to answer it: send the
    answer (which reopens the child with full context), THEN yield again."""
    p = delegation.delegation_preamble("SID-1", trigger="To do X,", model='"local"')
    assert "[needs clarification]" in p
    assert delegation.SEND in p
    assert delegation.YIELD_UNTIL in p
    # Ordering matters: a yielded parent can't send, so the answer goes first.
    assert "before" in p.lower()


def test_inter_agent_block_explains_child_clarification_reopen():
    block = delegation.INTER_AGENT_BLOCK
    assert "[needs clarification]" in block
    assert delegation.SEND in block


def test_inter_agent_block_references_full_protocol():
    block = delegation.INTER_AGENT_BLOCK
    for tool in (
        delegation.SPAWN,
        delegation.CHECK,
        delegation.SEND,
        delegation.SESSIONS_LIST,
        delegation.TRANSCRIPT_READ,
        delegation.YIELD_UNTIL,
    ):
        assert tool in block


def test_inter_agent_block_text_is_pinned():
    """Pin the exact block text. local_executor embeds this verbatim in its
    cached _SYSTEM_PROMPT_STATIC, so an accidental edit here would silently
    change (and invalidate the cache of) that prompt."""
    assert delegation.INTER_AGENT_BLOCK == (
        "<inter_agent>\n"
        "Other agent sessions are visible via `lifeos_agent_transcript_read` and\n"
        "`lifeos_agent_sessions_list`. Spawn child agents with `lifeos_agent_spawn`,\n"
        "message them with `lifeos_agent_send`, check status with\n"
        "`lifeos_agent_check`. When you have nothing to do until specific children\n"
        "finish, call `lifeos_agent_yield_until(children=[...])` — this ends your\n"
        "session cleanly (no idle billing) and resumes you when the children are\n"
        "done. Prefer `yield_until` over polling. If a child's output contains\n"
        "\"[needs clarification] …\", it stopped mid-task to ask you a question:\n"
        "answer with `lifeos_agent_send` (this reopens the child with its full prior\n"
        "context), then yield on it again — send the answer before yielding.\n"
        "</inter_agent>"
    )


def test_all_three_executors_draw_from_the_shared_source():
    # codex builds its header from the shared preamble
    from api.services.agent_worker.codex_executor import _delegation_header
    header = _delegation_header("S1")
    assert delegation.SPAWN in header and "S1" in header

    # local embeds the shared inter-agent block verbatim
    from api.services.agent_worker.local_executor import _SYSTEM_PROMPT_STATIC
    assert delegation.INTER_AGENT_BLOCK in _SYSTEM_PROMPT_STATIC

    # claude_code fills a {delegation} slot from the same helper
    from api.services.agent_worker import claude_code_executor
    assert "{delegation}" in claude_code_executor._SYSTEM_PROMPT
    assert claude_code_executor.delegation_preamble is delegation.delegation_preamble
