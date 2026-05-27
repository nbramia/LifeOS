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

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.services.agent_worker.session_store import (
    TERMINAL_STATUSES,
    Session,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore

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
    }


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


def _resume_env() -> dict[str, str]:
    """Build the environment dict for the resume subprocess.

    Inherits from the FastAPI process env (typical systemd-imported env),
    then layers in key=value lines from LIFEOS_CC_RESUME_ENV_FILE if set.
    The file lets operators pin DISPLAY / XAUTHORITY / WAYLAND_DISPLAY /
    DBUS_SESSION_BUS_ADDRESS explicitly — systemd usually omits them.
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

    # Template substitutions:
    #   {session_id}     — bare uuid (no cc: prefix)
    #   {cwd}            — decoded project working directory
    #   {session_id_url} — URL-encoded session_id for use inside `warp://`,
    #                      `vscode://` etc. query strings
    #   {cwd_url}        — URL-encoded cwd for the same
    import urllib.parse
    rendered = (
        template
        .replace("{session_id_url}", urllib.parse.quote(bare, safe=""))
        .replace("{cwd_url}", urllib.parse.quote(target.decoded_cwd, safe=""))
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
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

    # Render the inner command (the actual `claude --resume` invocation)
    # so the operator can paste it once the terminal opens — Warp Linux
    # ignores the `&command=` URI param.
    inner_template = (settings.cc_resume_inner_cmd or "").strip()
    inner_rendered = (
        inner_template
        .replace("{session_id}", bare)
        .replace("{cwd}", target.decoded_cwd)
    )
    # Push the rendered inner command directly to the system clipboard
    # via wl-copy / xclip. The browser-side Clipboard API silently fails
    # when the page loses focus (which happens the instant Warp opens),
    # so doing the copy from the server while it still has env access is
    # far more reliable. Failure here is non-fatal — the frontend gets
    # the command in the response and can offer manual copy.
    clipboard_copied = False
    if inner_rendered:
        clipboard_copied = _copy_to_clipboard(inner_rendered, env)

    # Give the spawn a beat. Two flavors of launcher are both valid:
    #   (a) the launcher BECOMES the terminal (long-running) — wait() times
    #       out, we return the running pid.
    #   (b) the launcher is a URL dispatcher that delegates to a running
    #       desktop app and exits cleanly (rc=0). `warp-terminal warp://…`
    #       does exactly this — Warp opens but the launcher process is gone.
    # rc=0 is success regardless of timing; only a non-zero exit is failure.
    response_body = {
        "spawned": True,
        "pid": proc.pid,
        "command": argv,
        "cwd": target.decoded_cwd,
        "inner_command": inner_rendered,
        "clipboard_copied": clipboard_copied,
    }
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        return response_body
    if proc.returncode == 0:
        return response_body
    # Non-zero exit within 0.5s — surface stderr.
    stderr_preview = b""
    try:
        if proc.stderr is not None:
            stderr_preview = proc.stderr.read(1024)
    except Exception:  # noqa: BLE001
        pass
    detail = (stderr_preview.decode("utf-8", errors="replace").strip()
              or f"resume process exited rc={proc.returncode}")
    raise HTTPException(status_code=500, detail=detail)


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
