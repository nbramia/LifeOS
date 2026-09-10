"""Contract tests for the narrow persona-to-MCP capability projection."""
import pytest

from api.services import persona_capabilities as capabilities

pytestmark = pytest.mark.unit


def test_fitness_projection_is_registered_permitted_and_action_complete():
    """The fitness projection mirrors the executable native tool and the
    live MCP/OpenAPI-built registration, including every supported action.
    """
    capabilities.validate_capability_contract()
    projected = capabilities.project_persona_tool_capabilities("fitness")

    assert len(projected) == 1
    workout = projected[0]
    assert workout.native_name == "manage_workouts"
    assert workout.mcp_name == "lifeos_workout_manage"
    assert workout.actions == (
        "log", "update", "list", "history", "summary", "log_metric",
        "metrics", "get_profile", "set_profile", "readiness",
    )
    assert workout.actions == capabilities._native_actions(workout.native_name)
    assert workout.actions == capabilities._mcp_actions(workout.mcp_name, workout.actions)


def test_ungranted_persona_does_not_inherit_a_registered_tool():
    """Only explicitly mapped personas receive projected capabilities."""
    assert capabilities.project_persona_tool_capabilities("primary") == ()
    assert capabilities.project_persona_tool_capabilities("synthetic") == ()


def test_temporarily_disabled_native_handler_is_omitted_without_breaking_others(
    monkeypatch,
):
    """A missing native dispatcher removes only that persona's projection."""
    from api.services import agent_tools

    monkeypatch.delitem(agent_tools._TOOL_HANDLERS, "manage_workouts")

    assert capabilities.project_persona_tool_capabilities("fitness") == ()
    assert capabilities.project_persona_tool_capabilities("primary") == ()


def test_absent_openapi_route_is_omitted_without_breaking_envelope(monkeypatch):
    """A curated workout entry cannot advertise actions when its route is
    absent from the live OpenAPI-built MCP tool list.
    """
    from copy import deepcopy
    from api.main import app

    openapi_spec = deepcopy(app.openapi())
    openapi_spec["paths"].pop("/api/fitness/workouts", None)
    monkeypatch.setattr(app, "openapi", lambda: openapi_spec)

    assert capabilities.project_persona_tool_capabilities("fitness") == ()
    assert capabilities.project_persona_tool_capabilities("primary") == ()


def test_absent_openapi_operation_is_omitted_without_breaking_envelope(monkeypatch):
    """A path without POST cannot advertise fitness actions."""
    from copy import deepcopy
    from api.main import app

    openapi_spec = deepcopy(app.openapi())
    openapi_spec["paths"]["/api/fitness/workouts"].pop("post")
    monkeypatch.setattr(app, "openapi", lambda: openapi_spec)

    assert capabilities.project_persona_tool_capabilities("fitness") == ()
    assert capabilities.project_persona_tool_capabilities("primary") == ()


def test_renamed_mcp_catalog_target_fails_the_projection_contract(monkeypatch):
    """Alias drift fails strict validation while live projection degrades."""
    monkeypatch.setitem(
        capabilities.NATIVE_TO_MCP_ALIASES,
        "manage_workouts",
        "lifeos_workout_manage_renamed",
    )

    with pytest.raises(capabilities.PersonaCapabilityContractError, match="MCP persona tool"):
        capabilities.validate_capability_contract()

    # Live envelope construction degrades safely instead of taking chat down.
    assert capabilities.project_persona_tool_capabilities("fitness") == ()


def test_removed_catalog_action_fails_the_projection_contract(monkeypatch):
    """Native action drift is omitted live and remains visible to validation."""
    from api.services import agent_tools

    monkeypatch.delitem(agent_tools._WORKOUT_ACTION_HANDLERS, "set_profile")

    projected = capabilities.project_persona_tool_capabilities("fitness")
    assert "set_profile" not in projected[0].actions
    with pytest.raises(capabilities.PersonaCapabilityContractError, match="Action catalog drift"):
        capabilities.validate_capability_contract()
