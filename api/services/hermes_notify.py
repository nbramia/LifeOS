"""Operator-facing delivery through the `hermes send` CLI.

A board card assigned to the Hermes engine runs inside Hermes, so its
operator-facing notices belong in the Hermes Telegram DM rather than on
LifeOS's own primary bot. `hermes send` is the purpose-built one-way pipe
for exactly that: it reuses the gateway's platform credentials
(`~/.hermes/.env` + `~/.hermes/config.yaml`), runs no LLM and no agent loop,
and needs no running gateway for a bot-token platform like Telegram.

Delivery is best-effort by construction, and every caller must treat it that
way. An install with no `hermes` binary, a CLI that exits non-zero, a
timeout, or output that isn't the documented JSON all resolve to `None`, and
the caller falls back to LifeOS's own Telegram channel. Shelling out couples
the API service to an external install; that is acceptable only on these
terms, because a task must still report somewhere.

The message body is piped on stdin rather than passed as an argument: a
notice can be long and carries arbitrary text, and stdin has neither an
argument-length ceiling nor any shell-quoting surface.

Nothing from the CLI's own output is logged. A failure log records the
outcome (a missing binary, an exit code, an exception type) and never the
message body or the delivery target, both of which are personal data.
"""
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# The value a session's `bot` column carries when its operator-facing
# messages belong on the Hermes channel rather than one of LifeOS's own
# Telegram bots. Deliberately outside the Telegram bot registry
# (`config/telegram_bots.json`), so a stale reader that routes it as a bot
# name resolves nothing and degrades to the primary bot.
HERMES_CHANNEL = "hermes"

# `hermes send --to <target>`; a bare platform name means that platform's
# home channel, which is the operator's own DM.
_DEFAULT_TARGET = "telegram"

# A send is a single bot-API call with no model behind it. A generous
# ceiling still bounds a wedged CLI well inside one worker tick.
_TIMEOUT_SECONDS = 30.0

_SEARCH_PATHS = (
    os.path.expanduser("~/.local/bin/hermes"),
    "/usr/local/bin/hermes",
    "/opt/homebrew/bin/hermes",
)

# Env prefixes stripped from the subprocess. `hermes send` needs no model
# credentials of its own, and the API service inherits LifeOS's `.env`
# (which carries `ANTHROPIC_API_KEY`) through systemd, so handing them to an
# external CLI would widen the blast radius for nothing.
_STRIPPED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE")


@dataclass(frozen=True)
class HermesDelivery:
    """The identity of one delivered message, as `hermes send --json`
    reports it. `chat_id` and `message_id` are opaque strings, matching the
    convention the `/api/hermes/*` contract already uses for Telegram ids —
    together they anchor a question so a threaded reply can be routed back
    to it."""

    chat_id: str
    message_id: str


def resolve_hermes_binary() -> Optional[str]:
    """The `hermes` executable, or `None` when this install has none."""
    found = shutil.which("hermes")
    if found:
        return found
    for path in _SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _clean_env() -> dict:
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith(_STRIPPED_ENV_PREFIXES)
    }


def send_via_hermes(
    text: str,
    *,
    target: str = _DEFAULT_TARGET,
    timeout: float = _TIMEOUT_SECONDS,
) -> Optional[HermesDelivery]:
    """Deliver `text` on the Hermes channel, returning the delivered
    message's identity — or `None` when this channel could not deliver it.

    `None` is an ordinary outcome, not an error: the caller is expected to
    fall back to LifeOS's own Telegram channel and log that it did.
    """
    binary = resolve_hermes_binary()
    if binary is None:
        logger.warning("hermes binary not found; the Hermes channel cannot deliver")
        return None
    try:
        completed = subprocess.run(
            [binary, "send", "--to", target, "--json"],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
    except Exception as exc:
        logger.warning("hermes send failed to run: %s", type(exc).__name__)
        return None
    if completed.returncode != 0:
        logger.warning("hermes send exited %s", completed.returncode)
        return None
    try:
        payload = json.loads(completed.stdout or "")
    except ValueError:
        logger.warning("hermes send returned output that is not the documented JSON")
        return None
    if not isinstance(payload, dict) or not payload.get("success"):
        logger.warning("hermes send reported an unsuccessful delivery")
        return None
    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    if chat_id is None or message_id is None:
        logger.warning("hermes send reported success without a message identity")
        return None
    return HermesDelivery(chat_id=str(chat_id), message_id=str(message_id))
