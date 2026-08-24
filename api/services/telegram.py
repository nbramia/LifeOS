"""
Telegram Bot service for LifeOS.

Three capabilities:
1. Send messages to Telegram (sync + async)
2. Internal chat client consuming the SSE chat pipeline
3. Bot listener (long-polling) for inbound messages

Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""
import asyncio
import json
import logging
import re
import tempfile
import threading
import time
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

import httpx

from config.settings import settings, TelegramBotConfig

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096
# Cap on quoted text prepended to threaded replies. Generous enough to keep a
# full nightly priorities summary (~1,000 chars) intact — truncating below that
# would cut off the bullet a follow-up question is asking about (#435).
MAX_QUOTED_REPLY_CHARS = 1500
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _extract_urls(text: str) -> list[str]:
    """Extract source URLs without asking the model to preserve them."""
    urls = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}>")
        if url and url not in urls:
            urls.append(url)
    return urls[:20]

# The bot token for the message currently being handled. A listener sets this
# once at the top of _handle_update so every outbound send during that update —
# of which there are many, scattered across command handlers — goes out from the
# right bot without threading a token through each call. Unset (None) means the
# primary bot, which is also the default for non-listener callers (agent worker,
# reminders, scheduler).
_active_bot_token: ContextVar[Optional[str]] = ContextVar("active_bot_token", default=None)


# ---------------------------------------------------------------------------
# Bot token resolution
# ---------------------------------------------------------------------------

def _resolve_bot(bot: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve a bot name to its ``(token, chat_id)``.

    Looks up ``bot`` in the primary bot and the specialized-bot registry.
    Returns the matching pair, or ``(None, None)`` if unresolved — callers then
    fall back to the primary token and chat. Resolving both together matters:
    a bot can only post to its own chat, so routing the token without the
    chat_id would send a specialized bot's message to the primary chat. Unknown
    or empty names are ignored so a misconfigured caller doesn't break the send.
    """
    if not bot:
        return None, None
    if bot == "primary":
        return (settings.telegram_bot_token or None), (settings.telegram_chat_id or None)
    for cfg in settings.telegram_bots:
        if cfg.name == bot:
            return (cfg.token or None), (cfg.chat_id or None)
    logger.warning(f"Telegram bot '{bot}' not found in registry, falling back to primary")
    return None, None


def _token_for_bot(bot: Optional[str]) -> Optional[str]:
    """Resolve a bot name to its token (see :func:`_resolve_bot`)."""
    return _resolve_bot(bot)[0]


def valid_bot_names() -> list[str]:
    """Names a caller may legitimately route a send to, newest state.

    ``"primary"`` plus whatever the registry (``config/telegram_bots.json``)
    currently holds. Read on every call rather than cached at import: the
    ``telegram_bots`` property is uncached and reflects the current
    environment, and a registry with no specialized bots is a valid state (a
    fresh clone configures only the primary bot).
    """
    return ["primary"] + [cfg.name for cfg in settings.telegram_bots]


def is_known_bot(bot: Optional[str]) -> bool:
    """Whether ``bot`` resolves to a configured bot. Empty means primary."""
    return not bot or bot in valid_bot_names()


def validate_bot_name(bot: Optional[str]) -> Optional[str]:
    """Return the name to store, or raise ``ValueError`` if it isn't configured.

    Used wherever a bot name is *written* (scheduler create/update) so an
    orphaned name — the residue of a bot rename — is rejected at the point of
    entry instead of silently degrading to the primary chat weeks later (#575).
    Empty or unset stays valid and continues to mean the primary bot.

    Surrounding whitespace is trimmed, so a tool argument that arrived as
    ``' ledger'`` stores as ``'ledger'`` and resolves at fire time. Case is
    *not* folded: matching stays exact, in parity with :func:`_resolve_bot`.
    """
    if bot is not None:
        bot = bot.strip()
    if is_known_bot(bot):
        return bot
    raise ValueError(
        f"Unknown Telegram bot '{bot}'. Configured names: "
        f"{', '.join(valid_bot_names())} — a registry entry "
        f"(config/telegram_bots.json) counts only once its token env var is set"
    )


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------

def _telegram_url(method: str, token: Optional[str] = None) -> str:
    token = token or _active_bot_token.get() or settings.telegram_bot_token
    return f"{TELEGRAM_API}/bot{token}/{method}"


def _split_message(text: str) -> list[str]:
    """Split text into chunks that fit Telegram's 4096-char limit."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    parts = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            parts.append(text)
            break
        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at < MAX_MESSAGE_LENGTH // 2:
            split_at = MAX_MESSAGE_LENGTH
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return parts


def _clean_markdown_for_telegram(text: str) -> str:
    """
    Strip Markdown constructs Telegram can't render.

    Telegram MarkdownV2 supports bold, italic, underline, strike, code, links.
    We keep it simple: use Markdown parse mode and strip unsupported constructs.
    """
    # Remove horizontal rules
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    # Convert headers to bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Remove image syntax
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    return text.strip()


async def send_typing_indicator(chat_id: str = None):
    """Send 'typing...' chat action. Lasts ~5 seconds in the Telegram UI."""
    chat_id = chat_id or settings.telegram_chat_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                _telegram_url("sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        pass  # Non-critical, don't log


class TypingIndicator:
    """Context manager that sends typing indicators every 4 seconds."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self._task: Optional[asyncio.Task] = None

    async def _loop(self):
        try:
            while True:
                await send_typing_indicator(self.chat_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def send_message_capture_ids(text: str, chat_id: str = None, bot: str = None) -> list[int]:
    """Send a message and return the Telegram `message_id` of every chunk sent.

    Used by the agent worker to track clarification questions and terminal-state
    notifications so reply-threaded answers can be matched back to the right
    pending question. Long messages split across multiple 4096-char chunks; a
    reply can land on *any* chunk, so we capture them all (not just the first).
    Falls back to plain text if Markdown parse fails. Returns an empty list when
    Telegram is disabled or every send failed.

    Args:
        bot: Optional bot name to send from. Resolved via the registry;
            falls back to the primary token if unset or unknown.
    """
    if not settings.telegram_enabled:
        return []
    token, bot_chat_id = _resolve_bot(bot)
    chat_id = chat_id or bot_chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)
    ids: list[int] = []
    for part in _split_message(text):
        try:
            resp = httpx.post(
                _telegram_url("sendMessage", token),
                json={"chat_id": chat_id, "text": part, "parse_mode": "Markdown"},
                timeout=30.0,
            )
            if resp.status_code != 200:
                resp = httpx.post(
                    _telegram_url("sendMessage", token),
                    json={"chat_id": chat_id, "text": part},
                    timeout=30.0,
                )
            if resp.status_code == 200:
                msg_id = (resp.json().get("result") or {}).get("message_id")
                if msg_id is not None:
                    ids.append(int(msg_id))
            else:
                logger.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
    return ids


def send_message(text: str, chat_id: str = None, bot: str = None) -> bool:
    """
    Send a message via Telegram (synchronous).

    Use from background threads (scheduler, alerts).
    Falls back to plain text if Markdown parse fails.

    Args:
        bot: Optional bot name to send from. Resolved via the registry;
            falls back to the primary token if unset or unknown.
    """
    if not settings.telegram_enabled:
        logger.debug("Telegram not configured, skipping send")
        return False

    token, bot_chat_id = _resolve_bot(bot)
    chat_id = chat_id or bot_chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)

    success = True
    for part in _split_message(text):
        try:
            resp = httpx.post(
                _telegram_url("sendMessage", token),
                json={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "Markdown",
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                # Retry without parse_mode (plain text fallback)
                resp = httpx.post(
                    _telegram_url("sendMessage", token),
                    json={"chat_id": chat_id, "text": part},
                    timeout=30.0,
                )
            if resp.status_code != 200:
                logger.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
                success = False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            success = False
    return success


async def send_message_async(text: str, chat_id: str = None, bot: str = None) -> bool:
    """
    Send a message via Telegram (async).

    Use from FastAPI routes.

    Args:
        bot: Optional bot name to send from. Resolved via the registry;
            falls back to the primary token if unset or unknown.
    """
    if not settings.telegram_enabled:
        return False

    token, bot_chat_id = _resolve_bot(bot)
    chat_id = chat_id or bot_chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)

    success = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        for part in _split_message(text):
            try:
                resp = await client.post(
                    _telegram_url("sendMessage", token),
                    json={
                        "chat_id": chat_id,
                        "text": part,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code != 200:
                    resp = await client.post(
                        _telegram_url("sendMessage", token),
                        json={"chat_id": chat_id, "text": part},
                    )
                if resp.status_code != 200:
                    logger.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
                    success = False
            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                success = False
    return success


# ---------------------------------------------------------------------------
# Internal chat client (consumes SSE from /api/ask/stream)
# ---------------------------------------------------------------------------

async def chat_via_api(
    question: str,
    conversation_id: str = None,
    persona: str = None,
    source: dict | None = None,
) -> dict:
    """
    Run a question through the full LifeOS chat pipeline (non-streaming).

    POSTs to the local /api/ask/stream endpoint and collects SSE events.

    Args:
        persona: Optional per-bot system-prompt preamble (e.g. the fitness bot),
            forwarded to the orchestrator so its replies are domain-primed.

    Returns:
        {"answer": str, "conversation_id": str, "claude_intent": bool, "task": str|None}
    """
    port = settings.port
    body: dict = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    if persona:
        body["persona"] = persona
    if source:
        body["source"] = source

    full_text = ""
    conv_id = conversation_id
    claude_intent = False
    task = None
    engine = "claude_code"
    sources = []
    statuses = []
    perf_trace = None

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"http://localhost:{port}/api/ask/stream",
            json=body,
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                raise RuntimeError(f"Chat pipeline returned HTTP {resp.status_code}: {error_body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "content":
                    full_text += event.get("content", "")
                elif etype == "self_correction":
                    full_text = ""
                elif etype == "conversation_id":
                    conv_id = event.get("conversation_id", conv_id)
                elif etype == "claude_intent":
                    claude_intent = True
                    task = event.get("task", question)
                    engine = event.get("engine", "claude_code")
                elif etype == "status":
                    statuses.append(event.get("message", ""))
                elif etype == "sources":
                    sources = event.get("sources", [])
                elif etype == "perf_trace":
                    perf_trace = event
                elif etype == "error":
                    error_msg = event.get("message", "Unknown error")
                    logger.error(f"Chat pipeline error: {error_msg}")
                    full_text += f"\n\nError: {error_msg}" if full_text else f"Error: {error_msg}"

    return {
        "answer": full_text,
        "conversation_id": conv_id,
        "claude_intent": claude_intent,
        "task": task,
        "engine": engine,
        "sources": sources,
        "statuses": statuses,
        "perf_trace": perf_trace,
    }


async def chat_via_api_with_log(question: str) -> dict:
    """Run a question through the chat pipeline and capture execution metadata.

    Like chat_via_api() but also returns tool_statuses, token usage, and cost
    for reminder execution logging.

    Returns:
        {"answer": str, "conversation_id": str, "tool_statuses": list[str],
         "cost_usd": float, "model": str, "input_tokens": int, "output_tokens": int}
    """
    port = settings.port
    body: dict = {"question": question}

    full_text = ""
    conv_id = None
    tool_statuses: list[str] = []
    cost_usd = 0.0
    model = ""
    input_tokens = 0
    output_tokens = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"http://localhost:{port}/api/ask/stream",
            json=body,
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                raise RuntimeError(f"Chat pipeline returned HTTP {resp.status_code}: {error_body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if etype == "content":
                    full_text += event.get("content", "")
                elif etype == "self_correction":
                    full_text = ""
                elif etype == "conversation_id":
                    conv_id = event.get("conversation_id", conv_id)
                elif etype == "status":
                    tool_statuses.append(event.get("message", ""))
                elif etype == "usage":
                    cost_usd = event.get("cost_usd", 0)
                    model = event.get("model", "")
                    input_tokens = event.get("input_tokens", 0)
                    output_tokens = event.get("output_tokens", 0)
                elif etype == "error":
                    error_msg = event.get("message", "Unknown error")
                    full_text += f"\n\nError: {error_msg}" if full_text else f"Error: {error_msg}"

    return {
        "answer": full_text,
        "conversation_id": conv_id,
        "tool_statuses": tool_statuses,
        "cost_usd": cost_usd,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


# ---------------------------------------------------------------------------
# Bot listener (long-polling)
# ---------------------------------------------------------------------------

class TelegramBotListener:
    """
    Background thread that receives messages via Telegram long-polling.

    Forwards messages through the LifeOS chat pipeline and sends responses back.
    """

    _STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "telegram_state.json"
    _DEDUP_WINDOW = 1000  # Track last N message IDs for deduplication

    def __init__(self, bot: Optional[TelegramBotConfig] = None):
        # bot=None means the primary bot (legacy behavior). The primary keeps the
        # legacy state file so its update offset survives this upgrade; named bots
        # get their own offset file so concurrent pollers don't clobber each other.
        self._bot = bot or settings.telegram_primary_bot
        self._is_primary = self._bot.name == "primary"
        # Bots that own Claude Code session reply threads: the primary, plus any
        # orchestration bot (e.g. doctor). These run the agent/Claude-Code reply
        # hooks — scoped to their own bot — instead of being pure chat (#348).
        self._owns_agent_sessions = self._is_primary or self._bot.orchestrates
        self._token = self._bot.token
        self._chat_id = self._bot.chat_id
        self._persona = self._bot.persona
        self._state_file = (
            self._STATE_FILE if self._is_primary
            else Path(f"data/telegram_state_{self._bot.name}.json")
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Conversation state: chat_id -> conversation_id (per-instance, so each
        # bot's threads stay isolated from the others')
        self._conversations: dict[str, str] = {}
        self._last_result: dict | None = None  # Last chat result for /inspect
        self._last_update_id = self._load_last_update_id()
        self._processed_ids: deque[int] = deque(maxlen=self._DEDUP_WINDOW)

    def _load_last_update_id(self) -> int:
        """Load persisted update_id from disk, or 0 if unavailable."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                update_id = data.get("last_update_id", 0)
                if not isinstance(update_id, int) or update_id < 0:
                    logger.warning(f"Invalid update_id in state file: {update_id!r}, resetting to 0")
                    return 0
                logger.info(f"Restored Telegram update offset for '{self._bot.name}': {update_id}")
                return update_id
        except Exception as e:
            logger.warning(f"Could not load Telegram state: {e}")
        return 0

    def _save_last_update_id(self):
        """Persist current update_id to disk (atomic write)."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_update_id": self._last_update_id}))
            tmp.rename(self._state_file)
        except Exception as e:
            logger.warning(f"Could not save Telegram state: {e}")

    def start(self):
        if not (self._token and self._chat_id):
            logger.info(f"Telegram bot '{self._bot.name}' not configured, listener not started")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"TelegramBotListener-{self._bot.name}",
        )
        self._thread.start()
        logger.info(f"Telegram bot listener started: {self._bot.name}")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Telegram bot listener stopped")

    def _run(self):
        """Main polling loop (runs in background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:
            logger.error(f"Telegram bot listener crashed: {e}")
        finally:
            self._loop.close()

    async def _poll_loop(self):
        """Long-polling loop for Telegram updates."""
        logger.info("Telegram bot polling started")

        while not self._stop_event.is_set():
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                # Wait before retrying on error
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict]:
        """Fetch new updates from Telegram with long-polling."""
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.get(
                    _telegram_url("getUpdates", self._token),
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                if resp.status_code != 200:
                    logger.warning(f"getUpdates failed: {resp.status_code}")
                    return []
                data = resp.json()
                if not data.get("ok"):
                    return []
                updates = data.get("result", [])
                if updates:
                    self._last_update_id = updates[-1]["update_id"]
                    self._save_last_update_id()
                return updates
        except httpx.ReadTimeout:
            # Normal for long-polling
            return []
        except Exception as e:
            logger.error(f"getUpdates error: {e}")
            await asyncio.sleep(2)
            return []

    def _maybe_deposit_agent_answer(self, reply_to_message_id: int, text: str) -> bool:
        """If `reply_to_message_id` matches an open agent-worker clarification
        question, record the answer and short-circuit the chat pipeline.

        Importing the session store lazily keeps the chat-only deployment
        path workable even when the agent worker package isn't initialized.
        """
        try:
            from api.services.agent_worker.session_store import SessionStore
            store = SessionStore()
            return store.deposit_answer(reply_to_message_id, text, bot=self._bot.name)
        except Exception as exc:
            logger.warning(f"agent-worker deposit_answer failed: {exc}")
            return False

    async def _handle_agent_spawn(self, rest: str, chat_id: str):
        """Spawn an operator agent on demand: `/agent [local|claude] <task>`.

        Explicit `local`/`claude` forces the model; otherwise preflight
        auto-routes (and asks via the clarification flow when ambiguous). The
        completion notification is replyable via the Phase 1 follow-up model.
        """
        explicit = None
        parts = rest.split(maxsplit=1)
        if parts and parts[0].lower() in ("local", "claude"):
            explicit = parts[0].lower()
            task = parts[1].strip() if len(parts) > 1 else ""
        else:
            task = rest.strip()

        if not task:
            await send_message_async(
                "Usage: /agent [local|claude] <task>\n"
                "e.g. `/agent draft a reply to the landlord` (auto-routes), "
                "or `/agent claude refactor the parser`.",
                chat_id=chat_id,
            )
            return

        try:
            from api.services.agent_worker.operator_spawn import create_operator_session
            from api.services.agent_worker.session_store import SessionStore
            store = SessionStore()
            result = await asyncio.to_thread(
                create_operator_session, store, task, explicit_routing=explicit,
            )
        except Exception as exc:
            logger.warning(f"operator spawn failed: {exc}")
            await send_message_async(
                f"Couldn't spawn agent: {str(exc)[:200]}", chat_id=chat_id,
            )
            return

        if not result.get("ok"):
            await send_message_async(
                f"Couldn't spawn agent: {result.get('error')}", chat_id=chat_id,
            )
            return

        if result.get("needs_routing"):
            # Preflight couldn't pick a model — ask, and register the answer so
            # the worker resolves routing and dispatches. Operator replies to
            # this message (the reply-deposit hook matches it).
            question = (
                "Should I run this on the local Gemma model or on Claude? "
                "Reply 'local' or 'claude'."
            )
            ids = send_message_capture_ids(question, chat_id)
            if ids:
                store.create_pending_question(
                    session_id=result["session_id"], task_id=result["task_id"],
                    question=question, sent_message_id=ids[0],
                    sent_message_ids=ids, kind="clarification",
                )
            return

        label = "Claude (cloud)" if result["routing"] == "claude" else "local Gemma"
        await send_message_async(
            f"🤖 Spawned {label} agent: {task[:120]}\n"
            f"I'll send the result here when it's done — reply to it (or just message "
            f"me within 30 min) to continue the thread.",
            chat_id=chat_id,
        )

    async def _handle_orchestration_message(self, text: str, chat_id: str):
        """Spawn a Claude Code session for an orchestration bot (e.g. doctor).

        The bot's persona is the orchestration contract; the user's message is
        the problem report. The session is tagged with this bot's name so the
        worker routes every [NOTIFY]/[CLARIFY]/completion notice back to this
        bot, and a threaded reply (handled by the resume hook) continues it.
        Working dir is the canonical LifeOS checkout, not whatever worktree the
        operator happens to be in.
        """
        import os
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import SessionStore

        # A short bare affirmative ("yes", "approved", "go ahead") is never a
        # problem report — it's almost certainly a mis-threaded approval of a
        # pending goal (#453: each such plain message used to spawn a fresh
        # context-free session). Route it to the most recent open goal gate on
        # this bot instead; only genuinely unmatched text falls through to a
        # spawn. The length bound keeps real reports that merely START with
        # "yes ..." ("yes the calendar tool is broken again") spawning normally.
        stripped = text.strip()
        if len(stripped) <= 25:
            from api.services.agent_worker.worker import _is_affirmative
            if _is_affirmative(stripped):
                try:
                    gate = SessionStore().get_latest_open_question(
                        bot=self._bot.name, kind="goal_approval",
                    )
                except Exception as exc:
                    logger.warning(f"goal-gate lookup failed: {exc}")
                    gate = None
                if gate and await self._maybe_handle_claude_code_reply(
                    int(gate["sent_message_id"]), stripped, chat_id,
                ):
                    return
                if not gate:
                    await send_message_async(
                        f'I got "{stripped}" but there\'s nothing here waiting '
                        "for an approval — no session was started. If you meant "
                        "to answer an earlier message, use Telegram's Reply on "
                        "it; otherwise describe the problem you want me to fix.",
                        chat_id=chat_id,
                    )
                    return

        working_dir = os.path.expanduser(os.path.join(str(settings.code_dir), "LifeOS"))
        prompt = (
            f"{self._persona}\n\n"
            f"---\n\n"
            f"The user just sent this report via the {self._bot.name} bot:\n\n{text}"
        )
        # Send the ack with id capture so it can be registered as a reply
        # anchor once the spawn returns a session id — replying to "On it"
        # then routes into the session like any other thread message (#458).
        from api.services.agent_worker.worker import _with_reply_footer
        ack_ids = []
        try:
            ack_ids = send_message_capture_ids(
                _with_reply_footer("🩺 On it — taking a look now. I'll follow up here as I go."),
                chat_id,
            ) or []
        except Exception as exc:
            logger.warning(f"orchestration ack send failed: {exc}")
        if not ack_ids:
            await send_message_async(
                "🩺 On it — taking a look now. I'll follow up here as I go.",
                chat_id=chat_id,
            )
        store = SessionStore()
        try:
            result = await asyncio.to_thread(
                spawn_claude_code_session,
                store,
                prompt,
                working_dir=working_dir,
                plan_mode=False,
                chat_id=chat_id,
                bot=self._bot.name,
            )
        except Exception as exc:
            logger.warning(f"orchestration spawn failed: {exc}")
            await send_message_async(
                f"Couldn't start the session: {str(exc)[:200]}", chat_id=chat_id,
            )
            return
        if ack_ids and result.get("ok") and result.get("session_id"):
            try:
                store.add_reply_anchors(
                    result["session_id"], result.get("task_id") or "",
                    ack_ids, bot=self._bot.name,
                )
            except Exception as exc:
                logger.warning(f"on-it anchor registration failed: {exc}")
        if not result.get("ok"):
            await send_message_async(
                f"Couldn't start the session: {result.get('error')}", chat_id=chat_id,
            )

    async def _handle_update(self, update: dict):
        """Process a single Telegram update."""
        # Route every outbound send during this update through this bot's token.
        # Set once here so the many send sites below need no per-call token.
        _active_bot_token.set(self._token)

        message = update.get("message")
        if not message:
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        voice = message.get("voice") or message.get("audio")
        media_type = next(
            (
                kind for kind in (
                    "photo", "video", "document", "animation", "video_note", "sticker",
                ) if message.get(kind)
            ),
            None,
        )
        media_payload = message.get(media_type) if media_type else None
        if isinstance(media_payload, list):
            media_payload = media_payload[-1] if media_payload else None
        chat_id = str(message["chat"]["id"])

        # Auth check first — don't let unauthorized chats pollute the dedup window
        if chat_id != self._chat_id:
            logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        # Dedup safety net: skip messages already processed in this session
        message_id = message.get("message_id")
        if message_id and message_id in self._processed_ids:
            logger.debug(f"Skipping duplicate message_id {message_id}")
            return
        if message_id:
            self._processed_ids.append(message_id)

        if not text and voice:
            await send_typing_indicator(chat_id)
            try:
                file_id = voice.get("file_id")
                async with httpx.AsyncClient(timeout=30.0) as client:
                    file_resp = await client.get(
                        _telegram_url("getFile", self._token),
                        params={"file_id": file_id},
                    )
                    file_resp.raise_for_status()
                    file_path = file_resp.json().get("result", {}).get("file_path")
                    if not file_path:
                        raise RuntimeError("Telegram did not return an audio file path")
                    audio_resp = await client.get(
                        f"{TELEGRAM_API}/file/bot{self._token}/{file_path}"
                    )
                    audio_resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=Path(file_path).suffix or ".ogg") as audio_file:
                    audio_file.write(audio_resp.content)
                    audio_file.flush()
                    from api.services.telegram_transcription import transcribe
                    text = await asyncio.to_thread(transcribe, audio_file.name)
                if text:
                    text = f"[Voice message transcription]\n{text}"
                    logger.info("Transcribed Telegram voice message (%d chars)", len(text))
                else:
                    raise RuntimeError("The transcription was empty")
            except Exception as exc:
                logger.exception("Telegram voice transcription failed")
                await send_message_async(
                    "I received your voice message but couldn't transcribe it. "
                    "Please try again or send it as text.", chat_id=chat_id,
                )
                return

        # Telegram puts text accompanying media in ``caption`` rather than
        # ``text``. Preserve captioned forwards/attachments as normal inbox
        # input, and acknowledge uncaptioned media instead of silently
        # dropping the update. The file is not downloaded or interpreted yet;
        # its Telegram file_id and message provenance travel with the request.
        if media_type and not voice:
            if text:
                text = f"[Telegram {media_type}]\n{text}"
            else:
                text = f"[Telegram {media_type} without caption]"

        if not text:
            return

        # Agent-worker clarification hook (Issue F). If this message is a
        # reply-thread to a previously-sent clarification question, deposit
        # the answer and short-circuit — don't route to the chat pipeline.
        # Only bots that own agent/Claude-Code reply threads run this — the
        # primary and any orchestration bot (doctor). The lookup is scoped to
        # this bot (#348): Telegram message_ids are unique per chat (per bot),
        # so without scoping a specialized bot could collide with the primary's
        # ids in the shared follow-up table. Pure-chat specialized bots skip it.
        reply_to = message.get("reply_to_message")
        if self._owns_agent_sessions and reply_to and reply_to.get("message_id"):
            reply_to_id = int(reply_to["message_id"])
            # A reply to a /claude completion resumes that Claude Code session
            # (#237) — checked before the agent-worker deposit since both use
            # the shared follow-up table but resume different subsystems.
            if await self._maybe_handle_claude_code_reply(
                reply_to_id, text, chat_id, quoted_text=reply_to.get("text"),
            ):
                return
            if self._maybe_deposit_agent_answer(reply_to_id, text):
                return

        logger.info(f"Telegram message: {text[:100]}")

        # Show typing immediately so the user knows we received their message
        await send_typing_indicator(chat_id)

        # Handle known commands (unrecognized /commands fall through to chat)
        if text.startswith("/"):
            handled = await self._handle_command(text, chat_id)
            if handled:
                return

        # Plan approval, clarification, and /claude follow-up are all routed
        # through threaded replies (handled near the top of this method
        # via _maybe_handle_claude_code_reply / _maybe_deposit_agent_answer). A
        # plain non-threaded message is always a fresh chat query, never
        # an implicit follow-up — so unrelated questions never get
        # silently swallowed into a finished agent or /claude thread.

        # Orchestration bots (e.g. doctor) drive a Claude Code session instead
        # of the chat pipeline. A threaded reply to one of its messages is
        # already handled above (resume hook); a fresh message starts a new
        # repair session whose clarify/implement gates round-trip on this bot.
        if self._bot.orchestrates:
            await self._handle_orchestration_message(text, chat_id)
            return

        # Threaded-reply context: when the user replies to one of the bot's own
        # messages (e.g. correcting a "Logged: …" line, or asking about a bullet
        # in a summary sent via the raw Bot API), pass the quoted text as context
        # so the orchestrator can resolve deictic questions about it (#435).
        # Primary-bot agent/Claude-Code reply threads short-circuited above, so
        # this only sees replies to ordinary messages.
        effective_text = text
        if reply_to and reply_to.get("text"):
            quoted = reply_to["text"].strip()
            if len(quoted) > MAX_QUOTED_REPLY_CHARS:
                quoted = quoted[:MAX_QUOTED_REPLY_CHARS] + "…"
            effective_text = f'[replying to my earlier message: "{quoted}"]\n{text}'

        # Send through chat pipeline (intent classification happens there)
        try:
            source = {
                "type": "telegram",
                "chat_id": chat_id,
                "message_id": message_id,
            }
            urls = _extract_urls(effective_text)
            if urls:
                source["urls"] = urls
            if media_type:
                source.update({
                    "media_type": media_type,
                    "file_id": media_payload.get("file_id")
                    if isinstance(media_payload, dict) else None,
                    "forwarded": bool(message.get("forward_origin") or message.get("forward_from")),
                })
            async with TypingIndicator(chat_id):
                conv_id = self._conversations.get(chat_id)
                result = await chat_via_api(
                    effective_text,
                    conversation_id=conv_id,
                    persona=self._persona,
                    source=source,
                )
                self._conversations[chat_id] = result["conversation_id"]
                self._last_result = result

            # Check if the chat pipeline detected a code intent or an explicit
            # engine handoff ("use codex" / "use claude code", #305b). Engine
            # handoffs spawn background sessions whose follow-ups route to the
            # primary bot, so only the primary honors them. A specialized bot
            # redirects instead of falling through to a possibly-empty answer.
            if result.get("claude_intent"):
                task = result.get("task", text)
                engine = result.get("engine", "claude_code")
                if not self._is_primary:
                    await send_message_async(
                        "That looks like a coding/agent task — send it to your main "
                        "LifeOS bot, which runs Claude Code and Codex.",
                        chat_id=chat_id,
                    )
                    return
                if engine == "codex":
                    logger.info(f"Engine handoff → Codex: {task[:50]}...")
                    await self._handle_codex_command(task, chat_id)
                else:
                    logger.info(f"Engine handoff → Claude Code: {task[:50]}...")
                    await self._handle_claude_command(task, chat_id)
                return

            answer = result["answer"]
            if not answer:
                answer = "No response generated."

            await send_message_async(answer, chat_id=chat_id)
        except Exception as e:
            logger.error(f"Error processing Telegram message: {e}")
            await send_message_async(
                f"Error processing your message: {str(e)[:200]}",
                chat_id=chat_id,
            )

    # Commands that spawn agent-worker / Claude Code / Codex sessions. Their
    # completion notices and clarification questions are sent later from a
    # background thread (where the active-bot token is unset → primary), and the
    # reply-thread hooks only run on the primary listener. So these belong to the
    # primary bot only; specialized bots are pure chat surfaces and redirect.
    _PRIMARY_ONLY_COMMANDS = frozenset({
        "/agent", "/claude", "/codex",
        "/claude_status", "/claudestatus", "/claude_cancel", "/claudecancel",
        "/codex_status", "/codexstatus", "/codex_cancel", "/codexcancel",
    })

    async def _handle_command(self, text: str, chat_id: str) -> bool:
        """Handle known bot commands. Returns True if handled, False to fall through to chat."""
        command = text.split()[0].lower()

        if not self._is_primary and command in self._PRIMARY_ONLY_COMMANDS:
            await send_message_async(
                "Agent and Claude Code/Codex commands run on your main LifeOS bot — "
                "send this there.",
                chat_id=chat_id,
            )
            return True

        if command in ("/new", "/clear"):
            self._conversations.pop(chat_id, None)
            await send_message_async("Started a new conversation.", chat_id=chat_id)

        elif command == "/status":
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"http://localhost:{settings.port}/health")
                    if resp.status_code == 200:
                        data = resp.json()
                        status = data.get("status", "unknown")
                        await send_message_async(
                            f"LifeOS status: *{status}*",
                            chat_id=chat_id,
                        )
                    else:
                        await send_message_async(
                            "LifeOS health check failed.",
                            chat_id=chat_id,
                        )
            except Exception as e:
                await send_message_async(
                    f"Could not reach LifeOS server: {e}",
                    chat_id=chat_id,
                )

        elif command == "/codex":
            task = text[len("/codex"):].strip()
            if not task:
                await send_message_async("Usage: /codex <task description>", chat_id=chat_id)
            else:
                await self._handle_codex_command(task, chat_id)

        elif command == "/claude":
            task = text[len("/claude"):].strip()
            if not task:
                await send_message_async("Usage: /claude <task description>", chat_id=chat_id)
            else:
                await self._handle_claude_command(task, chat_id)

        elif command == "/agent":
            await self._handle_agent_spawn(text[len("/agent"):].strip(), chat_id)

        elif command in ("/claude_status", "/claudestatus"):
            await self._handle_claude_status(chat_id)

        elif command in ("/claude_cancel", "/claudecancel"):
            await self._handle_claude_cancel(chat_id)

        elif command in ("/codex_status", "/codexstatus"):
            await self._handle_codex_status(chat_id)

        elif command in ("/codex_cancel", "/codexcancel"):
            await self._handle_codex_cancel(chat_id)

        elif command == "/inspect":
            await self._handle_inspect(chat_id)

        elif command == "/help":
            help_text = (
                "*LifeOS Telegram Bot*\n\n"
                "Send any message to query LifeOS (calendar, emails, vault, etc.)\n\n"
                "*Commands:*\n"
                "/new or /clear - Start a new conversation\n"
                "/status - Check LifeOS server health\n"
                "/inspect - Show sources checked in last response\n"
                "/agent [local|claude] <task> - Spawn an agent (auto-routes if no model given)\n"
                "/claude <task> - Run a task with Claude Code\n"
                "/claude\\_status - Check active Claude Code session\n"
                "/claude\\_cancel - Cancel active Claude Code session\n"
                "/codex <task> - Run a task with Codex\n"
                "/codex\\_status - Check active Codex session\n"
                "/codex\\_cancel - Cancel active Codex session\n"
                "/help - Show this message"
            )
            await send_message_async(help_text, chat_id=chat_id)

        else:
            # Unknown command — fall through to chat pipeline
            return False

        return True

    # ------------------------------------------------------------------
    # Claude Code orchestration
    # ------------------------------------------------------------------

    _APPROVAL_KEYWORDS = {"approve", "approved", "yes", "go", "proceed", "ok"}
    _REJECTION_KEYWORDS = {"reject", "rejected", "no", "cancel", "stop"}

    async def _maybe_handle_claude_code_reply(
        self, reply_to_message_id: int, text: str, chat_id: str,
        quoted_text: str | None = None,
    ) -> bool:
        """If a reply targets a CLI (Claude Code or Codex) completion message,
        resume the linked agent-worker session.

        Rows are registered with ``kind='followup'`` against a session_store
        row whose ``routing='claude_code'`` or ``routing='codex'``. Depositing
        the answer is enough — the worker's ``_resume_as_followup`` picks it
        up on the next tick and routes through the right executor's resume().

        ``kind='goal_approval'`` replies are also consumed here, with an
        immediate ack: the worker only drains the answer on its next tick (up
        to poll_seconds later) and the agent may not emit anything for minutes
        after that, so without a deposit-time ack the operator can't tell
        whether their 'yes' landed at all.

        ``kind='status_anchor'`` (#458): every operator-facing session message
        (streamed [NOTIFY] bodies, heartbeats, acks) registers its Telegram
        message id against the session. A threaded reply to ANY of them is
        consumed here as a context note — queued (with ``quoted_text``, the
        message being replied to, as context) and delivered at the session's
        next turn boundary; a terminal session is reopened for it.

        Returns True if the reply was consumed.
        """
        try:
            from api.services.agent_worker.session_store import SessionStore
            store = SessionStore()
            q = store.get_open_question_by_message_id(reply_to_message_id, bot=self._bot.name)
        except Exception as exc:
            logger.warning(f"cli reply lookup failed: {exc}")
            return False
        if not q:
            # A reply landing on an ALREADY-answered goal message must not fall
            # through — on an orchestration bot it would spawn a brand-new
            # session with the bare reply as its "report" (the yes/yes/approved
            # fan-out). Consume it with a pointer instead.
            try:
                done = store.get_open_question_by_message_id(
                    reply_to_message_id, bot=self._bot.name, include_answered=True,
                )
            except Exception as exc:
                logger.warning(f"answered-question lookup failed: {exc}")
                done = None
            if done and done.get("answered_at") and done.get("kind") == "goal_approval":
                await send_message_async(
                    "I already have your answer for that goal — it's in motion. "
                    "To add something, reply to my latest update.",
                    chat_id=chat_id,
                )
                return True
            return False
        if q.get("kind") == "status_anchor":
            return await self._handle_status_anchor_reply(q, text, chat_id, quoted_text)
        if q.get("kind") == "goal_approval":
            # Same affirmative parser the worker's _resume_goal uses, so the
            # ack never disagrees with what the worker will actually do.
            from api.services.agent_worker.worker import _is_affirmative
            if not store.deposit_answer(reply_to_message_id, text, bot=self._bot.name):
                # Race: answered between the lookup above and this deposit.
                await send_message_async(
                    "I already have your answer for that goal — it's in motion. "
                    "To add something, reply to my latest update.",
                    chat_id=chat_id,
                )
                return True
            if _is_affirmative(text):
                ack = ("✅ Goal locked — starting work now. "
                       "I'll post updates here as I go.")
            else:
                ack = ("✏️ Got it — reworking the goal with your changes. "
                       "I'll propose an updated version shortly.")
            self._send_anchored_ack(store, q.get("session_id"), q.get("task_id"), ack, chat_id)
            return True
        if q.get("kind") != "followup":
            return False
        session_id = q.get("session_id")
        if not session_id:
            return False
        session = store.get_by_session_id(session_id)
        if not session or session.routing not in ("claude_code", "codex"):
            return False
        store.deposit_answer(reply_to_message_id, text, bot=self._bot.name)
        label = "Codex" if session.routing == "codex" else "Claude Code"
        await send_message_async(f"Resuming {label} session...", chat_id=chat_id)
        return True

    async def _handle_status_anchor_reply(
        self, q: dict, text: str, chat_id: str, quoted_text: str | None,
    ) -> bool:
        """Route a threaded reply on a status/heartbeat/ack message back into
        its session as a context note (#458).

        The note is queued with the quoted message as context and rides the
        session's next turn boundary: a RUNNING/CLAIMED/BLOCKED session picks
        it up when its pending messages next drain; a terminal session with a
        persisted CLI id is reopened (enqueue-then-CLAIM, mirroring
        reopen-on-send #428) so the dispatch tick resumes it with the note.
        """
        from api.services.agent_worker.session_store import (
            STATUS_BUDGET_EXCEEDED, STATUS_COMPLETED, STATUS_FAILED, SessionStore,
        )
        store = SessionStore()
        session_id = q.get("session_id")
        session = store.get_by_session_id(session_id) if session_id else None
        if session is None:
            return False
        quoted = (quoted_text or "").strip()
        if quoted:
            if len(quoted) > MAX_QUOTED_REPLY_CHARS:
                quoted = quoted[:MAX_QUOTED_REPLY_CHARS] + "…"
            composed = f'[operator replied to your status update: "{quoted}"]\n{text}'
        else:
            composed = f"(operator note) {text}"
        store.enqueue_message(session.session_id, "operator", composed)
        terminal = session.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_BUDGET_EXCEEDED)
        if terminal:
            if session.routing in ("claude_code", "codex") and session.claude_code_session_id:
                from api.services.agent_worker.session_store import STATUS_CLAIMED
                store.update_status(session.task_id, STATUS_CLAIMED)
                ack = "📨 Got it — waking the session with your note."
            else:
                ack = ("📨 Noted, but that session already ended and can't be "
                       "resumed — send a fresh message to start a new one.")
        else:
            ack = "📨 Noted — I'll pass this to the session at its next checkpoint."
        self._send_anchored_ack(store, session.session_id, session.task_id, ack, chat_id)
        return True

    def _send_anchored_ack(
        self, store, session_id: str | None, task_id: str | None,
        ack: str, chat_id: str,
    ) -> None:
        """Send a session-scoped ack with the reply-affordance footer and
        register its message id as a reply anchor, so the ack itself is part
        of the replyable work thread (#458). Falls back to a plain, footerless
        send when id capture is unavailable.
        """
        from api.services.agent_worker.worker import _with_reply_footer
        ids = []
        try:
            ids = send_message_capture_ids(_with_reply_footer(ack), chat_id) or []
        except Exception as exc:
            logger.warning(f"anchored ack send failed: {exc}")
        if ids and session_id and task_id:
            try:
                store.add_reply_anchors(session_id, task_id, ids, bot=self._bot.name)
            except Exception as exc:
                logger.warning(f"ack anchor registration failed: {exc}")
        elif not ids:
            try:
                send_message(ack, chat_id)  # plain, footerless fallback
            except Exception as exc:
                logger.warning(f"ack fallback send failed: {exc}")

    def _should_use_plan_mode(self, task: str) -> bool:
        """Conservative heuristic: plan mode only for complex-sounding tasks.
        Delegates to the shared helper so Telegram and web-chat stay in sync."""
        from api.services.agent_worker.claude_code_spawn import should_use_plan_mode
        return should_use_plan_mode(task)

    async def _handle_claude_command(self, task: str, chat_id: str):
        """Spawn a /claude session by writing a routing='claude_code' row
        that the agent worker dispatches on its next tick."""
        from api.services.agent_worker.claude_code_spawn import spawn_claude_code_session
        from api.services.agent_worker.session_store import SessionStore
        from api.services.directory_resolver import resolve_working_directory

        working_dir = resolve_working_directory(task)
        plan_mode = self._should_use_plan_mode(task)
        mode_label = " (plan mode)" if plan_mode else ""

        await send_message_async(
            f"Starting Claude Code{mode_label}...\nDirectory: `{working_dir}`",
            chat_id=chat_id,
        )
        result = spawn_claude_code_session(
            SessionStore(),
            task,
            working_dir=working_dir,
            plan_mode=plan_mode,
            chat_id=chat_id,
        )
        if not result.get("ok"):
            await send_message_async(f"Error: {result.get('error')}", chat_id=chat_id)

    async def _handle_claude_status(self, chat_id: str):
        """List non-terminal routing='claude_code' sessions from the session_store."""
        from api.services.agent_worker.session_store import (
            TERMINAL_STATUSES, SessionStore,
        )
        store = SessionStore()
        active = [
            s for s in store.list_sessions(routing="claude_code", limit=10)
            if s.status not in TERMINAL_STATUSES
        ]
        if not active:
            await send_message_async("No active Claude Code session.", chat_id=chat_id)
            return
        lines = ["*Claude Code Sessions*"]
        for s in active:
            elapsed = max(0, int(time.time() - (s.started_at or 0)))
            minutes, seconds = divmod(elapsed, 60)
            lines.append(
                f"- `{s.session_id[:8]}` status: {s.status} "
                f"({minutes}m {seconds}s, ${s.total_dollars:.4f})"
            )
        await send_message_async("\n".join(lines), chat_id=chat_id)

    async def _handle_claude_cancel(self, chat_id: str):
        """Mark non-terminal routing='claude_code' sessions as FAILED so the
        worker won't dispatch them again.

        A subprocess already running in a worker tick keeps running until
        it finishes or hits the watchdog — true mid-flight subprocess kill
        requires a cross-process signal that isn't wired here. The status
        update is enough to prevent further dispatch and surface
        'cancelled' in /agents.
        """
        from api.services.agent_worker.session_store import (
            STATUS_FAILED, TERMINAL_STATUSES, SessionStore,
        )
        store = SessionStore()
        active = [
            s for s in store.list_sessions(routing="claude_code", limit=20)
            if s.status not in TERMINAL_STATUSES
        ]
        if not active:
            await send_message_async("No active Claude Code session.", chat_id=chat_id)
            return
        for s in active:
            store.update_status(s.task_id, STATUS_FAILED)
        await send_message_async(
            f"Marked {len(active)} Claude Code session(s) cancelled.",
            chat_id=chat_id,
        )

    # ------------------------------------------------------------------
    # Codex orchestration — mirrors the /claude handlers above
    # ------------------------------------------------------------------

    async def _handle_codex_command(self, task: str, chat_id: str):
        """Spawn a /codex session by writing a routing='codex' row that the
        agent worker dispatches on its next tick."""
        from api.services.agent_worker.codex_spawn import spawn_codex_session
        from api.services.agent_worker.session_store import SessionStore
        from api.services.directory_resolver import resolve_working_directory

        working_dir = resolve_working_directory(task)

        await send_message_async(
            f"Starting Codex...\nDirectory: `{working_dir}`",
            chat_id=chat_id,
        )
        result = spawn_codex_session(
            SessionStore(),
            task,
            working_dir=working_dir,
            chat_id=chat_id,
        )
        if not result.get("ok"):
            await send_message_async(f"Error: {result.get('error')}", chat_id=chat_id)

    async def _handle_codex_status(self, chat_id: str):
        """List non-terminal routing='codex' sessions from the session_store."""
        from api.services.agent_worker.session_store import (
            TERMINAL_STATUSES, SessionStore,
        )
        store = SessionStore()
        active = [
            s for s in store.list_sessions(routing="codex", limit=10)
            if s.status not in TERMINAL_STATUSES
        ]
        if not active:
            await send_message_async("No active Codex session.", chat_id=chat_id)
            return
        lines = ["*Codex Sessions*"]
        for s in active:
            elapsed = max(0, int(time.time() - (s.started_at or 0)))
            minutes, seconds = divmod(elapsed, 60)
            lines.append(
                f"- `{s.session_id[:8]}` status: {s.status} "
                f"({minutes}m {seconds}s, ${s.total_dollars:.4f})"
            )
        await send_message_async("\n".join(lines), chat_id=chat_id)

    async def _handle_codex_cancel(self, chat_id: str):
        """Mark non-terminal routing='codex' sessions as FAILED — same
        contract as /claude_cancel (status flip, not a real subprocess kill).
        """
        from api.services.agent_worker.session_store import (
            STATUS_FAILED, TERMINAL_STATUSES, SessionStore,
        )
        store = SessionStore()
        active = [
            s for s in store.list_sessions(routing="codex", limit=20)
            if s.status not in TERMINAL_STATUSES
        ]
        if not active:
            await send_message_async("No active Codex session.", chat_id=chat_id)
            return
        for s in active:
            store.update_status(s.task_id, STATUS_FAILED)
        await send_message_async(
            f"Marked {len(active)} Codex session(s) cancelled.",
            chat_id=chat_id,
        )

    async def _handle_inspect(self, chat_id: str):
        """Show sources checked and results from the last query."""
        if not self._last_result:
            await send_message_async("No previous query to inspect.", chat_id=chat_id)
            return

        r = self._last_result
        lines = ["*Last query inspection*\n"]

        # Tool actions taken
        statuses = r.get("statuses", [])
        if statuses:
            lines.append("*Actions:*")
            for s in statuses:
                lines.append(f"  - {s}")
            lines.append("")

        # Sources found (show only tool name + type, not input args)
        sources = r.get("sources", [])
        if sources:
            lines.append("*Sources checked:*")
            for src in sources:
                stype = src.get("source_type", "")
                # file_name contains tool(args) — extract just the tool name
                raw = src.get("file_name", "")
                tool_name = raw.split("(")[0] if "(" in raw else raw
                lines.append(f"  - [{stype}] {tool_name}")
            lines.append("")

        # Performance
        perf = r.get("perf_trace")
        if perf:
            total_ms = perf.get("total_ms", 0)
            lines.append(f"*Time:* {total_ms:.0f}ms")
            spans = perf.get("spans", [])
            for span in spans:
                if span.get("duration_ms", 0) > 50:
                    lines.append(f"  - {span['name']}: {span['duration_ms']:.0f}ms")

        if len(lines) == 1:
            lines.append("No tool calls or sources in last response.")

        await send_message_async("\n".join(lines), chat_id=chat_id)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_telegram_listeners: Optional[list[TelegramBotListener]] = None


def get_telegram_listeners() -> list[TelegramBotListener]:
    """Get or create the listeners for the primary bot plus any specialized bots.

    The primary is included only when configured; specialized bots come from the
    registry (``settings.telegram_bots``), which already drops any whose token is
    unset — so a fresh clone with no extra tokens yields just the primary.
    """
    global _telegram_listeners
    if _telegram_listeners is None:
        listeners: list[TelegramBotListener] = []
        if settings.telegram_enabled:
            listeners.append(TelegramBotListener(settings.telegram_primary_bot))
        for bot in settings.telegram_bots:
            listeners.append(TelegramBotListener(bot))
        _telegram_listeners = listeners
    return _telegram_listeners
