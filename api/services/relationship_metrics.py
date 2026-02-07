"""
Relationship Metrics - Compute relationship strength scores.

Relationship strength is computed using the formula:
    strength = (recency × RECENCY_WEIGHT) + (frequency × FREQUENCY_WEIGHT) + (diversity × DIVERSITY_WEIGHT)

Where:
- recency: max(0, 1 - days_since_last/RECENCY_WINDOW_DAYS)
- frequency: min(1, weighted_interactions/FREQUENCY_TARGET)
- diversity: unique_sources / total_sources

Interaction weights are applied per source_type (e.g., imessage=1.5, gmail=0.8).
See config/relationship_weights.py for all weights.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.services.person_entity import PersonEntity, get_person_entity_store, compute_person_category
from api.services.interaction_store import get_interaction_store
from api.services.source_entity import get_source_entity_store, SOURCE_TYPES

# Import weights from centralized config
from config.relationship_weights import (
    RECENCY_WEIGHT,
    FREQUENCY_WEIGHT,
    DIVERSITY_WEIGHT,
    RECENCY_WINDOW_DAYS,
    FREQUENCY_TARGET,
    FREQUENCY_WINDOW_DAYS,
    get_interaction_weight,
    compute_weighted_interaction_count,
    compute_weighted_interaction_count_detailed,
    INTERACTION_TYPE_WEIGHTS,
    USE_LOG_FREQUENCY_SCALING,
    LIFETIME_FREQUENCY_ENABLED,
    LIFETIME_FREQUENCY_WEIGHT,
    RECENT_FREQUENCY_WEIGHT,
    LIFETIME_FREQUENCY_TARGET,
    MIN_INTERACTIONS_FOR_FULL_RECENCY,
    ZERO_INTERACTION_RECENCY_MULTIPLIER,
    PERIPHERAL_THRESHOLD,
    STRENGTH_OVERRIDES_BY_ID,
    CIRCLE_OVERRIDES_BY_ID,
    TAG_OVERRIDES_BY_ID,
)
import math

logger = logging.getLogger(__name__)


def compute_recency_score(last_seen: Optional[datetime]) -> float:
    """
    Compute recency score (0.0-1.0).

    Score is 1.0 if last interaction was today, decreasing linearly
    to 0.0 at RECENCY_WINDOW_DAYS days ago.

    Args:
        last_seen: Last interaction timestamp

    Returns:
        Recency score between 0.0 and 1.0
    """
    if last_seen is None:
        return 0.0

    now = datetime.now(timezone.utc)

    # Ensure last_seen is timezone-aware
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    # Cap future dates at today (e.g., from future calendar events)
    if last_seen > now:
        last_seen = now

    days_since = (now - last_seen).days

    return max(0.0, 1.0 - (days_since / RECENCY_WINDOW_DAYS))


def compute_frequency_score(interaction_count: float) -> float:
    """
    Compute frequency score (0.0-1.0).

    With linear scaling: Score increases linearly from 0 to 1.0 as interactions
    approach FREQUENCY_TARGET.

    With logarithmic scaling (USE_LOG_FREQUENCY_SCALING=True): Uses log scale
    to better differentiate between casual (10 interactions) and close (100+)
    contacts. Formula: log(1 + count) / log(1 + target)

    Args:
        interaction_count: Number of interactions (can be weighted, so float)

    Returns:
        Frequency score between 0.0 and 1.0
    """
    if interaction_count <= 0:
        return 0.0

    if USE_LOG_FREQUENCY_SCALING:
        # Logarithmic scaling spreads out the distribution
        # log(1 + 10) / log(1 + 150) ≈ 0.48
        # log(1 + 50) / log(1 + 150) ≈ 0.78
        # log(1 + 100) / log(1 + 150) ≈ 0.92
        # log(1 + 150) / log(1 + 150) = 1.0
        return min(1.0, math.log(1 + interaction_count) / math.log(1 + FREQUENCY_TARGET))
    else:
        return min(1.0, interaction_count / FREQUENCY_TARGET)


def compute_weighted_frequency_score(interactions_by_type: dict[str, int]) -> float:
    """
    Compute frequency score with interaction type weighting.

    Different interaction types are weighted differently:
    - imessage/whatsapp: 1.5 (direct personal contact)
    - phone_call: 2.0 (high effort synchronous)
    - slack: 1.2 (work DM)
    - calendar: 1.0 (meetings)
    - gmail: 0.8 (often passive/CC)
    - vault: 0.7 (mentioned in notes)

    Args:
        interactions_by_type: Dict mapping source_type to count

    Returns:
        Frequency score between 0.0 and 1.0
    """
    weighted_count = compute_weighted_interaction_count(interactions_by_type)
    return compute_frequency_score(weighted_count)


def compute_hybrid_frequency_score(
    recent_interactions: dict[str, int] | list[dict],
    lifetime_interactions: dict[str, int] | list[dict],
) -> float:
    """
    Compute frequency score combining recent and lifetime interactions.

    Formula: (recent_score * RECENT_WEIGHT) + (lifetime_score * LIFETIME_WEIGHT)

    This ensures historical relationships don't completely vanish while
    still prioritizing recent activity.

    Args:
        recent_interactions: Interactions within FREQUENCY_WINDOW_DAYS.
            Can be dict[source_type -> count] for simple weighting,
            or list[dict] with subtype/source_account for detailed weighting.
        lifetime_interactions: All-time interactions (same format as recent)

    Returns:
        Frequency score between 0.0 and 1.0
    """
    if not LIFETIME_FREQUENCY_ENABLED:
        return compute_weighted_frequency_score(recent_interactions)

    # Recent frequency score (uses FREQUENCY_TARGET)
    # Check if using detailed format (list of dicts) or simple format (dict)
    if isinstance(recent_interactions, list):
        recent_weighted = compute_weighted_interaction_count_detailed(recent_interactions)
    else:
        recent_weighted = compute_weighted_interaction_count(recent_interactions)

    if USE_LOG_FREQUENCY_SCALING and recent_weighted > 0:
        recent_score = min(1.0, math.log(1 + recent_weighted) / math.log(1 + FREQUENCY_TARGET))
    else:
        recent_score = min(1.0, recent_weighted / FREQUENCY_TARGET) if recent_weighted > 0 else 0.0

    # Lifetime frequency score (uses higher LIFETIME_FREQUENCY_TARGET)
    if isinstance(lifetime_interactions, list):
        lifetime_weighted = compute_weighted_interaction_count_detailed(lifetime_interactions)
    else:
        lifetime_weighted = compute_weighted_interaction_count(lifetime_interactions)

    if USE_LOG_FREQUENCY_SCALING and lifetime_weighted > 0:
        lifetime_score = min(1.0, math.log(1 + lifetime_weighted) / math.log(1 + LIFETIME_FREQUENCY_TARGET))
    else:
        lifetime_score = min(1.0, lifetime_weighted / LIFETIME_FREQUENCY_TARGET) if lifetime_weighted > 0 else 0.0

    # Combine with weights
    return (recent_score * RECENT_FREQUENCY_WEIGHT) + (lifetime_score * LIFETIME_FREQUENCY_WEIGHT)


def compute_diversity_score(sources: list[str]) -> float:
    """
    Compute diversity score (0.0-1.0).

    Score is the ratio of unique sources used to total possible sources.

    Args:
        sources: List of source types used for interactions

    Returns:
        Diversity score between 0.0 and 1.0
    """
    if not sources:
        return 0.0

    unique_sources = len(set(sources))
    total_sources = len(SOURCE_TYPES)

    return min(1.0, unique_sources / total_sources)


def compute_relationship_strength(
    last_seen: Optional[datetime],
    interaction_count: float,  # Changed to float to support weighted counts
    sources: list[str],
) -> float:
    """
    Compute overall relationship strength score.

    Uses the formula:
        strength = (recency × RECENCY_WEIGHT) + (frequency × FREQUENCY_WEIGHT) + (diversity × DIVERSITY_WEIGHT)

    Args:
        last_seen: Last interaction timestamp
        interaction_count: Number of interactions in window (weighted or raw)
        sources: List of source types used

    Returns:
        Relationship strength between 0 and 100
    """
    recency = compute_recency_score(last_seen)
    frequency = compute_frequency_score(interaction_count)
    diversity = compute_diversity_score(sources)

    strength = (
        recency * RECENCY_WEIGHT +
        frequency * FREQUENCY_WEIGHT +
        diversity * DIVERSITY_WEIGHT
    )

    # Scale to 0-100 to match UI slider
    return round(strength * 100, 1)


def compute_relationship_strength_weighted(
    last_seen: Optional[datetime],
    interactions_by_type: dict[str, int],
    sources: list[str],
) -> float:
    """
    Compute relationship strength with interaction type weighting.

    This is the preferred method as it weights different interaction types.

    Args:
        last_seen: Last interaction timestamp
        interactions_by_type: Dict mapping source_type to count
        sources: List of source types used

    Returns:
        Relationship strength between 0 and 100
    """
    recency = compute_recency_score(last_seen)
    frequency = compute_weighted_frequency_score(interactions_by_type)
    diversity = compute_diversity_score(sources)

    strength = (
        recency * RECENCY_WEIGHT +
        frequency * FREQUENCY_WEIGHT +
        diversity * DIVERSITY_WEIGHT
    )

    # Scale to 0-100 to match UI slider
    return round(strength * 100, 1)


def compute_strength_for_person(person: PersonEntity) -> float:
    """
    Compute relationship strength for a PersonEntity.

    Fetches interaction data from stores and computes the score
    using weighted interaction counts by type and subtype. Uses hybrid frequency
    that combines recent and lifetime interactions. Adds small bonuses
    for LinkedIn connections and family members.

    Uses detailed subtype/account weighting when available:
    - Gmail: sent (1.2x), received (1.0x), CC (0.3x)
    - Calendar: 1on1 (6.0x), small group (4.0x), large meeting (2.0x)
    - Personal account: 2x gmail, 3x calendar

    Args:
        person: PersonEntity to compute strength for

    Returns:
        Relationship strength between 0 and 100
    """
    from api.services.relationship import get_relationship_store, TYPE_FAMILY
    from config.settings import settings

    interaction_store = get_interaction_store()

    # Get recent interaction counts with subtype detail (within frequency window)
    recent_interactions = interaction_store.get_interaction_counts_with_subtypes(
        person.id,
        days_back=FREQUENCY_WINDOW_DAYS,
    )

    # Get lifetime interaction counts with subtype detail (all-time, 10 years)
    lifetime_interactions = interaction_store.get_interaction_counts_with_subtypes(
        person.id,
        days_back=3650,  # 10 years
    )

    # Get source types from interactions
    sources = list({item["source_type"] for item in lifetime_interactions})

    # Also include sources from the person's source list
    sources.extend(person.sources)
    sources = list(set(sources))

    # Compute component scores
    recency_score = compute_recency_score(person.last_seen)
    frequency_score = compute_hybrid_frequency_score(recent_interactions, lifetime_interactions)
    diversity_score = compute_diversity_score(sources)

    # Apply recency discount for low/zero interaction contacts
    # Prevents contact syncs from inflating scores for people you've never actually interacted with
    total_interactions = sum(item["count"] for item in lifetime_interactions)
    if total_interactions < MIN_INTERACTIONS_FOR_FULL_RECENCY:
        if total_interactions == 0:
            recency_multiplier = ZERO_INTERACTION_RECENCY_MULTIPLIER
        else:
            # Linear interpolation: 0 interactions → 25%, MIN_INTERACTIONS → 100%
            recency_multiplier = ZERO_INTERACTION_RECENCY_MULTIPLIER + (
                (1.0 - ZERO_INTERACTION_RECENCY_MULTIPLIER) *
                (total_interactions / MIN_INTERACTIONS_FOR_FULL_RECENCY)
            )
        recency_score *= recency_multiplier

    # Combine into overall strength
    base_strength = (
        recency_score * RECENCY_WEIGHT +
        frequency_score * FREQUENCY_WEIGHT +
        diversity_score * DIVERSITY_WEIGHT
    ) * 100  # Scale to 0-100

    # Apply multiplier bonuses for LinkedIn connection and family (only for relationships with me)
    # Using multipliers instead of flat additions prevents low-strength contacts from being inflated
    multiplier = 1.0
    my_person_id = settings.my_person_id
    if my_person_id and person.id != my_person_id:
        rel_store = get_relationship_store()
        rel = rel_store.get_between(my_person_id, person.id)
        if rel:
            # LinkedIn connection bonus: 3% boost
            if rel.is_linkedin_connection:
                multiplier *= 1.03
            # Family bonus: 5% boost
            if rel.relationship_type == TYPE_FAMILY:
                multiplier *= 1.05

    # Cap at 100
    return min(100.0, round(base_strength * multiplier, 1))


def update_strength_for_person(person_id: str) -> Optional[float]:
    """
    Compute and update relationship strength for a person.

    Updates the PersonEntity with the new strength score and peripheral contact flag.
    Note: dunbar_circle is NOT updated here - it requires ranking all people
    and is only computed by update_all_strengths().

    Args:
        person_id: ID of the person to update

    Returns:
        New relationship strength, or None if person not found
    """
    store = get_person_entity_store()
    person = store.get_by_id(person_id)

    if not person:
        logger.warning(f"Person not found: {person_id}")
        return None

    strength = compute_strength_for_person(person)
    person.relationship_strength = strength
    person.is_peripheral_contact = strength < PERIPHERAL_THRESHOLD
    store.update(person)

    logger.debug(f"Updated relationship strength for {person.canonical_name}: {strength} (peripheral={person.is_peripheral_contact})")
    return strength


def update_all_strengths() -> dict:
    """
    Update relationship strength for all people.

    Also updates is_peripheral_contact and dunbar_circle for all people.

    Returns:
        Statistics about the update
    """
    store = get_person_entity_store()
    people = store.get_all()

    updated = 0
    failed = 0
    peripheral_count = 0

    for person in people:
        try:
            strength = compute_strength_for_person(person)
            # Apply manual override if defined
            override = STRENGTH_OVERRIDES_BY_ID.get(person.id)
            if override is not None:
                strength = override
            person.relationship_strength = strength
            person.is_peripheral_contact = strength < PERIPHERAL_THRESHOLD
            if person.is_peripheral_contact:
                person.dunbar_circle = 7  # Pre-assign peripheral contacts to circle 7
                peripheral_count += 1
            # Sync category from computed value (keeps stored value in sync with UI)
            person.category = compute_person_category(person)
            store.update(person)
            updated += 1
        except Exception as e:
            logger.error(f"Failed to update strength for {person.id}: {e}")
            failed += 1

    # Compute Dunbar circles for non-peripheral contacts
    circles_result = compute_all_dunbar_circles(store)

    # Apply tag overrides
    tags_result = apply_tag_overrides(store)

    # Save all updates
    store.save()

    logger.info(f"Updated relationship strength for {updated} people ({failed} failed, {peripheral_count} peripheral)")
    return {
        "updated": updated,
        "failed": failed,
        "total": len(people),
        "peripheral_count": peripheral_count,
        "circles_computed": circles_result.get("assigned", 0),
        "tags_applied": tags_result.get("applied", 0),
    }


def _get_effective_strength(person: PersonEntity) -> float:
    """Get relationship strength, applying manual overrides if defined (by ID)."""
    override = STRENGTH_OVERRIDES_BY_ID.get(person.id)
    if override is not None:
        return override
    return person.relationship_strength or 0


def compute_all_dunbar_circles(store=None) -> dict:
    """
    Compute Dunbar circles for all non-peripheral contacts.

    Circle 0 is RESERVED for manual overrides only (spouse, children, self).
    Other circles are assigned based on relationship_strength ranking:
    - Circle 0: Manual overrides only (CIRCLE_OVERRIDES_BY_ID)
    - Circle 1: Top 5 people (close friends/family)
    - Circle 2: Next 15 (good friends)
    - Circle 3: Next 50 (friends)
    - Circle 4: Next 150 (meaningful acquaintances)
    - Circle 5: Next 500 (acquaintances)
    - Circle 6: Next 1500 (recognizable)
    - Circle 7: Everyone else (peripheral, pre-assigned)

    Manual STRENGTH_OVERRIDES are respected when ranking people.

    Args:
        store: PersonEntityStore instance (optional, will get if not provided)

    Returns:
        Statistics about the assignment
    """
    if store is None:
        store = get_person_entity_store()

    # Get all non-peripheral contacts, sorted by effective strength descending
    # Effective strength = override if defined, else computed strength
    all_people = store.get_all(include_hidden=True)
    non_peripheral = [p for p in all_people if not p.is_peripheral_contact]
    non_peripheral.sort(key=_get_effective_strength, reverse=True)

    # Separate work category people - they don't count toward thresholds
    # but will still be assigned circles based on strength cutoffs
    non_work = [p for p in non_peripheral if p.category != "work"]
    work_people = [p for p in non_peripheral if p.category == "work"]

    # Sort non-work by strength (already sorted, but ensure it)
    non_work.sort(key=_get_effective_strength, reverse=True)

    # Dunbar circle thresholds (cumulative sizes) - starts at circle 1
    # Circle 0 is RESERVED for manual overrides only (e.g., spouse, children)
    # Circle 1: top 5, Circle 2: next 15, Circle 3: next 50, etc.
    # Thresholds are calculated based on non-work people only
    circle_thresholds = [5, 20, 70, 220, 720, 2220]  # 5, 15, 50, 150, 500, 1500

    assigned = 0
    ranking_index = 0  # Separate counter for non-override, non-work people

    # Track strength cutoffs for each circle (min strength to be in that circle)
    # Will be populated as we assign non-work people
    circle_strength_cutoffs = {}  # circle -> min strength

    # First pass: assign circles to non-work people (they define the thresholds)
    for person in non_work:
        # Check for manual circle override first
        if person.id in CIRCLE_OVERRIDES_BY_ID:
            circle = CIRCLE_OVERRIDES_BY_ID[person.id]
            # Override people don't count toward ranking thresholds
        else:
            # Find which circle this person belongs to based on their ranking
            # among non-override people (circle 0 overrides don't consume slots)
            circle = 6  # Default to circle 6 if beyond all thresholds
            for c, threshold in enumerate(circle_thresholds):
                if ranking_index < threshold:
                    circle = c + 1  # +1 because circle 0 is reserved
                    break
            ranking_index += 1

            # Record this person's strength as the cutoff for this circle
            # (last person in each circle defines the minimum strength)
            strength = _get_effective_strength(person)
            circle_strength_cutoffs[circle] = strength

        if person.dunbar_circle != circle:
            person.dunbar_circle = circle
            store.update(person)
        assigned += 1

    # Second pass: assign circles to work people based on strength cutoffs
    # Work people get the same circle as non-work people with similar strength
    for person in work_people:
        if person.id in CIRCLE_OVERRIDES_BY_ID:
            circle = CIRCLE_OVERRIDES_BY_ID[person.id]
        else:
            strength = _get_effective_strength(person)
            # Find which circle this strength qualifies for
            circle = 6  # Default
            for c in range(1, 7):
                cutoff = circle_strength_cutoffs.get(c)
                if cutoff is not None and strength >= cutoff:
                    circle = c
                    break

        if person.dunbar_circle != circle:
            person.dunbar_circle = circle
            store.update(person)
        assigned += 1

    logger.info(f"Computed Dunbar circles for {assigned} non-peripheral contacts")
    return {
        "assigned": assigned,
        "total_non_peripheral": len(non_peripheral),
    }


def apply_tag_overrides(store=None) -> dict:
    """
    Apply tag overrides from TAG_OVERRIDES_BY_ID to all matching people.

    This ensures tags defined in config/relationship_weights.py are applied
    and persist across syncs. Override tags are ADDED to existing tags (merged),
    preserving any user-added tags.

    Args:
        store: PersonEntityStore instance (optional, will get if not provided)

    Returns:
        Statistics about the application
    """
    if store is None:
        store = get_person_entity_store()

    applied = 0
    not_found = 0

    for person_id, override_tags in TAG_OVERRIDES_BY_ID.items():
        person = store.get_by_id(person_id)
        if person:
            # Merge: existing tags + any missing override tags
            existing_set = set(person.tags)
            override_set = set(override_tags)
            if not override_set.issubset(existing_set):
                # Add missing override tags while preserving existing ones
                merged_tags = list(existing_set | override_set)
                person.tags = merged_tags
                store.update(person)
                applied += 1
        else:
            not_found += 1

    if applied > 0:
        logger.info(f"Applied tag overrides to {applied} people ({not_found} not found)")

    return {
        "applied": applied,
        "not_found": not_found,
        "total_overrides": len(TAG_OVERRIDES_BY_ID),
    }


def get_strength_breakdown(person: PersonEntity) -> dict:
    """
    Get detailed breakdown of relationship strength components.

    Useful for debugging and displaying in UI.

    Args:
        person: PersonEntity to analyze

    Returns:
        Dict with component scores and details
    """
    interaction_store = get_interaction_store()

    # Get interaction counts with subtype detail for weighted calculation
    interactions_detailed = interaction_store.get_interaction_counts_with_subtypes(
        person.id,
        days_back=FREQUENCY_WINDOW_DAYS,
    )

    # Also get simple counts for backward compatibility
    interactions_by_type = interaction_store.get_interaction_counts(
        person.id,
        days_back=FREQUENCY_WINDOW_DAYS,
    )

    sources = list(interactions_by_type.keys())
    sources.extend(person.sources)
    sources = list(set(sources))

    # Calculate raw and weighted counts (using detailed data)
    raw_interaction_count = sum(item["count"] for item in interactions_detailed)
    weighted_interaction_count = compute_weighted_interaction_count_detailed(interactions_detailed)

    recency_score = compute_recency_score(person.last_seen)
    # Use detailed data for frequency score
    frequency_score = compute_frequency_score(weighted_interaction_count)
    diversity_score = compute_diversity_score(sources)

    days_since_last = None
    if person.last_seen:
        now = datetime.now(timezone.utc)
        last_seen = person.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        days_since_last = (now - last_seen).days

    # Build weighted breakdown with subtype detail
    interaction_weights_detail = {}
    for item in interactions_detailed:
        source_type = item["source_type"]
        subtype = item.get("subtype")
        source_account = item.get("source_account")
        count = item["count"]

        # Use subtype as key if available, otherwise source_type
        key = subtype if subtype else source_type
        if source_account:
            key = f"{key}:{source_account}"

        weight = get_interaction_weight(source_type, subtype, source_account)
        interaction_weights_detail[key] = {
            "count": count,
            "weight": weight,
            "weighted_count": round(count * weight, 2),
            "source_type": source_type,
            "subtype": subtype,
            "source_account": source_account,
        }

    return {
        "overall_strength": person.relationship_strength or compute_strength_for_person(person),
        "recency": {
            "score": recency_score,
            "weight": RECENCY_WEIGHT,
            "weighted_score": round(recency_score * RECENCY_WEIGHT, 4),
            "last_seen": person.last_seen.isoformat() if person.last_seen else None,
            "days_since_last": days_since_last,
            "window_days": RECENCY_WINDOW_DAYS,
        },
        "frequency": {
            "score": frequency_score,
            "weight": FREQUENCY_WEIGHT,
            "weighted_score": round(frequency_score * FREQUENCY_WEIGHT, 4),
            "raw_interaction_count": raw_interaction_count,
            "weighted_interaction_count": round(weighted_interaction_count, 2),
            "target": FREQUENCY_TARGET,
            "interactions_by_type": interaction_weights_detail,
        },
        "diversity": {
            "score": diversity_score,
            "weight": DIVERSITY_WEIGHT,
            "weighted_score": round(diversity_score * DIVERSITY_WEIGHT, 4),
            "sources_used": sources,
            "total_sources": len(SOURCE_TYPES),
        },
    }
