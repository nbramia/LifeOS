"""Synthetic coverage for canonical execution resolution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import inf, nan
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.services.agent_worker.execution import (
    BillingClass, Budget, CatalogFacts, CatalogState, ExecutionConstraints,
    ExecutionContext, ExecutionFacts, ExecutionLayer, ExecutionRequest,
    ExecutionSpec, ExecutorFacts, ReadinessState, ResolutionStatus, Source,
    TemporaryOverride, parse_execution_request, parse_legacy_route_alias,
    parse_legacy_spawn, resolve_execution,
)
from api.services.agent_worker.session_store import STATUS_COMPLETED, SessionStore

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 10, 14, 30, tzinfo=timezone.utc)


def _executor(name, *, readiness=ReadinessState.READY, catalog=None,
              native_model_id=None, capabilities=(),
              billing=BillingClass.SUBSCRIPTION, provider=None, runtime=None):
    return ExecutorFacts(name, provider, runtime, capabilities, readiness,
                         catalog or CatalogFacts(), native_model_id, billing)


def _facts(*items, hosts=("api", "build-box")):
    items = items or (
        _executor("local", native_model_id="gemma", billing=BillingClass.LOCAL_FREE),
        _executor("remote", native_model_id="remote-model", billing=BillingClass.METERED),
        _executor("claude", billing=BillingClass.METERED),
        _executor("hermes", billing=BillingClass.UNKNOWN),
        _executor("claude_code"), _executor("codex"),
        _executor("native_inline", provider="synthetic", runtime="inline"),
    )
    return ExecutionFacts(NOW, tuple(items), True, hosts)


def _loaded(*models):
    return CatalogFacts(CatalogState.LOADED, tuple(models), NOW)


def test_parser_rejects_unknown_derived_alias_and_trusted_fields():
    result = parse_execution_request({"executor": "#codex", "provider": "x",
                                      "future": True, "root_session_id": "x"})
    assert result.request is None
    assert {item.code for item in result.diagnostics} == {
        "unknown_executor", "legacy_alias", "derived_field_not_writable",
        "unknown_field", "trusted_field_not_writable",
    }


@pytest.mark.parametrize("value", ["", "  ", 3, True])
def test_explicit_empty_or_wrong_type_is_not_unset(value):
    assert not parse_execution_request({"executor": "codex", "host": value}).ok
    assert parse_execution_request({"executor": "codex"}).ok


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_non_finite_budget_is_rejected(value):
    result = parse_execution_request({"executor": "codex",
                                      "budget": {"max_dollars": value}})
    assert not result.ok
    assert any(item.code == "invalid_budget" for item in result.diagnostics)


def test_parser_normalizes_nested_values_without_mutating_input():
    payload = {"executor": "codex", "constraints": {
        "required_capabilities": [" browser ", "browser"]},
        "budget": {"max_tokens": 128}}
    original = deepcopy(payload)
    result = parse_execution_request(payload)
    assert result.ok and payload == original
    assert result.request.constraints.required_capabilities == ("browser",)


@pytest.mark.parametrize(("alias", "executor", "model"), [
    ("#claude", "claude_code", None), ("#codex", "codex", None),
    ("#local", "local", None), ("#hermes", "hermes", None),
    ("#cloud", "remote", None), ("remote", "remote", None),
    ("#cloud-haiku", "claude", "claude-haiku-4-5"),
    ("#cloud-sonnet", "claude", "claude-sonnet-5"),
])
def test_legacy_aliases_remain_distinct(alias, executor, model):
    result = parse_legacy_route_alias(alias)
    assert result.recognized
    assert (result.request.executor, result.request.model_id) == (executor, model)


def test_legacy_spawn_model_is_engine_and_tier_is_preserved_or_ignored():
    assert parse_legacy_spawn("codex").engine == "codex"
    assert parse_legacy_spawn("claude_code", "sonnet").tier == "sonnet"
    ignored = parse_legacy_spawn("local", "sonnet")
    assert ignored.recognized and ignored.tier is None
    assert ignored.diagnostics[0].fatal is False


def test_explicit_request_precedes_temporary_and_assignment():
    override = TemporaryOverride("lineage", "root", NOW - timedelta(minutes=1),
                                 executor="local", effort="low")
    result = resolve_execution(
        ExecutionRequest(executor="codex", effort="max"),
        context=ExecutionContext("child", root_session_id="root"),
        assignment=ExecutionLayer(executor="claude_code", effort="medium"),
        facts=_facts(), temporary_override=override)
    assert result.spec.executor == "codex" and result.spec.effort == "max"
    assert result.spec.provenance[0].source == Source.EXPLICIT


def test_lineage_override_matches_trusted_root_only_and_expires():
    override = TemporaryOverride("lineage", "root-a", NOW - timedelta(minutes=1),
                                 executor="codex", expires_at=NOW + timedelta(minutes=1))
    matching = resolve_execution(context=ExecutionContext("child", root_session_id="root-a"),
        installation=ExecutionLayer(executor="local"), facts=_facts(), temporary_override=override)
    foreign = resolve_execution(context=ExecutionContext("child", root_session_id="root-b"),
        installation=ExecutionLayer(executor="local"), facts=_facts(), temporary_override=override)
    expired_facts = ExecutionFacts(NOW + timedelta(minutes=2), _facts().executors)
    expired = resolve_execution(context=ExecutionContext("child", root_session_id="root-a"),
        installation=ExecutionLayer(executor="local"), facts=expired_facts, temporary_override=override)
    assert matching.spec.executor == "codex"
    assert foreign.spec.executor == expired.spec.executor == "local"


def test_invalid_inherited_model_continues_to_valid_lower_default():
    result = resolve_execution(ExecutionRequest(executor="codex"),
        assignment=ExecutionLayer("codex", "removed", "codex"),
        workflow=ExecutionLayer("codex", "available", "codex"),
        facts=_facts(_executor("codex", catalog=_loaded("available"))))
    assert result.spec.model_id == "available"
    assert any(item.code == "inherited_model_not_available" for item in result.diagnostics)


def test_cross_engine_pin_is_ignored_and_cli_default_stays_unset():
    result = resolve_execution(ExecutionRequest(executor="codex"),
        assignment=ExecutionLayer("local", "gemma-pin", "local"),
        facts=_facts(_executor("codex", native_model_id="display-label")))
    assert result.spec.model_id is None and result.spec.cli_model_args == ()


@pytest.mark.parametrize("executor", ["local", "remote"])
def test_inline_native_model_pin_is_preserved_for_supported_engine(executor):
    """Canonical native requests carry their explicit model to dispatch."""
    result = resolve_execution(
        ExecutionRequest(executor=executor, model_id="synthetic-pinned-model"),
        facts=_facts(),
    )
    assert result.ok
    assert result.spec.model_id == "synthetic-pinned-model"


@pytest.mark.parametrize("executor", ["claude_code", "codex"])
def test_cli_unset_model_omits_flag_even_with_native_label(executor):
    result = resolve_execution(ExecutionRequest(executor=executor),
        facts=_facts(_executor(executor, native_model_id="label")))
    assert result.spec.model_id is None and result.spec.cli_model_args == ()


@pytest.mark.parametrize("state", [CatalogState.UNKNOWN, CatalogState.UNAVAILABLE,
                                    CatalogState.UNCONFIGURED])
def test_unverified_catalog_preserves_same_engine_pin(state):
    result = resolve_execution(ExecutionRequest(executor="codex", model_id="pin"),
        facts=_facts(_executor("codex", catalog=CatalogFacts(state))))
    assert result.spec.model_id == "pin"
    assert any(item.code == "model_catalog_unverified" for item in result.diagnostics)


def test_loaded_and_empty_catalog_reject_explicit_pin():
    for catalog in (_loaded("other"), CatalogFacts(CatalogState.EMPTY_VALID)):
        result = resolve_execution(ExecutionRequest(executor="codex", model_id="pin"),
            facts=_facts(_executor("codex", catalog=catalog)))
        assert result.status == ResolutionStatus.UNSUPPORTED


def test_constraint_intersection_precedes_readiness():
    result = resolve_execution(ExecutionRequest(executor="codex",
        constraints=ExecutionConstraints(("codex",), (), ("subscription",))),
        assignment=ExecutionLayer(executor="codex",
            constraints=ExecutionConstraints(("local",), (), ())),
        facts=_facts(_executor("codex", readiness=ReadinessState.UNAVAILABLE)))
    assert result.status == ResolutionStatus.INVALID
    assert any(item.code == "constraint_conflict" for item in result.diagnostics)


def test_billing_and_capability_constraints_fail_closed():
    result = resolve_execution(ExecutionRequest(executor="remote",
        constraints=ExecutionConstraints((), ("browser",), ("subscription",))),
        facts=_facts(_executor("remote", billing=BillingClass.METERED)))
    assert result.status == ResolutionStatus.UNSUPPORTED


@pytest.mark.parametrize(("executor", "field", "value"), [
    ("hermes", "effort", "max"),
    ("local", "host", "api"),
    ("remote", "host", "api"),
    ("claude", "host", "api"),
    ("hermes", "host", "api"),
    ("hermes", "budget", Budget(max_tokens=10)),
    ("claude", "model_id", "pin"),
    ("native_inline", "budget", Budget(max_tokens=10)),
])
def test_new_unsupported_field_is_rejected(executor, field, value):
    explicit = resolve_execution(
        ExecutionRequest(executor=executor, **{field: value}),
        facts=_facts(),
    )
    assert explicit.status == ResolutionStatus.UNSUPPORTED


def test_legacy_inherited_unsupported_field_is_ignored():
    inherited = resolve_execution(ExecutionRequest(executor="hermes"),
        assignment=ExecutionLayer(
            executor="hermes", effort="max", host="api",
            budget=Budget(max_tokens=10),
        ), facts=_facts())
    assert inherited.ok
    assert inherited.spec.effort is None
    assert inherited.spec.host is None
    assert inherited.spec.budget is None


@pytest.mark.parametrize(("readiness", "status"), [
    (ReadinessState.UNAVAILABLE, ResolutionStatus.UNAVAILABLE),
    (ReadinessState.UNCONFIGURED, ResolutionStatus.UNAVAILABLE),
    (ReadinessState.UNKNOWN, ResolutionStatus.UNKNOWN),
])
def test_readiness_never_falls_back(readiness, status):
    result = resolve_execution(ExecutionRequest(executor="codex"),
        installation=ExecutionLayer(executor="local"),
        facts=_facts(_executor("codex", readiness=readiness)))
    assert result.status == status and result.spec is None


def test_host_validation_fails_closed():
    unavailable = resolve_execution(ExecutionRequest(executor="codex", host="api"),
        facts=ExecutionFacts(NOW, (_executor("codex"),), False))
    unknown = resolve_execution(ExecutionRequest(executor="codex", host="other"),
        facts=_facts(_executor("codex")))
    assert unavailable.status == ResolutionStatus.UNKNOWN
    assert unknown.status == ResolutionStatus.UNSUPPORTED


def test_identity_and_effort_are_derived_for_target():
    result = resolve_execution(ExecutionRequest(executor="codex", effort="max"),
        facts=_facts(_executor("codex", provider="configured", runtime="custom")))
    assert (result.spec.provider, result.spec.runtime) == ("configured", "custom")
    assert result.spec.mapped_effort == "xhigh"
    claude_code = resolve_execution(
        ExecutionRequest(executor="claude_code", effort="max"), facts=_facts(),
    )
    assert claude_code.spec.mapped_effort == "max"
    local = resolve_execution(
        ExecutionRequest(executor="local", effort="high"), facts=_facts(),
    )
    assert local.spec.local_thinking is True


def test_spec_round_trip_is_deeply_immutable_and_inputs_are_unchanged():
    request = ExecutionRequest(executor="codex", budget=Budget(max_tokens=128),
        constraints=ExecutionConstraints(("codex",), ("shell",), ("subscription",)))
    original = deepcopy(request)
    result = resolve_execution(request,
        facts=_facts(_executor("codex", capabilities=("shell",))))
    restored = ExecutionSpec.from_dict(result.spec.to_dict())
    assert request == original and restored == result.spec
    assert isinstance(restored.constraints.allowed_executors, tuple)
    with pytest.raises((AttributeError, TypeError)):
        restored.constraints.allowed_executors += ("local",)


def test_session_snapshot_is_compare_and_set_and_survives_restart(tmp_path):
    path = tmp_path / "sessions.db"
    store = SessionStore(path)
    store.create("task-1")
    first = {"executor": "codex", "marker": "first"}
    second = {"executor": "local", "marker": "second"}
    assert store.set_execution_snapshot(
        "task-1", request={"executor": "codex"}, spec=first,
    ) == first
    assert store.set_execution_snapshot(
        "task-1", request={"executor": "local"}, spec=second,
    ) == first
    restarted = SessionStore(path).get("task-1")
    assert restarted.execution_spec == first
    assert restarted.execution_request == {"executor": "codex"}


def test_concurrent_snapshot_resolution_returns_one_persisted_winner(tmp_path):
    path = tmp_path / "sessions.db"
    store = SessionStore(path)
    store.create("task-1")

    def write(marker):
        return store.set_execution_snapshot(
            "task-1",
            request={"executor": marker},
            spec={"executor": marker, "marker": marker},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(write, ("codex", "local")))
    assert winners[0] == winners[1]
    assert SessionStore(path).get("task-1").execution_spec == winners[0]


def test_new_execution_can_only_replace_terminal_snapshot(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.create("task-1", execution_spec={"executor": "codex"})
    with pytest.raises(ValueError):
        store.begin_new_execution("task-1", request={"executor": "local"})
    store.update_status("task-1", STATUS_COMPLETED)
    store.begin_new_execution("task-1", request={"executor": "local"})
    session = store.get("task-1")
    assert session.execution_spec is None
    assert session.execution_request == {"executor": "local"}


def test_scoped_override_persistence_prefers_session_over_lineage(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    lineage = TemporaryOverride("lineage", "root", NOW, executor="local")
    session = TemporaryOverride("session", "child", NOW, executor="codex")
    store.set_execution_override(lineage)
    store.set_execution_override(session)
    restored = store.get_execution_override(
        session_id="child", root_session_id="root",
    )
    assert restored == session
    store.clear_execution_override(scope="session", scope_id="child")
    assert store.get_execution_override(
        session_id="child", root_session_id="root",
    ) == lineage


def test_expired_session_override_reveals_active_lineage_override(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    current = datetime.now(timezone.utc)
    lineage = TemporaryOverride("lineage", "root", current, executor="local")
    expired = TemporaryOverride(
        "session", "child", current - timedelta(hours=2), executor="codex",
        expires_at=current - timedelta(hours=1),
    )
    store.set_execution_override(lineage)
    store.set_execution_override(expired)
    assert store.get_execution_override(
        session_id="child", root_session_id="root",
    ) == lineage
