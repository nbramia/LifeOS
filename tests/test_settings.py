"""Tests for configuration settings."""


def test_chroma_url_setting():
    """ChromaDB URL should be configurable."""
    from config.settings import settings

    # Default should be localhost:8001
    assert settings.chroma_url == "http://localhost:8001"
    assert hasattr(settings, 'chroma_url')
