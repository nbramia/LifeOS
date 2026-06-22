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
