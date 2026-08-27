"""Tests for scripts/sync_monarch_money.py's clean-skip path (issue #687).

Before this fix, an unconfigured Monarch install (no cached session, no
MONARCH_EMAIL/MONARCH_PASSWORD) raised inside MonarchClient._get_client(),
propagated through sync_monarch() to main()'s broad `except Exception`, and
recorded SyncStatus.FAILED every night forever on any install without
Monarch. These tests pin both directions: an unconfigured install skips
cleanly (SYNC_SKIPPED marker + SyncStatus.SKIPPED, run succeeds), and a
configured-but-genuinely-broken install still fails loud, so absence of
config and presence of real errors are never conflated.
"""
import sys

import pytest

pytestmark = pytest.mark.unit


class TestSyncMonarchFunctionSkip:
    """Exercises sync_monarch() directly -- the structured status/reason it
    returns, and the SYNC_SKIPPED marker run_all_syncs._parse_sync_output
    reads from stdout."""

    @pytest.mark.asyncio
    async def test_unconfigured_returns_skip_and_prints_marker(self, monkeypatch, capsys):
        from scripts.sync_monarch_money import sync_monarch

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: False)

        result = await sync_monarch(dry_run=False, month="2026-01")

        assert result["status"] == "skipped"
        assert result["reason"] == "monarch_not_configured"
        captured = capsys.readouterr()
        assert "SYNC_SKIPPED:" in captured.out

    @pytest.mark.asyncio
    async def test_configured_does_not_skip(self, monkeypatch):
        from scripts.sync_monarch_money import sync_monarch

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: True)

        class FakeClient:
            async def write_monthly_report(self, year, month, dry_run=False):
                return {"status": "success", "file": "x.md", "size": 3}

        monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: FakeClient())

        result = await sync_monarch(dry_run=False, month="2026-01")
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_dry_run_never_touches_configuration_check(self, monkeypatch):
        """A plain (non-executing) dry run must behave exactly as before --
        it never calls the Monarch client at all, configured or not."""
        from scripts.sync_monarch_money import sync_monarch

        def _boom():
            raise AssertionError("is_monarch_configured must not be called on a dry run")

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", _boom)

        result = await sync_monarch(dry_run=True, month="2026-01")
        assert result["status"] == "dry_run"


class TestMainRecordsSkipStatus:
    """Exercises main() end-to-end -- the sync_health status it records is
    the structured contract the orchestrator (and /health) actually reads,
    not the log text (#646's lesson)."""

    def _patch_sync_health(self, monkeypatch, recorded):
        monkeypatch.setattr(
            "api.services.sync_health.record_sync_start", lambda source: 1
        )

        def fake_complete(run_id, status=None, **kwargs):
            recorded["status"] = status
            recorded["kwargs"] = kwargs

        monkeypatch.setattr(
            "api.services.sync_health.record_sync_complete", fake_complete
        )

    def test_unconfigured_execute_records_skipped_and_succeeds(self, monkeypatch):
        from api.services.sync_health import SyncStatus
        from scripts.sync_monarch_money import main

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: False)
        recorded = {}
        self._patch_sync_health(monkeypatch, recorded)
        monkeypatch.setattr(sys, "argv", ["sync_monarch_money.py", "--execute"])

        main()  # must NOT sys.exit — a skip is not a failure

        assert recorded["status"] == SyncStatus.SKIPPED

    def test_configured_but_broken_still_records_failed(self, monkeypatch):
        """A real outage (bad session, network, wrong password) on a
        configured install must still exit nonzero and record FAILED --
        the exact case a naive "no exception = skip" gate would have hidden."""
        from api.services.sync_health import SyncStatus
        from scripts.sync_monarch_money import main

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: True)

        class BrokenClient:
            async def write_monthly_report(self, year, month, dry_run=False):
                raise RuntimeError("session invalid, re-auth required")

        monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: BrokenClient())

        recorded = {}
        self._patch_sync_health(monkeypatch, recorded)
        monkeypatch.setattr(sys, "argv", ["sync_monarch_money.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert recorded["status"] == SyncStatus.FAILED

    def test_empty_exception_message_falls_back_to_class_name(self, monkeypatch):
        """A bare exception with no message (issue #781) must still record
        something an operator — and run_all_syncs.py's transient-failure
        classifier, which reads this same subprocess's stderr — can act
        on, rather than an empty string that matches nothing."""
        from api.services.sync_health import SyncStatus
        from scripts.sync_monarch_money import main

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: True)

        class BrokenClient:
            async def write_monthly_report(self, year, month, dry_run=False):
                raise ConnectionError()  # str(e) == ""

        monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: BrokenClient())

        recorded = {}
        self._patch_sync_health(monkeypatch, recorded)
        monkeypatch.setattr(sys, "argv", ["sync_monarch_money.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert recorded["status"] == SyncStatus.FAILED
        error_message = recorded["kwargs"]["error_message"]
        assert error_message
        assert "ConnectionError" in error_message

    def test_normal_exception_message_unchanged(self, monkeypatch, caplog):
        """An exception with a real message must be recorded exactly as
        before this change — no class-name prefix added when it isn't
        needed (#781's acceptance criteria: behavior-preserving). Also
        pins the emitted log text, since that's what run_all_syncs.py's
        transient-failure classifier actually reads (this script runs as
        a subprocess and the classifier inspects captured stderr)."""
        import logging
        from api.services.sync_health import SyncStatus
        from scripts.sync_monarch_money import main

        monkeypatch.setattr("api.services.monarch.is_monarch_configured", lambda: True)

        class BrokenClient:
            async def write_monthly_report(self, year, month, dry_run=False):
                raise RuntimeError("session invalid, re-auth required")

        monkeypatch.setattr("api.services.monarch.get_monarch_client", lambda: BrokenClient())

        recorded = {}
        self._patch_sync_health(monkeypatch, recorded)
        monkeypatch.setattr(sys, "argv", ["sync_monarch_money.py", "--execute"])

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert "Monarch Money sync failed: session invalid, re-auth required" in caplog.text

        assert exc_info.value.code == 1
        assert recorded["status"] == SyncStatus.FAILED
        assert recorded["kwargs"]["error_message"] == "session invalid, re-auth required"


class TestFailureMessageEnrichment:
    """Unit tests for _failure_message()'s fallback construction (#781)."""

    def test_empty_message_uses_class_name(self):
        from scripts.sync_monarch_money import _failure_message

        assert _failure_message(ConnectionError()) == "ConnectionError"

    def test_non_empty_message_returned_unchanged(self):
        from scripts.sync_monarch_money import _failure_message

        e = RuntimeError("session invalid, re-auth required")
        assert _failure_message(e) == "session invalid, re-auth required"

    def test_empty_message_with_status_code_attr_included(self):
        from scripts.sync_monarch_money import _failure_message

        class HTTPError(Exception):
            pass

        e = HTTPError()
        e.status_code = 429
        assert _failure_message(e) == "HTTPError (status=429)"

    def test_empty_message_with_response_status_included_no_body(self):
        """Status code is included; response body is deliberately excluded
        (Monarch is a financial data source — an error body could carry
        account/personal detail, and the "never log real personal data"
        boundary applies even on this fallback path)."""
        from scripts.sync_monarch_money import _failure_message

        class FakeResponse:
            status_code = 500
            text = "Internal Server Error: account 1234-5678 over limit"

        class HTTPError(Exception):
            pass

        e = HTTPError()
        e.response = FakeResponse()
        message = _failure_message(e)
        assert message == "HTTPError (status=500)"
        assert "1234-5678" not in message

    def test_misbehaving_response_property_does_not_crash(self):
        """A third-party exception whose `response`/`status_code`
        attributes raise on access must not itself crash the failure
        handler it's meant to describe — fall back to just the class
        name."""
        from scripts.sync_monarch_money import _failure_message

        class BoomResponse:
            @property
            def status_code(self):
                raise RuntimeError("not decoded yet")

        class HTTPError(Exception):
            pass

        e = HTTPError()
        e.response = BoomResponse()
        assert _failure_message(e) == "HTTPError"
