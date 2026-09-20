"""Speculative fan-out for task routing: one Jev call per task answers
where it belongs, how hard it is, which preset class fits, and whether it's
software work.

`directory_resolver.resolve_working_directory`, `claude_code_spawn.
should_use_plan_mode`, and `preflight._apply_preset_class` each consume the
one answer relevant to them, falling back to their existing keyword/tag
logic when Jev isn't configured, the call fails, or the relevant answer's
confidence is too low. `judge_task` is memoized per exact title so the
several callers that ask about the same task in one dispatch share a single
Jev call instead of issuing one each.

Never logs the task title, or any other call content — only the exception
class name on failure.
"""
from __future__ import annotations

import logging
import math
from collections import OrderedDict
from dataclasses import dataclass

from api.services.jev_client import JevClient, JevError, jev_configured

logger = logging.getLogger(__name__)

# Per-title cache of successful judgments only — a title Jev failed to
# judge (unconfigured, rate-limited, transport error) is never memoized,
# so a later call for the same title gets a fresh attempt instead of being
# stuck replaying the earlier failure for the rest of the process's life.
# A plain `functools.lru_cache` can't express that (it caches every return
# value, `None` included), hence this small hand-rolled LRU.
_JUDGE_CACHE_MAXSIZE = 256
_judge_cache: "OrderedDict[str, TaskJudgment]" = OrderedDict()

# Score question criteria: five difficulty levels, in ascending order so a
# returned score can be compared numerically.
_DIFFICULTY_LEVELS = (
    "trivial lookup or one-line change",
    "small change in one file",
    "multi-file feature",
    "architectural or cross-cutting change",
    "unclear, needs research first",
)

# Preset-class choice criteria. Mirrors tool_filter._CLASS_SPECIALTIES'
# class names (not the tool lists themselves — those stay
# tool_filter's concern) with a short description of what belongs in each.
_PRESET_CLASS_CRITERIA: dict[str, str] = {
    "personal-comm": "Personal email, messaging, or calendar work (Gmail, iMessage, personal calendar)",
    "work-comm": "Work email, Slack, or work calendar/Drive",
    "research": "Open-ended research, memory search, or people/relationship lookups",
    "financial": "Budgets, transactions, accounts, cashflow",
    "crm": "CRM/people/interaction data",
    "fullstack": "General software work, or anything not clearly one of the above specialties",
}


@dataclass(frozen=True)
class JevAnswer:
    """One typed answer from a Jev question. Exactly one of `choice`/
    `score`/`noul` is populated, matching the question type asked. `noul`
    is a probability (0.0-1.0) that the stated proposition holds, not a
    boolean. `confidence` is 0.0 when Jev didn't return one (the `noul`
    type carries no confidence per the API contract)."""
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class TaskJudgment:
    """The fan-out answers for one task title. Any field may be `None`
    if Jev omitted that answer."""
    location: JevAnswer | None
    difficulty: JevAnswer | None
    preset_class: JevAnswer | None
    software_work: JevAnswer | None


def _location_criteria() -> dict[str, str]:
    """Location choice criteria: name -> description, built from
    `directory_resolver`'s option set (the operator's GitHub repos, scanned
    local projects, LifeOS, the vault, and home). Imported locally to avoid
    a module-load cycle — `directory_resolver.resolve_working_directory`
    imports `judge_task` from this module the same way."""
    from api.services.directory_resolver import _location_options

    return {name: description for name, description, _path in _location_options()}


def _valid_choice(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_number(value: object, *, lo: float | None = None, hi: float | None = None) -> float | None:
    """`value` as a finite float within `[lo, hi]`, or `None` — never
    raises. `bool` is deliberately excluded even though it's an `int`
    subclass (`float(True) == 1.0` would otherwise silently pass as a
    score/probability)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if lo is not None and number < lo:
        return None
    if hi is not None and number > hi:
        return None
    return number


def _parse_answer(raw: object) -> JevAnswer | None:
    """Validates every field independently — an invalid field becomes
    `None` on the resulting `JevAnswer`, never an exception. Consumers
    already treat a `None` field as "no judgment for this field" the same
    way they treat `judge_task` returning `None` outright."""
    if not isinstance(raw, dict):
        return None
    confidence = _valid_number(raw.get("confidence"), lo=0.0, hi=1.0)
    return JevAnswer(
        choice=_valid_choice(raw.get("choice")),
        score=_valid_number(raw.get("score")),
        noul=_valid_number(raw.get("noul"), lo=0.0, hi=1.0),
        confidence=confidence if confidence is not None else 0.0,
    )


def _cache_get(title: str) -> TaskJudgment | None:
    judgment = _judge_cache.get(title)
    if judgment is not None:
        _judge_cache.move_to_end(title)
    return judgment


def _cache_put(title: str, judgment: TaskJudgment) -> None:
    _judge_cache[title] = judgment
    _judge_cache.move_to_end(title)
    while len(_judge_cache) > _JUDGE_CACHE_MAXSIZE:
        _judge_cache.popitem(last=False)


def judge_task(title: str) -> TaskJudgment | None:
    """Ask Jev about `title`: which project/vault area it belongs to, how
    difficult it is, which preset class fits, and whether it's software
    work — in one call. Returns `None` when Jev isn't configured or the
    call raises for any reason; every caller falls back to its existing
    keyword/tag behavior in that case, so no dispatch ever fails because of
    this judgment.

    Memoized per exact title so the several callers that judge the same
    task title in one dispatch — working-directory resolution, plan-mode,
    and preset-class — share a single Jev call. Only a successful judgment
    is cached; a failed call is never memoized, so a later call for the
    same title gets a fresh attempt rather than replaying the failure.
    """
    cached = _cache_get(title)
    if cached is not None:
        return cached
    if not jev_configured():
        return None
    try:
        answers = JevClient().ask(
            {"task_title": title},
            {
                "location": {
                    "type": "choice",
                    "instructions": "Which project, repository, or vault area does this task belong to?",
                    "criteria": _location_criteria(),
                },
                "difficulty": {
                    "type": "score",
                    "instructions": "How difficult is this task to execute?",
                    "criteria": list(_DIFFICULTY_LEVELS),
                },
                "preset_class": {
                    "type": "choice",
                    "instructions": "Which specialty area best fits this task?",
                    "criteria": dict(_PRESET_CLASS_CRITERIA),
                },
                "software_work": {
                    "type": "noul",
                    "instructions": "this is a coding or software task",
                },
            },
        )
        judgment = TaskJudgment(
            location=_parse_answer(answers.get("location")),
            difficulty=_parse_answer(answers.get("difficulty")),
            preset_class=_parse_answer(answers.get("preset_class")),
            software_work=_parse_answer(answers.get("software_work")),
        )
    except JevError as exc:
        logger.warning("Jev task-routing judgment failed: %s", type(exc).__name__)
        return None
    except Exception as exc:  # noqa: BLE001 - a Jev failure must never fail a dispatch
        logger.warning("Jev task-routing judgment failed unexpectedly: %s", type(exc).__name__)
        return None

    _cache_put(title, judgment)
    return judgment
