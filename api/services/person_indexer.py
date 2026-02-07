"""
Person Profile Indexer - Index CRM person data to vector store.

Generates searchable documents for people with relationship context,
enabling semantic searches like "my important contacts" or "people I work with".

Key design principle: Only index significant contacts (relationship_strength >= 15
or non-hidden) with at least 3 interactions in last 90 days.
"""
import json
import logging
from typing import Optional

from api.services.vectorstore import VectorStore
from api.services.person_entity import PersonEntity, get_person_entity_store
from api.services.relationship_summary import get_relationship_summary, RelationshipSummary
from api.services.person_facts import get_person_fact_store

logger = logging.getLogger(__name__)

# Collection name for person profiles (separate from vault documents)
PERSON_COLLECTION = "lifeos_people"

# Thresholds for indexing
MIN_STRENGTH_FOR_INDEX = 15  # Minimum relationship strength (0-100)
MIN_INTERACTIONS_FOR_INDEX = 3  # Minimum interactions in 90 days


def generate_person_document(person: PersonEntity, summary: RelationshipSummary) -> str:
    """
    Generate a searchable document for a person.

    The document is structured to be semantically searchable, with
    natural language descriptions of the relationship.

    Args:
        person: PersonEntity to generate document for
        summary: RelationshipSummary with channel activity data

    Returns:
        Markdown-formatted document string
    """
    parts = [
        f"# {person.display_name or person.canonical_name}",
    ]

    # Relationship strength description
    strength = summary.relationship_strength
    if strength >= 80:
        parts.append(f"Very strong relationship (strength: {strength}/100)")
    elif strength >= 60:
        parts.append(f"Strong relationship (strength: {strength}/100)")
    elif strength >= 40:
        parts.append(f"Moderate relationship (strength: {strength}/100)")
    elif strength >= 20:
        parts.append(f"Light relationship (strength: {strength}/100)")
    else:
        parts.append(f"Distant relationship (strength: {strength}/100)")

    # Category
    category_desc = {
        "work": "Work contact",
        "personal": "Personal contact",
        "family": "Family member",
    }
    parts.append(f"Category: {category_desc.get(person.category, person.category)}")

    # Company/position
    if person.company:
        if person.position:
            parts.append(f"Works at {person.company} as {person.position}")
        else:
            parts.append(f"Works at {person.company}")

    # Communication channels
    if summary.channels:
        parts.append("\n## Communication Channels")
        for ch in summary.channels:
            status = "recently active" if ch.is_recent else "dormant"
            if ch.count_90d > 0:
                parts.append(f"- {ch.source_type}: {ch.count_90d} interactions in 90 days ({status})")

        if summary.primary_channel:
            parts.append(f"\nPrimary communication channel: {summary.primary_channel}")

    # Contact recency
    if summary.days_since_contact < 7:
        parts.append(f"\nLast contact: within the last week")
    elif summary.days_since_contact < 30:
        parts.append(f"\nLast contact: within the last month")
    elif summary.days_since_contact < 90:
        parts.append(f"\nLast contact: within the last 3 months")
    elif summary.days_since_contact < 365:
        parts.append(f"\nLast contact: within the last year")
    else:
        parts.append(f"\nLast contact: over a year ago")

    # Aliases (for search matching)
    if person.aliases:
        parts.append(f"\nAlso known as: {', '.join(person.aliases)}")

    # Tags
    if person.tags:
        parts.append(f"\nTags: {', '.join(person.tags)}")

    # Notes
    if person.notes:
        parts.append(f"\nNotes: {person.notes}")

    # Facts (when available)
    if summary.has_facts:
        try:
            fact_store = get_person_fact_store()
            facts = fact_store.get_for_person(person.id)
            if facts:
                parts.append("\n## Known Facts")
                for fact in facts[:30]:  # Limit to top 30
                    parts.append(f"- {fact.category}: {fact.value}")
        except Exception as e:
            logger.debug(f"Could not get facts for {person.id}: {e}")

    return "\n".join(parts)


def index_person_to_vectorstore(
    person: PersonEntity,
    summary: RelationshipSummary,
    vector_store: Optional[VectorStore] = None,
) -> bool:
    """
    Index a single person to the vector store.

    Args:
        person: PersonEntity to index
        summary: RelationshipSummary with channel activity
        vector_store: Optional VectorStore instance (creates one if not provided)

    Returns:
        True if indexed successfully, False otherwise
    """
    if vector_store is None:
        vector_store = VectorStore(collection_name=PERSON_COLLECTION)

    try:
        doc = generate_person_document(person, summary)

        # Prepare metadata - ChromaDB needs flat values
        metadata = {
            "file_path": f"person:{person.id}",  # Used as unique key
            "file_name": person.display_name or person.canonical_name,
            "source_type": "crm_person",
            "person_id": person.id,
            "person_name": person.canonical_name,
            "relationship_strength": summary.relationship_strength,
            "category": person.category,
            "active_channels": ",".join(summary.active_channels),
            "primary_channel": summary.primary_channel or "",
            "days_since_contact": summary.days_since_contact,
            "has_facts": "true" if summary.has_facts else "false",
            # Store emails as JSON for potential filtering
            "emails": json.dumps(person.emails[:5]) if person.emails else "[]",
        }

        # Create a single chunk (person profiles are typically not long enough to chunk)
        chunks = [{"content": doc, "chunk_index": 0}]

        # Delete existing entry first (update)
        vector_store.delete_document(f"person:{person.id}")

        # Add new entry
        vector_store.add_document(chunks, metadata)

        return True

    except Exception as e:
        logger.error(f"Failed to index person {person.id}: {e}")
        return False


def sync_people_to_vectorstore(
    min_strength: float = MIN_STRENGTH_FOR_INDEX,
    min_interactions: int = MIN_INTERACTIONS_FOR_INDEX,
) -> dict:
    """
    Index significant contacts to the vector store.

    Only indexes people who meet the threshold criteria:
    - relationship_strength >= min_strength OR not hidden
    - total_interactions_90d >= min_interactions

    Args:
        min_strength: Minimum relationship strength to index
        min_interactions: Minimum 90-day interactions to index

    Returns:
        Statistics dict with indexed count, skipped count, errors
    """
    person_store = get_person_entity_store()
    vector_store = VectorStore(collection_name=PERSON_COLLECTION)

    people = person_store.get_all()
    logger.info(f"Checking {len(people)} people for vector store indexing...")

    indexed = 0
    skipped = 0
    errors = 0

    for person in people:
        # Skip hidden people
        if person.hidden:
            skipped += 1
            continue

        # Get relationship summary
        summary = get_relationship_summary(person.id)
        if not summary:
            skipped += 1
            continue

        # Check thresholds
        if summary.relationship_strength < min_strength and summary.total_interactions_90d < min_interactions:
            skipped += 1
            continue

        # Index the person
        if index_person_to_vectorstore(person, summary, vector_store):
            indexed += 1
        else:
            errors += 1

    logger.info(f"Indexed {indexed} people to vector store ({skipped} skipped, {errors} errors)")

    return {
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "total_checked": len(people),
    }


def remove_person_from_vectorstore(person_id: str) -> bool:
    """
    Remove a person from the vector store.

    Args:
        person_id: PersonEntity ID to remove

    Returns:
        True if removed successfully
    """
    try:
        vector_store = VectorStore(collection_name=PERSON_COLLECTION)
        vector_store.delete_document(f"person:{person_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to remove person {person_id} from vector store: {e}")
        return False


def search_people_semantic(
    query: str,
    top_k: int = 10,
    min_strength: Optional[float] = None,
    category: Optional[str] = None,
) -> list[dict]:
    """
    Search indexed people semantically.

    Args:
        query: Natural language search query
        top_k: Number of results to return
        min_strength: Optional minimum relationship strength filter
        category: Optional category filter (work, personal, family)

    Returns:
        List of search results with person info and scores
    """
    vector_store = VectorStore(collection_name=PERSON_COLLECTION)

    # Build filters
    filters = {"source_type": "crm_person"}
    if category:
        filters["category"] = category

    # Search
    results = vector_store.search(
        query=query,
        top_k=top_k * 2,  # Fetch extra for post-filtering
        filters=filters,
        recency_weight=0.3,  # Lower recency weight for people (relationship matters more)
    )

    # Post-filter by strength if specified
    if min_strength is not None:
        results = [r for r in results if r.get("relationship_strength", 0) >= min_strength]

    return results[:top_k]
