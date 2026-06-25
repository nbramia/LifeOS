"""
Conversation API endpoints for LifeOS.

Manages conversation threads with persistence.
"""
import json
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.services.conversation_store import (
    get_store, generate_title
)
from api.services.hybrid_search import HybridSearch
from api.services.synthesizer import construct_prompt, get_synthesizer
from api.services.query_router import QueryRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""
    title: Optional[str] = None


class ConversationResponse(BaseModel):
    """Response with conversation data."""
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    persona_id: str = "primary"


class MessageResponse(BaseModel):
    """Response with message data."""
    id: str
    role: str
    content: str
    created_at: str
    sources: Optional[list] = None
    routing: Optional[dict] = None


class PendingQuestionResponse(BaseModel):
    """An open [CLARIFY]/[GOAL] from this conversation's spawned session (#403)."""
    session_id: str
    question: str
    kind: str  # clarification | goal_approval | followup


class ConversationDetailResponse(BaseModel):
    """Response with full conversation including messages."""
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[MessageResponse]
    # Present only when an orchestrating-persona session spawned by this
    # conversation is currently awaiting an answer (#403). The client renders
    # an answer affordance and POSTs to `{id}/answer`. Absent/None otherwise.
    pending_question: Optional[PendingQuestionResponse] = None
    # Whether a session spawned by this conversation is still running (#311).
    # True iff the conversation links a session whose status is non-terminal.
    # False when there's no linked session or it has reached a terminal status
    # (completed/failed/budget_exceeded). The client's result-streaming poll
    # uses this to STOP once the session is done and nothing awaits an answer —
    # otherwise the 4s poll would run forever after the session finishes.
    agent_session_active: bool = False


class ConversationListResponse(BaseModel):
    """Response with list of conversations."""
    conversations: list[ConversationResponse]


class AskRequest(BaseModel):
    """Request to ask a question in a conversation."""
    question: str


class AnswerRequest(BaseModel):
    """Request to answer a spawned session's open [CLARIFY]/[GOAL] question."""
    answer: str


@router.get("", response_model=ConversationListResponse)
async def list_conversations(persona_id: str = "primary"):
    """
    List conversations for a persona, sorted by most recent.

    Returns up to 50 conversations with metadata. ``persona_id`` defaults to
    ``"primary"`` so web chat (which omits it) keeps seeing its own threads;
    pass e.g. ``?persona_id=fitness`` to scope to a specialized persona.
    """
    store = get_store()
    conversations = store.list_conversations(persona_id=persona_id)

    return ConversationListResponse(
        conversations=[
            ConversationResponse(
                id=c.id,
                title=c.title,
                created_at=c.created_at.isoformat(),
                updated_at=c.updated_at.isoformat(),
                message_count=c.message_count,
                persona_id=c.persona_id,
            )
            for c in conversations
        ]
    )


@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(request: CreateConversationRequest):
    """
    Create a new conversation thread.

    If no title provided, defaults to "New Conversation".
    """
    store = get_store()
    conv = store.create_conversation(title=request.title)

    return ConversationResponse(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        message_count=0
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(conversation_id: str):
    """
    Get a conversation with all its messages.
    """
    store = get_store()
    conv = store.get_conversation(conversation_id)

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = store.get_messages(conversation_id)

    # Surface an open [CLARIFY]/[GOAL] from a session this conversation spawned
    # (#403) so the client can show an answer affordance, and report whether that
    # session is still running (#311) so the client's result-streaming poll can
    # stop once it's done. Both are best-effort: a lookup failure (or no spawned
    # session) just leaves pending_question=None / agent_session_active=False and
    # never breaks the read.
    pending_question = None
    agent_session_active = False
    if conv.agent_session_id:
        try:
            from api.services.agent_worker.session_store import (
                SessionStore,
                TERMINAL_STATUSES,
            )
            session_store = SessionStore()
            q = session_store.get_open_question_by_session_id(conv.agent_session_id)
            if q:
                pending_question = PendingQuestionResponse(
                    session_id=conv.agent_session_id,
                    question=q.get("question") or "",
                    kind=q.get("kind") or "clarification",
                )
            # Active = the linked session row exists and hasn't reached a
            # terminal status. A missing session row (never created, or pruned)
            # counts as not-active, so the client stops polling.
            session = session_store.get_by_session_id(conv.agent_session_id)
            agent_session_active = bool(
                session is not None and session.status not in TERMINAL_STATUSES
            )
        except Exception:  # noqa: BLE001
            logger.warning("pending-question lookup failed", exc_info=True)

    return ConversationDetailResponse(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=m.created_at.isoformat(),
                sources=m.sources,
                routing=m.routing
            )
            for m in messages
        ],
        pending_question=pending_question,
        agent_session_active=agent_session_active,
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str):
    """
    Delete a conversation and all its messages.
    """
    store = get_store()
    deleted = store.delete_conversation(conversation_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.post("/{conversation_id}/ask")
async def ask_in_conversation(conversation_id: str, request: AskRequest):
    """
    Ask a question within a conversation.

    Streams the response using Server-Sent Events.
    Persists both the question and answer to the conversation.
    """
    store = get_store()
    conv = store.get_conversation(conversation_id)

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    async def generate():
        try:
            # Save user message
            store.add_message(conversation_id, "user", request.question)

            # Auto-update title if this is the first message
            if conv.message_count == 0:
                new_title = generate_title(request.question)
                store.update_title(conversation_id, new_title)
                yield f"data: {json.dumps({'type': 'title_update', 'title': new_title})}\n\n"

            # Route query
            query_router = QueryRouter()
            routing_result = await query_router.route(request.question)

            logger.info(
                f"Query routed to: {routing_result.sources} "
                f"(latency: {routing_result.latency_ms}ms)"
            )

            yield f"data: {json.dumps({'type': 'routing', 'sources': routing_result.sources, 'reasoning': routing_result.reasoning, 'latency_ms': routing_result.latency_ms})}\n\n"

            # Get conversation history for context
            history = store.get_messages(conversation_id, limit=10)
            # Exclude the message we just added (it's the current question)
            history = history[:-1] if history else []

            # Get relevant chunks using hybrid search (vector + BM25)
            chunks = []
            if "vault" in routing_result.sources or not routing_result.sources:
                hybrid_search = HybridSearch()
                chunks = hybrid_search.search(query=request.question, top_k=10)

            # Send sources
            sources = []
            if chunks:
                seen_files = set()
                for chunk in chunks:
                    file_name = chunk.get('metadata', {}).get('file_name', '')
                    if file_name and file_name not in seen_files:
                        seen_files.add(file_name)
                        sources.append({
                            'file_name': file_name,
                            'file_path': chunk.get('metadata', {}).get('file_path', ''),
                        })

            yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

            # Construct prompt with conversation history
            prompt = construct_prompt(
                request.question,
                chunks,
                conversation_history=history
            )

            # Stream from Claude
            synthesizer = get_synthesizer()
            full_response = ""

            async for content in synthesizer.stream_response(prompt):
                full_response += content
                yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"
                await asyncio.sleep(0)

            # Save assistant message with metadata
            store.add_message(
                conversation_id,
                "assistant",
                full_response,
                sources=sources,
                routing={
                    "sources": routing_result.sources,
                    "reasoning": routing_result.reasoning
                }
            )

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Error in conversation ask: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@router.post("/{conversation_id}/answer")
async def answer_in_conversation(conversation_id: str, request: AnswerRequest):
    """Answer the [CLARIFY]/[GOAL] question of the session this conversation spawned.

    Web/voice parity for orchestrating personas (#403). When an orchestrating
    persona (e.g. doctor) is selected on `/api/ask/stream`, the turn spawns a
    background Claude Code session and links it to this conversation. If that
    session emits `[CLARIFY]`/`[GOAL]`, the worker registers an open
    `pending_questions` row keyed on the session. This endpoint deposits the
    answer onto that existing row via the **session-keyed** deposit — preserving
    its `kind` so the worker's existing tick resumes it through the same path a
    Telegram reply takes (`_resume_goal` / `_resume_as_followup`). No second
    resume mechanism, no Telegram `message_id` needed.

    The complementary output direction (streaming the session's results back
    into this thread) is #311.
    """
    text = (request.answer or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="answer cannot be empty")

    store = get_store()
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    session_id = conv.agent_session_id
    if not session_id:
        raise HTTPException(
            status_code=409,
            detail="this conversation has no spawned session awaiting an answer",
        )

    from api.services.agent_worker.session_store import SessionStore
    session_store = SessionStore()
    deposited = session_store.deposit_answer_by_session_id(session_id, text)
    if not deposited:
        # No open question — either the session never asked, already got an
        # answer, or the question timed out. Idempotent: a second answer to an
        # already-answered question is a no-op, surfaced as a 409 (not a crash).
        raise HTTPException(
            status_code=409,
            detail="no open question to answer for this conversation's session",
        )

    # Echo the answer into the conversation so the thread reflects the
    # round-trip (the session's resumed output arrives separately, #311).
    store.add_message(conversation_id, "user", text)
    return {"ok": True, "session_id": session_id, "status": "answer_deposited"}
