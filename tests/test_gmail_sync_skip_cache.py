"""
The Gmail sync must not re-fetch mail it already judged to be marketing.

Discarded messages produce no interaction row, so the sync's existing_base_ids
set could never remember them: ~13k messages were re-fetched every night for
their whole 30-day life, costing ~42 min per run (#552).
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.unit

from api.services.gmail import EmailMessage  # noqa: E402
from api.services.gmail_skip_cache import GmailSkipCache  # noqa: E402
from api.services.google_auth import GoogleAccount  # noqa: E402


def _email(message_id, sender, sender_name="Sender"):
    from datetime import datetime, timezone
    return EmailMessage(
        message_id=message_id,
        thread_id=f"t_{message_id}",
        subject="Subject",
        sender=sender,
        sender_name=sender_name,
        date=datetime.now(timezone.utc),
        snippet="snippet",
        source_account="personal",
    )


@pytest.fixture
def sync_env(tmp_path):
    """Run the Gmail sync against isolated databases with a mocked API."""
    from api.services.interaction_store import InteractionStore

    cache = GmailSkipCache(db_path=str(tmp_path / "skip.db"))
    interactions_path = str(tmp_path / "interactions.db")
    InteractionStore(db_path=interactions_path)  # creates the real schema

    gmail = MagicMock()
    # Two promotional messages and one real one.
    listed = [{"id": "promo1"}, {"id": "promo2"}, {"id": "real1"}]
    gmail.service.users().messages().list().execute.return_value = {"messages": listed}
    gmail.get_messages_batch.side_effect = lambda ids, **kw: {
        mid: _email(
            mid,
            "deals@mailchimp.com" if mid.startswith("promo") else "dana@example.com",
        )
        for mid in ids
    }

    with patch("scripts.sync_gmail_calendar_interactions.GmailService", return_value=gmail), \
         patch("scripts.sync_gmail_calendar_interactions.get_gmail_skip_cache", return_value=cache), \
         patch("scripts.sync_gmail_calendar_interactions.get_interaction_db_path",
               return_value=interactions_path), \
         patch("scripts.sync_gmail_calendar_interactions.get_source_entity_store",
               return_value=MagicMock()), \
         patch("scripts.sync_gmail_calendar_interactions.get_entity_resolver") as resolver:
        resolver.return_value.resolve.return_value = MagicMock(
            entity=MagicMock(id="person1"), confidence=1.0, match_type="email_exact",
        )
        yield gmail, cache


def _run(**kwargs):
    from scripts.sync_gmail_calendar_interactions import sync_gmail_interactions
    return sync_gmail_interactions(
        account_type=GoogleAccount.PERSONAL, dry_run=False, days_back=30, **kwargs
    )


class TestSkipCacheIntegration:
    def test_first_run_fetches_everything_and_records_discards(self, sync_env):
        gmail, cache = sync_env

        stats = _run()

        fetched = gmail.get_messages_batch.call_args.args[0]
        assert set(fetched) == {"promo1", "promo2", "real1"}
        assert stats["marketing_skipped"] == 2
        # The discards are remembered for next time.
        assert cache.get_skipped_ids("personal") == {"promo1", "promo2"}

    def test_second_run_does_not_refetch_known_marketing(self, sync_env):
        """The whole point: yesterday's promotional mail costs nothing today."""
        gmail, cache = sync_env
        _run()
        gmail.get_messages_batch.reset_mock()

        stats = _run()

        fetched = gmail.get_messages_batch.call_args.args[0]
        assert "promo1" not in fetched and "promo2" not in fetched
        # Still counted as skipped, so the stats stay honest.
        assert stats["marketing_skipped"] == 2

    def test_cached_skips_are_not_counted_as_errors(self, sync_env):
        """
        A cached skip is never fetched, so it is absent from the batch result.
        Treating that absence as a fetch failure would fake an error every night.
        """
        gmail, cache = sync_env
        _run()

        stats = _run()

        assert stats["errors"] == 0

    def test_dry_run_does_not_write_to_the_cache(self, sync_env):
        gmail, cache = sync_env
        from scripts.sync_gmail_calendar_interactions import sync_gmail_interactions

        sync_gmail_interactions(
            account_type=GoogleAccount.PERSONAL, dry_run=True, days_back=30,
        )

        assert cache.get_skipped_ids("personal") == set()

    def test_legitimate_mail_is_never_cached_as_skipped(self, sync_env):
        """
        The cache must only ever suppress discards. Caching a real sender would
        silently drop them from the CRM forever.
        """
        gmail, cache = sync_env

        stats = _run()

        assert "real1" not in cache.get_skipped_ids("personal")
        assert stats["inserted"] > 0, "the legitimate message should be stored"

    def test_steady_state_fetches_nothing(self, sync_env):
        """
        With every message either stored or cached, a run costs no fetches at
        all — this is what takes the nightly run from ~42 min to seconds.
        """
        gmail, cache = sync_env
        _run()
        gmail.get_messages_batch.reset_mock()

        _run()

        fetched = gmail.get_messages_batch.call_args.args[0]
        assert fetched == []
