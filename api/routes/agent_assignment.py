"""New card-assignment endpoints (#851): the model catalog and the
Assigned-card "open" action.

Kept in its OWN router module rather than appended to `api/routes/agents.py`
— that file is being extended concurrently (and independently) by the
Kanban board UI issue with a *different* set of endpoints (`/board`,
`/board/cards/{id}/lane`, `/accept`, `/pending-questions...`); putting this
issue's two new paths here means the two branches touch different files and
merge without conflict. Both routers share the `/api/agents` prefix —
FastAPI accepts two routers on one prefix as long as the paths differ,
which they do here.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from api.services.agent_worker.host_catalog import get_host_catalog
from api.services.agent_worker.model_catalog import get_model_catalog
from api.services.agent_worker.session_store import (
    CLI_STATUS_ENDED,
    TERMINAL_STATUSES,
    SessionStore,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# A real ceiling rather than an inference from the tailscale probe's own
# 1.5s timeout, which doesn't cover asyncio.Lock queueing or event-loop
# scheduling and can be exceeded under concurrent load. Just under 2s,
# comfortably above the probe timeout so a healthy build still completes
# inside it.
_HOSTS_ROUTE_TIMEOUT_SECONDS = 1.8

# Recognized assignee tags, in the order checked. Mirrors preflight.py's
# `_apply_tag_overrides` tag set for the CLI-spawning engines — "local" is
# excluded here because Local Gemma runs headless (no interactive
# terminal to open).
_ASSIGNEE_TAGS = ("claude", "codex", "hermes")

# (round 1, finding #6) Serializes the todo/running-session checks and the
# actual spawn in `open_board_card` — without it, two concurrent requests
# (a double-click) can both pass the checks before either has spawned,
# opening two terminals onto the same card. A single process-wide lock is
# enough: this is a single API process (see module docstring), and the
# critical section is a few dict/DB reads plus a subprocess spawn, never
# a network call.
#
# The lock alone isn't sufficient by itself, though: neither `task.status`
# (the vault task doesn't flip to `in_progress` until the spawned CLI
# registers itself over its OWN lifecycle hook, seconds later — not
# synchronously within this request) nor `session_store` (nothing here
# writes to it; that also only happens on registration) changes as a
# result of a spawn, so a call that lands right after another one's spawn
# would see the exact same "nothing running yet" state and spawn again —
# lock-serialized, but still two spawns. `_opening_card_ids` closes that
# gap: a card_id already claimed by an in-flight-or-completed spawn this
# process has made stays 409'd regardless of what the vault/session_store
# say, until an actual registered session (or a failed spawn, which frees
# the card_id again) supersedes it.
#
# (round 2, finding #1) That gap is only ever a few seconds wide — the CLI
# registers its own lifecycle event shortly after it starts. So the claim
# doesn't need to live forever: `_opening_card_ids` maps card_id ->
# `time.monotonic()` at claim time, and the membership check below only
# honors it while younger than `_OPENING_GRACE_SECONDS`. Once expired, the
# card is eligible to be claimed again — closing the gap without
# permanently 409ing a card for the life of the process after a
# successful open (a fresh claim overwrites the stale timestamp, which is
# how an expired entry gets dropped — no separate removal pass needed).
_open_lock = threading.Lock()
_opening_card_ids: dict[str, float] = {}
_OPENING_GRACE_SECONDS = 30

_session_store: SessionStore | None = None


def _get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


@router.get("/models")
async def get_models() -> dict[str, Any]:
    """Per-engine model catalog for the board's assignment pickers.

    `{engines: {claude: [...], codex: [...], local: [...], hermes: [...]},
    refreshed_at, stale}` — see `model_catalog.py` for how each engine's
    list is sourced and cached.
    """
    return await get_model_catalog().get()


@router.get("/hosts")
async def get_hosts() -> dict[str, Any]:
    """Registry of hosts a card's `host` field can target, with
    reachability — the source for the board's host picker.

    `{hosts: [{name, ssh_target, online, is_api_host}], refreshed_at}` —
    see `host_catalog.py` for how the list is built and `online` probed
    and cached. `HostCatalog.get()` already degrades internally on a build
    failure (registry intact, every non-API host `online: null`) rather
    than raising, but two more layers back this up: a hard
    `_HOSTS_ROUTE_TIMEOUT_SECONDS` ceiling in case a build somehow outlives
    it, and a bare `except Exception` for anything else that still goes
    wrong. Both fall back to `HostCatalog.degraded()` — the same
    registry-at-`online: null` list, never a bare single-row degenerate —
    and log at `error` with a traceback: dropping the operator's whole
    host list is not a quiet `warning`-level event. Only if `degraded()`
    itself somehow raises does this fall back further, to a single
    API-host row.
    """
    catalog = get_host_catalog()
    try:
        return await asyncio.wait_for(catalog.get(), timeout=_HOSTS_ROUTE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            "host catalog fetch exceeded the %.1fs route budget; degrading to registry-at-online-null",
            _HOSTS_ROUTE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — the picker must never 500
        logger.exception("host catalog fetch failed: %s", exc)
    try:
        return catalog.degraded()
    except Exception as exc:  # noqa: BLE001 — absolute last resort
        logger.exception("host catalog degraded fallback failed: %s", exc)
        from api.services.agent_worker.remote_spawn import api_host_name
        return {
            "hosts": [{
                "name": api_host_name(), "ssh_target": None,
                "online": True, "is_api_host": True,
            }],
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def _card_assignee(tags: list[str]) -> str | None:
    normalized = {t.strip().lower().lstrip("#") for t in (tags or [])}
    for candidate in _ASSIGNEE_TAGS:
        if candidate in normalized:
            return candidate
    return None


def _has_running_cli_session(card_id: str) -> bool:
    store = _get_session_store()
    for cli in store.list_cli_sessions():
        if cli.task_id == card_id and cli.status != CLI_STATUS_ENDED:
            return True
    return False


@router.post("/board/cards/{card_id}/open")
async def open_board_card(card_id: str) -> dict[str, Any]:
    """Open an Assigned card: spawn the interactive CLI in a terminal with
    the card's prompt, or deep-link to its Hermes conversation.

    A card is "Assigned" for this endpoint's purposes when its status is
    still `todo` (the worker hasn't claimed it — `#agent-running` swaps
    status away from `todo`), it carries a recognized assignee tag
    (`claude`/`codex`/`hermes`), and no session — worker-dispatched or a
    prior interactive open — is already running against it. 409 on any of
    those failing, so the board never opens a second terminal onto the
    same card.

    `claude`/`codex`: spawns the CLI locally (or over ssh when the card
    names a registered `host` field) with `LIFEOS_TASK_ID=<card_id>` set —
    `scripts/lifeos-agent-hook.sh` already forwards that as `task_id` on
    every lifecycle event it posts (#849), and `POST /cli-sessions/events`
    now moves the card to `in_progress` the moment that session registers
    (see `cli_session_event` in `api/routes/agents.py`).

    `hermes`: no terminal to spawn — returns `open_url` pointing at the
    card's Hermes conversation in `/chat` once one exists (set by
    `HermesExecutor` on the session row), else 409.
    """
    from api.services.task_manager import get_task_manager

    manager = get_task_manager()
    task = manager.get(card_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"card {card_id} not found")

    assignee = _card_assignee(task.tags)
    if assignee is None:
        raise HTTPException(
            status_code=409,
            detail="card has no recognized assignee tag (#claude, #codex, or #hermes)",
        )

    session_store = _get_session_store()

    if assignee == "hermes":
        session = session_store.get(card_id)
        if session is None or not session.conversation_id:
            raise HTTPException(status_code=409, detail="no Hermes conversation yet for this card")
        return {"open_url": f"/chat?conversation={session.conversation_id}"}

    # (round 1, finding #6) Hold the lock across the checks AND the spawn —
    # not just the spawn — so a double-click can't have both requests pass
    # the checks before either has actually spawned anything.
    with _open_lock:
        if task.status != "todo":
            raise HTTPException(
                status_code=409,
                detail=f"card is not in Assigned state (status={task.status!r})",
            )
        existing = session_store.get(card_id)
        if existing is not None and existing.status not in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="card already has a running session")
        if _has_running_cli_session(card_id):
            raise HTTPException(status_code=409, detail="card already has a running interactive session")
        claimed_at = _opening_card_ids.get(card_id)
        if claimed_at is not None and time.monotonic() - claimed_at < _OPENING_GRACE_SECONDS:
            raise HTTPException(status_code=409, detail="card open is already in progress")
        _opening_card_ids[card_id] = time.monotonic()
        try:
            return _spawn_interactive_cli(card_id, task, assignee)
        except Exception:
            # Spawn failed (bad launcher config, missing binary, ...) — free
            # the card_id so a retry isn't permanently locked out.
            _opening_card_ids.pop(card_id, None)
            raise


def _spawn_interactive_cli(card_id: str, task, assignee: str) -> dict[str, Any]:
    """Launch `claude`/`codex` in a terminal (local, or over ssh for a
    registered `host` field) seeded with the card's prompt. Reuses the
    same `cc_resume_cmd`/`codex_resume_cmd` WezTerm launcher templates
    `/resume` uses — only `{inner_command}` differs (a fresh interactive
    invocation with `LIFEOS_TASK_ID` set, not a `--resume <id>`)."""
    from api.routes.agents import _inject_wezterm_pane, _resume_env, api_host_name
    from api.services.agent_worker.assignment import extract_assignment
    from api.services.directory_resolver import resolve_working_directory
    from config.settings import settings

    prompt = (task.description or "").strip()
    if task.notes:
        prompt = f"{prompt}\n\n{task.notes}".strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="card has no title to seed the session with")

    if assignee == "claude":
        if not getattr(settings, "cc_resume_enabled", False):
            raise HTTPException(status_code=400, detail="cc resume disabled — set LIFEOS_CC_RESUME_ENABLED=true")
        template = (settings.cc_resume_cmd or "").strip()
        binary = "claude"
    else:
        if not getattr(settings, "codex_resume_enabled", False):
            raise HTTPException(status_code=400, detail="codex resume disabled — set LIFEOS_CODEX_RESUME_ENABLED=true")
        template = (settings.codex_resume_cmd or "").strip()
        binary = "codex"
    if not template:
        raise HTTPException(status_code=400, detail="no launcher command configured for this engine")

    assignment = extract_assignment(task.fields)
    host = assignment.host or ""
    remote_target: str | None = None
    if host and host != api_host_name():
        remote_target = settings.agent_hosts.get(host)
        if not remote_target:
            raise HTTPException(
                status_code=409,
                detail=f"host {host!r} is not configured in LIFEOS_AGENT_HOSTS",
            )

    cwd = resolve_working_directory(task.description or "")
    # `env NAME=value cmd...` — sets LIFEOS_TASK_ID for the spawned CLI so
    # the hook script (already reading it — see this function's docstring)
    # links the registered session back to this card. Works identically
    # whether the whole thing runs locally or is later ssh-wrapped, since
    # `env` runs as part of the command itself either way.
    inner_command = f"env LIFEOS_TASK_ID={shlex.quote(card_id)} {binary} {shlex.quote(prompt)}"
    rendered = template.replace("{cwd}", cwd).replace("{inner_command}", inner_command)
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"launcher command parse failed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="launcher command resolved to an empty argv")

    env = _resume_env()
    popen_argv = argv
    popen_cwd: str | None = cwd
    if remote_target:
        from api.services.agent_worker.remote_spawn import build_remote_launcher_argv
        popen_argv = build_remote_launcher_argv(argv, target=remote_target)
        popen_cwd = None
    elif "wezterm" in argv[0]:
        _inject_wezterm_pane(env)

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv only, no shell=True (explicit shlex.split above)
            popen_argv,
            cwd=popen_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"launcher binary not found: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"launcher spawn failed: {exc}") from exc

    return {
        "opened": True,
        "pid": proc.pid,
        "host": host or None,
        "cwd": cwd,
        "command": argv,
    }
