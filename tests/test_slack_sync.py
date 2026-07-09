"""Tests for SlackSync orchestrator — the daily-vs-full-sync entry points."""
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def sync_with_mocks(monkeypatch):
    """A SlackSync wired up with mock client/indexer/entity_store/interaction_store.

    Patches the heavy collaborators so we can assert on call ordering and
    return shape without touching the network or any SQLite store. The
    rate-limit pacing sleeps (inter-channel and inter-thread — up to 1.2s
    per thread on backfills) are zeroed out so tests stay fast.
    """
    from api.services.slack_sync import SlackSync

    monkeypatch.setattr("api.services.slack_sync.time.sleep", lambda _s: None)

    mock_client = MagicMock()
    mock_indexer = MagicMock()
    mock_entity_store = MagicMock()
    mock_interaction_store = MagicMock()

    sync = SlackSync(
        client=mock_client,
        indexer=mock_indexer,
        entity_store=mock_entity_store,
        interaction_store=mock_interaction_store,
    )

    # Stub out the things we don't want exercised in unit tests.
    sync._store_message_counts = MagicMock()
    return sync


class TestIncrementalSyncCallsSyncUsers:
    """``incremental_sync`` must refresh the user list before each nightly run.

    Issue #224: prior behaviour only called ``sync_messages``, so new Slack
    users added to the workspace between manual ``full_sync`` runs never
    landed in ``source_entities`` — silent drift for 83 days in the
    incident that motivated this fix.
    """

    def test_sync_users_invoked_before_messages(self, sync_with_mocks):
        sync = sync_with_mocks
        sync.sync_users = MagicMock(return_value={
            "total": 10, "created": 2, "updated": 8,
            "skipped_bots": 0, "skipped_deleted": 0,
        })
        sync.sync_messages = MagicMock(return_value={
            "channels_synced": 1, "messages_indexed": 5,
            "interactions_created": 5, "errors": [],
            "message_counts": {}, "affected_person_ids": set(),
        })

        sync.incremental_sync(create_interactions=True)

        sync.sync_users.assert_called_once()
        sync.sync_messages.assert_called_once()

    def test_return_shape_matches_full_sync(self, sync_with_mocks):
        """Callers (scripts/sync_slack.py) read ``results["users"]`` and
        ``results["messages"]`` — the wrapper that ran for ``full_sync`` but
        not ``incremental_sync`` is the whole reason the SYNC_STATS line
        reported zeros for nightly slack runs."""
        sync = sync_with_mocks
        users_payload = {"total": 10, "created": 2, "updated": 8}
        messages_payload = {
            "channels_synced": 1, "messages_indexed": 5,
            "interactions_created": 5, "errors": [],
            "message_counts": {}, "affected_person_ids": set(),
        }
        sync.sync_users = MagicMock(return_value=users_payload)
        sync.sync_messages = MagicMock(return_value=messages_payload)

        result = sync.incremental_sync()

        assert result["users"] == users_payload
        assert result["messages"] == messages_payload
        assert "status" in result
        assert "errors" in result

    def test_user_sync_failure_does_not_abort_message_sync(self, sync_with_mocks):
        """A flaky ``users.list`` API call shouldn't take down the whole
        nightly. Match the ``full_sync`` resilience pattern."""
        sync = sync_with_mocks
        sync.sync_users = MagicMock(side_effect=RuntimeError("rate limited"))
        sync.sync_messages = MagicMock(return_value={
            "channels_synced": 1, "messages_indexed": 5,
            "interactions_created": 5, "errors": [],
            "message_counts": {}, "affected_person_ids": set(),
        })

        result = sync.incremental_sync()

        sync.sync_messages.assert_called_once()
        assert result["status"] == "partial"
        assert any("rate limited" in e or "user sync" in e.lower()
                   for e in result["errors"])
        assert result["users"] == {}

    def test_new_workspace_user_persists_to_source_entities(self, tmp_path):
        """End-to-end: a user added between full_syncs lands in source_entities on
        the next nightly run. This is issue #224 AC #2 — the regression the PR
        actually exists to fix. Uses a real ``SourceEntityStore`` against a
        tmp_path so we exercise the persistence layer, not just mocks.
        """
        from api.services.slack_integration import SlackUser
        from api.services.slack_sync import SlackSync
        from api.services.source_entity import SourceEntityStore

        # Real store, isolated from the project DB.
        entity_store = SourceEntityStore(db_path=str(tmp_path / "crm.db"))

        # Mock SlackClient returns one workspace user.
        new_user = SlackUser(
            user_id="U_NEW_HUMAN",
            username="alice",
            real_name="Alice Wonderland",
            display_name="alice",
            email="alice@example.com",
            is_bot=False,
            is_deleted=False,
            team_id="T_WORKSPACE",
        )
        mock_client = MagicMock()
        mock_client.list_users.return_value = [new_user]

        # sync_messages is irrelevant for this test — short-circuit it.
        mock_indexer = MagicMock()
        sync = SlackSync(
            client=mock_client,
            indexer=mock_indexer,
            entity_store=entity_store,
            interaction_store=MagicMock(),
        )
        sync.sync_messages = MagicMock(return_value={
            "channels_synced": 0, "messages_indexed": 0,
            "interactions_created": 0, "errors": [],
            "message_counts": {}, "affected_person_ids": set(),
        })
        sync._store_message_counts = MagicMock()

        # Precondition: store starts empty.
        assert entity_store.get_by_source("slack", "default:U_NEW_HUMAN") is None

        result = sync.incremental_sync(create_interactions=False)

        # Postcondition: the user is now in source_entities.
        persisted = entity_store.get_by_source("slack", "default:U_NEW_HUMAN")
        assert persisted is not None, \
            "incremental_sync should have persisted the new workspace user as a source_entity"
        assert persisted.observed_email == "alice@example.com"
        assert result["users"]["created"] == 1
        assert result["status"] == "success"

    def test_message_counts_still_persisted_after_shape_change(self, sync_with_mocks):
        """Internal ``_store_message_counts`` reaches into the nested
        ``results["messages"]["message_counts"]`` now — make sure the
        refactor didn't break that path."""
        sync = sync_with_mocks
        sync.sync_users = MagicMock(return_value={"total": 0, "created": 0, "updated": 0})
        sync.sync_messages = MagicMock(return_value={
            "channels_synced": 1, "messages_indexed": 1,
            "interactions_created": 1, "errors": [],
            "message_counts": {"alice": 3},
            "affected_person_ids": set(),
        })

        sync.incremental_sync()

        sync._store_message_counts.assert_called_once()
        args, kwargs = sync._store_message_counts.call_args
        # Either positional or keyword — accept both shapes.
        passed_counts = args[0] if args else kwargs.get("message_counts")
        assert passed_counts == {"alice": 3}


class TestChannelIndexing:
    """Issue #439: nightly entry points must index channel messages, not just DMs.

    The index historically contained zero public/private channel messages
    because full_sync/incremental_sync hardcoded ``dm_only=True``.
    """

    def _messages_payload(self):
        return {
            "channels_processed": 1, "messages_indexed": 5,
            "interactions_created": 0, "errors": [],
            "message_counts": {}, "affected_person_ids": set(),
        }

    def test_full_sync_includes_channels(self, sync_with_mocks):
        sync = sync_with_mocks
        sync.sync_users = MagicMock(return_value={"total": 0, "created": 0, "updated": 0})
        sync.sync_messages = MagicMock(return_value=self._messages_payload())

        sync.full_sync()

        kwargs = sync.sync_messages.call_args.kwargs
        assert kwargs["dm_only"] is False
        assert kwargs["linked_only"] is True  # DM filtering semantics unchanged

    def test_incremental_sync_includes_channels(self, sync_with_mocks):
        sync = sync_with_mocks
        sync.sync_users = MagicMock(return_value={"total": 0, "created": 0, "updated": 0})
        sync.sync_messages = MagicMock(return_value=self._messages_payload())

        sync.incremental_sync()

        kwargs = sync.sync_messages.call_args.kwargs
        assert kwargs["dm_only"] is False
        assert kwargs["linked_only"] is True

    def test_sync_messages_enumerates_member_channels_only(self, sync_with_mocks):
        """Channel enumeration must use membership (users.conversations), not
        the full workspace list — Nathan can only have messages where he's a
        member, and the full list burns rate limit (a catch-up sync 429'd on
        page 3 of conversations.list)."""
        sync = sync_with_mocks
        sync.client.list_channels.return_value = []

        sync.sync_messages(dm_only=False)

        kwargs = sync.client.list_channels.call_args.kwargs
        assert kwargs.get("member_only") is True

    def test_channel_messages_indexed_without_interactions(self, sync_with_mocks):
        """A public channel gets indexed to ChromaDB but never creates CRM
        interactions — channel chatter isn't a 1:1 interaction."""
        from datetime import datetime, timezone
        from api.services.slack_integration import SlackChannel, SlackMessage

        sync = sync_with_mocks
        channel = SlackChannel(channel_id="C123", name="general")
        message = SlackMessage(
            ts="1700000000.000100", channel_id="C123", user_id="U1",
            text="hello", timestamp=datetime.now(timezone.utc),
        )
        sync.client.list_channels.return_value = [channel]
        sync.client.get_all_channel_history.return_value = [message]
        sync.indexer.get_latest_timestamp.return_value = None
        sync.indexer.index_messages.return_value = 1
        sync._create_interactions_for_channel = MagicMock()

        stats = sync.sync_messages(dm_only=False, create_interactions=True)

        assert stats["messages_indexed"] == 1
        assert stats["channels_processed"] == 1
        sync._create_interactions_for_channel.assert_not_called()

    def test_channel_history_limited_to_window(self, sync_with_mocks):
        """First sync of a channel is bounded by the 90-day window (oldest
        passed to the API), unlike DMs which pull full history."""
        from datetime import datetime, timedelta, timezone
        from api.services.slack_integration import SlackChannel

        sync = sync_with_mocks
        channel = SlackChannel(channel_id="C123", name="general")
        sync.client.list_channels.return_value = [channel]
        sync.client.get_all_channel_history.return_value = []
        sync.indexer.get_latest_timestamp.return_value = None

        sync.sync_messages(dm_only=False, channel_history_days=90)

        kwargs = sync.client.get_all_channel_history.call_args.kwargs
        oldest = kwargs["oldest"]
        expected = datetime.now(timezone.utc) - timedelta(days=90)
        assert abs((oldest - expected).total_seconds()) < 60

    def test_linked_only_still_filters_dms_but_not_channels(self, sync_with_mocks):
        """linked_only applies to 1:1 DMs only; channels are indexed
        regardless of CRM linkage."""
        from api.services.slack_integration import SlackChannel

        sync = sync_with_mocks
        unlinked_dm = SlackChannel(channel_id="D1", name="U_UNLINKED", is_im=True)
        channel = SlackChannel(channel_id="C1", name="general")
        sync.client.list_channels.return_value = [unlinked_dm, channel]
        sync.client.get_all_channel_history.return_value = []
        sync.indexer.get_latest_timestamp.return_value = None
        sync._get_linked_slack_user_ids = MagicMock(return_value=set())

        stats = sync.sync_messages(dm_only=False, linked_only=True)

        assert stats["channels_skipped"] == 1  # the unlinked DM
        assert stats["channels_processed"] == 1  # the public channel


class TestThreadReplySync:
    """Issue #440: thread replies must be fetched via conversations.replies and
    indexed like normal messages — conversations.history only returns
    top-level messages, so replies were invisible to the index."""

    def _parent(self, ts="1700000000.000100", reply_count=2, latest_reply=None):
        from datetime import datetime, timezone
        from api.services.slack_integration import SlackMessage

        return SlackMessage(
            ts=ts, channel_id="C123", user_id="U1", text="parent",
            timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            thread_ts=ts if reply_count else None,
            reply_count=reply_count, latest_reply=latest_reply,
        )

    def _reply(self, ts="1700000100.000200"):
        from datetime import datetime, timezone
        from api.services.slack_integration import SlackMessage

        return SlackMessage(
            ts=ts, channel_id="C123", user_id="U2", text="a reply",
            timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            thread_ts="1700000000.000100",
        )

    def _channel(self):
        from api.services.slack_integration import SlackChannel
        return SlackChannel(channel_id="C123", name="general")

    def test_full_sync_fetches_replies_for_threaded_parents(self, sync_with_mocks):
        sync = sync_with_mocks
        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.return_value = [self._parent(reply_count=2)]
        sync.client.get_thread_replies.return_value = [self._reply()]
        sync.indexer.index_messages.return_value = 1

        stats = sync.sync_messages(full=True, dm_only=False)

        kwargs = sync.client.get_thread_replies.call_args.kwargs
        assert kwargs["thread_ts"] == "1700000000.000100"
        assert kwargs["oldest"] is None  # full sync pulls entire threads
        # main history + replies both indexed
        assert sync.indexer.index_messages.call_count == 2
        assert stats["messages_indexed"] == 2

    def test_no_thread_fetch_without_replies(self, sync_with_mocks):
        sync = sync_with_mocks
        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.return_value = [self._parent(reply_count=0)]
        sync.indexer.index_messages.return_value = 1

        sync.sync_messages(full=True, dm_only=False)

        sync.client.get_thread_replies.assert_not_called()

    def test_incremental_catches_reply_to_old_parent(self, sync_with_mocks):
        """A reply posted today to a week-old parent: the parent doesn't appear
        in the cursor-windowed history fetch, so a rescan of recent parents
        must find it via latest_reply."""
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        now = datetime.now(timezone.utc)
        cursor = now - timedelta(days=1)
        old_parent_ts = f"{(now - timedelta(days=5)).timestamp():.6f}"
        new_reply_ts = f"{(now - timedelta(hours=2)).timestamp():.6f}"

        old_parent = self._parent(
            ts=old_parent_ts, reply_count=3, latest_reply=new_reply_ts)

        sync.client.list_channels.return_value = [self._channel()]
        # First call: main incremental fetch (empty — no new top-level messages).
        # Second call: thread rescan window returns the old parent.
        sync.client.get_all_channel_history.side_effect = [[], [old_parent]]
        sync.client.get_thread_replies.return_value = [self._reply(ts=new_reply_ts)]
        sync.indexer.get_latest_timestamp.return_value = cursor
        sync.indexer.index_messages.return_value = 1

        stats = sync.sync_messages(full=False, dm_only=False)

        kwargs = sync.client.get_thread_replies.call_args.kwargs
        assert kwargs["thread_ts"] == old_parent_ts
        # only new replies are fetched
        expected_oldest = cursor - timedelta(seconds=1)
        assert abs((kwargs["oldest"] - expected_oldest).total_seconds()) < 2
        assert stats["messages_indexed"] == 1

    def test_incremental_skips_threads_without_new_replies(self, sync_with_mocks):
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        now = datetime.now(timezone.utc)
        cursor = now - timedelta(days=1)
        stale_parent = self._parent(
            ts=f"{(now - timedelta(days=5)).timestamp():.6f}",
            reply_count=3,
            latest_reply=f"{(now - timedelta(days=3)).timestamp():.6f}",  # before cursor
        )

        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.side_effect = [[], [stale_parent]]
        sync.indexer.get_latest_timestamp.return_value = cursor

        sync.sync_messages(full=False, dm_only=False)

        sync.client.get_thread_replies.assert_not_called()

    def test_parent_in_both_fetch_and_rescan_fetched_once(self, sync_with_mocks):
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        now = datetime.now(timezone.utc)
        cursor = now - timedelta(days=1)
        new_ts = f"{(now - timedelta(hours=3)).timestamp():.6f}"
        parent = self._parent(ts=new_ts, reply_count=1,
                              latest_reply=f"{(now - timedelta(hours=1)).timestamp():.6f}")

        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.side_effect = [[parent], [parent]]
        sync.client.get_thread_replies.return_value = [self._reply()]
        sync.indexer.get_latest_timestamp.return_value = cursor
        sync.indexer.index_messages.return_value = 1

        sync.sync_messages(full=False, dm_only=False)

        assert sync.client.get_thread_replies.call_count == 1

    def test_thread_fetch_failure_does_not_kill_channel(self, sync_with_mocks):
        """A failed thread fetch must not fail the channel, but it must be
        visible: the same run's main fetch advances the cursor, so a silently
        skipped thread is filtered out of every future incremental run —
        silent success would be permanent silent loss."""
        sync = sync_with_mocks
        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.return_value = [self._parent(reply_count=2)]
        sync.client.get_thread_replies.side_effect = RuntimeError("ratelimited")
        sync.indexer.index_messages.return_value = 1

        stats = sync.sync_messages(full=True, dm_only=False)

        # main history still indexed; channel not recorded as a hard failure
        assert stats["messages_indexed"] == 1
        assert stats["channels_processed"] == 1
        # ... but the loss is surfaced: run reports partial with an error
        assert stats["status"] == "partial"
        assert any("thread" in e.lower() and "ratelimited" in e
                   for e in stats["errors"])

    def test_rescan_failure_surfaces_in_stats(self, sync_with_mocks):
        """A failed 7-day rescan fetch has the same permanent-loss property
        as a failed thread fetch — it must reach stats["errors"]."""
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        cursor = datetime.now(timezone.utc) - timedelta(days=1)
        sync.client.list_channels.return_value = [self._channel()]
        # First call: main incremental fetch. Second call: rescan blows up.
        sync.client.get_all_channel_history.side_effect = [
            [], RuntimeError("ratelimited")]
        sync.indexer.get_latest_timestamp.return_value = cursor

        stats = sync.sync_messages(full=False, dm_only=False)

        assert stats["channels_processed"] == 1
        assert stats["status"] == "partial"
        assert any("rescan" in e.lower() for e in stats["errors"])

    def test_reply_after_sync_start_deferred(self, sync_with_mocks):
        """A reply posted during the sync run (ts after sync-start) must not
        be indexed this run: indexing it would advance the channel cursor
        past top-level messages posted mid-run that the main fetch never saw,
        permanently skipping them. The deferred reply is recovered next run
        (its parent's latest_reply stays above the cursor)."""
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        now = datetime.now(timezone.utc)
        past_reply = self._reply(ts=f"{(now - timedelta(minutes=5)).timestamp():.6f}")
        future_reply = self._reply(ts=f"{(now + timedelta(minutes=5)).timestamp():.6f}")

        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.return_value = [self._parent(reply_count=2)]
        sync.client.get_thread_replies.return_value = [past_reply, future_reply]
        sync.indexer.index_messages.side_effect = lambda messages, **kw: len(messages)

        stats = sync.sync_messages(full=True, dm_only=False)

        # 1 parent from main history + only the pre-sync-start reply
        assert stats["messages_indexed"] == 2
        reply_call_kwargs = sync.indexer.index_messages.call_args_list[-1].kwargs
        assert reply_call_kwargs["messages"] == [past_reply]

    def test_rescan_history_window_is_rescan_days(self, sync_with_mocks):
        """The rescan's history call must be bounded to THREAD_RESCAN_DAYS —
        not the channel cursor, and not unbounded."""
        from datetime import datetime, timedelta, timezone
        from api.services.slack_sync import THREAD_RESCAN_DAYS

        sync = sync_with_mocks
        cursor = datetime.now(timezone.utc) - timedelta(days=1)
        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.side_effect = [[], []]
        sync.indexer.get_latest_timestamp.return_value = cursor

        sync.sync_messages(full=False, dm_only=False)

        assert sync.client.get_all_channel_history.call_count == 2
        rescan_kwargs = sync.client.get_all_channel_history.call_args_list[1].kwargs
        expected = datetime.now(timezone.utc) - timedelta(days=THREAD_RESCAN_DAYS)
        assert abs((rescan_kwargs["oldest"] - expected).total_seconds()) < 60

    def test_rescan_skipped_when_cursor_covers_window(self, sync_with_mocks):
        """No second history call when the main fetch's window already covers
        the rescan window: first incremental sync of a channel (no cursor,
        90-day history_days window) and a cursor older than the window."""
        from datetime import datetime, timedelta, timezone

        sync = sync_with_mocks
        sync.client.list_channels.return_value = [self._channel()]
        sync.client.get_all_channel_history.return_value = []

        # Case 1: first incremental sync — no cursor, history_days window.
        sync.indexer.get_latest_timestamp.return_value = None
        sync.sync_messages(full=False, dm_only=False, channel_history_days=90)
        assert sync.client.get_all_channel_history.call_count == 1

        # Case 2: cursor older than the rescan window start.
        sync.client.get_all_channel_history.reset_mock()
        sync.indexer.get_latest_timestamp.return_value = (
            datetime.now(timezone.utc) - timedelta(days=30))
        sync.sync_messages(full=False, dm_only=False)
        assert sync.client.get_all_channel_history.call_count == 1
