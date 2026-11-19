"""Hand a Pebble capture addressed to a spoken phrase to a configured command.

A capture whose transcript opens with the operator's configured phrase (for
example "Assistant, ...") is a message for whoever that phrase names, not a
journal entry to classify. ``handoff_message`` recognizes the phrase the way
speech recognition and typing actually render it, and ``run_handoff`` pipes
the rest of the capture to the operator's command. LifeOS knows nothing about
the recipient: the command decides where the message goes.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

HANDOFF_TIMEOUT_SECONDS = 60

# Words a speaker may say before the phrase without changing who is addressed.
_FILLERS = (
    "um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "oh", "okay", "ok",
    "so", "hey", "hi", "hello", "yo", "alright", "all right", "well", "and",
)
_LEAD = r"""[\s"'“‘*_(\[]*"""
_PUNCT = r"[\s,.:;!?…—–-]*"
_WORD_SEPARATOR = r"[\s\-–—]+"


class PebbleHandoffError(RuntimeError):
    """The hand-off command did not accept the message."""


@lru_cache(maxsize=8)
def _phrase_pattern(phrase: str) -> Optional[re.Pattern[str]]:
    words = [w for w in (re.sub(r"^\W+|\W+$", "", part) for part in re.split(r"[\s\-–—]+", phrase)) if w]
    if not words:
        return None
    fillers = "|".join(
        _WORD_SEPARATOR.join(re.escape(part) for part in filler.split())
        for filler in sorted(_FILLERS, key=len, reverse=True)
    )
    # An apostrophe in the phrase ("Sam's helper") may be typed straight,
    # curly, or dropped by speech recognition.
    body = _WORD_SEPARATOR.join(
        "['’]?".join(re.escape(piece) for piece in re.split(r"['’]", word)) for word in words
    )
    return re.compile(
        rf"^{_LEAD}(?:(?:{fillers})\b{_PUNCT}){{0,3}}{body}"
        # A whole-word match that is not the possessive ("<phrase>'s desk").
        rf"(?!\w)(?![’']s\b){_PUNCT}(?P<rest>.*)\Z",
        re.IGNORECASE | re.DOTALL,
    )


def handoff_message(text: str, phrase: str) -> Optional[str]:
    """Return the message after ``phrase`` when ``text`` opens with it.

    Accepts any capitalization, hyphens for spaces ("chief-of-staff"), trailing
    punctuation after the phrase, and up to three leading filler words. A
    capture that only mentions the phrase later on, uses it as a possessive,
    or has nothing after it returns ``None`` and is filed as usual.
    """
    pattern = _phrase_pattern(phrase.strip())
    if pattern is None:
        return None
    match = pattern.match(text)
    if match is None:
        return None
    rest = match.group("rest").strip()
    return rest or None


@dataclass(frozen=True)
class PebbleHandoff:
    """The operator's phrase and the command that receives its messages."""

    phrase: str
    command: str

    def message(self, text: str) -> Optional[str]:
        return handoff_message(text, self.phrase)

    def send(self, message: str, *, recorded_at: str, key: str) -> None:
        run_handoff(self.command, message, recorded_at=recorded_at, key=key)


def run_handoff(command: str, message: str, *, recorded_at: str, key: str) -> None:
    """Pipe ``message`` to ``command`` on stdin; raise unless it exits 0.

    The command also gets ``LIFEOS_HANDOFF_RECORDED_AT`` (the capture's UTC
    timestamp) and ``LIFEOS_HANDOFF_KEY`` (stable per capture) in its
    environment. Its output is never logged: it may echo the message.
    """
    argv = shlex.split(command)
    if not argv:
        raise PebbleHandoffError("no hand-off command configured")
    env = {**os.environ, "LIFEOS_HANDOFF_RECORDED_AT": recorded_at, "LIFEOS_HANDOFF_KEY": key}
    try:
        result = subprocess.run(
            argv, input=message, text=True, env=env, capture_output=True,
            timeout=HANDOFF_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PebbleHandoffError(f"hand-off command failed to run ({type(exc).__name__})") from exc
    if result.returncode != 0:
        raise PebbleHandoffError(f"hand-off command exited {result.returncode}")


def configured_handoff(phrase: str, command: str) -> Optional[PebbleHandoff]:
    """Return the hand-off when both its phrase and its command are set."""
    if not phrase.strip() or not command.strip():
        return None
    if _phrase_pattern(phrase.strip()) is None:
        logger.warning("Pebble hand-off phrase has no words; hand-off disabled")
        return None
    return PebbleHandoff(phrase.strip(), command.strip())
