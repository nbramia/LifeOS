"""Narrow, validated persona-tool capability projection.

Persona prompts describe behavior; they do not grant tool access.  This module
is the small contract boundary that projects only operations which are
executable in the native runtime and registered by the live MCP/OpenAPI tool
builder.
"""
from __future__ import annotations

from dataclasses import dataclass


class PersonaCapabilityContractError(RuntimeError):
    """A configured capability does not match the callable tool catalog."""


@dataclass(frozen=True)
class PersonaToolCapability:
    """One native tool's callable counterpart on the LifeOS MCP surface."""

    native_name: str
    mcp_name: str
    actions: tuple[str, ...]

    def as_envelope(self) -> dict:
        """JSON-safe representation carried in ``lifeos_context.persona``."""
        return {
            "native_name": self.native_name,
            "mcp_name": self.mcp_name,
            "actions": list(self.actions),
        }


# Native names are prompt-facing names used by the in-process orchestrator.
# MCP names are the actual names registered by mcp_server.py.  Keep aliases
# here rather than teaching persona prompts or Hermes to guess names.
NATIVE_TO_MCP_ALIASES: dict[str, str] = {
    "manage_workouts": "lifeos_workout_manage",
}

# Personas select the small set of native capabilities whose aliases need to
# cross the LifeOS/Hermes boundary. This is not an execution grant: actual
# permission is derived below from the executable native handler registry and
# the MCP tool list built from the current OpenAPI routes. Persona prose never
# enters the decision.
PERSONA_NATIVE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "fitness": ("manage_workouts",),
}


def _native_action_catalogs(
    native_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    from api.services.agent_tools import (
        TOOL_DEFINITIONS,
        _TOOL_HANDLERS,
        _WORKOUT_ACTION_HANDLERS,
    )

    if native_name not in _TOOL_HANDLERS:
        return None

    definition = next(
        (item for item in TOOL_DEFINITIONS if item.get("name") == native_name),
        None,
    )
    if definition is None:
        return None
    actions = (
        definition.get("input_schema", {})
        .get("properties", {})
        .get("action", {})
        .get("enum")
    )
    if not isinstance(actions, list) or not all(
        isinstance(action, str) for action in actions
    ):
        return None
    action_handlers = {
        "manage_workouts": _WORKOUT_ACTION_HANDLERS,
    }.get(native_name)
    if action_handlers is None:
        return None
    return tuple(actions), tuple(action_handlers)


def _native_actions(native_name: str) -> tuple[str, ...] | None:
    catalogs = _native_action_catalogs(native_name)
    if catalogs is None:
        return None
    declared, executable = catalogs
    return tuple(action for action in declared if action in executable)


def _live_mcp_tools() -> tuple[dict, ...]:
    """Build the MCP tools from this process's current OpenAPI route set.

    The static curated map is only a permission hint.  The MCP server omits a
    curated entry when its route is absent from OpenAPI, so capability claims
    must inspect the same built tool list rather than the map alone.  Construct
    the builder without ``__init__`` to avoid an outbound OpenAPI HTTP request
    or any live tool/model call while projecting an envelope.
    """
    try:
        from api.main import app
        from mcp_server import LifeOSMCPServer

        server = LifeOSMCPServer.__new__(LifeOSMCPServer)
        server.openapi_spec = app.openapi()
        server.tools = []
        server._build_tools_from_spec()
        return tuple(server.tools)
    except Exception:
        return ()


def _mcp_actions(mcp_name: str, executable_actions: tuple[str, ...]) -> tuple[str, ...] | None:
    tool = next(
        (item for item in _live_mcp_tools() if item.get("name") == mcp_name),
        None,
    )
    if tool is None:
        return None
    # The MCP builder intentionally flattens the OpenAPI request model into a
    # compact schema and does not retain the action enum. Presence in this
    # built list proves the live route/tool registration; action truth comes
    # from the native dispatcher catalog above.
    return executable_actions


def project_persona_tool_capabilities(persona_id: str) -> tuple[PersonaToolCapability, ...]:
    """Return the registered-and-permitted tool operations for ``persona_id``.

    Ordinary runtime unavailability is represented by omission: it must not
    break the envelope or unrelated personas. ``validate_capability_contract``
    is the strict drift guard used by focused verification.
    """
    projected: list[PersonaToolCapability] = []
    for native_name in PERSONA_NATIVE_CAPABILITIES.get(persona_id, ()):
        mcp_name = NATIVE_TO_MCP_ALIASES.get(native_name)
        if not mcp_name:
            continue

        native_actions = _native_actions(native_name)
        mcp_actions = _mcp_actions(mcp_name, native_actions or ())
        if native_actions is None or mcp_actions is None:
            continue

        # Preserve native catalog order for stable prompt bytes. The live MCP
        # tool registration is the permission boundary for transporting these
        # executable operations to an MCP client.
        actions = tuple(
            action
            for action in native_actions
            if action in mcp_actions
        )
        if actions:
            projected.append(PersonaToolCapability(native_name, mcp_name, actions))
    return tuple(projected)


def validate_capability_contract() -> None:
    """Fail when an alias or action catalog has drifted across runtimes.

    This is intentionally separate from projection: focused verification must
    catch a rename, while a temporarily unavailable runtime tool must merely
    disappear from a live envelope rather than taking unrelated chat down.
    """
    for persona_id, native_names in PERSONA_NATIVE_CAPABILITIES.items():
        for native_name in native_names:
            mcp_name = NATIVE_TO_MCP_ALIASES.get(native_name)
            if not mcp_name:
                raise PersonaCapabilityContractError(
                    f"No MCP alias registered for {persona_id!r} tool {native_name!r}"
                )
            catalogs = _native_action_catalogs(native_name)
            if catalogs is None:
                raise PersonaCapabilityContractError(
                    f"Native persona tool {native_name!r} is not executable with an action catalog"
                )
            declared_actions, executable_actions = catalogs
            if declared_actions != executable_actions:
                raise PersonaCapabilityContractError(
                    f"Action catalog drift for {native_name!r}: "
                    f"schema={declared_actions!r}, handlers={executable_actions!r}"
                )
            mcp_actions = _mcp_actions(mcp_name, executable_actions)
            if mcp_actions is None:
                raise PersonaCapabilityContractError(
                    f"MCP persona tool {mcp_name!r} is not permitted with an action catalog"
                )
            if executable_actions != mcp_actions:
                raise PersonaCapabilityContractError(
                    f"Action catalog drift for {native_name!r}/{mcp_name!r}: "
                    f"native={executable_actions!r}, mcp={mcp_actions!r}"
                )
