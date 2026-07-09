"""Exit-code hardening for credential-dependent sync scripts.

A skipped or fatally-failed sync must exit nonzero so run_all_syncs records
FAILED (and alerts) instead of silent success — see issue #438, where the
nightly slack sync "succeeded" in 0.28s for 8 days because the not-enabled
skip path exited 0.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSlackSyncExitCodes:
    """scripts/sync_slack.py must fail loudly when the integration is disabled."""

    def test_skip_exits_nonzero(self):
        """Missing SLACK_USER_TOKEN → exit code 2, not silent success."""
        from scripts.sync_slack import main

        with patch("api.services.slack_integration.is_slack_enabled", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                main(["--execute"])

        assert exc_info.value.code == 2

    def test_dry_run_skip_also_exits_nonzero(self):
        """A disabled integration is a misconfiguration even in dry-run mode."""
        from scripts.sync_slack import main

        with patch("api.services.slack_integration.is_slack_enabled", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                main([])

        assert exc_info.value.code == 2

    def test_enabled_dry_run_exits_cleanly(self):
        """An enabled dry run completes without raising."""
        from scripts.sync_slack import main

        with patch("api.services.slack_integration.is_slack_enabled", return_value=True):
            main([])  # no SystemExit

    def test_errors_with_zero_work_exit_nonzero(self):
        """Errors and no work done (e.g. dead token at API-call time) → exit 1.

        full_sync/incremental_sync report this as status "partial", so status
        alone can't distinguish a total failure from a mostly-good run.
        """
        from scripts.sync_slack import main

        total_failure = {
            "status": "partial",
            "errors": ["invalid_auth: token revoked"],
            "users": {},
            "messages": {"channels_synced": 0, "messages_indexed": 0, "interactions_created": 0},
        }
        with patch("scripts.sync_slack.run_slack_sync", return_value=total_failure):
            with pytest.raises(SystemExit) as exc_info:
                main(["--execute"])

        assert exc_info.value.code == 1

    def test_errors_with_real_work_do_not_fail_the_run(self):
        """Partial errors alongside real work remain success (like gmail's
        per-message error tolerance)."""
        from scripts.sync_slack import main

        partial_with_work = {
            "status": "partial",
            "errors": ["one channel failed"],
            "users": {},
            "messages": {"channels_synced": 5, "messages_indexed": 120, "interactions_created": 40},
        }
        with patch("scripts.sync_slack.run_slack_sync", return_value=partial_with_work):
            main(["--execute"])  # no SystemExit


class TestGoogleDocsSyncExitCodes:
    """scripts/sync_google_docs.py must fail loudly when the sync dies outright.

    google_docs is the third documented casualty of issue #438 — its typical
    ~12s duration sits below the 60s duration-collapse gate, so the exit code
    is the only defense.
    """

    def test_error_status_exits_nonzero(self):
        """sync_gdocs raising (e.g. expired OAuth) → exit code 1."""
        from scripts.sync_google_docs import main

        # sync_gdocs is imported inside sync_google_docs(), so patch the
        # source module attribute.
        with patch(
            "api.services.gdoc_sync.sync_gdocs",
            side_effect=RuntimeError("invalid_grant: Token has been expired"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(["--execute"])

        assert exc_info.value.code == 1

    def test_healthy_execute_exits_cleanly(self):
        """A successful sync completes without raising."""
        from scripts.sync_google_docs import main

        healthy = {"synced": 2, "failed": 0, "skipped": 0, "documents": []}
        with patch("api.services.gdoc_sync.sync_gdocs", return_value=healthy):
            main(["--execute"])  # no SystemExit

    def test_dry_run_exits_cleanly(self):
        """Dry run never calls sync_gdocs and never exits nonzero."""
        from scripts.sync_google_docs import main

        main([])  # no SystemExit


class TestGmailCalendarSyncExitCodes:
    """scripts/sync_gmail_calendar_interactions.py must fail loudly on
    account-level (fatal) errors while tolerating per-message errors."""

    def _gmail_stats(self, **overrides):
        stats = {
            "fetched": 0,
            "inserted": 0,
            "marketing_skipped": 0,
            "already_exists": 0,
            "no_person": 0,
            "errors": 0,
            "source_entities_created": 0,
            "affected_person_ids": set(),
        }
        stats.update(overrides)
        return stats

    def test_fatal_error_exits_nonzero(self):
        """An account-level failure (e.g. dead credentials) → exit code 1."""
        import scripts.sync_gmail_calendar_interactions as mod

        fatal = self._gmail_stats(errors=1, fatal_error="invalid_grant: Token has been expired")
        with patch.object(mod, "sync_gmail_interactions", return_value=fatal):
            with pytest.raises(SystemExit) as exc_info:
                mod.main(["--gmail-only", "--account", "personal"])

        assert exc_info.value.code == 1

    def test_per_message_errors_do_not_fail_the_run(self):
        """A handful of per-message errors is normal — the run still succeeds."""
        import scripts.sync_gmail_calendar_interactions as mod

        noisy = self._gmail_stats(fetched=500, inserted=480, errors=3)
        with patch.object(mod, "sync_gmail_interactions", return_value=noisy):
            result = mod.main(["--gmail-only", "--account", "personal"])

        assert result is not None

    def test_fatal_error_recorded_by_outer_exception_handler(self, tmp_path):
        """The account-level except block stamps fatal_error into stats."""
        import sqlite3

        import scripts.sync_gmail_calendar_interactions as mod
        from api.services.google_auth import GoogleAccount
        from unittest.mock import MagicMock

        db = tmp_path / "interactions.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE interactions (id TEXT, source_id TEXT, source_type TEXT)")
        conn.commit()
        conn.close()

        # An API failure inside the fetch loop (e.g. expired credentials) is
        # caught by the account-level except — it must be marked fatal.
        broken_gmail = MagicMock()
        broken_gmail.service.users.side_effect = RuntimeError("auth exploded")

        with (
            patch.object(mod, "get_interaction_db_path", return_value=str(db)),
            patch.object(mod, "get_entity_resolver"),
            patch.object(mod, "get_source_entity_store"),
            patch.object(mod, "GmailService", return_value=broken_gmail),
        ):
            stats = mod.sync_gmail_interactions(
                account_type=GoogleAccount.PERSONAL, dry_run=True
            )

        assert "auth exploded" in stats.get("fatal_error", "")
        assert stats["errors"] == 1
