"""Model catalog for the board's assignment pickers (#851).

`GET /api/agents/models` (`api/routes/agent_assignment.py`) needs "what
models can each engine actually run right now" — the same "observed, not
declared" discipline `model_readout.py` already established for the /chat
surfaces, extended here to the four engines a card can be assigned to.
Never hardcodes a model list beyond `pricing.PRICING`'s rates, which are
merged into whichever entries they cover as a `pricing` hint.

Sources, one per engine:
  - **claude**: the Anthropic SDK's own `models.list()` — never a
    hand-maintained table (that's exactly what went stale in pricing.py
    before #655/#656). Skipped (empty list, not an error) when no API key
    is configured.
  - **codex**: the Codex CLI's own `~/.codex/models_cache.json`
    (`settings.codex_models_cache_path`), falling back to a live OpenAI
    models list call when the file is missing/unreadable/empty AND
    `settings.openai_api_key` is set; otherwise empty (not an error).
  - **local**: the running llama-server's `/v1/models`, via
    `model_readout._probe_live_model` — the same live probe /chat's local
    picker already trusts over a declared setting.
  - **hermes**: `model_readout.get_hermes_models()`'s `hermes_chat` entry —
    observed from the last real turn, never probed (see that module's
    docstring for why Hermes can't be probed for "what it would run").

Cached for `settings.agent_model_catalog_ttl_seconds` (default 24h) so a
picker open doesn't cost a provider round trip every time. A refresh
failure (any engine's fetch raising) falls back to the last successful
catalog with `stale: true` rather than 500ing the picker or discarding
what's cached — the same "observed beats nothing" instinct as everywhere
else in this module, applied to failure instead of absence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from api.services.agent_worker.pricing import PRICING, _DATED_SNAPSHOT_SUFFIX
from config.settings import settings


logger = logging.getLogger(__name__)

ENGINES = ("claude", "codex", "local", "hermes")

_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_OPENAI_TIMEOUT = httpx.Timeout(10.0)


def _pricing_for(model_id: str) -> Optional[dict]:
    rates = PRICING.get(model_id) or PRICING.get(_DATED_SNAPSHOT_SUFFIX.sub("", model_id))
    return dict(rates) if rates else None


def _entry(model_id: str, label: str | None = None) -> dict:
    return {
        "id": model_id,
        "label": label or model_id,
        "pricing": _pricing_for(model_id),
    }


@dataclass
class ModelCatalog:
    """Injectable-everything catalog builder (test seams on every provider
    call, plus the clock) so tests never touch the network and can assert
    the TTL cache's call counts deterministically.
    """

    anthropic_client_factory: Optional[Callable[[], Any]] = None
    codex_cache_path: Optional[str] = None
    openai_http_client_factory: Optional[Callable[[], httpx.Client]] = None
    local_probe: Optional[Callable[[], Any]] = None  # async () -> Optional[str]
    hermes_probe: Optional[Callable[[], Any]] = None  # async () -> dict (model_readout.get_hermes_models shape)
    clock: Callable[[], float] = field(default=time.monotonic)

    provider_call_count: int = field(default=0, init=False)

    _cached: Optional[dict] = field(default=None, init=False, repr=False)
    _cached_at: Optional[float] = field(default=None, init=False, repr=False)

    async def get(self, *, ttl_seconds: Optional[int] = None) -> dict:
        ttl = ttl_seconds if ttl_seconds is not None else settings.agent_model_catalog_ttl_seconds
        now = self.clock()
        if self._cached is not None and self._cached_at is not None and (now - self._cached_at) < ttl:
            return {**self._cached, "stale": False}
        try:
            fresh = await self._fetch_all()
        except Exception as exc:  # noqa: BLE001 — any single engine's provider failure
            logger.warning("model catalog refresh failed: %s", exc)
            if self._cached is not None:
                return {**self._cached, "stale": True}
            raise
        self._cached = fresh
        self._cached_at = now
        return {**fresh, "stale": False}

    async def _fetch_all(self) -> dict:
        engines = {
            "claude": await self._fetch_claude(),
            "codex": await self._fetch_codex(),
            "local": await self._fetch_local(),
            "hermes": await self._fetch_hermes(),
        }
        return {
            "engines": engines,
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    # ------------------------------------------------------------------
    # Per-engine fetchers
    # ------------------------------------------------------------------

    async def _fetch_claude(self) -> list[dict]:
        if not settings.anthropic_api_key:
            return []  # unconfigured, not a failure — mirrors get_hermes_models()
        self.provider_call_count += 1
        client = (self.anthropic_client_factory or self._default_anthropic_client)()

        def _list_sync() -> list[dict]:
            page = client.models.list()
            return [_entry(m.id, getattr(m, "display_name", None) or m.id) for m in page.data]

        return await asyncio.to_thread(_list_sync)

    @staticmethod
    def _default_anthropic_client():
        import anthropic
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)

    async def _fetch_codex(self) -> list[dict]:
        path = os.path.expanduser(self.codex_cache_path or settings.codex_models_cache_path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            models = data.get("models") or []
            if models:
                return [
                    _entry(m.get("slug") or m.get("id"), m.get("display_name"))
                    for m in models if m.get("slug") or m.get("id")
                ]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return await self._fetch_codex_fallback()

    async def _fetch_codex_fallback(self) -> list[dict]:
        if not settings.openai_api_key:
            return []  # unconfigured, not a failure
        self.provider_call_count += 1
        client = (self.openai_http_client_factory or (lambda: httpx.Client(timeout=_OPENAI_TIMEOUT)))()

        def _list_sync() -> list[dict]:
            resp = client.get(
                _OPENAI_MODELS_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            return [_entry(m["id"]) for m in data if isinstance(m, dict) and m.get("id")]

        try:
            return await asyncio.to_thread(_list_sync)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    async def _fetch_local(self) -> list[dict]:
        probe = self.local_probe or self._default_local_probe
        model = await probe()
        return [_entry(model)] if model else []

    @staticmethod
    async def _default_local_probe() -> Optional[str]:
        from api.services.model_readout import _probe_live_model
        return await _probe_live_model(settings.local_llm_url)

    async def _fetch_hermes(self) -> list[dict]:
        probe = self.hermes_probe or self._default_hermes_probe
        readout = await probe()
        chat = (readout or {}).get("hermes_chat") or {}
        model = chat.get("model") if chat.get("status") == "ok" else None
        return [_entry(model)] if model else []

    @staticmethod
    async def _default_hermes_probe() -> dict:
        from api.services.model_readout import get_hermes_models
        return await get_hermes_models()


# Process-wide singleton — mirrors model_readout.py's in-memory-only
# pattern (resets on restart, matching every other live-observed cache in
# this module). Tests construct their own ModelCatalog() with stub
# providers instead of touching this singleton.
_catalog: Optional[ModelCatalog] = None


def get_model_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog()
    return _catalog
