"""Tests for the per-surface live model readout (api/services/model_readout.py, #658).

The local-LLM probe hits an OpenAI-compatible `/v1/models` shape, stubbed via
ASGITransport (no sockets) — mirroring the pattern tests/test_hermes_proxy.py
and tests/test_agent_proxy.py already use for stubbing httpx calls to an
external backend. Hermes chat is read from an in-memory "last observed turn"
cache instead (#658 review: the configured Hermes URL is LifeOS's own
adapter, not the Hermes gateway, so there is no `/v1/models` to probe there —
and even if there were, a capability probe can't reflect a per-turn model
override the way an actually-observed turn can) — those tests call
`record_hermes_chat_turn_model()` directly, the same entry point
`api/routes/hermes_proxy.py` calls from a real turn's `usage` event.
"""

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.services import model_readout as mr

pytestmark = pytest.mark.unit

stub_models = FastAPI()
_state = {"model_id": "some-model-v1"}


@stub_models.get("/v1/models")
async def _stub_models(request: Request):
    return JSONResponse({"object": "list", "data": [{"id": _state["model_id"]}]})


@pytest.fixture
def stub_client(monkeypatch):
    """Route mr._client() through the in-process stub instead of a real socket."""
    _state["model_id"] = "some-model-v1"

    def _client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub_models), base_url="http://stub"
        )

    monkeypatch.setattr(mr, "_client", _client)
    return _state


class _FailingClient:
    """httpx.AsyncClient stand-in whose every request raises — simulates an
    unreachable backend (connection refused / DNS failure / timeout)."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("connection refused")


@pytest.fixture(autouse=True)
def _reset_hermes_observation():
    """The observed-model cache is a module-level singleton (by design —
    see model_readout.py) so it must be reset between tests to keep them
    independent."""
    mr._hermes_chat_last_model = None
    mr._hermes_chat_last_observed_at = None
    yield
    mr._hermes_chat_last_model = None
    mr._hermes_chat_last_observed_at = None


# --- LifeOS native picker -----------------------------------------------

async def test_native_anthropic_backend_reads_live_settings(monkeypatch):
    """Anthropic backend: the configured model IS the live value — no probe."""
    monkeypatch.setattr(mr.settings, "llm_backend", "anthropic")
    monkeypatch.setattr(mr.settings, "anthropic_model", "claude-haiku-4-5")
    result = await mr.get_lifeos_native_model()
    assert result == {"status": "ok", "backend": "anthropic", "model": "claude-haiku-4-5"}


async def test_native_local_backend_probes_live_server(monkeypatch, stub_client):
    monkeypatch.setattr(mr.settings, "llm_backend", "local")
    monkeypatch.setattr(mr.settings, "local_llm_url", "http://stub")
    stub_client["model_id"] = "gemma-4-26b-a4b"

    result = await mr.get_lifeos_native_model()

    assert result == {"status": "ok", "backend": "local", "model": "gemma-4-26b-a4b"}


async def test_native_local_backend_reflects_a_changed_model(monkeypatch, stub_client):
    """A model swap on the live local server is reflected without any code
    or config change on the LifeOS side."""
    monkeypatch.setattr(mr.settings, "llm_backend", "local")
    monkeypatch.setattr(mr.settings, "local_llm_url", "http://stub")

    stub_client["model_id"] = "gemma-4-26b-a4b"
    first = await mr.get_lifeos_native_model()
    assert first["model"] == "gemma-4-26b-a4b"

    stub_client["model_id"] = "gemma-4-26b-a4b-q8"
    second = await mr.get_lifeos_native_model()
    assert second["model"] == "gemma-4-26b-a4b-q8"


async def test_native_local_backend_unreachable_reports_unknown_not_configured_value(monkeypatch):
    """The configured LIFEOS_LLM_MODEL must never stand in for a value that
    couldn't actually be confirmed live — that's the exact hermes#49 mistake."""
    monkeypatch.setattr(mr.settings, "llm_backend", "local")
    monkeypatch.setattr(mr.settings, "local_llm_url", "http://stub-down")
    monkeypatch.setattr(mr.settings, "local_llm_model", "configured-model-should-not-appear")
    monkeypatch.setattr(mr, "_client", lambda: _FailingClient())

    result = await mr.get_lifeos_native_model()

    assert result["status"] == "unknown"
    assert result["model"] is None
    assert "configured-model-should-not-appear" not in str(result)


# --- Hermes chat / Hermes Telegram ---------------------------------------

async def test_hermes_not_configured(monkeypatch):
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "")
    result = await mr.get_hermes_models()
    assert result["hermes_chat"] == {"status": "not_configured", "model": None}
    assert result["hermes_telegram"]["status"] == "not_configured"
    assert result["hermes_telegram"]["model"] is None


async def test_hermes_chat_unknown_before_any_observed_turn(monkeypatch):
    """Configured but no turn has been relayed yet in this process — must
    not be confused with, or fall back to, anything configured."""
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    result = await mr.get_hermes_models()
    assert result["hermes_chat"] == {"status": "unknown", "model": None}


async def test_hermes_chat_reports_the_last_observed_turn_model(monkeypatch):
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    mr.record_hermes_chat_turn_model("accounts/fireworks/models/deepseek-v4-flash-0731")

    result = await mr.get_hermes_models()

    assert result["hermes_chat"]["status"] == "ok"
    assert result["hermes_chat"]["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert result["hermes_chat"]["observed_at"]  # a timestamp is present


async def test_hermes_chat_reflects_a_changed_model(monkeypatch):
    """A later turn served by a different model overwrites the observation
    — this is exactly the per-turn override the adapter's model_hint makes
    possible, and the readout must track it, not the first thing it saw."""
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")

    mr.record_hermes_chat_turn_model("model-a")
    first = await mr.get_hermes_models()
    assert first["hermes_chat"]["model"] == "model-a"

    mr.record_hermes_chat_turn_model("model-b")
    second = await mr.get_hermes_models()
    assert second["hermes_chat"]["model"] == "model-b"


async def test_hermes_telegram_is_never_reported_as_matching_hermes_chat(monkeypatch):
    """The two surfaces can genuinely differ (adapter model_hint vs the
    gateway's own Telegram-served model) — telegram must never borrow
    chat's observed value."""
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    mr.record_hermes_chat_turn_model("accounts/fireworks/models/deepseek-v4-flash-0731")

    result = await mr.get_hermes_models()

    assert result["hermes_chat"]["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert result["hermes_telegram"]["status"] == "not_observable"
    assert result["hermes_telegram"]["model"] is None


async def test_record_hermes_chat_turn_model_ignores_falsy(monkeypatch):
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    mr.record_hermes_chat_turn_model("real-model")
    mr.record_hermes_chat_turn_model("")  # must not clobber the real observation
    mr.record_hermes_chat_turn_model(None)

    result = await mr.get_hermes_models()

    assert result["hermes_chat"]["model"] == "real-model"


# --- Full readout + credential safety --------------------------------------

async def test_get_model_readout_aggregates_all_three_surfaces(monkeypatch):
    monkeypatch.setattr(mr.settings, "llm_backend", "anthropic")
    monkeypatch.setattr(mr.settings, "anthropic_model", "claude-haiku-4-5")
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    mr.record_hermes_chat_turn_model("accounts/fireworks/models/deepseek-v4-flash-0731")

    result = await mr.get_model_readout()

    assert set(result.keys()) == {"lifeos_native", "hermes_chat", "hermes_telegram"}
    assert result["lifeos_native"]["model"] == "claude-haiku-4-5"
    assert result["hermes_chat"]["model"] == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert result["hermes_telegram"]["status"] == "not_observable"


async def test_health_full_includes_the_model_readout(monkeypatch):
    """Wiring check: GET /health/full (api/main.py) surfaces the readout
    under a `models` key, separate from the pass/fail `checks` — an
    "unknown" Hermes readout shouldn't flip LifeOS's own health status."""
    import api.main as main

    monkeypatch.setattr(main.settings, "llm_backend", "anthropic")
    monkeypatch.setattr(main.settings, "anthropic_model", "claude-haiku-4-5")
    monkeypatch.setattr(main.settings, "hermes_backend_url", "")
    # `full_health_check()`'s vault-root sanity check (#762) reaches the
    # real `get_vector_store()` singleton whenever the vault_search probe
    # reports "ok" -- which it does on a host that happens to have a live
    # LifeOS API + ChromaDB running. That's a live-store touch this test
    # doesn't need; stub it to raise, which `_check_vault_root_sanity`'s own
    # `except Exception` already treats as a benign hiccup (#828).
    monkeypatch.setattr(
        "api.services.vectorstore.get_vector_store",
        MagicMock(side_effect=Exception("no live vector store in tests (#828)")),
    )

    result = await main.full_health_check()

    assert result["models"]["lifeos_native"] == {
        "status": "ok", "backend": "anthropic", "model": "claude-haiku-4-5",
    }
    assert result["models"]["hermes_chat"]["status"] == "not_configured"
    # The pass/fail counting below `checks` is untouched by this section.
    assert "models" not in result["checks"]


async def test_no_credential_leaks_when_local_probe_fails(monkeypatch):
    """Belt-and-suspenders: even in the failure path, no configured secret
    ever appears in the aggregated readout. hermes_backend_token is set here
    even though the current Hermes path never sends it anywhere (#658
    review removed the live Hermes probe) — guards against a future
    regression that reintroduces a network call without this check."""
    monkeypatch.setattr(mr.settings, "llm_backend", "local")
    monkeypatch.setattr(mr.settings, "local_llm_url", "http://stub-down")
    monkeypatch.setattr(mr.settings, "hermes_backend_url", "http://adapter")
    monkeypatch.setattr(mr.settings, "hermes_backend_token", "super-secret-token-value")
    monkeypatch.setattr(mr, "_client", lambda: _FailingClient())

    result = await mr.get_model_readout()

    assert "super-secret-token-value" not in str(result)
    assert result["lifeos_native"]["status"] == "unknown"
    assert result["hermes_chat"]["status"] == "unknown"
    assert result["hermes_telegram"]["status"] == "not_observable"


# --- hermes_proxy.py wiring: a real turn's usage event feeds the readout --

async def test_hermes_proxy_usage_event_records_the_observed_model(monkeypatch):
    """The exact call site: _HermesTurnPersister._handle_usage() must call
    record_hermes_chat_turn_model() when a turn's usage event validates."""
    from api.routes.hermes_proxy import _HermesTurnPersister

    persister = _HermesTurnPersister(question="hi", persona_id="primary")
    persister._handle_usage({
        "type": "usage",
        "model": "accounts/fireworks/models/deepseek-v4-flash-0731",
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 0.001,
    })

    model, observed_at = mr._last_observed_hermes_chat_model()
    assert model == "accounts/fireworks/models/deepseek-v4-flash-0731"
    assert observed_at is not None


async def test_hermes_proxy_malformed_usage_event_does_not_record(monkeypatch):
    from api.routes.hermes_proxy import _HermesTurnPersister

    persister = _HermesTurnPersister(question="hi", persona_id="primary")
    persister._handle_usage({"type": "usage", "model": None, "input_tokens": 10, "output_tokens": 20})

    model, _ = mr._last_observed_hermes_chat_model()
    assert model is None
