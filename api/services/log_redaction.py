"""Redaction of Telegram bot tokens from log records (#519).

The Telegram Bot API embeds the bot token directly in the request path
(``https://api.telegram.org/bot<digits>:<secret>/<method>``). ``httpx``'s
request logger defaults to INFO and logs the full URL of every request —
which is how a live, full-control credential ended up in plaintext in
``logs/server.log`` and ``logs/sync.log``.

This module provides two layers of defense:

1. ``silence_noisy_http_loggers()`` — the direct fix. Raises the relevant
   HTTP client loggers to WARNING so a *successful* send never logs the URL
   (and therefore the token) in the first place.
2. ``TelegramTokenRedactionFilter`` / ``install_telegram_redaction_filter()``
   — a defensive backstop. Installed on the root logger's handlers, it
   rewrites any log record whose rendered message contains a Telegram bot
   token shape, so a future library, log level change, or code path (e.g. an
   httpx exception whose string form embeds the request URL) can't
   reintroduce the leak even if the level-based fix above is bypassed.

Call ``configure_telegram_log_redaction()`` once per process, after the
process's own root logging setup (``logging.basicConfig`` or equivalent) has
run — it needs at least one handler already attached to root for the filter
to have something to attach to.
"""
from __future__ import annotations

import logging
import re

# Matches a Telegram bot token wherever it shows up in a message: the
# `bot<digits>:<secret>` path segment the Telegram Bot API uses (whether or
# not `api.telegram.org` precedes it — e.g. a stringified httpx exception
# may render just the path, or a relative URL). `[A-Za-z0-9_-]+` covers the
# base64url-ish token body Telegram issues.
TELEGRAM_TOKEN_PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

REDACTED_TOKEN = "bot<REDACTED>"

# Loggers that log full outbound request URLs at INFO by default. httpx logs
# "HTTP Request: <method> <url> ..." for every request it makes, and the
# Telegram Bot API puts the token in that URL.
_NOISY_HTTP_LOGGER_NAMES = ("httpx",)


class TelegramTokenRedactionFilter(logging.Filter):
    """Rewrites any log record whose message contains a Telegram bot token.

    Never drops a record — only redacts the token in place — so error paths
    stay visible. Safe to attach to a `Logger` or a `Handler`; attaching to
    handlers on the root logger is what lets it catch records that
    *propagate* up from other loggers (httpx, or anything not otherwise
    silenced), not just ones logged directly against the object it's on.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Don't let a malformed record (bad % args, etc.) break logging.
            return True

        if TELEGRAM_TOKEN_PATTERN.search(message):
            record.msg = TELEGRAM_TOKEN_PATTERN.sub(REDACTED_TOKEN, message)
            record.args = ()

        return True


def silence_noisy_http_loggers(logger_names=_NOISY_HTTP_LOGGER_NAMES) -> None:
    """Raise HTTP client request loggers to WARNING so successful requests
    stop logging their (token-bearing) URLs. Failures still log — this only
    raises the level of the *request* log line, not error handling."""
    for name in logger_names:
        logging.getLogger(name).setLevel(logging.WARNING)


def install_telegram_redaction_filter() -> None:
    """Install `TelegramTokenRedactionFilter` on the root logger and every
    handler currently attached to it. Idempotent."""
    root = logging.getLogger()
    filt = TelegramTokenRedactionFilter()

    if not any(isinstance(f, TelegramTokenRedactionFilter) for f in root.filters):
        root.addFilter(filt)

    for handler in root.handlers:
        if not any(isinstance(f, TelegramTokenRedactionFilter) for f in handler.filters):
            handler.addFilter(filt)


def configure_telegram_log_redaction() -> None:
    """Apply both defenses. Call once per process, after root logging is
    configured (i.e. after `logging.basicConfig(...)` has given root at
    least one handler)."""
    silence_noisy_http_loggers()
    install_telegram_redaction_filter()
