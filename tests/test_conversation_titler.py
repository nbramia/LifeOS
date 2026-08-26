"""Unit tests for the shared post-turn conversation titling seam
(api/services/conversation_titler.py) — the one titling implementation
shared by the native chat turn (api/routes/chat.py), the Hermes proxy tee
(api/routes/hermes_proxy.py), and the #711 voice tee (api/routes/voice.py).

Wiring — that each of those three surfaces actually calls
``schedule_retitle()`` at the right point — is covered by call-site tests in
test_chat_api.py / test_hermes_proxy.py / test_voice_proxy.py. This file
covers the shared logic itself: the not-before-2nd-message gate,
failure-safety, and title sanitization — using a fake LLM client
(monkeypatched ``generate_text``) rather than a real local model.
"""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from api.services import conversation_titler as titler
from api.services.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return ConversationStore(db_path=str(tmp_path / "conversations.db"))


def _seed(store, conv_id, *, user_messages, persona_id="primary"):
    """Create a conversation with the placeholder title and alternating
    user/assistant messages, mirroring a real turn-by-turn thread."""
    store.create_conversation(conv_id=conv_id, persona_id=persona_id)
    for i, text in enumerate(user_messages):
        store.add_message(conv_id, "user", text)
        store.add_message(conv_id, "assistant", f"reply {i}")


class TestNotBeforeSecondMessage:
    """Short/trivial conversations keep their current title; the intelligent
    pass fires exactly once, at the 2nd user message — never again."""

    async def test_no_retitle_with_zero_or_one_user_messages(self, store, monkeypatch):
        mock_generate = AsyncMock(return_value="Some Title")
        monkeypatch.setattr(titler, "generate_text", mock_generate)
        monkeypatch.setattr(titler, "get_store", lambda: store)

        store.create_conversation(conv_id="empty")
        await titler._maybe_retitle("empty")
        mock_generate.assert_not_called()

        _seed(store, "one-turn", user_messages=["hi there"])
        await titler._maybe_retitle("one-turn")
        mock_generate.assert_not_called()
        assert store.get_conversation("one-turn").title == "New Conversation"

    async def test_retitles_at_exactly_two_user_messages(self, store, monkeypatch):
        mock_generate = AsyncMock(return_value="Weekend Trip Planning")
        monkeypatch.setattr(titler, "generate_text", mock_generate)
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["where should we go", "how about the coast"])

        await titler._maybe_retitle("c1")

        mock_generate.assert_awaited_once()
        assert store.get_conversation("c1").title == "Weekend Trip Planning"

    async def test_no_retitle_past_two_user_messages(self, store, monkeypatch):
        """The 3rd+ user message must not re-trigger — there's no rename
        feature to defend the title from being clobbered otherwise (see the
        module docstring), so "exactly once" is the whole guard."""
        mock_generate = AsyncMock(return_value="Should Not Be Used")
        monkeypatch.setattr(titler, "generate_text", mock_generate)
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["one", "two", "three"])

        await titler._maybe_retitle("c1")

        mock_generate.assert_not_called()
        assert store.get_conversation("c1").title == "New Conversation"

    async def test_prompt_includes_both_user_and_assistant_content(self, store, monkeypatch):
        mock_generate = AsyncMock(return_value="A Title")
        monkeypatch.setattr(titler, "generate_text", mock_generate)
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["plan a trip to the coast", "book the coastal inn"])

        await titler._maybe_retitle("c1")

        prompt = mock_generate.await_args.args[0]
        assert "plan a trip to the coast" in prompt
        assert "book the coastal inn" in prompt
        assert "reply 0" in prompt


class TestFailureSafety:
    async def test_llm_failure_keeps_existing_title_and_logs_once(self, store, monkeypatch, caplog):
        async def _boom(*args, **kwargs):
            raise RuntimeError("local LLM unavailable")

        monkeypatch.setattr(titler, "generate_text", _boom)
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["a", "b"])

        with caplog.at_level(logging.WARNING, logger=titler.logger.name):
            await titler._maybe_retitle("c1")

        assert store.get_conversation("c1").title == "New Conversation"
        failures = [r for r in caplog.records if "conversation titling failed" in r.message]
        assert len(failures) == 1  # logged once, no retry storm

    async def test_timeout_is_caught_like_any_other_failure(self, store, monkeypatch):
        async def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(titler, "generate_text", _timeout)
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["a", "b"])

        await titler._maybe_retitle("c1")  # must not raise

        assert store.get_conversation("c1").title == "New Conversation"

    async def test_empty_llm_response_keeps_existing_title(self, store, monkeypatch):
        monkeypatch.setattr(titler, "generate_text", AsyncMock(return_value="   "))
        monkeypatch.setattr(titler, "get_store", lambda: store)
        _seed(store, "c1", user_messages=["a", "b"])

        await titler._maybe_retitle("c1")

        assert store.get_conversation("c1").title == "New Conversation"

    async def test_missing_conversation_does_not_raise(self, store, monkeypatch):
        monkeypatch.setattr(titler, "get_store", lambda: store)
        await titler._maybe_retitle("does-not-exist")


class TestSanitizeTitle:
    @pytest.mark.parametrize("raw,expected", [
        ('"Weekend Getaway Plans"', "Weekend Getaway Plans"),
        ("**Budget Review**", "Budget Review"),
        ("Fixing the login bug.", "Fixing the login bug"),
        ("Trip   planning   for    June", "Trip planning for June"),
        ("`docker compose setup`", "docker compose setup"),
        ("Title: Weekend Trip Planning", "Weekend Trip Planning"),
        ("title - Budget Review", "Budget Review"),
        ("Refactor the parser?", "Refactor the parser"),
    ])
    def test_strips_quotes_markdown_and_trailing_punctuation(self, raw, expected):
        assert titler.sanitize_title(raw) == expected

    def test_truncates_to_max_length_at_word_boundary(self):
        raw = "This is a very long title that definitely exceeds the fifty character budget we allow"
        result = titler.sanitize_title(raw)
        assert len(result) <= titler.MAX_TITLE_LENGTH
        assert not result.endswith(" ")
        assert raw.startswith(result)  # truncation, not rewriting

    def test_empty_or_junk_input_returns_empty_string(self):
        assert titler.sanitize_title("") == ""
        assert titler.sanitize_title("   ") == ""
        assert titler.sanitize_title('""') == ""
        assert titler.sanitize_title(None) == ""


class TestScheduleRetitle:
    def test_noop_for_missing_conversation_id(self):
        titler.schedule_retitle(None)
        titler.schedule_retitle("")

    def test_noop_outside_a_running_event_loop(self):
        # Plain (non-async) test function -- no event loop exists here at
        # all, exercising the RuntimeError guard directly.
        titler.schedule_retitle("some-conv-id")  # must not raise

    async def test_schedules_a_background_task_inside_a_running_loop(self, monkeypatch):
        called = AsyncMock()
        monkeypatch.setattr(titler, "_maybe_retitle", called)

        titler.schedule_retitle("conv-1")
        await asyncio.sleep(0)  # let the scheduled task actually run

        called.assert_awaited_once_with("conv-1")
