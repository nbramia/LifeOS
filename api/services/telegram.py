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
        {"answer": str, "conversation_id": str}
    """
    port = settings.port
    body: dict = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id

    full_text = ""
    conv_id = conversation_id

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"http://localhost:{port}/api/ask/stream",
            json=body,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content":
                    full_text += event.get("content", "")
                elif event.get("type") == "conversation_id":
                    conv_id = event.get("conversation_id", conv_id)
                elif event.get("type") == "error":
                    error_msg = event.get("message", "Unknown error")
                    logger.error(f"Chat pipeline error: {error_msg}")
                    if not full_text:
                        full_text = f"Error: {error_msg}"

    return {"answer": full_text, "conversation_id": conv_id}


# ---------------------------------------------------------------------------
# Bot listener (long-polling)
# ---------------------------------------------------------------------------

class TelegramBotListener:
    """
    Background thread that receives messages via Telegram long-polling.

    Forwards messages through the LifeOS chat pipeline and sends responses back.
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Conversation state: chat_id -> conversation_id
        self._conversations: dict[str, str] = {}
        self._last_update_id = 0

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
                return updates
        except httpx.ReadTimeout:
            # Normal for long-polling
            return []
        except Exception as e:
            logger.error(f"getUpdates error: {e}")
            await asyncio.sleep(2)
            return []

    async def _handle_update(self, update: dict):
        """Process a single Telegram update."""
        message = update.get("message")
        if not message:
            return

        text = message.get("text", "").strip()
        chat_id = str(message["chat"]["id"])

        # Only respond to the configured chat
        if chat_id != settings.telegram_chat_id:
            logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        if not text:
            return

        logger.info(f"Telegram message: {text[:100]}")

        # Handle commands
        if text.startswith("/"):
            await self._handle_command(text, chat_id)
            return

        # Send through chat pipeline
        try:
            conv_id = self._conversations.get(chat_id)
            result = await chat_via_api(text, conversation_id=conv_id)
            self._conversations[chat_id] = result["conversation_id"]

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

    async def _handle_command(self, text: str, chat_id: str):
        """Handle bot commands."""
        command = text.split()[0].lower()

        if command == "/new":
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

        elif command == "/help":
            help_text = (
                "*LifeOS Telegram Bot*\n\n"
                "Send any message to query LifeOS (calendar, emails, vault, etc.)\n\n"
                "*Commands:*\n"
                "/new - Start a new conversation\n"
                "/status - Check LifeOS server health\n"
                "/help - Show this message"
            )
            await send_message_async(help_text, chat_id=chat_id)

        else:
            await send_message_async(
                f"Unknown command: {command}. Try /help",
                chat_id=chat_id,
            )


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
