"""Contacts authorization pre-check for push_birthdays_to_contacts.

A CNContactStore fetch with authorization status notDetermined makes macOS try
to raise a consent prompt. Under launchd/cron there is no GUI session to show
it, so the call blocks indefinitely instead of erroring — seen in production as
a 60-minute nightly sync timeout that also dependency-skipped downstream
sources, from a source with 12 birthdays to push.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


NOT_DETERMINED, RESTRICTED, DENIED, AUTHORIZED = 0, 1, 2, 3


def _install_fake_contacts(status):
    """Stub the macOS-only Contacts module so this runs on any platform."""
    mod = types.ModuleType("Contacts")
    mod.CNContactStore = MagicMock()
    mod.CNContactStore.authorizationStatusForEntityType_ = MagicMock(return_value=status)
    mod.CNContactIdentifierKey = "id"
    mod.CNContactGivenNameKey = "given"
    mod.CNContactFamilyNameKey = "family"
    mod.CNContactEmailAddressesKey = "emails"
    mod.CNContactBirthdayKey = "birthday"
    mod.CNContactFetchRequest = MagicMock()
    return mod


def _run(status, isatty, capsys):
    from scripts import push_birthdays_to_contacts as mod

    fake = _install_fake_contacts(status)
    with patch.dict(sys.modules, {"Contacts": fake}), \
         patch.object(sys.stdin, "isatty", return_value=isatty):
        result = mod.get_apple_contacts_with_email()
    return result, capsys.readouterr().out


class TestContactsAuthPrecheck:

    @pytest.mark.parametrize("status", [NOT_DETERMINED, RESTRICTED, DENIED])
    def test_unauthorized_non_interactive_skips_instead_of_blocking(self, status, capsys):
        """The scheduled case: declare a skip rather than hang on a prompt."""
        result, out = _run(status, isatty=False, capsys=capsys)
        assert result == {}
        assert "SYNC_SKIPPED" in out
        assert "Contacts access not granted" in out

    def test_skip_message_names_the_actual_status(self, capsys):
        _, out = _run(DENIED, isatty=False, capsys=capsys)
        assert "denied" in out

    def test_unauthorized_but_interactive_does_not_skip(self, capsys):
        """A human at a TTY must still get macOS's prompt.

        Skipping here would make the permission effectively ungrantable.
        """
        from scripts import push_birthdays_to_contacts as mod

        fake = _install_fake_contacts(NOT_DETERMINED)
        with patch.dict(sys.modules, {"Contacts": fake}), \
             patch.object(sys.stdin, "isatty", return_value=True):
            mod.get_apple_contacts_with_email()

        out = capsys.readouterr().out
        assert "SYNC_SKIPPED" not in out
        # Fell through to real store construction rather than short-circuiting.
        assert fake.CNContactStore.alloc.called

    def test_authorized_proceeds_normally(self, capsys):
        from scripts import push_birthdays_to_contacts as mod

        fake = _install_fake_contacts(AUTHORIZED)
        with patch.dict(sys.modules, {"Contacts": fake}), \
             patch.object(sys.stdin, "isatty", return_value=False):
            mod.get_apple_contacts_with_email()

        out = capsys.readouterr().out
        assert "SYNC_SKIPPED" not in out
        assert fake.CNContactStore.alloc.called
