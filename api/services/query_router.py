"""
Query Router for LifeOS.

Routes user queries to appropriate data sources using local LLM.
Falls back to keyword matching when Ollama is unavailable.

v3 additions:
- Populates relationship_context from CRM for person queries
- Determines fetch_depth based on query patterns and relationship strength
"""
import json
import re
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from api.services.ollama_client import OllamaClient, OllamaError
from api.services.model_selector import classify_query_complexity

logger = logging.getLogger(__name__)

# Valid data sources
VALID_SOURCES = {"vault", "calendar", "gmail", "drive", "people", "actions", "slack", "photos", "web"}

# Minimum relationship strength to be considered "strong" for disambiguation
DISAMBIGUATION_STRENGTH_THRESHOLD = 0.3

# Fetch depth limits for context retrieval
# Maps fetch_depth -> (email_char_limit, vault_chunks, message_limit)
FETCH_DEPTH_LIMITS = {
    "shallow": {
        "email_char_limit": 1500,
        "vault_chunks": 5,
        "message_limit": 50,
    },
    "normal": {
        "email_char_limit": 3000,
        "vault_chunks": 10,
        "message_limit": 100,
    },
    "deep": {
        "email_char_limit": 5000,
        "vault_chunks": 20,
        "message_limit": 200,
    },
}

# Load router prompt from file
PROMPT_FILE = Path(__file__).parent.parent.parent / "config" / "prompts" / "query_router.txt"


def _load_router_prompt() -> str:
    """Load the router prompt from file, with fallback."""
    try:
        return PROMPT_FILE.read_text()
    except FileNotFoundError:
        logger.warning(f"Router prompt file not found at {PROMPT_FILE}, using fallback")
        return """You are a query router. Classify the query by the data source(s) needed to answer the query.
Sources: vault (notes), calendar (events), gmail (email), drive (files), people (contacts), actions (tasks).
Respond with JSON only: {{"sources": ["vault"], "reasoning": "explanation"}}

Query: {query}"""


ROUTER_PROMPT = _load_router_prompt()


@dataclass
class RoutingResult:
    """Result of query routing decision."""
    sources: list[str]
    reasoning: str
    confidence: float
    latency_ms: int
    recommended_model: str = "sonnet"  # "haiku", "sonnet", or "opus"
    complexity_score: float = 0.5  # 0.0-1.0
    extracted_person_name: Optional[str] = None
    # v3: Orchestration intelligence
    fetch_depth: str = "normal"  # "shallow", "normal", "deep"
    min_results_threshold: int = 3  # Minimum chunks needed
    relationship_context: Optional[dict] = None  # CRM signals
    # v4: Compound query support
    action_after: Optional[str] = None  # "task_create", "reminder_create", "compose"


class QueryRouter:
    """
    Routes queries to appropriate data sources.

    Uses local Ollama LLM for intelligent routing with
    keyword-based fallback when unavailable.
    """

    def __init__(self, ollama_client: Optional[OllamaClient] = None):
        """
        Initialize query router.

        Args:
            ollama_client: Optional custom Ollama client (default creates new one)
        """
        self.ollama_client = ollama_client or OllamaClient()

    def _extract_person_name(self, query: str) -> Optional[str]:
        """Extract person name from a people-related query."""
        # Common words that should NOT be captured as part of a name
        # Includes verb forms that regex might accidentally capture
        stop_words = {'on', 'at', 'today', 'tomorrow', 'this', 'next', 'monday',
                      'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
                      'sunday', 'morning', 'afternoon', 'evening', 'week', 'about',
                      'last', 'month', 'year', 'recently', 'lately',
                      # Verb forms that patterns might accidentally capture
                      'been', 'being', 'be', 'am', 'is', 'are', 'was', 'were',
                      'do', 'does', 'did', 'have', 'has', 'had', 'just', 'ever'}

        patterns = [
            # Email patterns (allow lowercase names like "tay")
            r"email(?:ed)?\s+(?:to\s+)?([a-zA-Z]+)\s+(?:about|regarding)",
            r"(?:I\s+)?(?:sent|wrote|emailed)\s+(?:an?\s+)?(?:email\s+)?(?:to\s+)?([a-zA-Z]+)\s+(?:about|regarding|a\s+)",
            r"email\s+(?:I\s+)?sent\s+(?:to\s+)?([a-zA-Z]+)\s",
            r"email\s+(?:from|to)\s+([a-zA-Z]+)(?:\s|$)",
            # Meeting/call prep patterns
            r"prep(?:are)?\s+(?:me\s+)?for\s+(?:my\s+)?(?:meeting|call|1[:\-]1)\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"meeting\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            # Info/briefing patterns
            r"tell\s+me\s+about\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"who\s+is\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"background\s+on\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"brief(?:ing)?\s+(?:me\s+)?(?:on|for|about)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            # Knowledge/catch-up patterns
            r"what\s+do\s+I\s+know\s+about\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"catch\s+me\s+up\s+on\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"(?:update|fill)\s+me\s+(?:in\s+)?on\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"what(?:'s| is)\s+(?:the\s+)?(?:latest|status)\s+(?:with|on)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            # Photo/picture patterns
            r"(?:photo|picture|pic)s?\s+(?:of|with|of\s+me\s+(?:and|with))\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"in\s+a\s+(?:photo|picture|pic)\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            # Message/text patterns
            r"(?:text|message|sms)s?\s+(?:with|from|to)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"(?:texted?|messaged?)\s+(?:with\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"what\s+(?:did|have)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s+and\s+I",
            r"what\s+(?:did|have)\s+I\s+(?:and\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s+(?:discuss|talk|text|message)",
            # Conversation patterns
            r"conversation(?:s)?\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            r"discuss(?:ed|ions?)?\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            # Generic "with [Name]" — broad fallback, must be last
            r"with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s*(?:\?|$)",
            r"with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s+(?:last|this|in|about|recently|lately)",
        ]
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Skip if the entire captured name is a stop word
                if name.lower() in stop_words:
                    continue
                # Remove trailing stop words (captured due to IGNORECASE)
                words = name.split()
                while words and words[-1].lower() in stop_words:
                    words.pop()
                if words:
                    return ' '.join(words)
        return None

    def _fetch_crm_context(self, person_name: str) -> Optional[dict]:
        """
        Fetch CRM context for a person to inform routing decisions.

        Returns dict with:
        - entity_id: Person's entity ID
        - relationship_strength: 0-100 score
        - active_channels: List of recently active channels
        - email_count: Total emails
        - message_count: Total messages
        - primary_channel: Most used channel

        If resolution fails, may return dict with resolution_failed=True
        containing disambiguation candidates or fuzzy suggestions.

        Returns None only on errors.
        """
        try:
            from api.services.entity_resolver import get_entity_resolver
            from api.services.relationship_summary import get_relationship_summary
            from api.services.person_entity_store import get_person_entity_store

            resolver = get_entity_resolver()
            result = resolver.resolve(name=person_name)

            if not result or not result.entity:
                # No match - try to get suggestions
                suggestions = self._get_fuzzy_suggestions(person_name)
                if suggestions:
                    return {
                        "resolution_failed": True,
                        "failure_type": "no_match",
                        "query_name": person_name,
                        "suggestions": suggestions,
                    }
                logger.debug(f"Could not resolve person: {person_name}")
                return None

            entity = result.entity

            # Check for ambiguous matches (multiple strong candidates with similar names)
            if result.confidence and result.confidence < 0.9:
                # Look for other potential matches
                store = get_person_entity_store()
                candidates = store.search(name=person_name, limit=10)
                strong_matches = [
                    p for p in candidates
                    if p.relationship_strength >= DISAMBIGUATION_STRENGTH_THRESHOLD * 100
                    and self._names_match(person_name, p.canonical_name)
                ]

                if len(strong_matches) > 1:
                    # Multiple strong matches - return disambiguation options
                    return {
                        "resolution_failed": True,
                        "failure_type": "ambiguous",
                        "query_name": person_name,
                        "candidates": [
                            {
                                "id": p.id,
                                "name": p.canonical_name,
                                "strength": p.relationship_strength,
                                "context": self._get_disambiguation_context(p),
                            }
                            for p in strong_matches[:5]  # Max 5 options
                        ],
                    }

            summary = get_relationship_summary(entity.id)
            if not summary:
                # Return basic context even without relationship summary
                return {
                    "entity_id": entity.id,
                    "person_name": entity.canonical_name,
                    "relationship_strength": entity.relationship_strength,
                    "active_channels": [],
                    "email_count": entity.email_count,
                    "message_count": entity.message_count,
                    "primary_channel": None,
                }

            return {
                "entity_id": entity.id,
                "person_name": entity.canonical_name,
                "relationship_strength": summary.relationship_strength,
                "active_channels": summary.active_channels,
                "email_count": entity.email_count,
                "message_count": entity.message_count,
                "primary_channel": summary.primary_channel,
                "total_interactions_90d": summary.total_interactions_90d,
                "days_since_contact": summary.days_since_contact,
                "has_facts": summary.has_facts,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch CRM context for {person_name}: {e}")
            return None

    def _names_match(self, query: str, candidate: str) -> bool:
        """Check if names are similar enough to be the same person."""
        from rapidfuzz import fuzz
        q_lower = query.lower()
        c_lower = candidate.lower()
        # Exact match, starts with query, or high fuzzy match
        return (
            c_lower == q_lower
            or c_lower.startswith(q_lower)
            or q_lower in c_lower.split()
            or fuzz.ratio(q_lower, c_lower) >= 85
        )

    def _get_disambiguation_context(self, person) -> str:
        """Get contextual info to help distinguish this person."""
        parts = []
        if person.company:
            parts.append(person.company)
        if person.category:
            parts.append(person.category)
        if person.emails:
            # Show domain of first email
            email = person.emails[0]
            domain = email.split("@")[-1] if "@" in email else None
            if domain and domain not in ["gmail.com", "yahoo.com", "hotmail.com", "icloud.com"]:
                parts.append(domain)
        return " · ".join(parts) if parts else "No additional context"

    def _get_fuzzy_suggestions(self, name: str, threshold: int = 70) -> list[str]:
        """Get fuzzy name suggestions from CRM."""
        try:
            from api.services.person_entity_store import get_person_entity_store
            from rapidfuzz import fuzz

            store = get_person_entity_store()
            # Get top people by interaction count for suggestions
            people = store.list_all(limit=200)

            matches = []
            for person in people:
                score = fuzz.ratio(name.lower(), person.canonical_name.lower())
                if score >= threshold:
                    matches.append((person.canonical_name, score))

            matches.sort(key=lambda x: x[1], reverse=True)
            return [m[0] for m in matches[:3]]
        except Exception as e:
            logger.warning(f"Failed to get fuzzy suggestions: {e}")
            return []

    def _determine_fetch_depth(
        self,
        query: str,
        relationship_context: Optional[dict] = None
    ) -> str:
        """
        Determine fetch depth based on query patterns and relationship context.

        Returns: "shallow", "normal", or "deep"
        """
        query_lower = query.lower()

        # Deep patterns - explicit requests for full context
        deep_patterns = [
            "catch me up",
            "fill me in",
            "what's going on with",
            "what's happening with",
            "everything about",
            "full context",
            "deep dive",
            "comprehensive",
        ]
        if any(p in query_lower for p in deep_patterns):
            return "deep"

        # Shallow patterns - quick lookups
        shallow_patterns = [
            "what's their email",
            "phone number",
            "when did i last",
            "how long since",
            "quick question",
        ]
        if any(p in query_lower for p in shallow_patterns):
            return "shallow"

        # Relationship-based depth
        if relationship_context:
            strength = relationship_context.get("relationship_strength", 0)
            # High-strength relationships get more context by default
            if strength >= 70:
                return "deep"
            elif strength >= 40:
                return "normal"
            else:
                return "shallow"

        return "normal"

    async def route(self, query: str) -> RoutingResult:
        """
        Route a query to appropriate data sources.

        Args:
            query: The user's query text

        Returns:
            RoutingResult with sources, reasoning, and recommended model
        """
        start_time = time.time()

        # Check if Ollama is available
        if not self.ollama_client.is_available():
            logger.info("Ollama unavailable, using keyword fallback")
            result = self._keyword_fallback(query)
            result.latency_ms = int((time.time() - start_time) * 1000)
            # Add model selection
            complexity = classify_query_complexity(query, source_count=len(result.sources))
            result.recommended_model = complexity.recommended_model
            result.complexity_score = complexity.complexity_score
        else:
            # Try LLM routing
            try:
                result = await self._llm_route(query)
                result.latency_ms = int((time.time() - start_time) * 1000)
                # Add model selection
                complexity = classify_query_complexity(query, source_count=len(result.sources))
                result.recommended_model = complexity.recommended_model
                result.complexity_score = complexity.complexity_score
            except OllamaError as e:
                logger.warning(f"Ollama error, using fallback: {e}")
                result = self._keyword_fallback(query)
                result.latency_ms = int((time.time() - start_time) * 1000)
                # Add model selection
                complexity = classify_query_complexity(query, source_count=len(result.sources))
                result.recommended_model = complexity.recommended_model
                result.complexity_score = complexity.complexity_score

        # v3: Enrich with CRM context for people queries
        if "people" in result.sources:
            # Prefer LLM-extracted name (set by _llm_route), fall back to regex
            person_name = result.extracted_person_name or self._extract_person_name(query)
            if person_name:
                result.extracted_person_name = person_name
                crm_context = self._fetch_crm_context(person_name)
                if crm_context:
                    result.relationship_context = crm_context
                    result.fetch_depth = self._determine_fetch_depth(query, crm_context)
                    logger.info(
                        f"CRM context for {person_name}: "
                        f"strength={crm_context.get('relationship_strength', 0):.0f}, "
                        f"fetch_depth={result.fetch_depth}"
                    )
                else:
                    # No CRM context, use query-based depth
                    result.fetch_depth = self._determine_fetch_depth(query, None)
            else:
                result.fetch_depth = self._determine_fetch_depth(query, None)

        return result

    async def _llm_route(self, query: str) -> RoutingResult:
        """
        Route using the local LLM.

        Args:
            query: The user's query

        Returns:
            RoutingResult from LLM decision
        """
        prompt = ROUTER_PROMPT.format(query=query)
        response = await self.ollama_client.generate(prompt)

        # Try to parse JSON from response
        try:
            # Find JSON in response - handle nested braces by finding balanced braces
            json_str = self._extract_json(response)
            if not json_str:
                raise json.JSONDecodeError("No JSON found", response, 0)

            data = json.loads(json_str)
            sources = data.get("sources", [])
            reasoning = data.get("reasoning", "LLM routing")

            # Handle case where sources is list of strings OR list of objects
            if sources and isinstance(sources[0], dict):
                # Extract source names from objects like {"type": "Calendar"}
                sources = [s.get("type", "").lower() for s in sources if isinstance(s, dict)]
            elif sources and isinstance(sources[0], str):
                sources = [s.lower() for s in sources]

            # Validate sources
            valid_sources = [s for s in sources if s in VALID_SOURCES]
            if not valid_sources:
                # LLM returned invalid sources, use keyword fallback
                logger.warning(f"LLM returned no valid sources, using keyword fallback")
                return self._keyword_fallback(query)

            # Extract person name from LLM response
            people_mentioned = data.get("people_mentioned", [])
            extracted_name = None
            if people_mentioned and isinstance(people_mentioned, list):
                # Use the first person mentioned
                extracted_name = people_mentioned[0] if isinstance(people_mentioned[0], str) else None
                if extracted_name:
                    logger.info(f"LLM extracted person name: {extracted_name}")

            # Extract action_after for compound queries
            action_after = data.get("action_after")
            if action_after:
                logger.info(f"LLM detected action_after: {action_after}")

            result = RoutingResult(
                sources=valid_sources,
                reasoning=reasoning,
                confidence=0.9,  # High confidence for LLM
                latency_ms=0  # Will be set by caller
            )
            if extracted_name:
                result.extracted_person_name = extracted_name
            if action_after:
                result.action_after = action_after
            return result

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            # Use keyword fallback instead of defaulting to vault
            return self._keyword_fallback(query)

    def _extract_json(self, text: str) -> Optional[str]:
        """
        Extract JSON object from text, handling nested braces.

        Args:
            text: Text that may contain JSON

        Returns:
            Extracted JSON string or None
        """
        # Find the first opening brace
        start = text.find('{')
        if start == -1:
            return None

        # Count braces to find matching closing brace
        depth = 0
        for i, char in enumerate(text[start:], start):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i+1]

        return None

    def _keyword_fallback(self, query: str) -> RoutingResult:
        """
        Route using keyword matching (fallback).

        Args:
            query: The user's query

        Returns:
            RoutingResult from keyword matching
        """
        query_lower = query.lower()
        sources = set()
        reasons = []
        action_after = None

        # General knowledge patterns - Claude answers directly (empty sources)
        general_knowledge_patterns = [
            r"what is the capital of",
            r"what'?s the capital of",
            r"explain\b.*\bhow",
            r"how do(?:es)? .+ work",
            r"write (?:a |me )?(?:poem|haiku|story|song|limerick)",
            r"how (?:do i|to|can i) .+ in (?:python|javascript|java|c\+\+|rust|go|typescript)",
            r"what(?:'s| is) \d+.*(?:plus|minus|times|divided|percent|\+|\-|\*|\/|%)",
            r"^define\b",
            r"^what does .+ mean",
            r"^who (?:was|is) .+ (?:in history|historically)",
        ]
        is_general_knowledge = any(
            re.search(pattern, query_lower) for pattern in general_knowledge_patterns
        )

        # Web search keywords - current/local info needs web
        web_keywords = [
            "weather", "news", "price", "stock", "current",
            "hours", "near me", "zip code", "trash pickup",
            "recycling", "bus schedule", "train schedule",
            "what time does", "is .+ open", "store hours",
        ]
        is_web_query = any(kw in query_lower for kw in web_keywords[:6]) or any(
            re.search(kw, query_lower) for kw in web_keywords[6:]
        )

        # Compound query patterns - action after info gathering
        if re.search(r"(?:and|then)?\s*(?:remind|set a reminder)", query_lower):
            action_after = "reminder_create"
        elif re.search(r"(?:and|then)?\s*(?:add|create).*(?:task|to-?do)", query_lower):
            action_after = "task_create"
        elif re.search(r"(?:and|then)?\s*(?:draft|compose|write).*(?:email|message)", query_lower):
            action_after = "compose"

        # If it's a general knowledge query with no personal data indicators, return empty sources
        if is_general_knowledge and not is_web_query:
            return RoutingResult(
                sources=[],
                reasoning="General knowledge - Claude answers directly",
                confidence=0.8,
                latency_ms=0,
                action_after=action_after,
            )

        # If it needs web search
        if is_web_query:
            sources.add("web")
            reasons.append("web search keywords")

        # Calendar keywords
        calendar_keywords = [
            "meeting", "calendar", "schedule", "appointment",
            "when is", "what's on", "tomorrow", "today",
            "week", "month", "1-1", "1:1"
        ]
        if any(kw in query_lower for kw in calendar_keywords):
            sources.add("calendar")
            reasons.append("calendar keywords")

        # Email keywords
        email_keywords = [
            "email", "gmail", "inbox", "sent", "received",
            "mail", "message from", "wrote", "reply"
        ]
        if any(kw in query_lower for kw in email_keywords):
            sources.add("gmail")
            reasons.append("email keywords")

        # Drive keywords
        drive_keywords = [
            "drive", "spreadsheet", "document", "doc",
            "google doc", "sheet", "file", "slides"
        ]
        if any(kw in query_lower for kw in drive_keywords):
            sources.add("drive")
            reasons.append("drive keywords")

        # People keywords
        people_keywords = [
            "tell me about", "who is", "prep me for",
            "meeting with", "briefing", "background on",
            "text with", "texts with", "texted", "texting",
            "message with", "messages with", "messaged", "messaging",
            "discuss with", "discussed with", "talked with", "talking with",
            "talking about", "been talking",
            "conversation with", "conversations with",
            "and i discuss", "and i talk", "and i text",
            "did i text", "did i message", "sms with",
            "interactions with", "interaction with", "in touch with",
            "lately", "recently"
        ]
        if any(kw in query_lower for kw in people_keywords):
            sources.add("people")
            reasons.append("people keywords")
            # Also search vault for people context
            sources.add("vault")
            # Also search calendar for meeting history
            sources.add("calendar")
            # Also search gmail for email communications
            sources.add("gmail")
            # Also search slack for DM communications
            sources.add("slack")

        # Slack keywords
        slack_keywords = [
            "slack", "dm ", "dms", "direct message",
            "slack message", "slack conversation",
            "on slack", "in slack", "said in slack",
            "slacked", "slacking"
        ]
        if any(kw in query_lower for kw in slack_keywords):
            sources.add("slack")
            reasons.append("slack keywords")

        # Photos keywords
        photos_keywords = [
            "photo", "photos", "picture", "pictures", "pic", "pics",
            "selfie", "selfies", "in a photo", "in a picture",
        ]
        if any(kw in query_lower for kw in photos_keywords):
            sources.add("photos")
            sources.add("people")  # Photos are person-linked
            reasons.append("photos keywords")

        # Action keywords
        action_keywords = [
            "action item", "todo", "task", "commitment",
            "open items", "what did i commit", "follow up"
        ]
        if any(kw in query_lower for kw in action_keywords):
            sources.add("actions")
            reasons.append("action keywords")

        # Default to vault if no specific sources matched
        if not sources:
            sources.add("vault")
            reasons.append("default vault search")

        return RoutingResult(
            sources=list(sources),
            reasoning=f"Keyword fallback: {', '.join(reasons)}",
            confidence=0.7,
            latency_ms=0,
            action_after=action_after,
        )
