"""
CRM Configuration Loader.

Loads CRM mappings and settings from YAML files with fallback to defaults.
"""
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Config file paths
CONFIG_DIR = Path(__file__).parent
MAPPINGS_FILE = CONFIG_DIR / "crm_mappings.yaml"
SETTINGS_FILE = CONFIG_DIR / "crm_settings.yaml"

# Cached config
_mappings: Optional[dict] = None
_settings: Optional[dict] = None


def _load_yaml(path: Path) -> dict:
    """Load YAML file with error handling."""
    try:
        if path.exists():
            with open(path, "r") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
    return {}


def get_mappings() -> dict:
    """
    Get CRM domain and company mappings.

    Returns:
        Dict with domain_mappings and company_normalization
    """
    global _mappings
    if _mappings is None:
        _mappings = _load_yaml(MAPPINGS_FILE)
    return _mappings


def get_settings() -> dict:
    """
    Get CRM settings.

    Returns:
        Dict with all CRM settings
    """
    global _settings
    if _settings is None:
        _settings = _load_yaml(SETTINGS_FILE)
    return _settings


def reload_config() -> None:
    """Reload configuration from files."""
    global _mappings, _settings
    _mappings = None
    _settings = None
    logger.info("CRM configuration reloaded")


# Convenience functions for common lookups


def get_domain_mapping(domain: str) -> Optional[dict]:
    """
    Get mapping info for an email domain.

    Args:
        domain: Email domain (e.g., "work.example.com")

    Returns:
        Dict with company, vault_contexts, category; or None if not found
    """
    mappings = get_mappings()
    domain_mappings = mappings.get("domain_mappings", {})
    return domain_mappings.get(domain.lower())


def get_vault_contexts_for_domain(domain: str) -> list[str]:
    """
    Get vault contexts associated with an email domain.

    Args:
        domain: Email domain (e.g., "work.example.com")

    Returns:
        List of vault context paths, or empty list if domain unknown
    """
    mapping = get_domain_mapping(domain)
    if mapping:
        return mapping.get("vault_contexts", [])
    return []


def get_company_for_domain(domain: str) -> Optional[str]:
    """
    Get company name for an email domain.

    Args:
        domain: Email domain

    Returns:
        Company name or None
    """
    mapping = get_domain_mapping(domain)
    if mapping:
        return mapping.get("company")
    return None


def get_category_for_domain(domain: str) -> str:
    """
    Get category (work/personal) for an email domain.

    Args:
        domain: Email domain

    Returns:
        Category string, defaults to "unknown"
    """
    mapping = get_domain_mapping(domain)
    if mapping:
        return mapping.get("category", "unknown")
    return "unknown"


def get_company_normalization(company_name: str) -> Optional[dict]:
    """
    Get normalization info for a company name.

    Args:
        company_name: Company name from LinkedIn

    Returns:
        Dict with domains and vault_contexts; or None if not found
    """
    mappings = get_mappings()
    company_normalization = mappings.get("company_normalization", {})
    return company_normalization.get(company_name)


def get_domains_for_company(company_name: str) -> list[str]:
    """
    Get email domains associated with a company name.

    Args:
        company_name: Company name from LinkedIn

    Returns:
        List of email domains, or empty list if company unknown
    """
    normalization = get_company_normalization(company_name)
    if normalization:
        return normalization.get("domains", [])
    return []


def get_vault_contexts_for_company(company_name: str) -> list[str]:
    """
    Get vault contexts associated with a company name.

    Args:
        company_name: Company name from LinkedIn

    Returns:
        List of vault context paths, or empty list if company unknown
    """
    normalization = get_company_normalization(company_name)
    if normalization:
        return normalization.get("vault_contexts", [])
    return []


# Settings access functions


def get_entity_resolution_config() -> dict:
    """Get entity resolution configuration."""
    settings = get_settings()
    return settings.get("entity_resolution", {})


def get_relationship_strength_config() -> dict:
    """Get relationship strength configuration."""
    settings = get_settings()
    return settings.get("relationship_strength", {})


def get_discovery_config() -> dict:
    """Get relationship discovery configuration."""
    settings = get_settings()
    return settings.get("discovery", {})


def get_pending_links_config() -> dict:
    """Get pending links configuration."""
    settings = get_settings()
    return settings.get("pending_links", {})


def get_sync_config() -> dict:
    """Get sync configuration."""
    settings = get_settings()
    return settings.get("sync", {})


def get_source_config(source_name: str) -> dict:
    """
    Get configuration for a specific data source.

    Args:
        source_name: Source name (gmail, calendar, slack, etc.)

    Returns:
        Dict with source configuration
    """
    settings = get_settings()
    sources = settings.get("sources", {})
    return sources.get(source_name, {})


def is_source_enabled(source_name: str) -> bool:
    """
    Check if a data source is enabled.

    Args:
        source_name: Source name

    Returns:
        True if enabled, False otherwise
    """
    config = get_source_config(source_name)
    return config.get("enabled", False)
