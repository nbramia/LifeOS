"""Card-assignment plumbing shared by the executors and the worker (#851).

The Kanban board assigns a card to an engine (`claude`/`codex`/`local`/
`hermes` tags), a model, an effort level, and a host — written onto the
task as `[key:: value]` inline fields (round-tripped verbatim by
`TaskManager`/`Task.fields`, see docs/specs/technical/task-management.md).
This module has two small, independent jobs:

1. `extract_assignment()` — pull `model`/`effort`/`host`/`assigned_by` off
   `task["fields"]` into one typed shape, so `worker.py` doesn't repeat the
   same four `.get()` calls at every dispatch site.
2. Per-engine effort mapping — the board's effort vocabulary is exactly
   `low|medium|high|max`; each engine speaks a different vocabulary (or
   none at all), so `map_effort_for_engine()` translates once, in one
   place, instead of scattering the mapping across three executors.
"""
from __future__ import annotations

from dataclasses import dataclass


# The board's own effort vocabulary — the only values a card's `effort`
# field should ever carry. Anything else is treated as absent (no override)
# rather than raising, since a stray/legacy value must not crash dispatch.
BOARD_EFFORT_LEVELS = ("low", "medium", "high", "max")

# Claude Code CLI: `claude --effort <level>` accepts exactly these five
# (verified via `claude --help` on the operator's host). The board has no
# "xhigh" tier, so the mapping is 1:1 for the four it does have.
CLAUDE_CODE_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}

# Codex CLI: `-c model_reasoning_effort=<level>` accepts
# minimal|low|medium|high|xhigh. The board's "max" maps to Codex's ceiling,
# "xhigh" — there is no board tier above it to lose.
CODEX_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}

ENGINE_CLAUDE_CODE = "claude_code"
ENGINE_CODEX = "codex"
ENGINE_LOCAL = "local"
ENGINE_HERMES = "hermes"

_EFFORT_MAPS: dict[str, dict[str, str]] = {
    ENGINE_CLAUDE_CODE: CLAUDE_CODE_EFFORT_MAP,
    ENGINE_CODEX: CODEX_EFFORT_MAP,
}


def map_effort_for_engine(engine: str, effort: str | None) -> str | None:
    """Translate a board effort level to the engine's own flag value.

    Returns `None` when there's nothing to pass: `effort` unset/unrecognized,
    or `engine` doesn't take an effort flag at all (`local`, `hermes` — see
    `local_thinking_for_effort()` and the Hermes executor, which handle
    their own engines' effort semantics separately since neither is a CLI
    flag).
    """
    if not effort:
        return None
    mapping = _EFFORT_MAPS.get(engine)
    if mapping is None:
        return None
    return mapping.get(effort)


def local_thinking_for_effort(effort: str | None) -> bool | None:
    """Whether the local Gemma executor should turn thinking on for this
    session's effort level.

    `high`/`max` → True (thinking on). `low`/`medium` → False (thinking
    off). Anything else (unset, unrecognized) → `None`, meaning "no
    per-session override — fall back to `settings.local_agent_enable_thinking`".
    """
    if effort in ("high", "max"):
        return True
    if effort in ("low", "medium"):
        return False
    return None


@dataclass(frozen=True)
class Assignment:
    """The board-assignment fields extracted from one task, typed and
    defaulted so every caller reads the same shape."""

    model: str | None = None
    effort: str | None = None
    host: str | None = None
    assigned_by: str | None = None

    @property
    def is_board_assigned(self) -> bool:
        return (self.assigned_by or "").strip().lower() == "board"


def extract_assignment(fields: dict | None) -> Assignment:
    """Pull `model`/`effort`/`host`/`assigned_by` off a task's `fields` dict
    (`Task.fields`, round-tripped `[key:: value]` inline fields — see
    `docs/specs/technical/task-management.md`). Blank/whitespace-only values
    normalize to `None` so callers never have to special-case `""`.
    """
    fields = fields or {}

    def _clean(key: str) -> str | None:
        value = fields.get(key)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    effort = _clean("effort")
    if effort not in BOARD_EFFORT_LEVELS:
        effort = None

    return Assignment(
        model=_clean("model"),
        effort=effort,
        host=_clean("host"),
        assigned_by=_clean("assigned_by"),
    )
