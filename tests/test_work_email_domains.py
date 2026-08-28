"""
Tests for Settings.work_email_domains (#764).

Work email domains were hard-capped at two (LIFEOS_WORK_DOMAIN,
LIFEOS_WORK_DOMAIN_2). LIFEOS_WORK_DOMAINS_EXTRA adds an arbitrary number
of further domains without touching the first two, which must keep
behaving exactly as before for an already-configured install.
"""
import pytest

from config.settings import Settings

pytestmark = pytest.mark.unit


def _settings(monkeypatch, **env):
    """Build an isolated Settings instance with only the given env vars set."""
    for key in (
        "LIFEOS_WORK_DOMAIN",
        "LIFEOS_WORK_DOMAIN_2",
        "LIFEOS_WORK_DOMAINS_EXTRA",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


class TestWorkEmailDomains:
    def test_none_set(self, monkeypatch):
        settings = _settings(monkeypatch)
        assert settings.work_email_domains == []

    def test_only_first_domain_set(self, monkeypatch):
        """Unchanged from current behavior."""
        settings = _settings(monkeypatch, LIFEOS_WORK_DOMAIN="acme.com")
        assert settings.work_email_domains == ["acme.com"]

    def test_first_and_second_domain_set(self, monkeypatch):
        """Unchanged from current behavior."""
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAIN="acme.com",
            LIFEOS_WORK_DOMAIN_2="othercompany.com",
        )
        assert settings.work_email_domains == ["acme.com", "othercompany.com"]

    def test_only_extra_list_set(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAINS_EXTRA="thirdco.com,fourthco.com",
        )
        assert settings.work_email_domains == ["thirdco.com", "fourthco.com"]

    def test_only_second_domain_set(self, monkeypatch):
        """LIFEOS_WORK_DOMAIN_2 alone, with no first domain, is still honored."""
        settings = _settings(monkeypatch, LIFEOS_WORK_DOMAIN_2="othercompany.com")
        assert settings.work_email_domains == ["othercompany.com"]

    def test_first_domain_and_extra_set(self, monkeypatch):
        """First domain ahead of the extra list, with no second domain set."""
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAIN="acme.com",
            LIFEOS_WORK_DOMAINS_EXTRA="thirdco.com,fourthco.com",
        )
        assert settings.work_email_domains == ["acme.com", "thirdco.com", "fourthco.com"]

    def test_second_domain_and_extra_set(self, monkeypatch):
        """Second domain ahead of the extra list, with no first domain set."""
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAIN_2="othercompany.com",
            LIFEOS_WORK_DOMAINS_EXTRA="thirdco.com,fourthco.com",
        )
        assert settings.work_email_domains == ["othercompany.com", "thirdco.com", "fourthco.com"]

    def test_first_second_and_extra_set(self, monkeypatch):
        """First domain, then second domain, ahead of the new list's entries."""
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAIN="acme.com",
            LIFEOS_WORK_DOMAIN_2="othercompany.com",
            LIFEOS_WORK_DOMAINS_EXTRA="thirdco.com,fourthco.com",
        )
        assert settings.work_email_domains == [
            "acme.com",
            "othercompany.com",
            "thirdco.com",
            "fourthco.com",
        ]

    def test_extra_list_ignores_blank_entries_and_whitespace(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAINS_EXTRA=" thirdco.com ,, fourthco.com ,",
        )
        assert settings.work_email_domains == ["thirdco.com", "fourthco.com"]

    def test_existing_vars_still_named_and_readable_directly(self, monkeypatch):
        """LIFEOS_WORK_DOMAIN / LIFEOS_WORK_DOMAIN_2 are not removed or renamed."""
        settings = _settings(
            monkeypatch,
            LIFEOS_WORK_DOMAIN="acme.com",
            LIFEOS_WORK_DOMAIN_2="othercompany.com",
        )
        assert settings.work_email_domain == "acme.com"
        assert settings.work_email_domain_2 == "othercompany.com"
