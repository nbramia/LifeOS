"""
Tests for configuration file structure and integrity.

These tests validate that config files have the correct structure
without exposing actual values. This ensures config changes don't
break the application.
"""
import pytest


@pytest.mark.unit
class TestDomainContextMap:
    """Tests for DOMAIN_CONTEXT_MAP configuration."""

    def test_has_entries(self):
        """Config should have at least personal email domains."""
        from config.people_config import DOMAIN_CONTEXT_MAP
        # At minimum: gmail.com, icloud.com for personal domains
        assert len(DOMAIN_CONTEXT_MAP) >= 2

    def test_structure(self):
        """All entries must have correct structure."""
        from config.people_config import DOMAIN_CONTEXT_MAP
        for domain, contexts in DOMAIN_CONTEXT_MAP.items():
            assert isinstance(domain, str), f"Domain must be string: {domain}"
            assert isinstance(contexts, list), f"Contexts must be list for {domain}"
            for ctx in contexts:
                assert "/" in ctx, f"Context must be a path: {ctx}"


@pytest.mark.unit
class TestCompanyNormalization:
    """Tests for COMPANY_NORMALIZATION configuration."""

    def test_structure(self):
        """Company entries must have domains or vault_contexts."""
        from config.people_config import COMPANY_NORMALIZATION
        for company, mapping in COMPANY_NORMALIZATION.items():
            assert isinstance(company, str), f"Company name must be string: {company}"
            if mapping:  # Empty dict is OK (open-source default)
                assert isinstance(mapping, dict), f"Mapping must be dict for {company}"
                # If non-empty, should have useful keys
                if len(mapping) > 0:
                    valid_keys = {"domains", "vault_contexts", "aliases"}
                    assert any(key in mapping for key in valid_keys), \
                        f"Mapping for {company} should have domains, vault_contexts, or aliases"


@pytest.mark.unit
class TestCrmMappings:
    """Tests for CRM mappings YAML configuration."""

    def test_yaml_loads(self):
        """CRM mappings YAML must be valid and loadable."""
        from config.crm_config import get_mappings
        mappings = get_mappings()
        assert mappings is not None
        assert isinstance(mappings, dict)

    def test_has_domain_mappings(self):
        """CRM mappings should have domain_mappings section."""
        from config.crm_config import get_mappings
        mappings = get_mappings()
        assert "domain_mappings" in mappings


@pytest.mark.unit
class TestSettingsStructure:
    """Tests for Settings configuration structure."""

    def test_settings_loads(self):
        """Settings should load without error."""
        from config.settings import Settings
        settings = Settings()
        assert settings is not None

    def test_required_paths_exist(self):
        """Settings should have required path fields."""
        from config.settings import Settings
        settings = Settings()
        # These are the key paths the app needs
        assert hasattr(settings, "vault_path")
        assert hasattr(settings, "chroma_path")
        assert hasattr(settings, "chroma_url")

    def test_server_config_exists(self):
        """Settings should have server configuration."""
        from config.settings import Settings
        settings = Settings()
        assert hasattr(settings, "host")
        assert hasattr(settings, "port")
        assert settings.port > 0


@pytest.mark.unit
class TestPeopleDictionary:
    """Tests for People Dictionary structure (if loaded)."""

    def test_structure_if_exists(self):
        """If people dictionary exists, verify its structure."""
        import json
        from pathlib import Path

        dict_path = Path("config/people_dictionary.json")
        if not dict_path.exists():
            pytest.skip("People dictionary not configured (expected for open-source)")

        with open(dict_path) as f:
            data = json.load(f)

        assert isinstance(data, dict)
        # Each entry should have expected structure
        for name, info in data.items():
            assert isinstance(name, str)
            if isinstance(info, dict):
                # Common fields: aliases, category, emails, phones
                valid_keys = {"aliases", "category", "emails", "phones", "company"}
                assert any(key in info for key in valid_keys) or info == {}
