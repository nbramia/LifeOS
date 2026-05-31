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
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------

def _telegram_url(method: str) -> str:
    return f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/{method}"


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


def send_message_capture_ids(text: str, chat_id: str = None) -> list[int]:
    """Send a message and return the Telegram `message_id` of every chunk sent.

    Used by the agent worker to track clarification questions and terminal-state
    notifications so reply-threaded answers can be matched back to the right
    pending question. Long messages split across multiple 4096-char chunks; a
    reply can land on *any* chunk, so we capture them all (not just the first).
    Falls back to plain text if Markdown parse fails. Returns an empty list when
    Telegram is disabled or every send failed.
    """
    if not settings.telegram_enabled:
        return []
    chat_id = chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)
    ids: list[int] = []
    for part in _split_message(text):
        try:
            resp = httpx.post(
                _telegram_url("sendMessage"),
                json={"chat_id": chat_id, "text": part, "parse_mode": "Markdown"},
                timeout=30.0,
            )
            if resp.status_code != 200:
                resp = httpx.post(
                    _telegram_url("sendMessage"),
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


def send_message(text: str, chat_id: str = None) -> bool:
    """
    Send a message via Telegram (synchronous).

    Use from background threads (scheduler, alerts).
    Falls back to plain text if Markdown parse fails.
    """
    if not settings.telegram_enabled:
        logger.debug("Telegram not configured, skipping send")
        return False

    chat_id = chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)

    success = True
    for part in _split_message(text):
        try:
            resp = httpx.post(
                _telegram_url("sendMessage"),
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
                    _telegram_url("sendMessage"),
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


async def send_message_async(text: str, chat_id: str = None) -> bool:
    """
    Send a message via Telegram (async).

    Use from FastAPI routes.
    """
    if not settings.telegram_enabled:
        return False

    chat_id = chat_id or settings.telegram_chat_id
    text = _clean_markdown_for_telegram(text)

    success = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        for part in _split_message(text):
            try:
                resp = await client.post(
                    _telegram_url("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": part,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code != 200:
                    resp = await client.post(
                        _telegram_url("sendMessage"),
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

async def chat_via_api(question: str, conversation_id: str = None) -> dict:
    """
    Run a question through the full LifeOS chat pipeline (non-streaming).

    POSTs to the local /api/ask/stream endpoint and collects SSE events.

    Returns:
        {"answer": str, "conversation_id": str, "claude_intent": bool, "task": str|None}
    """
    port = settings.port
    body: dict = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id

    full_text = ""
    conv_id = conversation_id
    claude_intent = False
    task = None
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

    _STATE_FILE = Path("data/telegram_state.json")
    _DEDUP_WINDOW = 1000  # Track last N message IDs for deduplication

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Conversation state: chat_id -> conversation_id
        self._conversations: dict[str, str] = {}
        self._last_result: dict | None = None  # Last chat result for /inspect
        self._last_update_id = self._load_last_update_id()
        self._processed_ids: deque[int] = deque(maxlen=self._DEDUP_WINDOW)

    def _load_last_update_id(self) -> int:
        """Load persisted update_id from disk, or 0 if unavailable."""
        try:
            if self._STATE_FILE.exists():
                data = json.loads(self._STATE_FILE.read_text())
                update_id = data.get("last_update_id", 0)
                if not isinstance(update_id, int) or update_id < 0:
                    logger.warning(f"Invalid update_id in state file: {update_id!r}, resetting to 0")
                    return 0
                logger.info(f"Restored Telegram update offset: {update_id}")
                return update_id
        except Exception as e:
            logger.warning(f"Could not load Telegram state: {e}")
        return 0

    def _save_last_update_id(self):
        """Persist current update_id to disk (atomic write)."""
        try:
            self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_update_id": self._last_update_id}))
            tmp.rename(self._STATE_FILE)
        except Exception as e:
            logger.warning(f"Could not save Telegram state: {e}")

    def start(self):
        if not settings.telegram_enabled:
            logger.info("Telegram not configured, bot listener not started")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="TelegramBotListener",
        )
        self._thread.start()
        logger.info("Telegram bot listener started")

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
                    _telegram_url("getUpdates"),
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
            return store.deposit_answer(reply_to_message_id, text)
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

    async def _handle_update(self, update: dict):
        """Process a single Telegram update."""
        message = update.get("message")
        if not message:
            return

        text = message.get("text", "").strip()
        chat_id = str(message["chat"]["id"])

        # Auth check first — don't let unauthorized chats pollute the dedup window
        if chat_id != settings.telegram_chat_id:
            logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        # Dedup safety net: skip messages already processed in this session
        message_id = message.get("message_id")
        if message_id and message_id in self._processed_ids:
            logger.debug(f"Skipping duplicate message_id {message_id}")
            return
        if message_id:
            self._processed_ids.append(message_id)

        if not text:
            return

        # Agent-worker clarification hook (Issue F). If this message is a
        # reply-thread to a previously-sent clarification question, deposit
        # the answer and short-circuit — don't route to the chat pipeline.
        reply_to = message.get("reply_to_message")
        if reply_to and reply_to.get("message_id"):
            reply_to_id = int(reply_to["message_id"])
            # A reply to a /claude completion resumes that Claude Code session
            # (#237) — checked before the agent-worker deposit since both use
            # the shared follow-up table but resume different subsystems.
            if await self._maybe_handle_claude_code_reply(reply_to_id, text, chat_id):
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

        # Send through chat pipeline (intent classification happens there)
        try:
            async with TypingIndicator(chat_id):
                conv_id = self._conversations.get(chat_id)
                result = await chat_via_api(text, conversation_id=conv_id)
                self._conversations[chat_id] = result["conversation_id"]
                self._last_result = result

            # Check if the chat pipeline detected a "code" intent
            if result.get("claude_intent"):
                # Natural-language intent classifier flagged this as a
                # Claude Code task (terminal / filesystem / browser work).
                task = result.get("task", text)
                logger.info(f"Claude intent detected, invoking Claude Code: {task[:50]}...")
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

    async def _handle_command(self, text: str, chat_id: str) -> bool:
        """Handle known bot commands. Returns True if handled, False to fall through to chat."""
        command = text.split()[0].lower()

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
    _PLAN_MODE_KEYWORDS = [
        "refactor", "implement", "redesign", "migrate",
        "integrate", "build a", "set up a",
        "rewrite", "overhaul", "replace", "restructure",
        "add a new", "create a new", "remove all", "delete all",
    ]

    async def _maybe_handle_claude_code_reply(self, reply_to_message_id: int, text: str, chat_id: str) -> bool:
        """If a reply targets a CLI (Claude Code or Codex) completion message,
        resume the linked agent-worker session.

        Rows are registered with ``kind='followup'`` against a session_store
        row whose ``routing='claude_code'`` or ``routing='codex'``. Depositing
        the answer is enough — the worker's ``_resume_as_followup`` picks it
        up on the next tick and routes through the right executor's resume().

        Returns True if the reply was consumed as a CLI follow-up.
        """
        try:
            from api.services.agent_worker.session_store import SessionStore
            store = SessionStore()
            q = store.get_open_question_by_message_id(reply_to_message_id)
        except Exception as exc:
            logger.warning(f"cli reply lookup failed: {exc}")
            return False
        if not q or q.get("kind") != "followup":
            return False
        session_id = q.get("session_id")
        if not session_id:
            return False
        session = store.get_by_session_id(session_id)
        if not session or session.routing not in ("claude_code", "codex"):
            return False
        store.deposit_answer(reply_to_message_id, text)
        label = "Codex" if session.routing == "codex" else "Claude Code"
        await send_message_async(f"Resuming {label} session...", chat_id=chat_id)
        return True

    def _should_use_plan_mode(self, task: str) -> bool:
        """Conservative heuristic: plan mode only for complex-sounding tasks."""
        task_lower = task.lower()
        return any(kw in task_lower for kw in self._PLAN_MODE_KEYWORDS)

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

_telegram_listener: Optional[TelegramBotListener] = None


def get_telegram_listener() -> TelegramBotListener:
    """Get or create TelegramBotListener singleton."""
    global _telegram_listener
    if _telegram_listener is None:
        _telegram_listener = TelegramBotListener()
    return _telegram_listener
