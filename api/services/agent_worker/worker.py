"""Main poll loop for the LifeOS agent worker.

Per-tick flow:
  - wake any sessions whose sleep timer has expired
  - check the daily spend cap
  - list todo+#agent tasks from the API
  - for each unclaimed candidate: atomic tag swap, preflight, route
    (local → run on Gemma; claude → defer to Issue D; ask/ambiguous → block)
  - on terminal outcomes, swap to the matching #agent-* status tag and
    notify via Telegram

The worker is a stand-alone process (`python -m api.services.agent_worker.worker`)
managed by the `lifeos-agent-worker.service` systemd unit. It does not import
the FastAPI app — all task ops go through `/api/tasks`. This keeps the worker
trivially restartable and lets the API enforce its own locking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

from api.services.agent_worker.completion_signal import has_positive_completion_signal
from api.services.agent_worker.preflight import (
    ROUTE_ASK,
    ROUTE_CLAUDE,
    ROUTE_CLAUDE_CODE,
    ROUTE_CODEX,
    ROUTE_LOCAL,
    ROUTE_REMOTE,
    PreflightResult,
    run_preflight,
)
from api.services.agent_worker.session_store import (
    STATUS_BLOCKED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_YIELDED,
    Session,
    SessionStore,
)
from api.services.agent_worker.managed_executor import _sanitize_title as _managed_sanitize_title
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.conversation_store import ConversationStore
from api.services.interaction_store import build_obsidian_link
from api.services.log_redaction import configure_telegram_log_redaction
from config.settings import settings


# The engine-choice confirmation (#584). Every route offered here except the
# last is free of per-token cost or is the operator's own cheaper remote
# provider — the two CLIs bill the operator's subscriptions, Gemma runs
# on-box, and 'cloud' is the configured remote provider (#809) — so the
# Anthropic-API option is listed last and labelled, and nothing reaches it
# without the operator naming Anthropic/a Claude model specifically.
ROUTING_ASK_QUESTION = (
    "Which engine should run this? "
    "Reply 'claude code' (subscription), 'codex' (subscription), "
    "'local' (on-box Gemma), 'cloud' (remote provider — costs credits), "
    "or 'anthropic'/a Claude model name like 'opus' (Anthropic API — costs credits)."
)

# Engine words accepted in a reply. Ordered longest-first so "claude code"
# matches before the bare "claude" alternative. Each named group maps to the
# routing it selects.
#
# (#809) 'cloud' moved from the `api` group to its own `remote` group: the
# tag means the configured remote provider now, not Anthropic, and a reply
# is held to the same standard as the tag. Reaching the Anthropic API via a
# typed reply now requires 'anthropic', 'api', 'managed', or a model name
# ('opus'/'sonnet'/'haiku') — 'cloud' no longer implies it.
_ROUTING_ANSWER_RE = re.compile(
    r"(?i)"
    r"(?P<claude_code>claude[\s_-]*code)"
    r"|(?P<codex>codex)"
    r"|(?P<local>local|gemma)"
    r"|(?P<remote>cloud|deepseek|fireworks)"
    r"|(?P<api>managed|\banthropic\b|\bapi\b|opus|sonnet|haiku)"
    r"|(?P<bare_claude>claude)"
)
_ROUTING_ANSWER_ROUTES = {
    "claude_code": ROUTE_CLAUDE_CODE,
    "codex": ROUTE_CODEX,
    "local": ROUTE_LOCAL,
    "remote": ROUTE_REMOTE,
    "api": ROUTE_CLAUDE,
    "bare_claude": ROUTE_CLAUDE_CODE,
}


logger = logging.getLogger(__name__)

# Inline-summary cap. Telegram allows ~4096 chars; we leave headroom for
# the worker's header (icon + title + token/cost line) and any footer
# (init-failed MCPs). Above this we spill the body to a vault note and
# put just a 1-line preview + obsidian:// link in the Telegram message.
_INLINE_SUMMARY_MAX_CHARS = 2000

# When a BLOCKED session's reply-prompt (the resume anchor the operator replies
# to) can't be delivered, retry a bounded number of times before escalating —
# rather than leaving the session BLOCKED forever with no way to resume it
# (#402). The delay is a module constant so tests can zero it out.
_BLOCKED_PROMPT_SEND_ATTEMPTS = 3
_BLOCKED_PROMPT_RETRY_DELAY_S = 0.5

# A recurring (cron) schedule stamps its handed-off #agent task with a
# `sched-<id>` tag (see scheduler_store._hand_off_to_agent). The worker reads
# it on completion to append every fire's output to one shared note per
# schedule instead of a new note per fire.
_SCHED_TAG_RE = re.compile(r"^sched-(\w+)$")

# Affirmative replies that lock a proposed [GOAL] (#398). A goal-approval reply
# that isn't affirmative is treated as a refinement and passed back verbatim.
_GOAL_AFFIRMATIVE = {
    "yes", "y", "yep", "yeah", "approve", "approved", "ok", "okay", "k",
    "go", "go ahead", "do it", "ship it", "lock it", "lock", "start",
    "sounds good", "sure", "lgtm", "looks good", "yes please",
}
# Phrases that signal the operator wants changes, not a lock. These take
# precedence over the affirmative prefix check so "yes but make it stricter"
# is treated as a refinement (it must NOT lock the stale condition).
_GOAL_REFINE_SIGNALS = (
    "but ", "however", "instead", "except", "change", "make it", " also ",
    "add ", "remove", "stricter", "actually", "with change", "no,", "don't",
    "rather", "tweak", "adjust",
)


# Filename of the self-restart marker the detached worker-restart primitive
# (`scripts/server.sh restart-worker-detached`) drops next to the session DB
# before bouncing `lifeos-agent-worker` (#401). `resume_pending()` consults it
# on startup: a session named here was killed by a *deliberate* end-of-goal
# restart, not a crash, so it's finalized quietly (COMPLETED, no rollback /
# "could not be safely resumed" notice). The marker is JSON
# (`{"session_ids": [...], "task_ids": [...]}`); either key is honored so the
# primitive can name the doctor's run by whichever id it has on hand.
_SELF_RESTART_MARKER_NAME = "self_restart.json"


def _self_restart_marker_path(db_path: Path) -> Path:
    """Path to the self-restart marker, co-located with the session DB so the
    primitive (a separate process) and the worker agree on its location."""
    from pathlib import Path as _Path  # module-level Path import is TYPE_CHECKING-only
    return _Path(db_path).parent / _SELF_RESTART_MARKER_NAME


def _read_self_restart_marker(db_path: Path) -> tuple[set[str], set[str]]:
    """Read the self-restart marker → (session_ids, task_ids). Missing or
    malformed marker yields empty sets — a corrupt marker must never make a
    real crash look deliberate, so we fail closed (treat nothing as planned)."""
    path = _self_restart_marker_path(db_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return set(), set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("self-restart marker at %s is unparseable; ignoring", path)
        return set(), set()
    sids = {str(s) for s in (data.get("session_ids") or []) if s}
    tids = {str(t) for t in (data.get("task_ids") or []) if t}
    return sids, tids


def _clear_self_restart_marker(db_path: Path) -> None:
    """Remove the self-restart marker. Consumed once per restart, so it's
    deleted after `resume_pending()` honors it — a leftover would otherwise
    quiet a later, unrelated session."""
    try:
        _self_restart_marker_path(db_path).unlink()
    except (FileNotFoundError, OSError):
        pass


def write_self_restart_marker(
    session_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    db_path: Path | str | None = None,
) -> Path:
    """Write the self-restart marker before a deliberate end-of-goal worker
    restart (#401). Called by `scripts/server.sh restart-worker-detached` (via
    `python -m api.services.agent_worker.worker --mark-self-restart …`) so the
    bash primitive and the worker share one marker format. Returns the path
    written. `db_path` defaults to the SessionStore default so the caller
    doesn't need to know where the DB lives.

    The production primitive names the doctor's run by `session_id` (a single-use
    UUID that never recurs), not `task_id`. That matters: a stale marker (one not
    consumed for any reason) naming a session_id can match nothing on a later
    startup, whereas task_ids are vault-stable and could quiet a future real
    crash of a session reusing that id. The `task_ids` path exists for the
    CLI/tests; prefer `session_ids` for anything that writes a real marker."""
    from pathlib import Path as _Path
    from api.services.agent_worker.session_store import DEFAULT_DB_PATH
    resolved = _Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path = _self_restart_marker_path(resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_ids": [s for s in (session_ids or []) if s],
        "task_ids": [t for t in (task_ids or []) if t],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# Telegram reply affordance markers. Every operator-facing session message
# ends with exactly one of these so the operator can tell at a glance whether
# a threaded reply will reach the session (see #458: any anchored message is
# replyable; replies queue as context notes and ride the next turn boundary).
REPLYABLE_FOOTER = "\u21a9\ufe0f reply in thread"
NO_REPLY_FOOTER = "\U0001f6ab do not reply"


def _with_reply_footer(text: str, replyable: bool = True) -> str:
    """Append the reply-affordance footer as the message's final line."""
    return f"{text}\n\n{REPLYABLE_FOOTER if replyable else NO_REPLY_FOOTER}"


def _is_affirmative(text: str) -> bool:
    """True when a goal-approval reply means 'lock it and go' (#398).

    A refinement signal anywhere in the reply wins over the affirmative prefix
    so "yes but make it stricter" / "approve with changes" don't lock the stale
    proposed condition — they fall through to the refinement path."""
    t = text.strip().lower()
    if any(sig in t for sig in _GOAL_REFINE_SIGNALS):
        return False
    t = t.rstrip("!. ")
    return t in _GOAL_AFFIRMATIVE or t.startswith("yes") or t.startswith("approve")


def _slugify(text: str) -> str:
    """Lowercase, hyphenated, filesystem-safe slug capped at 60 chars.
    Returns '' for empty/symbol-only input."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()[:60]


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a leading `---\\n...\\n---` YAML frontmatter block from the body.
    Returns (frontmatter_with_delimiters, body). When there's no frontmatter,
    returns ('', text) so the whole document is treated as body."""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\n.*?\n---\n?", text, re.DOTALL)
    if not m:
        return "", text
    return text[: m.end()], text[m.end():]


def _resolve_json_pointer(doc: Any, pointer: str) -> Any:
    """Minimal RFC 6901 JSON Pointer resolver — enough for `done_when`'s
    `pointer` field (`/status`, `/nested/field`), no external dependency.
    An empty pointer (or `/`) returns `doc` itself. Raises `KeyError`/
    `IndexError`/`TypeError` for a pointer that doesn't resolve, same as
    dict/list indexing would — the worker's tick treats that as a failed
    check for this card, not a crash."""
    if not pointer or pointer == "/":
        return doc
    cur = doc
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _worker_label(routing: str | None, served_by: str = "") -> str:
    """Telegram-message prefix that names the route — operator wants to
    know at a glance whether a result came from local Gemma or cloud
    Claude. Defaults to the generic "Agent worker" when the routing
    isn't known yet (e.g., startup recovery messages).

    (#699) `served_by` is the model id that actually ran the session when
    it differs from what the routing name implies — currently only the
    flag-gated remote fallback on the "local" route. Empty (the default,
    and every session not on that fallback) leaves the label unchanged —
    report observed, not configured (#658), only when there's something
    to report."""
    if routing == ROUTE_LOCAL:
        label = "Local agent worker"
        if served_by:
            label += f" (remote fallback: {served_by})"
        return label
    if routing == ROUTE_REMOTE:
        label = "Remote agent worker"
        if served_by:
            label += f" ({served_by})"
        return label
    if routing == ROUTE_CLAUDE:
        return "Cloud agent worker"
    return "Agent worker"


# Friendly names for the engine a child session ran on, used in the escalation
# flag (#349) so the operator sees where delegated work actually executed.
_ENGINE_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "claude": "cloud Claude",
    "local": "local Gemma",
    "remote": "remote provider",
}


def _format_token_buckets(
    tokens_in: int,
    cache_creation: int,
    cache_read: int,
    tokens_out: int,
) -> str:
    """Render the four token buckets for the completion message.

    Cache buckets are only included when non-zero so local-path completions
    (which never write or read the prompt cache) collapse to the original
    "N tokens" form. Managed cloud sessions always have at least
    cache_creation populated on first turn.
    """
    parts: list[str] = [f"{tokens_in:,} input"]
    if cache_creation:
        parts.append(f"{cache_creation:,} cached-write")
    if cache_read:
        parts.append(f"{cache_read:,} cached-read")
    parts.append(f"{tokens_out:,} output")
    return " + ".join(parts)


def _is_readable_tool_result(text: str) -> bool:
    """Heuristic: is this tool result useful to dump inline as the
    operator-facing completion body? Skip raw JSON dumps (list_threads,
    gmail_search payloads) and oversized text — those become unreadable
    walls of `{"threads":[…]}` in Telegram. Prefer text-shaped results
    that the agent itself would have written (e.g. a drafted email,
    a short calendar summary)."""
    if not text:
        return False
    head = text.lstrip()[:5]
    if head.startswith(("{", "[")):
        return False  # JSON-shaped — agent should have summarized
    if len(text) > 1500:
        return False  # too big to inline; tool-call summary is friendlier
    return True


def _iter_transcript(path):
    """Yield decoded JSON events from a session transcript, skipping
    malformed lines. Used by recovery helpers to scan without raising."""
    import json as _json
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                yield _json.loads(line)
            except _json.JSONDecodeError:
                continue


AGENT_TAG = "agent"
RUNNING_TAG = "agent-running"
COMPLETED_TAG = "agent-completed"
BLOCKED_TAG = "agent-blocked"
FAILED_TAG = "agent-failed"
BUDGET_EXCEEDED_TAG = "agent-budget-exceeded"

# Task statuses that the worker will pick up for execution. `todo` is the
# everyday-task default; `urgent` (Obsidian Tasks `[!]`) is for high-priority
# items the operator wants run ahead of the queue — both should trigger the
# agent if tagged `#agent`. The list API only accepts a single status per
# request, so we fan out and dedupe.
AGENT_PICKUP_STATUSES = ("todo", "urgent")

# #760: best-effort WIP-branch discovery for an interrupted CLI session — a
# regex over past tool_use transcript events, never a live `git` call.
_WIP_BRANCH_RE = re.compile(r"git\s+(?:switch\s+-c|checkout\s+-b)\s+([A-Za-z0-9._/-]+)")


class _SynchronousPool:
    """A ``submit()``-compatible pool that runs work inline.

    The default CLI dispatch pool is a real ``ThreadPoolExecutor``; tests inject
    this instead so spawned-child dispatch stays deterministic (the work runs
    before ``submit`` returns, so assertions don't race a background thread).
    """

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: D401 - parity with executor API
        pass


class Worker:
    """Single-process poll loop. One instance per process."""

    def __init__(
        self,
        api_base: str | None = None,
        session_store: SessionStore | None = None,
        conversation_store: ConversationStore | None = None,
        transcript_store: TranscriptStore | None = None,
        spend_tracker: SpendTracker | None = None,
        poll_seconds: float | None = None,
        telegram_send=None,  # injectable for tests
        telegram_send_with_id=None,  # injectable; returns list of sent chunk message_ids
        http_client: httpx.Client | None = None,
        preflight_caller=None,    # injectable; defaults to Anthropic Haiku
        local_executor=None,      # injectable LocalExecutor for tests
        remote_executor=None,     # injectable LocalExecutor (remote-forced, #809) for tests
        managed_executor=None,    # injectable ManagedExecutor for tests
        claude_code_executor=None,  # injectable ClaudeCodeExecutor for tests
        codex_executor=None,        # injectable CodexExecutor for tests
        cli_pool=None,              # injectable dispatch pool; tests pass _SynchronousPool
    ) -> None:
        self.api_base = (api_base or os.environ.get("LIFEOS_API_URL", "http://localhost:8000")).rstrip("/")
        self.session_store = session_store or SessionStore()
        # #311: resolves a spawned session back to the web/voice conversation it
        # originated from, so the session's progress + result can be mirrored
        # into that thread (additive — Telegram routing is untouched).
        self.conversation_store = conversation_store or ConversationStore()
        self.transcript_store = transcript_store or TranscriptStore()
        self.spend_tracker = spend_tracker or SpendTracker(
            daily_cap_dollars=settings.agent_daily_cap_dollars,
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.agent_worker_poll_seconds
        self._stop = False
        # Telegram senders default to no-ops. Tests get isolation for free
        # (no risk of a test ever hitting the operator's real chat), and
        # production wiring is explicit in `main()`. The previous default
        # — auto-importing the real Telegram module — leaked a test stub
        # message to a real operator chat once; never again.
        def _noop_telegram(text, chat_id=None, bot=None):
            return False
        def _noop_with_id(text, chat_id=None, bot=None):
            return []
        self._telegram_send = telegram_send if telegram_send is not None else _noop_telegram
        self._telegram_send_with_id = (
            telegram_send_with_id if telegram_send_with_id is not None else _noop_with_id
        )
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=10.0)
        # Human-queue done_when poll (#852) — throttled independently of the
        # main tick interval, which runs far more often (60s) than the
        # default human-queue poll (300s). 0.0 so the very first tick always
        # checks.
        self._last_human_queue_check = 0.0
        self._preflight_caller = preflight_caller  # None → use Anthropic SDK by default
        self._local_executor = local_executor  # lazily instantiated on first use
        self._remote_executor = remote_executor  # lazily instantiated on first #cloud task (#809)
        self._managed_executor = managed_executor  # lazily instantiated on first claude task
        self._claude_code_executor = claude_code_executor  # lazily instantiated on first /claude task
        # Per-bot ClaudeCodeExecutor cache (#348). An orchestration bot (doctor)
        # needs its [NOTIFY]/[CLARIFY] notices routed to its own Telegram bot, so
        # each bot gets an executor whose notification_callback is bound to that
        # bot. Bypassed entirely when a test injects `_claude_code_executor`.
        self._claude_code_executors: dict[str, object] = {}
        self._codex_executor = codex_executor  # lazily instantiated on first /codex task
        # CLI dispatches (claude_code/codex) are long-running subprocesses — both
        # spawned children/operator root-spawns AND top-level #agent tasks routed
        # to a CLI engine (#753) go through this pool via _submit_cli_dispatch.
        # Running them off the tick keeps the poll loop free to claim new tasks,
        # dispatch siblings, and keep servicing sleeps/managed-polling/clarification
        # while a session's subprocess runs (up to its budget wall, 4h by default).
        # Bounded so concurrent subprocesses stay capped; tests inject a
        # _SynchronousPool for deterministic dispatch. `_cli_inflight` guards
        # against a re-scan re-dispatching a session that's been submitted but
        # hasn't yet flipped CLAIMED→RUNNING.
        self._cli_pool = cli_pool or ThreadPoolExecutor(
            max_workers=max(2, 2 * settings.agent_max_concurrent_managed),
            thread_name_prefix="cli-dispatch",
        )
        self._cli_inflight: set[str] = set()
        self._cli_lock = threading.Lock()
        self._warn_deprecated_settings()

    @staticmethod
    def _warn_deprecated_settings() -> None:
        """Log a single warning if deprecated env vars are still set.

        `LIFEOS_AGENT_CONNECTORS` and `LIFEOS_AGENT_EXTRA_MCP_SERVERS` were
        used by the pre-refactor driver to build per-session MCP / connector
        lists. The current driver expects those to live on the agent preset
        (configured in the Anthropic console) and ignores both fields. Operators
        with stale .env files would otherwise silently lose configuration, so
        surface a clear deprecation message at startup.
        """
        deprecated = []
        if getattr(settings, "agent_connectors", "") or "":
            deprecated.append("LIFEOS_AGENT_CONNECTORS")
        if getattr(settings, "agent_extra_mcp_servers", "") or "":
            deprecated.append("LIFEOS_AGENT_EXTRA_MCP_SERVERS")
        if deprecated:
            logger.warning(
                "Deprecated env var(s) set and ignored: %s. MCP servers and "
                "connectors now live on the Managed Agents preset "
                "(LIFEOS_AGENT_PRESET_ID), not in session creation. Remove "
                "these from .env to silence this warning. See "
                "docs/guides/agent-worker-setup.md.",
                ", ".join(deprecated),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Mark the loop for graceful shutdown. Safe to call from a signal."""
        self._stop = True
        # Drop any *queued* CLI dispatches and don't block the caller. Children
        # already running aren't force-killed here — their own watchdog times
        # them out, and on a hard stop systemd's TimeoutStopSec SIGKILLs the
        # process; either way resume_pending() reconciles them on next start.
        # (Pool threads are non-daemon, so a still-running child can delay a
        # natural interpreter exit until its watchdog fires.)
        self._cli_pool.shutdown(wait=False, cancel_futures=True)

    def run(self) -> None:
        """Main loop. Runs until `stop()` is called or the process is killed."""
        logger.info(
            "agent worker starting (api=%s, poll=%ss, daily_cap=$%s)",
            self.api_base, self.poll_seconds, self.spend_tracker.daily_cap_dollars,
        )
        # Recover any sessions left non-terminal by a previous crash before
        # starting fresh poll cycles (issue #100 acceptance: restart-resumable).
        self.resume_pending()
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover — loop guard
                logger.exception("worker tick failed: %s", exc)
            # Sleep in small slices so SIGTERM is honored promptly.
            slept = 0.0
            while slept < self.poll_seconds and not self._stop:
                step = min(1.0, self.poll_seconds - slept)
                time.sleep(step)
                slept += step
        logger.info("agent worker stopped")
        if self._owns_http_client:
            self._http.close()

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def resume_pending(self) -> int:
        """Finalize sessions left non-terminal by a previous crash.

        - STATUS_YIELDED with a `sleeps` row: leave alone — the wake-up loop
          in `tick()` will pick it up at the right time.
        - STATUS_RUNNING / STATUS_CLAIMED / STATUS_BLOCKED: mark FAILED and
          roll the tag back to #agent so the operator can retry. We can't
          safely re-enter a partially-driven LLM conversation without risking
          duplicate side effects (file writes, API calls, etc.).

        Exception — deliberate self-restart (#401): a session named in the
        self-restart marker was killed by an end-of-goal `restart-worker-detached`
        (the doctor restarting the worker after shipping an agent-worker-code
        change), not by a crash. Its final `[NOTIFY]` was already delivered
        before SIGTERM, so it's finalized quietly as COMPLETED — no FAILED
        status, no #agent rollback, no "could not be safely resumed" notice.
        """
        pending = self.session_store.list_non_terminal()
        # Sessions the detached-restart primitive deliberately killed. Read once
        # and cleared after the loop so a single marker is honored exactly once.
        self_restart_sids, self_restart_tids = _read_self_restart_marker(
            self.session_store.db_path
        )
        if not pending:
            if self_restart_sids or self_restart_tids:
                _clear_self_restart_marker(self.session_store.db_path)
            return 0
        recovered = 0
        for session in pending:
            sid = session.session_id
            # Sleeping sessions are healthy — main loop will wake them.
            if session.status == STATUS_YIELDED:
                continue
            # Blocked sessions are waiting on the user; leave alone.
            if session.status == STATUS_BLOCKED:
                continue
            # Deliberate self-restart: finalize quietly, skip the alarming
            # rollback. The doctor already shipped + notified before the bounce.
            if sid in self_restart_sids or session.task_id in self_restart_tids:
                self.transcript_store.append(
                    sid, "resume_self_restart",
                    {"prior_status": session.status},
                )
                self.session_store.update_status(session.task_id, STATUS_COMPLETED)
                # Mirror the canonical COMPLETED path (see _handle_outcome /
                # _finalize_terminal): advance the vault checkbox to done ([x])
                # AND swap the tag — gated on has_vault_task. Operator-spawned
                # roots and spawned children carry a synthetic task_id with no
                # vault row, so both vault ops are skipped for them (#401 review).
                # The session-row update_status above stays unconditional.
                has_vault_task = session.origin != "operator" and not session.parent_session_id
                if has_vault_task:
                    self._complete_task(session.task_id)
                    self._swap_tag(session.task_id, RUNNING_TAG, COMPLETED_TAG)
                logger.info(
                    "session %s finalized after a deliberate self-restart "
                    "(no rollback notice)", sid,
                )
                recovered += 1
                continue
            # #198: a remote Managed Agents session survives a worker restart —
            # it keeps running on Anthropic's infrastructure, making MCP tool
            # calls with real side effects (task creation, vault writes, sends)
            # long after this rollback tells the operator the task was rolled
            # back. Kill it before finalizing. Best-effort: a kill failure
            # (404, network) must not block the rollback.
            if session.managed_agent_session_id:
                managed = self._get_managed_executor()
                if managed is not None and managed.driver is not None:
                    try:
                        managed.driver.kill_session(
                            session.managed_agent_session_id,
                            reason="worker_restart_rollback",
                        )
                        self.transcript_store.append(
                            sid, "orphan_remote_session_killed", {
                                "managed_agent_session_id": session.managed_agent_session_id,
                            })
                    except Exception as exc:
                        logger.warning(
                            "kill_session %s on restart rollback failed: %s",
                            session.managed_agent_session_id, exc,
                        )
            self.transcript_store.append(sid, "resume_failed", {"prior_status": session.status})
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            # Spawned children belong to a parent's lineage — they have no
            # backing vault task (`spawn_xxx` task_id is synthetic), so
            # tag/status updates are no-ops, and the operator-facing
            # rollback notification should not fire (PR #132 invariant:
            # children's terminal state stays parent-internal).
            if session.parent_session_id:
                recovered += 1
                continue
            self._swap_tag(session.task_id, RUNNING_TAG, AGENT_TAG)
            # Rolling back to #agent — return the vault checkbox to "todo"
            # so the operator sees the task as un-started rather than stuck
            # in "in_progress".
            self._set_task_status(session.task_id, "todo")
            self._notify(
                f"⚠️ {_worker_label(session.routing)}: task left in {session.status!r} from a prior "
                f"run could not be safely resumed — tag rolled back to "
                f"#{AGENT_TAG} for retry. Transcript: "
                f"`data/agent_transcripts/{sid}.jsonl`"
            )
            recovered += 1
        # Consume the marker so it can't quiet a later, unrelated session.
        if self_restart_sids or self_restart_tids:
            _clear_self_restart_marker(self.session_store.db_path)
        if recovered:
            logger.info("rolled back %d non-resumable session(s) on startup", recovered)
        return recovered

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def tick(self) -> int:
        """Process one poll cycle. Returns the number of tasks handled (for tests)."""
        # Use the configured per-task default budget as the "can I afford to
        # start the cheapest task right now?" estimate. Calling with 0.0 would
        # let claims through even at cap=0 — see SpendTracker.can_start_task
        # for the pause semantics.
        estimate = settings.agent_default_budget_dollars
        if not self.spend_tracker.can_start_task(estimate):
            logger.info(
                "daily spend cap reached or paused (cap=$%s, today=$%.2f); skipping poll",
                self.spend_tracker.daily_cap_dollars, self.spend_tracker.today_total(),
            )
            return 0

        # First, resume any sleeping sessions whose wake time has arrived.
        self._wake_sleeping_sessions()
        # Then advance any in-flight Managed Agents sessions one polling step.
        self._poll_managed_sessions()
        # Resume yielded sessions whose children have all reached terminal state.
        self._resume_yielded_for_children()
        # Dispatch any newly-spawned sessions (no #agent task — created via
        # lifeos_agent_spawn). They show up with status=claimed and a routing.
        self._dispatch_spawned_sessions()
        # Resume blocked sessions whose Telegram clarifications have arrived.
        self._process_clarification_answers()
        # Timeout long-unanswered clarifications.
        self._timeout_stale_clarifications()
        # Resolve Human-queue cards whose done_when condition now passes.
        self._process_human_queue()

        candidates = self._list_agent_tasks()
        handled = 0
        for task in candidates:
            task_id = task.get("id")
            if not task_id:
                continue
            # Skip tasks we've already claimed in a previous run.
            if self.session_store.get(task_id) is not None:
                continue
            if not self._claim(task_id):
                continue
            self._dispatch(task)
            handled += 1
        return handled

    def _resume_yielded_for_children(self) -> None:
        """Resume yielded sessions whose listed children have all terminated.

        Local sessions get the children's outputs injected as a new user turn
        and re-enter the executor loop. Managed yield-and-resume is not yet
        supported (session was killed remotely; re-creation with full history
        transfer is a follow-up PR) — for now those land in FAILED with a
        clear reason so the operator can re-tag.
        """
        from api.services.agent_worker.session_store import TERMINAL_STATUSES as _TS
        yielded = self.session_store.list_yielded_waiting_on_children()
        for session in yielded:
            children = session.yield_waiting_for or []
            if not children:
                continue
            child_sessions = self.session_store.list_by_session_ids(children)
            all_done = (
                len(child_sessions) == len(children)
                and all(c.status in _TS for c in child_sessions)
            )
            if not all_done:
                continue

            task = self._fetch_task(session.task_id) or {
                "id": session.task_id, "description": session.task_id,
            }

            # Build the resume turn — same shape for local and cloud, but
            # cloud also pulls each child's final_text (from the managed_cursor
            # cache populated during the children's runs) since the cloud
            # parent's fresh session won't have access to the children's
            # transcripts on its own.
            resume_message = self._build_resume_message(session, task, child_sessions)

            if session.routing == ROUTE_CLAUDE:
                ok = self._resume_cloud_parent(session, task, child_sessions, resume_message)
                if not ok:
                    # _resume_cloud_parent already marked failed + logged.
                    continue
                self.session_store.set_yield_waiting_for(session.task_id, None)
                self.transcript_store.append(
                    session.session_id, "resume_after_children_cloud",
                    {"children": children},
                )
                continue

            # (#809) A remote-routed parent keeps its conversation history in
            # session_store like local does (it's the same LocalExecutor,
            # just pointed at the remote provider) — no fresh-session
            # restatement needed, unlike the cloud/Managed-Agents branch
            # above. Only the executor construction differs.
            if session.routing == ROUTE_REMOTE:
                executor = self._get_remote_executor(caller_session_id=session.session_id)
            else:
                executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                # Append the resume message first so the executor sees it on
                # the next turn; clear yield_waiting_for *after* the executor
                # returns so a crash leaves the session retryable (still
                # yielded with the same children list).
                self.session_store.append_message(session.session_id, "user", resume_message)
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("resume after children crashed for %s: %s",
                                 session.task_id, exc)
                # Leave yield_waiting_for set so the next tick re-attempts.
                self._mark_failed(session, task, f"resume crashed: {exc}")
                continue
            self.session_store.set_yield_waiting_for(session.task_id, None)
            self.transcript_store.append(session.session_id, "resume_after_children", {
                "children": children,
            })
            self._handle_outcome(session, task, outcome)

    def _dispatch_spawned_sessions(self) -> None:
        """Pick up sessions created via lifeos_agent_spawn that have no #agent
        task backing — they show up with status=claimed and an explicit routing.

        CLI children (claude_code/codex) are long-running subprocesses, so they
        run on the bounded `_cli_pool` rather than blocking the tick (#299). The
        `local` route stays inline — it's in-process, GPU-bound, and capped at
        one concurrent session, so a pool wouldn't buy real parallelism.
        """
        claimed = self.session_store.list_by_status(STATUS_CLAIMED)
        for session in claimed:
            # Skip top-level claimed sessions from the #agent tick claim path
            # (those are dispatched by _dispatch). Pick up spawned children
            # (parent set) and operator root-spawns (#235, no parent but
            # origin='operator').
            if not session.parent_session_id and session.origin != "operator":
                continue
            # CLI children already handed to the pool: skip BEFORE draining, so a
            # re-scan in the CLAIMED→RUNNING window doesn't consume (and discard)
            # the session's pending messages while the running dispatch skips them.
            if session.routing in ("claude_code", "codex"):
                with self._cli_lock:
                    if session.session_id in self._cli_inflight:
                        continue
            # The first pending message is the prompt from the parent — drain
            # it so the executor's seeded user turn picks it up.
            pending = self.session_store.drain_pending_messages(session.session_id)
            description = pending[0]["content"] if pending else session.session_id
            task = {"id": session.task_id, "description": description}
            if session.routing == "local":
                executor = self._get_local_executor(caller_session_id=session.session_id)
                try:
                    outcome = executor.execute(session, task)
                except Exception as exc:
                    logger.exception("spawned local execute crashed for %s: %s", session.task_id, exc)
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    continue
                self._handle_outcome(session, task, outcome)
            elif session.routing == "claude":
                managed = self._get_managed_executor()
                if managed is None:
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    self.transcript_store.append(
                        session.session_id, "spawn_failed_no_managed", {},
                    )
                    continue
                try:
                    outcome = managed.start(session, task)
                except Exception as exc:
                    logger.exception("spawned managed start crashed for %s: %s", session.task_id, exc)
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                    continue
                if outcome.status == STATUS_FAILED:
                    self._handle_outcome(session, task, outcome)
                # On RUNNING, let _poll_managed_sessions handle the rest.
            elif session.routing == "claude_code":
                # /claude sessions — operator-spawned via claude_code_spawn.spawn_claude_code_session.
                self._submit_cli_dispatch(session, pending, self._dispatch_claude_code_session)
            elif session.routing == "codex":
                # /codex sessions — operator-spawned via codex_spawn.spawn_codex_session.
                self._submit_cli_dispatch(session, pending, self._dispatch_codex_session)

    def _submit_cli_dispatch(self, session, pending, dispatch_fn) -> None:
        """Run a claude_code/codex dispatch (`_dispatch_claude_code_session` or
        `_dispatch_codex_session`) on the pool instead of inline.

        Shared by two callers: `_dispatch_spawned_sessions` (spawned children
        and operator root-spawns, #299) and `_dispatch` (top-level `#agent`
        tasks routed to a CLI engine, #753) — both hand the same long-running
        subprocess call off the tick thread through the same pool + guard.

        Guards with `_cli_inflight` so a re-scan on the next tick doesn't submit
        the same session twice in the window between submission and the
        executor flipping the row CLAIMED→RUNNING. The session_id is cleared
        when the dispatch finishes (success or failure).
        """
        sid = session.session_id
        with self._cli_lock:
            if sid in self._cli_inflight:
                return
            self._cli_inflight.add(sid)

        # The closure captures the tick-time `session` snapshot; the dispatch
        # only reads immutable fields (ids, routing) and re-reads mutable state
        # from the store, so the snapshot going stale is harmless.
        def _run() -> None:
            try:
                dispatch_fn(session, pending)
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("async CLI dispatch crashed for %s: %s", session.task_id, exc)
                try:
                    self.session_store.update_status(session.task_id, STATUS_FAILED)
                except Exception:
                    logger.exception("failed to mark %s failed after dispatch crash", session.task_id)
            finally:
                with self._cli_lock:
                    self._cli_inflight.discard(sid)

        self._cli_pool.submit(_run)

    def _poll_managed_sessions(self) -> None:
        """Advance all in-flight Managed Agents sessions one polling step."""
        active = self.session_store.list_active_managed()
        if not active:
            return
        managed = self._get_managed_executor()
        if managed is None:
            return  # operator removed credentials between starts; sessions are stuck
        for session in active:
            task = self._fetch_task(session.task_id) or {
                "id": session.task_id,
                "description": session.task_id,
            }
            pre_dollars = session.total_dollars or 0.0
            try:
                outcome = managed.poll(session)
            except Exception as exc:
                logger.exception("managed.poll crashed for %s: %s", session.task_id, exc)
                self._mark_failed(session, task, f"managed poll crashed: {exc}")
                continue
            # Push the per-poll dollar delta into the daily ledger so the
            # global cap reflects in-flight managed cost, not just claim-time
            # estimates.
            refreshed = self.session_store.get(session.task_id)
            delta_dollars = max(0.0, (refreshed.total_dollars or 0.0) - pre_dollars) if refreshed else 0.0
            if delta_dollars > 0:
                self.spend_tracker.record(delta_dollars)
            if outcome.status != STATUS_RUNNING:
                self._handle_outcome(session, task, outcome)

    def _process_clarification_answers(self) -> None:
        """Resume sessions whose Telegram replies arrived.

        The Telegram listener (api/services/telegram.py) deposits user
        replies into `pending_questions.answer`. Each tick we drain any
        answered+unprocessed rows. Two kinds:

        * kind="clarification" — agent asked a question mid-task, session
          is BLOCKED. Inject the answer as a user turn, swap the task tag
          back to `#agent-running`, and re-invoke the executor.

        * kind="followup" — task already completed, operator replied on
          the completion message to continue the thread (e.g., "now turn
          this into a .md"). Reopen the COMPLETED session: append the
          reply as a new user turn, swap `#agent-completed` →
          `#agent-running`, and re-run the executor. The agent retains
          full prior context because the conversation history is
          preserved.

        Special case for routing-ask: when the session's routing is "ask",
        the answer tells us which model to use. We parse "local"/"claude"
        out of the answer text and update session.routing accordingly
        before dispatching.
        """
        answered = self.session_store.list_answered_unprocessed_questions()
        for q in answered:
            session_id = q["session_id"]
            task_id = q["task_id"]
            session = self.session_store.get_by_session_id(session_id)
            if session is None:
                # Stale — session was deleted? Mark processed so we don't loop.
                self.session_store.mark_question_processed(q["id"])
                continue

            answer = q["answer"] or ""
            kind = q.get("kind") or "clarification"

            if kind == "goal_approval":
                self._resume_goal(q, session, answer)
                continue

            if kind == "followup":
                self._resume_as_followup(q, session, answer)
                continue

            # Routing-ask resolution: if session.routing == "ask", the answer
            # should contain "local" or "claude". Update the session's routing
            # before dispatching so the right executor handles the resume.
            if session.routing == "ask":
                resolved = self._parse_routing_answer(answer)
                if resolved is None:
                    # Couldn't parse — re-ask. Mark processed but send a new
                    # clarification asking specifically for the model name.
                    self.session_store.mark_question_processed(q["id"])
                    self.transcript_store.append(session_id, "routing_ask_unparseable", {
                        "answer_chars": len(answer),
                    })
                    self.ask_user_via_telegram(
                        session_id, task_id,
                        "I couldn't tell which engine you wanted. "
                        + ROUTING_ASK_QUESTION,
                    )
                    continue
                self.session_store.set_routing_and_budget(
                    task_id, routing=resolved,
                    budget=session.budget, expected_output=session.expected_output,
                )
                self.transcript_store.append(session_id, "routing_resolved", {
                    "from": "ask", "to": resolved,
                })
                if session.origin == "operator":
                    # Operator root-spawn: flip back to CLAIMED so the
                    # spawned-session dispatch picks it up next tick (it drains
                    # the enqueued prompt as the task description). The #agent
                    # inline path below would look up a non-existent vault task
                    # and lose the prompt.
                    self.session_store.update_status(task_id, STATUS_CLAIMED)
                    self.session_store.mark_question_processed(q["id"])
                    self.transcript_store.append(
                        session_id, "operator_routing_resolved", {"to": resolved},
                    )
                    continue
                # Refresh the session view so downstream code sees the new routing.
                session = self.session_store.get_by_session_id(session_id)

            # Inject the answer as a user message.
            self.session_store.append_message(
                session_id, "user",
                f"(user answered via Telegram) {answer}",
            )
            self.transcript_store.append(session_id, "clarification_answered", {
                "question_id": q["id"],
                "answer_chars": len(answer),
            })
            self._swap_tag(task_id, BLOCKED_TAG, RUNNING_TAG)
            self._set_task_status(task_id, "in_progress")
            self.session_store.update_status(task_id, STATUS_RUNNING)

            task = self._fetch_task(task_id) or {"id": task_id, "description": task_id}
            if session.routing == "local":
                executor = self._get_local_executor(caller_session_id=session_id)
                try:
                    outcome = executor.execute(session, task)
                except Exception as exc:
                    logger.exception(
                        "clarification resume crashed for %s: %s", task_id, exc,
                    )
                    # Leave question unprocessed so retry can be attempted.
                    self._mark_failed(session, task, f"clarification resume crashed: {exc}")
                    continue
                # Only mark processed once the executor returns cleanly — if
                # the worker crashes mid-execute, the question stays open and
                # the next tick re-attempts the resume.
                self.session_store.mark_question_processed(q["id"])
                self._handle_outcome(session, task, outcome)
            elif session.routing == ROUTE_REMOTE:
                # (#809) A "cloud" reply to the engine-choice question resolves
                # here. Same not-configured guard as the tag path in
                # `_dispatch` (never fall through to another engine), just
                # via `_mark_failed` rather than a re-block — mirrors how the
                # sibling "claude"/Managed-Agents branch below handles its
                # own not-configured case in this same resume flow.
                if not settings.remote_llm_configured:
                    self.session_store.mark_question_processed(q["id"])
                    self._mark_failed(
                        session, task,
                        "remote route resolved but remote provider not configured — "
                        "set LIFEOS_REMOTE_LLM_*",
                    )
                    continue
                executor = self._get_remote_executor(caller_session_id=session_id)
                try:
                    outcome = executor.execute(session, task)
                except Exception as exc:
                    logger.exception(
                        "clarification resume crashed for %s: %s", task_id, exc,
                    )
                    self._mark_failed(session, task, f"clarification resume crashed: {exc}")
                    continue
                self.session_store.mark_question_processed(q["id"])
                self._handle_outcome(session, task, outcome)
            elif session.routing == "claude":
                managed = self._get_managed_executor()
                if managed is None:
                    self.session_store.mark_question_processed(q["id"])
                    self._mark_failed(
                        session, task,
                        "claude route resolved but Managed Agents isn't configured",
                    )
                    continue
                try:
                    outcome = managed.start(session, task)
                except Exception as exc:
                    logger.exception(
                        "clarification resume claude.start crashed for %s: %s", task_id, exc,
                    )
                    self._mark_failed(session, task, f"clarification resume crashed: {exc}")
                    continue
                self.session_store.mark_question_processed(q["id"])
                if outcome.status == STATUS_FAILED:
                    self._handle_outcome(session, task, outcome)
                # otherwise: managed session is now running, _poll_managed_sessions takes over
            else:
                # Other managed-side clarification (mid-loop lifeos_agent_user_ask):
                # not yet supported because the original session was killed.
                self.session_store.mark_question_processed(q["id"])
                self.transcript_store.append(
                    session_id, "managed_clarification_resume_unsupported", {},
                )
                self._mark_failed(
                    session, task,
                    "managed clarification resume not yet supported",
                )

    def _resume_goal(self, q: dict, session: Session, answer: str) -> None:
        """Operator replied to a proposed [GOAL] (#398). On approval, inject
        `/goal <condition>` so Claude Code's native goal mode is armed for the
        resumed session; on a refinement (non-affirmative) reply, pass the raw
        answer back so the doctor re-proposes. Mirrors the `claude_code` branch
        of `_resume_as_followup` — enqueue a pending message and flip status to
        CLAIMED so `_dispatch_claude_code_session` drains it and resumes.
        """
        sid = session.session_id
        task_id = session.task_id
        condition = self._pending_goal_condition(sid)
        if _is_affirmative(answer):
            if condition:
                resume_msg = f"/goal {condition}"
                self.transcript_store.append(sid, "claude_code_goal_locked", {
                    "condition_chars": len(condition),
                })
            else:
                # Approved, but the proposed condition couldn't be recovered from
                # the transcript (e.g. it was already locked, or never recorded).
                # Forwarding a bare "yes" would be meaningless to the agent — ask
                # it to re-propose so the operator can approve a real goal.
                resume_msg = (
                    "Approval received, but I couldn't recover the proposed goal "
                    "to lock. Please re-emit the [GOAL] you proposed."
                )
                self.transcript_store.append(sid, "claude_code_goal_lock_failed", {})
        else:
            resume_msg = answer  # refinement — doctor re-proposes
            self.transcript_store.append(sid, "claude_code_goal_refine", {
                "answer_chars": len(answer),
            })
        # Operator root-spawns (#235) have no backing vault task, so skip the
        # tag/status mutations (they would 404).
        if session.origin != "operator":
            self._swap_tag(task_id, BLOCKED_TAG, RUNNING_TAG)
            self._set_task_status(task_id, "in_progress")
        self.session_store.enqueue_message(sid, "operator", resume_msg)
        self.session_store.update_status(task_id, STATUS_CLAIMED)
        self.session_store.mark_question_processed(q["id"])

    def _pending_goal_condition(self, session_id: str) -> str | None:
        """The most recent proposed-but-not-yet-locked [GOAL] condition for a
        session, or None. Scans the transcript: an `awaiting_goal_approval`
        event sets the pending condition; a later `goal_locked` clears it (so a
        second proposal after a refinement supersedes the first)."""
        condition = None
        for ev in self.transcript_store.read(session_id):
            k = ev.get("kind")
            if k == "claude_code_awaiting_goal_approval":
                condition = (ev.get("payload") or {}).get("condition")
            elif k == "claude_code_goal_locked":
                condition = None
        return condition

    def _resume_as_followup(self, q: dict, session: Session, answer: str) -> None:
        """Operator replied to a completion message — reopen the COMPLETED
        session as a follow-up turn. The conversation history is preserved
        so the agent retains full context ("turn this into a .md" works
        because "this" is still in the assistant's prior turn).
        """
        sid = session.session_id
        task_id = session.task_id

        # The task may be parked at any terminal tag — completed, failed, or
        # budget-exceeded are all replyable now. Swap whichever is current
        # back to running. Operator root-spawns (#235) have no backing vault
        # task, so skip the tag/status mutations (they would 404).
        if session.origin != "operator":
            for terminal_tag in (COMPLETED_TAG, FAILED_TAG, BUDGET_EXCEEDED_TAG):
                if self._swap_tag(task_id, terminal_tag, RUNNING_TAG):
                    break
            self._set_task_status(task_id, "in_progress")
        self.session_store.update_status(task_id, STATUS_RUNNING)
        self.transcript_store.append(sid, "followup_received", {
            "question_id": q["id"], "answer_chars": len(answer),
        })

        task = self._fetch_task(task_id) or {"id": task_id, "description": task_id}

        if session.routing in (ROUTE_LOCAL, ROUTE_REMOTE):
            # Surface-neutral prefix: follow-ups arrive from Telegram replies and
            # the web /chat thread view (#236), so don't hardcode "Telegram".
            self.session_store.append_message(
                sid, "user", f"(operator reply) {answer}",
            )
            # (#809) Remote-routed follow-ups resume the same way local ones
            # do — conversation history lives in session_store either way, so
            # only the executor's target LLM client differs.
            if session.routing == ROUTE_REMOTE:
                executor = self._get_remote_executor(caller_session_id=sid)
            else:
                executor = self._get_local_executor(caller_session_id=sid)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("followup local resume crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"followup resume crashed: {exc}")
                self.session_store.mark_question_processed(q["id"])
                return
            self.session_store.mark_question_processed(q["id"])
            self._handle_outcome(session, task, outcome)
            return

        if session.routing == "claude_code":
            # /claude follow-ups. Hand the reply off as a fresh pending
            # message so _dispatch_claude_code_session drains it and resumes
            # via ClaudeCodeExecutor.resume(). Flipping status back to CLAIMED
            # puts the session in the same shape as a freshly-claimed one —
            # the dispatcher's resume branch keys on session.claude_code_session_id
            # being set, so it knows this is a resume rather than first run.
            self.session_store.enqueue_message(sid, "operator", answer)
            self.session_store.update_status(task_id, STATUS_CLAIMED)
            self.session_store.mark_question_processed(q["id"])
            return

        if session.routing == "codex":
            # /codex follow-ups — same pattern as /claude. CodexExecutor.resume()
            # uses the persisted claude_code_session_id (reused column) to invoke
            # `codex exec resume <id>`.
            self.session_store.enqueue_message(sid, "operator", answer)
            self.session_store.update_status(task_id, STATUS_CLAIMED)
            self.session_store.mark_question_processed(q["id"])
            return

        if session.routing == ROUTE_CLAUDE:
            managed = self._get_managed_executor()
            if managed is None or managed.driver is None:
                self._mark_failed(
                    session, task,
                    "followup arrived but Managed Agents isn't configured",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            # Post the operator's reply as a new user turn on the existing
            # managed session. If Anthropic has already GC'd that session
            # (or it was killed), `post_user_message` 404s; we tell the
            # operator their thread can't be resumed cleanly.
            remote_id = session.managed_agent_session_id
            if not remote_id:
                self._mark_failed(
                    session, task,
                    "followup arrived but no managed session id on record",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            try:
                managed.driver.post_user_message(remote_id, answer)
            except Exception as exc:
                # The remote session may have been cleaned up — surface a
                # clear message so the operator can re-create the task if
                # they want a fresh thread.
                logger.warning(
                    "followup post_user_message failed for %s: %s",
                    remote_id, exc,
                )
                self._mark_failed(
                    session, task,
                    f"followup couldn't be delivered to the existing managed "
                    f"session (it may have been cleaned up): {type(exc).__name__}",
                )
                self.session_store.mark_question_processed(q["id"])
                return
            # The next _poll_managed_sessions tick picks it up and we'll
            # send a fresh completion notification when it finishes.
            self.session_store.mark_question_processed(q["id"])
            return

        # Unknown routing — shouldn't happen post-preflight.
        self._mark_failed(session, task, f"followup with unknown routing: {session.routing}")
        self.session_store.mark_question_processed(q["id"])

    @staticmethod
    def _parse_routing_answer(answer: str) -> str | None:
        """Best-effort parse of an engine choice from a free-text Telegram reply.

        Returns a routing constant, or None when nothing matched. Handles
        combined ambiguity+routing replies like "1. John Doe 2. local" by
        scanning anywhere in the text; when several engines are named, the one
        mentioned LAST wins (the operator's most recent statement).

        A bare "claude" resolves to the Claude Code CLI, not the API (#584).
        That is the safe reading of an ambiguous word now that both exist: the
        CLI is subscription-billed, so a misread costs nothing, while the same
        misread in the other direction spends credits the operator didn't
        agree to. Reaching the API takes a word that can only mean the API —
        "anthropic", "api", "managed", or a model name (#809: "cloud" no
        longer qualifies — it resolves to the configured remote provider
        instead, mirroring the `#cloud` tag's own remapped meaning).
        """
        if not answer:
            return None
        last: tuple[int, str] | None = None
        for m in _ROUTING_ANSWER_RE.finditer(answer):
            route = _ROUTING_ANSWER_ROUTES[m.lastgroup]
            if last is None or m.start() >= last[0]:
                last = (m.start(), route)
        return last[1] if last else None

    def _timeout_stale_clarifications(self) -> None:
        """Send a one-time nudge for clarifications older than the configured
        timeout. The task stays at #agent-blocked permanently after that — the
        operator can manually re-tag with #agent to retry.
        """
        timeout_seconds = settings.agent_clarification_timeout_hours * 3600
        cutoff = int(time.time()) - timeout_seconds
        stale = self.session_store.list_timed_out_questions(cutoff)
        for q in stale:
            # Close every stale row, but only nudge for actual clarifications
            # (agent BLOCKED awaiting input). Completion follow-ups
            # (kind='followup', #234) are just replyable notifications; a
            # "re-tag with #agent to retry" nudge is wrong for them. Marking
            # them timed out also keeps stale follow-up rows from accumulating.
            self.session_store.mark_question_timed_out(q["id"])
            if (q.get("kind") or "clarification") != "clarification":
                continue
            self.transcript_store.append(q["session_id"], "clarification_timed_out", {
                "question_id": q["id"],
            })
            stale_session = self.session_store.get_by_session_id(q["session_id"])
            label = _worker_label(stale_session.routing if stale_session else None)
            self._notify(
                f"⏰ {label}: task is still waiting on your reply.\n\n"
                f"Question: {q['question'][:300]}\n\n"
                f"(Task remains at #{BLOCKED_TAG}. Reply to the original "
                f"question to unblock, or re-tag with #{AGENT_TAG} to retry.)"
            )

    def _process_human_queue(self) -> None:
        """Resolve open Human-queue cards whose `done_when` check now passes
        (#852). Throttled to `settings.human_queue_poll_seconds` — `tick()`
        itself may run far more often. Talks to the store through the HTTP
        API, not the in-process TaskManager, matching every other cross-
        process access in this worker (the worker and the API may run on
        different hosts). Never raises: a listing failure, a single card's
        check failure/error, or a resolve failure is logged and skipped —
        the untouched card is picked up again next tick.
        """
        now = time.time()
        if now - self._last_human_queue_check < settings.human_queue_poll_seconds:
            return
        self._last_human_queue_check = now

        try:
            resp = self._http.get(f"{self.api_base}/api/tasks/human-queue")
            resp.raise_for_status()
            cards = resp.json().get("cards", [])
        except Exception as exc:
            logger.warning("human-queue tick: failed to list cards: %s", exc)
            return

        for card in cards:
            done_when = card.get("done_when")
            if not done_when:
                continue
            try:
                passed, check_desc = self._check_human_queue_done_when(done_when)
            except Exception as exc:
                logger.warning(
                    "human-queue tick: done_when check errored for %s: %s", card.get("id"), exc
                )
                continue
            if not passed:
                continue
            try:
                resp = self._http.put(
                    f"{self.api_base}/api/tasks/human-queue/{card['id']}/resolve",
                    json={"note": f"Auto-resolved: {check_desc}"},
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("human-queue tick: failed to resolve %s: %s", card.get("id"), exc)
                continue
            logger.info("human-queue: resolved %s (%s)", card.get("id"), check_desc)

    def _check_human_queue_done_when(self, done_when: dict) -> tuple[bool, str]:
        """Evaluate one card's `done_when`. Returns `(passed, description)`;
        raises on a malformed check or a request/IO error — the caller
        treats that the same as a failed check (card left untouched)."""
        dw_type = done_when.get("type")
        if dw_type == "endpoint":
            path = done_when["path"]
            pointer = done_when.get("pointer", "")
            equals = done_when.get("equals")
            resp = self._http.get(f"{self.api_base}{path}", timeout=5.0)
            resp.raise_for_status()
            value = _resolve_json_pointer(resp.json(), pointer)
            desc = f"endpoint {path}{pointer} == {equals!r}"
            return value == equals, desc
        if dw_type == "file_exists":
            path = done_when["path"]
            return os.path.exists(path), f"file_exists {path}"
        raise ValueError(f"unsupported done_when type {dw_type!r}")

    def ask_user_via_telegram(
        self,
        session_id: str,
        task_id: str,
        question: str,
    ) -> int | None:
        """Send a clarification question via Telegram and record the
        sent_message_id so the reply-thread hook can match it back.

        Returns the Telegram message_id (or None if Telegram isn't configured
        — caller should mark the session blocked anyway since we can't ask).
        """
        try:
            sent_ids = self._telegram_send_with_id(question) or []
        except Exception as exc:
            logger.warning(f"ask_user_via_telegram failed: {exc}")
            return None
        if not sent_ids:
            return None
        self.session_store.create_pending_question(
            session_id=session_id,
            task_id=task_id,
            question=question,
            sent_message_id=sent_ids[0],
            sent_message_ids=sent_ids,
        )
        self.transcript_store.append(session_id, "clarification_sent", {
            "sent_message_id": sent_ids[0],
            "sent_message_ids": sent_ids,
            "question_chars": len(question),
        })
        return sent_ids[0]

    def _wake_sleeping_sessions(self) -> None:
        """Resume any sessions whose `sleeps` row has expired."""

        due = self.session_store.due_sleeps()
        if not due:
            return
        for session_id in due:
            session = self.session_store.get_by_session_id(session_id)
            if session is None:
                # Defensive — stale sleep row referencing a deleted session.
                self.session_store.remove_sleep(session_id)
                continue
            self.session_store.remove_sleep(session_id)
            self.transcript_store.append(session_id, "wake", {})
            # Re-fetch the task description from the API so the conversation
            # context stays accurate (someone may have edited the title).
            task = self._fetch_task(session.task_id) or {"id": session.task_id, "description": ""}
            # (#809) A remote-routed session that slept must wake back onto
            # the remote provider, not silently switch to local Gemma — same
            # conversation-history-based resume, different target client.
            if session.routing == ROUTE_REMOTE:
                executor = self._get_remote_executor(caller_session_id=session.session_id)
            else:
                executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("wake execute failed for %s: %s", session_id, exc)
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
                self._notify(
                    f"⚠️ {_worker_label(session.routing)}: error while resuming sleeping task "
                    f"'{task.get('description', session.task_id)}': {exc}"
                )
                continue
            self._handle_outcome(session, task, outcome)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _list_agent_tasks(self) -> list[dict[str, Any]]:
        """Fetch open `#agent` tasks from the API.

        The list endpoint only filters on a single status string, so we fan
        out across `AGENT_PICKUP_STATUSES` (`todo` + `urgent` by default) and
        dedupe by task id. A task that appears under both statuses in quick
        succession is still claimed once.
        """
        seen: set[str] = set()
        all_tasks: list[dict[str, Any]] = []
        for status in AGENT_PICKUP_STATUSES:
            try:
                resp = self._http.get(
                    f"{self.api_base}/api/tasks",
                    params={"status": status, "tag": AGENT_TAG},
                )
                resp.raise_for_status()
                for task in resp.json().get("tasks", []):
                    tid = task.get("id")
                    if tid and tid not in seen:
                        seen.add(tid)
                        all_tasks.append(task)
            except Exception as exc:
                logger.warning("failed to list agent tasks (status=%s): %s", status, exc)
        return all_tasks

    def _swap_tag(self, task_id: str, from_tag: str, to_tag: str) -> bool:
        try:
            resp = self._http.post(
                f"{self.api_base}/api/tasks/{task_id}/swap-tag",
                params={"from": from_tag, "to": to_tag},
            )
            resp.raise_for_status()
            return bool(resp.json().get("swapped"))
        except Exception as exc:
            logger.warning("swap_tag failed for %s: %s", task_id, exc)
            return False

    def _complete_task(self, task_id: str) -> bool:
        try:
            resp = self._http.put(f"{self.api_base}/api/tasks/{task_id}/complete")
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("complete failed for %s: %s", task_id, exc)
            return False

    def _set_task_status(self, task_id: str, status: str) -> bool:
        """Update the vault checkbox status alongside an `#agent-*` tag swap.

        The operator wants Obsidian's status (the `- [ ]` / `- [/]` / `- [?]`
        checkbox symbol) to track execution state, not just the tag. This
        is fire-and-forget — failure is logged but does not block the
        transition; the tag itself remains the worker's source of truth.
        """
        try:
            resp = self._http.put(
                f"{self.api_base}/api/tasks/{task_id}",
                json={"status": status},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("set_task_status(%s, %s) failed: %s", task_id, status, exc)
            return False

    def _reconcile_vault_terminal(self, session, status: str) -> None:
        """Update the backing #agent vault task when a CLI session ends.

        The ``claude_code`` / ``codex`` dispatch paths don't go through
        ``_handle_outcome`` (which owns vault reconciliation for the local
        and managed routes), so terminal outcomes there must reconcile the
        vault themselves — otherwise a vault-routed ``#agent #claude`` /
        ``#codex`` task is stranded at ``[/]`` / ``#agent-running`` forever
        even though the agent finished.

        Operator-spawned sessions (``origin='operator'``) and spawned
        children (``parent_session_id`` set) have no backing #agent vault
        row, so the complete / swap-tag / set-status calls would 404 — skip
        them. This mirrors ``has_vault_task`` in ``_handle_outcome``.
        """
        has_vault_task = session.origin != "operator" and not session.parent_session_id
        if not has_vault_task:
            return
        task_id = session.task_id
        if status == STATUS_COMPLETED:
            self._complete_task(task_id)  # mark `done` ([x]) in the vault
            self._swap_tag(task_id, RUNNING_TAG, COMPLETED_TAG)
        elif status == STATUS_BUDGET_EXCEEDED:
            self._swap_tag(task_id, RUNNING_TAG, BUDGET_EXCEEDED_TAG)
            self._set_task_status(task_id, "cancelled")
        elif status == STATUS_FAILED:
            self._swap_tag(task_id, RUNNING_TAG, FAILED_TAG)
            self._set_task_status(task_id, "cancelled")

    def _discover_wip_branch(self, session_id: str) -> str | None:
        """Best-effort scan of this session's OWN past transcript for a WIP
        branch the CLI created via ``git switch -c <branch>`` / ``git
        checkout -b <branch>`` (#760). Returns the LAST such branch name
        found (a later branch supersedes an earlier one across resumes), or
        None if none was ever recorded. Read-only — this never runs git
        itself, only greps tool_use events the executor already wrote.
        """
        branch = None
        try:
            for ev in self.transcript_store.read(session_id):
                if ev.get("kind") not in ("claude_code_tool_use", "codex_tool_use"):
                    continue
                payload = ev.get("payload") or {}
                # claude_code_tool_use: {"name": "Bash", "input": {"command": ...}}
                # codex_tool_use: {"type": ..., "preview": "..."}
                command = ""
                if "input" in payload:
                    command = (payload.get("input") or {}).get("command", "") or ""
                elif "preview" in payload:
                    command = payload.get("preview", "") or ""
                if not command:
                    continue
                match = _WIP_BRANCH_RE.search(command)
                if match:
                    branch = match.group(1)
        except Exception as exc:
            logger.warning("wip-branch discovery failed for %s: %s", session_id, exc)
        return branch

    def _handle_cli_interrupted(self, session: Session, outcome, *, bot: str | None = None) -> None:
        """A claude_code/codex session's subprocess reached a nominal
        STATUS_COMPLETED without an earned completion signal (#760,
        ``completion_signal.has_positive_completion_signal``) — treat it as
        interrupted mid-work rather than done.

        Resumable case (a CLI session id was persisted): park it exactly
        like the executor's own BLOCKED outcomes above — register a
        pending_question of ``kind='followup'`` so a threaded reply
        round-trips through ``_resume_as_followup``, which for
        claude_code/codex routing just re-enqueues the reply and flips the
        session to CLAIMED so the next dispatch drains it through
        ``resume()`` on the persisted CLI session id. The vault tag is left
        at ``#agent-running`` (mirrors the CLARIFY/GOAL/PLAN block path,
        which also doesn't swap it) — only the session row moves to BLOCKED
        so ``/agents`` reflects it.

        Fallback (no CLI session id persisted — ``init`` never fired, so
        there is nothing to resume against, or Telegram delivery failed and
        left no reply anchor): fail with the interrupted context preserved
        in the message, same disposition as an undeliverable block prompt.
        """
        sid = session.session_id
        task_id = session.task_id
        final_text = (outcome.final_text or "").strip()
        wip_branch = self._discover_wip_branch(sid)

        self.transcript_store.append(sid, "cli_session_interrupted", {
            "final_text": final_text,
            "final_chars": len(final_text),
            "notifications_sent": outcome.notifications_sent,
            "exit_meta": outcome.exit_meta,
            "wip_branch": wip_branch,
        })

        current = self.session_store.get_by_session_id(sid)
        resumable = bool(current is not None and current.claude_code_session_id)

        parts = [
            "⚠️ Session interrupted mid-work — reply to resume." if resumable
            else "⚠️ Session interrupted mid-work."
        ]
        if wip_branch:
            parts.append(f"WIP branch preserved: `{wip_branch}`.")
        if final_text:
            preview = final_text if len(final_text) <= 500 else final_text[:500] + "…"
            parts.append(f"Last activity:\n\n{preview}")
        if not resumable:
            parts.append(
                "No CLI session id was persisted for this run, so it can't "
                "be resumed automatically — marked failed instead; "
                "re-trigger it to retry."
            )
        message = "\n\n".join(parts)

        def _send(text):
            return self._telegram_send(text, bot=bot) if bot else self._telegram_send(text)

        def _send_with_id(text):
            return (
                self._telegram_send_with_id(text, bot=bot) if bot
                else self._telegram_send_with_id(text)
            )

        if resumable:
            self.session_store.update_status(task_id, STATUS_BLOCKED)
            sent_ids: list = []
            try:
                sent_ids = _send_with_id(_with_reply_footer(message)) or []
            except Exception as exc:
                logger.warning("interrupted-session notice send failed: %s", exc)
            if sent_ids:
                self.session_store.create_pending_question(
                    session_id=sid,
                    task_id=task_id,
                    question=message[:200],
                    sent_message_id=sent_ids[0],
                    sent_message_ids=sent_ids,
                    kind="followup",
                    bot=bot,
                )
                self.transcript_store.append(sid, "cli_interrupted_prompt_registered", {
                    "message_ids": sent_ids, "wip_branch": wip_branch,
                })
                self._mirror_to_conversation(sid, message)
                return
            # Delivery failed after retries would just repeat the same
            # failure — no anchor means the operator can't reply to resume,
            # so a BLOCKED row would sit silent forever. Escalate to
            # failed-with-preserved-context (documented fallback, #760).
            self.transcript_store.append(sid, "cli_interrupted_prompt_undelivered", {})

        self.session_store.update_status(task_id, STATUS_FAILED)
        try:
            _send(_with_reply_footer(message, replyable=False))
        except Exception as exc:
            logger.warning("interrupted-session (unresumable) notice send failed: %s", exc)
        self._mirror_to_conversation(sid, message)
        self._reconcile_vault_terminal(session, STATUS_FAILED)

    # ------------------------------------------------------------------
    # Claim + dispatch
    # ------------------------------------------------------------------

    def _claim(self, task_id: str) -> bool:
        """Atomically swap `#agent` → `#agent-running` and record the session.

        Returns True iff this worker won the race.
        """
        if not self._swap_tag(task_id, AGENT_TAG, RUNNING_TAG):
            return False
        # Sync vault status to in_progress so the operator can see at a
        # glance which tasks are actively being worked on, not just by
        # tag color.
        self._set_task_status(task_id, "in_progress")
        try:
            session = self.session_store.create(
                task_id=task_id,
                status=STATUS_CLAIMED,
            )
            self.transcript_store.append(
                session.session_id,
                "claim",
                {"task_id": task_id, "worker": "agent-worker"},
            )
            return True
        except Exception as exc:
            # Already-claimed by a sibling worker, or DB hiccup. Try to un-do
            # the tag swap so the task remains pickable.
            logger.error("session create failed for %s: %s", task_id, exc)
            if not self._swap_tag(task_id, RUNNING_TAG, AGENT_TAG):
                # Rollback failed — task is stuck at #agent-running. Notify so
                # the operator can intervene before this silently strands work.
                self._notify(
                    f"⚠️ Agent worker: failed to claim task {task_id} and could "
                    f"not roll back tag. Task stuck at #{RUNNING_TAG} — please "
                    f"re-tag manually if you want it retried."
                )
            return False

    def _get_local_executor(self, caller_session_id: str | None = None):
        """Return the local executor configured for `caller_session_id`.

        The inter-agent tool context depends on the caller's identity, so we
        rebuild the tool registry when the caller changes. For tests an
        injected executor wins.
        """
        if self._local_executor is not None:
            return self._local_executor
        from api.services.agent_worker.inter_agent import Caps, InterAgentContext
        from api.services.agent_worker.local_executor import LocalExecutor
        from api.services.agent_worker.tools import ToolRegistry
        ctx = None
        if caller_session_id:
            ctx = InterAgentContext(
                session_store=self.session_store,
                transcript_store=self.transcript_store,
                caller_session_id=caller_session_id,
                caps=Caps(
                    max_spawn_depth=settings.agent_max_spawn_depth,
                    max_descendants_per_root=settings.agent_max_descendants_per_root,
                    max_concurrent_local=settings.agent_max_concurrent_local,
                    max_concurrent_managed=settings.agent_max_concurrent_managed,
                ),
                worker_handle=self,
            )
        registry = ToolRegistry(inter_agent_context=ctx)
        return LocalExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            tool_registry=registry,
        )

    def _get_remote_executor(self, caller_session_id: str | None = None):
        """(#809) Return the executor for `ROUTE_REMOTE` (`#cloud`) tasks —
        a `LocalExecutor` forced onto the configured remote provider via
        `local_executor._remote_only_llm_client`, never the local
        llama-server. Callers must confirm `settings.remote_llm_configured`
        themselves first (`_dispatch`'s `ROUTE_REMOTE` branch parks the task
        otherwise); this method doesn't re-check it.

        Mirrors `_get_local_executor` exactly (same inter-agent context
        wiring, same caller-identity-keyed tool registry) except for which
        LLM client the executor is constructed with — kept as a separate
        cached instance (`self._remote_executor`) rather than a parameter on
        `_get_local_executor`, so an install running both local and #cloud
        tasks doesn't have one execute silently switch the other's target
        client underneath it.
        """
        if self._remote_executor is not None:
            return self._remote_executor
        from api.services.agent_worker.inter_agent import Caps, InterAgentContext
        from api.services.agent_worker.local_executor import LocalExecutor, _remote_only_llm_client
        from api.services.agent_worker.tools import ToolRegistry
        ctx = None
        if caller_session_id:
            ctx = InterAgentContext(
                session_store=self.session_store,
                transcript_store=self.transcript_store,
                caller_session_id=caller_session_id,
                caps=Caps(
                    max_spawn_depth=settings.agent_max_spawn_depth,
                    max_descendants_per_root=settings.agent_max_descendants_per_root,
                    max_concurrent_local=settings.agent_max_concurrent_local,
                    max_concurrent_managed=settings.agent_max_concurrent_managed,
                ),
                worker_handle=self,
            )
        registry = ToolRegistry(inter_agent_context=ctx)
        client, model_name, is_remote = _remote_only_llm_client()
        return LocalExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            tool_registry=registry,
            llm_client=client,
            model_name=model_name,
            is_remote=is_remote,
        )

    def _get_managed_executor(self):
        """Return the cached ManagedExecutor, lazily constructed.

        Returns None if Managed Agents isn't configured — the dispatcher then
        parks the task at #agent-blocked with an operator-facing explanation.

        Required settings (all must be set):
          - `anthropic_api_key` — for the control-plane auth header
          - `agent_preset_id` — `agent_…` ID created in the Anthropic console.
            Holds the model, system prompt, MCP servers, and tools.
          - `agent_environment_id` — `env_…` ID for where tool calls execute
            (cloud container by default; self-hosted sandbox in #111).
        Optional:
          - `agent_vault_id` — `vlt_…` ID supplying OAuth credentials for
            MCP servers declared in the agent preset. Without it, OAuth-
            protected MCPs (Gmail, Slack, etc.) reject the agent's calls.
        """
        if self._managed_executor is not None:
            return self._managed_executor
        api_key = settings.anthropic_api_key
        agent_id = settings.agent_preset_id
        environment_id = settings.agent_environment_id
        if not api_key or not agent_id or not environment_id:
            return None
        from api.services.agent_worker.managed_driver import ManagedAgentsDriver
        from api.services.agent_worker.managed_executor import ManagedExecutor
        driver = ManagedAgentsDriver(api_key=api_key)
        vault_ids = [settings.agent_vault_id] if settings.agent_vault_id else []
        self._managed_executor = ManagedExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            driver=driver,
            agent_id=agent_id,
            environment_id=environment_id,
            vault_ids=vault_ids,
            # Dev iteration may set LIFEOS_AGENT_MANAGED_MODEL_FOR_TESTS to a
            # cheaper model (e.g. Haiku) so the executor's client-side cost
            # accounting matches the model the operator is actually charged
            # for during prompt-engineering runs.
            model=settings.agent_managed_model_for_tests or settings.agent_managed_model,
        )
        return self._managed_executor

    def _cli_subprocess_launch_count(
        self, session_id: str, spawn_kind: str, not_found_kind: str
    ) -> int:
        """How many times a CLI subprocess *actually launched* for this session.

        Defense-in-depth signal for the CLI dispatch fork (#400). The executor
        writes ``spawn_kind`` immediately *before* the spawn call — but on a
        missing-binary misconfig it then writes ``not_found_kind`` and the
        subprocess never launched (no side effects, safe to re-execute). So a raw
        spawn count over-counts: we subtract the not-found events, leaving only
        spawns where a subprocess truly started. A positive result with a NULL CLI
        session id means a subprocess launched but ``init`` never persisted —
        re-executing could repeat side effects.

        Note: a genuine worker crash mid-run is already finalized by
        ``resume_pending()`` at startup (RUNNING/CLAIMED → FAILED before the tick
        loop), so this guard's real role is catching a *non-restart* re-dispatch
        of a session that already launched a subprocess. Append-only transcript
        reads tolerate a missing/empty file (returns []).
        """
        try:
            spawns = 0
            not_found = 0
            for e in self.transcript_store.read(session_id):
                kind = e.get("kind")
                if kind == spawn_kind:
                    spawns += 1
                elif kind == not_found_kind:
                    not_found += 1
            return max(spawns - not_found, 0)
        except Exception as exc:
            # A read failure must not strand dispatch; fall back to the old
            # behavior (treat as not-yet-launched) rather than crash the worker.
            logger.warning("spawn-marker read failed for %s: %s", session_id, exc)
            return 0

    def _mirror_to_conversation(self, session_id: str, text: str) -> None:
        """#311: mirror a web-spawned session's operator-facing output into its
        linked conversation thread (additive — Telegram is untouched).

        No-op when the session isn't linked to a conversation (i.e.
        Telegram-origin), which keeps AC2: Telegram-origin handoffs unaffected.
        Best-effort: a mirror failure only loses the web round-trip, never the
        session's Telegram delivery. The reverse lookup is keyed on the
        per-call session_id, so concurrent sessions can't cross-write threads.
        """
        if not session_id or not text or not text.strip():
            return
        try:
            conv_id = self.conversation_store.get_conversation_id_by_agent_session_id(session_id)
            if conv_id:
                self.conversation_store.add_message(
                    conv_id, "assistant", text.strip(),
                    routing={"agent_session_id": session_id},
                )
        except Exception as exc:
            logger.warning("web-thread mirror failed for %s: %s", session_id, exc)

    def _dispatch_claude_code_session(self, session, pending: list[dict]) -> None:
        """Drive one ``routing='claude_code'`` session through ``ClaudeCodeExecutor``.

        Handles both the fresh-spawn case (``claude_code_session_id`` is NULL
        — call ``execute()``) and the resume case (``claude_code_session_id``
        set — call ``resume(message)`` with ALL drained pending messages
        concatenated in order).
        On a BLOCKED outcome the operator-facing reply prompt is sent via the
        id-capturing Telegram sender and registered in ``pending_questions``
        so a threaded reply round-trips through ``_resume_as_followup``.
        """
        from api.services.agent_worker.claude_code_executor import (
            REASON_AWAITING_CLARIFICATION,
            REASON_AWAITING_GOAL_APPROVAL,
            REASON_AWAITING_PLAN_APPROVAL,
            REASON_KILLED,
        )
        from api.services.agent_worker.claude_code_spawn import parse_claude_code_spawn_payload

        # The owning bot (NULL = primary) routes every operator-facing notice for
        # this session — the streaming [NOTIFY]/[CLARIFY] (via the executor's
        # callback) and the worker-sent block/completion/failure messages below.
        bot = session.bot
        claude_code = self._get_claude_code_executor(bot)
        sid = session.session_id

        # Only thread `bot` when set so the primary path's send signature is
        # byte-identical to before this change (#348). bot=None → primary.
        def _send(text):
            return self._telegram_send(text, bot=bot) if bot else self._telegram_send(text)

        def _send_with_id(text):
            return self._telegram_send_with_id(text, bot=bot) if bot else self._telegram_send_with_id(text)

        # Build the task dict + resume message from drained pending messages.
        # Fresh spawns carry the JSON payload produced by spawn_claude_code_session;
        # resumes carry plain reply text (enqueued by `_resume_as_followup`
        # below or by the Telegram reply hook).
        is_resume = bool(session.claude_code_session_id)

        # Defense-in-depth: don't re-execute a fresh spawn whose subprocess
        # already launched once (#400). The fresh-spawn branch below calls
        # execute() with the original prompt; the CLI session UUID only persists
        # on the `init` event (executor :615), which fires *after* the subprocess
        # spawns. A genuine worker crash mid-run is already handled — resume_pending()
        # finalizes RUNNING/CLAIMED → FAILED at startup, before this tick loop —
        # so the real case this guards is a *non-restart* re-dispatch of a session
        # that already launched a subprocess (init never persisted ⇒ session id
        # still NULL). Re-running could repeat side effects (for a doctor turn:
        # re-file the issue / restart /implement), and there's no UUID to resume
        # with (`-r` needs it), so the only safe disposition is to FAIL it for
        # operator attention: mark FAILED, append an audit event, best-effort
        # notify. The launch count subtracts binary-not-found events so a missing
        # `claude` binary (spawn event written, but no subprocess) is NOT
        # misdiagnosed as a side-effecting interruption — that case re-executes
        # safely once Fix A (terminal-status persistence below) stops the loop.
        # The codex path (_dispatch_codex_session) carries the same guard via the
        # shared _cli_subprocess_launch_count helper (#411).
        prior_launches = self._cli_subprocess_launch_count(
            sid, "claude_code_spawn", "claude_code_binary_not_found"
        )
        if not is_resume and prior_launches:
            self.transcript_store.append(sid, "claude_code_reexecute_averted", {
                "reason": "subprocess launched but init never persisted — not "
                          "re-executing to avoid duplicate side effects; "
                          "re-trigger to retry",
                "prior_launch_count": prior_launches,
            })
            self._record_child_failure_reason(
                session, STATUS_FAILED,
                "not re-executed after prior subprocess launch — possible "
                "duplicate side effects")
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            try:
                _send(_with_reply_footer(
                    "⚠️ A code session: a previous attempt may have already "
                    "started, so it wasn't retried automatically (to avoid "
                    "repeating side effects). It was marked failed — re-trigger "
                    "it if you want to retry.",
                    replyable=False,
                ))
            except Exception as exc:  # best-effort; the surface may be down
                logger.warning("re-execute-averted notify failed: %s", exc)
            self._reconcile_vault_terminal(session, STATUS_FAILED)
            return

        if is_resume:
            # Every drained message rides the resume turn, in order. Draining
            # returns ALL pending rows, and reopen-on-send (#428) makes
            # multi-enqueue likely (e.g. a parent answers twice before the
            # dispatch tick claims the reopened child) — resuming with only
            # pending[0] would silently drop messages `lifeos_agent_send`
            # already acknowledged as delivered.
            resume_message = "\n\n".join(m["content"] for m in pending) if pending else ""
            task: dict = {"id": session.task_id, "description": resume_message}
            self.transcript_store.append(sid, "claude_code_user_prompt", {
                "text": resume_message, "resume": True,
            })
            try:
                outcome = claude_code.resume(session, resume_message)
            except Exception as exc:
                logger.exception("claude_code resume crashed for %s: %s", session.task_id, exc)
                self._record_child_failure_reason(
                    session, STATUS_FAILED, f"claude_code resume crashed: {exc}")
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                return
        else:
            payload = parse_claude_code_spawn_payload(pending[0]["content"]) if pending else {
                "prompt": "", "working_dir": None, "plan_mode": False, "chat_id": None,
            }
            task = {
                "id": session.task_id,
                "description": payload["prompt"],
                "working_dir": payload["working_dir"],
                "plan_mode": payload["plan_mode"],
                "chat_id": payload["chat_id"],
            }
            self.transcript_store.append(sid, "claude_code_user_prompt", {
                "text": payload["prompt"], "resume": False,
            })
            try:
                outcome = claude_code.execute(session, task)
            except Exception as exc:
                logger.exception("claude_code execute crashed for %s: %s", session.task_id, exc)
                self._record_child_failure_reason(
                    session, STATUS_FAILED, f"claude_code execute crashed: {exc}")
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                return

        if outcome.status == STATUS_BLOCKED:
            if outcome.reason == REASON_AWAITING_PLAN_APPROVAL:
                prompt = "Plan ready — reply 'approve' to proceed, 'reject' to cancel, or send feedback to refine."
            elif outcome.reason == REASON_AWAITING_CLARIFICATION:
                # ONE anchored message: the question itself (the executor
                # defers its Telegram delivery to here, like [GOAL]) plus how
                # to answer — the reply must be threaded to THIS message.
                question_body = (outcome.final_text or "").strip()
                instruction = "Answer by replying to this message."
                prompt = (
                    f"{question_body}\n\n{instruction}" if question_body
                    else "Awaiting your reply — reply to this message to continue."
                )
            elif outcome.reason == REASON_AWAITING_GOAL_APPROVAL:
                # ONE anchored message: the goal body (the executor defers its
                # Telegram delivery to here) plus how to answer it. Only a
                # THREADED reply to this message reaches the session — on an
                # orchestration bot a plain chat message spawns a fresh
                # session instead — so the instruction names the mechanics
                # and lives on the same message as the goal it gates.
                goal_body = (outcome.final_text or "").strip()
                instruction = (
                    "Reply to this message with 'yes' to lock this goal and "
                    "start, or with changes to refine it. (Use Telegram's "
                    "Reply on this message — a plain chat message won't "
                    "reach this session.)"
                )
                prompt = f"{goal_body}\n\n{instruction}" if goal_body else instruction
            else:
                prompt = "Awaiting your reply to continue."
            # Goal-approval replies route through `_resume_goal` (which injects
            # `/goal <condition>` on a yes); everything else is a followup (#398).
            kind = "goal_approval" if outcome.reason == REASON_AWAITING_GOAL_APPROVAL else "followup"
            sent_ids: list = []
            for attempt in range(_BLOCKED_PROMPT_SEND_ATTEMPTS):
                try:
                    sent_ids = _send_with_id(_with_reply_footer(prompt)) or []
                except Exception as exc:
                    logger.warning(
                        "code blocked reply prompt send failed (attempt %d/%d): %s",
                        attempt + 1, _BLOCKED_PROMPT_SEND_ATTEMPTS, exc,
                    )
                    sent_ids = []
                if sent_ids:
                    break
                if attempt + 1 < _BLOCKED_PROMPT_SEND_ATTEMPTS and _BLOCKED_PROMPT_RETRY_DELAY_S:
                    time.sleep(_BLOCKED_PROMPT_RETRY_DELAY_S)
            if sent_ids:
                # kind='followup' so _resume_as_followup picks the reply up
                # alongside agent threads (unified routing model, #248). The
                # session.routing == 'code' tells _resume_as_followup which
                # executor branch to take. `bot` scopes the reply match so a
                # doctor reply can't collide with a primary question (#348).
                self.session_store.create_pending_question(
                    session_id=sid,
                    task_id=session.task_id,
                    question=prompt,
                    sent_message_id=sent_ids[0],
                    sent_message_ids=sent_ids,
                    kind=kind,
                    bot=bot,
                )
                self.transcript_store.append(sid, "code_block_prompt_registered", {
                    "reason": outcome.reason, "message_ids": sent_ids,
                })
                return
            # Delivery failed after all retries. Without a sent message id there
            # is no anchor for the operator to reply to, so the session would sit
            # BLOCKED forever, unresumable and silent. Escalate instead: record
            # the undelivered question, mark the session FAILED so recovery and
            # the /agents view reflect reality, and best-effort notify the owning
            # surface that a question is stuck (#402).
            self.transcript_store.append(sid, "code_block_prompt_undelivered", {
                "reason": outcome.reason, "attempts": _BLOCKED_PROMPT_SEND_ATTEMPTS,
            })
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            try:
                _send(_with_reply_footer(
                    "⚠️ A session needs your input, but the question couldn't be "
                    "delivered. It was marked failed — re-trigger it to retry.",
                    replyable=False,
                ))
            except Exception as exc:  # best-effort; the same surface may be down
                logger.warning("blocked-session escalation send failed: %s", exc)
            self._reconcile_vault_terminal(session, STATUS_FAILED)
            return

        if outcome.status == STATUS_COMPLETED:
            # #760: a subprocess exiting cleanly (or emitting a degenerate
            # terminal event) doesn't mean the agent actually finished — it
            # can hit --max-turns or die mid-turn and still land here with a
            # mid-thought final_text and zero notifications. Require an
            # earned signal before treating this as real completion. Spawned
            # children are exempt (parity with the empty-result guard in
            # _handle_outcome): their outcome is consumed by the parent, not
            # surfaced to the operator directly.
            if not session.parent_session_id and not has_positive_completion_signal(
                outcome.final_text, outcome.notifications_sent,
            ):
                self._handle_cli_interrupted(session, outcome, bot=bot)
                return
            # Send the final assistant text (if any) to Telegram with id
            # capture so a threaded reply can resume the session via
            # _resume_as_followup. [NOTIFY] bodies that already streamed
            # during execution are stripped from final_text by the executor.
            # Spawned children (have a parent) stay silent to the operator —
            # the parent relays their findings in its own single completion
            # message (#349); the child's final_text reaches the parent via
            # _child_final_text instead.
            body = outcome.final_text.strip() if outcome.final_text else ""
            if body and not session.parent_session_id:
                try:
                    sent_ids = _send_with_id(_with_reply_footer(body)) or []
                except Exception as exc:
                    logger.warning("code completion send failed: %s", exc)
                    sent_ids = []
                if sent_ids:
                    self.session_store.create_pending_question(
                        session_id=sid,
                        task_id=session.task_id,
                        question=body[:200],
                        sent_message_id=sent_ids[0],
                        sent_message_ids=sent_ids,
                        kind="followup",
                        bot=bot,
                    )
                # #311: also land the final result in the web/voice thread that
                # spawned this session (no-op for Telegram-origin). Gated the same
                # way as the Telegram send above — non-empty, non-child.
                self._mirror_to_conversation(sid, body)
            self.transcript_store.append(sid, "code_handled_completion", {
                "final_chars": len(body),
            })
            self._reconcile_vault_terminal(session, STATUS_COMPLETED)
            # A reply that arrived MID-RUN (status-anchor route, #458) is
            # queued in pending_messages with nothing to deliver it — the
            # dispatch tick only drains CLAIMED sessions. Reopen now that the
            # turn boundary is here, mirroring reopen-on-send (#428): resume
            # needs the persisted CLI id (re-fetch the row — the executor sets
            # it during this very run, so the claim-time snapshot may predate
            # it), and children stay parent-driven.
            if not session.parent_session_id and self.session_store.has_pending_messages(sid):
                current = self.session_store.get_by_session_id(sid)
                if current is not None and current.claude_code_session_id:
                    if session.origin != "operator":
                        for terminal_tag in (COMPLETED_TAG, FAILED_TAG, BUDGET_EXCEEDED_TAG):
                            if self._swap_tag(session.task_id, terminal_tag, RUNNING_TAG):
                                break
                        self._set_task_status(session.task_id, "in_progress")
                    self.session_store.update_status(session.task_id, STATUS_CLAIMED)
                    self.transcript_store.append(sid, "code_reopened_for_pending_messages", {})
            return

        # FAILED / BUDGET_EXCEEDED — surface a brief operator notification.
        # Skip _handle_outcome here (it owns the local/managed routes), but a
        # vault-routed #claude task still needs its tag/checkbox reconciled —
        # operator-spawned /claude sessions have no vault row and are no-ops.
        label = "Code session"
        notice = ""
        if outcome.status == STATUS_BUDGET_EXCEEDED:
            notice = f"⚠️ {label} hit its budget ({outcome.reason})."
        elif outcome.status == STATUS_FAILED and outcome.reason != REASON_KILLED:
            # #379: an operator-killed session must NOT emit a post-kill notice —
            # the operator stopped it deliberately. The row is already FAILED and
            # the kill endpoint owns the operator_killed transcript event; here we
            # just skip the spurious "failed" notice + web mirror. (Status
            # persistence and vault reconciliation below still run.)
            notice = f"⚠️ {label} failed: {outcome.reason}."
        # #431: spawned children stay silent to the operator on failure/budget
        # too — the parent's resume turn carries the child's [failed] /
        # [budget_exceeded] status header, so the notice would be duplicate
        # noise for a session the operator never directly started.
        if notice and not session.parent_session_id:
            # A failed/budget session with a persisted CLI id is resumable
            # (`_resume_as_followup` swaps any terminal tag), so register the
            # notice as a followup anchor and mark it replyable; without the
            # CLI id there is nothing to resume — say so (#458).
            current = self.session_store.get_by_session_id(sid)
            resumable = bool(current is not None and current.claude_code_session_id)
            sent_ids = []
            if resumable:
                try:
                    sent_ids = _send_with_id(_with_reply_footer(notice)) or []
                except Exception as exc:
                    logger.warning("failure notice send (with id) failed: %s", exc)
            if sent_ids:
                self.session_store.create_pending_question(
                    session_id=sid,
                    task_id=session.task_id,
                    question=notice[:200],
                    sent_message_id=sent_ids[0],
                    sent_message_ids=sent_ids,
                    kind="followup",
                    bot=bot,
                )
            else:
                # No registered anchor (unresumable, or id capture failed) —
                # a "reply in thread" footer would be a lie either way.
                _send(_with_reply_footer(notice, replyable=False))
            # #311: mirror the same failure/budget notice into the web/voice
            # thread (no-op for Telegram-origin); a child is never
            # conversation-linked, so the child gate above also keeps this correct.
            self._mirror_to_conversation(sid, notice)
        # #433: persist the failure reason so the parent's resume turn can
        # carry a `reason:` line (no-op for non-children).
        self._record_child_failure_reason(session, outcome.status, outcome.reason)
        # Persist the terminal status to the session ROW (#400). The executor
        # sets RUNNING on launch but doesn't always flip to a terminal status on
        # failure (e.g. the binary-not-found path returns FAILED while leaving the
        # row CLAIMED/RUNNING). _reconcile_vault_terminal only touches the vault,
        # so for an operator/child session (no vault row) it's a no-op — without
        # this the row stays non-terminal and _dispatch_spawned_sessions re-picks
        # it every tick → infinite re-dispatch. update_status is idempotent, so
        # this is harmless for vault sessions the executor already finalized.
        self.session_store.update_status(session.task_id, outcome.status)
        self._reconcile_vault_terminal(session, outcome.status)

    def _send_session_message(self, session, body: str) -> None:
        """Send an operator-facing session message (streamed [NOTIFY] body,
        heartbeat) with reply-anchor registration: the message ends with the
        "reply in thread" footer, its Telegram message id(s) are captured and
        registered against the session, and a threaded reply to it routes back
        into the session as a context note (#458). Falls back to the plain
        one-way sender (no footer — the affordance would be a lie) when id
        capture is unavailable or fails.
        """
        bot = session.bot
        text = _with_reply_footer(body)
        try:
            sent_ids = (
                self._telegram_send_with_id(text, bot=bot) if bot
                else self._telegram_send_with_id(text)
            ) or []
        except Exception as exc:
            logger.warning("session message send (with id) failed for %s: %s",
                           session.task_id, exc)
            sent_ids = []
        if not sent_ids:
            try:
                if bot:
                    self._telegram_send(body, bot=bot)
                else:
                    self._telegram_send(body)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("session message fallback send failed: %s", exc)
            return
        try:
            self.session_store.add_reply_anchors(
                session.session_id, session.task_id, sent_ids, bot=bot,
            )
        except Exception as exc:  # best-effort — a lost anchor only loses
            logger.warning("reply-anchor registration failed for %s: %s",
                           session.task_id, exc)  # the reply route, not the message

    def _get_claude_code_executor(self, bot: str | None = None):
        """Lazy-construct the ClaudeCodeExecutor for /claude sessions.

        Tests inject one via the constructor (returned for every bot). Production
        builds one executor per owning bot, each wired to a notification callback
        bound to that bot so [NOTIFY]/[CLARIFY] bodies stream live to the right
        Telegram surface — the doctor bot's notices go to the doctor bot, not the
        primary (#348). ``bot=None`` is the primary.
        """
        if self._claude_code_executor is not None:
            return self._claude_code_executor
        key = bot or "primary"
        cached = self._claude_code_executors.get(key)
        if cached is not None:
            return cached
        from api.services.agent_worker.claude_code_executor import ClaudeCodeExecutor
        # Primary keeps the original callback (byte-identical to pre-#348). An
        # orchestration bot gets a callback bound to its bot so streaming
        # [NOTIFY]/[CLARIFY] bodies land on its Telegram surface.
        notify = (lambda text: self._telegram_send(text, bot=bot)) if bot else self._telegram_send
        executor = ClaudeCodeExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            notification_callback=notify,
            # Preferred operator sender (#458): captures Telegram ids and
            # registers them as reply anchors so a threaded reply to ANY
            # streamed message routes back into the session. The session's own
            # `bot` picks the surface, so one binding serves every bot.
            operator_send=self._send_session_message,
            # #311: mirror each streamed [NOTIFY]/[CLARIFY]/[GOAL] into the
            # web/voice thread that spawned the session (no-op when unlinked).
            conversation_mirror=self._mirror_to_conversation,
        )
        # CLI dispatch runs on a thread pool, so two same-bot sessions can reach
        # here concurrently. The executor is cheap and stateless (per-session
        # state lives in SessionStore), so the race is benign — setdefault just
        # makes both callers return the same cached instance (#354 review).
        return self._claude_code_executors.setdefault(key, executor)

    def _dispatch_codex_session(self, session, pending: list[dict]) -> None:
        """Drive one ``routing='codex'`` session through ``CodexExecutor``.

        Mirrors ``_dispatch_claude_code_session`` but without plan-mode /
        [CLARIFY] branches — Codex doesn't have those conventions. A
        completed session relays its final agent message to the operator's
        chat and registers it as a follow-up anchor so a threaded reply
        resumes the session.
        """
        from api.services.agent_worker.codex_executor import REASON_KILLED
        from api.services.agent_worker.codex_spawn import parse_codex_spawn_payload

        codex = self._get_codex_executor()
        sid = session.session_id

        is_resume = bool(session.claude_code_session_id)

        # Re-execute guard (#411) — mirrors the claude_code dispatch (#400/#408).
        # The codex executor writes `codex_spawn` immediately before launching the
        # subprocess and only persists the session id on `codex_init`. A spawn with
        # a still-NULL claude_code_session_id (the reused session-id column) means a
        # subprocess launched but init never persisted — a non-restart re-dispatch
        # would re-run the original prompt and repeat side effects. (A genuine crash
        # is finalized by resume_pending() at startup; a missing codex binary is
        # excluded by the launch-count's not-found subtraction.)
        if not is_resume and self._cli_subprocess_launch_count(
            sid, "codex_spawn", "codex_binary_not_found"
        ) > 0:
            self.transcript_store.append(sid, "codex_reexecute_averted", {
                "reason": "subprocess launched without a persisted session id "
                          "(interrupted before init); not re-executing to avoid repeating side effects",
            })
            self._record_child_failure_reason(
                session, STATUS_FAILED,
                "not re-executed after prior subprocess launch — possible "
                "duplicate side effects")
            self.session_store.update_status(session.task_id, STATUS_FAILED)
            try:
                self._telegram_send(
                    "⚠️ A codex session may have already started before it could be "
                    "resumed safely, so it wasn't retried automatically. It's marked "
                    "failed — re-trigger it to retry."
                )
            except Exception as exc:  # best-effort; the surface may be down
                logger.warning("codex re-execute-prevented notify failed: %s", exc)
            self._reconcile_vault_terminal(session, STATUS_FAILED)
            return

        if is_resume:
            # All drained messages ride the resume turn in order — same
            # multi-enqueue rationale as the claude_code dispatch above
            # (a codex child can collect both an operator threaded reply
            # and a parent reopen answer before the tick claims it, #428).
            resume_message = "\n\n".join(m["content"] for m in pending) if pending else ""
            task: dict = {"id": session.task_id, "description": resume_message}
            self.transcript_store.append(sid, "codex_user_prompt", {
                "text": resume_message, "resume": True,
            })
            try:
                outcome = codex.resume(session, resume_message)
            except Exception as exc:
                logger.exception("codex resume crashed for %s: %s", session.task_id, exc)
                self._record_child_failure_reason(
                    session, STATUS_FAILED, f"codex resume crashed: {exc}")
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                return
        else:
            payload = parse_codex_spawn_payload(pending[0]["content"]) if pending else {
                "prompt": "", "working_dir": None, "chat_id": None,
            }
            task = {
                "id": session.task_id,
                "description": payload["prompt"],
                "working_dir": payload["working_dir"],
                "chat_id": payload["chat_id"],
            }
            self.transcript_store.append(sid, "codex_user_prompt", {
                "text": payload["prompt"], "resume": False,
            })
            try:
                outcome = codex.execute(session, task)
            except Exception as exc:
                logger.exception("codex execute crashed for %s: %s", session.task_id, exc)
                self._record_child_failure_reason(
                    session, STATUS_FAILED, f"codex execute crashed: {exc}")
                self.session_store.update_status(session.task_id, STATUS_FAILED)
                return

        if outcome.status == STATUS_COMPLETED:
            # #760: parity with the claude_code gate — a clean exit doesn't
            # mean the agent finished. Codex has no [NOTIFY] convention, so
            # this always falls through to the PR-mention / summary-shape
            # checks. Spawned children are exempt, same rationale as claude_code.
            if not session.parent_session_id and not has_positive_completion_signal(
                outcome.final_text, outcome.notifications_sent,
            ):
                self._handle_cli_interrupted(session, outcome, bot=None)
                return
            # Spawned children (have a parent) stay silent to the operator —
            # the parent relays their findings in its own completion message
            # (#429, the #349 gate the codex path never got). The child's
            # final_text reaches the parent via the codex_completed transcript
            # event / _child_final_text instead. Gating the followup anchor too
            # matters for reopen-on-send (#428): an operator threaded reply and
            # a parent answer must not both enqueue against the same child.
            body = outcome.final_text.strip() if outcome.final_text else ""
            if body and not session.parent_session_id:
                try:
                    sent_ids = self._telegram_send_with_id(body) or []
                except Exception as exc:
                    logger.warning("codex completion send failed: %s", exc)
                    sent_ids = []
                if sent_ids:
                    self.session_store.create_pending_question(
                        session_id=sid,
                        task_id=session.task_id,
                        question=body[:200],
                        sent_message_id=sent_ids[0],
                        sent_message_ids=sent_ids,
                        kind="followup",
                    )
                # #311: mirror the final result into the web/voice thread that
                # spawned this codex session (no-op for Telegram-origin). Codex
                # has no rich [NOTIFY] stream, so this terminal mirror is the
                # whole web round-trip for it. A child is never
                # conversation-linked, so the child gate above also keeps this
                # correct.
                self._mirror_to_conversation(sid, body)
            self.transcript_store.append(sid, "codex_handled_completion", {
                "final_chars": len(body),
            })
            self._reconcile_vault_terminal(session, STATUS_COMPLETED)
            return

        label = "Codex session"
        notice = ""
        if outcome.status == STATUS_BUDGET_EXCEEDED:
            notice = f"⚠️ {label} hit its budget ({outcome.reason})."
        elif outcome.status == STATUS_FAILED and outcome.reason != REASON_KILLED:
            # #379: an operator-killed codex session must NOT emit a post-kill
            # notice — the operator stopped it deliberately. Parity with the
            # claude_code dispatch; status persistence + vault reconciliation
            # below still run.
            notice = f"⚠️ {label} failed: {outcome.reason}."
        # #431: spawned children stay silent to the operator on failure/budget
        # too — parity with the claude_code branch; the parent's resume turn
        # carries the child's terminal status header.
        if notice and not session.parent_session_id:
            self._telegram_send(notice)
            # #311: mirror the same failure/budget notice into the web/voice
            # thread (no-op for Telegram-origin). A child is never
            # conversation-linked, so the child gate above also keeps this correct.
            self._mirror_to_conversation(sid, notice)
        # #433: persist the failure reason for the parent's resume turn —
        # parity with the claude_code branch (no-op for non-children).
        self._record_child_failure_reason(session, outcome.status, outcome.reason)
        # Persist the terminal status to the session row so an operator/child codex
        # session (no vault row → _reconcile_vault_terminal is a no-op for it) can't
        # linger CLAIMED and be re-dispatched every tick (mirrors #408 / #400).
        self.session_store.update_status(session.task_id, outcome.status)
        self._reconcile_vault_terminal(session, outcome.status)

    def _get_codex_executor(self):
        """Lazy-construct the CodexExecutor for /codex sessions."""
        if self._codex_executor is not None:
            return self._codex_executor
        from api.services.agent_worker.codex_executor import CodexExecutor
        self._codex_executor = CodexExecutor(
            session_store=self.session_store,
            transcript_store=self.transcript_store,
            notification_callback=self._telegram_send,
        )
        return self._codex_executor

    def _fetch_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            resp = self._http.get(f"{self.api_base}/api/tasks/{task_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("fetch_task %s failed: %s", task_id, exc)
            return None

    def _dispatch(self, task: dict[str, Any]) -> None:
        """Run preflight + route the task to the appropriate executor."""
        task_id = task["id"]
        title = task.get("description", task_id)
        session = self.session_store.get(task_id)
        if session is None:  # pragma: no cover — _claim just inserted it
            logger.error("no session for claimed task %s — skipping", task_id)
            return
        sid = session.session_id

        # Preflight: budget, routing, ambiguity, sanity.
        try:
            pre: PreflightResult = run_preflight(
                title=title,
                tags=task.get("tags", []),
                caller=self._preflight_caller,
            )
        except Exception as exc:
            logger.exception("preflight crashed for %s: %s", task_id, exc)
            self._mark_failed(session, task, f"preflight crashed: {exc}")
            return

        self.transcript_store.append(sid, "preflight", {
            "routing": pre.routing,
            "routing_reason": pre.routing_reason,
            "expected_output": pre.expected_output,
            "ambiguity": pre.ambiguity.question if pre.ambiguity else None,
            # (#751) A default route demotes ambiguity to advisory rather than
            # discarding it — logged here as context for whoever reads the
            # transcript, not as a blocking question to the operator.
            "demoted_ambiguity": pre.demoted_ambiguity,
            # (#757) A default route also demotes an uncorroborated LLM-chosen
            # route (no title cue backing it) to the configured default —
            # logged here the same way, so the route the model actually
            # picked isn't lost even though it didn't take effect.
            "demoted_routing": pre.demoted_routing,
            # (#803) A default route also demotes a non-fatal sane=False —
            # the model's own inferred "not executable" opinion, never a
            # sane_fatal one — to advisory the same way. Logged here so the
            # opinion isn't lost even though it no longer blocks or parks
            # the task; `pre.sane` already reads True by the time we get
            # here when this fired, so `sane_reason` alone wouldn't show it.
            "demoted_sanity": pre.demoted_sanity,
            "sane": pre.sane,
            "sane_reason": pre.sane_reason,
            "sane_fatal": pre.sane_fatal,
            "budget": {
                "wall_seconds": pre.budget.wall_seconds,
                "max_tokens": pre.budget.max_tokens,
                "max_dollars": pre.budget.max_dollars,
            },
        })

        # Persist routing + budget onto the session row for the executor to see.
        budget_json = {
            "wall_seconds": pre.budget.wall_seconds,
            "max_tokens": pre.budget.max_tokens,
            "max_dollars": pre.budget.max_dollars,
        }
        self.session_store.set_routing_and_budget(
            task_id,
            routing=pre.routing,
            budget=budget_json,
            expected_output=pre.expected_output,
            preset_class=pre.preset_class,
        )
        session = self.session_store.get(task_id)  # refresh

        # Sanity gate (#747). Only a *fatal* sane=False fails the task
        # closed — an empty title, a preflight-call error, or a title the
        # code itself matched as a deterministically destructive shape (see
        # `preflight._DESTRUCTIVE_TITLE_RE`). Everything else is the
        # classifier's own inferred "this isn't executable" opinion, which
        # has been observed ignoring the prompt's "mundane tasks are sane"
        # rule — treating that single cheap-model judgement as authoritative
        # would silently cancel real work, so it's parked below instead,
        # same as an ambiguous task: a wrong inference costs one question,
        # not the task. As of #803, a non-fatal opinion is parked here only
        # when no default route is configured (or the setting is invalid) —
        # `run_preflight` already demoted it to `pre.sane=True` before this
        # function ever sees it when a valid default route IS configured
        # (see `_apply_default_route`), so `pre.sane` below already reads
        # True on that path and neither gate fires; `pre.demoted_sanity`
        # carries the opinion into the transcript event above instead.
        if not pre.sane and pre.sane_fatal:
            self._mark_failed(
                session, task,
                f"preflight flagged task as unsafe to run: {pre.sane_reason}",
            )
            return

        # Ambiguity / ask-routing / non-fatal sanity objection → block on
        # user input (Issue F closes this loop). Each contributes its own
        # sentence so the operator can tell a sanity objection apart from a
        # genuine ambiguity or an engine-choice question at a glance, rather
        # than seeing one generic "blocked" string.
        question_parts: list[str] = []
        if not pre.sane:
            question_parts.append(
                f"Preflight flagged this task as possibly not executable: "
                f"{pre.sane_reason} Reply to confirm it should run as-is, "
                f"or edit/retag the task in the vault."
            )
        if pre.ambiguity:
            question_parts.append(pre.ambiguity.question)
        if pre.routing == ROUTE_ASK:
            question_parts.append(ROUTING_ASK_QUESTION)
        if question_parts:
            self._mark_blocked(session, task, " ".join(question_parts))
            return

        # Local route: run the executor.
        if pre.routing == ROUTE_LOCAL:
            executor = self._get_local_executor(caller_session_id=session.session_id)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("local executor crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"executor crashed: {exc}")
                return
            self._handle_outcome(session, task, outcome)
            return

        # Remote route (#809, `#cloud` tag): the configured remote
        # OpenAI-compatible provider — never the Anthropic API. If it isn't
        # configured, park the task rather than falling through to any other
        # engine; the operator's standing rule is that the Anthropic API is
        # never a hidden fallback, and silently running on local Gemma
        # instead would just as surely defeat what `#cloud` was asked for.
        if pre.routing == ROUTE_REMOTE:
            if not settings.remote_llm_configured:
                self._swap_tag(task_id, RUNNING_TAG, BLOCKED_TAG)
                self._set_task_status(task_id, "blocked")
                self.session_store.update_status(task_id, STATUS_BLOCKED)
                self.transcript_store.append(sid, "remote_not_configured", {})
                self._notify(
                    f"⏸ {_worker_label(ROUTE_REMOTE)}: task '{title}' routed to #cloud but the "
                    f"remote provider not configured — set LIFEOS_REMOTE_LLM_* "
                    f"(LIFEOS_REMOTE_LLM_URL, LIFEOS_REMOTE_LLM_MODEL, "
                    f"LIFEOS_REMOTE_LLM_API_KEY) in .env, then retag with #{AGENT_TAG}."
                )
                return
            executor = self._get_remote_executor(caller_session_id=session.session_id)
            try:
                outcome = executor.execute(session, task)
            except Exception as exc:
                logger.exception("remote executor crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"executor crashed: {exc}")
                return
            self._handle_outcome(session, task, outcome)
            return

        # Claude route: hand off to Managed Agents.
        if pre.routing == ROUTE_CLAUDE:
            managed = self._get_managed_executor()
            if managed is None:
                # Operator hasn't configured Managed Agents (no API key or vault).
                self._swap_tag(task_id, RUNNING_TAG, BLOCKED_TAG)
                self._set_task_status(task_id, "blocked")
                self.session_store.update_status(task_id, STATUS_BLOCKED)
                self.transcript_store.append(sid, "managed_not_configured", {})
                self._notify(
                    f"⏸ {_worker_label(ROUTE_CLAUDE)}: task '{title}' routed to Claude but "
                    f"Managed Agents isn't configured. Set ANTHROPIC_API_KEY, "
                    f"LIFEOS_AGENT_PRESET_ID, and LIFEOS_AGENT_ENVIRONMENT_ID "
                    f"in .env (see docs/guides/agent-worker-setup.md for the "
                    f"console flow), then retag with #{AGENT_TAG}."
                )
                return
            try:
                outcome = managed.start(session, task)
            except Exception as exc:
                logger.exception("managed.start crashed for %s: %s", task_id, exc)
                self._mark_failed(session, task, f"managed start crashed: {exc}")
                return
            # Don't finalize on START — the session is running remotely.
            # `_poll_managed_sessions` in the next tick will pick it up.
            if outcome.status == STATUS_FAILED:
                self._handle_outcome(session, task, outcome)
            return

        # CLI routes (#claude → Claude Code, #codex → Codex). Synthesize the
        # pending-message payload that _dispatch_*_session expects so the
        # operator-/claude-style entry path can be reused unchanged. The task
        # title is the prompt; working_dir is picked from the description so
        # the CLI runs inside the relevant project.
        #
        # #753: these subprocesses can run for the session's full budget wall
        # (up to 4h) — calling _dispatch_*_session inline here would block
        # this tick thread for that whole span, starving every other tick
        # responsibility (new claims, sleeping-session wakes, managed
        # polling, clarification processing/timeouts) exactly like a spawned
        # CLI child would. Reuse _submit_cli_dispatch — the same pool +
        # _cli_inflight machinery spawned sessions already use (#299) — so a
        # top-level #agent task gets the identical off-tick treatment.
        if pre.routing in (ROUTE_CLAUDE_CODE, ROUTE_CODEX):
            from api.services.directory_resolver import resolve_working_directory
            working_dir = resolve_working_directory(title)
            payload = {
                "prompt": title,
                "working_dir": working_dir,
                # No originating chat — progress/notify goes via the worker's
                # default Telegram sender like cloud-routed #agent tasks.
                "chat_id": None,
            }
            pending = [{"content": json.dumps(payload)}]
            if pre.routing == ROUTE_CLAUDE_CODE:
                payload["plan_mode"] = False  # /claude expects this key
                pending[0]["content"] = json.dumps(payload)
                self._submit_cli_dispatch(session, pending, self._dispatch_claude_code_session)
            else:
                self._submit_cli_dispatch(session, pending, self._dispatch_codex_session)
            return

        # Should not reach here — routing was validated in preflight.
        self._mark_failed(session, task, f"unknown routing: {pre.routing}")

    # ------------------------------------------------------------------
    # Outcome handling (shared between fresh dispatch and sleep wake-up)
    # ------------------------------------------------------------------

    def _handle_outcome(self, session: Session, task: dict[str, Any], outcome) -> None:
        title = task.get("description", session.task_id)
        sid = session.session_id
        # Spawned children belong to the parent agent's flow — their
        # terminal state is consumed by `_resume_yielded_for_children`.
        # The operator should only ever see Telegram notifications for
        # the ROOT session (the one tied to a #agent task in the vault).
        # Without this guard, a child's failure / completion message
        # leaks to the operator with the parent's internal prompt as
        # the "task description" — confusing and operator-irrelevant.
        is_spawned = bool(session.parent_session_id)
        # Operator root-spawns (#235) are root sessions (no parent) so they DO
        # notify the operator, but they have no backing #agent vault task — the
        # vault mutations (complete / swap-tag / set-status) would 404. Gate
        # those on `has_vault_task`; notifications + follow-up still fire.
        has_vault_task = not is_spawned and session.origin != "operator"

        if outcome.status == STATUS_COMPLETED:
            # Guard against silent "I gave up" completions. When the agent
            # produces no final text AND no side-effect tool was successfully
            # called (no draft, no vault write, no calendar event, etc.), the
            # session is effectively a no-op. Marking it `done` hides the
            # failure — the operator sees `#agent-completed` and assumes work
            # happened. Route these through the failure path instead so the
            # tag becomes `#agent-failed` and the operator can decide whether
            # to retry. Spawned children keep the old behavior; their parent
            # consumes their outcome and decides what to surface.
            #
            # Cost gate: when the agent spent real money, give the benefit of
            # the doubt even if the transcript looks light — Anthropic's events
            # endpoint can lag (see managed_executor._backfill_events_on_terminal),
            # and a session with non-trivial spend almost certainly did real
            # work that produced a side effect we just don't see in the local
            # transcript. The $0.05 floor is roughly "two LLM rounds with cache
            # reads" — below that, an empty result really is no-work.
            refreshed_for_guard = self.session_store.get(session.task_id) or session
            spent = float(refreshed_for_guard.total_dollars or 0.0)
            if (
                not is_spawned
                and not (outcome.final_text or "").strip()
                and not self._had_side_effect_tool_use(sid)
                and spent < 0.05
            ):
                if has_vault_task:
                    self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
                    self._set_task_status(session.task_id, "cancelled")
                recovered = self._recover_result_from_transcript(sid) or (
                    "no tool calls or final text recovered"
                )
                self._notify_terminal(
                    session,
                    f"⚠️ {_worker_label(session.routing, served_by=getattr(outcome, 'served_by', ''))}: "
                    f"task '{title}' returned "
                    f"empty result with no side-effect tool use — marking failed. "
                    f"What the agent did:\n\n{recovered}\n\n"
                    f"Transcript: `data/agent_transcripts/{sid}.jsonl`",
                    label=title,
                )
                return
            if not is_spawned:
                if has_vault_task:
                    self._complete_task(session.task_id)  # mark `done` in the vault
                    # Swap the tag so the task surfaces as #agent-completed for symmetry
                    # with the failed / budget-exceeded / blocked terminal tags. Failure
                    # of the swap is non-critical — _swap_tag logs and the task is
                    # already marked done in the vault.
                    self._swap_tag(session.task_id, RUNNING_TAG, COMPLETED_TAG)
                # Send via the with-id sender so we can match a future Telegram
                # reply to this completion message and resume the task as a
                # follow-up turn (e.g., "now turn this into a .md in my vault").
                body = self._completion_summary(session, task, outcome)
                self._notify_terminal(session, body, label=title)
            else:
                # Child completion — record only; parent picks it up via yield_until.
                self.transcript_store.append(sid, "child_completed_internal", {
                    "parent_session_id": session.parent_session_id,
                    "final_chars": len(outcome.final_text or ""),
                })
            return

        label = _worker_label(session.routing)
        if outcome.status == STATUS_BUDGET_EXCEEDED:
            if not is_spawned:
                if has_vault_task:
                    self._swap_tag(session.task_id, RUNNING_TAG, BUDGET_EXCEEDED_TAG)
                    self._set_task_status(session.task_id, "cancelled")
                self._notify_terminal(
                    session,
                    f"⚠️ {label}: task '{title}' hit its budget ({outcome.reason}). "
                    f"Transcript: `data/agent_transcripts/{sid}.jsonl`",
                    label=title,
                )
            else:
                self.transcript_store.append(sid, "child_budget_exceeded_internal", {
                    "parent_session_id": session.parent_session_id,
                    "reason": outcome.reason or "",
                })
            return

        if outcome.status == STATUS_FAILED:
            if not is_spawned:
                if has_vault_task:
                    self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
                    self._set_task_status(session.task_id, "cancelled")
                self._notify_terminal(
                    session,
                    f"⚠️ {label}: task '{title}' failed: {outcome.reason}. "
                    f"Transcript: `data/agent_transcripts/{sid}.jsonl`",
                    label=title,
                )
            else:
                self.transcript_store.append(sid, "child_failed_internal", {
                    "parent_session_id": session.parent_session_id,
                    "reason": outcome.reason or "",
                })
            return

        if outcome.status == STATUS_YIELDED:
            # Session is sleeping. The transcript already records "sleep".
            # No Telegram on yield — operator only hears about terminal states.
            return

        logger.warning("unhandled outcome status %r for %s", outcome.status, session.task_id)

    def _escalation_note(self, session: Session) -> str:
        """One-line flag naming the engine(s) a session delegated work to (#349).

        Empty when the session spawned no children. When it did, the operator
        gets a single completion message (the children stayed silent), so this
        tells them the work was escalated and where it ran — e.g.
        "⤴️ Escalated to Claude Code (haiku)".
        """
        children = self.session_store.list_sessions(parent_session_id=session.session_id)
        if not children:
            return ""
        labels: list[str] = []
        for child in children:
            engine = _ENGINE_LABELS.get(child.routing, child.routing or "agent")
            if child.routing == "claude_code":
                engine = f"{engine} ({child.claude_code_model or 'opus'})"
            if engine not in labels:
                labels.append(engine)
        return "⤴️ Escalated to " + ", ".join(labels)

    def _completion_summary(self, session: Session, task: dict[str, Any], outcome) -> str:
        refreshed = self.session_store.get(session.task_id) or session
        # Use active seconds (excludes sleeps) so the figure reflects real
        # work, not wall time since the session was first created.
        active_s = int(refreshed.total_active_seconds or 0)
        expected = refreshed.expected_output or "text"
        title = task.get("description", session.task_id)
        label = _worker_label(refreshed.routing or session.routing, served_by=getattr(outcome, "served_by", ""))
        # Four-bucket token breakdown so the operator can see what drove cost.
        # For local sessions cache buckets are always zero and collapse out of
        # the rendered string.
        tokens_summary = _format_token_buckets(
            refreshed.total_input_tokens or 0,
            refreshed.total_cache_creation_tokens or 0,
            refreshed.total_cache_read_tokens or 0,
            refreshed.total_output_tokens or 0,
        )

        # Body: prefer the agent's final text. When it's empty (the agent
        # used a tool and idled without summarizing — sometimes happens with
        # Sonnet on tight-budget tasks), surface a transcript pointer so the
        # operator can inspect what the agent actually did instead of seeing
        # a blank message. The transcript captures every tool call.
        final_text = (outcome.final_text or "").strip()
        if not final_text:
            # The agent did work but never produced a final assistant
            # message. Common with Sonnet on tasks that end with a tool
            # call (e.g., drafting an email via lifeos_gmail_draft and
            # not bothering to summarize after). Surface whatever the
            # last meaningful tool produced so the operator sees the
            # actual result (the draft, the calendar event, etc.) rather
            # than just a transcript pointer.
            recovered = self._recover_result_from_transcript(session.session_id)
            if recovered:
                result_blurb = (
                    f"(no final text from the agent — surfacing the last "
                    f"tool result instead)\n\n{recovered}"
                )
            else:
                result_blurb = (
                    f"(agent idled without a final text reply — check transcript at "
                    f"`data/agent_transcripts/{session.session_id}.jsonl` for tool-use detail)"
                )
        else:
            # Every completed task now lands a durable note in the vault's
            # Agent Output folder — one-off tasks get a new note, recurring
            # (cron-scheduled) tasks append to one shared note per schedule.
            # The agent's system prompts also ask it to create artifacts for
            # long outputs, but the operator-facing record shouldn't depend on
            # the agent following that guidance: the worker always writes.
            written = self._write_agent_output(session, task, final_text)
            if len(final_text) <= _INLINE_SUMMARY_MAX_CHARS:
                # Short answer: show it inline. Append a pointer to the saved
                # note when the write succeeded (vault may be unconfigured).
                result_blurb = final_text
                if written is not None:
                    rel_path, obsidian_url = written
                    result_blurb += (
                        f"\n\nSaved to vault: `{rel_path}`\n"
                        f"[Open in Obsidian]({obsidian_url})"
                    )
            elif written is None:
                # Over-length but vault not configured / write failed —
                # preserve the old behavior so the operator still gets
                # *something* readable instead of an empty message.
                result_blurb = final_text[:_INLINE_SUMMARY_MAX_CHARS] + "…"
            else:
                # Over-length: link to the note instead of truncating mid-answer.
                rel_path, obsidian_url = written
                preview = final_text.split("\n\n", 1)[0].strip()
                if len(preview) > 400:
                    preview = preview[:400].rsplit(" ", 1)[0] + "…"
                # Wrap path + URL in Markdown link form so Telegram (which
                # uses parse_mode=Markdown) doesn't interpret underscores in
                # the path/URL as italic markers. Inline path also gets
                # backticks so it renders as code and stays copy-pasteable.
                result_blurb = (
                    f"{preview}\n\n"
                    f"Full answer saved to vault: `{rel_path}`\n"
                    f"[Open in Obsidian]({obsidian_url})"
                )

        # Footer: when some MCP servers failed to initialize during the session,
        # list them so the operator can fix or remove the broken connectors from
        # the agent preset. Only populated by the managed (cloud) path.
        init_failed = getattr(outcome, "init_failed_mcps", None) or []
        footer = ""
        if init_failed:
            footer = f"\n\nNote: {len(init_failed)} MCP server(s) unavailable this session: {', '.join(init_failed)}"

        escalation = self._escalation_note(session)
        escalation_line = f"\n{escalation}" if escalation else ""

        return (
            f"✅ {label}: completed '{title}' "
            f"({expected}) — {tokens_summary}, ${refreshed.total_dollars:.2f}, "
            f"{active_s}s active.{escalation_line}\n\n{result_blurb}{footer}"
        )

    def _recover_result_from_transcript(self, session_id: str) -> str:
        """When the agent's `final_text` is empty, scan the transcript for
        the last successful tool result and use it as the operator-facing
        body. Caps the recovered text to fit the inline-summary budget
        (anything bigger gets ellipsized — the full content is in the
        transcript). Returns empty string when nothing usable exists.

        Looks at both shapes:
        - Local executor: `tool_call` events with `is_error=False` and a
          non-trivial `output_chars`. The full output isn't recorded in
          the transcript itself; the agent didn't summarize and the
          transcript only captured metadata, so we report what tool was
          called.
        - Managed executor: `managed_event_agent.mcp_tool_result` and
          `managed_event_agent.tool_result` events carry the actual
          textual content in `payload.content[*].text`. Those are the
          interesting ones — when the cloud agent drafts an email, the
          email body lands here.
        """
        import json as _json

        try:
            path = self.transcript_store.dir / f"{session_id}.jsonl"
            if not path.exists():
                return ""
            tool_results: list[tuple[str, str]] = []  # (tool_name, text)
            tool_calls: list[str] = []  # local-executor tool names

            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    kind = d.get("kind", "")
                    payload = d.get("payload", {}) or {}

                    if kind == "tool_call" and not payload.get("is_error"):
                        # Local-executor shape — metadata only, no body
                        tool_calls.append(str(payload.get("tool", "tool")))

                    if kind in (
                        "managed_event_agent.mcp_tool_result",
                        "managed_event_agent.tool_result",
                    ) and not payload.get("is_error"):
                        # Managed-executor shape — body is in content[*].text
                        for c in payload.get("content", []) or []:
                            text = c.get("text") if isinstance(c, dict) else None
                            if text:
                                tool_name = (
                                    payload.get("mcp_tool_use_id")
                                    or payload.get("name")
                                    or "tool"
                                )
                                tool_results.append((tool_name, text))

            # Prefer the last result whose body is human-readable. A raw
            # JSON dump from list_threads / gmail_search is worse than
            # useless inline — operators got a 30KB threads payload as
            # their "completion message" when the agent idled after one
            # of those calls. Surface a tool-call summary instead.
            for tool_name, text in reversed(tool_results):
                if _is_readable_tool_result(text):
                    cap = _INLINE_SUMMARY_MAX_CHARS - 200
                    if len(text) > cap:
                        text = text[:cap].rsplit(" ", 1)[0] + "…"
                    return text

            # All recent tool results were JSON / oversized. Fall through
            # to a compact tool-call list so the operator at least knows
            # what the agent did, with a transcript pointer for full audit.
            managed_tool_names: list[str] = []
            for d in _iter_transcript(self.transcript_store.dir / f"{session_id}.jsonl"):
                kind = d.get("kind", "")
                payload = d.get("payload", {}) or {}
                if kind in ("managed_event_agent.mcp_tool_use",
                            "managed_event_agent.tool_use"):
                    managed_tool_names.append(
                        str(payload.get("name") or payload.get("mcp_server_name") or "tool")
                    )
            names = managed_tool_names or tool_calls
            if names:
                # De-duplicate adjacent repeats but keep order.
                summary = []
                for n in names:
                    if not summary or summary[-1] != n:
                        summary.append(n)
                joined = ", ".join(summary[-8:])
                return (
                    f"(agent did work but ended without a text reply. "
                    f"Tools called: {joined}. See transcript for detail.)"
                )
        except Exception as exc:
            logger.warning("recover_result_from_transcript failed for %s: %s",
                          session_id, exc)
        return ""

    # Tool names whose successful invocation is a real-world side effect
    # (file written, email drafted, calendar event created, memory saved,
    # task created, etc.). If the agent calls one of these and then idles
    # without a final text reply, the work happened — don't treat as failed.
    # Anything ending in these suffixes counts; updates the list as we add
    # write tools to the MCP server.
    _SIDE_EFFECT_TOOL_SUFFIXES = (
        "_create", "_update", "_delete", "_complete", "_write",
        "_send", "_draft", "_trigger", "_confirm", "_spawn", "_kill",
    )

    def _had_side_effect_tool_use(self, session_id: str) -> bool:
        """Return True if the agent successfully invoked any write-side-effect
        tool during the session. Used by the empty-final-text guard in
        `_handle_outcome` to distinguish "agent did real work but didn't
        summarize" (legitimate completion) from "agent gave up after a
        read-only research spree" (silent failure)."""
        import json as _json
        try:
            path = self.transcript_store.dir / f"{session_id}.jsonl"
            if not path.exists():
                return False
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    kind = d.get("kind", "")
                    payload = d.get("payload", {}) or {}

                    if kind == "tool_call" and not payload.get("is_error"):
                        name = str(payload.get("tool") or "")
                    elif kind in (
                        "managed_event_agent.mcp_tool_use",
                        "managed_event_agent.tool_use",
                    ):
                        # Pair-with-result would be more accurate, but the
                        # tool_use event fires only when the call is dispatched
                        # — an outright permission denial wouldn't reach here.
                        # Good-enough for the failure guard.
                        name = str(payload.get("name") or "")
                    else:
                        continue

                    if any(name.endswith(s) for s in self._SIDE_EFFECT_TOOL_SUFFIXES):
                        return True
        except Exception as exc:
            logger.warning("had_side_effect_tool_use failed for %s: %s",
                          session_id, exc)
        return False

    def _build_resume_message(
        self,
        session: Session,
        task: dict[str, Any],
        child_sessions: list[Session],
    ) -> str:
        """Compose the user turn injected when a parent resumes after its
        yield_until children terminate. The local executor sees this as
        the next user message in conversation history (which still
        carries the parent's prior turns). The cloud parent gets a fresh
        Anthropic session and only sees this message — so it must carry
        the original task description plus each child's output.
        """
        parts: list[str] = []
        original = (task.get("description") or "").strip()
        if session.routing == ROUTE_CLAUDE and original:
            # Cloud resume: fresh session — restate the original task so
            # the agent has the goal. Local already has it in history.
            parts.append(f"Resuming after spawned children finished.\n\nOriginal task: {original}")
        else:
            parts.append("Spawned children completed — incorporate their outputs.")
        parts.append("")
        parts.append("Children:")
        for c in child_sessions:
            tokens = (
                (c.total_input_tokens or 0)
                + (c.total_output_tokens or 0)
                + (c.total_cache_creation_tokens or 0)
                + (c.total_cache_read_tokens or 0)
            )
            header = f"- [{c.status}] {c.session_id} — {tokens} tokens, ${c.total_dollars:.4f}"
            parts.append(header)
            if c.status in (STATUS_FAILED, STATUS_BUDGET_EXCEEDED):
                # #433: tell the parent WHY the child died so it can decide
                # retry vs re-spawn vs escalate — status alone can't
                # distinguish "binary not found" from "tests failed".
                reason = self._child_failure_reason(c)
                if reason:
                    parts.append(f"  reason: {reason}")
            body = self._child_final_text(c)
            if body:
                # Cap each child body so the combined message stays
                # well under any per-message limits.
                if len(body) > 6000:
                    body = body[:6000].rsplit(" ", 1)[0] + "…"
                parts.append("  output:")
                for line in body.splitlines():
                    parts.append(f"  {line}")
        return "\n".join(parts)

    def _child_failure_reason(self, child: Session) -> str:
        """Pull a failed/budget child's failure reason from its transcript —
        the `child_failed_internal` / `child_budget_exceeded_internal` events
        written by _handle_outcome (local/managed children) and
        _record_child_failure_reason (CLI failure paths, #433). Latest event
        wins; returns "" when absent (transcripts from before the CLI paths
        wrote these)."""
        path = self.transcript_store.dir / f"{child.session_id}.jsonl"
        reason = ""
        for d in _iter_transcript(path):
            if d.get("kind", "") in (
                "child_failed_internal", "child_budget_exceeded_internal",
            ):
                reason = (d.get("payload", {}) or {}).get("reason") or ""
        return reason

    def _record_child_failure_reason(
        self, session: Session, status: str, reason: str | None
    ) -> None:
        """#433: persist a child's failure reason for the parent's resume turn —
        same child_*_internal vocabulary _handle_outcome writes for local/managed
        children; read back by _child_failure_reason. No-op for non-children."""
        if not session.parent_session_id:
            return
        kind = ("child_budget_exceeded_internal"
                if status == STATUS_BUDGET_EXCEEDED else "child_failed_internal")
        self.transcript_store.append(session.session_id, kind, {
            "parent_session_id": session.parent_session_id,
            "reason": reason or "",
        })

    def _child_final_text(self, child: Session) -> str:
        """Pull the child's final_text from the cache (cloud) or transcript
        (local). Returns empty string if the child produced no final text."""
        # Cloud children persist final_text via managed_cursor.final_text.
        try:
            cached = self.session_store.get_managed_final_text(child.task_id)
        except Exception:
            cached = None
        if cached:
            return cached
        # Fallback: scan the transcript for `completed` / `managed_completed` /
        # `claude_code_completed` / `codex_completed` events. The LATEST
        # event's final_text wins — even when empty — so a reopened child's
        # second run can't re-carry the first run's "[needs clarification] …"
        # question into the parent's resume turn (#428). All four kinds
        # persist a `final_text` key today; the key-presence guard keeps a
        # legacy event (final_chars only, pre-#349 claude_code / pre-#429
        # codex) from clobbering a real value.
        path = self.transcript_store.dir / f"{child.session_id}.jsonl"
        last_text = ""
        for d in _iter_transcript(path):
            kind = d.get("kind", "")
            payload = d.get("payload", {}) or {}
            if kind in ("completed", "managed_completed", "claude_code_completed",
                        "codex_completed"):
                if "final_text" in payload:
                    last_text = payload.get("final_text") or ""
        return last_text

    def _resume_cloud_parent(
        self,
        session: Session,
        task: dict[str, Any],
        child_sessions: list[Session],
        resume_message: str,
    ) -> bool:
        """Create a fresh Anthropic session for a yielded cloud parent.

        The old session was killed when `yield_until` fired (see
        inter_agent.yield_until). The new session inherits the same
        agent_preset / environment / vault but starts with a clean
        message history; we hand it the original task + children's
        outputs as the initial user turn so it can aggregate.
        Returns True on success, False after marking the session
        failed (so the caller can `continue`).
        """
        managed = self._get_managed_executor()
        if managed is None or managed.driver is None:
            self._mark_failed(
                session, task,
                "cloud yield-resume requires Managed Agents configured",
            )
            return False
        # Reset cursor + drop the old remote id so the new session starts
        # fresh on Anthropic's side.
        self.session_store.reset_managed_cursor(session.task_id)
        try:
            new_remote_id = managed.driver.create_session(
                agent_id=managed.agent_id,
                environment_id=managed.environment_id,
                vault_ids=managed.vault_ids,
                initial_message=resume_message,
                metadata={
                    "lifeos_session_id": session.session_id,
                    "task_id": session.task_id,
                    "resume_after_children": True,
                },
                title=_managed_sanitize_title(task.get("description") or ""),
            )
        except Exception as exc:
            logger.error(
                "cloud yield-resume create_session failed for %s: %s",
                session.task_id, type(exc).__name__,
            )
            self.transcript_store.append(
                session.session_id, "managed_resume_create_failed",
                {"error_type": type(exc).__name__},
            )
            self._mark_failed(
                session, task, f"cloud yield-resume create_session failed: {type(exc).__name__}",
            )
            return False
        self.session_store.set_managed_session_id(session.task_id, new_remote_id)
        self.session_store.update_status(session.task_id, STATUS_RUNNING)
        self.transcript_store.append(session.session_id, "managed_resume_created", {
            "remote_id": new_remote_id,
            "child_count": len(child_sessions),
        })
        return True

    def _schedule_id_from_task(self, task: dict[str, Any]) -> str | None:
        """Return the recurring schedule's id when the task carries a
        `sched-<id>` tag (stamped by scheduler_store._hand_off_to_agent for
        cron schedules), else None. Presence of the tag is the sole signal
        that a completion belongs to a recurring schedule."""
        for tag in task.get("tags") or []:
            m = _SCHED_TAG_RE.match(str(tag).lstrip("#"))
            if m:
                return m.group(1)
        return None

    def _resolve_schedule_name(self, schedule_id: str) -> str | None:
        """Look up a schedule's human name via the API so the recurring note
        can be titled readably. Returns None on 404 / any error — the caller
        falls back to a stable id-based filename so grouping still works."""
        try:
            resp = self._http.get(f"{self.api_base}/api/scheduler/{schedule_id}")
            if resp.status_code != 200:
                return None
            name = (resp.json() or {}).get("name") or ""
            return name.strip() or None
        except Exception as exc:
            logger.warning("resolve_schedule_name %s failed: %s", schedule_id, exc)
            return None

    def _write_agent_output(
        self, session: Session, task: dict[str, Any], final_text: str,
    ) -> tuple[str, str] | None:
        """Write the agent's final answer to a Markdown note in the vault's
        Agent Output folder and return (vault-relative path, obsidian:// URL).
        Returns None when the vault path is unset or the write fails — the
        caller keeps the inline summary so the operator never loses content.

        Two layouts, both under `<vault>/<LIFEOS_AGENT_OUTPUT_DIR>`
        (operator-configurable via `LIFEOS_AGENT_OUTPUT_DIR`, default
        `LifeOS/Tasks/Agent Output`):
        - One-off task → a new note `<YYYY-MM-DD>-<slug>-<sid>.md`.
        - Recurring (cron) task → one shared note per schedule
          (`<schedule-slug>.md`); each fire is prepended under a dated heading,
          newest on top, with frontmatter kept at the file head.
        """
        from datetime import datetime

        vault_root = settings.vault_path
        if not vault_root:
            return None
        try:
            vault_root = vault_root.expanduser() if hasattr(vault_root, "expanduser") else vault_root
        except Exception:
            return None

        title = (task.get("description") or session.task_id).strip()
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        folder = settings.agent_output_dir
        schedule_id = self._schedule_id_from_task(task)

        try:
            target_dir = vault_root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            if schedule_id:
                # One shared note per schedule, titled by the schedule's human
                # name when resolvable, else a stable id-keyed fallback so every
                # fire still maps to the same file.
                sched_name = self._resolve_schedule_name(schedule_id)
                slug = _slugify(sched_name) if sched_name else ""
                # Append the schedule id so two distinct schedules that share a
                # human name don't interleave into the same note.
                filename = f"{slug}-{schedule_id}.md" if slug else f"recurring-{schedule_id}.md"
                file_path = target_dir / filename
                content = self._recurring_content(
                    file_path, schedule_id, sched_name or title, now, final_text,
                )
            else:
                # Suffix with a short session id so two same-day tasks with
                # the same slug don't clobber each other now that every
                # completion writes a note.
                slug = _slugify(title) or session.task_id
                sid = session.session_id[-6:]
                filename = f"{today}-{slug}-{sid}.md"
                file_path = target_dir / filename
                frontmatter = (
                    "---\n"
                    f"task: {title}\n"
                    f"session_id: {session.session_id}\n"
                    f"routing: {session.routing or 'unknown'}\n"
                    f"created: {today}\n"
                    "source: agent-worker\n"
                    "---\n\n"
                )
                content = frontmatter + final_text
            file_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("agent output write failed for %s: %s", session.task_id, exc)
            return None

        rel_path = f"{folder}/{filename}"
        obsidian_url = build_obsidian_link(str(file_path), str(vault_root))
        return (rel_path, obsidian_url)

    def _recurring_content(
        self, file_path: Path, schedule_id: str, label: str,
        now: datetime, final_text: str,
    ) -> str:
        """Build the new contents for a recurring schedule's shared note by
        prepending this fire above any existing runs, newest first. Frontmatter
        stays at the file head: `created` is preserved from the first run,
        `updated` is bumped to today."""
        today = now.strftime("%Y-%m-%d")
        stamp = now.strftime("%Y-%m-%d %H:%M")
        run_block = f"## {stamp}\n\n{final_text}\n\n---\n"

        created = today
        existing_body = ""
        if file_path.exists():
            try:
                existing = file_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""
            fm, existing_body = _split_frontmatter(existing)
            existing_body = existing_body.lstrip("\n")
            # Keep the original creation date across fires.
            m = re.search(r"^created:\s*(.+)$", fm, re.M)
            if m:
                created = m.group(1).strip()

        frontmatter = (
            "---\n"
            f"schedule: {label}\n"
            f"schedule_id: {schedule_id}\n"
            f"created: {created}\n"
            f"updated: {today}\n"
            "source: agent-worker-recurring\n"
            "---\n\n"
        )
        body = run_block + ("\n" + existing_body if existing_body else "")
        return frontmatter + body

    def _mark_failed(self, session: Session, task: dict[str, Any], reason: str) -> None:
        title = task.get("description", session.task_id)
        self.session_store.update_status(session.task_id, STATUS_FAILED)
        self.transcript_store.append(session.session_id, "failed", {"reason": reason})
        # Spawned children flow back through `_resume_yielded_for_children`;
        # don't poke the vault tag system and don't ping Telegram (see
        # `_handle_outcome` for the full reasoning).
        if session.parent_session_id:
            return
        self._swap_tag(session.task_id, RUNNING_TAG, FAILED_TAG)
        self._set_task_status(session.task_id, "cancelled")
        self._notify(f"⚠️ {_worker_label(session.routing)}: task '{title}' failed: {reason}")

    def _mark_blocked(self, session: Session, task: dict[str, Any], question: str) -> None:
        title = task.get("description", session.task_id)
        self._swap_tag(session.task_id, RUNNING_TAG, BLOCKED_TAG)
        self._set_task_status(session.task_id, "blocked")
        self.session_store.update_status(session.task_id, STATUS_BLOCKED)
        self.transcript_store.append(session.session_id, "blocked", {"question": question})

        # If the blocked session has a running remote Managed Agents session,
        # kill it now so session-hour billing stops during the (potentially
        # 3-day) clarification wait. Managed resume after kill isn't supported
        # in this MVP — the operator effectively retries by re-tagging once
        # they reply.
        if session.managed_agent_session_id:
            managed = self._get_managed_executor()
            if managed is not None and managed.driver is not None:
                try:
                    managed.driver.kill_session(
                        session.managed_agent_session_id,
                        reason="blocked_for_clarification",
                    )
                except Exception as exc:
                    logger.warning("kill_session %s on block failed: %s",
                                   session.managed_agent_session_id, exc)

        # Issue F: send the question with reply-threading enabled so the user's
        # reply lands in pending_questions.answer and the worker resumes.
        body = (
            f"⏸ {_worker_label(session.routing)}: task '{title}' needs your input.\n\n"
            f"{question}\n\n"
            "Reply to this message to answer."
        )
        sent_id = self.ask_user_via_telegram(
            session.session_id, session.task_id, body,
        )
        if sent_id is None:
            # Telegram not configured — fall back to the legacy one-way
            # message so the operator at least sees the question.
            self._notify(body)

    def _notify(self, text: str) -> None:
        try:
            self._telegram_send(text)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("telegram notify failed: %s", exc)

    def _notify_terminal(self, session: Session, body: str, label: str) -> None:
        """Send a terminal-state notification (completed / failed / budget) and
        register a follow-up so a reply — to any chunk — resumes the session.

        Sends via the with-id sender so every chunk's message_id is captured;
        a reply landing on any of them (or a plain message within the 30-min
        window) reopens the session as a follow-up turn. Falls back to the
        plain one-way `_notify` when the with-id sender is unavailable (bot not
        configured, or a test stub that captures no ids).
        """
        sent_ids: list[int] = []
        try:
            sent_ids = self._telegram_send_with_id(_with_reply_footer(body)) or []
        except Exception as exc:
            logger.warning("terminal notify (with id) failed for %s: %s", session.task_id, exc)
        if not sent_ids:
            self._notify(body)  # no registered anchor → no (false) footer
            return
        try:
            self.session_store.register_completion_followup(
                session_id=session.session_id,
                task_id=session.task_id,
                sent_message_ids=sent_ids,
                label=label,
            )
        except Exception as exc:
            logger.warning(
                "register_completion_followup failed for %s: %s",
                session.task_id, exc,
            )


def _mark_self_restart_cli(argv: list[str]) -> int:
    """`--mark-self-restart [--session ID]... [--task ID]...` — write the
    self-restart marker, then exit. Invoked by the detached-restart primitive
    (`scripts/server.sh restart-worker-detached`) before it bounces the worker,
    so `resume_pending()` finalizes the named session quietly instead of firing
    the rollback notice (#401). Kept tiny and import-light so the bash primitive
    can call it without spinning up the full worker."""
    import argparse
    parser = argparse.ArgumentParser(prog="agent_worker --mark-self-restart")
    parser.add_argument("--mark-self-restart", action="store_true")
    parser.add_argument("--session", action="append", default=[], dest="sessions")
    parser.add_argument("--task", action="append", default=[], dest="tasks")
    args = parser.parse_args(argv)
    path = write_self_restart_marker(session_ids=args.sessions, task_ids=args.tasks)
    print(str(path))
    return 0


def main() -> None:
    # Marker-writing subcommand short-circuits before any worker wiring.
    if "--mark-self-restart" in sys.argv[1:]:
        raise SystemExit(_mark_self_restart_cli(sys.argv[1:]))
    logging.basicConfig(
        level=os.environ.get("LIFEOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # This worker sends progress/completion updates via Telegram. Without
    # this, httpx's request logger (INFO by default, logs the full request
    # URL — which embeds the bot token) would leak the token every send (#519).
    configure_telegram_log_redaction()
    # Wire up real Telegram senders in production. Worker() defaults to
    # no-op senders so tests can't accidentally hit a real chat — see
    # comment in __init__. If telegram.py isn't importable or the bot
    # isn't configured, the no-op fallbacks let the worker still run.
    telegram_send = None
    telegram_send_with_id = None
    try:
        from api.services.telegram import send_message, send_message_capture_ids
        telegram_send = send_message
        telegram_send_with_id = send_message_capture_ids
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Telegram module not importable; running with no-op senders: %s", exc)
    worker = Worker(
        telegram_send=telegram_send,
        telegram_send_with_id=telegram_send_with_id,
    )

    def _handle_signal(signum, _frame):
        logger.info("received signal %s; stopping after current tick", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    worker.run()


if __name__ == "__main__":
    main()
