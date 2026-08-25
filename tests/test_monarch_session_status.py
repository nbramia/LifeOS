"""Tests for the Monarch session-age detector (issue #199 §3).

The real bug we're guarding against: the cached Monarch session token is just
a pickle on disk that silently expires every ~30 days. The monthly sync
notices via a 401/525 from Monarch; by then the operator has missed two
months of data. This detector surfaces the expiry window early so it's
visible on the health dashboard before things break.
"""
import os
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_session(tmp_path, monkeypatch):
    """Point SESSION_PATH at a fresh tempfile each test."""
    from api.services import monarch as mod
    path = tmp_path / "monarch_session.pickle"
    monkeypatch.setattr(mod, "SESSION_PATH", path)
    return path


class TestGetSessionAgeDays:
    def test_returns_none_when_no_session(self, temp_session):
        from api.services.monarch import get_session_age_days
        assert get_session_age_days() is None

    def test_returns_age_in_days(self, temp_session):
        from api.services.monarch import get_session_age_days
        temp_session.write_bytes(b"")
        # Backdate the file mtime by 10 days.
        ten_days_ago = time.time() - 10 * 86400
        os.utime(temp_session, (ten_days_ago, ten_days_ago))
        age = get_session_age_days()
        assert age is not None
        assert 9.9 < age < 10.1


class TestGetSessionStatus:
    def test_missing_session(self, temp_session):
        from api.services.monarch import get_session_status
        status = get_session_status()
        assert status["exists"] is False
        assert status["status"] == "missing"

    def test_fresh_session(self, temp_session):
        from api.services.monarch import get_session_status
        temp_session.write_bytes(b"")
        status = get_session_status()
        assert status["exists"] is True
        assert status["status"] == "ok"

    def test_session_expiring_soon(self, temp_session):
        from api.services.monarch import get_session_status, SESSION_WARNING_DAYS
        temp_session.write_bytes(b"")
        # Backdate to one day past the warning threshold.
        backdate = time.time() - (SESSION_WARNING_DAYS + 1) * 86400
        os.utime(temp_session, (backdate, backdate))
        assert get_session_status()["status"] == "expiring_soon"

    def test_session_expired(self, temp_session):
        from api.services.monarch import get_session_status, SESSION_EXPIRY_DAYS
        temp_session.write_bytes(b"")
        backdate = time.time() - (SESSION_EXPIRY_DAYS + 5) * 86400
        os.utime(temp_session, (backdate, backdate))
        assert get_session_status()["status"] == "expired"


class TestIsMonarchConfigured:
    """Pins the exact "not configured" condition for issue #687: the
    nightly sync must skip cleanly ONLY when there is truly no way to
    authenticate (no cached session AND no credentials). Anything else
    (a session that exists but might be stale, or credentials present but
    wrong) has to keep reaching the real client so a genuine outage still
    fails loud -- conflating the two would hide a real Monarch outage on
    the maintainer's own box, the exact regression #646 fixed elsewhere.
    """

    def test_no_session_no_credentials_is_not_configured(self, temp_session, monkeypatch):
        from api.services.monarch import is_monarch_configured, settings
        monkeypatch.setattr(settings, "monarch_email", "")
        monkeypatch.setattr(settings, "monarch_password", "")
        assert is_monarch_configured() is False

    def test_cached_session_alone_is_configured(self, temp_session, monkeypatch):
        """A cached session is sufficient even with no credentials in .env --
        the documented flow lets credentials be scrubbed after first login."""
        from api.services.monarch import is_monarch_configured, settings
        monkeypatch.setattr(settings, "monarch_email", "")
        monkeypatch.setattr(settings, "monarch_password", "")
        temp_session.write_bytes(b"")
        assert is_monarch_configured() is True

    def test_credentials_alone_is_configured(self, temp_session, monkeypatch):
        """No cached session yet, but MONARCH_EMAIL/PASSWORD are set (e.g.
        first run before the initial login) still counts as configured."""
        from api.services.monarch import is_monarch_configured, settings
        monkeypatch.setattr(settings, "monarch_email", "user@example.com")
        monkeypatch.setattr(settings, "monarch_password", "hunter2")
        assert is_monarch_configured() is True

    def test_partial_credentials_is_not_configured(self, temp_session, monkeypatch):
        """Only one of email/password set (e.g. mid-edit .env) must not
        count as configured -- matches _get_client()'s `and` check."""
        from api.services.monarch import is_monarch_configured, settings
        monkeypatch.setattr(settings, "monarch_email", "user@example.com")
        monkeypatch.setattr(settings, "monarch_password", "")
        assert is_monarch_configured() is False


class TestWriteMonthlyReportVaultDir:
    """Pins that write_monthly_report() honors LIFEOS_MONARCH_VAULT_DIR
    (issue #687 #4) -- both the unset-default (must match the previously
    hardcoded path exactly) and an explicit override."""

    @pytest.mark.asyncio
    async def test_default_vault_dir_matches_hardcoded_path(self, tmp_path, monkeypatch):
        from api.services.monarch import MonarchClient, settings

        monkeypatch.setattr(settings, "vault_path", tmp_path)
        monkeypatch.setattr(settings, "monarch_vault_dir", "Personal/Finance/Monarch")

        client = MonarchClient()
        async def _fake_generate(year, month):
            return "content"

        monkeypatch.setattr(client, "generate_monthly_report", _fake_generate)

        result = await client.write_monthly_report(2026, 1, dry_run=True)
        assert result["file"] == str(tmp_path / "Personal" / "Finance" / "Monarch" / "2026-01.md")

    @pytest.mark.asyncio
    async def test_env_override_changes_vault_dir(self, tmp_path, monkeypatch):
        from api.services.monarch import MonarchClient, settings

        monkeypatch.setattr(settings, "vault_path", tmp_path)
        monkeypatch.setattr(settings, "monarch_vault_dir", "Personal/Money/Monarch")

        client = MonarchClient()
        async def _fake_generate(year, month):
            return "content"

        monkeypatch.setattr(client, "generate_monthly_report", _fake_generate)

        result = await client.write_monthly_report(2026, 1, dry_run=True)
        assert result["file"] == str(tmp_path / "Personal" / "Money" / "Monarch" / "2026-01.md")
