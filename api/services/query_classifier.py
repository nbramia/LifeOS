"""
Query type classifier for LifeOS.

Detects whether a query is "factual" (exact lookup) or "semantic" (discovery/conceptual).
Used by hybrid search to determine reranking strategy.

## Query Types

- **Factual**: Exact lookups (person's info, codes, IDs)
  - Reranker protects top BM25 matches
  - Examples: "Taylor's KTN", "Alex's phone"

- **Semantic**: Discovery and conceptual queries
  - Full cross-encoder reranking
  - Examples: "prepare me for meeting", "what files discuss budget"
"""
import re
import logging
from typing import Literal

logger = logging.getLogger(__name__)

# Keywords that indicate factual/identifier lookups
IDENTIFIER_KEYWORDS = {
    "passport", "ktn", "ssn", "phone", "email", "address",
    "birthday", "number", "id", "code", "pin", "account"
}

# Action verbs that indicate semantic queries
ACTION_VERBS = {
    "prepare", "summarize", "brief", "analyze", "review",
    "explain", "describe", "tell", "help", "find", "show"
}

# Discovery words that indicate semantic queries
DISCOVERY_WORDS = {
    "files", "documents", "notes", "about", "regarding",
    "related", "discuss", "mention", "contain", "cover"
}


def classify_query(query: str) -> Literal["factual", "semantic"]:
    """
    Classify a query as factual or semantic.

    Args:
        query: Search query string

    Returns:
        "factual" for exact lookups, "semantic" for discovery queries
    """
    query_lower = query.lower()
    words = query_lower.split()

    # Check for possessive patterns with proper nouns
    possessive_pattern = r"(\w+)'s\s+(\w+)"
    possessive_match = re.search(possessive_pattern, query_lower)

    if possessive_match:
        # Check if followed by identifier keyword
        following_word = possessive_match.group(2)
        if following_word in IDENTIFIER_KEYWORDS:
            logger.debug(f"Factual: possessive + identifier '{following_word}'")
            return "factual"

        # Check if the name is known (from ALIAS_MAP)
        try:
            from api.services.people import ALIAS_MAP
            name = possessive_match.group(1)
            if name in ALIAS_MAP or name.capitalize() in ALIAS_MAP:
                logger.debug(f"Factual: known person '{name}'")
                return "factual"
        except ImportError:
            pass

    # Check for known person names anywhere in query
    try:
        from api.services.people import ALIAS_MAP
        for word in words:
            clean = re.sub(r"[''`]s?$", "", word)  # Remove possessive
            if clean in ALIAS_MAP or clean.capitalize() in ALIAS_MAP:
                # Short query with known name = factual
                if len(words) <= 5:
                    logger.debug(f"Factual: short query with known name '{clean}'")
                    return "factual"
    except ImportError:
        pass

    # Check for identifier keywords
    if any(kw in words for kw in IDENTIFIER_KEYWORDS):
        # Short query with identifier = factual
        if len(words) <= 5:
            logger.debug(f"Factual: identifier keyword in short query")
            return "factual"

    # Check for action verbs (semantic indicators)
    if any(verb in words for verb in ACTION_VERBS):
        logger.debug(f"Semantic: action verb detected")
        return "semantic"

    # Check for discovery words (semantic indicators)
    if any(word in words for word in DISCOVERY_WORDS):
        logger.debug(f"Semantic: discovery word detected")
        return "semantic"

    # Long queries tend to be semantic
    if len(words) > 8:
        logger.debug(f"Semantic: long query ({len(words)} words)")
        return "semantic"

    # Default: short/medium queries without clear signals = factual
    # (better to preserve exact matches when unsure)
    logger.debug(f"Factual: default for ambiguous query")
    return "factual"
