"""
Cross-encoder re-ranking service for LifeOS.

Uses a cross-encoder model to re-rank search results by computing
query-document relevance scores. Much more accurate than bi-encoder
similarity because it sees query and document together.

## How It Works

Cross-encoders differ from bi-encoders:
- Bi-encoder: Embed query and document separately, compute similarity
- Cross-encoder: Process (query, document) pair together for direct relevance

This allows cross-encoders to catch nuances that bi-encoders miss,
like negation, specificity, and contextual relevance.

## Usage

    from api.services.reranker import get_reranker
    reranker = get_reranker()
    reranked = reranker.rerank(query, results, top_k=10)
"""
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)


class RerankerService:
    """
    Cross-encoder re-ranking service.

    Lazy-loads model on first use to avoid slow startup.
    Caches model in memory for subsequent calls.
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L6-v2"):
        """
        Initialize reranker service.

        Args:
            model_name: HuggingFace model name for cross-encoder.
                       Default is ms-marco-MiniLM-L6-v2 (~90MB, fast, accurate).
        """
        self.model_name = model_name
        self._model: Optional["CrossEncoder"] = None

    def _get_model(self) -> "CrossEncoder":
        """Lazy-load cross-encoder model."""
        if self._model is None:
            from sentence_transformers import CrossEncoder
            logger.info(f"Loading cross-encoder model: {self.model_name}")
            self._model = CrossEncoder(self.model_name)
            logger.info("Cross-encoder model loaded")
        return self._model

    def rerank(
        self,
        query: str,
        results: list[dict],
        top_k: int = 10,
        content_key: str = "content",
        protected_indices: list[int] | None = None
    ) -> list[dict]:
        """
        Re-rank search results using cross-encoder.

        Args:
            query: Search query string
            results: List of search results with content
            top_k: Number of results to return after re-ranking
            content_key: Key in result dict containing text to score
            protected_indices: Indices of results to protect from reranking.
                              Protected results appear first in their original order.

        Returns:
            Re-ranked results with cross_encoder_score added
        """
        if not results:
            return []

        # Separate protected and unprotected results
        protected_results = []
        unprotected_results = []

        if protected_indices:
            protected_set = set(protected_indices)
            for i, r in enumerate(results):
                if i in protected_set:
                    # Add cross_encoder_score using hybrid_score and mark as protected
                    r["cross_encoder_score"] = r.get("hybrid_score", 0.5)
                    r["protected"] = True
                    protected_results.append((i, r))  # Keep original index for ordering
                else:
                    unprotected_results.append(r)
            # Sort protected results by their original index to maintain order
            protected_results.sort(key=lambda x: x[0])
            protected_results = [r for _, r in protected_results]
        else:
            unprotected_results = results

        # If no unprotected results, return protected ones
        if not unprotected_results:
            return protected_results[:top_k]

        # Calculate how many unprotected results we need
        unprotected_needed = top_k - len(protected_results)

        if unprotected_needed <= 0:
            # Protected results fill the entire top_k
            return protected_results[:top_k]

        # Handle small result sets - don't load model if not enough to rerank
        if len(unprotected_results) <= unprotected_needed:
            # Not enough results to re-rank meaningfully
            # Still add cross_encoder_score for consistency
            for r in unprotected_results:
                r["cross_encoder_score"] = r.get("hybrid_score", 0.5)
            return protected_results + unprotected_results

        model = self._get_model()

        # Prepare query-document pairs for unprotected results only
        pairs = [(query, r.get(content_key, "")) for r in unprotected_results]

        # Score all pairs
        try:
            scores = model.predict(pairs, show_progress_bar=False)
        except Exception as e:
            logger.error(f"Cross-encoder scoring failed: {e}")
            # Fall back to original ranking
            for r in unprotected_results:
                r["cross_encoder_score"] = r.get("hybrid_score", 0.5)
            return (protected_results + unprotected_results)[:top_k]

        # Add scores to unprotected results
        for result, score in zip(unprotected_results, scores):
            result["cross_encoder_score"] = float(score)

        # Sort unprotected by cross-encoder score (descending)
        reranked_unprotected = sorted(
            unprotected_results, key=lambda x: -x["cross_encoder_score"]
        )

        # Combine: protected first, then reranked unprotected
        combined = protected_results + reranked_unprotected

        if combined:
            logger.debug(
                f"Reranked {len(unprotected_results)} unprotected results, "
                f"protected {len(protected_results)} results, "
                f"top score: {combined[0]['cross_encoder_score']:.3f}"
            )

        return combined[:top_k]

    def is_model_loaded(self) -> bool:
        """Check if model is already loaded."""
        return self._model is not None


# Singleton instance
_reranker_instance: Optional[RerankerService] = None


def get_reranker() -> RerankerService:
    """Get singleton reranker instance."""
    global _reranker_instance
    if _reranker_instance is None:
        from config.settings import settings
        model_name = getattr(settings, "reranker_model", "cross-encoder/ms-marco-MiniLM-L6-v2")
        _reranker_instance = RerankerService(model_name=model_name)
    return _reranker_instance
