"""
eero home-network client — pause/resume a household profile or
device's internet access through eero's undocumented consumer API.

Session token resolution: the gitignored state file (``STATE_PATH``) first,
falling back to ``settings.eero_session_token``. A token the vendor rejects
as unauthorized (HTTP 401) triggers exactly one refresh-and-retry; if that
fails the session is treated as dead — loudly alerted (Telegram + a
human-queue card keyed ``eero-session``) and surfaced to the caller as
``EeroSessionDead`` (mapped to 502 by the route layer). A successful refresh
persists the new token to the state file (mode 0600) so it survives a
restart.

Targets (profiles or devices) are configured in the gitignored
``config/home/eero_targets.json`` (see ``eero_targets.example.json``),
matched case-insensitively. A missing or malformed target entry is skipped
with a warning, never raised — an absent config file starts the service
with zero configured targets.

Every write is state-reconciling: set the value, then read it back and
report what the vendor actually has, flagging a ``mismatch`` if it differs
from what was requested. A vendor write/read that fails outright, or
returns a response shape this client doesn't recognize, raises
``EeroAPIError`` (502 + a human-queue card keyed ``eero-api-error``) rather
than reporting a false success.

A pause given a duration (explicit ``minutes``, or a target's configured
``default_minutes`` when ``minutes`` is omitted) hands the resume off to the
scheduler (``api/services/scheduler_store.py``) as a one-off ``endpoint``
action keyed ``eero-resume:<normalized-name>`` — durable across a restart,
since the scheduler's source of truth is the vault. Passing ``indefinite``
forces an indefinite pause regardless of ``default_minutes`` and clears any
pending resume. A scheduler-fired resume (``scheduled=True``) retries a
vendor-level failure (``EeroAPIError``, including a transport failure) up to
3 times with backoff inside the request; a dead session stops after one
attempt, since ``_authed_request`` already alerts once when it decides the
session is dead and every retry would fail the same way. Either outcome
alerts loudly (human-queue key ``eero-resume-failed:<normalized-name>``) and
returns 502, since the scheduler marks a one-off entry fired *before*
calling the endpoint and never re-fires it — this route is the only chance
to retry or alert.

Any pause or resume call carrying ``scheduled=True`` that succeeds with no
mismatch returns an empty ``scheduler_message`` — the scheduler's fire loop
sends nothing for an empty message, so a cron or one-off scheduled call
posts no Telegram line on an unremarkable success. A mismatch still
produces a non-empty message.
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from api.services.atomic_write import atomic_write_text
from api.services.scheduler_store import get_scheduler_store
from config.settings import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api-user.e2ro.com"

# Module-level so tests can monkeypatch them to a tmp_path without touching
# the real vault/data directories. Relative paths resolve against the
# process cwd, same convention as embedding_gpu_lock_path (every LifeOS
# process runs with cwd = the project root).
STATE_PATH = Path("data/home/eero_session.json")
TARGETS_PATH = Path("config/home/eero_targets.json")

# Backoff between scheduled-resume retry attempts (seconds), applied inside
# the request per the acceptance criteria. Short by default — the scheduler
# already waited for the scheduled time; this is only smoothing over a
# transient vendor blip, not a long outage (a long outage is exactly what
# the eero-resume-failed alert is for).
_RESUME_RETRY_BACKOFF = (1.0, 2.0)
_RESUME_RETRY_ATTEMPTS = 3


class EeroSessionUnconfigured(Exception):
    """No session token available anywhere (state file or env var)."""


class EeroSessionDead(Exception):
    """The vendor rejected the token and a refresh-and-retry also failed."""


class EeroAPIError(Exception):
    """The vendor rejected a write, or returned an unrecognized response shape."""


class EeroUnknownTarget(Exception):
    """A target name that isn't in the configured target map."""

    def __init__(self, name: str, configured_names: str):
        self.name = name
        self.configured_names = configured_names
        super().__init__(
            f"unknown eero target {name!r}; configured targets: {configured_names}"
        )


class EeroResumeFailed(Exception):
    """A scheduled resume exhausted every retry attempt against the vendor."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"eero resume for {name!r} failed after {_RESUME_RETRY_ATTEMPTS} attempts; "
            f"{name} is still paused"
        )


@dataclass(frozen=True)
class Target:
    """A configured pause/resume target — a profile or a device."""
    name: str
    type: str  # "profile" or "device"
    url: str = ""          # profile only
    network_id: str = ""   # device only
    mac: str = ""          # device only
    default_minutes: Optional[int] = None  # applied when pause omits `minutes`

    @property
    def vendor_path(self) -> str:
        if self.type == "profile":
            return self.url
        return f"/2.3/networks/{self.network_id}/devices/{self.mac}"


# ---------------------------------------------------------------------------
# Token / session
# ---------------------------------------------------------------------------

def _load_token() -> Optional[str]:
    """State file first, then LIFEOS_EERO_SESSION_TOKEN. Never raises."""
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except OSError:
        raw = None
    if raw is not None:
        try:
            data = json.loads(raw)
            token = data.get("token") if isinstance(data, dict) else None
        except json.JSONDecodeError:
            token = None
        if token:
            return token
    return settings.eero_session_token or None


def has_session_token() -> bool:
    return _load_token() is not None


def _save_token(token: str) -> None:
    """Persist a refreshed token to the state file, mode 0600."""
    atomic_write_text(STATE_PATH, json.dumps({"token": token}))
    STATE_PATH.chmod(0o600)


def _new_http_client() -> httpx.AsyncClient:
    """Factory for the vendor HTTP client. Monkeypatched in tests to inject
    an httpx.MockTransport instead of hitting the real eero API."""
    return httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)


async def _vendor_request(
    method: str, path: str, token: str, *, json: Optional[dict] = None
) -> httpx.Response:
    """One vendor call, carrying the session as `Cookie: s=<token>`. A
    transport failure (connection refused, timeout, DNS, ...) is raised as
    EeroAPIError — never the raw httpx exception, which can otherwise embed
    connection detail up the call stack unfiltered — so every caller's
    existing EeroAPIError handling (retry, alert, 502) covers it too."""
    headers = {"Cookie": f"s={token}"}
    try:
        async with _new_http_client() as client:
            return await client.request(method, path, json=json, headers=headers)
    except httpx.HTTPError as e:
        raise EeroAPIError(f"eero vendor request to {path} failed: {type(e).__name__}") from e


async def _refresh_token(old_token: str) -> Optional[str]:
    """One refresh attempt. Returns the new token (already persisted) on
    success, None on any failure — never raises."""
    try:
        resp = await _vendor_request("POST", "/2.2/login/refresh", old_token)
    except EeroAPIError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    new_token = body.get("data", {}).get("user_token") if isinstance(body, dict) else None
    if not new_token:
        return None
    _save_token(new_token)
    return new_token


async def _authed_request(
    method: str, path: str, *, json: Optional[dict] = None
) -> httpx.Response:
    """A vendor call with session handling: no token -> EeroSessionUnconfigured;
    a 401 -> one refresh + one retry, else EeroSessionDead (alerted here,
    the one place a dead session is decided, regardless of caller)."""
    token = _load_token()
    if not token:
        raise EeroSessionUnconfigured()
    resp = await _vendor_request(method, path, token, json=json)
    if resp.status_code == 401:
        new_token = await _refresh_token(token)
        if new_token is not None:
            resp = await _vendor_request(method, path, new_token, json=json)
        if new_token is None or resp.status_code == 401:
            await _alert_session_dead()
            raise EeroSessionDead()
    return resp


def _check_vendor_write_ok(resp: httpx.Response) -> None:
    if resp.status_code != 200:
        raise EeroAPIError(f"eero vendor write failed: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise EeroAPIError("eero vendor write returned a non-JSON response")
    if not isinstance(body, dict) or "data" not in body:
        raise EeroAPIError("eero vendor write returned an unexpected response shape")


def _extract_paused(resp: httpx.Response) -> bool:
    if resp.status_code != 200:
        raise EeroAPIError(f"eero vendor read failed: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise EeroAPIError("eero vendor read returned a non-JSON response")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or "paused" not in data:
        raise EeroAPIError("eero vendor read returned an unexpected response shape")
    return bool(data["paused"])


async def _authed_write(method: str, path: str, body: dict) -> None:
    """An authenticated write, alerting (eero-api-error) and re-raising on
    a bad response — the shared path for both a manual write and a status
    read hitting an API-shaped (not session-shaped) failure. The request
    itself is inside the try so a transport failure (which `_vendor_request`
    already wraps as `EeroAPIError`) alerts too, not just a bad status code
    or response shape; `EeroSessionDead` is a distinct exception type and is
    unaffected."""
    try:
        resp = await _authed_request(method, path, json=body)
        _check_vendor_write_ok(resp)
    except EeroAPIError:
        await _alert_api_error()
        raise


async def _authed_read(path: str) -> bool:
    """See `_authed_write` — same alert-on-`EeroAPIError` coverage,
    including a transport failure raised from inside the request."""
    try:
        resp = await _authed_request("GET", path)
        return _extract_paused(resp)
    except EeroAPIError:
        await _alert_api_error()
        raise


# ---------------------------------------------------------------------------
# Alerts — never let a failure to alert crash the request; the 502 is the
# primary signal, the alert is a best-effort loud surfacing on top of it.
# ---------------------------------------------------------------------------

async def _alert_session_dead() -> None:
    from api.services import human_queue
    from api.services.telegram import send_message_async
    try:
        await send_message_async(
            "Eero session is dead — pause/resume automation is down. "
            "Re-run scripts/eero_login.py."
        )
    except Exception:
        logger.exception("eero: failed to send eero-session Telegram alert")
    try:
        human_queue.add_card(
            "Eero session expired",
            notes="Pause/resume automation can't reach eero: the session "
                  "token was rejected and refreshing it failed. Re-run "
                  "scripts/eero_login.py (see docs/guides/home-eero.md) to "
                  "get a fresh session token.",
            key="eero-session",
        )
    except Exception:
        logger.exception("eero: failed to file eero-session human-queue card")


async def _alert_api_error() -> None:
    from api.services import human_queue
    from api.services.telegram import send_message_async
    try:
        await send_message_async(
            "Eero: the vendor API rejected a request or returned an "
            "unexpected response — pause/resume automation may be broken."
        )
    except Exception:
        logger.exception("eero: failed to send eero-api-error Telegram alert")
    try:
        human_queue.add_card(
            "Eero API error",
            notes="The eero vendor API rejected a write or returned an "
                  "unrecognized response shape (likely vendor drift). "
                  "Check server logs and docs/guides/home-eero.md.",
            key="eero-api-error",
        )
    except Exception:
        logger.exception("eero: failed to file eero-api-error human-queue card")


async def _alert_resume_failed(name: str) -> None:
    from api.services import human_queue
    from api.services.telegram import send_message_async
    try:
        await send_message_async(
            f"Eero: scheduled resume for '{name}' failed after retries — "
            f"{name} is still paused."
        )
    except Exception:
        logger.exception("eero: failed to send eero-resume-failed Telegram alert for %r", name)
    try:
        human_queue.add_card(
            f"Eero resume failed: {name}",
            notes=f"A scheduled resume for '{name}' failed after "
                  f"{_RESUME_RETRY_ATTEMPTS} attempts against the vendor. "
                  f"{name} is still paused (offline). Resume it manually "
                  "via the eero app or POST /api/home/eero/"
                  f"{quote(name, safe='')}/resume.",
            key=f"eero-resume-failed:{_normalize(name)}",
        )
    except Exception:
        logger.exception("eero: failed to file eero-resume-failed card for %r", name)


# ---------------------------------------------------------------------------
# Target config
# ---------------------------------------------------------------------------

def _load_targets() -> dict[str, Target]:
    """Case-insensitive name -> Target. Missing/malformed config never
    raises — a missing file yields zero targets, a malformed entry is
    skipped with a warning naming it."""
    try:
        raw_text = TARGETS_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.warning("eero targets config is not valid JSON (%s); starting with zero configured targets", e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("eero targets config must be a JSON object; starting with zero configured targets")
        return {}

    targets: dict[str, Target] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            logger.warning("eero target %r: entry must be an object; skipping", name)
            continue
        default_minutes = entry.get("default_minutes")
        if default_minutes is not None:
            if (
                isinstance(default_minutes, bool)
                or not isinstance(default_minutes, int)
                or not (1 <= default_minutes <= 1440)
            ):
                logger.warning(
                    "eero target %r: invalid 'default_minutes' (%r), must be an integer in 1..1440; skipping",
                    name, default_minutes,
                )
                continue
        ttype = entry.get("type")
        if ttype == "profile":
            url = entry.get("url")
            if not isinstance(url, str) or not url.startswith("/"):
                logger.warning("eero target %r: profile entry missing/invalid 'url'; skipping", name)
                continue
            target = Target(name=str(name), type="profile", url=url, default_minutes=default_minutes)
        elif ttype == "device":
            network_id = entry.get("network_id")
            mac = entry.get("mac")
            if not isinstance(network_id, str) or not network_id or not isinstance(mac, str) or not mac:
                logger.warning("eero target %r: device entry missing/invalid 'network_id'/'mac'; skipping", name)
                continue
            target = Target(name=str(name), type="device", network_id=network_id, mac=mac, default_minutes=default_minutes)
        else:
            logger.warning("eero target %r: 'type' must be 'profile' or 'device' (got %r); skipping", name, ttype)
            continue
        targets[str(name).strip().lower()] = target
    return targets


def configured_target_names() -> list[str]:
    return sorted(t.name for t in _load_targets().values())


def _resolve_target(name: str) -> Target:
    targets = _load_targets()
    target = targets.get((name or "").strip().lower())
    if target is None:
        names = ", ".join(sorted(t.name for t in targets.values())) or "(none configured)"
        raise EeroUnknownTarget(name, names)
    return target


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    """Slug used for the scheduler operation key and the resume-failed
    human-queue key — avoids characters (`[`, `]`, whitespace) that would
    otherwise collide with the scheduler markdown's own inline-field
    syntax."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _operation_key(name: str) -> str:
    return f"eero-resume:{_normalize(name)}"


# ---------------------------------------------------------------------------
# Scheduler-backed pending resume
# ---------------------------------------------------------------------------

def _entries_for_operation(store, op_key: str) -> list:
    return [e for e in store.list_all() if e.operation_key == op_key]


def _pending_entry(store, op_key: str):
    return next((e for e in _entries_for_operation(store, op_key) if e.enabled), None)


def _delete_pending(store, op_key: str) -> None:
    for entry in _entries_for_operation(store, op_key):
        if entry.enabled:
            store.delete(entry.id)


def _delete_finished(store, op_key: str) -> None:
    for entry in _entries_for_operation(store, op_key):
        if not entry.enabled:
            store.delete(entry.id)


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def _result(
    target: Target, *, requested_paused: bool, paused: bool, resume_at: Optional[str],
    scheduled: bool = False,
) -> dict:
    mismatch = requested_paused != paused
    if scheduled and not mismatch:
        # A scheduler-fired call (cron or the one-off auto-resume) that
        # succeeded as requested sends no Telegram line — the fire loop
        # suppresses an empty scheduler_message. A mismatch still reports.
        scheduler_message = ""
    else:
        state_word = "paused" if paused else "resumed"
        scheduler_message = f"{target.name}: {state_word}"
        if mismatch:
            scheduler_message += " (requested state did not take — check the eero app)"
    return {
        "name": target.name,
        "type": target.type,
        "requested_paused": requested_paused,
        "paused": paused,
        "mismatch": mismatch,
        "resume_at": resume_at,
        "scheduler_message": scheduler_message,
    }


async def pause(
    name: str, minutes: Optional[int] = None, *, indefinite: bool = False, scheduled: bool = False,
) -> dict:
    """Pause a target's internet access.

    Duration resolves in this order: `indefinite=True` forces an indefinite
    pause and clears any pending resume, regardless of the target's
    `default_minutes`. Otherwise explicit `minutes` (1-1440) schedules an
    automatic resume via the scheduler; if `minutes` is omitted, the
    target's configured `default_minutes` is used when present, else the
    pause is indefinite. `indefinite=True` together with `minutes` is a
    validation error.

    `minutes` and the indefinite/minutes conflict are validated before any
    vendor call — the route gets this for free from pydantic, but a caller
    that skips pydantic (the agent tool, fed loosely-typed JSON from a tool
    call) does not. `scheduled=True` (a cron or one-off scheduler fire)
    suppresses `scheduler_message` on an unremarkable success — see
    `_result`.

    The pending-resume scheduler entry is created (or cleared, for an
    indefinite pause) *before* the vendor write/read-back, not after —
    so a write that actually takes effect but a subsequent vendor
    write/read failure (which raises before returning) still leaves a
    durable resume behind rather than pausing the target with no way
    back except manual intervention. Resume is idempotent, so a resume
    entry left behind by a write that didn't actually take effect is
    harmless — it just resumes an already-resumed target."""
    if indefinite and minutes is not None:
        raise ValueError("cannot pass both `indefinite` and `minutes`")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not (1 <= minutes <= 1440):
            raise ValueError(f"minutes must be an integer in 1..1440, got {minutes!r}")
    target = _resolve_target(name)

    effective_minutes = None if indefinite else (minutes if minutes is not None else target.default_minutes)

    store = get_scheduler_store()
    op_key = _operation_key(target.name)
    resume_at = None
    if effective_minutes is not None:
        _delete_pending(store, op_key)
        _delete_finished(store, op_key)
        resume_dt = datetime.now(timezone.utc) + timedelta(minutes=effective_minutes)
        resume_at = resume_dt.isoformat()
        store.create(
            name=f"Eero resume: {target.name}",
            schedule_type="once",
            schedule_value=resume_at,
            action="endpoint",
            endpoint_config={
                "endpoint": f"/api/home/eero/{quote(target.name, safe='')}/resume",
                "method": "POST",
                "params": {"scheduled": True},
            },
            operation_key=op_key,
            _log_content=False,
        )
    else:
        _delete_pending(store, op_key)

    await _authed_write("PUT", target.vendor_path, {"paused": True})
    paused = await _authed_read(target.vendor_path)

    return _result(target, requested_paused=True, paused=paused, resume_at=resume_at, scheduled=scheduled)


async def resume(name: str, *, scheduled: bool = False) -> dict:
    """Resume a target's internet access. `scheduled=True` (the scheduler's
    own fire) retries up to 3 times with backoff and alerts loudly on
    exhaustion instead of failing silently — the scheduler already marked
    this one-off entry fired and will never call it again. A dead session
    stops after one attempt: `_authed_request` already alerts once per call
    when it decides a session is dead, and a token that's dead once will be
    dead on every retry, so retrying would only pile up duplicate alerts."""
    target = _resolve_target(name)
    store = get_scheduler_store()
    op_key = _operation_key(target.name)

    if scheduled:
        last_error: Optional[Exception] = None
        for attempt in range(_RESUME_RETRY_ATTEMPTS):
            try:
                resp = await _authed_request("PUT", target.vendor_path, json={"paused": False})
                _check_vendor_write_ok(resp)
                last_error = None
                break
            except EeroSessionDead as e:
                last_error = e
                break
            except EeroAPIError as e:
                last_error = e
                if attempt < _RESUME_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RESUME_RETRY_BACKOFF[attempt])
        if last_error is not None:
            await _alert_resume_failed(target.name)
            raise EeroResumeFailed(target.name) from last_error
    else:
        await _authed_write("PUT", target.vendor_path, {"paused": False})
        _delete_pending(store, op_key)

    paused = await _authed_read(target.vendor_path)
    return _result(target, requested_paused=False, paused=paused, resume_at=None, scheduled=scheduled)


async def list_status() -> list[dict]:
    """Every configured target's current state, read live from the vendor."""
    targets = _load_targets()
    store = get_scheduler_store()
    out = []
    for target in sorted(targets.values(), key=lambda t: t.name.lower()):
        paused = await _authed_read(target.vendor_path)
        pending = _pending_entry(store, _operation_key(target.name))
        out.append({
            "name": target.name,
            "type": target.type,
            "paused": paused,
            "resume_at": pending.schedule_value if pending else None,
        })
    return out
