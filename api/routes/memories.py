"""
Memories API routes for LifeOS (P6.3).

Provides endpoints for creating, reading, updating, and deleting persistent memories.
Memories are stored in ~/.lifeos/memories.json and included in future conversation context.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.memory_store import get_memory_store, Memory
from api.services.synthesizer import get_synthesizer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memories", tags=["memories"])


class CreateMemoryRequest(BaseModel):
    """Request to create a new memory."""
    content: str = Field(..., min_length=1, description="Memory content (can be casual/natural language)")
    synthesize: bool = Field(default=True, description="Whether to use Claude to format the memory")


class UpdateMemoryRequest(BaseModel):
    """Request to update an existing memory."""
    content: str = Field(..., min_length=1)


class MemoryResponse(BaseModel):
    """Response containing a single memory."""
    id: str
    content: str
    category: str
    keywords: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_memory(cls, memory: Memory) -> "MemoryResponse":
        return cls(
            id=memory.id,
            content=memory.content,
            category=memory.category,
            keywords=memory.keywords,
            created_at=memory.created_at.isoformat() if memory.created_at else "",
            updated_at=memory.updated_at.isoformat() if memory.updated_at else "",
        )


class MemoryListResponse(BaseModel):
    """Response containing a list of memories."""
    memories: list[MemoryResponse]
    total: int


# Prompt for synthesizing casual memory input into structured format
MEMORY_SYNTHESIS_PROMPT = """You are helping organize personal memories for a knowledge assistant.

The user has entered a casual note they want to remember. Your job is to:
1. Rewrite it as a clear, structured memory statement
2. Preserve ALL the specific details (names, spellings, facts)
3. Make it easy to retrieve later when relevant

User's input:
{content}

Rules:
- Keep it concise but complete
- If it's about name spellings/aliases, format as: "Alternative spellings: [Name] may be spelled as [Alt1], [Alt2], [Alt3]"
- If it's about a person, include their name prominently
- If it's a preference, state it clearly
- If it's a fact/decision, include context
- Do NOT add information that wasn't in the original
- Output ONLY the rewritten memory, nothing else"""


async def synthesize_memory(content: str) -> str:
    """
    Use Claude to synthesize casual input into a well-formatted memory.

    Args:
        content: Raw user input (casual language)

    Returns:
        Formatted memory string
    """
    synthesizer = get_synthesizer()
    prompt = MEMORY_SYNTHESIS_PROMPT.format(content=content)

    try:
        response = await synthesizer.get_response(prompt)
        return response.strip()
    except Exception as e:
        logger.warning(f"Failed to synthesize memory, using raw content: {e}")
        return content


@router.post("", response_model=MemoryResponse)
async def create_memory(request: CreateMemoryRequest):
    """
    **Save a persistent memory** that will be included in future conversation context.

    Use this when the user says things like:
    - "Remember that..." or "Don't forget..."
    - "Always check..." or "Note that..."
    - Personal preferences, name spellings, important facts

    Examples:
    - "Remember Taylor goes by 'Tay'" → saves alias for entity resolution
    - "Always ask about my dog Max" → saves personal context
    - "I prefer ES6 syntax" → saves coding preferences

    Memories persist across sessions and are automatically retrieved when relevant.
    """
    store = get_memory_store()

    # Synthesize if requested
    content = request.content
    if request.synthesize:
        content = await synthesize_memory(request.content)
        logger.info(f"Synthesized memory: {content[:100]}...")

    memory = store.create_memory(content)

    return MemoryResponse.from_memory(memory)


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    category: Optional[str] = None,
    limit: int = 100
):
    """
    **List stored memories** to see what the system remembers about the user.

    Use this to review saved memories before adding new ones.
    Filter by category: people, preferences, facts, decisions, reminders, context.
    """
    store = get_memory_store()
    memories = store.list_memories(category=category, limit=limit)

    return MemoryListResponse(
        memories=[MemoryResponse.from_memory(m) for m in memories],
        total=len(memories)
    )


@router.get("/{memory_id}", response_model=MemoryResponse)
async def get_memory(memory_id: str):
    """Get a specific memory by ID."""
    store = get_memory_store()
    memory = store.get_memory(memory_id)

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    return MemoryResponse.from_memory(memory)


@router.put("/{memory_id}", response_model=MemoryResponse)
async def update_memory(memory_id: str, request: UpdateMemoryRequest):
    """Update an existing memory."""
    store = get_memory_store()
    memory = store.update_memory(memory_id, request.content)

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    return MemoryResponse.from_memory(memory)


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    """
    Delete a memory (soft delete).

    The memory is marked as inactive but kept in storage for audit purposes.
    """
    store = get_memory_store()
    deleted = store.delete_memory(memory_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")

    return {"status": "deleted", "id": memory_id}


@router.get("/search/{query}", response_model=MemoryListResponse)
async def search_memories(query: str, limit: int = 10):
    """
    **Search memories by keyword** to find specific saved information.

    Use this to check if a memory already exists before creating a new one,
    or to retrieve specific remembered facts/preferences.
    """
    store = get_memory_store()
    memories = store.search_memories(query, limit=limit)

    return MemoryListResponse(
        memories=[MemoryResponse.from_memory(m) for m in memories],
        total=len(memories)
    )
