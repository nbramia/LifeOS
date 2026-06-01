"""
Synthesizer service for LifeOS.

Handles LLM API calls for RAG synthesis.
Uses the local LLM (OpenAI-compatible llama-server) by default.
"""
import asyncio
import base64
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import settings
from api.services.llm_client import get_local_llm
from api.services.resilience import is_retryable_api_error

logger = logging.getLogger(__name__)


def build_message_content(prompt: str, attachments: list[dict] = None) -> str | list:
    """
    Build message content, handling multi-modal if needed.

    Args:
        prompt: The text prompt
        attachments: Optional list of attachments, each with:
            - filename: str
            - media_type: str (e.g., "image/png")
            - data: str (base64 encoded)

    Returns:
        Either a simple string (text-only) or a list of content blocks (multi-modal)
    """
    if not attachments:
        return prompt  # Simple text message (backwards compatible)

    content = []
    text_file_contents = []

    # Process attachments by type
    for att in attachments:
        media_type = att["media_type"]
        filename = att["filename"]
        data = att["data"]

        if media_type.startswith("image/"):
            # Image attachments — local model can't process images directly,
            # so we note their presence in the prompt
            content.append(f"[Image attached: {filename}]")
            logger.debug(f"Image attachment noted: {filename} (not processed by local model)")

        elif media_type == "application/pdf":
            # PDF attachments — similarly just noted
            content.append(f"[PDF attached: {filename}]")
            logger.debug(f"PDF attachment noted: {filename}")

        elif media_type.startswith("text/") or media_type == "application/json":
            # Text file attachments - decode and include in prompt
            try:
                text_content = base64.b64decode(data).decode("utf-8")
                text_file_contents.append(
                    f"\n\n--- Attached File: {filename} ---\n{text_content}\n--- End of {filename} ---"
                )
                logger.debug(f"Added text attachment: {filename}")
            except Exception as e:
                logger.warning(f"Failed to decode text attachment {filename}: {e}")

    # Append text file contents to prompt
    if text_file_contents:
        prompt = prompt + "".join(text_file_contents)

    # For local model, flatten everything into a single string
    if content:
        prefix = "\n".join(content) + "\n\n"
        return prefix + prompt
    return prompt

# Default model tier (kept for API compatibility)
DEFAULT_MODEL_TIER = "sonnet"


class Synthesizer:
    """Service for synthesizing answers using the local LLM."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize synthesizer.

        Args:
            api_key: Ignored (kept for API compatibility).
        """
        self._client = None

    @property
    def client(self):
        """Lazy-load the LLM client."""
        if self._client is None:
            self._client = get_local_llm()
        return self._client

    def synthesize(
        self,
        prompt: str,
        max_tokens: int = 1024,
        model: str = None,
        model_tier: str = None
    ) -> str:
        """
        Generate a synthesized response.

        Args:
            prompt: The full prompt including context and question
            max_tokens: Maximum response length
            model: Ignored (kept for API compatibility)
            model_tier: Ignored (kept for API compatibility)

        Returns:
            Generated response text
        """
        logger.debug("Synthesizing response via local LLM")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                response = self.client.create(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                )
                return response.text
            except Exception as e:
                if attempt < max_retries and is_retryable_api_error(e):
                    import time as _time
                    delay = 2 * (2 ** attempt)
                    logger.warning(f"LLM transient error, retry {attempt + 1}/{max_retries} in {delay}s: {e}")
                    _time.sleep(delay)
                    continue
                logger.error(f"LLM error: {e}")
                raise

    async def stream_response(
        self,
        prompt: str,
        attachments: list[dict] = None,
        max_tokens: int = 1024,
        model: str = None,
        model_tier: str = None
    ):
        """
        Stream a response from the LLM.

        Args:
            prompt: The full prompt including context and question
            attachments: Optional list of attachments for multi-modal requests
            max_tokens: Maximum response length
            model: Ignored (kept for API compatibility)
            model_tier: Ignored (kept for API compatibility)

        Yields:
            Text chunks as they arrive, then a final dict with usage info
        """
        logger.debug("Streaming response via local LLM")

        # Build message content (handles attachments)
        message_content = build_message_content(prompt, attachments)
        if attachments:
            logger.info(f"Request with {len(attachments)} attachment(s)")

        max_retries = 2
        for attempt in range(max_retries + 1):
            text_yielded = False
            try:
                async for event in self.client.astream(
                    messages=[{"role": "user", "content": message_content}],
                    max_tokens=max_tokens,
                ):
                    if event["type"] == "text":
                        text_yielded = True
                        yield event["content"]
                    elif event["type"] == "done":
                        usage = event["usage"]
                        yield {
                            "type": "usage",
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cost_usd": 0.0,  # Local model — no cost
                            "model": "local"
                        }
                break  # success
            except Exception as e:
                if not text_yielded and attempt < max_retries and is_retryable_api_error(e):
                    delay = 2 * (2 ** attempt)
                    logger.warning(f"LLM streaming transient error, retry {attempt + 1}/{max_retries} in {delay}s: {e}")
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"LLM streaming error: {e}")
                raise

    async def get_response(
        self,
        prompt: str,
        max_tokens: int = 2048,
        model: str = None,
        model_tier: str = None
    ) -> str:
        """
        Get a complete response (async wrapper).

        Args:
            prompt: The full prompt
            max_tokens: Maximum response length
            model: Ignored (kept for API compatibility)
            model_tier: Ignored (kept for API compatibility)

        Returns:
            Generated response text
        """
        return self.synthesize(prompt, max_tokens, model, model_tier)


# System prompt for RAG synthesis
SYSTEM_CONTEXT = f"""You are LifeOS, a personal knowledge assistant for {settings.user_name}.
You have access to their Obsidian vault containing notes, meeting transcripts, and personal documents.

Your responses should be:
- Concise and direct (Paul Graham style - no fluff)
- Grounded in the provided context
- Citing sources when making claims

When answering:
1. Use only information from the provided context
2. If the context doesn't contain enough information, say so
3. Reference source files naturally (e.g., "According to the Budget Review notes...")
4. Extract and highlight action items if relevant
5. Be specific with dates, names, and numbers when available

Format:
- Keep answers focused and brief
- Use bullet points for lists
- Include relevant quotes when helpful
- End with sources list if multiple files referenced

Actions you can take directly:
- Create email drafts: Say "draft an email to..." and I'll create a Gmail draft
- Create reminders: Say "remind me..." or "set a reminder..." and I'll schedule a Telegram notification
- Search across calendar, email, drive, messages, and notes

Note: Tasks requiring file operations, terminal commands, code changes, or browser access are automatically routed to Claude Code. If the user asks for something you can't do (edit files, run scripts, browse websites, etc.) and it wasn't auto-routed, mention they can use /claude (Claude Code) or /codex (OpenAI Codex) directly.

If asked to create a reminder or email, respond naturally - the system will handle the action."""


def get_current_datetime_context() -> str:
    """Get the current date and time formatted for the prompt."""
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")


def construct_prompt(
    question: str,
    chunks: list[dict],
    conversation_history: list = None
) -> str:
    """
    Construct the full prompt for the LLM.

    Args:
        question: User's question
        chunks: Retrieved context chunks with metadata
        conversation_history: Optional list of previous messages for context

    Returns:
        Formatted prompt string
    """
    # Get current date/time context
    current_datetime = get_current_datetime_context()

    # Build context section
    if chunks:
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            file_name = chunk.get("file_name", "Unknown")
            content = chunk.get("content", "")
            context_parts.append(f"[Source {i}: {file_name}]\n{content}")

        context = "\n\n---\n\n".join(context_parts)
    else:
        context = "(No relevant context found in the vault)"

    # Build conversation history section
    history_section = ""
    if conversation_history:
        from api.services.conversation_store import format_conversation_history
        formatted_history = format_conversation_history(conversation_history)
        if formatted_history:
            history_section = f"""## Conversation History

{formatted_history}

---

"""

    # Construct full prompt
    prompt = f"""{SYSTEM_CONTEXT}

## Current Date and Time

{current_datetime}

## Context from Vault

{context}

{history_section}## Question

{question}

## Instructions

Answer the question based on the context above. Cite your sources by referencing the file names. If the context doesn't contain enough information to fully answer, acknowledge what's missing. If this is a follow-up question, consider the conversation history for context. Use the current date and time to interpret relative time references like "today", "this week", "tomorrow", etc."""

    return prompt


# Singleton instance
_synthesizer: Synthesizer | None = None


def get_synthesizer() -> Synthesizer:
    """Get or create synthesizer singleton."""
    global _synthesizer
    if _synthesizer is None:
        _synthesizer = Synthesizer()
    return _synthesizer
