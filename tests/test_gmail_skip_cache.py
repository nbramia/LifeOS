"""
Tests for the Gmail skip cache.

The cache exists because marketing mail is discarded without producing an
interaction row, so the sync's "already seen" set could never remember it and
re-fetched ~13k messages every night (#552).
"""
import pytest
import sqlite3
from datetime import datetime, timedelta, timezone

pytestmark = pytest.mark.unit

from api.services.gmail_skip_cache import GmailSkipCache  # noqa: E402


@pytest.fixture
def cache(tmp_path):
    return GmailSkipCache(db_path=str(tmp_path / "skip.db"))


class TestRecordAndRead:
    def test_records_and_returns_ids(self, cache):
        cache.record_skipped("personal", ["msg1", "msg2"])

        assert cache.get_skipped_ids("personal") == {"msg1", "msg2"}

    def test_accounts_are_isolated(self, cache):
        """
        Gmail ids are only unique within a mailbox, and one sync process handles
        several accounts — a personal skip must not suppress a work message.
        """
        cache.record_skipped("personal", ["shared_id"])

        assert cache.get_skipped_ids("work") == set()
        assert cache.get_skipped_ids("personal") == {"shared_id"}

    def test_recording_twice_is_a_noop(self, cache):
        """A partial run must be safe to repeat."""
        cache.record_skipped("personal", ["msg1"])
        cache.record_skipped("personal", ["msg1", "msg2"])

        assert cache.get_skipped_ids("personal") == {"msg1", "msg2"}

    def test_empty_write_is_harmless(self, cache):
        assert cache.record_skipped("personal", []) == 0
        assert cache.get_skipped_ids("personal") == set()

    def test_unknown_account_is_empty_not_an_error(self, cache):
        assert cache.get_skipped_ids("never_synced") == set()


class TestPruning:
    def _backdate(self, cache, message_id, days):
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(cache.db_path)
        conn.execute(
            "UPDATE skipped_messages SET skipped_at = ? WHERE message_id = ?",
            (stamp, message_id),
        )
        conn.commit()
        conn.close()

    def test_prunes_only_expired_entries(self, cache):
        """Unbounded growth is the failure mode pruning exists to prevent."""
        cache.record_skipped("personal", ["old", "recent"])
        self._backdate(cache, "old", days=120)

        removed = cache.prune(retention_days=90)

        assert removed == 1
        assert cache.get_skipped_ids("personal") == {"recent"}

    def test_retention_covers_the_sync_window(self, cache):
        """
        An entry must outlive the 30-day look-back window, or a message would
        be re-fetched while still in range — the bug this cache fixes.
        """
        cache.record_skipped("personal", ["msg1"])
        self._backdate(cache, "msg1", days=31)

        cache.prune(retention_days=90)

        assert cache.get_skipped_ids("personal") == {"msg1"}

    def test_prune_on_empty_cache(self, cache):
        assert cache.prune() == 0


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        """The whole point is remembering across nightly runs."""
        path = str(tmp_path / "skip.db")
        GmailSkipCache(db_path=path).record_skipped("personal", ["msg1"])

        assert GmailSkipCache(db_path=path).get_skipped_ids("personal") == {"msg1"}
