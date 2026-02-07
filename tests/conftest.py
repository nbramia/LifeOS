"""
Pytest configuration and shared fixtures for LifeOS tests.

Test Categories:
- unit: Fast tests with no external dependencies (< 100ms each)
- slow: Tests requiring ChromaDB, sentence-transformers, or file watchers
- integration: Tests requiring running server or external APIs
- browser: Playwright browser tests
- requires_ollama: Tests requiring Ollama LLM to be running
- requires_server: Tests requiring API server to be running

Run categories:
- pytest -m unit              # Fast unit tests only (~60s)
- pytest -m "not slow"        # Skip slow tests
- pytest -m "not integration" # Skip integration tests
- pytest -m browser           # Browser tests only
- pytest                      # All tests
- pytest -n auto              # Parallel execution (requires pytest-xdist)
"""
import pytest
import os


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: Fast unit tests")
    config.addinivalue_line("markers", "slow: Slow tests (ChromaDB, embeddings)")
    config.addinivalue_line("markers", "integration: Integration tests (server required)")
    config.addinivalue_line("markers", "browser: Browser tests using Playwright")
    config.addinivalue_line("markers", "requires_ollama: Requires Ollama running")
    config.addinivalue_line("markers", "requires_server: Requires API server running")
    config.addinivalue_line("markers", "requires_db: Requires direct database access (may conflict with running server)")


@pytest.fixture(scope="session")
def ollama_available():
    """Check if Ollama is available for tests."""
    try:
        import httpx
        response = httpx.get("http://localhost:11434", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def server_available():
    """Check if API server is available for tests."""
    try:
        import httpx
        response = httpx.get("http://localhost:8000/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_available():
    """
    Check if the interactions database is available for direct access.

    When the server is running, it may hold a lock on the SQLite database,
    preventing tests from accessing it directly. This fixture detects that
    situation and allows tests to skip gracefully.
    """
    import sqlite3
    try:
        from api.services.interaction_store import get_interaction_db_path
        db_path = get_interaction_db_path()
        conn = sqlite3.connect(db_path, timeout=1.0)
        # Try to execute a simple query to check for lock
        conn.execute("SELECT 1 FROM interactions LIMIT 1")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False
    except Exception:
        return False


@pytest.fixture(autouse=False)
def require_db(db_available):
    """Skip test if database is not available (e.g., locked by running server)."""
    if not db_available:
        pytest.skip("Database is locked (server may be running). Stop server to run these tests.")


@pytest.fixture(scope="session")
def test_vault_path(tmp_path_factory):
    """
    Create a temporary vault directory for tests.

    Session-scoped to avoid recreating for every test.
    """
    vault = tmp_path_factory.mktemp("test_vault")
    # Create standard folders
    (vault / "Granola").mkdir()
    (vault / "Work").mkdir()
    (vault / "Personal").mkdir()
    return vault


@pytest.fixture(scope="session")
def test_data_path(tmp_path_factory):
    """
    Create a temporary data directory for ChromaDB and SQLite.

    Session-scoped to share across tests that need persistence.
    """
    return tmp_path_factory.mktemp("test_data")


@pytest.fixture(scope="function")
def mock_settings(test_vault_path, test_data_path, monkeypatch):
    """
    Mock settings for testing.

    Uses temporary paths to avoid affecting real data.
    """
    from config.settings import Settings

    mock = Settings(
        vault_path=test_vault_path,
        chroma_path=test_data_path / "chromadb",
        anthropic_api_key="test-key-for-testing",
    )

    # Patch the global settings
    monkeypatch.setattr("config.settings.settings", mock)
    return mock


@pytest.fixture(scope="session")
def embedding_service():
    """
    Session-scoped embedding service to avoid repeated model loading.

    Loading sentence-transformers is slow (~2s), so we share one instance.
    """
    try:
        from api.services.embeddings import EmbeddingService
        return EmbeddingService()
    except Exception:
        pytest.skip("Embedding service not available")


# Parallel execution configuration
def pytest_collection_modifyitems(config, items):
    """
    Auto-mark tests based on location/name for better organization.

    Tests in test_*_api.py get the 'unit' marker by default.
    Tests with 'browser' in name get the 'browser' marker.
    """
    for item in items:
        # Browser tests
        if "browser" in item.name or "playwright" in str(item.fspath):
            item.add_marker(pytest.mark.browser)

        # Integration tests (those that hit real servers)
        if "integration" in item.name or "real_" in item.name:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def production_test_data():
    """
    Load production test data if available, else return None.

    This fixture allows tests to optionally use real production data
    (gitignored) while still passing with generic data in open-source.
    """
    try:
        from tests.fixtures.production_test_data import (
            WORK_DOMAIN,
            TEST_WORK_CONTACT,
            COLLEAGUE_NAMES,
            TEST_PERSONAL_CONTACT,
            TEST_FAMILY_CONTACT,
        )
        return {
            "work_domain": WORK_DOMAIN,
            "test_work_contact": TEST_WORK_CONTACT,
            "colleagues": COLLEAGUE_NAMES,
            "test_personal_contact": TEST_PERSONAL_CONTACT,
            "test_family_contact": TEST_FAMILY_CONTACT,
        }
    except ImportError:
        return None
