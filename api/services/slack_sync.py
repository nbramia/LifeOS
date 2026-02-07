"""
Slack Sync Orchestration for LifeOS.

Coordinates syncing Slack data across:
- Vector store (ChromaDB) for semantic search
- SourceEntity for user identity resolution
- Interaction records for CRM timeline

Supports full and incremental sync modes.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.services.slack_integration import (
    SlackClient,
    SlackChannel,
    SlackMessage,
    get_slack_client,
    get_workspace_id,
    is_slack_enabled,
    sync_slack_users,
    SLACK_TEAM_ID,
)
from api.services.slack_indexer import SlackIndexer, get_slack_indexer
from api.services.source_entity import SourceEntityStore, get_source_entity_store
from api.services.interaction_store import Interaction, InteractionStore

logger = logging.getLogger(__name__)

# Default sync configuration
DEFAULT_DM_HISTORY_DAYS = None  # Full history for DMs
DEFAULT_CHANNEL_HISTORY_DAYS = 90  # 90 days for channels


class SlackSync:
    """
    Orchestrates Slack data sync to LifeOS.

    Handles:
    - User sync to SourceEntity
    - Message indexing to ChromaDB
    - Interaction creation for CRM
    """

    def __init__(
        self,
        client: Optional[SlackClient] = None,
        indexer: Optional[SlackIndexer] = None,
        entity_store: Optional[SourceEntityStore] = None,
        interaction_store: Optional[InteractionStore] = None,
    ):
        """
        Initialize sync orchestrator.

        Args:
            client: Slack API client (uses singleton if not provided)
            indexer: Slack message indexer (uses singleton if not provided)
            entity_store: Source entity store (uses singleton if not provided)
            interaction_store: Interaction store (creates new if not provided)
        """
        self._client = client
        self._indexer = indexer
        self._entity_store = entity_store
        self._interaction_store = interaction_store
        self._workspace_id = get_workspace_id()

    @property
    def client(self) -> SlackClient:
        """Get Slack client."""
        if self._client is None:
            self._client = get_slack_client()
        return self._client

    @property
    def indexer(self) -> SlackIndexer:
        """Get Slack indexer."""
        if self._indexer is None:
            self._indexer = get_slack_indexer()
        return self._indexer

    @property
    def entity_store(self) -> SourceEntityStore:
        """Get source entity store."""
        if self._entity_store is None:
            self._entity_store = get_source_entity_store()
        return self._entity_store

    @property
    def interaction_store(self) -> InteractionStore:
        """Get interaction store."""
        if self._interaction_store is None:
            self._interaction_store = InteractionStore()
        return self._interaction_store

    def sync_users(self) -> dict:
        """
        Sync Slack users to SourceEntity store.

        Returns:
            Dict with sync statistics (total, created, updated, skipped_bots, skipped_deleted)
        """
        logger.info("Starting Slack user sync")
        stats = sync_slack_users(
            client=self.client,
            entity_store=self.entity_store,
            workspace_id=self._workspace_id,
        )
        logger.info(f"Slack user sync complete: {stats}")
        return stats

    def _get_linked_slack_user_ids(self) -> set[str]:
        """
        Get set of Slack user IDs that have canonical_person_id linked.

        Returns:
            Set of Slack user IDs (without workspace prefix)
        """
        linked_ids = set()
        # Query source entities with canonical_person_id set
        conn = self.entity_store._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT source_id FROM source_entities
                WHERE source_type = 'slack'
                AND canonical_person_id IS NOT NULL
                AND link_status != 'rejected'
                """
            )
            for row in cursor.fetchall():
                source_id = row[0]
                # source_id format is "workspace_id:user_id"
                if ":" in source_id:
                    user_id = source_id.split(":", 1)[1]
                    linked_ids.add(user_id)
        finally:
            conn.close()
        return linked_ids

    def sync_messages(
        self,
        full: bool = False,
        dm_only: bool = True,
        channel_history_days: int = DEFAULT_CHANNEL_HISTORY_DAYS,
        create_interactions: bool = True,
        linked_only: bool = False,
    ) -> dict:
        """
        Sync Slack messages to ChromaDB and optionally create interactions.

        Args:
            full: If True, sync all history; if False, only sync since last indexed
            dm_only: If True, only sync DMs; if False, also sync channels
            channel_history_days: Days of channel history to sync (for non-DM channels)
            create_interactions: If True, create CRM Interaction records
            linked_only: If True, only sync DMs for users linked to CRM people

        Returns:
            Dict with sync statistics
        """
        logger.info(f"Starting Slack message sync (full={full}, dm_only={dm_only}, linked_only={linked_only})")
        start_time = time.time()

        stats = {
            "channels_processed": 0,
            "channels_skipped": 0,
            "messages_indexed": 0,
            "interactions_created": 0,
            "affected_person_ids": set(),
            "message_counts": {},  # {person_id: actual message count}
            "errors": [],
        }

        # Get linked user IDs if filtering
        linked_user_ids = None
        if linked_only:
            linked_user_ids = self._get_linked_slack_user_ids()
            logger.info(f"Found {len(linked_user_ids)} linked Slack users")

        try:
            # Get list of accessible channels
            channels = self.client.list_channels(self._workspace_id)
            logger.info(f"Found {len(channels)} accessible channels")

            for channel in channels:
                # Skip non-DM channels if dm_only is True
                if dm_only and not (channel.is_im or channel.is_mpim):
                    continue

                # Skip unlinked DM users if linked_only is True
                if linked_only and channel.is_im:
                    if channel.name not in linked_user_ids:
                        stats["channels_skipped"] += 1
                        continue

                try:
                    channel_stats = self._sync_channel(
                        channel=channel,
                        full=full,
                        history_days=channel_history_days if not channel.is_im else None,
                        create_interactions=create_interactions,
                    )

                    stats["channels_processed"] += 1
                    stats["messages_indexed"] += channel_stats["messages_indexed"]
                    stats["interactions_created"] += channel_stats["interactions_created"]
                    stats["affected_person_ids"].update(channel_stats.get("affected_person_ids", set()))
                    # Aggregate message counts from each channel
                    for person_id, count in channel_stats.get("message_counts", {}).items():
                        stats["message_counts"][person_id] = stats["message_counts"].get(person_id, 0) + count

                except Exception as e:
                    error_msg = f"Error syncing channel {channel.channel_id}: {e}"
                    logger.error(error_msg)
                    stats["errors"].append(error_msg)

                # Rate limit: small delay between channels
                time.sleep(0.5)

        except Exception as e:
            error_msg = f"Error listing channels: {e}"
            logger.error(error_msg)
            stats["errors"].append(error_msg)

        elapsed = time.time() - start_time
        stats["elapsed_seconds"] = round(elapsed, 1)
        stats["status"] = "success" if not stats["errors"] else "partial"

        logger.info(
            f"Slack message sync complete: {stats['messages_indexed']} messages "
            f"from {stats['channels_processed']} channels in {elapsed:.1f}s"
        )

        return stats

    def _sync_channel(
        self,
        channel: SlackChannel,
        full: bool = False,
        history_days: Optional[int] = None,
        create_interactions: bool = True,
    ) -> dict:
        """
        Sync a single channel's messages.

        Args:
            channel: SlackChannel to sync
            full: If True, sync all history
            history_days: Days of history to sync (None = all available)
            create_interactions: If True, create CRM Interaction records

        Returns:
            Dict with channel sync statistics
        """
        stats = {
            "messages_indexed": 0,
            "interactions_created": 0,
            "affected_person_ids": set(),
        }

        # Determine oldest timestamp for fetch
        oldest = None
        if not full:
            # Incremental sync: get latest indexed timestamp
            oldest = self.indexer.get_latest_timestamp(channel.channel_id)
            if oldest:
                # Add small buffer to avoid missing messages
                oldest = oldest - timedelta(seconds=1)
                logger.debug(f"Incremental sync for {channel.channel_id} from {oldest}")

        if oldest is None and history_days:
            # Limit history to specified days
            oldest = datetime.now(timezone.utc) - timedelta(days=history_days)

        # Fetch messages
        messages = self.client.get_all_channel_history(
            channel_id=channel.channel_id,
            workspace_id=self._workspace_id,
            oldest=oldest,
        )

        if not messages:
            return stats

        logger.debug(f"Fetched {len(messages)} messages from {channel.channel_id}")

        # Index messages
        indexed = self.indexer.index_messages(
            messages=messages,
            channel=channel,
            workspace_id=self._workspace_id,
        )
        stats["messages_indexed"] = indexed

        # Create interactions for DMs
        if create_interactions and (channel.is_im or channel.is_mpim):
            interactions_created, affected_ids, channel_msg_counts = self._create_interactions_for_channel(
                channel=channel,
                messages=messages,
            )
            stats["interactions_created"] = interactions_created
            stats["affected_person_ids"].update(affected_ids)
            # Merge message counts into stats
            if "message_counts" not in stats:
                stats["message_counts"] = {}
            for person_id, count in channel_msg_counts.items():
                stats["message_counts"][person_id] = stats["message_counts"].get(person_id, 0) + count

        return stats

    def _create_interactions_for_channel(
        self,
        channel: SlackChannel,
        messages: list[SlackMessage],
    ) -> tuple[int, set[str], dict[str, int]]:
        """
        Create Interaction records for DM messages.

        Groups messages by conversation partner and creates one interaction
        per day of conversation.

        Args:
            channel: SlackChannel (should be DM)
            messages: List of messages from the channel

        Returns:
            Tuple of (interactions created, affected person IDs, {person_id: message_count})
        """
        affected_person_ids: set[str] = set()
        message_counts: dict[str, int] = {}

        if not messages:
            return 0, affected_person_ids, message_counts

        created = 0

        # For DMs, resolve the other person
        dm_partner_id = channel.name if channel.is_im else None
        if not dm_partner_id:
            return 0, affected_person_ids, message_counts

        # Try to resolve person_id from SourceEntity
        person_id = self._resolve_person_id(dm_partner_id)
        if not person_id:
            # Skip if we can't resolve the person
            logger.debug(f"Could not resolve person for Slack user {dm_partner_id}")
            return 0, affected_person_ids, message_counts

        # Track actual message count for this person
        message_counts[person_id] = len(messages)

        # Get the other person's name for display
        partner = self.client.get_user_cached(dm_partner_id, self._workspace_id)
        partner_name = (
            partner.real_name or partner.display_name or partner.username
            if partner
            else dm_partner_id
        )

        # Group messages by date for daily interactions
        messages_by_date: dict[str, list[SlackMessage]] = {}
        for msg in messages:
            date_key = msg.timestamp.strftime("%Y-%m-%d")
            if date_key not in messages_by_date:
                messages_by_date[date_key] = []
            messages_by_date[date_key].append(msg)

        # Create one interaction per date
        for date_key, day_messages in messages_by_date.items():
            # Use earliest message timestamp for the interaction
            earliest = min(day_messages, key=lambda m: m.timestamp)
            latest = max(day_messages, key=lambda m: m.timestamp)

            # Build snippet from first message with content
            snippet = None
            for msg in sorted(day_messages, key=lambda m: m.timestamp):
                if msg.text and msg.text.strip():
                    snippet = msg.text[:200]
                    if len(msg.text) > 200:
                        snippet += "..."
                    break

            # Create source_id for deduplication
            source_id = f"{channel.channel_id}:{date_key}"

            # Build Slack deep link
            team_id = SLACK_TEAM_ID or self._workspace_id
            source_link = f"slack://channel?team={team_id}&id={channel.channel_id}"

            interaction = Interaction(
                id=str(uuid.uuid4()),
                person_id=person_id,
                timestamp=earliest.timestamp,
                source_type="slack",
                title=f"Slack DM with {partner_name}",
                snippet=snippet,
                source_link=source_link,
                source_id=source_id,
            )

            # Add if not already exists
            _, was_added = self.interaction_store.add_if_not_exists(interaction)
            if was_added:
                created += 1
                affected_person_ids.add(person_id)

        return created, affected_person_ids, message_counts

    def _resolve_person_id(self, slack_user_id: str) -> Optional[str]:
        """
        Resolve Slack user ID to PersonEntity ID.

        Uses SourceEntity → PersonEntity link.

        Args:
            slack_user_id: Slack user ID

        Returns:
            PersonEntity ID or None if not resolved
        """
        source_id = f"{self._workspace_id}:{slack_user_id}"
        source_entity = self.entity_store.get_by_source("slack", source_id)

        if source_entity and source_entity.canonical_person_id:
            return source_entity.canonical_person_id

        return None

    def _store_message_counts(self, message_counts: dict[str, int], full_sync: bool = False) -> None:
        """
        Store actual Slack message counts on PersonEntity records.

        Args:
            message_counts: Dict mapping person_id to message count
            full_sync: If True, replace counts (full sync fetched all messages).
                       If False, accumulate counts (incremental sync adds new messages).
        """
        if not message_counts:
            return

        from api.services.person_entity import get_person_entity_store
        person_store = get_person_entity_store()

        updated = 0
        for person_id, count in message_counts.items():
            entity = person_store.get_by_id(person_id)
            if entity:
                if full_sync:
                    entity.slack_message_count = count  # Replace
                else:
                    entity.slack_message_count += count  # Accumulate
                person_store.update(entity)
                updated += 1

        if updated > 0:
            person_store.save()
            logger.info(f"Updated slack_message_count for {updated} people (full_sync={full_sync})")

    def full_sync(self, create_interactions: bool = True) -> dict:
        """
        Perform a full sync of all Slack data.

        This includes:
        1. Sync all users to SourceEntity
        2. Index all DM history to ChromaDB
        3. Create Interaction records

        Args:
            create_interactions: If True, create CRM Interaction records

        Returns:
            Combined sync statistics
        """
        logger.info("Starting full Slack sync")
        start_time = time.time()

        results = {
            "users": {},
            "messages": {},
            "status": "success",
            "errors": [],
        }

        # Step 1: Sync users first (needed for entity resolution)
        try:
            results["users"] = self.sync_users()
        except Exception as e:
            error_msg = f"User sync failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # Step 2: Sync messages (only for linked users to speed up full sync)
        try:
            results["messages"] = self.sync_messages(
                full=True,
                dm_only=True,  # DMs only
                create_interactions=create_interactions,
                linked_only=True,  # Only sync DMs for users linked to CRM people
            )
            if results["messages"].get("errors"):
                results["errors"].extend(results["messages"]["errors"])
        except Exception as e:
            error_msg = f"Message sync failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # Set final status
        if results["errors"]:
            results["status"] = "partial"

        elapsed = time.time() - start_time
        results["elapsed_seconds"] = round(elapsed, 1)

        # Store actual Slack message counts on PersonEntity (full sync = replace)
        message_counts = results.get("messages", {}).get("message_counts", {})
        if message_counts:
            self._store_message_counts(message_counts, full_sync=True)

        # Refresh PersonEntity stats for all affected people
        affected_ids = results.get("messages", {}).get("affected_person_ids", set())
        if affected_ids:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(affected_ids)} affected people...")
            refresh_person_stats(list(affected_ids))

        logger.info(f"Full Slack sync complete in {elapsed:.1f}s")
        return results

    def incremental_sync(self, create_interactions: bool = True) -> dict:
        """
        Perform an incremental sync of new Slack data.

        Only fetches messages newer than the last indexed timestamp.

        Args:
            create_interactions: If True, create CRM Interaction records

        Returns:
            Sync statistics
        """
        logger.info("Starting incremental Slack sync")

        results = self.sync_messages(
            full=False,
            dm_only=True,
            create_interactions=create_interactions,
        )

        # Store actual Slack message counts on PersonEntity (incremental = accumulate)
        message_counts = results.get("message_counts", {})
        if message_counts:
            self._store_message_counts(message_counts, full_sync=False)

        # Refresh PersonEntity stats for all affected people
        affected_ids = results.get("affected_person_ids", set())
        if affected_ids:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(affected_ids)} affected people...")
            refresh_person_stats(list(affected_ids))

        return results


# Singleton instance
_slack_sync: Optional[SlackSync] = None


def get_slack_sync() -> SlackSync:
    """Get or create SlackSync singleton."""
    global _slack_sync
    if _slack_sync is None:
        _slack_sync = SlackSync()
    return _slack_sync


def run_slack_sync(full: bool = False) -> dict:
    """
    Convenience function to run Slack sync.

    Args:
        full: If True, run full sync; if False, run incremental

    Returns:
        Sync statistics
    """
    if not is_slack_enabled():
        logger.warning("Slack integration not enabled")
        return {"status": "skipped", "reason": "Slack not enabled"}

    sync = get_slack_sync()

    if full:
        return sync.full_sync()
    else:
        return sync.incremental_sync()
