"""Canonical execution requests, immutable specs, and compatibility adapters.

Resolution is pure: callers inject observed executor/host facts and trusted
session context. The resulting spec is persisted before dispatch and reused
verbatim for retries and resumes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import math
from typing import Any, Mapping

from api.services.agent_worker.assignment import (
    BOARD_EFFORT_LEVELS,
    Assignment,
    local_thinking_for_effort,
    map_effort_for_engine,
)


class Executor(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    CLAUDE = "claude"
    HERMES = "hermes"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    NATIVE_INLINE = "native_inline"


class CatalogState(str, Enum):
    LOADED = "loaded"
    EMPTY_VALID = "empty-valid"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"
    UNKNOWN = "unknown"


class ReadinessState(str, Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"
    UNKNOWN = "unknown"


class BillingClass(str, Enum):
    SUBSCRIPTION = "subscription"
    METERED = "metered"
    LOCAL_FREE = "local_free"
    UNKNOWN = "unknown"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Source(str, Enum):
    EXPLICIT = "explicit"
    TEMPORARY = "temporary"
    ASSIGNMENT = "assignment"
    WORKFLOW = "workflow"
    INSTALLATION = "installation"
    NATIVE = "native"
    CONTEXT = "context"
    UNRESOLVED = "unresolved"


CANONICAL_EXECUTORS = frozenset(item.value for item in Executor)
CLI_EXECUTORS = frozenset({Executor.CLAUDE_CODE.value, Executor.CODEX.value})
CANONICAL_REQUEST_FIELDS = frozenset(
    {"executor", "model_id", "effort", "host", "working_dir", "budget", "constraints"}
)
TRUSTED_CONTEXT_FIELDS = frozenset(
    {"persona_id", "workflow_id", "parent_session_id", "root_session_id", "reply_destination"}
)
CONSTRAINT_FIELDS = frozenset(
    {"allowed_executors", "required_capabilities", "allowed_billing"}
)
SUPPORTED_EXPLICIT_FIELDS = {
    # OpenAI-compatible native backends accept a per-request model selector;
    # retaining it here lets a canonical inline request pin the model without
    # changing the route or falling through to a paid backend.
    "local": frozenset({"model_id", "effort", "working_dir", "budget"}),
    "remote": frozenset({"model_id", "working_dir", "budget"}),
    "claude": frozenset({"budget"}),
    "hermes": frozenset(),
    "claude_code": frozenset({"model_id", "effort", "host", "working_dir", "budget"}),
    "codex": frozenset({"model_id", "effort", "host", "working_dir", "budget"}),
    "native_inline": frozenset({"model_id"}),
}
STATIC_IDENTITY = {
    "local": ("local", "in_process"),
    "remote": ("remote", "in_process"),
    "claude": ("anthropic", "managed_api"),
    "hermes": ("hermes", "hermes"),
    "claude_code": ("claude_code", "cli"),
    "codex": ("openai", "cli"),
}
LEGACY_ROUTE_ALIASES = {
    "#claude": ("claude_code", None), "claude_code": ("claude_code", None),
    "code": ("claude_code", None), "#codex": ("codex", None),
    "codex": ("codex", None), "#local": ("local", None),
    "local": ("local", None), "gemma": ("local", None),
    "#hermes": ("hermes", None), "hermes": ("hermes", None),
    "#cloud": ("remote", None), "cloud": ("remote", None),
    "remote": ("remote", None),
    "#cloud-haiku": ("claude", "claude-haiku-4-5"),
    "cloud-haiku": ("claude", "claude-haiku-4-5"),
    "#cloud-sonnet": ("claude", "claude-sonnet-5"),
    "cloud-sonnet": ("claude", "claude-sonnet-5"),
}


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    field: str | None = None
    source: str | None = None
    fatal: bool = True


@dataclass(frozen=True)
class Budget:
    wall_seconds: int | None = None
    max_tokens: int | None = None
    max_dollars: float | None = None


@dataclass(frozen=True)
class ExecutionConstraints:
    allowed_executors: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    allowed_billing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionRequest:
    """Strict client-writable choices; authenticated identity is separate."""

    executor: str | None = None
    model_id: str | None = None
    effort: str | None = None
    host: str | None = None
    working_dir: str | None = None
    budget: Budget | None = None
    constraints: ExecutionConstraints = field(default_factory=ExecutionConstraints)

    @classmethod
    def from_canonical(cls, payload: Mapping[str, Any]) -> "ParseResult":
        return parse_execution_request(payload)


@dataclass(frozen=True)
class ExecutionLayer:
    """Trusted inherited values from one precedence source."""

    executor: str | None = None
    model_id: str | None = None
    model_executor: str | None = None
    effort: str | None = None
    host: str | None = None
    working_dir: str | None = None
    budget: Budget | None = None
    constraints: ExecutionConstraints = field(default_factory=ExecutionConstraints)

    @classmethod
    def from_request(cls, request: ExecutionRequest) -> "ExecutionLayer":
        return cls(
            executor=request.executor, model_id=request.model_id,
            model_executor=request.executor if request.model_id else None,
            effort=request.effort, host=request.host, working_dir=request.working_dir,
            budget=request.budget, constraints=request.constraints,
        )

    @classmethod
    def from_assignment(cls, assignment: Assignment, *, executor: str | None) -> "ExecutionLayer":
        return cls(
            executor=executor, model_id=assignment.model,
            model_executor=executor if assignment.model else None,
            effort=assignment.effort, host=assignment.host,
        )


@dataclass(frozen=True)
class ExecutionContext:
    """Server-derived identity that canonical clients cannot write."""

    session_id: str | None = None
    persona_id: str | None = None
    parent_session_id: str | None = None
    root_session_id: str | None = None
    reply_destination: str | None = None


@dataclass(frozen=True)
class TemporaryOverride:
    scope: str
    scope_id: str
    created_at: datetime
    executor: str | None = None
    model_id: str | None = None
    effort: str | None = None
    host: str | None = None
    working_dir: str | None = None
    expires_at: datetime | None = None

    def layer_for(self, context: ExecutionContext, now: datetime) -> tuple[ExecutionLayer | None, Diagnostic | None]:
        if self.scope not in {"session", "lineage"}:
            return None, Diagnostic("invalid_override_scope", "scope must be session or lineage", source="temporary")
        actual = context.session_id if self.scope == "session" else context.root_session_id
        if not self.scope_id or not actual or self.scope_id != actual:
            return None, Diagnostic("temporary_override_scope_mismatch", "override does not apply to this session lineage", source="temporary", fatal=False)
        if self.model_id and not self.executor:
            return None, Diagnostic("model_requires_executor", "override model_id must be scoped to an executor", "model_id", "temporary")
        try:
            if now < self.created_at:
                return None, Diagnostic("temporary_override_not_started", "override is not active yet", source="temporary", fatal=False)
            if self.expires_at is not None and now >= self.expires_at:
                return None, Diagnostic("temporary_override_expired", "override has expired for future resolutions", source="temporary", fatal=False)
        except TypeError:
            return None, Diagnostic("invalid_override_time", "override timestamps must use compatible timezones", source="temporary")
        return ExecutionLayer(
            executor=self.executor, model_id=self.model_id,
            model_executor=self.executor if self.model_id else None,
            effort=self.effort, host=self.host, working_dir=self.working_dir,
        ), None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.created_at.isoformat()
        value["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TemporaryOverride":
        return cls(**{
            **value,
            "created_at": datetime.fromisoformat(value["created_at"]),
            "expires_at": datetime.fromisoformat(value["expires_at"]) if value.get("expires_at") else None,
        })


@dataclass(frozen=True)
class CatalogFacts:
    state: CatalogState = CatalogState.UNKNOWN
    model_ids: tuple[str, ...] = ()
    observed_at: datetime | None = None


@dataclass(frozen=True)
class ExecutorFacts:
    executor: str
    provider: str | None = None
    runtime: str | None = None
    capabilities: tuple[str, ...] = ()
    readiness: ReadinessState = ReadinessState.UNKNOWN
    catalog: CatalogFacts = field(default_factory=CatalogFacts)
    native_model_id: str | None = None
    billing: BillingClass = BillingClass.UNKNOWN


@dataclass(frozen=True)
class ExecutionFacts:
    now: datetime
    executors: tuple[ExecutorFacts, ...]
    host_validation_available: bool = True
    valid_hosts: tuple[str, ...] = ()

    def for_executor(self, executor: str) -> ExecutorFacts | None:
        return next((item for item in self.executors if item.executor == executor), None)


@dataclass(frozen=True)
class FieldProvenance:
    field: str
    source: Source
    detail: str = ""


@dataclass(frozen=True)
class ExecutionSpec:
    executor: str
    provider: str
    runtime: str
    model_id: str | None
    effort: str | None
    host: str | None
    working_dir: str | None
    persona_id: str | None
    parent_session_id: str | None
    root_session_id: str | None
    reply_destination: str | None
    budget: Budget | None
    constraints: ExecutionConstraints
    billing: BillingClass
    resolved_at: datetime
    provenance: tuple[FieldProvenance, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def cli_model_args(self) -> tuple[str, ...]:
        return ("--model", self.model_id) if self.executor in CLI_EXECUTORS and self.model_id else ()

    @property
    def mapped_effort(self) -> str | None:
        return map_effort_for_engine(self.executor, self.effort)

    @property
    def local_thinking(self) -> bool | None:
        return local_thinking_for_effort(self.effort) if self.executor == "local" else None

    def to_dict(self) -> dict[str, Any]:
        scalar_names = (
            "executor", "provider", "runtime", "model_id", "effort", "host",
            "working_dir", "persona_id", "parent_session_id", "root_session_id",
            "reply_destination",
        )
        return {
            **{name: getattr(self, name) for name in scalar_names},
            "budget": asdict(self.budget) if self.budget else None,
            "constraints": asdict(self.constraints),
            "billing": self.billing.value,
            "resolved_at": self.resolved_at.isoformat(),
            "provenance": [
                {"field": item.field, "source": item.source.value, "detail": item.detail}
                for item in self.provenance
            ],
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionSpec":
        scalar_names = (
            "executor", "provider", "runtime", "model_id", "effort", "host",
            "working_dir", "persona_id", "parent_session_id", "root_session_id",
            "reply_destination",
        )
        constraints = value.get("constraints") or {}
        return cls(
            **{name: value.get(name) for name in scalar_names},
            budget=Budget(**value["budget"]) if value.get("budget") else None,
            constraints=ExecutionConstraints(
                tuple(constraints.get("allowed_executors", ())),
                tuple(constraints.get("required_capabilities", ())),
                tuple(constraints.get("allowed_billing", ())),
            ),
            billing=BillingClass(value["billing"]),
            resolved_at=datetime.fromisoformat(value["resolved_at"]),
            provenance=tuple(
                FieldProvenance(item["field"], Source(item["source"]), item.get("detail", ""))
                for item in value.get("provenance", ())
            ),
            diagnostics=tuple(Diagnostic(**item) for item in value.get("diagnostics", ())),
        )


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    spec: ExecutionSpec | None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == ResolutionStatus.RESOLVED and self.spec is not None


@dataclass(frozen=True)
class ParseResult:
    request: ExecutionRequest | None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.request is not None and not any(item.fatal for item in self.diagnostics)


@dataclass(frozen=True)
class LegacyAliasResult:
    alias: str
    request: ExecutionRequest | None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def recognized(self) -> bool:
        return self.request is not None


@dataclass(frozen=True)
class LegacySpawnResult:
    engine: str | None
    tier: str | None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def recognized(self) -> bool:
        return self.engine is not None and not any(item.fatal for item in self.diagnostics)


def _text(value: Any, name: str, diagnostics: list[Diagnostic]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        diagnostics.append(Diagnostic("invalid_type", f"{name} must be a string", name))
        return None
    cleaned = value.strip()
    if not cleaned:
        diagnostics.append(Diagnostic("empty_value", f"{name} must not be empty", name))
        return None
    return cleaned


def _parse_constraints(value: Any, diagnostics: list[Diagnostic]) -> ExecutionConstraints:
    if value is None:
        return ExecutionConstraints()
    if not isinstance(value, Mapping):
        diagnostics.append(Diagnostic("invalid_type", "constraints must be an object", "constraints"))
        return ExecutionConstraints()
    for key in value:
        if key not in CONSTRAINT_FIELDS:
            diagnostics.append(Diagnostic("unknown_field", f"unknown constraints field: {key}", f"constraints.{key}"))

    def strings(name: str) -> tuple[str, ...]:
        raw = value.get(name, ())
        if raw is None:
            return ()
        if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in raw):
            diagnostics.append(Diagnostic("invalid_type", f"constraints.{name} must be a list of non-empty strings", f"constraints.{name}"))
            return ()
        return tuple(dict.fromkeys(item.strip() for item in raw))

    executors = strings("allowed_executors")
    billings = strings("allowed_billing")
    for item in executors:
        if item not in CANONICAL_EXECUTORS:
            diagnostics.append(Diagnostic("unknown_executor", f"unknown allowed executor: {item}", "constraints.allowed_executors"))
    for item in billings:
        if item not in {entry.value for entry in BillingClass}:
            diagnostics.append(Diagnostic("unknown_billing", f"unknown billing class: {item}", "constraints.allowed_billing"))
    return ExecutionConstraints(executors, strings("required_capabilities"), billings)


def _parse_budget(value: Any, diagnostics: list[Diagnostic]) -> Budget | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        diagnostics.append(Diagnostic("invalid_type", "budget must be an object", "budget"))
        return None
    for key in value:
        if key not in {"wall_seconds", "max_tokens", "max_dollars"}:
            diagnostics.append(Diagnostic("unknown_field", f"unknown budget field: {key}", f"budget.{key}"))
    parsed: dict[str, int | float | None] = {}
    for key, kind in (("wall_seconds", int), ("max_tokens", int), ("max_dollars", (int, float))):
        raw = value.get(key)
        valid = raw is None or (
            not isinstance(raw, bool) and isinstance(raw, kind) and raw >= 0
        )
        if key == "max_dollars" and valid and raw is not None:
            valid = math.isfinite(float(raw))
        if not valid:
            diagnostics.append(Diagnostic("invalid_budget", f"budget.{key} must be finite and non-negative", f"budget.{key}"))
            parsed[key] = None
        else:
            parsed[key] = raw
    return Budget(**parsed)


def parse_execution_request(payload: Mapping[str, Any]) -> ParseResult:
    diagnostics: list[Diagnostic] = []
    if not isinstance(payload, Mapping):
        return ParseResult(None, (Diagnostic("invalid_type", "execution request must be an object"),))
    for key in payload:
        if key in TRUSTED_CONTEXT_FIELDS:
            diagnostics.append(Diagnostic("trusted_field_not_writable", f"{key} comes from authenticated context", key))
        elif key in {"provider", "runtime", "billing"}:
            diagnostics.append(Diagnostic("derived_field_not_writable", f"{key} is derived from executor facts", key))
        elif key not in CANONICAL_REQUEST_FIELDS:
            diagnostics.append(Diagnostic("unknown_field", f"unknown execution request field: {key}", str(key)))
    values = {
        key: _text(payload.get(key), key, diagnostics)
        for key in ("executor", "model_id", "effort", "host", "working_dir")
    }
    executor = values["executor"]
    if executor is not None and executor not in CANONICAL_EXECUTORS:
        diagnostics.append(Diagnostic("unknown_executor", f"unknown canonical executor: {executor}", "executor"))
        if executor.startswith("#"):
            diagnostics.append(Diagnostic("legacy_alias", "route aliases require a legacy adapter", "executor"))
    if values["model_id"] is not None and executor is None:
        diagnostics.append(Diagnostic("model_requires_executor", "model_id must be scoped to an executor", "model_id"))
    if values["effort"] is not None and values["effort"] not in BOARD_EFFORT_LEVELS:
        diagnostics.append(Diagnostic("invalid_effort", "effort must be low, medium, high, or max", "effort"))
    values["budget"] = _parse_budget(payload.get("budget"), diagnostics)
    values["constraints"] = _parse_constraints(payload.get("constraints"), diagnostics)
    request = ExecutionRequest(**values)
    return ParseResult(None if any(item.fatal for item in diagnostics) else request, tuple(diagnostics))


def parse_legacy_route_alias(alias: str) -> LegacyAliasResult:
    normalized = alias.strip().lower() if isinstance(alias, str) else ""
    if normalized not in LEGACY_ROUTE_ALIASES:
        return LegacyAliasResult(
            alias if isinstance(alias, str) else "", None,
            (Diagnostic("unknown_legacy_alias", "unknown legacy route alias", source="legacy"),),
        )
    executor, model_id = LEGACY_ROUTE_ALIASES[normalized]
    return LegacyAliasResult(alias, ExecutionRequest(executor=executor, model_id=model_id))


def parse_legacy_spawn(model: str, tier: str | None = None) -> LegacySpawnResult:
    engine = model.strip().lower() if isinstance(model, str) else ""
    if engine not in {"claude", "local", "claude_code", "codex"}:
        return LegacySpawnResult(None, None, (Diagnostic("unknown_legacy_engine", "legacy spawn model is unsupported", source="legacy"),))
    cleaned = tier.strip().lower() if isinstance(tier, str) and tier.strip() else None
    if cleaned and cleaned not in {"haiku", "sonnet", "opus"}:
        return LegacySpawnResult(engine, None, (Diagnostic("invalid_legacy_tier", "legacy tier must be haiku, sonnet, or opus", source="legacy"),))
    if cleaned and engine != "claude_code":
        return LegacySpawnResult(engine, None, (Diagnostic("legacy_tier_ignored", "legacy tier is only meaningful for claude_code", source="legacy", fatal=False),))
    return LegacySpawnResult(engine, cleaned)


def _failure(status: ResolutionStatus, diagnostics: list[Diagnostic]) -> ResolutionResult:
    return ResolutionResult(status, None, tuple(diagnostics))


def unsupported_explicit_fields(
    request: ExecutionRequest, executor: str | None = None,
) -> tuple[Diagnostic, ...]:
    """Return canonical fields the selected executor cannot consume."""
    target = executor or request.executor
    supported = SUPPORTED_EXPLICIT_FIELDS.get(target or "")
    if supported is None:
        return ()
    return tuple(
        Diagnostic(
            "unsupported_field",
            f"executor {target} cannot honor {name}",
            name,
            "explicit",
        )
        for name in ("model_id", "effort", "host", "working_dir", "budget")
        if getattr(request, name) is not None and name not in supported
    )


def _pick(layers: tuple[tuple[Source, ExecutionLayer | None], ...], name: str) -> tuple[Any, Source]:
    for source, layer in layers:
        if layer is not None and getattr(layer, name) is not None:
            return getattr(layer, name), source
    return None, Source.UNRESOLVED


def _merge_constraints(layers: tuple[ExecutionLayer | None, ...], diagnostics: list[Diagnostic]) -> ExecutionConstraints:
    executor_sets = [set(layer.constraints.allowed_executors) for layer in layers if layer and layer.constraints.allowed_executors]
    billing_sets = [set(layer.constraints.allowed_billing) for layer in layers if layer and layer.constraints.allowed_billing]
    executors = set.intersection(*executor_sets) if executor_sets else set()
    billings = set.intersection(*billing_sets) if billing_sets else set()
    if executor_sets and not executors:
        diagnostics.append(Diagnostic("constraint_conflict", "executor constraints have no common destination", "constraints"))
    if billing_sets and not billings:
        diagnostics.append(Diagnostic("constraint_conflict", "billing constraints have no common class", "constraints"))
    capabilities = {item for layer in layers if layer for item in layer.constraints.required_capabilities}
    return ExecutionConstraints(tuple(sorted(executors)), tuple(sorted(capabilities)), tuple(sorted(billings)))


def resolve_execution(
    request: ExecutionRequest | None = None,
    *,
    context: ExecutionContext = ExecutionContext(),
    assignment: ExecutionLayer | None = None,
    workflow: ExecutionLayer | None = None,
    installation: ExecutionLayer | None = None,
    facts: ExecutionFacts,
    temporary_override: TemporaryOverride | None = None,
) -> ResolutionResult:
    """Resolve and freeze one dispatch snapshot without executor fallback."""

    diagnostics: list[Diagnostic] = []
    request = request or ExecutionRequest()
    validated = parse_execution_request(asdict(request))
    if not validated.ok:
        return _failure(ResolutionStatus.INVALID, list(validated.diagnostics))
    request = validated.request
    explicit = ExecutionLayer.from_request(request)
    override_layer = None
    if temporary_override:
        override_layer, item = temporary_override.layer_for(context, facts.now)
        if item:
            diagnostics.append(item)
    layers = (
        (Source.EXPLICIT, explicit),
        (Source.TEMPORARY, override_layer),
        (Source.ASSIGNMENT, assignment),
        (Source.WORKFLOW, workflow),
        (Source.INSTALLATION, installation),
    )
    constraints = _merge_constraints(tuple(layer for _, layer in layers), diagnostics)
    if any(item.fatal for item in diagnostics):
        return _failure(ResolutionStatus.INVALID, diagnostics)
    executor, executor_source = _pick(layers, "executor")
    if executor is None:
        return _failure(ResolutionStatus.INVALID, diagnostics + [Diagnostic("missing_executor", "no executor was supplied", "executor")])
    if executor not in CANONICAL_EXECUTORS:
        return _failure(ResolutionStatus.INVALID, diagnostics + [Diagnostic("unknown_executor", f"unknown executor: {executor}", "executor")])
    if constraints.allowed_executors and executor not in constraints.allowed_executors:
        return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + [Diagnostic("executor_not_allowed", f"executor {executor} violates constraints", "executor")])
    target = facts.for_executor(executor)
    if target is None:
        return _failure(ResolutionStatus.UNKNOWN, diagnostics + [Diagnostic("missing_executor_facts", f"no facts for {executor}", "executor")])
    if target.readiness in {ReadinessState.UNAVAILABLE, ReadinessState.UNCONFIGURED}:
        return _failure(ResolutionStatus.UNAVAILABLE, diagnostics + [Diagnostic("executor_unavailable", f"executor {executor} is {target.readiness.value}", "executor")])
    if target.readiness == ReadinessState.UNKNOWN:
        return _failure(ResolutionStatus.UNKNOWN, diagnostics + [Diagnostic("executor_readiness_unknown", f"readiness for {executor} is unknown", "executor")])
    missing = tuple(item for item in constraints.required_capabilities if item not in target.capabilities)
    if missing:
        return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + [Diagnostic("missing_capability", f"executor lacks: {', '.join(missing)}", "constraints")])
    if constraints.allowed_billing and target.billing.value not in constraints.allowed_billing:
        return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + [Diagnostic("billing_not_allowed", f"billing class {target.billing.value} violates constraints", "constraints")])
    supported = SUPPORTED_EXPLICIT_FIELDS[executor]
    unsupported = unsupported_explicit_fields(request, executor)
    if unsupported:
        return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + list(unsupported))

    model_id, model_source, model_scope = None, Source.UNRESOLVED, None
    for source, layer in layers:
        if not layer or not layer.model_id:
            continue
        scope = layer.model_executor or layer.executor
        if scope != executor:
            diagnostics.append(Diagnostic("cross_executor_model_pin_ignored", f"ignored {source.value} model pin scoped to {scope}", "model_id", source.value, False))
            continue
        candidate = layer.model_id
        catalog = target.catalog
        incompatible = catalog.state in {CatalogState.LOADED, CatalogState.EMPTY_VALID} and candidate not in catalog.model_ids
        if incompatible:
            if source == Source.EXPLICIT:
                return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + [Diagnostic("model_not_available", f"model {candidate} is unavailable for {executor}", "model_id", source.value)])
            diagnostics.append(Diagnostic("inherited_model_not_available", f"ignored unavailable inherited model {candidate}", "model_id", source.value, False))
            continue
        model_id, model_source, model_scope = candidate, source, scope
        if catalog.state in {CatalogState.UNKNOWN, CatalogState.UNAVAILABLE, CatalogState.UNCONFIGURED}:
            diagnostics.append(Diagnostic("model_catalog_unverified", f"preserved model while catalog is {catalog.state.value}", "model_id", source.value, False))
        break
    if model_id is not None and "model_id" not in supported:
        diagnostics.append(Diagnostic("inherited_field_ignored", f"executor {executor} cannot honor inherited model_id", "model_id", model_source.value, False))
        model_id, model_source, model_scope = None, Source.UNRESOLVED, None
    if model_id is None and executor not in CLI_EXECUTORS and target.native_model_id:
        model_id, model_source, model_scope = target.native_model_id, Source.NATIVE, executor

    effort, effort_source = _pick(layers, "effort")
    if effort is not None and effort not in BOARD_EFFORT_LEVELS:
        return _failure(ResolutionStatus.INVALID, diagnostics + [Diagnostic("invalid_effort", "effort must be low, medium, high, or max", "effort", effort_source.value)])
    if effort is not None and "effort" not in supported:
        diagnostics.append(Diagnostic("inherited_field_ignored", f"executor {executor} cannot honor inherited effort", "effort", effort_source.value, False))
        effort, effort_source = None, Source.UNRESOLVED
    host, host_source = _pick(layers, "host")
    if host is not None and "host" not in supported:
        diagnostics.append(Diagnostic("inherited_field_ignored", f"executor {executor} cannot honor inherited host", "host", host_source.value, False))
        host, host_source = None, Source.UNRESOLVED
    if host is not None:
        if not facts.host_validation_available:
            return _failure(ResolutionStatus.UNKNOWN, diagnostics + [Diagnostic("host_validation_unavailable", "host facts are unavailable", "host")])
        if host not in facts.valid_hosts:
            return _failure(ResolutionStatus.UNSUPPORTED, diagnostics + [Diagnostic("unknown_host", f"unknown host: {host}", "host")])
    working_dir, working_source = _pick(layers, "working_dir")
    if working_dir is not None and "working_dir" not in supported:
        diagnostics.append(Diagnostic("inherited_field_ignored", f"executor {executor} cannot honor inherited working_dir", "working_dir", working_source.value, False))
        working_dir, working_source = None, Source.UNRESOLVED
    budget, budget_source = _pick(layers, "budget")
    if budget is not None and "budget" not in supported:
        diagnostics.append(Diagnostic("inherited_field_ignored", f"executor {executor} cannot honor inherited budget", "budget", budget_source.value, False))
        budget, budget_source = None, Source.UNRESOLVED
    provider, runtime = target.provider, target.runtime
    static_provider, static_runtime = STATIC_IDENTITY.get(executor, (None, None))
    provider, runtime = provider or static_provider, runtime or static_runtime
    if not provider or not runtime:
        return _failure(ResolutionStatus.UNKNOWN, diagnostics + [Diagnostic("missing_execution_identity", f"provider/runtime missing for {executor}")])
    provenance = tuple(
        FieldProvenance(name, source, detail)
        for name, source, detail in (
            ("executor", executor_source, ""), ("model_id", model_source, model_scope or ""),
            ("effort", effort_source, ""), ("host", host_source, ""),
            ("working_dir", working_source, ""), ("budget", budget_source, ""),
            ("persona_id", Source.CONTEXT, "trusted"),
            ("parent_session_id", Source.CONTEXT, "trusted"),
            ("root_session_id", Source.CONTEXT, "trusted"),
            ("reply_destination", Source.CONTEXT, "trusted"),
            ("provider", Source.NATIVE, "executor facts"),
            ("runtime", Source.NATIVE, "executor facts"),
            ("billing", Source.NATIVE, "executor facts"),
        )
    )
    spec = ExecutionSpec(
        executor=executor, provider=provider, runtime=runtime, model_id=model_id,
        effort=effort, host=host, working_dir=working_dir,
        persona_id=context.persona_id, parent_session_id=context.parent_session_id,
        root_session_id=context.root_session_id, reply_destination=context.reply_destination,
        budget=budget, constraints=constraints, billing=target.billing,
        resolved_at=facts.now, provenance=provenance, diagnostics=tuple(diagnostics),
    )
    return ResolutionResult(ResolutionStatus.RESOLVED, spec, tuple(diagnostics))


__all__ = [
    "BillingClass", "Budget", "CatalogFacts", "CatalogState", "Diagnostic",
    "ExecutionConstraints", "ExecutionContext", "ExecutionFacts", "ExecutionLayer",
    "ExecutionRequest", "ExecutionSpec", "Executor", "ExecutorFacts", "FieldProvenance",
    "LegacyAliasResult", "LegacySpawnResult", "ParseResult", "ReadinessState",
    "ResolutionResult", "ResolutionStatus", "Source", "TemporaryOverride",
    "parse_execution_request", "parse_legacy_route_alias",
    "parse_legacy_spawn", "resolve_execution", "unsupported_explicit_fields",
]
