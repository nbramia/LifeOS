"""Classifying a failed LLM call as retryable, on the event loop.

``is_retryable_api_error`` runs inside the chat loop's ``except`` block, on
the event-loop thread. It recognises httpx and Anthropic SDK errors without
importing either library, so the first failed model call in a process that
has not loaded the Anthropic SDK does not stall other requests while the SDK
imports.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from api.services.resilience import is_retryable_api_error

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_fresh(script: str) -> str:
    """Run ``script`` in a new interpreter at the repo root; return its stdout."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


class TestNoImportOnTheLoop:
    def test_classifying_an_error_does_not_load_the_anthropic_sdk(self):
        out = _run_fresh("""
            import sys
            from api.services.resilience import is_retryable_api_error

            assert "anthropic" not in sys.modules
            is_retryable_api_error(RuntimeError("synthetic failure"))
            print("anthropic loaded:", "anthropic" in sys.modules)
        """)
        assert "anthropic loaded: False" in out

    def test_a_failed_chat_turn_does_not_load_the_anthropic_sdk(self):
        """The whole turn, model failure included, runs without importing
        the SDK when the configured client is not the Anthropic one."""
        out = _run_fresh("""
            import asyncio
            import sys
            from unittest.mock import patch

            from api.services import agent_loop

            class _MemoryStore:
                def get_relevant_memories(self, query, limit=5):
                    return []

            class _Client:
                async def astream(self, messages, **kwargs):
                    if False:
                        yield {}
                    raise RuntimeError("synthetic model failure")

            async def turn():
                return [e async for e in agent_loop.run_agent_loop("synthetic question")]

            before = "anthropic" in sys.modules
            with patch.object(agent_loop, "_select_client", return_value=_Client()), \\
                    patch("api.services.memory_store.get_memory_store", return_value=_MemoryStore()):
                events = asyncio.run(turn())
            assert any(e["type"] == "text" for e in events)
            print("before:", before, "after:", "anthropic" in sys.modules)
        """)
        assert "before: False after: False" in out


class TestClassification:
    def test_anthropic_rate_limit_and_server_errors_are_retryable(self):
        import anthropic

        request = httpx.Request("POST", "https://api.example.test/v1/messages")

        def status_error(code):
            return anthropic.APIStatusError(
                "synthetic", response=httpx.Response(code, request=request), body=None
            )

        assert is_retryable_api_error(status_error(429)) is True
        assert is_retryable_api_error(status_error(529)) is True
        assert is_retryable_api_error(status_error(400)) is False
        assert is_retryable_api_error(anthropic.APIConnectionError(request=request)) is True

    def test_httpx_errors(self):
        request = httpx.Request("POST", "http://llm.example.test/v1/chat/completions")
        unavailable = httpx.HTTPStatusError(
            "synthetic", request=request, response=httpx.Response(503, request=request)
        )
        bad_request = httpx.HTTPStatusError(
            "synthetic", request=request, response=httpx.Response(400, request=request)
        )

        assert is_retryable_api_error(unavailable) is True
        assert is_retryable_api_error(bad_request) is False
        assert is_retryable_api_error(httpx.ConnectError("synthetic", request=request)) is True

    def test_other_errors(self):
        assert is_retryable_api_error(RuntimeError("synthetic")) is False
        assert is_retryable_api_error(ConnectionError("synthetic")) is True
        assert is_retryable_api_error(TimeoutError("synthetic")) is True
