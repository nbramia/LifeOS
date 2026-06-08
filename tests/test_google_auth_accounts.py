"""
Tests for google_auth account helpers (issue #330).

Locks in the WORK2-aware behavior of resolve_account / get_configured_accounts
that was previously dead — shadowed by simpler duplicate definitions. Guards
against the duplicates (or a re-simplification) coming back.
"""
import pytest

from api.services.google_auth import resolve_account, get_configured_accounts, GoogleAccount

pytestmark = pytest.mark.unit


class TestResolveAccount:
    def test_personal(self):
        assert resolve_account("personal") == GoogleAccount.PERSONAL

    def test_work(self):
        assert resolve_account("work") == GoogleAccount.WORK

    def test_work2_is_not_collapsed_to_work(self):
        # The bug fixed in #330: the shadowing duplicate mapped anything != "personal"
        # to WORK, losing WORK2.
        assert resolve_account("work2") == GoogleAccount.WORK2

    def test_unknown_defaults_to_personal(self):
        assert resolve_account("nonsense") == GoogleAccount.PERSONAL


class TestGetConfiguredAccounts:
    def test_personal_always_present(self):
        assert GoogleAccount.PERSONAL in get_configured_accounts()

    def test_detects_credential_files_including_work2(self, tmp_path, monkeypatch):
        # get_configured_accounts reads ./config relative to cwd.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "credentials-work2.json").write_text("{}")

        accounts = get_configured_accounts()
        assert GoogleAccount.PERSONAL in accounts   # always
        assert GoogleAccount.WORK2 in accounts      # credential file present
        assert GoogleAccount.WORK not in accounts   # no work credential file here

    def test_no_extra_accounts_without_credentials(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        accounts = get_configured_accounts()
        assert accounts == [GoogleAccount.PERSONAL]
