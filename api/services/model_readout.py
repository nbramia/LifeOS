"""Model readout (#658) — which model is actually serving each chat surface,
right now.

Two upstream failures motivated this: hermes#49 mistook a stale tracked
config snapshot for the live value, and hermes#50 had two surfaces answering
with materially different competence and nobody told. Both were
**invisibility**, not disagreement — this module answers "what's live" by
asking the actual running process, or observing what actually happened,
never by re-reading a config file:

- LifeOS native picker, Anthropic backend: `settings.anthropic_model` IS the
  live value here — it's read straight out of this process's own in-memory
  settings (the same object every request already uses), not re-parsed off
  disk. There's no separate Anthropic process on this box that could be
  running a different model out from under that setting.
- LifeOS native picker, local backend: `settings.local_llm_model` is only a
  declared intent — llama-server serves one model per process and can be
  restarted against a different one without this setting changing (exactly
  the tracked-snapshot-vs-live mismatch hermes#49 hit), so it's confirmed
  with a live probe instead of trusted.
- Hermes chat: `LIFEOS_HERMES_BACKEND_URL` (confirmed live on the real host,
  #658 review) points at LifeOS's own hermes-lifeos-adapter, not the Hermes
  gateway itself — the adapter has no `/v1/models` (or any capability
  endpoint) to probe, and even if it did, a capability probe answers "what
  COULD serve a turn", not "what DID": the adapter's `hermes_model` config
  (or a per-turn `model_hint`) can pick a different model per request, so a
  single "current capability" value would misrepresent turns that overrode
  it. The only trustworthy signal is the model Hermes itself *reported*
  serving a turn with, in that turn's own `usage` event — observed, not
  declared. `api/routes/hermes_proxy.py`'s `_HermesTurnPersister` already
  parses that event for every real Hermes chat turn; `record_hermes_chat_turn_model`
  below is its write side, called the moment a turn's usage event validates.
  "unknown" until this process has relayed at least one such turn.
- Hermes Telegram: Hermes's Telegram bot talks to the Hermes gateway
  directly, bypassing LifeOS entirely — by design, so Hermes's Telegram
  path stays independent of LifeOS uptime (see client-surfaces.md). That
  means LifeOS never sees a single byte of that traffic: not a config, not
  a probe, not an observed turn. Reported as "not_observable" — a status
  distinct from "unknown" ("tried to find out, couldn't") because there is
  no attempt to fail here, only an acknowledged structural blind spot. A
  borrowed value from hermes_chat would be dishonest: the adapter's
  `hermes_model`/`model_hint` machinery means the two CAN genuinely differ
  per turn (this is exactly the hermes#50 shape — two surfaces, potentially
  different competence — so asserting they match would recreate the same
  invisibility this readout exists to end, just with more confidence).

Every surface reports `{"status": "ok" | "unknown" | "not_configured" |
"not_observable", "model": str | None, ...}`. "unknown" is the deliberate
answer when a surface can't be confirmed — never falling back to whatever's
configured, which would repeat hermes#49's mistake in a different place.

Never returns a credential. `_probe_live_model` sends the bearer token it's
given (used only for the local-backend probe path today, which has no
credential) but never echoes it, logs it, or includes it in a returned
value.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# Short and symmetric (connect/read/write/pool) like the other inline health
# probes in api/main.py's full_health_check (e.g. the ChromaDB heartbeat) —
# this runs inside a health check, not a user-facing turn, so it should fail
# fast rather than hang the readout on a wedged backend.
_PROBE_TIMEOUT = httpx.Timeout(5.0)


def _client() -> httpx.AsyncClient:
    """httpx client for live model probes — a seam for tests."""
    return httpx.AsyncClient(timeout=_PROBE_TIMEOUT)


async def _probe_live_model(base_url: str, token: str = "") -> Optional[str]:
    """GET {base_url}/v1/models and return the first advertised model id, or
    None if unreachable, unauthorized, or the response is unparseable.

    Used today only for the LifeOS native picker's local backend
    (llama-server) — see module docstring for why Hermes is read via
    observation instead.
    """
    headers = {"authorization": f"Bearer {token}"} if token else {}
    try:
        async with _client() as client:
            resp = await client.get(f"{base_url.rstrip('/')}/v1/models", headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json().get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        model_id = data[0].get("id")
        return model_id if isinstance(model_id, str) and model_id else None
    except (httpx.HTTPError, ValueError):
        # ValueError covers resp.json() on a non-JSON body. Deliberately not
        # logging the exception text or returning it to the caller: never
        # surface anything derived from a response to a request that carried
        # this call's own Authorization header.
        return None


async def get_lifeos_native_model() -> dict:
    """The model the native LifeOS chat picker's default ("auto") turn
    actually runs on right now. See module docstring for the per-backend
    rationale."""
    backend = (settings.llm_backend or "anthropic").lower()
    if backend != "local":
        return {"status": "ok", "backend": "anthropic", "model": settings.anthropic_model}

    model = await _probe_live_model(settings.local_llm_url)
    if model is None:
        return {"status": "unknown", "backend": "local", "model": None}
    return {"status": "ok", "backend": "local", "model": model}


# --- Hermes chat: observed from the last real turn, not probed -----------
#
# In-memory only, deliberately: it resets to "nothing observed" on every
# restart rather than persisting a last-known value across one, which is
# the correct behavior — a value from before a restart is exactly the kind
# of stale-but-plausible reading hermes#49 got burned by. A single process
# is this repo's deployment model today (server.sh runs one), matching
# every other in-memory singleton here (e.g. ServiceHealthRegistry in
# service_health.py); the lock guards against a future multi-worker
# deployment, not against any concurrency that exists today.
_hermes_chat_lock = threading.Lock()
_hermes_chat_last_model: Optional[str] = None
_hermes_chat_last_observed_at: Optional[str] = None


def record_hermes_chat_turn_model(model: str) -> None:
    """Called by `api/routes/hermes_proxy.py`'s `_HermesTurnPersister` the
    moment a real Hermes chat turn's `usage` event validates and reports a
    model (#658). See module docstring: this is the only trustworthy
    "what's live" signal for Hermes chat, because Hermes can serve a
    different model per turn. Ignores a falsy model rather than clobbering
    a real prior observation with nothing.
    """
    if not model:
        return
    global _hermes_chat_last_model, _hermes_chat_last_observed_at
    with _hermes_chat_lock:
        _hermes_chat_last_model = model
        _hermes_chat_last_observed_at = datetime.now(timezone.utc).isoformat()


def _last_observed_hermes_chat_model() -> tuple[Optional[str], Optional[str]]:
    with _hermes_chat_lock:
        return _hermes_chat_last_model, _hermes_chat_last_observed_at


async def get_hermes_models() -> dict:
    """Hermes chat and Hermes Telegram. See module docstring for why the
    two are read completely differently and never assumed to match."""
    if not settings.hermes_backend_url:
        return {
            "hermes_chat": {"status": "not_configured", "model": None},
            "hermes_telegram": {"status": "not_configured", "model": None},
        }

    model, observed_at = _last_observed_hermes_chat_model()
    if model is None:
        hermes_chat = {"status": "unknown", "model": None}
    else:
        hermes_chat = {"status": "ok", "model": model, "observed_at": observed_at}

    return {
        "hermes_chat": hermes_chat,
        "hermes_telegram": {
            "status": "not_observable",
            "model": None,
            "note": (
                "Hermes's Telegram bot talks to the Hermes gateway directly, "
                "bypassing LifeOS by design (client-surfaces.md) — LifeOS has "
                "no channel to observe or probe it, and hermes_chat's model "
                "can genuinely differ per turn, so it is never assumed to match."
            ),
        },
    }


async def get_model_readout() -> dict:
    """Per-surface live model readout (#658): LifeOS native picker, Hermes
    chat, Hermes Telegram. See module docstring."""
    native = await get_lifeos_native_model()
    hermes = await get_hermes_models()
    return {"lifeos_native": native, **hermes}
