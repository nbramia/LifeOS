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
