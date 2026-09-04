"""Tests for #851's preflight surface: the new `#hermes` route/tag, and the
board-assignment routing-bypass acceptance criterion — "a task carrying
`[assigned_by:: board]` keeps its assignee tag's route".

On the bypass: `_apply_route_corroboration` already no-ops for ANY
recognized routing tag (`_has_route_override_tag`), and every tag branch in
`_apply_tag_overrides` (`#local`/`#claude`/`#codex`/`#hermes`/`#cloud*`)
returns immediately — nothing downstream ever re-examines a tagged route.
So a board-assigned card, which is always tagged with its assignee engine,
already can't be downgraded; these tests prove that invariant directly
(with `LIFEOS_AGENT_DEFAULT_ROUTE` configured to something else and a title
that names a DIFFERENT engine, so a corroboration bug would be caught) —
see the two `test_*_survives_default_route_and_uncorroborating_title` cases
below. `[assigned_by:: board]` itself never reaches `run_preflight` (it
isn't a tag), which is exactly why nothing about it needs threading through
preflight for the AC to hold: `worker.py` reads it from `task["fields"]`
via `assignment.extract_assignment()` for its own bookkeeping, and the tag
carries the routing.
"""
from __future__ import annotations

import json

import pytest

from api.services.agent_worker import preflight as pf


def _stub(reply: str):
    return lambda prompt: reply


def _golden_reply(**overrides) -> str:
    base = {
        "budget": {"wall_seconds": 14400, "max_tokens": 500000, "max_dollars": 5.0},
        "routing": "local",
        "routing_reason": "model's own guess",
        "routing_explicit": False,
        "expected_output": "text",
        "ambiguity": None,
        "sane": True,
        "sane_reason": "",
    }
    base.update(overrides)
    return json.dumps(base)


pytestmark = pytest.mark.unit


def test_hermes_tag_routes_to_hermes():
    result = pf.run_preflight(title="ask hermes about my schedule", tags=["agent", "hermes"], caller=_stub(_golden_reply()))
    assert result.routing == pf.ROUTE_HERMES
    assert result.routing_explicit is True


def test_hermes_is_a_known_route():
    assert pf.ROUTE_HERMES in pf.KNOWN_ROUTES


def test_claude_tag_survives_default_route_and_uncorroborating_title(monkeypatch):
    """AC: `[assigned_by:: board]` + `#claude` -> claude_code even with a
    title that names another engine. The default-route setting and an
    uncorroborating title are exactly the two levers that demote an
    LLM-guessed route (#757) — proving a `#claude`-tagged task survives
    both proves the tag itself, not luck, is what protects it."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "codex", raising=False)
    reply = _golden_reply(routing="local", routing_reason="model guessed local", routing_explicit=True)
    result = pf.run_preflight(
        title="run this with codex please",  # names a DIFFERENT engine than #claude
        tags=["agent", "claude"],
        caller=_stub(reply),
    )
    assert result.routing == pf.ROUTE_CLAUDE_CODE
    assert result.demoted_routing is None


def test_hermes_tag_survives_default_route_and_uncorroborating_title(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "local", raising=False)
    reply = _golden_reply(routing="local", routing_reason="model guessed local", routing_explicit=True)
    result = pf.run_preflight(
        title="use the local model for this",
        tags=["agent", "hermes"],
        caller=_stub(reply),
    )
    assert result.routing == pf.ROUTE_HERMES


def test_cloud_tag_still_yields_remote_route():
    """AC: `#cloud` still yields ROUTE_REMOTE — unaffected by the #851
    additions to `_apply_tag_overrides`/`KNOWN_ROUTES`."""
    result = pf.run_preflight(title="summarize this", tags=["agent", "cloud"], caller=_stub(_golden_reply()))
    assert result.routing == pf.ROUTE_REMOTE


def test_untagged_task_precedence_unchanged(monkeypatch):
    """AC: existing routing-tag precedence for tasks WITHOUT board
    assignment is unchanged — an untagged, uncorroborated LLM route still
    demotes to the configured default exactly as before #851."""
    from config.settings import settings
    monkeypatch.setattr(settings, "agent_default_route", "codex", raising=False)
    reply = _golden_reply(routing="local", routing_reason="model guessed local", routing_explicit=True)
    result = pf.run_preflight(title="just do the thing", tags=["agent"], caller=_stub(reply))
    assert result.routing == pf.ROUTE_CODEX
    assert result.demoted_routing == pf.ROUTE_LOCAL
