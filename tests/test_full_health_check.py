"""Unit tests for the health-honesty fixes in `GET /health` and `GET /health/full`
(api/main.py, #697): `api_key_configured` must reflect the Anthropic API key
rather than the local-LLM URL (which has a non-empty default regardless of
configuration), and the `local_llm` / `gmail_search` rows in `/health/full`
must not report "ok" for a service that is neither running nor in use.

`full_health_check()` also probes several other live endpoints (vault
search, calendar, drive, ...) via real HTTP calls to `settings.port` — that
behavior predates this file (see `test_health_full_includes_the_model_readout`
in tests/test_model_readout.py) and is unaffected by these changes; those
calls simply fail closed (caught, reported as an "error" row) when nothing
is listening, which is fine for the fields under test here.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_vault_root_sanity_vectorstore(monkeypatch):
    """`full_health_check()`'s vault-root sanity check (#762) reaches the
    real `get_vector_store()` singleton whenever the vault_search probe
    reports "ok" -- which, on a host that happens to have a live LifeOS API
    + ChromaDB running (as the maintainer's does), it does. That's a
    live-store touch none of this file's tests need; stub it to raise,
    which `_check_vault_root_sanity`'s own `except Exception` already
    treats as a benign hiccup -- the same fail-closed behavior this file's
    module docstring already relies on for the OTHER live probes
    `full_health_check()` makes (#828).
    """
    monkeypatch.setattr(
        "api.services.vectorstore.get_vector_store",
        MagicMock(side_effect=Exception("no live vector store in tests (#828)")),
    )


def test_health_api_key_configured_true_with_key():
    from unittest.mock import patch
    from api.main import app

    mock_settings = MagicMock()
    mock_settings.anthropic_api_key = "sk-ant-fake-value"
    mock_settings.local_llm_url = ""  # must no longer influence this field

    client = TestClient(app)
    with patch("config.settings.settings", mock_settings):
        response = client.get("/health")
    data = response.json()
    assert data["checks"]["api_key_configured"] is True


def test_health_api_key_configured_false_without_key():
    from unittest.mock import patch
    from api.main import app

    mock_settings = MagicMock()
    mock_settings.anthropic_api_key = ""
    # A non-empty local_llm_url (the actual default) must not make this
    # field true — that was exactly the #697 bug.
    mock_settings.local_llm_url = "http://localhost:8080"

    client = TestClient(app)
    with patch("config.settings.settings", mock_settings):
        response = client.get("/health")
    data = response.json()
    assert data["checks"]["api_key_configured"] is False
    assert data["status"] == "degraded"


async def test_full_health_local_llm_not_in_use_when_backend_not_local(monkeypatch):
    import api.main as main

    monkeypatch.setattr(main.settings, "llm_backend", "anthropic")

    result = await main.full_health_check()

    assert result["checks"]["local_llm"]["status"] == "not_in_use"
    # Not-in-use must not count as a failure for the overall summary.
    assert "local_llm" not in [
        k for k, v in result["checks"].items() if v["status"] == "error"
    ]


async def test_full_health_local_llm_probes_reachability_when_backend_local(monkeypatch):
    import api.main as main

    monkeypatch.setattr(main.settings, "llm_backend", "local")
    monkeypatch.setattr(main.settings, "local_llm_url", "http://stub-local-llm")

    async def _reachable(self):
        return True

    monkeypatch.setattr(
        "api.services.llm_client.LocalLLMClient.ais_available", _reachable
    )

    result = await main.full_health_check()

    assert result["checks"]["local_llm"]["status"] == "ok"


async def test_full_health_local_llm_reports_error_when_unreachable(monkeypatch):
    import api.main as main

    monkeypatch.setattr(main.settings, "llm_backend", "local")
    monkeypatch.setattr(main.settings, "local_llm_url", "http://stub-local-llm")

    async def _unreachable(self):
        return False

    monkeypatch.setattr(
        "api.services.llm_client.LocalLLMClient.ais_available", _unreachable
    )

    result = await main.full_health_check()

    assert result["checks"]["local_llm"]["status"] == "error"
    assert "local_llm" in [
        k for k, v in result["checks"].items() if v["status"] == "error"
    ]


async def test_full_health_gmail_search_reports_not_configured_shape(monkeypatch):
    """No Google credentials on disk: gmail_search must report the same
    not-configured ("error") shape as calendar/drive, not "ok, 0 emails" —
    GmailService.search() swallows the underlying FileNotFoundError and
    returns an empty list, which used to mask this at the endpoint level."""
    import api.main as main
    from api.services import google_auth

    class _MissingCredsAuth:
        credentials_path = type("P", (), {"exists": lambda self: False})()

    monkeypatch.setattr(
        google_auth, "get_google_auth", lambda account_type=None: _MissingCredsAuth()
    )

    result = await main.full_health_check()

    assert result["checks"]["gmail_search"]["status"] == "error"


async def test_full_health_gmail_search_runs_normally_when_configured(monkeypatch):
    """With credentials present, behavior is unchanged: the real endpoint is
    still probed (not short-circuited by the new not-configured check).
    `full_health_check()` defines its endpoint probe as an inner closure, so
    we can't patch it directly — assert indirectly instead: the row must not
    be pre-filled with the not-configured shape when credentials exist."""
    import api.main as main
    from api.services import google_auth

    class _ConfiguredAuth:
        credentials_path = type("P", (), {"exists": lambda self: True})()

    monkeypatch.setattr(
        google_auth, "get_google_auth", lambda account_type=None: _ConfiguredAuth()
    )

    result = await main.full_health_check()

    assert result["checks"]["gmail_search"]["status"] in ("ok", "error")
    if result["checks"]["gmail_search"]["status"] == "error":
        assert "not configured" not in result["checks"]["gmail_search"].get("error", "")
