"""Tests for configuration settings."""


def test_chroma_url_setting():
    """ChromaDB URL should be configurable."""
    from config.settings import settings

    # Default should be localhost:8001
    assert settings.chroma_url == "http://localhost:8001"
    assert hasattr(settings, 'chroma_url')


def test_local_llm_autostart_defaults_false():
    """Local LLM autostart should default to False."""
    from config.settings import Settings
    s = Settings()
    assert s.local_llm_autostart is False


def test_local_llm_model_default():
    """Local LLM model should default to gpt-oss-120b."""
    from config.settings import Settings
    s = Settings()
    assert s.local_llm_model == "ggml-org/gpt-oss-120b-GGUF"


def test_local_llm_autostart_from_env(monkeypatch):
    """Local LLM autostart should be configurable via env var."""
    monkeypatch.setenv("LIFEOS_LOCAL_LLM_AUTOSTART", "true")
    from config.settings import Settings
    s = Settings()
    assert s.local_llm_autostart is True


def test_local_llm_model_from_env(monkeypatch):
    """Local LLM model should be configurable via env var."""
    monkeypatch.setenv("LIFEOS_LLM_MODEL", "some-org/qwen3-32b-GGUF")
    from config.settings import Settings
    s = Settings()
    assert s.local_llm_model == "some-org/qwen3-32b-GGUF"
