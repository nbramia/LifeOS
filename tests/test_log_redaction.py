"""Tests for Telegram bot token log redaction (#519).

The Telegram Bot API embeds the bot token in the request URL
(`api.telegram.org/bot<digits>:<secret>/<method>`), and httpx's request
logger defaults to INFO and logs that full URL. These tests use an obviously
fake token — never a real one.
"""
import logging

import pytest

pytestmark = pytest.mark.unit

FAKE_TOKEN = "bot123456:FAKE_TOKEN_FOR_TESTS"


def _make_record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestTelegramTokenRedactionFilter:
    def test_redacts_telegram_request_url(self):
        from api.services.log_redaction import (
            TelegramTokenRedactionFilter,
            REDACTED_TOKEN,
        )

        record = _make_record(
            f'HTTP Request: POST https://api.telegram.org/{FAKE_TOKEN}/sendMessage "HTTP/1.1 200 OK"'
        )
        result = TelegramTokenRedactionFilter().filter(record)

        assert result is True  # never drops the record
        message = record.getMessage()
        assert FAKE_TOKEN not in message
        assert "FAKE_TOKEN_FOR_TESTS" not in message
        assert REDACTED_TOKEN in message

    def test_redacts_bare_bot_token_pattern_anywhere(self):
        """Generic `bot\\d+:secret` shape, not just the full telegram.org URL —
        e.g. what a stringified httpx exception might embed."""
        from api.services.log_redaction import (
            TelegramTokenRedactionFilter,
            REDACTED_TOKEN,
        )

        record = _make_record(f"getUpdates error: connection to {FAKE_TOKEN} timed out")
        TelegramTokenRedactionFilter().filter(record)

        message = record.getMessage()
        assert FAKE_TOKEN not in message
        assert REDACTED_TOKEN in message

    def test_leaves_unrelated_messages_untouched(self):
        from api.services.log_redaction import TelegramTokenRedactionFilter

        original = "Calendar sync scheduler started (times: 08:00, 12:00, 15:00 America/New_York)"
        record = _make_record(original)
        result = TelegramTokenRedactionFilter().filter(record)

        assert result is True
        assert record.getMessage() == original

    def test_leaves_unrelated_messages_with_args_untouched(self):
        """Records logged with %-args (not pre-formatted f-strings) should be
        unaffected when there's no token to redact."""
        from api.services.log_redaction import TelegramTokenRedactionFilter

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Restored update offset for '%s': %s",
            args=("primary", 42),
            exc_info=None,
        )
        TelegramTokenRedactionFilter().filter(record)
        assert record.getMessage() == "Restored update offset for 'primary': 42"

    def test_does_not_raise_on_bad_percent_args(self):
        """A record whose msg/args can't be formatted shouldn't crash filtering."""
        from api.services.log_redaction import TelegramTokenRedactionFilter

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%s %s",
            args=("only one",),
            exc_info=None,
        )
        result = TelegramTokenRedactionFilter().filter(record)
        assert result is True


class TestConfigureTelegramLogRedaction:
    def setup_method(self, method):
        self._root = logging.getLogger()
        self._root_filters_before = list(self._root.filters)

    def teardown_method(self, method):
        # Reset httpx logger level and any root-logger filters this test
        # installed, so tests don't leak global logging state into each other.
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        self._root.filters = self._root_filters_before

    def test_httpx_logger_raised_to_warning(self):
        from api.services.log_redaction import configure_telegram_log_redaction

        # Simulate an app that (like real callers) sets httpx to INFO
        # somewhere along the way, e.g. via a root-level basicConfig whose
        # effective level httpx inherits.
        logging.getLogger("httpx").setLevel(logging.INFO)

        configure_telegram_log_redaction()

        assert logging.getLogger("httpx").level == logging.WARNING

    def test_installs_redaction_filter_on_root_handlers(self):
        from api.services.log_redaction import (
            configure_telegram_log_redaction,
            TelegramTokenRedactionFilter,
        )

        handler = logging.StreamHandler()
        self._root.addHandler(handler)
        try:
            configure_telegram_log_redaction()
            assert any(
                isinstance(f, TelegramTokenRedactionFilter) for f in handler.filters
            )
        finally:
            self._root.removeHandler(handler)

    def test_idempotent_does_not_duplicate_filter(self):
        from api.services.log_redaction import (
            configure_telegram_log_redaction,
            TelegramTokenRedactionFilter,
        )

        handler = logging.StreamHandler()
        self._root.addHandler(handler)
        try:
            configure_telegram_log_redaction()
            configure_telegram_log_redaction()
            count = sum(
                1 for f in handler.filters if isinstance(f, TelegramTokenRedactionFilter)
            )
            assert count == 1
        finally:
            self._root.removeHandler(handler)
