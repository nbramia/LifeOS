"""Tests for the Jev task-routing fan-out (api.services.jev_task_routing)."""
from __future__ import annotations

import os

import pytest

import api.services.jev_task_routing as jtr
from api.services.jev_client import JevClient, JevError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_judge_cache():
    """`judge_task`'s cache is a module-level dict — clear it before and
    after every test in this file so real (unmocked) calls made here never
    leak a cached judgment into another test in this file or any other."""
    jtr._judge_cache.clear()
    yield
    jtr._judge_cache.clear()


def test_no_key_returns_none_without_constructing_client(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "")

    def _boom_init(self, *a, **kw):
        raise AssertionError("JevClient must not be constructed with no key configured")

    monkeypatch.setattr(JevClient, "__init__", _boom_init)
    assert jtr.judge_task("jev-test-no-key") is None


@pytest.mark.unit
def test_failure_is_not_memoized_second_call_retries(monkeypatch):
    """Mutation check: caching `None` (e.g. reverting to a plain
    `functools.lru_cache` over the whole return value) makes this test
    fail — the second call would return `None` again without a second
    `JevClient.ask` invocation."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")

    calls = []

    def _ask(self, state, questions, *, model=None):
        calls.append(1)
        if len(calls) == 1:
            raise JevError("status 500")
        return {
            "location": {"choice": "home", "confidence": 0.9},
            "difficulty": {"score": 3.0, "confidence": 0.8},
            "preset_class": {"choice": "research", "confidence": 0.8},
            "software_work": {"noul": 0.2},
        }

    monkeypatch.setattr(JevClient, "ask", _ask)

    first = jtr.judge_task("jev-test-flaky-title")
    assert first is None
    second = jtr.judge_task("jev-test-flaky-title")
    assert second is not None
    assert second.location.choice == "home"
    assert len(calls) == 2  # both calls actually reached JevClient.ask


def test_success_is_memoized_second_call_shares_result(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")

    calls = []

    def _ask(self, state, questions, *, model=None):
        calls.append(1)
        return {
            "location": {"choice": "home", "confidence": 0.9},
            "difficulty": None,
            "preset_class": None,
            "software_work": None,
        }

    monkeypatch.setattr(JevClient, "ask", _ask)

    first = jtr.judge_task("jev-test-stable-title")
    second = jtr.judge_task("jev-test-stable-title")
    assert first is second  # same cached TaskJudgment instance
    assert len(calls) == 1


def test_invalid_answer_fields_degrade_to_none_not_an_exception(monkeypatch):
    """A malformed answer body (confidence as a non-numeric string, score
    as a word, an out-of-range/NaN value) must degrade each field to
    `None`, never raise — and the two real consumers (plan-mode,
    working-directory resolution) fall back cleanly rather than crashing
    the dispatch."""
    from config.settings import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")

    def _ask(self, state, questions, *, model=None):
        return {
            "location": {"choice": "widget", "confidence": "nan"},
            "difficulty": {"score": "high", "confidence": 0.9},
            "preset_class": {"choice": "crm", "confidence": float("nan")},
            "software_work": {"noul": "yes"},
        }

    monkeypatch.setattr(JevClient, "ask", _ask)

    judgment = jtr.judge_task("jev-test-malformed-answer")
    assert judgment is not None
    assert judgment.location.choice == "widget"  # the string field was still valid
    assert judgment.location.confidence == 0.0  # invalid confidence -> field default
    assert judgment.difficulty.score is None  # "high" is not a number
    assert judgment.preset_class.confidence == 0.0  # NaN is not finite
    assert judgment.software_work.noul is None  # "yes" is not a number

    from api.services.agent_worker.claude_code_spawn import should_use_plan_mode
    from api.services.directory_resolver import resolve_working_directory

    # Fresh, still-unique titles (own cache entries) so each consumer
    # exercises the real fallback path — no exception, no crash. Avoids
    # every word directory_resolver's/claude_code_spawn's own keyword
    # cascades match on (e.g. "test", "code"), so a pass here is actually
    # exercising the Jev-answer-rejected path, not a lucky keyword hit.
    assert should_use_plan_mode("jev-malformed-payload-zzq-planmode") is False
    assert (
        resolve_working_directory("jev-malformed-payload-zzq-workingdir")
        == os.path.expanduser("~")
    )


def test_bool_is_rejected_as_a_number():
    """`bool` is an `int` subclass — `float(True) == 1.0` must not
    silently pass as a valid score/probability/confidence."""
    assert jtr._valid_number(True) is None
    assert jtr._valid_number(False) is None


def test_number_range_and_finiteness():
    assert jtr._valid_number(0.5, lo=0.0, hi=1.0) == 0.5
    assert jtr._valid_number(1.5, lo=0.0, hi=1.0) is None
    assert jtr._valid_number(-0.1, lo=0.0, hi=1.0) is None
    assert jtr._valid_number(float("nan")) is None
    assert jtr._valid_number(float("inf")) is None
    assert jtr._valid_number("not a number") is None


def test_empty_or_non_string_choice_is_rejected():
    assert jtr._valid_choice("crm") == "crm"
    assert jtr._valid_choice("") is None
    assert jtr._valid_choice("   ") is None
    assert jtr._valid_choice(None) is None
    assert jtr._valid_choice(5) is None
