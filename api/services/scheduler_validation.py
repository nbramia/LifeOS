"""
Shared validation for a schedule's action inputs.

A schedule's fire behavior depends entirely on its ``action`` — validated
here so every write path (``api/routes/scheduler.py``'s HTTP routes and
``api/services/agent_tools.py``'s ``manage_schedules`` chat tool) applies
the same rule, rather than a schedule being written with an action whose
fire path has nothing to work with (e.g. an ``endpoint`` action with no
endpoint configured produces "No endpoint configuration provided." only
once it actually fires).
"""
import math
from typing import Optional


class ScheduleActionValidationError(Exception):
    """A schedule's resulting action doesn't have what it needs to fire.

    ``field`` names the offending field (e.g. ``endpoint_config.method``,
    ``message_content``) and ``detail`` is a human-readable message safe to
    show verbatim to an operator or return from a tool call.
    """

    def __init__(self, field: str, detail: str):
        self.field = field
        self.detail = detail
        super().__init__(detail)


def validate_action_inputs(
    action: str, message_content: str, endpoint_config: Optional[dict],
    budget_dollars: Optional[float] = None, wall_seconds: Optional[int] = None,
) -> Optional[dict]:
    """Validate that a schedule's resulting action has the inputs it needs
    to fire, raising ``ScheduleActionValidationError`` on failure.

    ``budget_dollars``/``wall_seconds`` are the optional budget an
    ``action:: agent`` schedule hands its created task on every fire — valid
    only for ``action == "agent"`` and, when present, must be finite and
    non-negative.

    Returns the ``endpoint_config`` to store: for ``endpoint``, the same
    dict with ``method`` normalized to upper case; for every other action,
    ``endpoint_config`` unchanged.
    """
    if budget_dollars is not None or wall_seconds is not None:
        if action != "agent":
            field = "budget_dollars" if budget_dollars is not None else "wall_seconds"
            raise ScheduleActionValidationError(
                field, f"{field} is only valid for action='agent', got action={action!r}",
            )
        if budget_dollars is not None and (not math.isfinite(budget_dollars) or budget_dollars < 0):
            raise ScheduleActionValidationError(
                "budget_dollars", "budget_dollars must be a finite, non-negative number",
            )
        if wall_seconds is not None and (not math.isfinite(wall_seconds) or wall_seconds < 0):
            raise ScheduleActionValidationError(
                "wall_seconds", "wall_seconds must be a finite, non-negative number",
            )

    if action == "endpoint":
        cfg = endpoint_config if isinstance(endpoint_config, dict) else {}
        method = str(cfg.get("method", "")).strip().upper()
        if method not in ("GET", "POST"):
            raise ScheduleActionValidationError(
                "endpoint_config.method",
                f"endpoint_config.method must be 'GET' or 'POST', got {cfg.get('method')!r}",
            )
        path = cfg.get("endpoint")
        if not isinstance(path, str) or not path.startswith("/api/"):
            raise ScheduleActionValidationError(
                "endpoint_config.endpoint",
                f"endpoint_config.endpoint must start with '/api/', got {path!r}",
            )
        params = cfg.get("params")
        if params is not None and not isinstance(params, dict):
            raise ScheduleActionValidationError(
                "endpoint_config.params",
                "endpoint_config.params must be a JSON object",
            )
        normalized = dict(cfg)
        normalized["method"] = method
        return normalized

    if action in ("notify", "prompt", "agent"):
        if not (message_content or "").strip():
            raise ScheduleActionValidationError(
                "message_content", "message_content must not be blank",
            )

    return endpoint_config
