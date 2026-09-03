"""Tests for ModelCatalog (#851, AC11): per-engine model lists merged with
pricing, a 24h TTL cache that calls no provider on a cache hit, and a
provider failure falling back to the last cached list with `stale: true`.
Every provider is a stub — no network call is ever made.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.agent_worker.model_catalog import ModelCatalog


pytestmark = pytest.mark.unit


class _FrozenClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FakeAnthropicModel:
    def __init__(self, id_: str, display_name: str | None = None):
        self.id = id_
        self.display_name = display_name


class _FakeAnthropicPage:
    def __init__(self, models):
        self.data = models


class _FakeAnthropicModelsAPI:
    def __init__(self, models, raise_exc=None):
        self._models = models
        self._raise = raise_exc

    def list(self):
        if self._raise:
            raise self._raise
        return _FakeAnthropicPage(self._models)


class _FakeAnthropicClient:
    def __init__(self, models, raise_exc=None):
        self.models = _FakeAnthropicModelsAPI(models, raise_exc)


async def _noop_local_probe():
    return None


async def _noop_hermes_probe():
    return {"hermes_chat": {"status": "unknown", "model": None}}


def _write_codex_cache(path: Path, models: list[dict]):
    path.write_text(json.dumps({"fetched_at": "2026-01-01T00:00:00Z", "models": models}))


@pytest.mark.asyncio
async def test_claude_engine_empty_when_no_api_key(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    catalog = ModelCatalog(
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    assert result["engines"]["claude"] == []
    assert catalog.provider_call_count == 0


@pytest.mark.asyncio
async def test_claude_models_merged_with_pricing(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    fake_client = _FakeAnthropicClient([
        _FakeAnthropicModel("claude-opus-5", "Claude Opus 5"),
        _FakeAnthropicModel("claude-haiku-4-5", "Claude Haiku 4.5"),
    ])
    catalog = ModelCatalog(
        anthropic_client_factory=lambda: fake_client,
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    claude = {m["id"]: m for m in result["engines"]["claude"]}
    assert claude["claude-opus-5"]["label"] == "Claude Opus 5"
    assert claude["claude-opus-5"]["pricing"]["input"] > 0
    assert claude["claude-haiku-4-5"]["pricing"] is not None


@pytest.mark.asyncio
async def test_codex_reads_models_cache_file(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    cache_path = tmp_path / "models_cache.json"
    _write_codex_cache(cache_path, [
        {"slug": "gpt-5.5", "display_name": "GPT-5.5"},
        {"slug": "gpt-5.5-codex"},
    ])
    catalog = ModelCatalog(
        codex_cache_path=str(cache_path),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    codex_ids = {m["id"] for m in result["engines"]["codex"]}
    assert codex_ids == {"gpt-5.5", "gpt-5.5-codex"}


@pytest.mark.asyncio
async def test_codex_falls_back_to_provider_list_when_cache_missing(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai-test", raising=False)

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.5-mini"}]}

    class _FakeHttpClient:
        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse()

        def close(self):
            captured["closed"] = True

    catalog = ModelCatalog(
        codex_cache_path=str(tmp_path / "missing.json"),
        openai_http_client_factory=lambda: _FakeHttpClient(),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    codex_ids = {m["id"] for m in result["engines"]["codex"]}
    assert codex_ids == {"gpt-5.5", "gpt-5.5-mini"}
    assert captured["headers"]["Authorization"] == "Bearer sk-openai-test"
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_codex_empty_when_cache_missing_and_no_openai_key(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    catalog = ModelCatalog(
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    assert result["engines"]["codex"] == []
    assert catalog.provider_call_count == 0


@pytest.mark.asyncio
async def test_local_and_hermes_from_probes(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)

    async def local_probe():
        return "local"

    async def hermes_probe():
        return {"hermes_chat": {"status": "ok", "model": "deepseek-v4"}}

    catalog = ModelCatalog(
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=local_probe, hermes_probe=hermes_probe,
        clock=_FrozenClock(),
    )
    result = await catalog.get(ttl_seconds=86400)
    assert result["engines"]["local"] == [{"id": "local", "label": "local", "pricing": {"input": 0.0, "output": 0.0}}]
    assert result["engines"]["hermes"][0]["id"] == "deepseek-v4"


@pytest.mark.asyncio
async def test_second_call_within_ttl_calls_no_provider(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    fake_client = _FakeAnthropicClient([_FakeAnthropicModel("claude-opus-5")])
    clock = _FrozenClock()
    catalog = ModelCatalog(
        anthropic_client_factory=lambda: fake_client,
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=clock,
    )
    await catalog.get(ttl_seconds=86400)
    assert catalog.provider_call_count == 1
    clock.now += 3600  # 1 hour later, well within the 24h TTL
    result = await catalog.get(ttl_seconds=86400)
    assert catalog.provider_call_count == 1  # no second provider call
    assert result["stale"] is False


@pytest.mark.asyncio
async def test_ttl_expiry_triggers_a_fresh_call(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    fake_client = _FakeAnthropicClient([_FakeAnthropicModel("claude-opus-5")])
    clock = _FrozenClock()
    catalog = ModelCatalog(
        anthropic_client_factory=lambda: fake_client,
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=clock,
    )
    await catalog.get(ttl_seconds=100)
    clock.now += 101
    await catalog.get(ttl_seconds=100)
    assert catalog.provider_call_count == 2


@pytest.mark.asyncio
async def test_provider_failure_returns_stale_cached_list(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    good_client = _FakeAnthropicClient([_FakeAnthropicModel("claude-opus-5")])
    clock = _FrozenClock()
    catalog = ModelCatalog(
        anthropic_client_factory=lambda: good_client,
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=clock,
    )
    first = await catalog.get(ttl_seconds=1)
    assert first["stale"] is False
    assert first["engines"]["claude"][0]["id"] == "claude-opus-5"

    # Swap in a failing client and let the TTL expire.
    catalog.anthropic_client_factory = lambda: _FakeAnthropicClient([], raise_exc=RuntimeError("provider down"))
    clock.now += 2
    second = await catalog.get(ttl_seconds=1)
    assert second["stale"] is True
    assert second["engines"]["claude"][0]["id"] == "claude-opus-5"  # last good list, unchanged


@pytest.mark.asyncio
async def test_first_call_provider_failure_raises_when_nothing_cached(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    failing_client = _FakeAnthropicClient([], raise_exc=RuntimeError("provider down"))
    catalog = ModelCatalog(
        anthropic_client_factory=lambda: failing_client,
        codex_cache_path=str(tmp_path / "missing.json"),
        local_probe=_noop_local_probe, hermes_probe=_noop_hermes_probe,
        clock=_FrozenClock(),
    )
    with pytest.raises(RuntimeError):
        await catalog.get(ttl_seconds=86400)
