"""Tests for configuration settings."""


def test_chroma_url_setting():
    """ChromaDB URL should be configurable."""
    from config.settings import settings

    # Default should be localhost:8001
    assert settings.chroma_url == "http://localhost:8001"
    assert hasattr(settings, 'chroma_url')


def test_local_llm_autostart_defaults_false():
    """
    Local LLM autostart should default to False.

    Asserts the FIELD default, not a live instance. ``Settings()`` reads the
    local .env, so this used to assert whatever the developer's machine had
    configured — it failed on any host that sets LIFEOS_LOCAL_LLM_AUTOSTART and
    passed everywhere else, which is the opposite of what it claims to check.
    """
    from config.settings import Settings

    assert Settings.model_fields["local_llm_autostart"].default is False


def test_local_llm_model_default():
    """
    The shipped local-LLM default is a deliberate pin — update it here when the
    default model changes. It drifted silently once already: the assertion
    still named gpt-oss-120b long after the default moved to Gemma, so this
    test failed for everyone, including a fresh clone.

    Reads the field default so a host-level LIFEOS_LLM_MODEL override doesn't
    turn a local config choice into a test failure.
    """
    from config.settings import Settings

    assert (
        Settings.model_fields["local_llm_model"].default
        == "unsloth/gemma-4-26B-A4B-it-GGUF"
    )


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


def test_routing_llm_url_falls_back_to_local_llm_url_when_unset(monkeypatch):
    """Routing target URL defaults to the global local LLM URL when unset, so
    a fresh clone with no override behaves exactly as today (#566 PR 2)."""
    monkeypatch.delenv("LIFEOS_LOCAL_ROUTING_LLM_URL", raising=False)
    monkeypatch.setenv("LIFEOS_LOCAL_LLM_URL", "http://localhost:8080")
    from config.settings import Settings
    s = Settings()
    assert s.routing_llm_url == "http://localhost:8080"


def test_routing_llm_url_override(monkeypatch):
    """An explicit LIFEOS_LOCAL_ROUTING_LLM_URL wins over local_llm_url."""
    monkeypatch.setenv("LIFEOS_LOCAL_ROUTING_LLM_URL", "http://routing-box:9090")
    from config.settings import Settings
    s = Settings()
    assert s.routing_llm_url == "http://routing-box:9090"


def test_router_enable_thinking_defaults_true():
    """Router thinking stays on by default (#566 PR 2 does not flip
    behaviour) — the eventual flip is a one-line default change here."""
    from config.settings import Settings
    assert Settings.model_fields["router_enable_thinking"].default is True


def test_local_agent_enable_thinking_defaults_true():
    """The orchestrator's local-model thinking control (#567) defaults True —
    current behaviour, unchanged — mirroring router_enable_thinking (#566)."""
    from config.settings import Settings
    assert Settings.model_fields["local_agent_enable_thinking"].default is True


def test_specialist_model_default_is_current_alias():
    """#470 regression pin: the specialist-call model must be a model ALIAS,
    never a dated snapshot. The previous pin (claude-sonnet-4-20250514)
    retired and returned 404 on every relationship-insights / fact-extraction /
    tone-analysis call — silently, since callers swallow per-item errors.
    Aliases track the serving model and don't retire out from under us.

    Asserts the FIELD default (env-independent), not the live instance — a
    host may legitimately override via LIFEOS_ANTHROPIC_SPECIALIST_MODEL.
    """
    import re

    from config.settings import Settings

    default = Settings.model_fields["anthropic_specialist_model"].default
    assert default == "claude-sonnet-5"
    assert not re.search(r"-20\d{6}$", default), (
        "specialist model default is a dated snapshot — pin an alias instead "
        "(snapshots retire and 404)"
    )


def test_orchestrator_model_default_is_alias_not_snapshot():
    """Same rule for the orchestrator default (#470 guard, defense in depth)."""
    import re

    from config.settings import Settings

    default = Settings.model_fields["anthropic_model"].default
    assert not re.search(r"-20\d{6}$", default)
