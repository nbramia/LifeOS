"""
Pydantic models for CRM API responses and requests.

All response and request models are consolidated here for reuse across
the CRM route modules.
"""
from typing import Optional
from pydantic import BaseModel, Field


# ============================================================================
# Source Entity & Relationship Models
# ============================================================================

class SourceEntityResponse(BaseModel):
    """Response model for a source entity."""
    id: str
    source_type: str
    source_id: Optional[str] = None
    observed_name: Optional[str] = None
    observed_email: Optional[str] = None
    observed_phone: Optional[str] = None
    link_confidence: float = 0.0
    link_status: str = "auto"
    observed_at: Optional[str] = None
    source_badge: str = ""


class RelationshipResponse(BaseModel):
    """Response model for a relationship."""
    id: str
    person_a_id: str
    person_b_id: str
    relationship_type: str
    shared_contexts: list[str] = []
    shared_events_count: int = 0
    shared_threads_count: int = 0
    first_seen_together: Optional[str] = None
    last_seen_together: Optional[str] = None
    person_a_name: Optional[str] = None
    person_b_name: Optional[str] = None


class RelationshipDetailResponse(BaseModel):
    """Response model for relationship details between two people."""
    person_a_id: str
    person_a_name: str
    person_b_id: str
    person_b_name: str
    relationship_type: str
    shared_contexts: list[str] = []
    shared_events_count: int = 0
    shared_threads_count: int = 0
    shared_messages_count: int = 0
    shared_whatsapp_count: int = 0
    shared_slack_count: int = 0
    shared_phone_calls_count: int = 0
    is_linkedin_connection: bool = False
    total_interactions: int = 0
    first_seen_together: Optional[str] = None
    last_seen_together: Optional[str] = None
    weight: int = 0


# ============================================================================
# Person Models
# ============================================================================

class PersonDetailResponse(BaseModel):
    """Extended person response with CRM data."""
    id: str
    canonical_name: str
    display_name: str
    emails: list[str] = []
    phone_numbers: list[str] = []
    company: Optional[str] = None
    position: Optional[str] = None
    linkedin_url: Optional[str] = None
    category: str = "unknown"
    vault_contexts: list[str] = []
    tags: list[str] = []
    birthday: Optional[str] = None  # "MM-DD" format (month-day only)
    notes: str = ""
    sources: list[str] = []
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    relationship_strength: float = 0.0
    source_entity_count: int = 0
    meeting_count: int = 0
    email_count: int = 0
    mention_count: int = 0
    message_count: int = 0
    dunbar_circle: Optional[int] = None
    source_entities: list[SourceEntityResponse] = []
    relationships: list[RelationshipResponse] = []


class PersonListResponse(BaseModel):
    """Response for person list endpoint."""
    people: list[PersonDetailResponse]
    count: int
    total: int
    offset: int = 0
    has_more: bool = False


class PersonUpdateRequest(BaseModel):
    """Request for updating a person."""
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    category: Optional[str] = None
    birthday: Optional[str] = None  # "MM-DD" format (month-day only), empty string to clear


class PersonMergeRequest(BaseModel):
    """Request for merging people."""
    primary_id: str = Field(..., description="ID of the person to keep (survivor)")
    secondary_ids: list[str] = Field(..., description="IDs of people to merge into primary")


class PersonMergeResponse(BaseModel):
    """Response for merge operation."""
    status: str
    primary_id: str
    merged_ids: list[str]
    stats: dict


class PersonSplitRequest(BaseModel):
    """Request to split source entities from a person to another."""
    from_person_id: str
    to_person_id: Optional[str] = None
    new_person_name: Optional[str] = None
    source_entity_ids: list[str]
    create_overrides: bool = True


class PersonSplitResponse(BaseModel):
    """Response for split operation."""
    status: str
    from_person_id: str
    to_person_id: str
    source_entities_moved: int
    interactions_moved: int
    overrides_created: int


class ContactSource(BaseModel):
    """An aggregated contact source - a unique identifier linked to a person."""
    identifier: str
    identifier_type: str
    source_types: list[str]
    observation_count: int
    source_entity_ids: list[str]
    observed_names: list[str]
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class LinkConfirmRequest(BaseModel):
    """Request for confirming or rejecting a link."""
    create_new_person: bool = Field(
        default=False,
        description="If rejecting, create a new person from the source entity"
    )
    new_person_name: Optional[str] = Field(
        default=None,
        description="Name for the new person (required if create_new_person=True)"
    )


class HidePersonRequest(BaseModel):
    """Request for hiding a person (soft delete)."""
    reason: str = Field(
        default="",
        description="Reason for hiding (e.g., 'fake marketing persona')"
    )


# ============================================================================
# Timeline Models
# ============================================================================

class TimelineItem(BaseModel):
    """Response model for a timeline item."""
    id: str
    timestamp: str
    source_type: str
    title: str
    snippet: Optional[str] = None
    source_link: str = ""
    source_badge: str = ""


class TimelineResponse(BaseModel):
    """Response for timeline endpoint."""
    items: list[TimelineItem]
    count: int
    has_more: bool = False


class AggregatedTimelineItem(BaseModel):
    """Response model for an aggregated timeline item (grouped by day + type)."""
    date: str
    source_type: str
    source_badge: str
    count: int
    preview: Optional[str] = None
    items: list[TimelineItem] = []


class AggregatedDayGroup(BaseModel):
    """A day's worth of aggregated interactions."""
    date: str
    date_display: str
    total_count: int
    groups: list[AggregatedTimelineItem]


class AggregatedTimelineResponse(BaseModel):
    """Response for aggregated timeline endpoint."""
    days: list[AggregatedDayGroup]
    total_interactions: int
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None


# ============================================================================
# Connection Models
# ============================================================================

class ConnectionResponse(BaseModel):
    """Response model for a connection."""
    person_id: str
    name: str
    company: Optional[str] = None
    relationship_type: str
    shared_events_count: int = 0
    shared_threads_count: int = 0
    shared_messages_count: int = 0
    shared_whatsapp_count: int = 0
    shared_slack_count: int = 0
    shared_phone_calls_count: int = 0
    shared_contexts: list[str] = []
    relationship_strength: float = 0.0
    last_seen_together: Optional[str] = None


class ConnectionsResponse(BaseModel):
    """Response for connections endpoint."""
    connections: list[ConnectionResponse]
    count: int


class SuggestedConnectionResponse(BaseModel):
    """Response for a suggested connection."""
    person_id: str
    name: str
    company: Optional[str] = None
    score: float = 0.0
    shared_contexts: list[str] = []
    shared_sources: list[str] = []


class DiscoverResponse(BaseModel):
    """Response for discover endpoint."""
    suggestions: list[SuggestedConnectionResponse]
    count: int


# ============================================================================
# Network Graph Models
# ============================================================================

class NetworkNode(BaseModel):
    """A node in the network graph."""
    id: str
    name: str
    category: str = "unknown"
    strength: float = 0.0
    interaction_count: int = 0
    degree: int = 1


class NetworkEdge(BaseModel):
    """An edge in the network graph."""
    source: str
    target: str
    weight: int = 0
    type: str = "inferred"
    shared_events_count: int = 0
    shared_threads_count: int = 0
    shared_messages_count: int = 0
    shared_whatsapp_count: int = 0
    shared_slack_count: int = 0
    shared_phone_calls_count: int = 0
    is_linkedin_connection: bool = False


class NetworkGraphResponse(BaseModel):
    """Response for network graph endpoint."""
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]


# ============================================================================
# Facts Models
# ============================================================================

class PersonFactResponse(BaseModel):
    """Response model for a person fact."""
    id: str
    person_id: str
    category: str
    key: str
    value: str
    confidence: float = 0.5
    source_interaction_id: Optional[str] = None
    source_quote: Optional[str] = None
    source_link: Optional[str] = None
    extracted_at: Optional[str] = None
    confirmed_by_user: bool = False
    created_at: Optional[str] = None
    category_icon: str = ""


class PersonFactsResponse(BaseModel):
    """Response for person facts list."""
    facts: list[PersonFactResponse]
    count: int
    by_category: dict[str, list[PersonFactResponse]] = {}


class FactUpdateRequest(BaseModel):
    """Request for updating a fact."""
    value: Optional[str] = None
    confidence: Optional[float] = None
    category: Optional[str] = None
    key: Optional[str] = None


class FactExtractionResponse(BaseModel):
    """Response for fact extraction."""
    status: str
    extracted_count: int
    facts: list[PersonFactResponse] = []


# ============================================================================
# Statistics Models
# ============================================================================

class StatisticsResponse(BaseModel):
    """Response for CRM statistics."""
    total_people: int = 0
    by_category: dict[str, int] = {}
    by_source: dict[str, int] = {}
    total_source_entities: int = 0
    linked_entities: int = 0
    unlinked_entities: int = 0
    total_relationships: int = 0


# ============================================================================
# Me Dashboard Models
# ============================================================================

class MeStatsResponse(BaseModel):
    """Aggregate statistics for the owner's dashboard."""
    total_people: int
    total_emails: int
    total_meetings: int
    total_messages: int


class DailyAggregate(BaseModel):
    """Aggregated interactions for a single day."""
    date: str
    total: int
    sources: dict[str, int]


class TopContact(BaseModel):
    """A top contact with interaction count."""
    person_id: str
    person_name: str
    count: int


class TrendPerson(BaseModel):
    """Person with trend data (recent vs previous period)."""
    person_id: str
    person_name: str
    recent_count: int
    previous_count: int


class NeglectedContact(BaseModel):
    """A contact that hasn't been reached out to recently."""
    person_id: str
    person_name: str
    days_since_contact: int
    typical_gap_days: int
    dunbar_circle: int


class MonthlyNetworkGrowth(BaseModel):
    """Network growth for a single month."""
    month: str
    new_people: int
    cumulative_total: int


class MonthlyMessagingVolume(BaseModel):
    """Messaging volume by Dunbar circle for a single month."""
    month: str
    total: int
    by_circle: dict[str, int]
    circle_percentages: dict[str, float]


class HealthScorePoint(BaseModel):
    """Health score at a point in time."""
    date: str
    score: int
    count: int = 0


class TrackedRelationshipPoint(BaseModel):
    """Score at a point in time for tracked relationships."""
    date: str
    score: int
    count: int = 0


class TrackedRelationship(BaseModel):
    """Tracked relationship metrics for specific people."""
    name: str
    person_ids: list[str]
    person_names: list[str]
    current_score: int
    trend: str
    history: list[TrackedRelationshipPoint]
    healthy_direction: str
    average: float = 0.0


class MeInteractionsResponse(BaseModel):
    """Aggregated interaction data for the owner's dashboard."""
    daily: list[DailyAggregate]
    by_source: dict[str, int]
    by_month: dict[str, int]
    by_circle: dict[str, int]
    top_contacts: list[TopContact]
    warming: list[TrendPerson]
    cooling: list[TrendPerson]
    total_count: int
    relationship_health_score: int = 0
    health_score_history: list[HealthScorePoint] = []
    health_score_average: float = 0.0
    neglected_contacts: list[NeglectedContact] = []
    network_growth: list[MonthlyNetworkGrowth] = []
    messaging_by_circle: list[MonthlyMessagingVolume] = []
    tracked_relationships: list[TrackedRelationship] = []


# ============================================================================
# Family Dashboard Models
# ============================================================================

class FamilyMember(BaseModel):
    """Family member for multi-select dropdown and visualizations."""
    id: str
    name: str
    relationship_strength: float = 0.0
    dunbar_circle: Optional[int] = None
    last_seen: Optional[str] = None
    by_source: dict[str, int] = {}
    current_streak: int = 0


class FamilyMembersResponse(BaseModel):
    """List of family members for selection."""
    members: list[FamilyMember]


class FamilyStatsResponse(BaseModel):
    """Lifetime totals for selected family members."""
    total_emails: int
    total_meetings: int
    total_messages: int


class FamilyInteractionsResponse(BaseModel):
    """Aggregated interaction data for selected family members."""
    selected_ids: list[str]
    selected_names: list[str]
    daily: list[DailyAggregate]
    by_source: dict[str, int]
    by_month: dict[str, int]
    top_contacts: list[TopContact]
    warming: list[TrendPerson]
    cooling: list[TrendPerson]
    total_count: int
    relationship_health_score: int = 0
    health_score_history: list[HealthScorePoint] = []
    health_score_average: float = 0.0
    neglected_contacts: list[NeglectedContact] = []
    network_growth: list[MonthlyNetworkGrowth] = []
    messaging_by_circle: list[MonthlyMessagingVolume] = []
    tracked_relationships: list[TrackedRelationship] = []


class CommunicationGap(BaseModel):
    """A period of no contact with a family member."""
    person_id: str
    person_name: str
    start_date: str
    end_date: str
    gap_days: int
    avg_gap_days_before: Optional[float] = None


class CommunicationGapsResponse(BaseModel):
    """Communication gaps for family members over time."""
    gaps: list[CommunicationGap]
    person_summaries: list[dict]


class ChannelMixMember(BaseModel):
    """Channel mix data for a single family member."""
    id: str
    name: str
    by_source: dict[str, int] = {}


class ChannelMixResponse(BaseModel):
    """Response for channel mix by family members."""
    members: list[ChannelMixMember]


# ============================================================================
# Sync Health Models
# ============================================================================

class SyncHealthResponse(BaseModel):
    """Response for a single sync source health."""
    source: str
    description: str
    last_sync: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    is_stale: bool = True
    hours_since_sync: Optional[float] = None
    expected_frequency: str = "daily"


class SyncHealthSummaryResponse(BaseModel):
    """Response for overall sync health."""
    total_sources: int
    healthy: int
    stale: int
    failed: int
    never_run: int
    stale_sources: list[str] = []
    failed_sources: list[str] = []
    never_run_sources: list[str] = []
    all_healthy: bool


class SyncErrorResponse(BaseModel):
    """Response for a sync error."""
    id: int
    source: str
    timestamp: str
    error_type: Optional[str] = None
    error_message: str
    context: Optional[str] = None


# ============================================================================
# Review Queue Models
# ============================================================================

class ReviewQueueItem(BaseModel):
    """An item in the low-confidence match review queue."""
    id: str
    source_entity_id: str
    source_type: str
    observed_name: Optional[str] = None
    observed_email: Optional[str] = None
    observed_phone: Optional[str] = None
    proposed_person_id: str
    proposed_person_name: str
    confidence: float
    reason: str
    created_at: Optional[str] = None


class ReviewQueueResponse(BaseModel):
    """Response for review queue."""
    items: list[ReviewQueueItem]
    count: int
    total_pending: int


# ============================================================================
# Link Override Models
# ============================================================================

class LinkOverrideResponse(BaseModel):
    """Response model for a link override rule."""
    id: str
    name_pattern: str
    source_type: Optional[str] = None
    context_pattern: Optional[str] = None
    preferred_person_id: str
    preferred_person_name: Optional[str] = None
    rejected_person_id: Optional[str] = None
    rejected_person_name: Optional[str] = None
    reason: Optional[str] = None
    created_at: Optional[str] = None


# ============================================================================
# Relationship Insights Models
# ============================================================================

class RelationshipInsightResponse(BaseModel):
    """Response model for a relationship insight."""
    id: str
    person_id: str
    category: str
    text: str
    source_title: Optional[str] = None
    source_link: Optional[str] = None
    source_date: Optional[str] = None
    confirmed: bool = False
    created_at: Optional[str] = None
    category_icon: str = ""


class RelationshipInsightsResponse(BaseModel):
    """Response for relationship insights endpoint."""
    insights: list[RelationshipInsightResponse]
    last_generated: Optional[str] = None
    confirmed_count: int = 0
    unconfirmed_count: int = 0


class ToneDataPoint(BaseModel):
    """A single month's tone data."""
    month: str
    tone: str
    score: float
    sample_count: int = 0


class ToneAnalysisResponse(BaseModel):
    """Response for tone analysis endpoint."""
    monthly_tones: list[ToneDataPoint]
    trend: str
    generated_at: str
