"""Golden-output test for the #591 build_system_prompt extraction.

#591 extracted the per-turn context assembly (date/time, the relative-time
instruction, existing task tags) out of `build_system_prompt` into the
standalone `build_turn_context`, so the same computation can also back a
read-only endpoint and the Hermes envelope. The acceptance criterion is that
the native system prompt is byte-identical before and after that extraction,
for a turn with a persona, a turn without one, a voice turn, and a turn with
existing task tags.

This test pins that promise against a baseline captured from the
pre-refactor code (see tests/fixtures/agent_system_prompt_golden_591.py) —
it does NOT re-derive the expected value from the current code, which would
prove nothing about whether the refactor changed anything.

**Why this test pins `_STATIC_PROMPT` and `settings.timezone` explicitly,
not just the clock:** `agent_system_prompt._STATIC_PROMPT` interpolates
`settings.user_name` and the configured Google accounts exactly ONCE, at
first import of the module — it is deliberately cached for the life of the
process (see the comment at its definition). A full pytest run imports many
files before this one; if any earlier-imported file triggers `api.main`'s
import (its `load_dotenv()` walks upward and can load a real, machine-
specific `.env` when cwd is nested under the real checkout, as a worktree
is), `settings.user_name` is real by the time `agent_system_prompt` is first
imported — and `_STATIC_PROMPT` bakes that in permanently for the rest of
the process. No fixture in THIS file can prevent that, because it already
happened during collection, before any fixture ran. So instead of trying to
control upstream import order, `pinned_static_prompt` below directly
overwrites the already-baked `asp._STATIC_PROMPT` with a value rebuilt from
the module's own (config-independent) `_STATIC_PROMPT_TEMPLATE` and pinned
synthetic inputs — neutralizing any contamination that already occurred,
regardless of cause. `settings.timezone` is read live (not cached) inside
`build_system_prompt`, so it's pinned via an ordinary monkeypatch.

Recapture recipe (only if the static prompt's *wording* is deliberately
changed — see the fixture module's docstring for the full rationale): pin
`LIFEOS_USER_NAME=Test User` and `LIFEOS_TIMEZONE=America/New_York` via
`os.environ` BEFORE importing anything from this repo, load the pre-change
`api/services/agent_system_prompt.py` as an isolated module (not the normal
package import, which would pick up post-change code), freeze the clock the
same way `frozen_clock` does below, and call `build_system_prompt()` for
each of the four cases.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from api.services import agent_system_prompt as asp
from api.services.task_manager import TaskManager
from config.settings import settings
from tests.fixtures import agent_system_prompt_golden_591 as golden

pytestmark = pytest.mark.unit

# Same frozen instant used to capture the baseline.
_FIXED_NOW = datetime(2026, 8, 19, 9, 14, 22, tzinfo=ZoneInfo("America/New_York"))


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW if tz is not None else _FIXED_NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin `datetime.now()` inside agent_system_prompt to the baseline instant."""
    monkeypatch.setattr(asp, "datetime", _FrozenDatetime)


@pytest.fixture
def pinned_static_prompt(monkeypatch):
    """Neutralize ambient contamination of the already-baked `_STATIC_PROMPT`
    module constant, and pin `settings.timezone` (read live, so an ordinary
    monkeypatch suffices there). See the module docstring above for why this
    is necessary and why a plain fixture on `settings.user_name` would be
    too late to matter.
    """
    pinned_static = asp._STATIC_PROMPT_TEMPLATE.format(
        name=golden.PINNED_NAME, google_accounts=golden.PINNED_ACCOUNTS,
    )
    monkeypatch.setattr(asp, "_STATIC_PROMPT", pinned_static)
    monkeypatch.setattr(settings, "timezone", golden.PINNED_TIMEZONE)


@pytest.fixture
def empty_tags(tmp_path, monkeypatch):
    """An empty, isolated task manager (no tags) — the no-tags baseline cases."""
    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    return manager


def test_static_block_unchanged(frozen_clock, pinned_static_prompt, empty_tags):
    """The cached static prompt itself is untouched by the extraction."""
    assert asp._STATIC_PROMPT == golden.STATIC_TEXT


def test_golden_with_persona(frozen_clock, pinned_static_prompt, empty_tags):
    prompt = asp.build_system_prompt(persona="FITNESS-PERSONA-MARKER: you are the fitness bot.")
    assert prompt[0]["text"] == golden.STATIC_TEXT
    assert prompt[1:] == golden.WITH_PERSONA_TAIL


def test_golden_without_persona(frozen_clock, pinned_static_prompt, empty_tags):
    prompt = asp.build_system_prompt()
    assert prompt[0]["text"] == golden.STATIC_TEXT
    assert prompt[1:] == golden.WITHOUT_PERSONA_TAIL


def test_golden_voice_turn(frozen_clock, pinned_static_prompt, empty_tags):
    prompt = asp.build_system_prompt(
        voice_rules=("Speak in short sentences.", "Never read a URL aloud.")
    )
    assert prompt[0]["text"] == golden.STATIC_TEXT
    assert prompt[1:] == golden.VOICE_TURN_TAIL


def test_golden_with_tags(frozen_clock, pinned_static_prompt, empty_tags):
    empty_tags.create("a", tags=["work", "urgent"])
    empty_tags.create("b", tags=["work"])
    prompt = asp.build_system_prompt()
    assert prompt[0]["text"] == golden.STATIC_TEXT
    assert prompt[1:] == golden.WITH_TAGS_TAIL
