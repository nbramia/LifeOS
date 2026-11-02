"""
Person Profile Indexer - Index CRM person data to vector store.

Generates searchable documents for people with relationship context,
enabling semantic searches like "my important contacts" or "people I work with".

Key design principle: Only index significant contacts: not hidden, and either
relationship_strength >= MIN_STRENGTH_FOR_INDEX or at least
MIN_INTERACTIONS_FOR_INDEX interactions in the last 90 days. A sync leaves the
index holding exactly those people.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from api.services.vectorstore import VectorStore
from api.services.person_entity import PersonEntity, get_person_entity_store
from api.services.relationship_summary import get_relationship_summary, RelationshipSummary
from api.services.person_facts import get_person_fact_store
from config.relationship_weights import MIN_STRENGTH_FOR_INDEX, STRENGTH_DESCRIPTION_BANDS

logger = logging.getLogger(__name__)

# Collection name for person profiles (separate from vault documents)
PERSON_COLLECTION = "lifeos_people"

# Threshold for indexing, alongside MIN_STRENGTH_FOR_INDEX
MIN_INTERACTIONS_FOR_INDEX = 3  # Minimum interactions in 90 days

# A person's entry in the collection is keyed "person:<id>"
PERSON_PATH_PREFIX = "person:"

# A sync that would remove more than this share of the people index stops short
# of removing anyone: a drop that large points at bad input, not at departures.
MAX_REMOVAL_FRACTION = 0.5

# Candidates pulled from the index for a description. Most profiles carry only a
# name and channel counts, so the people who actually fit can rank well down.
DESCRIPTION_CANDIDATES = 100

# A profile that shares no word with a description has to be at least this similar to count.
DESCRIPTION_MIN_SIMILARITY = 0.65

# Words that say a question is about people without saying which people.
_DESCRIPTION_STOPWORDS = frozenset("""
    a an and any are at can do does for from has have how i in is it know knows me my of on or
    people person that the their them they to was were what when where which who whom why will
    with work works worked working you days someone anyone somebody anybody
""".split())

# Wording that says a search is for whoever fits, rather than for someone by name.
_ASKS_WHO_FITS = re.compile(
    r"\bwho\s+\w"
    r"|\b(?:someone|somebody|anyone|anybody|people|person|contacts?)\s+(?:who|that|in|at|from|with|working)\b"
    r"|^(?:do\s+i\s+know|which)\b"
    r"|^that\s+\w+\s+(?:from|who|at|in|with)\b",
    re.IGNORECASE,
)

# The same wording in front of a name: "who is Robin Example", "do I know Maria de la Cruz".
_ASKS_ABOUT_A_NAME = re.compile(
    r"(?:\bwho\s+(?:is|was)|^do\s+i\s+know)\s+"
    r"(?!(?:a|an|the|any|anyone|anybody|some|someone|somebody|that|in|at|from|on|with|my|our)\b)"
    r"|\bwho\s+knows?\s+(?-i:[A-Z])"
    r"|^(?:which|that)\s+(?-i:[A-Z])",
    re.IGNORECASE,
)


def is_index_eligible(
    person: PersonEntity,
    summary: Optional[RelationshipSummary],
    min_strength: float = MIN_STRENGTH_FOR_INDEX,
    min_interactions: int = MIN_INTERACTIONS_FOR_INDEX,
) -> bool:
    """Whether a person belongs in the people index."""
    if person.hidden or not summary:
        return False
    return summary.relationship_strength >= min_strength or summary.total_interactions_90d >= min_interactions


def plan_index_removal(
    indexed_paths: set[str],
    eligible_ids: set[str],
    allow_large_removal: bool = False,
) -> tuple[set[str], Optional[str]]:
    """
    Work out which indexed people to remove, and whether it is safe to.

    Args:
        indexed_paths: File paths currently in the people collection
        eligible_ids: IDs of everyone who belongs in the index
        allow_large_removal: Accept a removal above MAX_REMOVAL_FRACTION. An
            empty eligible set is refused regardless.

    Returns:
        (stale paths, reason removal must be skipped or None)
    """
    person_paths = {p for p in indexed_paths if p.startswith(PERSON_PATH_PREFIX)}
    stale = {p for p in person_paths if p[len(PERSON_PATH_PREFIX):] not in eligible_ids}

    if not stale:
        return stale, None
    if not eligible_ids:
        return stale, "no one is eligible"
    if not allow_large_removal and len(stale) / len(person_paths) > MAX_REMOVAL_FRACTION:
        return stale, (
            f"{len(stale)} of {len(person_paths)} indexed people would go, "
            f"over the {MAX_REMOVAL_FRACTION:.0%} limit"
        )
    return stale, None


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
    description = next(
        (label for cutoff, label in STRENGTH_DESCRIPTION_BANDS if strength >= cutoff),
        "Distant",
    )
    parts.append(f"{description} relationship (strength: {strength}/100)")

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

    # Contact recency. The never-contacted sentinel is a marker, not a
    # measurement: bucketing it lands in the final branch, which would index
    # "over a year ago" as a searchable claim about someone who was never
    # contacted at all.
    if not summary.contact_on_record:
        parts.append("\nLast contact: none on record")
    elif summary.days_since_contact < 7:
        parts.append("\nLast contact: within the last week")
    elif summary.days_since_contact < 30:
        parts.append("\nLast contact: within the last month")
    elif summary.days_since_contact < 90:
        parts.append("\nLast contact: within the last 3 months")
    elif summary.days_since_contact < 365:
        parts.append("\nLast contact: within the last year")
    else:
        parts.append("\nLast contact: over a year ago")

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
            "file_path": f"{PERSON_PATH_PREFIX}{person.id}",  # Used as unique key
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
        vector_store.delete_document(f"{PERSON_PATH_PREFIX}{person.id}")

        # Add new entry
        vector_store.add_document(chunks, metadata)

        return True

    except Exception as e:
        logger.error(f"Failed to index person {person.id}: {e}")
        return False


def sync_people_to_vectorstore(
    min_strength: float = MIN_STRENGTH_FOR_INDEX,
    min_interactions: int = MIN_INTERACTIONS_FOR_INDEX,
    allow_large_removal: bool = False,
) -> dict:
    """
    Make the people index hold exactly the eligible people.

    Indexes everyone who is not hidden and has either
    relationship_strength >= min_strength or
    total_interactions_90d >= min_interactions, then removes every other
    indexed person. Removal is skipped, and logged as an error, when the index
    cannot be listed, no one is eligible, or it would exceed
    MAX_REMOVAL_FRACTION of the index. A failure to load the people stops the
    sync before anything is removed.

    Args:
        min_strength: Minimum relationship strength to index
        min_interactions: Minimum 90-day interactions to index
        allow_large_removal: Accept a removal above MAX_REMOVAL_FRACTION

    Returns:
        Statistics dict with indexed, skipped, errors, total_checked, stale,
        removed, removal_errors and removal_skipped_reason (None when removal ran)
    """
    person_store = get_person_entity_store()
    vector_store = VectorStore(collection_name=PERSON_COLLECTION)

    # Listed before indexing, so the removal limit measures the index as it
    # stood rather than one this run has already grown.
    indexed_paths: Optional[set[str]] = None
    try:
        indexed_paths = vector_store.get_all_file_paths()
    except Exception as e:
        logger.error(f"Could not list the people index: {type(e).__name__}")

    people = person_store.get_all()
    logger.info(f"Checking {len(people)} people for vector store indexing...")

    indexed = 0
    skipped = 0
    errors = 0
    eligible_ids: set[str] = set()

    for person in people:
        summary = get_relationship_summary(person.id)

        if not is_index_eligible(person, summary, min_strength, min_interactions):
            skipped += 1
            continue

        eligible_ids.add(person.id)

        # Index the person
        if index_person_to_vectorstore(person, summary, vector_store):
            indexed += 1
        else:
            errors += 1

    logger.info(f"Indexed {indexed} people to vector store ({skipped} skipped, {errors} errors)")

    stale: set[str] = set()
    skip_reason: Optional[str] = "the index could not be listed"
    if indexed_paths is not None:
        stale, skip_reason = plan_index_removal(indexed_paths, eligible_ids, allow_large_removal)

    removed = 0
    removal_errors = 0
    if skip_reason:
        logger.error(f"People index removal skipped, {len(stale)} stale entries kept: {skip_reason}")
    else:
        for path in stale:
            if remove_person_from_vectorstore(path[len(PERSON_PATH_PREFIX):], vector_store):
                removed += 1
            else:
                removal_errors += 1
        if stale:
            logger.info(f"Removed {removed} people from the index ({removal_errors} errors)")

    return {
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "total_checked": len(people),
        "stale": len(stale),
        "removed": removed,
        "removal_errors": removal_errors,
        "removal_skipped_reason": skip_reason,
    }


def remove_person_from_vectorstore(person_id: str, vector_store: Optional[VectorStore] = None) -> bool:
    """
    Remove a person from the vector store.

    Args:
        person_id: PersonEntity ID to remove
        vector_store: Optional VectorStore instance (creates one if not provided)

    Returns:
        True if removed successfully
    """
    try:
        if vector_store is None:
            vector_store = VectorStore(collection_name=PERSON_COLLECTION)
        vector_store.delete_document(f"{PERSON_PATH_PREFIX}{person_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to remove person {person_id} from vector store: {type(e).__name__}")
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

    results = vector_store.search(
        query=query,
        top_k=top_k * 2,  # Fetch extra for post-filtering
        recency_weight=0.3,  # Lower recency weight for people (relationship matters more)
    )

    # An entry is keyed by its path and holds no other person fields. The index is
    # also rebuilt nightly, so it can still hold someone hidden or merged away
    # since. The live store says who a hit is, and whether they still exist.
    ids = [r.get("file_path", "").removeprefix(PERSON_PATH_PREFIX) for r in results]
    live = get_person_entity_store().get_by_ids(ids, exclude_hidden=True)

    people = []
    for person_id, result in zip(ids, results):
        person = live.get(person_id)
        if not person or person.id != person_id:
            continue
        if min_strength is not None and person.relationship_strength < min_strength:
            continue
        if category and person.category != category:
            continue
        people.append({**result, "person_id": person_id, "person": person})

    return people[:top_k]


@dataclass
class DescriptionMatch:
    """A person whose indexed profile fits a description."""

    person: PersonEntity
    profile: str  # The lines of their profile that say who they are
    matched_words: list[str]
    similarity: float


def _description_words(text: str) -> list[str]:
    return [
        word for word in re.findall(r"[a-z]+", text.lower())
        if len(word) > 2 and word not in _DESCRIPTION_STOPWORDS
    ]


def asks_who_fits(text: str) -> bool:
    """
    Whether a search says outright that it wants whoever fits a description.

    A name that was not found has to stay a miss, however many words it has or
    whatever its capitalization, because a similarity search would only answer it
    with people who are not that person. Only wording like "who works in" or
    "people in" lifts that, and not when a name follows it.

    Args:
        text: The search term

    Returns:
        True when the term is worth a search by description
    """
    words = text.split()
    if "@" in text or all(word[0].isupper() for word in words) or not _description_words(text):
        return False
    return bool(_ASKS_WHO_FITS.search(text)) and not _ASKS_ABOUT_A_NAME.search(text)


def _descriptive_lines(document: str) -> str:
    """The parts of a person document that say who they are, not how often they are in touch."""
    lines = []
    in_facts = False
    for line in document.splitlines()[1:]:
        if line.startswith("## "):
            in_facts = line.startswith("## Known Facts")
        elif in_facts or line.startswith(("Works at", "Tags:", "Notes:", "Also known as")):
            lines.append(line)
    return "\n".join(lines)


def find_people_by_description(query: str, top_k: int = 5) -> list[DescriptionMatch]:
    """
    Find people whose profile fits a description such as "who do I know in fundraising".

    The index narrows the field, then a profile only counts if it says something
    about the person. A profile sharing a word with the description ranks first;
    one sharing none has to clear DESCRIPTION_MIN_SIMILARITY. Results are
    suggestions: nothing here identifies a person the way a name does.

    Args:
        query: The description
        top_k: Most people to return

    Returns:
        Matches, best first. Empty when nothing fits or the index cannot be reached.
    """
    words = _description_words(query)
    if not words:
        return []
    patterns = {word: re.compile(rf"\b{word[:6]}") for word in words}

    try:
        hits = search_people_semantic(query, top_k=DESCRIPTION_CANDIDATES)
    except Exception as e:
        logger.warning(f"Could not search people by description: {type(e).__name__}")
        return []

    matches = []
    for hit in hits:
        profile = _descriptive_lines(hit.get("content", ""))
        if not profile:
            continue
        lowered = profile.lower()
        matched = [word for word, pattern in patterns.items() if pattern.search(lowered)]
        similarity = hit.get("semantic_score", 0)
        if matched or similarity >= DESCRIPTION_MIN_SIMILARITY:
            matches.append(DescriptionMatch(hit["person"], profile, matched, similarity))

    matches.sort(key=lambda m: (len(m.matched_words), m.similarity), reverse=True)
    return matches[:top_k]
