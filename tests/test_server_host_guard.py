"""Tests for the server host guard (#506).

LIFEOS_SERVER_HOSTNAME designates the one machine allowed to run the LifeOS
API server. Unset (the default) must never block a start — a fresh
open-source clone has to work out of the box. Set + matching hostname starts
normally; set + mismatched hostname refuses to start with a message naming
both hosts.

The real hostname of whatever machine runs the test suite is irrelevant here
— every case mocks `socket.gethostname()` rather than depending on it.
"""
import pytest

import api.main as main
from config.settings import settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_server_hostname():
    """Isolate `settings.server_hostname` across tests (it's a shared singleton)."""
    original = settings.server_hostname
    yield
    settings.server_hostname = original


def test_guard_unset_does_not_raise(monkeypatch):
    """Default (LIFEOS_SERVER_HOSTNAME unset) must never block a start."""
    settings.server_hostname = ""
    monkeypatch.setattr(main.socket, "gethostname", lambda: "whatever-host")

    main.check_server_host_guard()  # must not raise


def test_guard_set_matching_hostname_does_not_raise(monkeypatch):
    """Designated host running the server starts normally."""
    settings.server_hostname = "the-server"
    monkeypatch.setattr(main.socket, "gethostname", lambda: "the-server")

    main.check_server_host_guard()  # must not raise


def test_guard_set_mismatched_hostname_raises(monkeypatch):
    """A non-designated machine refuses to start; message names both hosts."""
    settings.server_hostname = "the-server"
    monkeypatch.setattr(main.socket, "gethostname", lambda: "some-other-machine")

    with pytest.raises(RuntimeError) as exc_info:
        main.check_server_host_guard()

    message = str(exc_info.value)
    assert "the-server" in message
    assert "some-other-machine" in message
