"""
People System Configuration for LifeOS.

Maps email domains to vault contexts and normalizes company names for entity resolution.

Note: Domain mappings and company normalization are loaded from config/crm_mappings.yaml
(gitignored - see config/crm_mappings.example.yaml for template).
Entity resolution weights are in config/relationship_weights.py
"""
from pathlib import Path
from typing import Optional
import logging

# Import entity resolution weights from centralized config
from config.relationship_weights import (
    NAME_SIMILARITY_WEIGHT,
    CONTEXT_BOOST_POINTS,
    RECENCY_BOOST_POINTS,
    RECENCY_BOOST_THRESHOLD_DAYS,
    DISAMBIGUATION_THRESHOLD,
    MIN_MATCH_SCORE,
    ENTITY_CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


def _load_crm_mappings() -> tuple[dict[str, list[str]], dict[str, dict]]:
    """
    Load CRM mappings from YAML config file.

    Returns:
        Tuple of (domain_context_map, company_normalization)
    """
    config_path = Path(__file__).parent / "crm_mappings.yaml"

    # Default mappings for common personal email domains
    default_domain_map: dict[str, list[str]] = {
        "gmail.com": ["Personal/"],
        "icloud.com": ["Personal/"],
        "outlook.com": ["Personal/"],
        "hotmail.com": ["Personal/"],
        "yahoo.com": ["Personal/"],
    }
    default_company_norm: dict[str, dict] = {}

    if not config_path.exists():
        logger.info("No crm_mappings.yaml found, using defaults only")
        return default_domain_map, default_company_norm

    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        # Parse domain mappings
        domain_map = dict(default_domain_map)  # Start with defaults
        for domain, info in config.get("domain_mappings", {}).items():
            if isinstance(info, dict) and "vault_contexts" in info:
                domain_map[domain.lower()] = info["vault_contexts"]

        # Parse company normalization
        company_norm = dict(default_company_norm)
        for company, info in config.get("company_normalization", {}).items():
            if isinstance(info, dict):
                company_norm[company] = {
                    "domains": info.get("domains", []),
                    "vault_contexts": info.get("vault_contexts", []),
                }

        return domain_map, company_norm

    except Exception as e:
        logger.warning(f"Failed to load crm_mappings.yaml: {e}, using defaults")
        return default_domain_map, default_company_norm


# Load mappings at module import time
DOMAIN_CONTEXT_MAP, COMPANY_NORMALIZATION = _load_crm_mappings()


# Entity Resolution Configuration
# Note: All weights are now imported from config/relationship_weights.py
class EntityResolutionConfig:
    """
    Configuration for entity resolution algorithm.

    All weights are imported from config/relationship_weights.py for centralized management.
    This class provides a convenient interface for accessing them.
    """

    # Fuzzy matching thresholds (imported from relationship_weights.py)
    NAME_SIMILARITY_WEIGHT: float = NAME_SIMILARITY_WEIGHT
    CONTEXT_BOOST_POINTS: int = CONTEXT_BOOST_POINTS
    RECENCY_BOOST_POINTS: int = RECENCY_BOOST_POINTS
    RECENCY_THRESHOLD_DAYS: int = RECENCY_BOOST_THRESHOLD_DAYS

    # Disambiguation threshold
    DISAMBIGUATION_THRESHOLD: int = DISAMBIGUATION_THRESHOLD

    # Minimum score to consider a match
    MIN_MATCH_SCORE: float = MIN_MATCH_SCORE

    # Cache settings
    QUERY_CACHE_TTL_SECONDS: int = ENTITY_CACHE_TTL_SECONDS


# Interaction Log Configuration
class InteractionConfig:
    """Configuration for interaction tracking."""

    # Default time window for interaction queries (10 years - volume controlled by limit)
    DEFAULT_WINDOW_DAYS: int = 3650

    # Maximum time window allowed for timeline queries (10 years + buffer)
    MAX_WINDOW_DAYS: int = 3660

    # Maximum interactions to return in a single query (default when no limit specified)
    MAX_INTERACTIONS_PER_QUERY: int = 1000

    # Snippet length for interaction preview
    SNIPPET_LENGTH: int = 100


def get_vault_contexts_for_domain(domain: str) -> list[str]:
    """
    Get vault contexts associated with an email domain.

    Args:
        domain: Email domain (e.g., "example.com")

    Returns:
        List of vault context paths, or empty list if domain unknown
    """
    return DOMAIN_CONTEXT_MAP.get(domain.lower(), [])


def get_domains_for_company(company_name: str) -> list[str]:
    """
    Get email domains associated with a company name.

    Args:
        company_name: Company name from LinkedIn (e.g., "Example Corp")

    Returns:
        List of email domains, or empty list if company unknown
    """
    company_info = COMPANY_NORMALIZATION.get(company_name, {})
    return company_info.get("domains", [])


def get_vault_contexts_for_company(company_name: str) -> list[str]:
    """
    Get vault contexts associated with a company name.

    Args:
        company_name: Company name from LinkedIn (e.g., "Example Corp")

    Returns:
        List of vault context paths, or empty list if company unknown
    """
    company_info = COMPANY_NORMALIZATION.get(company_name, {})
    return company_info.get("vault_contexts", [])


def normalize_domain(email: str) -> Optional[str]:
    """
    Extract and normalize domain from email address.

    Args:
        email: Full email address

    Returns:
        Lowercase domain, or None if invalid email
    """
    if not email or "@" not in email:
        return None
    return email.split("@")[1].lower()
