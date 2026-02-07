"""
Entity Resolver for LifeOS People System v2.

Implements three-pass entity resolution:
1. Email anchoring - exact email match across sources
2. Fuzzy name matching with context boost - for names without email
3. Disambiguation - create separate entities when ambiguous
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from api.services.person_entity import PersonEntity, PersonEntityStore, get_person_entity_store
from config.nickname_lookup import are_name_variants

# Name prefixes to strip before parsing (case-insensitive)
NAME_PREFIXES = {'dr', 'dr.', 'mr', 'mr.', 'mrs', 'mrs.', 'ms', 'ms.', 'prof', 'prof.', 'rev', 'rev.'}

# Name suffixes to strip before parsing (case-insensitive, may have trailing punctuation)
NAME_SUFFIXES = {
    'md', 'phd', 'jr', 'sr', 'ii', 'iii', 'iv', 'v',
    'esq', 'mph', 'dds', 'dmd', 'do', 'rn', 'cpa',
    'mba', 'jd', 'llm', 'msw', 'lcsw', 'psyd', 'edd',
}


@dataclass
class ParsedName:
    """Structured representation of a parsed name."""
    first: str  # First name (required)
    middles: list[str]  # Middle names (may be empty)
    last: Optional[str]  # Last name (None for single-word names)
    original: str  # Original input string


def parse_name(name: str) -> ParsedName:
    """
    Parse a name into structured components.

    Strips common prefixes (Dr., Mr., Mrs.) and suffixes (MD, PhD, Jr).
    Also strips anything after a comma (credentials like ", CLC, CSC" or ", PhD").
    Returns {first, middles[], last} structure.

    Examples:
        "John Smith" -> {first="John", middles=[], last="Smith"}
        "Dr. Mary Katherine Palmer MD" -> {first="Mary", middles=["Katherine"], last="Palmer"}
        "Taylor" -> {first="Taylor", middles=[], last=None}
        "Jane Mary Smith" -> {first="Jane", middles=["Mary"], last="Smith"}
        "Sarah Long, CLC, CSC" -> {first="Sarah", middles=[], last="Long"}

    Args:
        name: Name string to parse

    Returns:
        ParsedName with structured components
    """
    if not name or not name.strip():
        return ParsedName(first="", middles=[], last=None, original=name or "")

    original = name

    # First, strip anything after a comma (credentials like ", PhD" or ", CLC, CSC")
    if ',' in name:
        name = name.split(',')[0].strip()

    parts = name.strip().split()

    # Strip prefixes from the beginning
    while parts and parts[0].lower().rstrip('.,') in NAME_PREFIXES:
        parts.pop(0)

    # Strip suffixes from the end
    while parts and parts[-1].lower().rstrip('.,') in NAME_SUFFIXES:
        parts.pop()

    if not parts:
        # All parts were prefixes/suffixes, return original as first name
        return ParsedName(first=original.strip(), middles=[], last=None, original=original)

    if len(parts) == 1:
        return ParsedName(first=parts[0], middles=[], last=None, original=original)
    elif len(parts) == 2:
        return ParsedName(first=parts[0], middles=[], last=parts[1], original=original)
    else:
        # 3+ parts: first, middle(s), last
        return ParsedName(
            first=parts[0],
            middles=parts[1:-1],
            last=parts[-1],
            original=original,
        )
from api.services.people import resolve_person_name, PEOPLE_DICTIONARY
from api.services.link_override import get_link_override_store
from config.people_config import (
    DOMAIN_CONTEXT_MAP,
    COMPANY_NORMALIZATION,
    EntityResolutionConfig,
    get_vault_contexts_for_domain,
    get_domains_for_company,
    get_vault_contexts_for_company,
    normalize_domain,
)
from config.relationship_weights import (
    RELATIONSHIP_STRENGTH_BOOST_MAX,
    RELATIONSHIP_STRENGTH_BOOST_WEIGHT,
    FIRST_NAME_ONLY_BOOST_MULTIPLIER,
)
from config.settings import settings
from api.utils.datetime_utils import make_aware as _make_aware

logger = logging.getLogger(__name__)


@dataclass
class ResolutionCandidate:
    """A potential match for entity resolution."""

    entity: PersonEntity
    score: float
    match_type: str  # "email_exact", "name_fuzzy", "alias_exact"
    confidence: float  # 0.0-1.0


@dataclass
class ResolutionResult:
    """Result of entity resolution."""

    entity: PersonEntity
    is_new: bool  # True if a new entity was created
    confidence: float
    match_type: str
    disambiguation_applied: bool = False


class EntityResolver:
    """
    Resolves names/emails to PersonEntity instances.

    Uses a three-pass algorithm:
    1. Email anchoring (exact match)
    2. Fuzzy name matching with context boost
    3. Disambiguation for ambiguous cases
    """

    def __init__(self, entity_store: Optional[PersonEntityStore] = None):
        """
        Initialize the resolver.

        Args:
            entity_store: PersonEntityStore to use (default singleton)
        """
        self._store = entity_store or get_person_entity_store()

    @property
    def store(self) -> PersonEntityStore:
        """Get the entity store."""
        return self._store

    def resolve_by_email(self, email: str) -> Optional[PersonEntity]:
        """
        Pass 1: Exact email match.

        Args:
            email: Email address to look up

        Returns:
            PersonEntity if found, None otherwise
        """
        if not email:
            return None
        return self._store.get_by_email(email.lower())

    def resolve_by_phone(self, phone: str) -> Optional[PersonEntity]:
        """
        Phone anchor: Exact phone match (E.164 format).

        Args:
            phone: Phone number in E.164 format (+1XXXXXXXXXX)

        Returns:
            PersonEntity if found, None otherwise
        """
        if not phone:
            return None
        return self._store.get_by_phone(phone)

    def resolve_by_name(
        self,
        name: str,
        context_path: Optional[str] = None,
        create_if_missing: bool = False,
    ) -> Optional[ResolutionResult]:
        """
        Pass 2 & 3: Fuzzy name matching with context boost and disambiguation.

        Args:
            name: Name to resolve (will be canonicalized)
            context_path: Vault path for context boost (e.g., "Work/ML/meeting.md")
            create_if_missing: Create new entity if no match found

        Returns:
            ResolutionResult with matched/created entity, or None
        """
        if not name or not name.strip():
            return None

        # First, try to canonicalize using existing PEOPLE_DICTIONARY
        canonical = resolve_person_name(name)

        # Check for exact name/alias match in store
        exact_match = self._store.get_by_name(canonical)
        if exact_match:
            return ResolutionResult(
                entity=exact_match,
                is_new=False,
                confidence=1.0,
                match_type="name_exact",
            )

        # Check for link override (disambiguation rules from previous splits)
        override_store = get_link_override_store()
        override = override_store.find_matching(
            name=canonical,
            source_type=None,  # Will be passed in enhanced version
            context_path=context_path,
        )
        if override:
            preferred = self._store.get_by_id(override.preferred_person_id)
            if preferred:
                logger.debug(f"Link override matched: '{canonical}' -> {preferred.canonical_name}")
                return ResolutionResult(
                    entity=preferred,
                    is_new=False,
                    confidence=1.0,
                    match_type="link_override",
                )

        # Score all candidates using fuzzy matching
        candidates = self._score_candidates(canonical, context_path)

        if not candidates:
            if create_if_missing:
                return self._create_new_entity(canonical, context_path)
            return None

        # Sort by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)

        top = candidates[0]

        # Check if score meets minimum threshold
        if top.score < EntityResolutionConfig.MIN_MATCH_SCORE:
            if create_if_missing:
                return self._create_new_entity(canonical, context_path)
            return None

        # Check for disambiguation (Pass 3)
        if len(candidates) >= 2:
            second = candidates[1]
            score_diff = top.score - second.score

            if score_diff < EntityResolutionConfig.DISAMBIGUATION_THRESHOLD:
                # Ambiguous - check if we should create a new entity
                if create_if_missing:
                    return self._create_disambiguated_entity(
                        canonical, context_path, top.entity
                    )
                # Otherwise return top match with lower confidence
                return ResolutionResult(
                    entity=top.entity,
                    is_new=False,
                    confidence=top.confidence * 0.7,  # Reduce confidence for ambiguous
                    match_type="fuzzy_ambiguous",
                    disambiguation_applied=True,
                )

        return ResolutionResult(
            entity=top.entity,
            is_new=False,
            confidence=top.confidence,
            match_type=top.match_type,
        )

    def _score_candidates(
        self, name: str, context_path: Optional[str]
    ) -> list[ResolutionCandidate]:
        """
        Score all entities against a name using structured matching.

        Uses a three-phase approach:
        1. Hard disqualifiers - Different last names → skip candidate
        2. Name component matching - Exact/fuzzy matches on first, middle, last
        3. Bonus scoring - Context, recency, relationship strength

        Scoring system (approximate points):
        - Exact last name match: 30 points
        - Exact first name match: 25 points
        - First name = middle name cross-match: 15 points
        - Initial matches full name: 10 points
        - Context boost: 30 points
        - Recency boost: 10 points
        - Relationship strength: 0-25 points (scaled)

        Args:
            name: Name to match against
            context_path: Path for context boost

        Returns:
            List of candidates with scores
        """
        candidates = []

        # Parse the query name into components
        query = parse_name(name)
        query_first_lower = query.first.lower() if query.first else ""
        query_middles_lower = [m.lower() for m in query.middles]
        query_last_lower = query.last.lower() if query.last else None

        # Check if this is a first-name-only match (single word, no last name)
        # First-name mentions in notes usually refer to close contacts
        is_first_name_only = query.last is None

        # Check if query last name is an initial (single character)
        is_last_initial = query_last_lower and len(query_last_lower) == 1
        # Check if query first name is an initial (single character)
        is_first_initial = len(query_first_lower) == 1

        for entity in self._store.get_all():
            # Parse entity's canonical name
            entity_parsed = parse_name(entity.canonical_name)
            entity_first_lower = entity_parsed.first.lower() if entity_parsed.first else ""
            entity_middles_lower = [m.lower() for m in entity_parsed.middles]
            entity_last_lower = entity_parsed.last.lower() if entity_parsed.last else None

            # Also parse all aliases
            alias_parses = [parse_name(alias) for alias in entity.aliases]

            # ===== PHASE 1: HARD DISQUALIFIERS =====
            # If both names have last names, they must match (or be initials)
            if query_last_lower and not is_first_name_only:
                last_name_matches = False

                # Check canonical name
                if entity_last_lower:
                    if is_last_initial:
                        # Query last is initial: check prefix match
                        if entity_last_lower.startswith(query_last_lower):
                            last_name_matches = True
                    elif fuzz.ratio(query_last_lower, entity_last_lower) >= 85:
                        # Fuzzy match for typos/variations
                        last_name_matches = True

                # Check aliases for last name match
                if not last_name_matches:
                    for ap in alias_parses:
                        if ap.last:
                            ap_last = ap.last.lower()
                            if is_last_initial:
                                if ap_last.startswith(query_last_lower):
                                    last_name_matches = True
                                    break
                            elif fuzz.ratio(query_last_lower, ap_last) >= 85:
                                last_name_matches = True
                                break

                # Skip this candidate if last names don't match
                if not last_name_matches:
                    continue

            # ===== PHASE 2: NAME COMPONENT MATCHING =====
            score = 0.0
            match_type = "structured"

            # --- Last name matching (50 points max) - most important signal ---
            if query_last_lower and entity_last_lower:
                if query_last_lower == entity_last_lower:
                    score += 50  # Exact match
                elif is_last_initial and entity_last_lower.startswith(query_last_lower):
                    score += 35  # Initial prefix match
                elif fuzz.ratio(query_last_lower, entity_last_lower) >= 85:
                    score += 25  # Fuzzy match (typos/variations)

            # --- First name matching (25 points max) ---
            # Note: first-name-only bonus is applied later after checking for ambiguity
            first_matched = False
            first_name_exact_match = False
            if query_first_lower and entity_first_lower:
                if query_first_lower == entity_first_lower:
                    score += 25  # Exact match
                    first_matched = True
                    first_name_exact_match = True
                elif is_first_initial and entity_first_lower.startswith(query_first_lower):
                    score += 10  # Initial prefix match
                    first_matched = True
                elif len(entity_first_lower) == 1 and query_first_lower.startswith(entity_first_lower):
                    score += 10  # Entity has initial, query has full name
                    first_matched = True
                elif are_name_variants(query_first_lower, entity_first_lower):
                    score += 20  # Nickname match (Ben/Benjamin, Mike/Michael)
                    first_matched = True
                elif fuzz.ratio(query_first_lower, entity_first_lower) >= 85:
                    score += 20  # Fuzzy match (typos)
                    first_matched = True

            # --- First name = middle name cross-matching (15 points max) ---
            # Check if query first matches any entity middle
            if not first_matched and query_first_lower:
                for em in entity_middles_lower:
                    if query_first_lower == em:
                        score += 15
                        first_matched = True
                        break
                    elif fuzz.ratio(query_first_lower, em) >= 85:
                        score += 12
                        first_matched = True
                        break

            # Check if any query middle matches entity first
            if entity_first_lower:
                for qm in query_middles_lower:
                    if qm == entity_first_lower:
                        score += 15
                        break
                    elif fuzz.ratio(qm, entity_first_lower) >= 85:
                        score += 12
                        break

            # --- Middle name matching (10 points max) ---
            if query_middles_lower and entity_middles_lower:
                for qm in query_middles_lower:
                    for em in entity_middles_lower:
                        if qm == em:
                            score += 10
                            break
                        elif fuzz.ratio(qm, em) >= 85:
                            score += 7
                            break

            # --- Check aliases for additional matches ---
            best_alias_score = 0
            for ap in alias_parses:
                alias_score = 0
                ap_first = ap.first.lower() if ap.first else ""
                ap_last = ap.last.lower() if ap.last else None
                ap_middles = [m.lower() for m in ap.middles]

                # First name match in alias
                if query_first_lower and ap_first:
                    if query_first_lower == ap_first:
                        alias_score += 25
                    elif is_first_initial and ap_first.startswith(query_first_lower):
                        alias_score += 10

                # Cross-match: query first = alias middle
                if query_first_lower:
                    for am in ap_middles:
                        if query_first_lower == am:
                            alias_score += 15
                            break

                best_alias_score = max(best_alias_score, alias_score)

            # Add best alias bonus (but don't double-count with canonical)
            if best_alias_score > 0 and not first_matched:
                score += best_alias_score

            # ===== PHASE 3: BONUS SCORING =====

            # Context boost
            if context_path and entity.vault_contexts:
                if self._path_matches_context(context_path, entity.vault_contexts):
                    score += EntityResolutionConfig.CONTEXT_BOOST_POINTS
                    match_type = "structured_context"

            # Recency boost
            if entity.last_seen:
                days_since = (datetime.now(timezone.utc) - _make_aware(entity.last_seen)).days
                if days_since < EntityResolutionConfig.RECENCY_THRESHOLD_DAYS:
                    score += EntityResolutionConfig.RECENCY_BOOST_POINTS

            # Relationship strength boost
            # People you have strong relationships with are more likely to be
            # mentioned by first name in your notes
            rel_strength = entity.relationship_strength  # 0-100 scale
            if rel_strength > 0:
                # Calculate boost: strength (0-100) * weight -> points (0-25 default)
                rel_boost = min(
                    rel_strength * RELATIONSHIP_STRENGTH_BOOST_WEIGHT,
                    RELATIONSHIP_STRENGTH_BOOST_MAX
                )
                # Apply stronger boost for first-name-only matches
                if is_first_name_only:
                    rel_boost *= FIRST_NAME_ONLY_BOOST_MULTIPLIER
                score += rel_boost
                if rel_boost > 5:  # Significant boost
                    match_type = f"{match_type}_relationship"

            # For full names (both have first and last), require first name similarity
            # This prevents "John Smith" from matching "Jane Smith"
            if not is_first_name_only and query_last_lower and entity_last_lower:
                if not first_matched:
                    # Both have last names, but no first name match - skip
                    continue

            # For first-name-only queries, require first name to match
            # (prevents context-only matches like "Sarah" matching "Taylor" via context boost)
            if is_first_name_only and not first_matched:
                continue

            # Only add candidates with meaningful scores
            # Minimum: at least first OR last name should match (20+ points)
            if score >= 20:
                confidence = min(score / 100.0, 1.0)
                candidates.append(
                    ResolutionCandidate(
                        entity=entity,
                        score=score,
                        match_type=match_type,
                        confidence=confidence,
                    )
                )

        # === FIRST-NAME-ONLY AMBIGUITY CHECK ===
        # For first-name-only queries, check if there's only one clear match.
        # We consider:
        # 1. Only one candidate → unambiguous
        # 2. Multiple candidates, but only one passes MIN_MATCH_SCORE → unambiguous
        # 3. Multiple close relationship matches without clear winner → ambiguous
        # 4. One candidate has significantly higher score → unambiguous
        if is_first_name_only and candidates:
            MIN_SCORE = EntityResolutionConfig.MIN_MATCH_SCORE

            # Check how many candidates would pass the minimum threshold
            passing_candidates = [c for c in candidates if c.score >= MIN_SCORE]

            if len(candidates) == 1:
                # Only one person with this first name - unambiguous
                candidates[0].score += 15
                candidates[0].match_type = "first_name_unique"
                candidates[0].confidence = min(candidates[0].score / 100.0, 1.0)
            elif len(passing_candidates) == 1:
                # Only one candidate passes threshold (e.g., has context boost)
                passing_candidates[0].score += 10
                passing_candidates[0].match_type = "first_name_context_clear"
                passing_candidates[0].confidence = min(passing_candidates[0].score / 100.0, 1.0)
            elif len(passing_candidates) > 1:
                # Multiple candidates pass threshold - check for clear winner
                passing_candidates.sort(key=lambda c: c.score, reverse=True)
                score_diff = passing_candidates[0].score - passing_candidates[1].score

                if score_diff >= 20:
                    # Clear winner by score (significant lead)
                    passing_candidates[0].score += 10
                    passing_candidates[0].match_type = "first_name_score_dominant"
                    passing_candidates[0].confidence = min(passing_candidates[0].score / 100.0, 1.0)
                else:
                    # Check relationship strength as tiebreaker
                    CLOSE_THRESHOLD = 30
                    close_matches = [c for c in passing_candidates
                                     if c.entity.relationship_strength >= CLOSE_THRESHOLD]

                    if len(close_matches) == 1:
                        # Only one is "close" - use that one
                        close_matches[0].score += 15
                        close_matches[0].match_type = "first_name_close_unique"
                        close_matches[0].confidence = min(close_matches[0].score / 100.0, 1.0)
                    elif len(close_matches) > 1:
                        # Multiple close people - check relationship strength difference
                        close_matches.sort(key=lambda c: c.entity.relationship_strength, reverse=True)
                        strength_diff = close_matches[0].entity.relationship_strength - close_matches[1].entity.relationship_strength

                        if strength_diff >= 25:
                            # One person is clearly closer
                            close_matches[0].score += 10
                            close_matches[0].match_type = "first_name_relationship_dominant"
                            close_matches[0].confidence = min(close_matches[0].score / 100.0, 1.0)
                        else:
                            # Truly ambiguous - multiple close people with similar strength
                            logger.debug(f"First-name-only '{name}' ambiguous: {len(close_matches)} close matches")
                            return []
                    else:
                        # Multiple passing but none close - ambiguous
                        logger.debug(f"First-name-only '{name}' ambiguous: {len(passing_candidates)} passing, none close")
                        return []
            # else: no candidates pass threshold - will naturally return no match

        return candidates

    def _path_matches_context(
        self, file_path: str, vault_contexts: list[str]
    ) -> bool:
        """
        Check if a file path matches any of the vault contexts.

        Args:
            file_path: Path to check (e.g., "/Users/x/Notes 2025/Work/ML/meeting.md")
            vault_contexts: List of context prefixes (e.g., ["Work/ML/"])

        Returns:
            True if path is within any context
        """
        # Normalize path
        path_str = str(file_path).replace("\\", "/")

        for context in vault_contexts:
            context_normalized = context.replace("\\", "/")
            if context_normalized in path_str:
                return True

        return False

    def _create_new_entity(
        self, name: str, context_path: Optional[str]
    ) -> ResolutionResult:
        """
        Create a new PersonEntity for an unknown name.

        Args:
            name: Canonical name
            context_path: Path for inferring vault context

        Returns:
            ResolutionResult with new entity
        """
        vault_contexts = []
        category = "unknown"

        if context_path:
            # Try to infer vault context from path
            vault_contexts = self._infer_vault_contexts(context_path)
            category = self._infer_category(context_path)

        entity = PersonEntity(
            canonical_name=name,
            display_name=name,
            vault_contexts=vault_contexts,
            category=category,
            first_seen=None,  # Will be set from actual interactions
            last_seen=None,   # Will be set from actual interactions
        )

        stored = self._store.add(entity)

        return ResolutionResult(
            entity=stored,
            is_new=True,
            confidence=0.5,  # Lower confidence for new entities
            match_type="new_entity",
        )

    def _create_disambiguated_entity(
        self, name: str, context_path: Optional[str], similar_entity: PersonEntity
    ) -> ResolutionResult:
        """
        Create a disambiguated entity when name is ambiguous.

        Args:
            name: Canonical name
            context_path: Path for inferring context
            similar_entity: The entity this would be confused with

        Returns:
            ResolutionResult with disambiguated entity
        """
        vault_contexts = []
        category = "unknown"
        suffix = ""

        if context_path:
            vault_contexts = self._infer_vault_contexts(context_path)
            category = self._infer_category(context_path)
            suffix = self._infer_context_suffix(context_path)

        # Create display name with disambiguation
        display_name = f"{name} ({suffix})" if suffix else name

        entity = PersonEntity(
            canonical_name=name,
            display_name=display_name,
            vault_contexts=vault_contexts,
            category=category,
            first_seen=None,  # Will be set from actual interactions
            last_seen=None,   # Will be set from actual interactions
            confidence_score=0.7,  # Slightly lower for disambiguated
        )

        stored = self._store.add(entity)

        return ResolutionResult(
            entity=stored,
            is_new=True,
            confidence=0.7,
            match_type="disambiguated",
            disambiguation_applied=True,
        )

    def _infer_vault_contexts(self, file_path: str) -> list[str]:
        """Infer vault context from file path."""
        from config.settings import settings
        path_str = str(file_path).replace("\\", "/")

        # Check current work path first (most specific)
        if settings.current_work_path and settings.current_work_path in path_str:
            return [settings.current_work_path]
        # Check archive paths from crm_mappings (loaded dynamically)
        elif settings.personal_archive_path and settings.personal_archive_path in path_str:
            # Try to find more specific archive context
            from config.people_config import DOMAIN_CONTEXT_MAP
            for contexts in DOMAIN_CONTEXT_MAP.values():
                for ctx in contexts:
                    if ctx.startswith(settings.personal_archive_path) and ctx in path_str:
                        return [ctx]
            return [settings.personal_archive_path]
        elif "Personal/" in path_str:
            return ["Personal/"]
        elif "Work/" in path_str:
            return ["Work/"]

        return []

    def _infer_category(self, file_path: str) -> str:
        """Infer category from file path."""
        from config.settings import settings
        path_str = str(file_path).replace("\\", "/")

        if "Work/" in path_str:
            return "work"
        elif f"Personal/{settings.relationship_folder}" in path_str:
            return "family"
        elif "Personal/" in path_str:
            return "personal"

        return "unknown"

    def _infer_context_suffix(self, file_path: str) -> str:
        """Infer disambiguation suffix from file path."""
        from config.settings import settings
        from config.people_config import COMPANY_NORMALIZATION
        path_str = str(file_path).replace("\\", "/")

        # Check current work path
        if settings.current_work_path and settings.current_work_path in path_str:
            # Try to find company name from normalization map
            for company, info in COMPANY_NORMALIZATION.items():
                if any(settings.current_work_path in ctx for ctx in info.get("vault_contexts", [])):
                    return company.split()[0]  # First word of company name
            return "Work"

        # Check archive paths for company context
        if settings.personal_archive_path and settings.personal_archive_path in path_str:
            for company, info in COMPANY_NORMALIZATION.items():
                for ctx in info.get("vault_contexts", []):
                    if ctx in path_str:
                        return company.split()[0]
            return "Archive"

        if "Work/" in path_str:
            return "Work"
        elif "Personal/" in path_str:
            return "Personal"

        return ""

    def resolve(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        context_path: Optional[str] = None,
        create_if_missing: bool = False,
    ) -> Optional[ResolutionResult]:
        """
        Main entry point: resolve a person by name, email, and/or phone.

        Priority:
        1. Email exact match (if email provided)
        2. Phone exact match (if phone provided, E.164 format)
        3. Name exact match
        4. Fuzzy name match with context boost
        5. Create new entity (if create_if_missing)

        Args:
            name: Person's name
            email: Person's email
            phone: Person's phone (E.164 format, e.g., +1XXXXXXXXXX)
            context_path: Vault path for context boost
            create_if_missing: Create new entity if not found

        Returns:
            ResolutionResult or None
        """
        # Pass 1: Email exact match
        if email:
            entity = self.resolve_by_email(email)
            if entity:
                return ResolutionResult(
                    entity=entity,
                    is_new=False,
                    confidence=1.0,
                    match_type="email_exact",
                )

        # Pass 1b: Phone exact match
        if phone:
            entity = self.resolve_by_phone(phone)
            if entity:
                return ResolutionResult(
                    entity=entity,
                    is_new=False,
                    confidence=1.0,
                    match_type="phone_exact",
                )

        # Pass 2 & 3: Name matching
        if name:
            result = self.resolve_by_name(
                name, context_path, create_if_missing=create_if_missing
            )
            if result:
                # If we also have email/phone, add them to the entity
                updated = False
                if email and result.is_new:
                    result.entity.add_email(email)
                    updated = True
                if phone and result.is_new:
                    result.entity.add_phone(phone)
                    updated = True
                if updated:
                    self._store.update(result.entity)
                return result

        # If we have email but no name match, create entity from email
        if email and create_if_missing:
            # SAFETY: Double-check email index before creating to avoid race conditions
            # If the email index was stale (e.g., after concurrent operations), this prevents
            # creating a duplicate entity with a new ID
            existing = self._store.get_by_email(email.lower())
            if existing:
                logger.warning(f"Race condition avoided: email {email} already exists")
                return ResolutionResult(
                    entity=existing,
                    is_new=False,
                    confidence=1.0,
                    match_type="email_exact_late",
                )

            name_from_email = self._extract_name_from_email(email)
            domain = normalize_domain(email)
            vault_contexts = get_vault_contexts_for_domain(domain) if domain else []
            category = "work" if vault_contexts else "unknown"

            entity = PersonEntity(
                canonical_name=name_from_email,
                display_name=name_from_email,
                emails=[email.lower()],
                phone_numbers=[phone] if phone else [],
                phone_primary=phone,
                vault_contexts=vault_contexts,
                category=category,
                first_seen=None,  # Will be set from actual interactions
                last_seen=None,   # Will be set from actual interactions
            )

            stored = self._store.add(entity)

            return ResolutionResult(
                entity=stored,
                is_new=True,
                confidence=0.6,
                match_type="email_new",
            )

        return None

    def _extract_name_from_email(self, email: str) -> str:
        """
        Extract a display name from an email address.

        Args:
            email: Email address

        Returns:
            Best-guess name from email prefix
        """
        if not email or "@" not in email:
            return email or "Unknown"

        prefix = email.split("@")[0]

        # Handle common patterns
        # john.doe@... -> John Doe
        # johndoe@... -> Johndoe
        # john_doe@... -> John Doe
        # jdoe@... -> Jdoe

        # Replace separators with spaces
        name = re.sub(r"[._-]", " ", prefix)

        # Title case
        name = name.title()

        return name

    def resolve_from_linkedin(
        self,
        first_name: str,
        last_name: str,
        email: Optional[str],
        company: Optional[str],
        position: Optional[str],
        linkedin_url: Optional[str],
    ) -> ResolutionResult:
        """
        Resolve a person from LinkedIn data.

        Uses company normalization to infer email domains and vault contexts.

        Args:
            first_name: First name from LinkedIn
            last_name: Last name from LinkedIn
            email: Email if available
            company: Company name from LinkedIn
            position: Position/title
            linkedin_url: LinkedIn profile URL

        Returns:
            ResolutionResult with matched/created entity
        """
        full_name = f"{first_name} {last_name}".strip()

        # Try email first
        if email:
            entity = self.resolve_by_email(email)
            if entity:
                # Update with LinkedIn data
                entity.linkedin_url = linkedin_url or entity.linkedin_url
                entity.company = company or entity.company
                entity.position = position or entity.position
                if "linkedin" not in entity.sources:
                    entity.sources.append("linkedin")
                self._store.update(entity)

                return ResolutionResult(
                    entity=entity,
                    is_new=False,
                    confidence=1.0,
                    match_type="email_exact",
                )

        # Try to infer email domain from company
        vault_contexts = []
        if company:
            domains = get_domains_for_company(company)
            vault_contexts = get_vault_contexts_for_company(company)

            # Try to find existing entity by domain match
            query_parsed = parse_name(full_name)
            query_first = query_parsed.first.lower() if query_parsed.first else ""
            query_last = query_parsed.last.lower() if query_parsed.last else None

            for domain in domains:
                for entity in self._store.get_all():
                    for ent_email in entity.emails:
                        if ent_email.endswith(f"@{domain}"):
                            # Check if name matches using structured comparison
                            entity_parsed = parse_name(entity.canonical_name)
                            entity_first = entity_parsed.first.lower() if entity_parsed.first else ""
                            entity_last = entity_parsed.last.lower() if entity_parsed.last else None

                            # Require last name match (if both have last names)
                            last_match = True
                            if query_last and entity_last:
                                last_match = fuzz.ratio(query_last, entity_last) >= 85

                            # Require first name match
                            first_match = False
                            if query_first and entity_first:
                                first_match = fuzz.ratio(query_first, entity_first) >= 85

                            if last_match and first_match:
                                # Update with LinkedIn data
                                entity.linkedin_url = linkedin_url or entity.linkedin_url
                                entity.company = company or entity.company
                                entity.position = position or entity.position
                                if "linkedin" not in entity.sources:
                                    entity.sources.append("linkedin")
                                self._store.update(entity)

                                return ResolutionResult(
                                    entity=entity,
                                    is_new=False,
                                    confidence=0.85,
                                    match_type="linkedin_domain_match",
                                )

        # Try name matching
        result = self.resolve_by_name(full_name, create_if_missing=False)
        if result:
            entity = result.entity
            entity.linkedin_url = linkedin_url or entity.linkedin_url
            entity.company = company or entity.company
            entity.position = position or entity.position
            if "linkedin" not in entity.sources:
                entity.sources.append("linkedin")
            self._store.update(entity)
            return result

        # Create new entity
        category = "work" if vault_contexts else "unknown"

        entity = PersonEntity(
            canonical_name=full_name,
            display_name=full_name,
            emails=[email.lower()] if email else [],
            company=company,
            position=position,
            linkedin_url=linkedin_url,
            vault_contexts=vault_contexts,
            category=category,
            sources=["linkedin"],
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )

        stored = self._store.add(entity)

        return ResolutionResult(
            entity=stored,
            is_new=True,
            confidence=0.8,
            match_type="linkedin_new",
        )


# Singleton instance
_entity_resolver: Optional[EntityResolver] = None


def get_entity_resolver(
    entity_store: Optional[PersonEntityStore] = None,
) -> EntityResolver:
    """
    Get or create the singleton EntityResolver.

    Args:
        entity_store: PersonEntityStore to use

    Returns:
        EntityResolver instance
    """
    global _entity_resolver
    if _entity_resolver is None:
        _entity_resolver = EntityResolver(entity_store)
    return _entity_resolver
