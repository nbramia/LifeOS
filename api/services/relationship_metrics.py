"""
Relationship Metrics - Compute relationship strength scores.

Relationship strength is computed using the formula:
    strength = 100 * (recency * RECENCY_WEIGHT
                      + recent_frequency * RECENT_FREQUENCY_WEIGHT
                      + lifetime_frequency * LIFETIME_FREQUENCY_WEIGHT
                      + diversity * DIVERSITY_WEIGHT)

Where:
- recency: 0.5 ** (days_since_last_real_interaction / RECENCY_HALF_LIFE_DAYS),
  discounted for people with few interactions
- recent_frequency: min(1, log(1 + S) / log(1 + RECENT_FREQUENCY_TARGET)), with
  S = sum of weight * 0.5 ** (age_days / RECENT_FREQUENCY_HALF_LIFE_DAYS)
- lifetime_frequency: min(1, log(1 + W) / log(1 + LIFETIME_FREQUENCY_TARGET)),
  with W = sum of weight over LIFETIME_WINDOW_DAYS
- diversity: unique_sources / total_sources

Family and LinkedIn multipliers apply to the result, capped at 100.

Every interaction-derived input comes from the interactions table and ignores
future-dated rows. PersonEntity.last_seen is not an input: it also tracks source
entity observations, which syncs refresh without any interaction taking place.

Interaction weights depend on source type, subtype, and account.
See config/relationship_weights.py for all weights.
"""
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from api.services.person_entity import PersonEntity, get_person_entity_store, compute_person_category
from api.services.interaction_store import StrengthInputs, get_interaction_store
from api.services.source_entity import SOURCE_TYPES

# Import weights from centralized config
from config.relationship_weights import (
    RECENCY_WEIGHT,
    RECENT_FREQUENCY_WEIGHT,
    LIFETIME_FREQUENCY_WEIGHT,
    DIVERSITY_WEIGHT,
    RECENCY_HALF_LIFE_DAYS,
    RECENT_FREQUENCY_HALF_LIFE_DAYS,
    RECENT_FREQUENCY_TARGET,
    LIFETIME_FREQUENCY_TARGET,
    LIFETIME_WINDOW_DAYS,
    get_interaction_weight,
    MIN_INTERACTIONS_FOR_FULL_RECENCY,
    ZERO_INTERACTION_RECENCY_MULTIPLIER,
    PERIPHERAL_THRESHOLD,
    STRENGTH_OVERRIDES_BY_ID,
    CIRCLE_OVERRIDES_BY_ID,
    TAG_OVERRIDES_BY_ID,
)

logger = logging.getLogger(__name__)


def compute_recency_score(
    last_interaction: Optional[datetime],
    now: Optional[datetime] = None,
) -> float:
    """
    Compute recency score (0.0-1.0).

    Halves every RECENCY_HALF_LIFE_DAYS days since the last real interaction.

    Args:
        last_interaction: Timestamp of the last past interaction
        now: Current time (default: now)

    Returns:
        Recency score between 0.0 and 1.0
    """
    if last_interaction is None:
        return 0.0

    now = now or datetime.now(timezone.utc)

    # Ensure the timestamp is timezone-aware
    if last_interaction.tzinfo is None:
        last_interaction = last_interaction.replace(tzinfo=timezone.utc)

    days_since = max(0.0, (now - last_interaction).total_seconds() / 86400)

    return 0.5 ** (days_since / RECENCY_HALF_LIFE_DAYS)


def compute_recency_multiplier(total_interactions: int) -> float:
    """
    Discount recency for people with few interactions.

    Scales linearly up to full credit at MIN_INTERACTIONS_FOR_FULL_RECENCY, so a
    single interaction doesn't earn a whole relationship's worth of recency.

    Args:
        total_interactions: Past interactions within LIFETIME_WINDOW_DAYS

    Returns:
        Multiplier between ZERO_INTERACTION_RECENCY_MULTIPLIER and 1.0
    """
    if total_interactions >= MIN_INTERACTIONS_FOR_FULL_RECENCY:
        return 1.0
    if total_interactions <= 0:
        return ZERO_INTERACTION_RECENCY_MULTIPLIER
    return ZERO_INTERACTION_RECENCY_MULTIPLIER + (
        (1.0 - ZERO_INTERACTION_RECENCY_MULTIPLIER) *
        (total_interactions / MIN_INTERACTIONS_FOR_FULL_RECENCY)
    )


def _log_scaled(value: float, target: float) -> float:
    """Scale value onto 0.0-1.0 logarithmically, reaching 1.0 at target."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log(1 + value) / math.log(1 + target))


def compute_recent_frequency_score(decayed_weight: float) -> float:
    """
    Compute recent frequency score (0.0-1.0).

    Args:
        decayed_weight: Weighted interactions, each decayed by its age

    Returns:
        Log-scaled score, reaching 1.0 at RECENT_FREQUENCY_TARGET
    """
    return _log_scaled(decayed_weight, RECENT_FREQUENCY_TARGET)


def compute_lifetime_frequency_score(weighted_count: float) -> float:
    """
    Compute lifetime frequency score (0.0-1.0).

    Args:
        weighted_count: Weighted interactions within LIFETIME_WINDOW_DAYS

    Returns:
        Log-scaled score, reaching 1.0 at LIFETIME_FREQUENCY_TARGET
    """
    return _log_scaled(weighted_count, LIFETIME_FREQUENCY_TARGET)


def compute_diversity_score(sources: list[str]) -> float:
    """
    Compute diversity score (0.0-1.0).

    Score is the ratio of unique sources relative to total possible sources.

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
    recency: float,
    recent_frequency: float,
    lifetime_frequency: float,
    diversity: float,
) -> float:
    """
    Combine component scores into a strength before any multipliers.

    Args:
        recency: Recency score (0.0-1.0)
        recent_frequency: Recent frequency score (0.0-1.0)
        lifetime_frequency: Lifetime frequency score (0.0-1.0)
        diversity: Diversity score (0.0-1.0)

    Returns:
        Relationship strength between 0 and 100
    """
    return 100 * (
        recency * RECENCY_WEIGHT +
        recent_frequency * RECENT_FREQUENCY_WEIGHT +
        lifetime_frequency * LIFETIME_FREQUENCY_WEIGHT +
        diversity * DIVERSITY_WEIGHT
    )


@dataclass(frozen=True)
class StrengthComponents:
    """One person's component scores and the measurements behind them."""

    recency: float
    recent_frequency: float
    lifetime_frequency: float
    diversity: float
    last_interaction: Optional[datetime]
    decayed_weight: float
    lifetime_weight: float
    total_interactions: int
    sources: list[str]


def compute_strength_components(
    person: PersonEntity,
    inputs: StrengthInputs,
    now: datetime,
) -> StrengthComponents:
    """
    Turn a person's aggregated interactions into component scores.

    Args:
        person: The person being scored (contributes their source list)
        inputs: Their aggregated interactions
        now: Current time, the origin for every age

    Returns:
        Component scores and the measurements behind them
    """
    today = now.astimezone(timezone.utc).date()
    decayed_weight = 0.0
    lifetime_weight = 0.0
    total_interactions = 0
    sources = set(person.sources)

    for bucket in inputs.buckets:
        weight = bucket.count * get_interaction_weight(
            bucket.source_type, bucket.subtype, bucket.source_account
        )
        age_days = (today - date.fromisoformat(bucket.day)).days
        decayed_weight += weight * 0.5 ** (age_days / RECENT_FREQUENCY_HALF_LIFE_DAYS)
        lifetime_weight += weight
        total_interactions += bucket.count
        sources.add(bucket.source_type)

    return StrengthComponents(
        recency=(
            compute_recency_score(inputs.last_interaction, now)
            * compute_recency_multiplier(total_interactions)
        ),
        recent_frequency=compute_recent_frequency_score(decayed_weight),
        lifetime_frequency=compute_lifetime_frequency_score(lifetime_weight),
        diversity=compute_diversity_score(list(sources)),
        last_interaction=inputs.last_interaction,
        decayed_weight=decayed_weight,
        lifetime_weight=lifetime_weight,
        total_interactions=total_interactions,
        sources=sorted(sources),
    )


# Relationship strength for the CRM owner. Fixed rather than computed -- see
# the short-circuit in compute_strength_for_person.
SELF_STRENGTH = 100.0


def _get_person_inputs(person: PersonEntity, now: datetime) -> StrengthInputs:
    """Aggregate one person's interactions for scoring."""
    inputs = get_interaction_store().get_strength_inputs(
        person.id, days_back=LIFETIME_WINDOW_DAYS, now=now
    )
    return inputs.get(person.id, StrengthInputs())


def _compute_relationship_multiplier(person: PersonEntity) -> float:
    """
    Multiplier for a LinkedIn connection or family member (relationships with me).

    Multipliers rather than flat additions keep low-strength contacts from being
    inflated.
    """
    from api.services.relationship import get_relationship_store, TYPE_FAMILY
    from config.settings import settings

    multiplier = 1.0
    my_person_id = settings.my_person_id
    if my_person_id and person.id != my_person_id:
        rel = get_relationship_store().get_between(my_person_id, person.id)
        if rel:
            # LinkedIn connection bonus: 3% boost
            if rel.is_linkedin_connection:
                multiplier *= 1.03
            # Family bonus: 5% boost
            if rel.relationship_type == TYPE_FAMILY:
                multiplier *= 1.05
    return multiplier


def compute_strength_for_person(
    person: PersonEntity,
    inputs: Optional[StrengthInputs] = None,
    now: Optional[datetime] = None,
) -> float:
    """
    Compute relationship strength for a PersonEntity.

    Scores the person's interactions (see the module docstring for the formula)
    with subtype and account weighting, then applies the LinkedIn and family
    multipliers:
    - Gmail: sent (1.2x), received (1.0x), CC (0.3x)
    - Calendar: 1on1 (6.0x), small group (4.0x), large meeting (2.0x)
    - WhatsApp: direct (1.5x), group received (0.525x), group sent (0.225x)
    - Personal account: 2x gmail, 3x calendar

    Args:
        person: PersonEntity to compute strength for
        inputs: Their aggregated interactions, when the caller already has them
            (default: fetched from the interaction store)
        now: Current time (default: now)

    Returns:
        Relationship strength between 0 and 100
    """
    from config.settings import settings

    # The CRM owner is not a relationship to be scored. Their strength would
    # otherwise be computed from interactions *with themselves* -- whatever
    # happens to be self-addressed mail, self-invited calendar events, notes
    # they appear in -- which produced a nonsensical 94.3 on Taylor's install,
    # ranking her below her own partner in her own CRM. Anchor it at the
    # maximum so "me" always sorts first and never drifts with mailbox noise.
    if settings.my_person_id and person.id == settings.my_person_id:
        return SELF_STRENGTH

    now = now or datetime.now(timezone.utc)
    if inputs is None:
        inputs = _get_person_inputs(person, now)

    components = compute_strength_components(person, inputs, now)
    base_strength = compute_relationship_strength(
        components.recency,
        components.recent_frequency,
        components.lifetime_frequency,
        components.diversity,
    )

    # Cap at 100
    return min(100.0, round(base_strength * _compute_relationship_multiplier(person), 1))


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

    # One aggregate over the whole table, shared by every person in the loop.
    now = datetime.now(timezone.utc)
    all_inputs = get_interaction_store().get_strength_inputs(
        days_back=LIFETIME_WINDOW_DAYS, now=now
    )

    for cached_person in people:
        try:
            # Deep-copy rather than store.get_by_id(): every iteration ends in
            # an unconditional store.update(), so a private copy of the
            # cached entity gives the same mutation-safety without an extra
            # per-person SQL round trip across this full-list, sync-scale loop.
            person = PersonEntity.from_dict(cached_person.to_dict())
            strength = compute_strength_for_person(
                person, all_inputs.get(person.id, StrengthInputs()), now
            )
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
    # Hidden people don't rank: update_all_strengths doesn't rescore them, so their
    # stored strength can be arbitrarily stale and must not take a slot.
    all_people = store.get_all()
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
            updated_person = store.get_by_id(person.id)
            if updated_person:
                updated_person.dunbar_circle = circle
                store.update(updated_person)
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
            updated_person = store.get_by_id(person.id)
            if updated_person:
                updated_person.dunbar_circle = circle
                store.update(updated_person)
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
    now = datetime.now(timezone.utc)
    components = compute_strength_components(person, _get_person_inputs(person, now), now)

    # Lifetime counts by weight, with subtype detail
    interactions_detailed = get_interaction_store().get_interaction_counts_with_subtypes(
        person.id,
        days_back=LIFETIME_WINDOW_DAYS,
    )
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

    last_interaction = components.last_interaction
    return {
        "overall_strength": person.relationship_strength or compute_strength_for_person(person),
        "multiplier": _compute_relationship_multiplier(person),
        "recency": {
            "score": components.recency,
            "weight": RECENCY_WEIGHT,
            "weighted_score": round(components.recency * RECENCY_WEIGHT, 4),
            "last_interaction": last_interaction.isoformat() if last_interaction else None,
            "days_since_last": (now - last_interaction).days if last_interaction else None,
            "half_life_days": RECENCY_HALF_LIFE_DAYS,
        },
        "recent_frequency": {
            "score": components.recent_frequency,
            "weight": RECENT_FREQUENCY_WEIGHT,
            "weighted_score": round(components.recent_frequency * RECENT_FREQUENCY_WEIGHT, 4),
            "decayed_weight": round(components.decayed_weight, 2),
            "half_life_days": RECENT_FREQUENCY_HALF_LIFE_DAYS,
            "target": RECENT_FREQUENCY_TARGET,
        },
        "lifetime_frequency": {
            "score": components.lifetime_frequency,
            "weight": LIFETIME_FREQUENCY_WEIGHT,
            "weighted_score": round(components.lifetime_frequency * LIFETIME_FREQUENCY_WEIGHT, 4),
            "raw_interaction_count": components.total_interactions,
            "weighted_interaction_count": round(components.lifetime_weight, 2),
            "window_days": LIFETIME_WINDOW_DAYS,
            "target": LIFETIME_FREQUENCY_TARGET,
            "interactions_by_type": interaction_weights_detail,
        },
        "diversity": {
            "score": components.diversity,
            "weight": DIVERSITY_WEIGHT,
            "weighted_score": round(components.diversity * DIVERSITY_WEIGHT, 4),
            "sources_used": components.sources,
            "total_sources": len(SOURCE_TYPES),
        },
    }
