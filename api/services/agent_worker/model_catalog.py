"""Model catalog for the board's assignment pickers.

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
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from api.services.agent_worker.pricing import PRICING, _DATED_SNAPSHOT_SUFFIX
from config.settings import settings


logger = logging.getLogger(__name__)

ENGINES = ("claude", "codex", "local", "hermes")
FACT_ENGINES = ENGINES + ("claude_code", "remote")

# These are deliberately strings rather than a second policy enum.  The
# execution resolver owns routing decisions; this module only reports what a
# bounded discovery/readiness observation found.
CATALOG_STATES = ("loaded", "empty-valid", "unavailable", "unconfigured", "unknown")
READINESS_STATES = ("configured", "ready", "unavailable", "unknown")

_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_OPENAI_TIMEOUT = httpx.Timeout(10.0)
_ANTHROPIC_TIMEOUT_SECONDS = 10.0


def _pricing_for(model_id: str) -> Optional[dict]:
    rates = PRICING.get(model_id) or PRICING.get(_DATED_SNAPSHOT_SUFFIX.sub("", model_id))
    return dict(rates) if rates else None


def _entry(model_id: str, label: str | None = None) -> dict:
    return {
        "id": model_id,
        "label": label or model_id,
        "pricing": _pricing_for(model_id),
    }


@dataclass(frozen=True)
class _EngineResult:
    models: list[dict]
    state: str
    reason_code: str
    readiness: str
    readiness_source: str
    evidence_at: str | None = None


def _result(
    models: list[dict],
    state: str,
    reason_code: str,
    readiness: str,
    readiness_source: str,
    evidence_at: str | None = None,
) -> _EngineResult:
    return _EngineResult(
        models=models,
        state=state,
        reason_code=reason_code,
        readiness=readiness,
        readiness_source=readiness_source,
        evidence_at=evidence_at,
    )


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
    # The clock is used for both TTLs and serialized observation timestamps.
    # Keeping one injectable clock makes a refresh's timestamps deterministic
    # in tests and avoids mixing an observation with a later wall-clock read.
    clock: Callable[[], float] = field(default=time.time)

    provider_call_count: int = field(default=0, init=False)

    _cached: Optional[dict] = field(default=None, init=False, repr=False)
    _cached_at: Optional[float] = field(default=None, init=False, repr=False)

    def _timestamp(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def get(self, *, ttl_seconds: Optional[int] = None) -> dict:
        ttl = ttl_seconds if ttl_seconds is not None else settings.agent_model_catalog_ttl_seconds
        now = self.clock()
        if self._cached is not None and self._cached_at is not None and (now - self._cached_at) < ttl:
            return self._with_aggregate_stale(self._cached)
        fresh = await self._fetch_all(now=now)
        self._cached = fresh
        self._cached_at = now
        return self._with_aggregate_stale(fresh)

    @staticmethod
    def _with_aggregate_stale(response: dict) -> dict:
        # Keep the original aggregate fields exactly available to existing
        # board clients.  A partial refresh is stale only for the affected
        # engine(s), reflected by the additive per-engine metadata below.
        states = response.get("engine_states", {}).values()
        return {**response, "stale": any(item.get("stale") for item in states)}

    async def facts(self, *, ttl_seconds: Optional[int] = None) -> dict[str, dict]:
        """Return bounded execution facts for execution selection.

        This is a projection, not a routing decision: callers must choose
        whether an unknown or unavailable engine should be selected/fallback.
        """
        return facts_from_catalog(await self.get(ttl_seconds=ttl_seconds))

    async def _fetch_all(self, *, now: float | None = None) -> dict:
        observed_at = self._timestamp() if now is None else datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetchers = {
            "claude": self._fetch_claude,
            "codex": self._fetch_codex,
            "local": self._fetch_local,
            "hermes": self._fetch_hermes,
        }
        states: dict[str, dict] = {}
        for engine, fetcher in fetchers.items():
            try:
                result = await fetcher()
            except Exception as exc:  # noqa: BLE001 — isolate one provider
                logger.warning("model catalog refresh failed for %s: %s", engine, exc)
                result = _result([], "unavailable", "refresh_failed", "unknown", "prior_failure")
            previous = (self._cached or {}).get("engine_states", {}).get(engine, {})
            success = result.state in {"loaded", "empty-valid"}
            models = result.models if success else list(previous.get("models") or [])
            last_success_at = observed_at if success else previous.get("last_success_at")
            stale = not success and bool(models or previous.get("last_success_at"))
            states[engine] = {
                "models": models,
                "state": result.state,
                "observed_at": observed_at,
                "evidence_at": result.evidence_at or observed_at,
                "last_success_at": last_success_at,
                "stale": stale,
                "reason_code": result.reason_code,
                "staleness_reason": result.reason_code if stale else None,
                "readiness": {
                    "state": result.readiness,
                    "source": result.readiness_source,
                    "observed_at": observed_at,
                },
                # No provider quota endpoint is consulted by this catalog.
                "quota": {"state": "unknown", "source": "not_collected"},
            }
        return {
            # Legacy aggregate response, retained for existing board clients.
            "engines": {engine: state["models"] for engine, state in states.items()},
            "refreshed_at": observed_at,
            "engine_states": states,
            "readiness": {engine: state["readiness"] for engine, state in states.items()},
        }

    # ------------------------------------------------------------------
    # Per-engine fetchers
    # ------------------------------------------------------------------

    async def _fetch_claude(self) -> _EngineResult:
        if not settings.anthropic_api_key:
            return _result([], "unconfigured", "missing_api_key", "unavailable", "configuration")
        self.provider_call_count += 1
        client = (self.anthropic_client_factory or self._default_anthropic_client)()

        def _list_sync() -> list[dict]:
            page = client.models.list()
            return [_entry(m.id, getattr(m, "display_name", None) or m.id) for m in page.data]

        models = await asyncio.to_thread(_list_sync)
        return _result(
            models,
            "loaded" if models else "empty-valid",
            "loaded" if models else "empty_catalog",
            self._claude_readiness(),
            "configuration_and_models_endpoint",
        )

    @staticmethod
    def _claude_readiness() -> str:
        if not settings.anthropic_api_key:
            return "unavailable"
        if getattr(settings, "agent_preset_id", "") and getattr(settings, "agent_environment_id", ""):
            return "ready"
        return "configured"

    @staticmethod
    def _default_anthropic_client():
        import anthropic
        return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_ANTHROPIC_TIMEOUT_SECONDS)

    async def _fetch_codex(self) -> _EngineResult:
        path = os.path.expanduser(self.codex_cache_path or settings.codex_models_cache_path)
        cache_reason = "cache_missing"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            models = data.get("models") or []
            if models:
                entries = [
                    _entry(m.get("slug") or m.get("id"), m.get("display_name"))
                    for m in models if m.get("slug") or m.get("id")
                ]
                if entries:
                    return _result(
                        entries,
                        "loaded",
                        "cache_loaded",
                        self._codex_readiness(),
                        "codex_binary_presence",
                        _cache_evidence_timestamp(data),
                    )
            cache_reason = "cache_empty"
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            cache_reason = "cache_unreadable"
        return await self._fetch_codex_fallback(cache_reason)

    @staticmethod
    def _codex_readiness() -> str:
        binary = getattr(settings, "codex_binary", "codex")
        return "ready" if shutil.which(os.path.expanduser(binary)) else "unavailable"

    async def _fetch_codex_fallback(self, cache_reason: str) -> _EngineResult:
        readiness = self._codex_readiness()
        if not settings.openai_api_key:
            # A configured subscription CLI does not become invalid merely
            # because its optional model-list cache/API key is unavailable.
            state = "unknown" if readiness == "ready" else "unconfigured"
            return _result([], state, cache_reason, readiness, "codex_binary_presence")
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
            models = await asyncio.to_thread(_list_sync)
            return _result(
                models,
                "loaded" if models else "empty-valid",
                "provider_loaded" if models else "provider_empty",
                readiness,
                "codex_binary_presence",
            )
        except Exception:
            return _result([], "unavailable", "provider_unavailable", readiness, "codex_binary_presence")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    async def _fetch_local(self) -> _EngineResult:
        if not settings.local_llm_url:
            return _result([], "unconfigured", "missing_endpoint", "unavailable", "configuration")
        probe = self.local_probe or self._default_local_probe
        model = await probe()
        if isinstance(model, list):
            entries = [
                _entry(item.get("id"), item.get("label"))
                for item in model
                if isinstance(item, dict) and item.get("id")
            ]
            return _result(
                entries,
                "loaded" if entries else "empty-valid",
                "endpoint_loaded" if entries else "endpoint_empty",
                "ready",
                "models_endpoint",
            )
        if model:
            return _result([_entry(model)], "loaded", "endpoint_loaded", "ready", "models_endpoint")
        # `_probe_live_model` intentionally collapses auth, malformed, empty,
        # and unreachable responses to None.  Keep that uncertainty explicit.
        return _result([], "unknown", "no_observation", "unknown", "models_endpoint")

    @staticmethod
    async def _default_local_probe() -> Optional[str]:
        from api.services.model_readout import _probe_live_model
        return await _probe_live_model(settings.local_llm_url)

    async def _fetch_hermes(self) -> _EngineResult:
        # An injected probe is an explicit test/integration source and may be
        # used on a synthetic install with no configured URL. The default
        # probe still treats an absent URL as unconfigured.
        if not settings.hermes_backend_url and self.hermes_probe is None:
            return _result([], "unconfigured", "missing_endpoint", "unavailable", "configuration")
        probe = self.hermes_probe or self._default_hermes_probe
        readout = await probe()
        chat = (readout or {}).get("hermes_chat") or {}
        model = chat.get("model") if chat.get("status") == "ok" else None
        if model:
            return _result([_entry(model)], "loaded", "observed_turn", "ready", "observed_turn")
        return _result([], "unknown", "no_observed_turn", "configured", "hermes_configuration")

    @staticmethod
    async def _default_hermes_probe() -> dict:
        from api.services.model_readout import get_hermes_models
        return await get_hermes_models()


def _cache_evidence_timestamp(data: dict) -> str | None:
    """Normalize the CLI cache's source timestamp without trusting it as now."""
    value = data.get("fetched_at") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def facts_from_catalog(response: dict) -> dict[str, dict]:
    """Project catalog metadata into a small, policy-neutral facts mapping.

    The assignment/execution resolver can consume this without depending on
    the picker response shape.  In particular, ``unknown`` and
    ``unavailable`` are retained as facts; this adapter never turns either
    into a fallback or a dispatch decision.
    """
    output: dict[str, dict] = {}
    states = response.get("engine_states") or {}
    refreshed_at = response.get("refreshed_at")
    for engine in FACT_ENGINES:
        state = states.get(engine) or {}
        readiness = state.get("readiness") or {}
        models = state.get("models") or (response.get("engines") or {}).get(engine) or []
        if engine == "claude_code":
            binary = getattr(settings, "claude_binary", "claude")
            readiness = {
                "state": "ready" if shutil.which(os.path.expanduser(binary)) else "unavailable",
                "source": "claude_binary_presence",
                "observed_at": refreshed_at,
            }
            state = {
                "state": "unknown",
                "reason_code": "discovery_not_available",
                "stale": False,
                "observed_at": refreshed_at,
                "last_success_at": None,
            }
        elif engine == "remote":
            configured = bool(getattr(settings, "remote_llm_configured", False))
            readiness = {
                "state": "configured" if configured else "unavailable",
                "source": "remote_configuration",
                "observed_at": refreshed_at,
            }
            state = {
                "state": "unknown",
                "reason_code": "discovery_not_available",
                "stale": False,
                "observed_at": refreshed_at,
                "last_success_at": None,
            }
        output[engine] = {
            "engine": engine,
            "model_ids": tuple(item.get("id") for item in models if isinstance(item, dict) and item.get("id")),
            "catalog_state": state.get("state", "unknown"),
            "observed_at": state.get("observed_at"),
            "evidence_at": state.get("evidence_at") or state.get("observed_at"),
            "last_success_at": state.get("last_success_at"),
            "stale": bool(state.get("stale")),
            "reason_code": state.get("reason_code", "no_observation"),
            "staleness_reason": state.get("staleness_reason"),
            "readiness": readiness.get("state", "unknown"),
            "readiness_source": readiness.get("source", "unknown"),
            "readiness_observed_at": readiness.get("observed_at"),
            "quota": (state.get("quota") or {}).get("state", "unknown"),
        }
    return output


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
