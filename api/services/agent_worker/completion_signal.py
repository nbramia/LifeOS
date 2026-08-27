"""Deterministic "did this CLI session actually finish" signal (#760).

A ``claude_code``/``codex`` session ends when its subprocess exits. A clean
exit code — or even a terminal-looking stream event — says nothing about
whether the agent actually finished the work: a session can hit
``--max-turns``, get killed for budget, or otherwise stop mid-thought and
still exit 0 with a degenerate terminal event. Treating every such exit as
``completed`` hides interruptions behind ``#agent-completed`` (see the
``sess_099c0b8ca254486f`` field case: final text was a 64-char mid-thought
fragment, zero notifications sent, no PR opened — yet tagged completed).

This module defines what counts as an EARNED completion signal, cheaply and
without an LLM judge. The caller (worker.py's CLI dispatch) applies this only
to a nominal ``STATUS_COMPLETED`` outcome from the executor; it composes with
— does not replace — the empty-result/no-side-effect-tool-use guard in
``Worker._handle_outcome``, which serves the local/managed routes.
"""
from __future__ import annotations

import re

# A URL is the strong signal — unambiguous evidence a PR/issue was actually
# opened. A bare "#123" is kept conservative: it only counts alongside
# merge/PR-ish phrasing nearby, so a passing mention of an issue number isn't
# mistaken for "I opened/merged it".
_PR_URL_RE = re.compile(r"github\.com/\S+/(?:pull|issues)/\d+", re.IGNORECASE)
_PR_MENTION_RE = re.compile(
    r"\b(?:PR|pull request|merged?|opened|closes?|fixes?|resolves?)\b[^\n]{0,40}#\d+",
    re.IGNORECASE,
)

# A final text ending on one of these — as its last non-space character, or
# as its last word — reads as a sentence that got cut off rather than a
# finished thought (e.g. the field fixture: "...drop the no-longer-needed
# release:").
_DANGLING_TRAILING_CHARS = frozenset(":,;-–—")
_DANGLING_TRAILING_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "so", "to", "with", "for", "from",
    "that", "which", "of", "in", "on", "at", "as", "if", "because", "when",
    "while",
})

# Below this, even a clean-looking final text is too thin to trust as a real
# summary (a stray "Done." shouldn't earn completion on its own).
_MIN_SUMMARY_CHARS = 20


def _looks_like_summary(text: str) -> bool:
    """Cheap, deterministic check — NOT an LLM judgement. Fails closed:
    anything that doesn't clearly look like a finished thought is treated as
    a fragment."""
    if len(text) < _MIN_SUMMARY_CHARS:
        return False
    if text[-1] in _DANGLING_TRAILING_CHARS:
        return False
    words = text.split()
    if not words:
        return False
    last_word = re.sub(r"[^\w'-]", "", words[-1]).lower()
    if last_word in _DANGLING_TRAILING_WORDS:
        return False
    return True


def has_positive_completion_signal(final_text: str | None, notifications_sent: int) -> bool:
    """True iff a ``claude_code``/``codex`` session earned a ``completed``
    status.

    Positive signals, any one of which is sufficient:
      - at least one ``[NOTIFY]`` was sent during the run (Claude Code's
        system prompt instructs "Completion summaries (ALWAYS include one
        when done)", so a real completion nearly always has one; Codex has
        no notify convention and always passes 0 here, falling through to
        the other two signals);
      - the final text references a PR/issue (a URL is decisive; a bare
        ``#123`` only counts alongside merge/PR-ish phrasing);
      - the final text reads like a finished summary rather than an
        instruction fragment to itself.

    A subprocess exiting cleanly is necessary but not sufficient — see the
    module docstring.
    """
    if notifications_sent > 0:
        return True
    text = (final_text or "").strip()
    if not text:
        return False
    if _PR_URL_RE.search(text) or _PR_MENTION_RE.search(text):
        return True
    return _looks_like_summary(text)
