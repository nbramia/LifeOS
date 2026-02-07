"""
CRM API Routes Package.

This package contains the CRM API endpoints organized by domain:
- models.py: All Pydantic request/response models
- _utils.py: Shared helper functions (person lookup, category computation, etc.)
- people.py: Person CRUD endpoints (TODO)
- facts.py: Person facts endpoints (TODO)
- timeline.py: Timeline/interaction endpoints (TODO)
- relationships.py: Relationship endpoints (TODO)
- sources.py: Source sync + Apple Contacts (TODO)
- me.py: Me dashboard endpoints (TODO)
- family.py: Family dashboard endpoints (TODO)
- health.py: Sync health + review queue (TODO)
- insights.py: Relationship insights (TODO)

Migration Status:
- Phase 1: models.py and _utils.py created
- Phase 2: Endpoint migration in progress (using original crm.py as source)

For now, the original api/routes/crm.py file remains the primary router.
This package will be fully populated in future refactoring phases.
"""

# Re-export models for convenience
from api.routes.crm_models.models import (
    # Person models
    PersonDetailResponse,
    PersonListResponse,
    PersonUpdateRequest,
    PersonMergeRequest,
    PersonMergeResponse,
    PersonSplitRequest,
    PersonSplitResponse,
    ContactSource,
    LinkConfirmRequest,
    HidePersonRequest,
    # Source/Relationship models
    SourceEntityResponse,
    RelationshipResponse,
    RelationshipDetailResponse,
    # Timeline models
    TimelineItem,
    TimelineResponse,
    AggregatedTimelineItem,
    AggregatedDayGroup,
    AggregatedTimelineResponse,
    # Connection models
    ConnectionResponse,
    ConnectionsResponse,
    SuggestedConnectionResponse,
    DiscoverResponse,
    # Network graph models
    NetworkNode,
    NetworkEdge,
    NetworkGraphResponse,
    # Facts models
    PersonFactResponse,
    PersonFactsResponse,
    FactUpdateRequest,
    FactExtractionResponse,
    # Statistics
    StatisticsResponse,
    # Me dashboard
    MeStatsResponse,
    DailyAggregate,
    TopContact,
    TrendPerson,
    NeglectedContact,
    MonthlyNetworkGrowth,
    MonthlyMessagingVolume,
    HealthScorePoint,
    TrackedRelationshipPoint,
    TrackedRelationship,
    MeInteractionsResponse,
    # Family dashboard
    FamilyMember,
    FamilyMembersResponse,
    FamilyStatsResponse,
    FamilyInteractionsResponse,
    CommunicationGap,
    CommunicationGapsResponse,
    ChannelMixMember,
    ChannelMixResponse,
    # Sync health
    SyncHealthResponse,
    SyncHealthSummaryResponse,
    SyncErrorResponse,
    # Review queue
    ReviewQueueItem,
    ReviewQueueResponse,
    # Link overrides
    LinkOverrideResponse,
    # Relationship insights
    RelationshipInsightResponse,
    RelationshipInsightsResponse,
    ToneDataPoint,
    ToneAnalysisResponse,
)

# Re-export utility functions
from api.routes.crm_models._utils import (
    # Constants
    WORK_EMAIL_DOMAIN,
    MY_PERSON_ID,
    PARTNER_PERSON_ID,
    FAMILY_LAST_NAMES,
    FAMILY_EXACT_NAMES,
    # Functions
    get_strength_override,
    is_family_member,
    compute_person_category,
    tokenize,
    fuzzy_name_match,
    search_matches,
    source_entity_to_response,
    relationship_to_response,
    person_to_detail_response,
    # Underscore aliases for backward compat
    _get_strength_override,
    _is_family_member,
    _tokenize,
    _fuzzy_name_match,
    _search_matches,
    _source_entity_to_response,
    _relationship_to_response,
    _person_to_detail_response,
)

__all__ = [
    # Models
    "PersonDetailResponse",
    "PersonListResponse",
    "PersonUpdateRequest",
    "PersonMergeRequest",
    "PersonMergeResponse",
    "PersonSplitRequest",
    "PersonSplitResponse",
    "ContactSource",
    "LinkConfirmRequest",
    "HidePersonRequest",
    "SourceEntityResponse",
    "RelationshipResponse",
    "RelationshipDetailResponse",
    "TimelineItem",
    "TimelineResponse",
    "AggregatedTimelineItem",
    "AggregatedDayGroup",
    "AggregatedTimelineResponse",
    "ConnectionResponse",
    "ConnectionsResponse",
    "SuggestedConnectionResponse",
    "DiscoverResponse",
    "NetworkNode",
    "NetworkEdge",
    "NetworkGraphResponse",
    "PersonFactResponse",
    "PersonFactsResponse",
    "FactUpdateRequest",
    "FactExtractionResponse",
    "StatisticsResponse",
    "MeStatsResponse",
    "DailyAggregate",
    "TopContact",
    "TrendPerson",
    "NeglectedContact",
    "MonthlyNetworkGrowth",
    "MonthlyMessagingVolume",
    "HealthScorePoint",
    "TrackedRelationshipPoint",
    "TrackedRelationship",
    "MeInteractionsResponse",
    "FamilyMember",
    "FamilyMembersResponse",
    "FamilyStatsResponse",
    "FamilyInteractionsResponse",
    "CommunicationGap",
    "CommunicationGapsResponse",
    "ChannelMixMember",
    "ChannelMixResponse",
    "SyncHealthResponse",
    "SyncHealthSummaryResponse",
    "SyncErrorResponse",
    "ReviewQueueItem",
    "ReviewQueueResponse",
    "LinkOverrideResponse",
    "RelationshipInsightResponse",
    "RelationshipInsightsResponse",
    "ToneDataPoint",
    "ToneAnalysisResponse",
    # Constants
    "WORK_EMAIL_DOMAIN",
    "MY_PERSON_ID",
    "PARTNER_PERSON_ID",
    "FAMILY_LAST_NAMES",
    "FAMILY_EXACT_NAMES",
    # Functions
    "get_strength_override",
    "is_family_member",
    "compute_person_category",
    "tokenize",
    "fuzzy_name_match",
    "search_matches",
    "source_entity_to_response",
    "relationship_to_response",
    "person_to_detail_response",
]
