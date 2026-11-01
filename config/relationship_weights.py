"""
Relationship and Entity Resolution Weights Configuration.

Central configuration for all weights used in:
- Relationship strength calculation
- Entity resolution scoring
- Interaction type weighting

Edit this file to tune relationship scoring behavior.
"""

import json
import logging
from pathlib import Path

# =============================================================================
# RELATIONSHIP STRENGTH WEIGHTS
# =============================================================================
# Formula:
#   strength = 100 * (recency * RECENCY_WEIGHT
#                     + recent_frequency * RECENT_FREQUENCY_WEIGHT
#                     + lifetime_frequency * LIFETIME_FREQUENCY_WEIGHT
#                     + diversity * DIVERSITY_WEIGHT)
# The family and LinkedIn multipliers apply to the result, capped at 100.
#
# Pair strength (Relationship.pair_strength in api/services/relationship.py) is
# a separate formula with its own constants; nothing here affects it.

RECENCY_WEIGHT = 0.25             # How recently the last real interaction happened
RECENT_FREQUENCY_WEIGHT = 0.55    # Decayed volume of interaction: the current relationship
LIFETIME_FREQUENCY_WEIGHT = 0.15  # Undecayed volume: a long history doesn't vanish entirely
DIVERSITY_WEIGHT = 0.05           # How many channels are in use

# recency = 0.5 ** (days_since_last_real_interaction / RECENCY_HALF_LIFE_DAYS)
RECENCY_HALF_LIFE_DAYS = 45

# recent_frequency = min(1, log(1 + S) / log(1 + RECENT_FREQUENCY_TARGET))
# where S = sum of weight * 0.5 ** (age_days / RECENT_FREQUENCY_HALF_LIFE_DAYS)
RECENT_FREQUENCY_HALF_LIFE_DAYS = 90
RECENT_FREQUENCY_TARGET = 600

# lifetime_frequency = min(1, log(1 + W) / log(1 + LIFETIME_FREQUENCY_TARGET))
# where W = sum of weight over LIFETIME_WINDOW_DAYS
LIFETIME_FREQUENCY_TARGET = 2500
LIFETIME_WINDOW_DAYS = 3650

# Recency discount for zero-interaction contacts
# People with no tracked interactions get NO recency credit
# (contacts list and LinkedIn connections shouldn't inflate scores)
MIN_INTERACTIONS_FOR_FULL_RECENCY = 15  # Need at least 15 interactions for full recency credit
ZERO_INTERACTION_RECENCY_MULTIPLIER = 0.0  # Zero interactions = 0% recency (contacts/LinkedIn don't count)

# Peripheral contact threshold
# People with relationship_strength below this are marked as peripheral contacts
# and excluded from expensive aggregation calculations (placed in Dunbar circle 7)
PERIPHERAL_THRESHOLD = 5.0


# =============================================================================
# MANUAL STRENGTH OVERRIDES (loaded from config/relationship_overrides.json)
# =============================================================================
# Force specific people to have a fixed relationship strength regardless of
# calculated value. These also affect Dunbar circle placement.
# Keys are person IDs (UUIDs), values are strength (0-100).
#
# To configure, create config/relationship_overrides.json (see .example.json)

_logger = logging.getLogger(__name__)

def _load_relationship_overrides():
    """Load relationship overrides from JSON config file."""
    config_path = Path(__file__).parent / "relationship_overrides.json"
    strength_overrides = {}
    circle_overrides = {}

    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            strength_overrides = config.get("strength_overrides", {})
            circle_overrides = config.get("circle_overrides", {})
            # Convert string values to proper types
            strength_overrides = {k: float(v) for k, v in strength_overrides.items()}
            circle_overrides = {k: int(v) for k, v in circle_overrides.items()}
        except Exception as e:
            _logger.warning(f"Failed to load relationship overrides: {e}")

    return strength_overrides, circle_overrides

STRENGTH_OVERRIDES_BY_ID, CIRCLE_OVERRIDES_BY_ID = _load_relationship_overrides()


# =============================================================================
# INTERACTION TYPE WEIGHTS
# =============================================================================
# Weight applied to each interaction when calculating frequency scores.
# Higher weight = interaction counts more toward relationship strength.
#
# Rationale:
# - Direct 1:1 communication (DM, text, call) weighted highest
# - Synchronous communication (meetings, calls) weighted high
# - Asynchronous communication (email) weighted medium
# - Passive inclusion (being mentioned, CC'd) weighted lower
#
# Note: Currently we only have source_type. Future: add subtype for To/CC, 1:1/group.

INTERACTION_TYPE_WEIGHTS: dict[str, float] = {
    # Direct messaging (high intimacy, intentional contact)
    "imessage": 1.5,          # Personal text message
    "whatsapp": 1.5,          # Personal messaging app
    "signal": 1.5,            # Secure personal messaging
    "slack": 0.3,             # Work DM (less personal, often noisy)

    # Voice/Video (highest effort, synchronous)
    "phone_call": 3.0,        # Voice call - high effort
    "phone": 3.0,             # Phone call (alternate name)
    "facetime": 2.0,          # Video call - high effort

    # Calendar (meetings - synchronous, high signal)
    "calendar": 4.0,          # Meetings - strong relationship signal
    # Future: calendar_1on1: 1.5, calendar_small_group: 1.0, calendar_large_meeting: 0.5

    # Email (async, often broadcast/CC)
    "gmail": 0.8,             # Email - often CC'd or mass
    # Future: gmail_to: 1.0, gmail_cc: 0.3, gmail_sent: 1.2

    # Written content (you wrote about them - shows they're on your mind)
    "vault": 0.7,             # Mentioned in your notes
    "granola": 0.8,           # Meeting notes (AI-generated)

    # Photos (face recognition - in photos together)
    "photos": 5.0,            # In photos together

    # Contact sources (static, not interactions)
    "linkedin": 0.3,          # LinkedIn connection (passive)
    "contacts": 0.2,          # In your contacts (very passive)
    "phone_contacts": 0.2,    # Same as contacts
}

# Default weight for unknown interaction types
DEFAULT_INTERACTION_WEIGHT = 1.0


# =============================================================================
# INTERACTION SUBTYPE WEIGHTS
# =============================================================================
# More granular weights for interaction subtypes (parsed from title/metadata).
# These override the base INTERACTION_TYPE_WEIGHTS when available.

# Share of the whatsapp weight a group message carries. A group message you sent
# is one row per participant, so it says little about any one of them; a group
# message you received at least came from that person.
WHATSAPP_GROUP_SENT_MULTIPLIER = 0.15
WHATSAPP_GROUP_RECEIVED_MULTIPLIER = 0.35

INTERACTION_SUBTYPE_WEIGHTS: dict[str, float] = {
    # Gmail subtypes (parsed from title prefix: →/←/↔)
    "gmail_received": 1.0,      # ← prefix - received email
    "gmail_sent": 1.2,          # → prefix - sent email (higher effort)
    "gmail_cc": 0.3,            # ↔ prefix - CC'd/thread participant

    # Calendar subtypes (derived from attendee_count)
    "calendar_1on1": 6.0,         # 1 other attendee - high intimacy
    "calendar_small_group": 4.0,  # 2-5 other attendees
    "calendar_large_meeting": 2.0,  # 6+ other attendees - diluted attention

    # WhatsApp group subtypes (derived from attendee_count and source_id)
    "whatsapp_group_sent": INTERACTION_TYPE_WEIGHTS["whatsapp"] * WHATSAPP_GROUP_SENT_MULTIPLIER,
    "whatsapp_group_received": INTERACTION_TYPE_WEIGHTS["whatsapp"] * WHATSAPP_GROUP_RECEIVED_MULTIPLIER,
}


# =============================================================================
# ACCOUNT-BASED MULTIPLIERS
# =============================================================================
# Personal accounts are weighted higher than work accounts.
# Rationale: Personal email/calendar interactions are more relationship-focused.

ACCOUNT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "gmail": {"personal": 2.0, "work": 1.0},
    "calendar": {"personal": 3.0, "work": 1.0},
}


# =============================================================================
# ENTITY RESOLUTION WEIGHTS
# =============================================================================
# Used when matching source entities to canonical person entities

# Fuzzy name matching
NAME_SIMILARITY_WEIGHT = 0.4      # Weight for fuzzy name match score (0-1 scaled to 0-40)

# Context boosting
CONTEXT_BOOST_POINTS = 30         # Points added when email domain matches vault context
RECENCY_BOOST_POINTS = 10         # Points added for recently seen people
RECENCY_BOOST_THRESHOLD_DAYS = 30 # Days to consider someone "recently seen"

# Disambiguation
DISAMBIGUATION_THRESHOLD = 15     # If top two candidates within this score, it's ambiguous
MIN_MATCH_SCORE = 40.0           # Minimum score to consider a valid match

# Relationship strength boost for name-only resolution
# When resolving by name only (no email/phone), prefer people with existing relationship
RELATIONSHIP_STRENGTH_BOOST_MAX = 25    # Max points for relationship strength boost (0-100 strength -> 0-25 points)
RELATIONSHIP_STRENGTH_BOOST_WEIGHT = 0.25  # Multiplier: strength * weight = boost points

# First-name-only boost multiplier
# When matching just "Ben" instead of "Ben Calvin", apply stronger relationship boost
# because first-name-only mentions in notes usually refer to close contacts
FIRST_NAME_ONLY_BOOST_MULTIPLIER = 1.5  # Multiply relationship boost by this for single-word names

# Cache settings
ENTITY_CACHE_TTL_SECONDS = 1800  # 30 minutes


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_interaction_weight(
    source_type: str,
    subtype: str | None = None,
    source_account: str | None = None,
) -> float:
    """
    Get the weight for an interaction, considering subtype and account.

    Priority:
    1. If subtype provided and in INTERACTION_SUBTYPE_WEIGHTS, use that
    2. Otherwise, use base weight from INTERACTION_TYPE_WEIGHTS
    3. Apply account multiplier if source_account provided

    Args:
        source_type: The source type (e.g., "gmail", "imessage", "calendar")
        subtype: Optional subtype (e.g., "gmail_sent", "calendar_1on1")
        source_account: Optional account type ("personal" or "work")

    Returns:
        Weight multiplier for this interaction
    """
    # Get base weight (subtype takes priority if available)
    if subtype and subtype in INTERACTION_SUBTYPE_WEIGHTS:
        base_weight = INTERACTION_SUBTYPE_WEIGHTS[subtype]
    else:
        base_weight = INTERACTION_TYPE_WEIGHTS.get(source_type, DEFAULT_INTERACTION_WEIGHT)

    # Apply account multiplier if applicable
    if source_account and source_type in ACCOUNT_MULTIPLIERS:
        multiplier = ACCOUNT_MULTIPLIERS[source_type].get(source_account, 1.0)
        return base_weight * multiplier

    return base_weight


# =============================================================================
# TAG OVERRIDES (loaded from config/linkedin_tags.json)
# =============================================================================
# Manual tag overrides (person_id -> list of tags)
# Tags follow format: industry:X, seniority:X, state:XX, city:X
#
# To configure, create config/linkedin_tags.json (see .example.json)

def _load_tag_overrides():
    """Load tag overrides from JSON config file."""
    config_path = Path(__file__).parent / "linkedin_tags.json"
    tag_overrides = {}

    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            tag_overrides = config.get("tag_overrides", {})
        except Exception as e:
            _logger.warning(f"Failed to load tag overrides: {e}")

    return tag_overrides

TAG_OVERRIDES_BY_ID: dict[str, list[str]] = _load_tag_overrides()