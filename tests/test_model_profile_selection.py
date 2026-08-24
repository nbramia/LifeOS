from types import SimpleNamespace


def test_natural_model_profile_directive_is_explicit_only():
    from api.routes.chat import _requested_model_profile

    profiles = {"default": object(), "gemini": object(), "reasoning": object()}

    assert _requested_model_profile("Use Gemini for this.", profiles) == "gemini"
    assert _requested_model_profile("Use the strongest model for this turn.", profiles) == "reasoning"
    assert _requested_model_profile("I use Gemini for my project", profiles) is None
    assert _requested_model_profile("Use Claude for this.", profiles) is None


def test_agent_loop_selects_named_profile(monkeypatch):
    import api.services.llm_client as llm_client
    from api.services.agent_loop import _select_client

    sentinel = SimpleNamespace(model="gemini-2.5-flash")
    calls = []
    monkeypatch.setattr(llm_client, "get_llm", lambda **kwargs: calls.append(kwargs) or sentinel)

    assert _select_client(model_profile="gemini") is sentinel
    assert calls == [{"profile": "gemini"}]
