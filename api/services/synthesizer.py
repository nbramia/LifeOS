"""
Synthesizer service for LifeOS.

Handles Claude API calls for RAG synthesis.

NOTE: anthropic library is imported lazily to speed up test collection.
"""
import base64
import logging
from datetime import datetime
from typing import Optional, Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

from config.settings import settings
from api.services.model_selector import get_claude_model_name

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)


def build_message_content(prompt: str, attachments: list[dict] = None) -> str | list:
    """
    Build Claude message content, handling multi-modal if needed.

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
            # Image attachments - send as image blocks
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data
                }
            })
            logger.debug(f"Added image attachment: {filename}")

        elif media_type == "application/pdf":
            # PDF attachments - send as document blocks
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data
                }
            })
            logger.debug(f"Added PDF attachment: {filename}")

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

    # Add text prompt last (Claude expects images before text for best results)
    content.append({"type": "text", "text": prompt})

    return content

# Default model tier
DEFAULT_MODEL_TIER = "sonnet"


class Synthesizer:
    """Service for synthesizing answers using Claude."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize synthesizer.

        Args:
            api_key: Anthropic API key (defaults to settings)
        """
        # Use provided key, but only fall back to settings if not explicitly passed
        self.api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._client: Any = None

    def _validate_api_key(self):
        """Validate that API key is configured."""
        if not self.api_key or not self.api_key.strip():
            raise ValueError(
                "Anthropic API key not configured. "
                "Please set ANTHROPIC_API_KEY in your .env file."
            )

    @property
    def client(self) -> "anthropic.Anthropic":
        """Lazy-load the Anthropic client."""
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def synthesize(
        self,
        prompt: str,
        max_tokens: int = 1024,
        model: str = None,
        model_tier: str = None
    ) -> str:
        """
        Generate a synthesized response using Claude.

        Args:
            prompt: The full prompt including context and question
            max_tokens: Maximum response length
            model: Full Claude model name (overrides model_tier)
            model_tier: Model tier ("haiku", "sonnet", "opus")

        Returns:
            Generated response text

        Raises:
            Exception: If API call fails
        """
        # Validate API key before making request
        self._validate_api_key()

        # Resolve model name: explicit model > model_tier > default
        if model is None:
            tier = model_tier or DEFAULT_MODEL_TIER
            model = get_claude_model_name(tier)

        logger.debug(f"Using model: {model}")

        import anthropic

        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            return response.content[0].text
        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            raise
        except Exception as e:
            logger.error(f"Synthesizer error: {e}")
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
        Stream a response from Claude.

        Args:
            prompt: The full prompt including context and question
            attachments: Optional list of attachments for multi-modal requests
            max_tokens: Maximum response length
            model: Full Claude model name (overrides model_tier)
            model_tier: Model tier ("haiku", "sonnet", "opus")

        Yields:
            Text chunks as they arrive, then a final dict with usage info
        """
        # Validate API key before making request
        self._validate_api_key()

        # Resolve model name: explicit model > model_tier > default
        if model is None:
            tier = model_tier or DEFAULT_MODEL_TIER
            model = get_claude_model_name(tier)

        logger.debug(f"Streaming with model: {model}")

        # Build message content (handles multi-modal if attachments present)
        message_content = build_message_content(prompt, attachments)
        if attachments:
            logger.info(f"Multi-modal request with {len(attachments)} attachment(s)")

        import anthropic

        try:
            with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "user", "content": message_content}
                ]
            ) as stream:
                for text in stream.text_stream:
                    yield text

                # Get final message with usage data
                final_message = stream.get_final_message()
                if final_message and final_message.usage:
                    usage = final_message.usage
                    # Calculate cost based on model pricing (per million tokens)
                    # Sonnet 3.5: $3/M input, $15/M output
                    # Haiku 3.5: $0.80/M input, $4/M output
                    if "haiku" in model.lower():
                        input_cost = (usage.input_tokens / 1_000_000) * 0.80
                        output_cost = (usage.output_tokens / 1_000_000) * 4.00
                    else:  # Default to Sonnet pricing
                        input_cost = (usage.input_tokens / 1_000_000) * 3.00
                        output_cost = (usage.output_tokens / 1_000_000) * 15.00

                    total_cost = input_cost + output_cost

                    yield {
                        "type": "usage",
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cost_usd": total_cost,
                        "model": model
                    }
        except anthropic.APIError as e:
            logger.error(f"Claude API streaming error: {e}")
            raise
        except Exception as e:
            logger.error(f"Synthesizer streaming error: {e}")
            raise

    async def get_response(
        self,
        prompt: str,
        max_tokens: int = 2048,
        model: str = None,
        model_tier: str = None
    ) -> str:
        """
        Get a complete response from Claude (async wrapper).

        Args:
            prompt: The full prompt
            max_tokens: Maximum response length
            model: Full Claude model name (overrides model_tier)
            model_tier: Model tier ("haiku", "sonnet", "opus")

        Returns:
            Generated response text
        """
        return self.synthesize(prompt, max_tokens, model, model_tier)


# System prompt for RAG synthesis
SYSTEM_CONTEXT = """You are LifeOS, a personal knowledge assistant for Nathan.
You have access to his Obsidian vault containing notes, meeting transcripts, and personal documents.

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

Actions you can take:
- Create email drafts: Say "draft an email to..." and I'll create a Gmail draft
- Create reminders: Say "remind me..." or "set a reminder..." and I'll schedule a Telegram notification
- Search across calendar, email, drive, messages, and notes

If asked to create a reminder or email, respond naturally - the system will handle the action."""


def get_current_datetime_context() -> str:
    """Get the current date and time formatted for the prompt."""
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")


def construct_prompt(
    question: str,
    chunks: list[dict],
    conversation_history: list = None
) -> str:
    """
    Construct the full prompt for Claude.

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
