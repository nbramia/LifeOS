"""
Slack Message Indexer for LifeOS.

Indexes Slack DM and channel messages into ChromaDB for semantic search.
Integrates with the CRM for person interactions and relationship scoring.

Features:
- Indexes DM messages with user and channel metadata
- Supports semantic search over Slack content
- Creates Interaction records for CRM integration
- Handles incremental and full sync modes
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from api.services.slack_integration import (
    SlackClient,
    SlackMessage,
    SlackChannel,
    SlackUser,
    get_slack_client,
    get_workspace_id,
    is_slack_enabled,
    SLACK_TEAM_ID,
)
from api.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)

# Collection name for Slack messages
SLACK_COLLECTION = "lifeos_slack"


class SlackIndexer:
    """
    Indexes Slack messages into ChromaDB for semantic search.

    Messages are stored with metadata including:
    - source_type: "slack"
    - channel_id: Slack channel ID
    - channel_name: Human-readable channel name
    - channel_type: "im", "mpim", "channel", or "group"
    - user_id: Slack user ID who sent the message
    - user_name: Human-readable user name
    - timestamp: ISO format timestamp
    - thread_ts: Parent thread timestamp (if reply)
    - team_id: Slack workspace ID
    """

    def __init__(self, client: Optional[SlackClient] = None):
        """
        Initialize the Slack indexer.

        Args:
            client: Optional SlackClient instance (uses singleton if not provided)
        """
        self._vector_store: Optional[VectorStore] = None
        self._client = client
        self._user_cache: dict[str, SlackUser] = {}
        self._channel_cache: dict[str, SlackChannel] = {}

    @property
    def vector_store(self) -> VectorStore:
        """Lazy load vector store with Slack collection."""
        if self._vector_store is None:
            self._vector_store = VectorStore(collection_name=SLACK_COLLECTION)
        return self._vector_store

    @property
    def client(self) -> SlackClient:
        """Get Slack client (lazy load singleton)."""
        if self._client is None:
            self._client = get_slack_client()
        return self._client

    @property
    def collection(self):
        """Get the underlying ChromaDB collection for direct operations."""
        return self.vector_store._collection

    def _get_channel_type(self, channel: SlackChannel) -> str:
        """Determine channel type string from channel object."""
        if channel.is_im:
            return "im"
        elif channel.is_mpim:
            return "mpim"
        elif channel.is_private:
            return "group"
        else:
            return "channel"

    def _get_channel_display_name(
        self,
        channel: SlackChannel,
        workspace_id: str = "default"
    ) -> str:
        """Get human-readable channel name, resolving DM usernames."""
        if channel.is_im:
            # For DMs, the channel name is the user ID - resolve to real name
            user = self.client.get_user_cached(channel.name, workspace_id)
            if user:
                return f"DM with {user.real_name or user.display_name or user.username}"
            return f"DM with {channel.name}"
        elif channel.is_mpim:
            return f"Group DM: {channel.name}"
        else:
            return f"#{channel.name}"

    def _get_user_display_name(self, user_id: str, workspace_id: str = "default") -> str:
        """Get human-readable user name from user ID."""
        if not user_id:
            return "Unknown"
        user = self.client.get_user_cached(user_id, workspace_id)
        if user:
            return user.real_name or user.display_name or user.username
        return user_id

    def index_message(
        self,
        message: SlackMessage,
        channel_name: str,
        channel_type: str,
        user_name: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> bool:
        """
        Index a single Slack message into ChromaDB.

        Args:
            message: SlackMessage object to index
            channel_name: Human-readable channel name
            channel_type: Type of channel (im, mpim, channel, group)
            user_name: Human-readable sender name (resolved if not provided)
            team_id: Slack workspace ID

        Returns:
            True if indexed successfully, False otherwise
        """
        if not message.text or not message.text.strip():
            # Skip empty messages
            return False

        try:
            # Resolve user name if not provided
            if user_name is None:
                user_name = self._get_user_display_name(message.user_id)

            # Create document ID per PRD: slack:{channel_id}:{message_ts}
            doc_id = f"slack:{message.channel_id}:{message.ts}"

            # Build metadata for filtering and display
            metadata = {
                "file_path": doc_id,
                "file_name": f"Slack: {channel_name}",
                "modified_date": message.timestamp.strftime("%Y-%m-%d"),
                "note_type": "slack_message",
                "people": [user_name] if user_name and user_name != "Unknown" else [],
                "tags": ["slack", channel_type],
            }

            # Slack-specific metadata stored in chunk
            extra_meta = {
                "source_type": "slack",
                "channel_id": message.channel_id,
                "channel_name": channel_name,
                "channel_type": channel_type,
                "user_id": message.user_id,
                "user_name": user_name,
                "timestamp": message.timestamp.isoformat(),
                "team_id": team_id or SLACK_TEAM_ID or "default",
            }

            if message.thread_ts:
                extra_meta["thread_ts"] = message.thread_ts

            # Create a single chunk for this message
            chunks = [{
                "content": message.text,
                "chunk_index": 0,
                **extra_meta,
            }]

            # Add to vector store (upsert behavior via delete + add)
            self.vector_store.add_document(chunks=chunks, metadata=metadata)
            return True

        except Exception as e:
            logger.error(f"Failed to index message {message.ts}: {e}")
            return False

    def index_messages(
        self,
        messages: list[SlackMessage],
        channel: SlackChannel,
        workspace_id: str = "default",
    ) -> int:
        """
        Index multiple messages from a channel.

        Args:
            messages: List of SlackMessage objects
            channel: SlackChannel object
            workspace_id: Workspace ID for user lookups

        Returns:
            Number of messages successfully indexed
        """
        if not messages:
            return 0

        channel_name = self._get_channel_display_name(channel, workspace_id)
        channel_type = self._get_channel_type(channel)
        team_id = SLACK_TEAM_ID or workspace_id

        indexed = 0
        for message in messages:
            user_name = self._get_user_display_name(message.user_id, workspace_id)
            if self.index_message(
                message=message,
                channel_name=channel_name,
                channel_type=channel_type,
                user_name=user_name,
                team_id=team_id,
            ):
                indexed += 1

        return indexed

    def search(
        self,
        query: str,
        top_k: int = 20,
        channel_id: Optional[str] = None,
        channel_type: Optional[str] = None,
        user_id: Optional[str] = None,
        recency_weight: float = 0.4,
    ) -> list[dict]:
        """
        Search Slack messages by semantic similarity.

        Args:
            query: Search query text
            top_k: Number of results to return
            channel_id: Filter by specific channel
            channel_type: Filter by channel type (im, mpim, channel, group)
            user_id: Filter by message sender
            recency_weight: Weight for recency vs semantic similarity

        Returns:
            List of result dicts with content, metadata, and score
        """
        filters = {}
        if channel_id:
            filters["channel_id"] = channel_id
        if channel_type:
            filters["channel_type"] = channel_type
        if user_id:
            filters["user_id"] = user_id

        return self.vector_store.search(
            query=query,
            top_k=top_k,
            filters=filters if filters else None,
            recency_weight=recency_weight,
        )

    def get_message_count(self) -> int:
        """Get total number of indexed Slack messages."""
        return self.vector_store.get_document_count()

    def delete_channel_messages(self, channel_id: str) -> int:
        """
        Delete all indexed messages from a channel.

        Args:
            channel_id: Channel ID to delete messages for

        Returns:
            Number of messages deleted
        """
        # Find all messages with this channel_id
        results = self.collection.get(
            where={"channel_id": channel_id},
            include=[]
        )

        if results["ids"]:
            count = len(results["ids"])
            self.collection.delete(ids=results["ids"])
            return count
        return 0

    def get_indexed_channels(self) -> set[str]:
        """Get set of all indexed channel IDs."""
        results = self.collection.get(include=["metadatas"])
        channels = set()
        if results["metadatas"]:
            for meta in results["metadatas"]:
                if meta and "channel_id" in meta:
                    channels.add(meta["channel_id"])
        return channels

    def get_latest_timestamp(self, channel_id: str) -> Optional[datetime]:
        """
        Get the latest indexed message timestamp for a channel.

        Useful for incremental sync to only fetch newer messages.

        Args:
            channel_id: Channel ID to check

        Returns:
            Latest message timestamp or None if no messages indexed
        """
        results = self.collection.get(
            where={"channel_id": channel_id},
            include=["metadatas"]
        )

        if not results["metadatas"]:
            return None

        latest = None
        for meta in results["metadatas"]:
            if meta and "timestamp" in meta:
                try:
                    ts = datetime.fromisoformat(meta["timestamp"])
                    if latest is None or ts > latest:
                        latest = ts
                except (ValueError, TypeError):
                    continue

        return latest


# Singleton instance
_slack_indexer: Optional[SlackIndexer] = None


def get_slack_indexer() -> SlackIndexer:
    """Get or create SlackIndexer singleton."""
    global _slack_indexer
    if _slack_indexer is None:
        _slack_indexer = SlackIndexer()
    return _slack_indexer
