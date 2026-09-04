"""Redaction of secrets and personal data from log records (#519, #904).

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

A third, unrelated filter lives here too for the same reason: uvicorn's own
access logger (``uvicorn.access``) writes the full request line — path *and*
query string — for every request, at INFO, regardless of what any route
handler logs. `#904 <https://github.com/nbramia/LifeOS/issues/904>`_ fixed
the CRM people-list handler's own log line, but the raw text typed into the
CRM search box (``GET /api/crm/people?q=<text>``) still reached
``logs/server.log`` this way — a gap the handler-level fix structurally
cannot close, since it only ever sees the parsed query parameter, never the
access logger's own line. ``RequestQueryStringRedactionFilter`` /
``install_query_string_redaction_filter()`` strip everything from the first
``?`` onward in that line, for every route, not just this one.
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


# Matches the query string (and the `?` that introduces it) in a request
# line, e.g. "GET /api/crm/people?q=alex&limit=5 HTTP/1.1" -- everything from
# `?` up to the next whitespace (the URL itself never contains a literal,
# unencoded space or `?`).
QUERY_STRING_PATTERN = re.compile(r"\?\S*")

REDACTED_QUERY_STRING = "?<redacted>"


class RequestQueryStringRedactionFilter(logging.Filter):
    """Strips the query string from uvicorn's access-log request line.

    uvicorn's access logger (``uvicorn.access``) logs the full request line
    -- including the raw query string -- for every request, independent of
    anything a route handler itself logs. That's how `q=<search text>` from
    the CRM people list's search box (personal data: names, partial emails)
    reaches `logs/server.log` even after the handler's own log line (#904)
    stops naming it. Never drops a record, only redacts in place.

    #907 review round 2: ``uvicorn.logging.AccessFormatter.formatMessage``
    does not call ``record.getMessage()`` at all -- it unconditionally
    unpacks ``record.args`` as a 5-tuple
    (``client_addr, method, full_path, http_version, status_code``) and
    rebuilds the request line from those fields. An earlier version of this
    filter rewrote ``record.msg`` to the redacted line and cleared
    ``record.args`` to make ``getMessage()`` return the safe string -- which
    is exactly right for a *generic* ``logging.Formatter``, but broke
    uvicorn's formatter: unpacking ``()`` into five names raises
    ``ValueError: not enough values to unpack``, and that exception, not an
    access line, is what actually reached ``logs/server.log`` (as a 79-line
    "Logging error" traceback per request). The fix has to redact
    ``args[2]`` (the piece the formatter actually reads) in place and leave
    the 5-tuple shape intact. The ``record.msg``-based rewrite is kept only
    as a fallback for a record that isn't shaped like uvicorn's own
    access-log call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str) and "?" in args[2]:
            path, _, _query = args[2].partition("?")
            record.args = (args[0], args[1], path + REDACTED_QUERY_STRING, args[3], args[4])
            return True

        # Fallback: a record not shaped like uvicorn's access-log call (that
        # call always hits the branch above). Kept belt-and-braces for any
        # other caller of this filter, formatted the ordinary way via
        # record.getMessage()/record.msg.
        try:
            message = record.getMessage()
        except Exception:
            # Don't let a malformed record (bad % args, etc.) break logging.
            return True

        if "?" in message:
            record.msg = QUERY_STRING_PATTERN.sub(REDACTED_QUERY_STRING, message, count=1)
            record.args = ()

        return True


def install_query_string_redaction_filter() -> None:
    """Install `RequestQueryStringRedactionFilter` directly on the
    `uvicorn.access` logger. Idempotent.

    Not attached via the root logger's handlers (the pattern the Telegram
    filter above uses): uvicorn's default logging config gives
    `uvicorn.access` `propagate=False` and its own dedicated handler, so a
    record logged there never reaches root's handlers at all -- the filter
    has to sit on the logger itself to see it.

    Call this after `uvicorn.access` has already been configured (uvicorn's
    own `Config.configure_logging()` runs `logging.config.dictConfig()`,
    which resets that logger's filters). In the normal startup path
    (`uvicorn.run("api.main:app", ...)`, as `scripts/server.sh` uses), that
    dictConfig call happens during `Config.__init__`, strictly before the
    app string is imported and this module's own `uvicorn.access` logger's
    filters would be reset -- so this is safe to call at import time here.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RequestQueryStringRedactionFilter) for f in access_logger.filters):
        access_logger.addFilter(RequestQueryStringRedactionFilter())
