"""
Slack API endpoints for LifeOS.

Provides search and conversation access to Slack data.
Supports both vector search (semantic) and metadata filtering.
"""
import time
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.services.slack_integration import (
    get_slack_client,
    is_slack_enabled,
    SlackAPIError,
)
from api.services.slack_indexer import get_slack_indexer
from api.services.slack_sync import get_slack_sync, run_slack_sync

router = APIRouter(prefix="/api/slack", tags=["slack"])


# Request/Response Models


class SlackSearchRequest(BaseModel):
    """Slack search request schema."""
    query: str = Field(..., min_length=1, description="Search query text")
    top_k: int = Field(default=20, ge=1, le=100, description="Number of results")
    channel_id: Optional[str] = Field(None, description="Filter by channel ID")
    channel_type: Optional[str] = Field(
        None,
        description="Filter by channel type: im, mpim, channel, group"
    )
    user_id: Optional[str] = Field(None, description="Filter by sender user ID")


class SlackMessageResult(BaseModel):
    """Individual Slack message search result."""
    content: str
    channel_id: str
    channel_name: str
    channel_type: str
    user_id: str
    user_name: str
    timestamp: str
    thread_ts: Optional[str] = None
    score: float
    semantic_score: Optional[float] = None
    recency_score: Optional[float] = None


class SlackSearchResponse(BaseModel):
    """Slack search response schema."""
    results: list[SlackMessageResult]
    query_time_ms: int
    total_indexed: int


class SlackChannelResponse(BaseModel):
    """Slack channel info."""
    channel_id: str
    name: str
    display_name: str
    is_private: bool
    is_im: bool
    is_mpim: bool
    member_count: int


class SlackConversationsResponse(BaseModel):
    """Response for conversations list."""
    channels: list[SlackChannelResponse]
    total: int


class SlackSyncResponse(BaseModel):
    """Response for sync operations."""
    status: str
    messages_indexed: int = 0
    interactions_created: int = 0
    users_synced: int = 0
    elapsed_seconds: float = 0
    errors: list[str] = []


class SlackStatusResponse(BaseModel):
    """Slack integration status."""
    enabled: bool
    connected: bool
    indexed_messages: int
    indexed_channels: int


# Helper Functions


def _require_slack_enabled():
    """Raise 503 if Slack is not enabled."""
    if not is_slack_enabled():
        raise HTTPException(
            status_code=503,
            detail="Slack integration not enabled. Set SLACK_USER_TOKEN in .env"
        )


# Endpoints


@router.get("/status", response_model=SlackStatusResponse)
async def get_slack_status():
    """
    Get Slack integration status.

    Returns whether Slack is enabled, connected, and index statistics.
    """
    enabled = is_slack_enabled()
    connected = False
    indexed_messages = 0
    indexed_channels = 0

    if enabled:
        try:
            client = get_slack_client()
            connected = client.is_connected()

            indexer = get_slack_indexer()
            indexed_messages = indexer.get_message_count()
            indexed_channels = len(indexer.get_indexed_channels())
        except Exception:
            pass

    return SlackStatusResponse(
        enabled=enabled,
        connected=connected,
        indexed_messages=indexed_messages,
        indexed_channels=indexed_channels,
    )


@router.post("/search", response_model=SlackSearchResponse)
async def search_slack(request: SlackSearchRequest):
    """
    **Search Slack messages** using semantic search.

    Searches indexed Slack DM and channel messages using vector similarity.
    Returns matching messages with channel, user, and timestamp metadata.

    Use this for:
    - "What did John say about the deadline?"
    - "Find messages about the product launch"
    - "Search for budget discussions"

    Filters:
    - channel_id: Specific channel to search
    - channel_type: "im" (DMs), "mpim" (group DMs), "channel", "group"
    - user_id: Messages from specific user
    """
    _require_slack_enabled()

    start_time = time.time()

    indexer = get_slack_indexer()

    # Perform search
    raw_results = indexer.search(
        query=request.query,
        top_k=request.top_k,
        channel_id=request.channel_id,
        channel_type=request.channel_type,
        user_id=request.user_id,
    )

    # Format results
    results = []
    for r in raw_results:
        results.append(SlackMessageResult(
            content=r.get("content", ""),
            channel_id=r.get("channel_id", ""),
            channel_name=r.get("channel_name", ""),
            channel_type=r.get("channel_type", ""),
            user_id=r.get("user_id", ""),
            user_name=r.get("user_name", ""),
            timestamp=r.get("timestamp", ""),
            thread_ts=r.get("thread_ts"),
            score=r.get("score", 0),
            semantic_score=r.get("semantic_score"),
            recency_score=r.get("recency_score"),
        ))

    query_time_ms = int((time.time() - start_time) * 1000)

    return SlackSearchResponse(
        results=results,
        query_time_ms=query_time_ms,
        total_indexed=indexer.get_message_count(),
    )


@router.get("/search")
async def search_slack_get(
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(20, ge=1, le=100),
    channel_id: Optional[str] = None,
    channel_type: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """
    Search Slack messages (GET version for convenience).

    Same as POST /search but with query parameters.
    """
    request = SlackSearchRequest(
        query=q,
        top_k=top_k,
        channel_id=channel_id,
        channel_type=channel_type,
        user_id=user_id,
    )
    return await search_slack(request)


@router.get("/conversations", response_model=SlackConversationsResponse)
async def list_conversations():
    """
    List accessible Slack conversations.

    Returns all DMs, group DMs, and channels the user has access to.
    """
    _require_slack_enabled()

    client = get_slack_client()

    try:
        channels = client.list_channels()
    except SlackAPIError as e:
        raise HTTPException(status_code=502, detail=f"Slack API error: {e}")

    # Format response
    formatted = []
    for ch in channels:
        # Get display name for DMs
        display_name = ch.name
        if ch.is_im:
            # Resolve user name
            user = client.get_user_cached(ch.name)
            if user:
                display_name = f"DM with {user.real_name or user.display_name or user.username}"
            else:
                display_name = f"DM with {ch.name}"
        elif ch.is_mpim:
            display_name = f"Group DM: {ch.name}"
        else:
            display_name = f"#{ch.name}"

        formatted.append(SlackChannelResponse(
            channel_id=ch.channel_id,
            name=ch.name,
            display_name=display_name,
            is_private=ch.is_private,
            is_im=ch.is_im,
            is_mpim=ch.is_mpim,
            member_count=ch.member_count,
        ))

    return SlackConversationsResponse(
        channels=formatted,
        total=len(formatted),
    )


@router.post("/sync", response_model=SlackSyncResponse)
async def sync_slack(full: bool = False):
    """
    Sync Slack data to LifeOS.

    Performs message indexing and optionally creates CRM interactions.
    Automatically recalculates relationship strengths after sync.

    Args:
        full: If True, re-sync all history. If False, incremental sync only.
    """
    _require_slack_enabled()

    try:
        result = run_slack_sync(full=full)

        # Extract stats from result
        messages_indexed = 0
        interactions_created = 0
        users_synced = 0
        errors = result.get("errors", [])

        if "users" in result:
            users_synced = result["users"].get("created", 0) + result["users"].get("updated", 0)

        if "messages" in result:
            messages_indexed = result["messages"].get("messages_indexed", 0)
            interactions_created = result["messages"].get("interactions_created", 0)
            if result["messages"].get("errors"):
                errors.extend(result["messages"]["errors"])
        else:
            # Incremental sync returns flat structure
            messages_indexed = result.get("messages_indexed", 0)
            interactions_created = result.get("interactions_created", 0)

        # Automatically recalculate relationship strengths if interactions were created
        if interactions_created > 0:
            from api.services.relationship_metrics import update_all_strengths
            try:
                update_all_strengths()
            except Exception as e:
                errors.append(f"Strength update failed: {e}")

        return SlackSyncResponse(
            status=result.get("status", "success"),
            messages_indexed=messages_indexed,
            interactions_created=interactions_created,
            users_synced=users_synced,
            elapsed_seconds=result.get("elapsed_seconds", 0),
            errors=errors,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@router.get("/channels/{channel_id}/messages")
async def get_channel_messages(
    channel_id: str,
    limit: int = Query(100, ge=1, le=1000),
    oldest: Optional[str] = None,
    latest: Optional[str] = None,
):
    """
    Get recent messages from a specific channel.

    This fetches live data from Slack API, not the indexed data.

    Args:
        channel_id: Slack channel ID
        limit: Max messages to return
        oldest: Only return messages after this ISO timestamp
        latest: Only return messages before this ISO timestamp
    """
    _require_slack_enabled()

    client = get_slack_client()

    # Parse timestamps if provided
    oldest_dt = None
    latest_dt = None
    if oldest:
        try:
            oldest_dt = datetime.fromisoformat(oldest)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid oldest timestamp format")
    if latest:
        try:
            latest_dt = datetime.fromisoformat(latest)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid latest timestamp format")

    try:
        messages = client.get_channel_history(
            channel_id=channel_id,
            limit=limit,
            oldest=oldest_dt,
            latest=latest_dt,
        )

        return {
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        }

    except SlackAPIError as e:
        raise HTTPException(status_code=502, detail=f"Slack API error: {e}")
