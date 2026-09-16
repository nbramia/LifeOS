"""Tests for the shared CLI binary resolver.

Readiness (the worker's execution-facts snapshot, the model catalog's
presence probes) and spawn (each executor's own resolution at launch) must
agree on whether a configured command resolves to a real executable. These
tests patch the environment and filesystem rather than depending on
whether `claude`/`codex` happen to be installed on the host running the
suite.
"""
from __future__ import annotations

import shutil
import stat
from datetime import datetime, timezone

import httpx
import pytest

from api.services.agent_worker.binary_resolver import (
    describe_candidates,
    resolve_binary,
    resolve_for_spawn,
)
from api.services.agent_worker.execution import (
    BillingClass,
    CatalogFacts,
    ExecutionConstraints,
    ExecutionContext,
    ExecutionFacts,
    ExecutionRequest,
    ExecutorFacts,
    ReadinessState,
    resolve_execution,
)
from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


def _make_worker(tmp_path):
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"tasks": []}))
    client = httpx.Client(transport=transport, base_url="http://api")
    return Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        conversation_store=ConversationStore(db_path=str(tmp_path / "conversations.db")),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        http_client=client,
    )


def _make_executable(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _block_path(monkeypatch) -> None:
    """Simulate a process PATH that does not contain the command — the
    systemd unit's minimal environment in the reported incident."""
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)


@pytest.mark.parametrize("command", ["claude", "codex"])
def test_regression_bare_command_resolves_via_fallback_search(monkeypatch, tmp_path, command):
    """The exact failing shape from the incident: a bare command name, a
    search path that excludes the install directory (simulated by blocking
    shutil.which), and the executable present only in a fallback location
    (~/.local/bin). Readiness must be READY — a resolver that degrades to
    shutil.which alone reports UNAVAILABLE here and fails this test."""
    fake_home = tmp_path / "home"
    target = fake_home / ".local" / "bin" / command
    _make_executable(target)
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    resolution = resolve_binary(command)

    assert resolution.ready is True
    assert resolution.resolved == str(target)


def test_override_absolute_path_honoured(tmp_path):
    """An absolute configured value that names an existing executable is an
    explicit operator override — honoured exactly."""
    target = tmp_path / "custom" / "claude"
    _make_executable(target)

    resolution = resolve_binary(str(target))

    assert resolution.override is True
    assert resolution.ready is True
    assert resolution.resolved == str(target)


def test_override_missing_path_is_unavailable_without_fallback_search(monkeypatch, tmp_path):
    """An absolute override pointing at a path that does not exist must be
    unavailable — it must NEVER fall through to the fallback search, even
    when a same-named binary exists in one of the search locations. A wrong
    override must be visible, not silently repaired."""
    fake_home = tmp_path / "home"
    # A real, executable "claude" sits in the fallback location the search
    # would otherwise find.
    _make_executable(fake_home / ".local" / "bin" / "claude")
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    missing_override = str(tmp_path / "does" / "not" / "exist" / "claude")
    resolution = resolve_binary(missing_override)

    assert resolution.override is True
    assert resolution.ready is False
    assert resolution.resolved is None
    # Only the override path itself was tried — proof the search never ran.
    assert resolution.candidates == (missing_override,)


def test_override_pointing_at_non_executable_file_is_unavailable(tmp_path):
    """An override that exists but lacks the executable bit is unavailable,
    not silently accepted."""
    target = tmp_path / "custom" / "claude"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not executable")
    # Deliberately no chmod +x.

    resolution = resolve_binary(str(target))

    assert resolution.ready is False
    assert resolution.resolved is None


def test_nothing_resolves_is_unavailable(monkeypatch, tmp_path):
    """A bare command that exists in none of the searched locations — PATH,
    the fixed install directories, or nvm — is unavailable."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    resolution = resolve_binary("totally-unknown-cli-binary")

    assert resolution.ready is False
    assert resolution.resolved is None
    assert len(resolution.candidates) > 1  # PATH plus each fallback dir tried


def test_nvm_fallback_prefers_newest_version(monkeypatch, tmp_path):
    """codex (and, after unification, claude too) can land under an
    active nvm install; the newest node version is tried first."""
    fake_home = tmp_path / "home"
    nvm_root = fake_home / ".nvm" / "versions" / "node"
    _make_executable(nvm_root / "v18.20.0" / "bin" / "codex")
    newest = nvm_root / "v24.19.0" / "bin" / "codex"
    _make_executable(newest)
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    resolution = resolve_binary("codex")

    assert resolution.resolved == str(newest)


def test_diagnostic_names_searched_candidates(monkeypatch, tmp_path):
    """When nothing resolves, the description handed to callers for
    diagnostics enumerates every location that was searched."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    resolution = resolve_binary("claude")
    detail = describe_candidates(resolution)

    expected_fallback_dirs = [
        str(fake_home / ".local" / "bin" / "claude"),
        "/usr/local/bin/claude",
        str(fake_home / ".npm" / "bin" / "claude"),
        "/opt/homebrew/bin/claude",
    ]
    for candidate in expected_fallback_dirs:
        assert candidate in detail


def test_resolution_unavailable_diagnostic_surfaces_candidates():
    """`resolve_execution`'s `executor_unavailable` diagnostic must include
    the candidate locations carried on the executor facts, not just the
    bare readiness word."""
    facts = ExecutionFacts(
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        executors=(
            ExecutorFacts(
                "codex",
                readiness=ReadinessState.UNAVAILABLE,
                catalog=CatalogFacts(),
                billing=BillingClass.SUBSCRIPTION,
                unavailable_detail="searched: $PATH (codex), /usr/local/bin/codex",
            ),
        ),
    )
    result = resolve_execution(
        ExecutionRequest(executor="codex", constraints=ExecutionConstraints()),
        facts=facts,
        context=ExecutionContext(),
    )

    assert not result.ok
    messages = [d.message for d in result.diagnostics if d.code == "executor_unavailable"]
    assert messages, "expected an executor_unavailable diagnostic"
    assert "/usr/local/bin/codex" in messages[0]


def test_readiness_and_spawn_resolve_identically(monkeypatch, tmp_path):
    """The value the readiness check bases its verdict on (`resolve_binary`)
    must be the exact value the executor would launch (`resolve_for_spawn`)
    for identical settings and environment — the bug this issue fixes was
    the two disagreeing."""
    fake_home = tmp_path / "home"
    target = fake_home / ".local" / "bin" / "claude"
    _make_executable(target)
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)

    readiness_path = resolve_binary("claude").resolved
    spawn_path = resolve_for_spawn("claude")

    assert readiness_path == spawn_path
    assert readiness_path == str(target)


def test_configured_codex_binary_setting_is_defined():
    """`LIFEOS_CODEX_BINARY` must be a real, defined setting, mirroring
    `claude_binary` — not silently absorbed by a getattr default."""
    from config.settings import Settings

    assert "codex_binary" in Settings.model_fields
    field = Settings.model_fields["codex_binary"]
    assert field.default == "codex"
    assert field.alias == "LIFEOS_CODEX_BINARY"


def test_codex_binary_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("LIFEOS_CODEX_BINARY", "/opt/custom/codex")
    from config.settings import Settings

    assert Settings(_env_file=None).codex_binary == "/opt/custom/codex"


def test_worker_readiness_gate_agrees_with_executor_spawn_resolution(monkeypatch, tmp_path):
    """End-to-end regression check: the worker's own `_execution_facts()`
    readiness gate (what cancelled the task in the incident) and each
    executor's spawn-time resolver (`_resolve_claude_binary` /
    `_resolve_codex_binary`, what would have launched it successfully) must
    reach the same conclusion for a bare command name missing from the
    process PATH but present in a fallback install directory."""
    from api.services.agent_worker.claude_code_executor import _resolve_claude_binary
    from api.services.agent_worker.codex_executor import _resolve_codex_binary
    from config.settings import settings

    fake_home = tmp_path / "home"
    claude_target = fake_home / ".local" / "bin" / "claude"
    codex_target = fake_home / ".local" / "bin" / "codex"
    _make_executable(claude_target)
    _make_executable(codex_target)
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)
    monkeypatch.setattr(settings, "claude_binary", "claude", raising=False)
    monkeypatch.setattr(settings, "codex_binary", "codex", raising=False)

    worker = _make_worker(tmp_path)
    facts = worker._execution_facts()

    claude_facts = facts.for_executor("claude_code")
    codex_facts = facts.for_executor("codex")
    assert claude_facts.readiness == ReadinessState.READY
    assert codex_facts.readiness == ReadinessState.READY

    # The readiness gate said ready; the spawn path must launch the exact
    # same binary it was declared ready for.
    assert _resolve_claude_binary() == str(claude_target)
    assert _resolve_codex_binary() == str(codex_target)


def test_worker_execution_facts_detail_names_candidates_when_unavailable(monkeypatch, tmp_path):
    """When nothing resolves, the ExecutorFacts the worker hands to
    resolution carry a detail naming the searched candidates, and that
    detail reaches the `executor_unavailable` diagnostic."""
    from config.settings import settings

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _block_path(monkeypatch)
    monkeypatch.setattr(settings, "codex_binary", "codex", raising=False)

    worker = _make_worker(tmp_path)
    facts = worker._execution_facts()
    codex_facts = facts.for_executor("codex")

    assert codex_facts.readiness == ReadinessState.UNAVAILABLE
    assert codex_facts.unavailable_detail is not None
    assert "/usr/local/bin/codex" in codex_facts.unavailable_detail

    result = resolve_execution(
        ExecutionRequest(executor="codex"),
        facts=facts,
        context=ExecutionContext(),
    )
    messages = [d.message for d in result.diagnostics if d.code == "executor_unavailable"]
    assert messages and "/usr/local/bin/codex" in messages[0]
