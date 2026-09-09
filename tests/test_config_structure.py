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

    def test_has_domain_mappings(self, tmp_path, monkeypatch):
        """CRM mappings should have a domain_mappings section."""
        import config.crm_config as crm_config

        synthetic = tmp_path / "crm_mappings.yaml"
        synthetic.write_text("domain_mappings:\n  example.com:\n    context: Work\n", encoding="utf-8")
        monkeypatch.setattr(crm_config, "MAPPINGS_FILE", synthetic)
        crm_config.reload_config()
        try:
            mappings = crm_config.get_mappings()
            # Assert the exact synthetic mapping was loaded, not merely
            # that a domain_mappings key exists -- a broken loader could
            # otherwise satisfy a bare key-presence check via some
            # unrelated fallback value.
            assert mappings["domain_mappings"] == {"example.com": {"context": "Work"}}
        finally:
            crm_config.reload_config()


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

    def test_structure_if_exists(self, tmp_path, monkeypatch):
        """Verify people dictionary structure through the real loader."""
        import json

        import api.services.people as people_module

        dict_path = tmp_path / "people_dictionary.json"
        synthetic = {
            "Alex Chen": {"aliases": ["Al Chen"], "category": "work"},
            "Sam Rivera": {"category": "family"},
        }
        dict_path.write_text(json.dumps(synthetic), encoding="utf-8")
        monkeypatch.setattr(people_module, "PEOPLE_DICTIONARY_PATH", dict_path)

        data = people_module._load_people_dictionary()

        # A broken or silently-empty loader must fail this, not just a
        # malformed-content check -- assert the real loader actually
        # produced our exact synthetic entry, not an empty fallback.
        assert data == synthetic
        assert "Alex Chen" in data
        # Each entry should have expected structure
        for name, info in data.items():
            assert isinstance(name, str)
            if isinstance(info, dict):
                # Common fields: aliases, category, emails, phones
                valid_keys = {"aliases", "category", "emails", "phones", "company"}
                assert any(key in info for key in valid_keys) or info == {}
