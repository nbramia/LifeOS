"""Post-turn conversation titling.

Generates a conversation title from the actual content once the user's
second message in a thread has been persisted, replacing whatever placeholder
("New Conversation") or first-message-truncation title (`generate_title()` in
conversation_store.py) currently sits there. This is the one shared seam for
all three surfaces that persist chat turns — the native `/api/ask/stream`
turn (api/routes/chat.py), the Hermes proxy tee (api/routes/hermes_proxy.py),
and the #711 voice tee (api/routes/voice.py) — each calls `schedule_retitle()`
once its turn is done, instead of three separate titling implementations.

LLM selection mirrors `query_router.py` / `agent_viz_summary.py` / person-fact
filtering: all four use `llm_client.generate_text()`, which prefers the local
llama-server and never touches the paid Claude API, regardless of
`LIFEOS_LLM_BACKEND` — the established pattern in this codebase for cheap,
auxiliary, non-user-facing LLM calls. When the local server is unreachable,
`generate_text()` falls back to the configured remote provider instead of
silently doing nothing (`generate_text`'s local-then-remote retry, #773 —
this used to be pinned to local only, #716's original bug for this exact
caller). That means
titling works on a no-Anthropic-key install (local, remote, or Hermes-backend
chat) and never touches the paid API path. Thinking is explicitly disabled
(`enable_thinking=False`), matching query_router's routing call — titling is
a short classification-like task that doesn't benefit from chain-of-thought
and shouldn't pay for it.

`schedule_retitle()` is fire-and-forget (`asyncio.create_task`) — the turn
that calls it never awaits the title, so a slow or unavailable local LLM
can't delay or break the chat response. Any failure is caught, logged once
(no retry), and leaves the existing title in place.

There's no rename feature in the UI or API today — a conversation's title is
only ever system-set (the truncation default, or this module). So "never
retitle a manual rename" reduces to "retitle exactly once": the guard below
fires only when the conversation has *exactly* 2 user messages, checked fresh
against the store on every call. Calling `schedule_retitle()` after every
turn — even ones that persisted nothing new, even repeatedly — is therefore
safe and idempotent; it silently no-ops once the count moves past 2.
"""
from __future__ import annotations

import asyncio
import logging
import re

from api.services.conversation_store import format_conversation_history, get_store
from api.services.llm_client import generate_text

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 50

_TITLE_PROMPT = """Based on this conversation, write a short, specific title that \
captures what it's actually about.

{transcript}

Rules:
- Plain text only. No quotes, no markdown, no trailing punctuation.
- {max_len} characters or fewer.
- Describe the specific topic — never a generic label like "Conversation" or "Chat".

Respond with the title text only, nothing else."""


def schedule_retitle(conversation_id: str | None) -> None:
    """Fire-and-forget entry point for the three persisting paths.

    No-op for a missing id, and no-op outside a running event loop (all three
    call sites are async route/tee handlers, so this only guards test/script
    contexts that call in synchronously).
    """
    if not conversation_id:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(_maybe_retitle(conversation_id))


async def _maybe_retitle(conversation_id: str) -> None:
    try:
        store = get_store()
        messages = store.get_messages(conversation_id)
        user_message_count = sum(1 for m in messages if m.role == "user")
        # Short/trivial conversations (fewer than 2 user messages) keep
        # whatever title they already have. Exactly 2 (not >=2) makes this
        # the one-time intelligent pass rather than a re-title on every turn.
        if user_message_count != 2:
            return

        transcript = format_conversation_history(messages, max_tokens=1000)
        if not transcript:
            return

        prompt = _TITLE_PROMPT.format(transcript=transcript, max_len=MAX_TITLE_LENGTH)
        text = await generate_text(
            prompt, max_tokens=80, temperature=0.3, timeout=20.0,
            enable_thinking=False,
        )
        title = sanitize_title(text)
        if title:
            store.update_title(conversation_id, title)
    except Exception as exc:  # noqa: BLE001 -- never break/delay the turn over a title
        logger.warning("conversation titling failed for %s: %s", conversation_id, exc)


def sanitize_title(text: str) -> str:
    """Clean a raw model response into a plain-text title.

    Strips surrounding quotes/markdown emphasis a model might add despite the
    prompt, collapses whitespace, drops trailing punctuation, and truncates
    at a word boundary to `MAX_TITLE_LENGTH`. Returns "" for empty/junk input
    so the caller can treat that as "keep the existing title".
    """
    title = (text or "").strip()
    # A model occasionally answers "Title: ..." despite the prompt asking
    # for bare text -- strip that one common leading-label failure mode
    # before the general quote/markdown stripping below.
    title = re.sub(r"(?i)^title\s*[:\-]\s*", "", title)
    title = title.strip("\"'`*_“”‘’")
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip(".!?,;: ")
    if len(title) > MAX_TITLE_LENGTH:
        truncated = title[:MAX_TITLE_LENGTH]
        last_space = truncated.rfind(" ")
        if last_space > MAX_TITLE_LENGTH // 2:
            truncated = truncated[:last_space]
        title = truncated.strip()
    return title
