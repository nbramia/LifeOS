"""Read-only agent activity visualization endpoints.

Powers the `/agents` UI: a live graph of in-flight and recently-completed
agent worker sessions, plus per-session transcript tailing. See
`docs/specs/technical/agent-worker.md` and issue #133.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.services.agent_worker.session_store import (
    TERMINAL_STATUSES,
    Session,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services import agent_viz_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


# Module-level lazy singletons. Tests monkeypatch these directly so they can
# point the endpoints at temp-dir-backed stores without hitting the real data
# directory.
_session_store: SessionStore | None = None
_transcript_store: TranscriptStore | None = None

# Labels are stable once derived from a non-fallback source, so cache to avoid
# re-deriving on every snapshot tick. Capped to bound memory if the process
# accumulates a lot of session IDs over a long uptime.
_label_cache: dict[str, str] = {}
_LABEL_CACHE_MAX = 500

# Transcript tail size used for snapshot summary fields. Capping keeps SSE
# tick cost bounded for sessions with long transcripts.
_SUMMARY_TAIL = 100

# How many sessions to list per snapshot. Sessions are returned newest-first.
_SNAPSHOT_LIMIT = 200


# Event kinds that count as errors. Includes operator/peer-initiated kills
# because the frontend renders them as failures and the count chip should
# match. The endswith check picks up future `*_failed`/`*_error` kinds.
_ERROR_KINDS = frozenset({
    "failed",
    "managed_failed",
    "child_failed_internal",
    "killed",
    "cascade_killed",
})


def _get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


def _get_transcript_store() -> TranscriptStore:
    global _transcript_store
    if _transcript_store is None:
        _transcript_store = TranscriptStore()
    return _transcript_store


def _is_error_kind(kind: str) -> bool:
    if kind in _ERROR_KINDS:
        return True
    return kind.endswith("_failed") or kind.endswith("_error")


def _label_for_session(s: Session, events: list[dict[str, Any]]) -> str:
    cached = _label_cache.get(s.session_id)
    if cached is not None:
        return cached

    label: str | None = None

    # Future-proof: if a transcript event ever carries a description directly
    # (e.g. via a future enrichment in worker._claim), use it.
    for ev in events[:5]:
        payload = ev.get("payload") or {}
        if ev.get("kind") in ("claim", "seed", "spawn"):
            desc = (
                payload.get("description")
                or payload.get("task_description")
                or payload.get("prompt")
            )
            if desc:
                label = str(desc)
                break

    # Real worker `claim`/`seed` payloads today only carry `task_id`, so for
    # root sessions look the task description up via the TaskManager. This
    # call is cheap (in-memory dict in the singleton) and the result is
    # cached below.
    if label is None and s.parent_session_id is None and s.task_id:
        try:
            from api.services.task_manager import get_task_manager

            task = get_task_manager().get(s.task_id)
            if task is not None and task.description:
                label = task.description
        except Exception as exc:  # noqa: BLE001 — defensive: never break the snapshot on label lookup
            logger.debug("task_manager lookup failed for %s: %s", s.task_id, exc)

    # Only cache when we found a real label — never the fallback, so a snapshot
    # tick that races the `claim` append doesn't poison the cache.
    found = label is not None
    if label is None:
        label = s.task_id or s.session_id
    label = label.strip().replace("\n", " ")
    if len(label) > 60:
        label = label[:57] + "…"

    if found:
        if len(_label_cache) >= _LABEL_CACHE_MAX:
            # Drop one arbitrary entry to keep the cache bounded.
            _label_cache.pop(next(iter(_label_cache)))
        _label_cache[s.session_id] = label
    return label


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    tool_call_count = 0
    error_count = 0
    last_event_kind = ""
    for ev in events:
        kind = str(ev.get("kind", ""))
        if kind == "tool_call":
            tool_call_count += 1
        if _is_error_kind(kind):
            error_count += 1
        last_event_kind = kind
    return {
        "tool_call_count": tool_call_count,
        "error_count": error_count,
        "last_event_kind": last_event_kind,
    }


def _session_to_dict(s: Session, transcript: TranscriptStore) -> dict[str, Any]:
    try:
        events = transcript.read(s.session_id)
    except Exception as exc:  # noqa: BLE001 — defensive: read should never break the snapshot
        logger.warning("transcript read failed for %s: %s", s.session_id, exc)
        events = []
    tail = events[-_SUMMARY_TAIL:] if len(events) > _SUMMARY_TAIL else events
    summary = _summarize_events(tail)
    return {
        "session_id": s.session_id,
        "task_id": s.task_id,
        "status": s.status,
        "routing": s.routing,
        "parent_session_id": s.parent_session_id,
        "root_session_id": s.root_session_id,
        "spawn_depth": s.spawn_depth,
        "yield_waiting_for": s.yield_waiting_for or [],
        "managed_agent_session_id": s.managed_agent_session_id,
        "started_at": s.started_at,
        "last_activity_at": s.last_activity_at,
        "total_input_tokens": s.total_input_tokens,
        "total_output_tokens": s.total_output_tokens,
        "total_cache_creation_tokens": getattr(s, "total_cache_creation_tokens", 0),
        "total_cache_read_tokens": getattr(s, "total_cache_read_tokens", 0),
        "total_dollars": round(s.total_dollars, 6),
        "total_active_seconds": round(s.total_active_seconds, 3),
        "expected_output": s.expected_output,
        "label": _label_for_session(s, events),
        "model_label": _model_label_for_routing(s.routing),
        "last_event_kind": summary["last_event_kind"],
        "tool_call_count": summary["tool_call_count"],
        "error_count": summary["error_count"],
        # Populated only if a previous /summary call has cached a result for
        # this session at its current last_activity_at. Snapshot ticks never
        # block on the LLM — short labels for unfetched sessions appear when
        # the operator opens the panel.
        "short_label": _short_label_for_snapshot(
            s.session_id, s.last_activity_at or 0.0, s.status or "",
        ),
    }


def _short_label_for_snapshot(session_id: str, last_activity_at: float, status: str = "") -> str | None:
    cached = agent_viz_summary.get_cached_summary(session_id, last_activity_at, status)
    return cached.short_label if cached else None


def _claude_code_enabled() -> bool:
    try:
        from config.settings import settings
        return bool(getattr(settings, "claude_code_viz_enabled", True))
    except Exception:  # noqa: BLE001 — degrade gracefully if settings fail to load
        return False


def _claude_code_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover + parse Claude Code sessions for the snapshot. Cached."""
    if not _claude_code_enabled():
        return [], []
    try:
        from config.settings import settings
        from api.services.claude_code import session_ingest as cc

        return cc.build_snapshot(
            projects_dir=settings.claude_code_projects_dir,
            lookback_days=int(getattr(settings, "claude_code_lookback_days", 7)),
        )
    except Exception as exc:  # noqa: BLE001 — never break the LifeOS snapshot if cc ingest fails
        logger.warning("claude_code snapshot failed: %s", exc)
        return [], []


def _build_snapshot() -> dict[str, Any]:
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    sessions = session_store.list_sessions(limit=_SNAPSHOT_LIMIT)
    session_dicts = [_session_to_dict(s, transcript_store) for s in sessions]
    # Tag LifeOS sessions with a source discriminator so the frontend can
    # distinguish them from Claude Code sessions in the union below.
    for sd in session_dicts:
        sd.setdefault("source", "lifeos_agent")
        sd.setdefault("model_label", _model_label_for_routing(sd.get("routing")))
    edges = [
        {"from": s.parent_session_id, "to": s.session_id, "type": "spawn"}
        for s in sessions
        if s.parent_session_id
    ]

    cc_sessions, cc_edges = _claude_code_snapshot()
    # CC session dicts come from claude_code/session_ingest.py and don't carry
    # the short_label field — attach it the same way (cached-only, no LLM in
    # the snapshot path).
    for sd in cc_sessions:
        sd.setdefault(
            "short_label",
            _short_label_for_snapshot(
                sd.get("session_id") or "",
                sd.get("last_activity_at") or 0.0,
                sd.get("status") or "",
            ),
        )
    session_dicts.extend(cc_sessions)
    edges.extend(cc_edges)

    return {
        "sessions": session_dicts,
        "edges": edges,
        "generated_at": int(time.time()),
    }


def _model_label_for_routing(routing: str | None) -> str:
    if (routing or "local") == "local":
        return "Local"
    try:
        from config.settings import settings
        m = (settings.agent_managed_model or "").lower()
    except Exception:  # noqa: BLE001
        m = ""
    if "haiku" in m:
        return "Haiku"
    if "sonnet" in m:
        return "Sonnet"
    if "opus" in m:
        return "Opus"
    return "Claude"


@router.get("/snapshot")
async def get_snapshot() -> dict[str, Any]:
    """Full current snapshot of agent sessions + spawn edges."""
    return _build_snapshot()


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Recent transcript events for one session (paginated tail).

    Dispatches by `cc:` prefix — Claude Code session ids are served by the
    `claude_code` ingest service, everything else by the LifeOS transcript
    store.
    """
    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from config.settings import settings
            from api.services.claude_code import session_ingest as cc

            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tail = events[-limit:] if len(events) > limit else events
        return {"session_id": session_id, "events": tail, "total": len(events)}

    transcript_store = _get_transcript_store()
    try:
        events = transcript_store.read(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tail = events[-limit:] if len(events) > limit else events
    return {"session_id": session_id, "events": tail, "total": len(events)}


@router.get("/sessions/{session_id}/summary")
async def get_session_summary(session_id: str) -> dict[str, Any]:
    """Concise "what did this session work on" summary for the /agents panel.

    Pulls the first user message, final assistant message, and any PR
    references out of the transcript, then asks the local Gemma LLM to
    produce a short node label (2-6 words) and a 1-2 sentence recap.

    Cached by (session_id, last_activity_at). A session that hasn't moved
    since the last call gets a cache hit and zero LLM cost.
    """
    # Resolve the session's label + last_activity_at + status + events.
    last_activity = 0.0
    label = session_id
    status = ""

    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from config.settings import settings
            from api.services.claude_code import session_ingest as cc

            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Find this CC session in the cached snapshot to pull label + activity.
        cc_sessions, _ = _claude_code_snapshot()
        match = next((s for s in cc_sessions if s.get("session_id") == session_id), None)
        if match:
            label = str(match.get("label") or session_id)
            last_activity = float(match.get("last_activity_at") or 0.0)
            status = str(match.get("status") or "")
    else:
        session_store = _get_session_store()
        s = session_store.get_by_session_id(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        transcript_store = _get_transcript_store()
        try:
            events = transcript_store.read(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        label = _label_for_session(s, events)
        last_activity = float(s.last_activity_at or 0.0)
        status = s.status or ""

    result = await agent_viz_summary.summarize_session(
        session_id,
        label=label,
        last_activity_at=last_activity,
        events=events,
        status=status,
    )
    return {"session_id": session_id, **result.as_dict()}


@router.get("/stream")
async def stream_snapshots() -> StreamingResponse:
    """SSE stream emitting a full snapshot every ~2 seconds."""

    async def generate():
        yield ": ok\n\n"
        while True:
            try:
                snap = _build_snapshot()
                yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
            except Exception as exc:  # noqa: BLE001 — keep the stream alive on errors
                logger.warning("snapshot stream tick failed: %s", exc)
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


class KillRequest(BaseModel):
    reason: str = ""


def _collect_subtree(session_store: SessionStore, target: Session) -> list[Session]:
    """Return `target` followed by every non-self descendant in its subtree.

    Walks via `parent_session_id` so non-root targets only take down their
    own children — not unrelated peers under the same root.
    """
    root_id = target.root_session_id or target.session_id
    all_in_root: list[Session] = list(session_store.list_descendants(root_id))
    if target.session_id != root_id:
        # `list_descendants` excludes the root itself; ensure we have the
        # root in the pool too in case the target's parent chain runs back
        # up through it.
        root_session = session_store.get_by_session_id(root_id)
        if root_session is not None:
            all_in_root.append(root_session)

    children_of: dict[str, list[Session]] = {}
    for s in all_in_root:
        if s.session_id == target.session_id:
            continue
        children_of.setdefault(s.parent_session_id or "", []).append(s)

    subtree: list[Session] = [target]
    queue: list[str] = [target.session_id]
    seen: set[str] = {target.session_id}
    while queue:
        sid = queue.pop(0)
        for child in children_of.get(sid, []):
            if child.session_id in seen:
                continue
            seen.add(child.session_id)
            subtree.append(child)
            queue.append(child.session_id)
    return subtree


def _maybe_managed_driver():
    """Lazy-instantiate a ManagedAgentsDriver when credentials are available.

    Returning `None` is fine — `teardown_session` then degrades to a
    local-only kill, and the worker's next managed poll reconciles the
    remote side. We don't reuse the worker's driver because the worker
    runs in a separate process.
    """
    try:
        from config.settings import settings
        api_key = settings.anthropic_api_key
        if not api_key:
            return None
        from api.services.agent_worker.managed_driver import ManagedAgentsDriver
        return ManagedAgentsDriver(api_key=api_key)
    except Exception as exc:  # noqa: BLE001 — never block the kill on driver setup
        logger.warning("operator kill: managed driver unavailable: %s", exc)
        return None


@router.post("/sessions/{session_id}/kill")
async def operator_kill_session(session_id: str, body: KillRequest | None = None) -> dict[str, Any]:
    """Operator-initiated kill: stop the target session and all descendants
    in its subtree. Target gets an `operator_killed` transcript event;
    descendants get `cascade_killed`.

    Local-network only — must NOT be exposed via Tailscale Funnel or the
    public MCP HTTP transport.
    """
    session_store = _get_session_store()
    transcript_store = _get_transcript_store()
    reason = (body.reason if body else "").strip() if body else ""

    target = session_store.get_by_session_id(session_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    if target.status in TERMINAL_STATUSES:
        return {"killed": [], "failures": [], "reason": f"already {target.status}"}

    subtree = _collect_subtree(session_store, target)
    driver = _maybe_managed_driver()
    try:
        from api.services.agent_worker.inter_agent import teardown_session as _teardown

        killed: list[str] = []
        failures: list[dict[str, str]] = []
        for idx, s in enumerate(subtree):
            if s.status in TERMINAL_STATUSES:
                continue
            if idx == 0:
                kind = "operator_killed"
                payload = {
                    "reason": reason,
                    "managed_remote": s.managed_agent_session_id,
                }
            else:
                kind = "cascade_killed"
                payload = {
                    "root": target.session_id,
                    "reason": reason,
                    "managed_remote": s.managed_agent_session_id,
                }
            result = _teardown(
                session_store, transcript_store, s,
                transcript_kind=kind,
                transcript_payload=payload,
                managed_driver=driver,
            )
            killed.append(s.session_id)
            if result.get("managed_failure"):
                failures.append({
                    "session_id": s.session_id,
                    "reason": result["managed_failure"],
                })
        return {"killed": killed, "failures": failures}
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass


class CCResumeRequest(BaseModel):
    """Body for POST /api/agents/sessions/{id}/resume — accepts overrides
    for the spawn env when the systemd-inherited env isn't enough."""
    extra_env: dict[str, str] = {}


class CCPaneBindRequest(BaseModel):
    """Body for POST /api/agents/cc-pane-bind — written by the SessionStart
    hook script on every `claude` start. Lets the /agents Focus / Go To
    button target sessions that were never opened via /agents Resume.
    """
    session_id: str = Field(..., min_length=1, max_length=128)
    pane_id: int = Field(..., ge=0)
    cwd: str = Field(default="", max_length=4096)


def _copy_to_clipboard(text: str, env: dict[str, str]) -> bool:
    """Push `text` to the system clipboard via `wl-copy` (Wayland) or
    `xclip` (X11). Returns True on success, False on any failure. Never
    raises — the resume flow proceeds either way; the frontend gets the
    rendered command in the response as a backup.
    """
    import shutil
    import subprocess

    # Wayland first (the user is on a Wayland session — WAYLAND_DISPLAY is
    # set in the env overlay). xclip works on X11 / Xwayland.
    candidates: list[list[str]] = []
    if shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if shutil.which("xclip"):
        candidates.append(["xclip", "-selection", "clipboard"])
    for argv in candidates:
        try:
            proc = subprocess.run(
                argv,
                input=text.encode("utf-8"),
                env=env,
                timeout=2.0,
                check=False,
                capture_output=True,
            )
            if proc.returncode == 0:
                return True
            logger.debug("clipboard helper %s exited rc=%d: %s",
                         argv[0], proc.returncode, proc.stderr[:200])
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("clipboard helper %s failed: %s", argv[0], exc)
            continue
    return False


def _find_live_wezterm_socket(xdg_runtime_dir: str | None) -> str | None:
    """Return the path to a wezterm-gui socket whose owning PID is alive.

    Scans `$XDG_RUNTIME_DIR/wezterm/gui-sock-*`. Each filename ends in
    the wezterm-gui process's pid; we keep only sockets whose pid
    responds to a `kill -0` liveness check (filters out stale sockets
    left behind by crashed wezterm-gui processes). If multiple GUI
    instances are running, pick the most-recently-modified socket
    (the user's "current" wezterm typically being the one they just
    interacted with).

    Returns None if no live socket exists, leaving wezterm cli to fall
    back to its default discovery.
    """
    import os

    if not xdg_runtime_dir:
        return None
    sock_dir = os.path.join(xdg_runtime_dir, "wezterm")
    if not os.path.isdir(sock_dir):
        return None
    candidates: list[tuple[float, str]] = []
    try:
        names = os.listdir(sock_dir)
    except OSError:
        return None
    for name in names:
        if not name.startswith("gui-sock-"):
            continue
        pid_str = name[len("gui-sock-"):]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        try:
            os.kill(pid, 0)  # signal 0 = liveness check, no actual signal sent
        except (OSError, ProcessLookupError, PermissionError):
            continue
        path = os.path.join(sock_dir, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _notify_dock(env: dict[str, str], summary: str, body: str) -> None:
    """Best-effort `notify-send --urgency=critical` so a hidden wezterm
    window pulses the dock icon. Wayland disallows cross-client window
    raise; this is the strongest attention hint we can issue from
    outside the focused client.

    Crucially, we send the notification AS wezterm (via app-name +
    desktop-entry hint pointing at `org.wezfurlong.wezterm`) so GNOME
    Shell associates the urgency with wezterm's dock icon and pulses
    *that* icon — not a generic LifeOS one. The .desktop file lives at
    `/usr/share/applications/org.wezfurlong.wezterm.desktop` (set by the
    wezterm package).

    Failure (no notify-send, libnotify not running, etc.) is silent on
    purpose — the dock flash is an extra; the underlying spawn/focus
    already succeeded by the time this is called.
    """
    import shutil
    import subprocess

    notify_bin = shutil.which("notify-send")
    if not notify_bin:
        return
    try:
        subprocess.run(  # noqa: S603 — fixed argv
            [
                notify_bin,
                "--urgency=critical",
                "--app-name=org.wezfurlong.wezterm",
                "--hint=string:desktop-entry:org.wezfurlong.wezterm",
                "--icon=org.wezfurlong.wezterm",
                summary,
                body,
            ],
            env=env,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _inject_wezterm_pane(env: dict[str, str]) -> None:
    """Set $WEZTERM_PANE in `env` to an existing pane id, so `wezterm cli
    spawn` knows which window to add the new tab to. No-op if WEZTERM_PANE
    is already set, if wezterm isn't running, or if the probe fails — in
    those cases the spawn will surface its own error.

    Pane selection: prefer panes in the `default` workspace (the user's
    primary window), fall back to any pane. We deliberately don't store
    the chosen window — wezterm's mux state is the source of truth, and
    pane ids reset on wezterm-gui restart.
    """
    import json
    import shutil
    import subprocess

    if env.get("WEZTERM_PANE"):
        return
    wezterm_bin = shutil.which("wezterm")
    if not wezterm_bin:
        return
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            [wezterm_bin, "cli", "list", "--format", "json"],
            env=env,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    if proc.returncode != 0:
        return
    try:
        panes = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return
    if not isinstance(panes, list) or not panes:
        return

    preferred = next(
        (p for p in panes
         if isinstance(p, dict) and p.get("workspace") == "default" and "pane_id" in p),
        None,
    )
    chosen = preferred or next(
        (p for p in panes if isinstance(p, dict) and "pane_id" in p),
        None,
    )
    if chosen is None:
        return
    env["WEZTERM_PANE"] = str(chosen["pane_id"])


def _resume_env() -> dict[str, str]:
    """Build the environment dict for the resume subprocess.

    Inherits from the FastAPI process env (typical systemd-imported env),
    then layers in key=value lines from LIFEOS_CC_RESUME_ENV_FILE if set.
    The file lets operators pin DISPLAY / XAUTHORITY / WAYLAND_DISPLAY /
    DBUS_SESSION_BUS_ADDRESS explicitly — systemd usually omits them.

    Also derives XDG_RUNTIME_DIR (`/run/user/<uid>`) when missing — it's
    required for wezterm cli to locate the GUI's socket at
    `$XDG_RUNTIME_DIR/wezterm/gui-sock-<pid>`. Without it, `wezterm cli`
    silently auto-starts a headless `wezterm-mux-server` and spawns
    tabs into THAT (invisible to the user). The systemd unit's env
    usually lacks XDG_RUNTIME_DIR even when the env file is set, so
    we always backfill the convention.
    """
    import os
    env: dict[str, str] = dict(os.environ)
    try:
        from config.settings import settings
        path = (settings.cc_resume_env_file or "").strip()
    except Exception:  # noqa: BLE001
        path = ""
    if path:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        except OSError as exc:
            logger.warning("cc_resume_env_file unreadable (%s): %s", path, exc)

    # Backfill XDG_RUNTIME_DIR using the standard `/run/user/<uid>`
    # convention if the env didn't already define it. wezterm cli needs
    # this to find the running GUI's socket.
    if not env.get("XDG_RUNTIME_DIR"):
        uid = os.getuid()
        candidate = f"/run/user/{uid}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate

    # Point wezterm cli directly at the LIVE wezterm-gui socket. Without
    # this, wezterm cli scans `$XDG_RUNTIME_DIR/wezterm/gui-sock-*` and
    # picks a stale socket reference (observed even when only one live
    # socket file exists on disk — wezterm appears to cache historical
    # gui pids from log files). Setting WEZTERM_UNIX_SOCKET bypasses the
    # discovery and forces the connection to the user's actual window.
    if not env.get("WEZTERM_UNIX_SOCKET"):
        socket_path = _find_live_wezterm_socket(env.get("XDG_RUNTIME_DIR"))
        if socket_path:
            env["WEZTERM_UNIX_SOCKET"] = socket_path

    # Prepend $HOME/.local/bin to PATH so the spawned terminal can find
    # user-installed binaries (notably claude itself, which the npm CLI
    # installs there). systemd's lifeos-api unit inherits the bare system
    # PATH (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`)
    # so without this the inner `claude --resume` errors with
    # "No viable candidates found in PATH". The wezterm CLI propagates
    # our env through to the spawned tab, so setting PATH here is
    # sufficient — no template wrapping needed.
    home = env.get("HOME", "")
    if home:
        local_bin = f"{home}/.local/bin"
        current_path = env.get("PATH", "")
        if os.path.isdir(local_bin) and local_bin not in current_path.split(":"):
            env["PATH"] = f"{local_bin}:{current_path}" if current_path else local_bin
    return env


@router.post("/sessions/{session_id}/resume")
async def resume_claude_code_session(
    session_id: str,
    body: CCResumeRequest | None = None,
) -> dict[str, Any]:
    """Spawn a local terminal that re-opens a Claude Code session.

    Only valid for `cc:`-prefixed sessions. Opt-in via
    `LIFEOS_CC_RESUME_ENABLED`. Local-network only — do not expose via
    Tailscale Funnel or the public MCP HTTP transport.
    """
    import shlex
    import subprocess

    if not session_id.startswith("cc:"):
        raise HTTPException(status_code=400, detail="resume is only available for Claude Code sessions")

    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001 — settings should always load
        raise HTTPException(status_code=500, detail=f"settings unavailable: {exc}") from exc

    if not getattr(settings, "cc_resume_enabled", False):
        raise HTTPException(status_code=400, detail="cc resume disabled — set LIFEOS_CC_RESUME_ENABLED=true")
    template = (settings.cc_resume_cmd or "").strip()
    if not template:
        raise HTTPException(status_code=400, detail="LIFEOS_CC_RESUME_CMD is empty")

    from api.services.claude_code import session_ingest as cc

    try:
        bare = cc.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Subagent synthetic ids point at the parent's bare uuid; the operator
    # really wants to resume the parent terminal session, not a sub-slice.
    if ":agent:" in bare:
        bare = bare.split(":agent:", 1)[0]

    # Find the matching jsonl to recover the working directory.
    metas = cc.discover_sessions(
        projects_dir=settings.claude_code_projects_dir,
        lookback_days=max(int(settings.claude_code_lookback_days), 365),  # widen lookback for resume
    )
    target = next((m for m in metas if m.raw_session_id == bare), None)
    if target is None or not target.decoded_cwd:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found or has no cwd")

    # Render the inner command first so the outer template can substitute
    # `{inner_command}` (wezterm path) AND so we can copy it to the clipboard
    # as a backup if the spawn doesn't actually run it (legacy launcher path).
    inner_template = (settings.cc_resume_inner_cmd or "").strip()
    inner_rendered = (
        inner_template
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
    )

    # Template substitutions on the launcher:
    #   {session_id}     — bare uuid (no cc: prefix)
    #   {cwd}            — decoded project working directory
    #   {session_id_url} — URL-encoded session_id for use inside `warp://`,
    #                      `vscode://` etc. query strings
    #   {cwd_url}        — URL-encoded cwd for the same
    #   {inner_command}  — the rendered inner_command, inserted unquoted so
    #                      shlex.split sees its individual argv tokens (used
    #                      by the default `wezterm cli spawn ... -- {inner_command}`)
    import urllib.parse
    rendered = (
        template
        .replace("{session_id_url}", urllib.parse.quote(bare, safe=""))
        .replace("{cwd_url}", urllib.parse.quote(target.decoded_cwd, safe=""))
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
        .replace("{inner_command}", inner_rendered)
    )
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"resume_cmd parse failed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="resume_cmd resolved to an empty argv")

    env = _resume_env()
    if body and body.extra_env:
        env.update(body.extra_env)

    # `wezterm cli spawn` (the default launcher) needs to know which window
    # to spawn the new tab into. When called from a systemd context there's
    # no $WEZTERM_PANE and wezterm can't probe focus, so it errors out
    # ("--pane-id was not specified and $WEZTERM_PANE is not set"). Probe
    # `wezterm cli list` for an existing pane and pin its id into the env
    # — wezterm then spawns into that pane's window.
    if "wezterm" in argv[0]:
        _inject_wezterm_pane(env)

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv only, no shell=True (explicit shlex.split above)
            argv,
            cwd=target.decoded_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"resume binary not found: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"resume spawn failed: {exc}") from exc

    # Push a cwd-portable form of the inner command to the system
    # clipboard. `claude --resume <id>` only finds the session when run
    # in the matching project directory (claude scopes sessions by cwd),
    # so we wrap with `cd <cwd> && ...` — that way pasting the clipboard
    # into any terminal works. The wezterm path doesn't need this (it
    # already runs with `--cwd target.decoded_cwd`), but the clipboard
    # is a backup for operator-overridden non-wezterm launchers AND
    # for the case where the wezterm tab opened off-screen and the
    # operator just wants to run the command somewhere visible.
    clipboard_text = ""
    if inner_rendered:
        clipboard_text = (
            f"cd {shlex.quote(target.decoded_cwd)} && {inner_rendered}"
            if target.decoded_cwd else inner_rendered
        )
    clipboard_copied = False
    if clipboard_text:
        clipboard_copied = _copy_to_clipboard(clipboard_text, env)

    # Two launcher flavors coexist:
    #   (a) `wezterm cli spawn` exits cleanly with a pane id on stdout — we
    #       want to wait for it, parse the id, store it, and return.
    #   (b) The launcher BECOMES the terminal (rare; not the default) —
    #       communicate() times out, no pane id available, return spawned=True.
    # A 1.5s timeout is plenty for (a) and short enough that the API stays
    # snappy for (b).
    pane_id: int | None = None
    stdout_bytes = b""
    stderr_bytes = b""
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=1.5)
    except subprocess.TimeoutExpired:
        # Long-running launcher — no pane id this turn, but the spawn is
        # in progress; surface what we have.
        response_body = {
            "spawned": True,
            "pid": proc.pid,
            "pane_id": None,
            "command": argv,
            "cwd": target.decoded_cwd,
            "inner_command": clipboard_text or inner_rendered,
            "clipboard_copied": clipboard_copied,
        }
        return response_body

    if proc.returncode != 0:
        detail = (stderr_bytes.decode("utf-8", errors="replace").strip()
                  or f"resume process exited rc={proc.returncode}")
        raise HTTPException(status_code=500, detail=detail)

    # `wezterm cli spawn` prints a single integer (the pane id) on stdout.
    # If the operator overrode cc_resume_cmd to a launcher that doesn't
    # print one, we just skip the focus mapping for this session.
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    if stdout_text:
        first_token = stdout_text.split()[0]
        try:
            pane_id = int(first_token)
        except ValueError:
            pane_id = None

    if pane_id is not None:
        try:
            from api.services.cc_wezterm_store import get_default_store
            get_default_store().upsert(session_id, pane_id, target.decoded_cwd)
        except Exception as exc:  # noqa: BLE001 — store failure is non-fatal
            logger.warning("cc_wezterm_store upsert failed for %s: %s", session_id, exc)

    # Pulse the wezterm dock icon so the operator knows a new tab is
    # waiting — Wayland disallows cross-client window raise, so without
    # this hint the spawn would be invisible if wezterm is hidden.
    pane_label = f"pane {pane_id}" if pane_id is not None else "new tab"
    _notify_dock(
        env,
        "Agent session resumed",
        f"Wezterm opened {pane_label} in {shlex.quote(target.decoded_cwd)}",
    )

    return {
        "spawned": True,
        "pid": proc.pid,
        "pane_id": pane_id,
        "command": argv,
        "cwd": target.decoded_cwd,
        "inner_command": clipboard_text or inner_rendered,
        "clipboard_copied": clipboard_copied,
    }


def _lookup_cc_session_meta(session_id: str):
    """Resolve a `cc:`-prefixed session id back to its discovered SessionMeta.

    Used by /focus to locate the transcript jsonl for the FD probe fallback.
    Returns (meta, bare_id) or raises HTTPException with an appropriate status.

    Searches project dirs directly for `<bare>.jsonl` rather than calling
    `discover_sessions`, which would parse every transcript under
    `~/.claude/projects/` on every Go To cache miss. The /focus caller only
    reads `meta.jsonl_path` and `meta.decoded_cwd`, so we synthesize a
    minimal SessionMeta with just those two fields populated.
    """
    import os
    from pathlib import Path

    from api.services.claude_code.session_ingest import (
        CC_PREFIX,
        SessionMeta,
        decode_project_key,
        validate_session_id,
    )
    from config.settings import settings

    try:
        bare = validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ":agent:" in bare:
        bare = bare.split(":agent:", 1)[0]

    root = Path(os.path.expanduser(str(settings.claude_code_projects_dir)))
    if root.exists() and root.is_dir():
        for proj in root.iterdir():
            if not proj.is_dir():
                continue
            candidate = proj / f"{bare}.jsonl"
            if candidate.exists():
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    mtime = 0.0
                project_key = proj.name
                meta = SessionMeta(
                    session_id=CC_PREFIX + bare,
                    raw_session_id=bare,
                    project_key=project_key,
                    decoded_cwd=decode_project_key(project_key),
                    jsonl_path=str(candidate),
                    mtime=mtime,
                )
                return meta, bare

    raise HTTPException(status_code=404, detail=f"session {session_id} not found")


def _activate_pane(pane_id: int, env: dict[str, str]) -> tuple[bool, str]:
    """Run `wezterm cli activate-pane --pane-id <id>`. Returns (success, detail).

    Raises HTTPException only for unrecoverable errors (wezterm missing,
    timeout). A non-zero rc returns (False, stderr) so the caller can
    decide whether to re-probe.
    """
    import shutil
    import subprocess

    wezterm_bin = shutil.which("wezterm") or "wezterm"
    argv = [wezterm_bin, "cli", "activate-pane", "--pane-id", str(pane_id)]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            argv,
            env=env,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"wezterm not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"wezterm activate-pane timed out: {exc}") from exc

    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr.decode("utf-8", errors="replace").strip()
                   or f"wezterm activate-pane exited rc={proc.returncode}")


@router.post("/cc-pane-bind")
async def cc_pane_bind(request: Request, body: CCPaneBindRequest) -> dict[str, Any]:
    """Localhost-only endpoint called by the Claude Code SessionStart hook.

    Writes `session_id → pane_id` into `data/cc_wezterm.db` at the moment a
    `claude` invocation starts inside a wezterm pane, so the /agents
    Focus / Go To button can later target the pane authoritatively without
    the FD-probe fallback. The session_id from the hook payload is the
    bare uuid; we prefix with `cc:` to match the store's keying convention.

    Bound to 127.0.0.1 only — never expose this endpoint publicly. Anyone
    who can reach it could rewrite the pane mapping for any session.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="cc-pane-bind is localhost-only")

    from api.services.claude_code import session_ingest as cc

    try:
        bare = cc.validate_session_id(body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Storage convention: keys are always `cc:`-prefixed (matches /resume's
    # upsert). `validate_session_id` strips the prefix from the request, so
    # we re-prefix unconditionally.
    storage_id = f"cc:{bare}"
    cwd = body.cwd or ""

    from api.services.cc_wezterm_store import get_default_store
    mapping = get_default_store().upsert(storage_id, int(body.pane_id), cwd)
    return {
        "bound": True,
        "session_id": storage_id,
        "pane_id": mapping.pane_id,
        "cwd": mapping.cwd,
    }


@router.post("/sessions/{session_id}/focus")
async def focus_claude_code_session(session_id: str) -> dict[str, Any]:
    """Activate the WezTerm pane running this Claude Code session.

    Resolution order:
      1. Cached mapping in `data/cc_wezterm.db` (written by /resume or by
         the SessionStart hook → /cc-pane-bind).
      2. FD-probe fallback (`cc_pane_locate`): walk the session's
         transcript file's holders to a wezterm pane via TTY matching.
         On hit, cache the mapping so subsequent calls are O(1).
      3. If activate-pane fails on a cached mapping (typical: user closed
         the tab), the mapping is cleared and the probe runs once more —
         the session may have been resumed in a fresh pane.

    Returns 404 only when no mapping exists AND probe finds nothing
    (session not running, non-wezterm terminal). Returns 410 when a pane
    existed but is now gone and no replacement can be found.

    Caveat: on GNOME Wayland the compositor disallows cross-client window
    raise, so this reliably switches the tab within wezterm but cannot
    bring a hidden wezterm window to the foreground. A `notify-send`
    follows the activate-pane call to flash the dock icon as a hint.
    """
    import shlex

    if not session_id.startswith("cc:"):
        raise HTTPException(status_code=400, detail="focus is only available for Claude Code sessions")

    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"settings unavailable: {exc}") from exc

    if not getattr(settings, "cc_resume_enabled", False):
        raise HTTPException(status_code=400, detail="cc resume disabled — set LIFEOS_CC_RESUME_ENABLED=true")

    from api.services.cc_wezterm_store import get_default_store
    from api.services import cc_pane_locate

    store = get_default_store()
    env = _resume_env()

    # Resolve session metadata lazily — only needed if cache misses or the
    # cached pane is stale and we need to re-probe.
    meta = None
    def _meta():
        nonlocal meta
        if meta is None:
            meta, _ = _lookup_cc_session_meta(session_id)
        return meta

    def _probe_and_store() -> int | None:
        try:
            m = _meta()
        except HTTPException:
            return None
        if not m.jsonl_path:
            return None
        pane_id = cc_pane_locate.locate_pane_for_transcript(m.jsonl_path, env=env)
        if pane_id is None:
            return None
        store.upsert(session_id, pane_id, m.decoded_cwd or "")
        return pane_id

    mapping = store.get(session_id)
    probed_this_request = False

    if mapping is None:
        # No cache → probe before giving up.
        probed = _probe_and_store()
        probed_this_request = True
        if probed is None:
            raise HTTPException(
                status_code=404,
                detail="no tracked wezterm tab and pane probe found nothing — "
                       "install the SessionStart hook or run Resume",
            )
        mapping = store.get(session_id)
        assert mapping is not None  # _probe_and_store just wrote it

    ok, detail = _activate_pane(mapping.pane_id, env)
    if not ok:
        # Stale mapping — pane was closed, or wezterm restarted and pane
        # ids reset. Clear the mapping; if the cache came from a prior
        # request, re-probe once before giving up.
        store.delete(session_id)
        if probed_this_request:
            raise HTTPException(status_code=410, detail=detail)
        probed = _probe_and_store()
        if probed is None:
            raise HTTPException(status_code=410, detail=detail)
        mapping = store.get(session_id)
        assert mapping is not None
        ok, detail = _activate_pane(mapping.pane_id, env)
        if not ok:
            store.delete(session_id)
            raise HTTPException(status_code=410, detail=detail)

    _notify_dock(
        env,
        "Agent session focused",
        f"Switched wezterm to pane {mapping.pane_id} ({shlex.quote(mapping.cwd)})",
    )

    return {
        "focused": True,
        "pane_id": mapping.pane_id,
        "cwd": mapping.cwd,
    }


async def _stream_claude_code_session(session_id: str, backfill: int):
    """Per-session SSE generator for Claude Code (cc:-prefixed) sessions."""
    from config.settings import settings
    from api.services.claude_code import session_ingest as cc

    yield ": ok\n\n"
    try:
        events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
    except Exception as exc:  # noqa: BLE001
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        return
    tail = events[-backfill:] if backfill and len(events) > backfill else events
    for ev in tail:
        yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"

    line_count = len(events)
    # Track new-events time and heartbeat time separately so the
    # idle-close check actually fires (heartbeats don't postpone close).
    now0 = time.time()
    last_new_event_at = now0
    last_heartbeat_at = now0
    while True:
        await asyncio.sleep(1.0)
        try:
            events = cc.read_normalized_events(session_id, settings.claude_code_projects_dir)
        except Exception:
            events = []
        if len(events) > line_count:
            for ev in events[line_count:]:
                yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"
            line_count = len(events)
            last_new_event_at = time.time()
            last_heartbeat_at = last_new_event_at
        elif time.time() - last_heartbeat_at >= 15.0:
            yield ": heartbeat\n\n"
            last_heartbeat_at = time.time()
        # Claude Code has no DB status — close after 5 minutes of no new
        # transcript events so we don't hold connections forever. Heartbeats
        # do NOT postpone this — only real new events do.
        if time.time() - last_new_event_at > 300.0:
            yield (
                "event: closed\n"
                f"data: {json.dumps({'session_id': session_id, 'status': 'idle'})}\n\n"
            )
            return


@router.get("/sessions/{session_id}/stream")
async def stream_session_transcript(
    session_id: str,
    backfill: int = Query(50, ge=0, le=500),
) -> StreamingResponse:
    """Per-session SSE: backfill last N events, then live-tail the JSONL file.

    Closes cleanly when the session reaches a terminal status.
    Dispatches by `cc:` prefix to the Claude Code ingest path.
    """
    if session_id.startswith("cc:"):
        if not _claude_code_enabled():
            raise HTTPException(status_code=404, detail="claude_code viz disabled")
        try:
            from api.services.claude_code.session_ingest import validate_session_id
            validate_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return StreamingResponse(
            _stream_claude_code_session(session_id, backfill),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    transcript_store = _get_transcript_store()
    session_store = _get_session_store()

    # Validate session_id up-front via the store's path protection — fail fast
    # before opening the streaming response.
    try:
        transcript_store._path(session_id)  # raises ValueError on traversal
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def generate():
        yield ": ok\n\n"
        try:
            events = transcript_store.read(session_id)
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            return
        tail = events[-backfill:] if backfill and len(events) > backfill else events
        for ev in tail:
            yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"

        line_count = len(events)
        last_heartbeat = time.time()
        terminal_check_at = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                events = transcript_store.read(session_id)
            except Exception:
                events = []
            if len(events) > line_count:
                for ev in events[line_count:]:
                    yield f"event: transcript_event\ndata: {json.dumps(ev)}\n\n"
                line_count = len(events)
                last_heartbeat = time.time()
            elif time.time() - last_heartbeat >= 15.0:
                yield ": heartbeat\n\n"
                last_heartbeat = time.time()

            # Check terminal status at most every ~5s to avoid hammering the DB.
            now = time.time()
            if now - terminal_check_at >= 5.0:
                terminal_check_at = now
                try:
                    s = session_store.get_by_session_id(session_id)
                    if s and s.status in TERMINAL_STATUSES:
                        yield (
                            "event: closed\n"
                            f"data: {json.dumps({'session_id': session_id, 'status': s.status})}\n\n"
                        )
                        return
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
