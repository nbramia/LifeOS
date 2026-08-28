"""Keyless-install regression matrix (#788).

Running with no model-provider key, no reachable local model server, and no
#699 remote provider configured is a documented, supported way to run this
system -- but every failure that shape has produced (#697, #704, #706, #716,
#787) was found live by a real person, fixed one at a time, with nothing
standing guard against the next one in the same shape. This module is that
guard: a reusable fully-keyless fixture, plus one regression assertion per
affected path, so a future path hit by the same "no key, no local model"
gap can be added here instead of discovered live again.

Fixture design avoids two hazards named in #689/#755:

- Never reloads `config.settings` (which splits the singleton -- #689).
  Every module under test did `from config.settings import settings` at
  its own import time, binding its own name to the *same* underlying
  `Settings()` object (`config/settings.py`'s module-level `settings =
  Settings()`, instantiated exactly once). Swapping in a whole new
  `Settings(...)` instance the way `tests/conftest.py`'s `mock_settings`
  does would only be visible to code that re-fetches
  `config.settings.settings` fresh -- not to a module's already-bound
  name. Patching *attributes* on the shared object instead is visible
  from every module's binding without needing a fresh import anywhere.
- Never reads an ambient environment variable / real `.env` (#755) --
  every setting this matrix cares about is pinned explicitly via
  `monkeypatch.setattr`, never left to whatever the process environment
  happens to contain.

No test here requires network access, a real credential, a GPU, a running
server, or writes to a real database.

Status per motivating bug, as of this write (landed = already true on
`origin/feat/oss-portability-audit`, confirmed by reading the code, not
by GitHub issue state -- an issue closes on merge to `main`, and these
land on the integration branch first):

- #697 (health honesty)              -- landed  -> real assertion
- #704 (preflight doesn't raise)     -- landed  -> real assertion
- #706 (LocalLLMClient /v1 doubling) -- landed  -> real assertion
- #716 (titling doesn't raise)       -- N/A     -> real assertion (see below)
- #787 (chat omits raw exception)    -- landed  -> real assertion (promoted from
                                                    an xfail(strict=True) placeholder)

#716's own acceptance criteria is broader than what's tested here (a
shared local-or-remote fallback resolver, tracked by #773, so titling can
actually *succeed* on a remote-only or Anthropic-only install instead of
only ever trying the local server) -- that part is still open and out of
this matrix's scope. What #788 itself asks for regarding titling is
narrower: that a keyless install's titling failure doesn't raise and
leaves the placeholder title in place. That guarantee already holds today
(the broad `except Exception` in `_maybe_retitle`), so it's written below
as a real, currently-passing assertion rather than a placeholder.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def keyless_settings(monkeypatch):
    """Patch the shared settings singleton into a fully keyless shape.

    No Anthropic key, no reachable local model server (the *setting* that
    would point at one is pinned to a bogus URL; reachability itself is
    mocked per-test rather than dialed for real), no #699 remote provider,
    no Telegram. Returns the shared object so a test can read from it if
    needed.
    """
    from config.settings import settings

    patches = {
        "anthropic_api_key": "",
        "local_llm_url": "http://127.0.0.1:1",  # never actually dialed
        "llm_backend": "anthropic",
        "agent_remote_executor": False,
        "remote_llm_base_url": "",
        "remote_llm_model": "",
        "remote_llm_api_key": "",
        "agent_default_route": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }
    for name, value in patches.items():
        # raising=True (default): a typo'd/renamed field must fail loudly,
        # not silently no-op while the singleton keeps whatever value it
        # picked up at import time (possibly from a real ambient .env/
        # environment variable -- exactly the #755 hazard this fixture
        # exists to avoid).
        monkeypatch.setattr(settings, name, value)

    # Belt-and-suspenders against the same hazard from the other direction:
    # nothing here should matter given the settings patches above (no code
    # path this matrix exercises reads these env vars directly instead of
    # going through `settings`), but scrub them anyway so a provider SDK's
    # own env-var fallback can never smuggle a real credential into a test.
    for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    return settings


class TestHealthReportsHonestly:
    """#697 (landed) -- `api_key_configured` reflects whether an Anthropic
    key is actually set, not merely whether `local_llm_url` (which has a
    non-empty default regardless of backend) happens to be a non-empty
    string."""

    def test_api_key_configured_false_when_keyless(self, keyless_settings):
        from fastapi.testclient import TestClient

        from api.main import app

        client = TestClient(app)
        response = client.get("/health")
        data = response.json()
        assert data["checks"]["api_key_configured"] is False
        assert data["status"] == "degraded"


class TestPreflightDegradesInsteadOfRaising:
    """#704 (landed) -- with no Anthropic key, no reachable local server,
    and #699's remote provider unconfigured, `_default_llm_caller`'s final
    `raise RuntimeError(...)` used to propagate straight out of
    `run_preflight`. It's now caught by `run_preflight`'s own
    except-clause, which resolves to the same fail-closed
    sane=False/sane_fatal=True/routing=ask result any other preflight
    error produces -- never an unhandled exception reaching the worker."""

    def test_default_caller_degrades_to_ask_not_an_exception(self, keyless_settings):
        from api.services.agent_worker import preflight as pf
        from api.services.llm_client import LocalLLMClient

        with patch.object(LocalLLMClient, "is_available", return_value=False):
            result = pf.run_preflight(title="build a feature", tags=["agent"])

        assert result.sane is False
        assert result.sane_fatal is True
        assert result.routing == pf.ROUTE_ASK


class TestRemoteClientDoesNotDoubleV1:
    """#706 (landed) -- an OpenAI-compatible remote base URL that already
    ends in `/v1` (the documented convention for these providers, e.g. the
    #699 remote-executor path on a keyless install) must not be doubled
    into `.../v1/v1/chat/completions` once call sites append their own
    `/v1/chat/completions` suffix."""

    def test_base_url_ending_in_v1_is_stripped_once(self):
        from api.services.llm_client import LocalLLMClient

        client = LocalLLMClient(
            base_url="https://api.example.com/v1", model="some-remote-model", api_key="k",
        )
        assert client.base_url == "https://api.example.com"

    def test_base_url_without_v1_is_unaffected(self):
        from api.services.llm_client import LocalLLMClient

        client = LocalLLMClient(base_url="http://localhost:8080")
        assert client.base_url == "http://localhost:8080"


class _FakeTitlerMessage:
    def __init__(self, role: str, content: str = "hello"):
        self.role = role
        # `format_conversation_history` (conversation_store.py) reads
        # `.content` on every message before `_maybe_retitle` ever calls
        # `generate_text` -- a message missing it would raise inside that
        # helper, get caught by the same broad `except Exception`, and make
        # this test pass without ever reaching the code path it's meant to
        # exercise. Codex review flagged exactly this vacuous-pass risk.
        self.content = content


class _FakeTitlerStore:
    def __init__(self, messages):
        self._messages = messages
        self.update_title_calls: list[tuple[str, str]] = []

    def get_messages(self, conversation_id):
        return self._messages

    def update_title(self, conversation_id, title):
        self.update_title_calls.append((conversation_id, title))
        return True


class TestTitlingDoesNotRaiseWhenNoModelIsUsable:
    """#716's narrow slice that #788 actually asks for: on a keyless
    install where the titler's local-only call fails, the existing
    placeholder title is left in place without raising -- not "titling
    succeeds via some fallback" (that's #773's shared resolver, still
    open, out of scope here)."""

    @pytest.mark.asyncio
    async def test_maybe_retitle_swallows_unreachable_model_error(self, keyless_settings, monkeypatch):
        from api.services import conversation_titler as titler_mod

        store = _FakeTitlerStore([
            _FakeTitlerMessage("user"),
            _FakeTitlerMessage("assistant"),
            _FakeTitlerMessage("user"),
        ])
        monkeypatch.setattr(titler_mod, "get_store", lambda: store)

        calls = []

        async def _unreachable(*args, **kwargs):
            calls.append((args, kwargs))
            raise ConnectionError("[Errno 111] Connection refused")

        monkeypatch.setattr(titler_mod, "generate_text", _unreachable)

        # Must not raise -- the placeholder title stays untouched.
        await titler_mod._maybe_retitle("conv-1")

        # Guards against the test passing vacuously (e.g. an earlier
        # exception -- a malformed fake message, a missing attribute --
        # getting caught by the same broad `except Exception` before
        # `generate_text` is ever reached): confirm the unreachable-model
        # branch is the one that actually fired.
        assert calls, "generate_text was never called -- test would pass for the wrong reason"
        assert store.update_title_calls == []


class TestChatErrorMessageOmitsRawException:
    """#787 (landed) -- when a chat turn's model call exhausts its retries,
    `agent_loop.py`'s round-loop fatal branch used to interpolate the raw
    exception straight into the user-facing text
    (`f"Sorry, I encountered an error: {e}"`). On a keyless install that
    read like an SDK's own internal message, not a plain "this isn't set
    up yet" signal. #787 replaced it with fixed, generic text; this is now
    a normal always-on regression test rather than the xfail(strict=True)
    placeholder it started as.
    """

    @pytest.mark.asyncio
    async def test_fatal_round_error_omits_raw_exception_text(self, keyless_settings):
        from api.services import agent_loop

        secret_detail = "sk-ant-totallyFakeTestKey0000"

        class _FailingClient:
            model = "local"

            async def astream(self, *args, **kwargs):
                raise RuntimeError(f"upstream 401: invalid_api_key {secret_detail}")
                yield  # pragma: no cover -- unreachable; keeps this an async generator

        with patch.object(agent_loop, "_select_client", return_value=_FailingClient()):
            events = [e async for e in agent_loop.run_agent_loop("hello", max_tool_rounds=1)]

        text = "".join(e.get("content", "") for e in events if e["type"] == "text")
        assert secret_detail not in text
