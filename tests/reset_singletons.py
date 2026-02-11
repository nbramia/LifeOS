"""
Centralized singleton reset utilities for testing.

These functions reset global singleton instances to prevent test pollution.
Singletons that persist across tests can cause:
- Stale data from previous tests
- Mock objects leaking between tests
- Settings changes not taking effect

Usage in conftest.py:
    @pytest.fixture(autouse=True)
    def reset_singletons_after_test():
        yield
        reset_lightweight_singletons()

Usage in specific tests:
    from tests.reset_singletons import reset_all_singletons
    reset_all_singletons()
"""


def reset_lightweight_singletons() -> None:
    """
    Reset fast singletons (no model reloading).

    Safe to call after every test. Resets:
    - ServiceHealthRegistry
    - ModelSelector
    - ConversationStore
    - HybridSearch
    - BM25Index

    Does NOT reset embedding service (causes slow model reload).
    """
    from api.services.service_health import reset_service_health
    from api.services.model_selector import reset_model_selector
    from api.services.conversation_store import reset_conversation_store
    from api.services.hybrid_search import reset_hybrid_search
    from api.services.bm25_index import reset_bm25_index

    reset_service_health()
    reset_model_selector()
    reset_conversation_store()
    reset_hybrid_search()
    reset_bm25_index()


def reset_ml_singletons() -> None:
    """
    Reset ML-related singletons (causes model reload).

    Only call when necessary (e.g., end of test session).
    Resets:
    - EmbeddingService (~2s reload)
    """
    from api.services.embeddings import reset_embedding_service

    reset_embedding_service()


def reset_all_singletons() -> None:
    """
    Reset all singletons (includes slow ML model reloads).

    Use sparingly - prefer reset_lightweight_singletons() for most tests.
    """
    reset_lightweight_singletons()
    reset_ml_singletons()
