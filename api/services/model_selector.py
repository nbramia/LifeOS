"""
Smart Model Selection for LifeOS (P6.1).

Selects appropriate Claude model (Haiku/Sonnet/Opus) based on query complexity.
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Claude model identifiers (using latest aliases for automatic updates)
CLAUDE_MODELS = {
    "haiku": "claude-sonnet-4-5-20250929",  # Use Sonnet 4.5 as minimum (Haiku 4.5 may not be available)
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-5-20251124",
}

# Approximate costs per query (for reference)
MODEL_COSTS = {
    "haiku": 0.001,   # ~$0.001 per query
    "sonnet": 0.01,   # ~$0.01 per query
    "opus": 0.10,     # ~$0.05-0.10 per query
}

# Complexity classification keywords
HAIKU_KEYWORDS = [
    # Simple lookups
    "what time", "when is", "who is", "where is",
    "list my", "show me", "find the", "get the",
    # Direct questions
    "how many", "what is the", "is there",
    # Simple commands
    "show", "list", "find", "get",
]

SONNET_KEYWORDS = [
    # Standard synthesis
    "summarize", "explain", "describe", "overview",
    "what happened", "tell me about", "what did",
    "how did", "why did", "recap",
    # General questions requiring synthesis
    "what were", "how was", "details about",
]

OPUS_KEYWORDS = [
    # Complex reasoning
    "analyze", "compare", "evaluate", "assess",
    "strategy", "strategic", "implications", "impact",
    "trade-offs", "tradeoffs", "pros and cons",
    "recommend", "advise", "suggest approach",
    # Multi-step reasoning
    "step by step", "think through", "consider",
    "second-order", "third-order", "downstream",
    # Deep analysis
    "patterns", "themes", "connections", "relationships",
    "synthesis", "integrate", "comprehensive",
]


@dataclass
class ComplexityResult:
    """Result of complexity classification."""
    recommended_model: str  # "haiku", "sonnet", or "opus"
    complexity_score: float  # 0.0-1.0
    reasoning: str


def classify_query_complexity(
    query: str,
    source_count: int = 1,
    context_tokens: int = 1000
) -> ComplexityResult:
    """
    Classify query complexity and recommend appropriate model.

    Args:
        query: The user's query text
        source_count: Number of data sources involved
        context_tokens: Approximate number of context tokens

    Returns:
        ComplexityResult with recommended model and score
    """
    query_lower = query.lower()
    reasons = []

    # Check for keywords in each category
    opus_matches = sum(1 for kw in OPUS_KEYWORDS if kw in query_lower)
    sonnet_matches = sum(1 for kw in SONNET_KEYWORDS if kw in query_lower)
    haiku_matches = sum(1 for kw in HAIKU_KEYWORDS if kw in query_lower)

    # Start with keyword-based scoring (primary signal)
    # Opus keywords are strong signals - override most other considerations
    if opus_matches > 0:
        reasons.append(f"complex reasoning keywords ({opus_matches})")

    if sonnet_matches > 0:
        reasons.append(f"synthesis keywords ({sonnet_matches})")

    if haiku_matches > 0:
        reasons.append(f"simple lookup keywords ({haiku_matches})")

    # Query length (secondary signal)
    word_count = len(query.split())
    if word_count <= 5:
        reasons.append("short query")
    elif word_count > 15:
        reasons.append("long query")

    # Source count factor (can upgrade the model)
    if source_count >= 4:
        reasons.append(f"many sources ({source_count})")
    elif source_count == 1:
        reasons.append("single source")

    # Context size factor (can upgrade the model)
    if context_tokens >= 8000:
        reasons.append(f"large context ({context_tokens} tokens)")

    # Decision logic: keywords take precedence, then context factors
    recommended_model = "sonnet"  # Default
    complexity_score = 0.5

    # Opus: complex reasoning keywords are the primary signal
    if opus_matches >= 1:
        recommended_model = "opus"
        complexity_score = 0.7 + min(opus_matches * 0.1, 0.3)

    # Large context bumps to opus regardless of keywords
    elif context_tokens >= 8000:
        recommended_model = "opus"
        complexity_score = 0.75

    # Many sources bumps to at least sonnet, possibly opus
    elif source_count >= 4:
        if sonnet_matches > 0 or opus_matches > 0:
            recommended_model = "opus"
            complexity_score = 0.7
        else:
            recommended_model = "sonnet"
            complexity_score = 0.5

    # Sonnet: synthesis keywords without opus keywords
    elif sonnet_matches >= 1 and opus_matches == 0:
        recommended_model = "sonnet"
        complexity_score = 0.4 + min(sonnet_matches * 0.1, 0.2)

    # Haiku: simple lookups with no synthesis/opus keywords
    elif haiku_matches >= 1 and sonnet_matches == 0 and opus_matches == 0:
        # Additional check: simple question pattern
        if re.match(r'^(what time|when is|who is|where is|list|show|find|get)\s', query_lower):
            recommended_model = "haiku"
            complexity_score = 0.1 + min(haiku_matches * 0.05, 0.15)
        else:
            # Has haiku keywords but not a simple pattern - use sonnet to be safe
            recommended_model = "sonnet" if word_count > 5 else "haiku"
            complexity_score = 0.3 if recommended_model == "sonnet" else 0.2

    # Short query with no keywords - likely simple
    elif word_count <= 5 and sonnet_matches == 0 and opus_matches == 0:
        recommended_model = "haiku"
        complexity_score = 0.2
        reasons.append("short simple query")

    # Default case: sonnet is the safe default
    else:
        recommended_model = "sonnet"
        complexity_score = 0.5

    # Final overrides for strong signals
    # Multiple opus keywords is a very strong signal
    if opus_matches >= 2:
        recommended_model = "opus"
        complexity_score = max(complexity_score, 0.8)

    reasoning = "; ".join(reasons) if reasons else "general query"

    return ComplexityResult(
        recommended_model=recommended_model,
        complexity_score=round(min(max(complexity_score, 0.0), 1.0), 2),
        reasoning=reasoning
    )


def get_claude_model_name(model_tier: str) -> str:
    """
    Get the full Claude model name for a tier.

    Args:
        model_tier: "haiku", "sonnet", or "opus"

    Returns:
        Full Claude model identifier
    """
    return CLAUDE_MODELS.get(model_tier.lower(), CLAUDE_MODELS["sonnet"])


class ModelSelector:
    """
    Service for selecting appropriate Claude model based on query complexity.
    """

    def __init__(self):
        """Initialize the model selector."""
        pass

    def select_model(
        self,
        query: str,
        source_count: int = 1,
        context_tokens: int = 1000
    ) -> ComplexityResult:
        """
        Select the appropriate model for a query.

        Args:
            query: The user's query text
            source_count: Number of data sources involved
            context_tokens: Approximate number of context tokens

        Returns:
            ComplexityResult with recommended model and reasoning
        """
        return classify_query_complexity(query, source_count, context_tokens)

    def get_model_name(self, tier: str) -> str:
        """Get full Claude model name for a tier."""
        return get_claude_model_name(tier)


# Singleton instance
_model_selector: Optional[ModelSelector] = None


def get_model_selector() -> ModelSelector:
    """Get or create ModelSelector singleton."""
    global _model_selector
    if _model_selector is None:
        _model_selector = ModelSelector()
    return _model_selector


def reset_model_selector() -> None:
    """
    Reset the model selector singleton.

    For testing only - allows tests to start with fresh state.
    """
    global _model_selector
    _model_selector = None
