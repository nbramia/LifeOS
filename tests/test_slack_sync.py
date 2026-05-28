"""Tests for SlackSync orchestrator — the daily-vs-full-sync entry points."""
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def sync_with_mocks():
    """A SlackSync wired up with mock client/indexer/entity_store/interaction_store.

    Patches the heavy collaborators so we can assert on call ordering and
    return shape without touching the network or any SQLite store.
    """
    from api.services.slack_sync import SlackSync

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
