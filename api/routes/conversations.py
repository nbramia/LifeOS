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
    get_store, generate_title, format_conversation_history
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


class MessageResponse(BaseModel):
    """Response with message data."""
    id: str
    role: str
    content: str
    created_at: str
    sources: Optional[list] = None
    routing: Optional[dict] = None


class ConversationDetailResponse(BaseModel):
    """Response with full conversation including messages."""
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[MessageResponse]


class ConversationListResponse(BaseModel):
    """Response with list of conversations."""
    conversations: list[ConversationResponse]


class AskRequest(BaseModel):
    """Request to ask a question in a conversation."""
    question: str


@router.get("", response_model=ConversationListResponse)
async def list_conversations():
    """
    List all conversations sorted by most recent.

    Returns up to 50 conversations with metadata.
    """
    store = get_store()
    conversations = store.list_conversations()

    return ConversationListResponse(
        conversations=[
            ConversationResponse(
                id=c.id,
                title=c.title,
                created_at=c.created_at.isoformat(),
                updated_at=c.updated_at.isoformat(),
                message_count=c.message_count
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
        ]
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
