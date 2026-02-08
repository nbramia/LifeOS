"""
LifeOS Services Package.

This package contains all business logic and data access services.
Use this module to import commonly-used services.

Example:
    from api.services import (
        get_person_entity_store,
        get_interaction_store,
        get_relationship_store,
    )

Key service modules:
- person_entity: PersonEntity model and store
- source_entity: SourceEntity for raw observations
- interaction_store: Interaction records
- relationship: Relationship model and store
- person_facts: Fact extraction and storage
- chat_helpers: Query parsing utilities
"""

# ============================================================================
# Person & CRM Services
# ============================================================================

from api.services.person_entity import (
    PersonEntity,
    get_person_entity_store,
)

from api.services.source_entity import (
    SourceEntity,
    get_source_entity_store,
    LINK_STATUS_CONFIRMED,
    LINK_STATUS_REJECTED,
)

from api.services.interaction_store import (
    Interaction,
    get_interaction_store,
)

from api.services.relationship import (
    Relationship,
    get_relationship_store,
)

from api.services.person_facts import (
    PersonFact,
    get_person_fact_store,
    get_person_fact_extractor,
    FACT_CATEGORIES,
)

# ============================================================================
# Relationship Services
# ============================================================================

from api.services.relationship_metrics import (
    compute_strength_for_person,
    update_all_strengths,
    update_strength_for_person,
)

from api.services.relationship_discovery import (
    run_full_discovery,
    get_suggested_connections,
)

# ============================================================================
# Chat Helpers
# ============================================================================

from api.services.chat_helpers import (
    extract_search_keywords,
    expand_followup_query,
    detect_compose_intent,
    detect_reminder_intent,
    extract_date_context,
    extract_message_date_range,
    extract_message_search_terms,
    ReminderIntentType,
    classify_reminder_intent,
    detect_reminder_edit_intent,
    detect_reminder_list_intent,
    detect_reminder_delete_intent,
    extract_reminder_topic,
)

# ============================================================================
# Shared Utilities (re-exported from api.utils)
# ============================================================================

from api.utils import make_aware, get_crm_db_path


__all__ = [
    # Person/CRM
    "PersonEntity",
    "get_person_entity_store",
    "SourceEntity",
    "get_source_entity_store",
    "LINK_STATUS_CONFIRMED",
    "LINK_STATUS_REJECTED",
    "Interaction",
    "get_interaction_store",
    "Relationship",
    "get_relationship_store",
    "PersonFact",
    "get_person_fact_store",
    "get_person_fact_extractor",
    "FACT_CATEGORIES",
    # Relationships
    "compute_strength_for_person",
    "update_all_strengths",
    "update_strength_for_person",
    "run_full_discovery",
    "get_suggested_connections",
    # Chat helpers
    "extract_search_keywords",
    "expand_followup_query",
    "detect_compose_intent",
    "detect_reminder_intent",
    "extract_date_context",
    "extract_message_date_range",
    "extract_message_search_terms",
    "ReminderIntentType",
    "classify_reminder_intent",
    "detect_reminder_edit_intent",
    "detect_reminder_list_intent",
    "detect_reminder_delete_intent",
    "extract_reminder_topic",
    # Shared utilities
    "make_aware",
    "get_crm_db_path",
]
