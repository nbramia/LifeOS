"""Tests for the scoped host tools (api/services/host_tools.py) and their
Telegram approval gate (api/services/host_approval.py).

All data is synthetic. No command is ever executed: the shell runner is a
recording fake, and the run-as account check is stubbed except in the tests
that exercise it directly.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server
from api.services import host_approval, host_tools
from api.services.host_tools import HostToolError, HostToolsConfig

pytestmark = pytest.mark.unit


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "ok\n", stderr: str = ""):
        self.calls: list[dict] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    # A vault path with a space in it, like many real vault folders.
    vault = tmp_path / "Synthetic Vault"
    (vault / "Work").mkdir(parents=True)
    (vault / "Work" / "Plan.md").write_text("# Plan\nsynthetic\n")
    (vault / ".env").write_text("SYNTHETIC=1\n")
    (vault / "data").mkdir()
    (vault / "data" / "session.pickle").write_bytes(b"\x80\x04synthetic")
    scratch = tmp_path / "scratch"
    lifeos = tmp_path / "lifeos"
    (lifeos / "api").mkdir(parents=True)
    (lifeos / "api" / "main.py").write_text("print('synthetic')\n")
    (lifeos / "data").mkdir()
    (lifeos / "data" / "notes.txt").write_text("private\n")
    (lifeos / ".env").write_text("LIFEOS_SYNTHETIC=1\n")
    (lifeos / "logs").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the roots\n")
    return {"vault": vault, "scratch": scratch, "lifeos": lifeos, "logs": lifeos / "logs", "outside": outside}


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def config(roots, runner, tmp_path) -> HostToolsConfig:
    return HostToolsConfig(
        roots={k: v for k, v in roots.items() if k != "outside"},
        run_as="lifeos-tools",
        vault_autocommit=False,
        audit_path=tmp_path / "audit.jsonl",
        runner=runner,
    )


@pytest.fixture
def run_as_ok(monkeypatch):
    monkeypatch.setattr(host_tools, "check_run_as", lambda user: user)


def _audit(config: HostToolsConfig) -> list[dict]:
    return [json.loads(line) for line in config.audit_path.read_text().splitlines()]


# ── path containment ─────────────────────────────────────────────────────


def test_read_inside_a_root_with_a_space_in_its_path(config):
    result = host_tools.fs_read(config, {"root": "vault", "path": "Work/Plan.md"})
    assert result["content"].startswith("# Plan")
    assert result["path"] == "Work/Plan.md"


@pytest.mark.parametrize("path", ["/etc/hostname", "~/notes.md", "../outside/secret.txt", "Work/../../x"])
def test_absolute_home_and_parent_paths_are_refused(config, path):
    with pytest.raises(HostToolError):
        host_tools.fs_read(config, {"root": "vault", "path": path})


def test_unknown_root_is_refused(config):
    with pytest.raises(HostToolError, match="root must be one of"):
        host_tools.fs_read(config, {"root": "home", "path": "x"})


def test_symlink_out_of_the_root_is_refused_for_reads(config, roots):
    (roots["vault"] / "escape.md").symlink_to(roots["outside"] / "secret.txt")
    (roots["vault"] / "escape-dir").symlink_to(roots["outside"], target_is_directory=True)
    with pytest.raises(HostToolError, match="outside the root"):
        host_tools.fs_read(config, {"root": "vault", "path": "escape.md"})
    with pytest.raises(HostToolError, match="outside the root"):
        host_tools.fs_read(config, {"root": "vault", "path": "escape-dir/secret.txt"})
    with pytest.raises(HostToolError, match="outside the root"):
        host_tools.fs_list(config, {"root": "vault", "path": "escape-dir"})


def test_symlink_inside_the_root_can_be_read_but_not_written(config, roots):
    (roots["vault"] / "alias.md").symlink_to(roots["vault"] / "Work" / "Plan.md")
    (roots["vault"] / "WorkLink").symlink_to(roots["vault"] / "Work", target_is_directory=True)
    assert "synthetic" in host_tools.fs_read(config, {"root": "vault", "path": "alias.md"})["content"]
    with pytest.raises(HostToolError, match="symlinks"):
        host_tools.fs_write(config, {"root": "vault", "path": "alias.md", "content": "x"})
    with pytest.raises(HostToolError, match="symlinks"):
        host_tools.fs_write(config, {"root": "vault", "path": "WorkLink/new.md", "content": "x"})
    assert (roots["vault"] / "Work" / "Plan.md").read_text().startswith("# Plan")


def test_write_through_a_symlink_out_of_the_root_is_refused(config, roots):
    (roots["vault"] / "out").symlink_to(roots["outside"], target_is_directory=True)
    with pytest.raises(HostToolError):
        host_tools.fs_write(config, {"root": "vault", "path": "out/secret.txt", "content": "overwritten"})
    assert (roots["outside"] / "secret.txt").read_text() == "outside the roots\n"


def test_symlink_to_a_denied_file_inside_the_root_is_refused(config, roots):
    (roots["vault"] / "innocent.md").symlink_to(roots["vault"] / ".env")
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_read(config, {"root": "vault", "path": "innocent.md"})


@pytest.mark.parametrize("path", [".env", "data/session.pickle", ".git/config", "keys/id_ed25519", "x/server.pem"])
def test_denylisted_paths_are_refused(config, path):
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_read(config, {"root": "vault", "path": path})
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_write(config, {"root": "vault", "path": path, "content": "x"})


def test_lifeos_data_directory_and_env_are_refused(config):
    with pytest.raises(HostToolError, match="'data/'"):
        host_tools.fs_read(config, {"root": "lifeos", "path": "data/notes.txt"})
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_read(config, {"root": "lifeos", "path": ".env"})
    assert "synthetic" in host_tools.fs_read(config, {"root": "lifeos", "path": "api/main.py"})["content"]


def test_lifeos_code_and_logs_are_read_only(config):
    for root in ("lifeos", "logs"):
        with pytest.raises(HostToolError, match="root must be one of: vault, scratch"):
            host_tools.fs_write(config, {"root": root, "path": "api/main.py", "content": "x"})


def test_listing_hides_denied_entries(config):
    names = {e["name"] for e in host_tools.fs_list(config, {"root": "lifeos", "path": ""})["entries"]}
    assert names == {"api", "logs"}
    names = {e["name"] for e in host_tools.fs_list(config, {"root": "vault"})["entries"]}
    assert ".env" not in names and "Work" in names


# ── writes ───────────────────────────────────────────────────────────────


def test_write_create_append_and_size_cap(config, roots):
    host_tools.fs_write(config, {"root": "scratch", "path": "notes/a.md", "content": "one\n"})
    assert (roots["scratch"] / "notes" / "a.md").read_text() == "one\n"
    with pytest.raises(HostToolError, match="already exists"):
        host_tools.fs_write(config, {"root": "scratch", "path": "notes/a.md", "content": "x", "mode": "create"})
    host_tools.fs_write(config, {"root": "scratch", "path": "notes/a.md", "content": "two\n", "mode": "append"})
    assert (roots["scratch"] / "notes" / "a.md").read_text() == "one\ntwo\n"
    big = "x" * (host_tools.WRITE_MAX_BYTES + 1)
    with pytest.raises(HostToolError, match="over"):
        host_tools.fs_write(config, {"root": "scratch", "path": "big.txt", "content": big})


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_vault_write_commits_only_that_path(config, roots):
    vault = roots["vault"]
    _git("init", "-q", cwd=vault)
    _git("config", "user.email", "test@example.com", cwd=vault)
    _git("config", "user.name", "Synthetic Tester", cwd=vault)
    _git("add", "Work/Plan.md", cwd=vault)
    _git("commit", "-q", "-m", "init", cwd=vault)
    (vault / "Work" / "Plan.md").write_text("# Plan\nunrelated edit\n")
    config.vault_autocommit = True

    result = host_tools.fs_write(config, {"root": "vault", "path": "Inbox/New note.md", "content": "hello\n"})

    assert result["commit"]
    assert _git("show", "--name-only", "--format=", "HEAD", cwd=vault).split() == ["Inbox/New", "note.md"]
    assert "Work/Plan.md" in _git("status", "--porcelain", cwd=vault)


# ── shell: fail closed ───────────────────────────────────────────────────


def test_shell_refuses_when_no_run_as_account_is_configured(config, runner):
    config.run_as = ""
    result = host_tools.call(config, "lifeos_shell", {"argv": ["git", "status"]}, caller="oauth:client-a")
    assert result["isError"] and "not configured" in result["content"][0]["text"]
    assert runner.calls == []
    assert _audit(config)[-1]["decision"] == "refused"


def test_run_as_must_be_an_existing_non_root_other_account(monkeypatch):
    monkeypatch.setattr(host_tools.shutil, "which", lambda name: "/usr/bin/sudo")
    accounts = {"root": 0, "lifeos": 1000, "lifeos-tools": 1500}

    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=accounts[name])

    monkeypatch.setattr(host_tools.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(host_tools.os, "geteuid", lambda: 1000)
    with pytest.raises(HostToolError, match="does not exist"):
        host_tools.check_run_as("nobody-synthetic")
    with pytest.raises(HostToolError, match="root"):
        host_tools.check_run_as("root")
    with pytest.raises(HostToolError, match="differ"):
        host_tools.check_run_as("lifeos")
    assert host_tools.check_run_as("lifeos-tools") == "lifeos-tools"


# ── shell: allowlist ─────────────────────────────────────────────────────


def test_allowlisted_git_status_runs_as_the_low_privilege_account(config, runner, run_as_ok, roots):
    result = host_tools.shell(config, {"argv": ["git", "status", "--short"]}, caller="full-access-token")
    assert result["exit_code"] == 0 and result["rule"] == "git status"
    call = runner.calls[0]
    assert call["argv"][:5] == ["sudo", "-n", "-u", "lifeos-tools", "--"]
    assert call["argv"][5:] == [*host_tools._GIT_PREFIX, "status", "--short"]
    assert call["cwd"] == str(roots["lifeos"].resolve())
    assert call["stdin"] == subprocess.DEVNULL
    entry = _audit(config)[-1]
    assert entry["argv"] == ["git", "status", "--short"] and entry["decision"] == "git status"


def test_git_diff_disables_external_diff_and_textconv(config, runner, run_as_ok):
    host_tools.shell(config, {"argv": ["git", "diff", "--stat", "HEAD~1"]}, caller="c")
    assert "--no-ext-diff" in runner.calls[0]["argv"] and "--no-textconv" in runner.calls[0]["argv"]


def test_restart_of_a_named_unit_uses_sudo_systemctl(config, runner, run_as_ok):
    host_tools.shell(config, {"argv": ["systemctl", "restart", "lifeos-api"]}, caller="c")
    assert runner.calls[0]["argv"][5:] == ["sudo", "-n", "/usr/bin/systemctl", "restart", "lifeos-api.service"]


@pytest.mark.parametrize("argv", [
    ["git", "log", "--output=/tmp/x"],
    ["git", "-c", "core.pager=sh", "log"],
    ["git", "push"],
    ["git", "diff", "--ext-diff"],
    ["systemctl", "restart", "lifeos-llm"],
    ["systemctl", "restart", "sshd"],
    ["systemctl", "stop", "lifeos-api"],
    ["systemctl", "status", "tailscaled"],
    ["ls", "../"],
    ["ls", "/etc"],
    ["ls", "data"],
    ["ls", "-R"],
    ["tail", "-f", "logs/server.log"],
    ["tail", "-n", "5", ".env"],
    ["rm", "-rf", "api"],
    ["bash", "-c", "id"],
])
def test_commands_outside_the_allowlist_are_refused(config, runner, run_as_ok, argv):
    with pytest.raises(HostToolError, match="refused"):
        host_tools.shell(config, {"argv": argv}, caller="c")
    assert runner.calls == []
    assert _audit(config)[-1]["decision"] == "refused"


@pytest.mark.parametrize("argv", [[], "git status", ["/bin/ls"], ["ls", "a\x00b"], [1]])
def test_malformed_argv_is_refused(config, run_as_ok, argv):
    with pytest.raises(HostToolError):
        host_tools.shell(config, {"argv": argv}, caller="c")


def test_ls_and_tail_inside_roots_are_allowed(config, runner, run_as_ok, roots):
    (roots["logs"] / "server.log").write_text("a\nb\n")
    host_tools.shell(config, {"argv": ["ls", "-la", "api"]}, caller="c")
    host_tools.shell(config, {"argv": ["tail", "-n", "20", "logs/server.log"]}, caller="c")
    assert runner.calls[1]["argv"][5:] == ["tail", "-n", "20", "--", "logs/server.log"]


def test_shell_cwd_is_confined_to_a_root(config, run_as_ok):
    with pytest.raises(HostToolError):
        host_tools.shell(config, {"argv": ["ls"], "cwd": "../outside"}, caller="c")
    with pytest.raises(HostToolError, match="'data/'"):
        host_tools.shell(config, {"argv": ["ls"], "cwd": "data"}, caller="c")


def test_shell_output_is_capped_and_redacted(config, runner, run_as_ok):
    runner.stdout = "token bot123456:SYNTHETICfakeTOKEN_value\n" + "x" * (host_tools.SHELL_OUTPUT_MAX_BYTES + 10)
    result = host_tools.shell(config, {"argv": ["git", "status"]}, caller="c")
    assert result["truncated"] is True
    assert len(result["stdout"].encode()) <= host_tools.SHELL_OUTPUT_MAX_BYTES
    runner.stdout = "token bot123456:SYNTHETICfakeTOKEN_value\n"
    assert "SYNTHETIC" not in host_tools.shell(config, {"argv": ["git", "status"]}, caller="c")["stdout"]


# ── shell: approval gate ─────────────────────────────────────────────────


def test_gate_off_by_default_refuses_without_asking(config, runner, run_as_ok):
    asked = []
    config.approver = lambda summary, caller: asked.append(summary) or "approved"
    assert config.approval_enabled is False
    with pytest.raises(HostToolError, match="refused"):
        host_tools.shell(config, {"argv": ["uptime"]}, caller="c")
    assert asked == [] and runner.calls == []


def test_approved_command_runs_as_the_low_privilege_account(config, runner, run_as_ok):
    asked = []
    config.approval_enabled = True
    config.approver = lambda summary, caller: asked.append((summary, caller)) or "approved"
    result = host_tools.shell(config, {"argv": ["uptime"]}, caller="oauth:client-a")
    assert result["rule"] == "approved"
    assert runner.calls[0]["argv"] == ["sudo", "-n", "-u", "lifeos-tools", "--", "uptime"]
    assert "uptime" in asked[0][0] and asked[0][1] == "oauth:client-a"


@pytest.mark.parametrize("verdict", ["denied", "timeout"])
def test_unapproved_command_is_refused(config, runner, run_as_ok, verdict):
    config.approval_enabled = True
    config.approver = lambda summary, caller: verdict
    with pytest.raises(HostToolError, match=verdict):
        host_tools.shell(config, {"argv": ["uptime"]}, caller="c")
    assert runner.calls == []
    assert _audit(config)[-1]["decision"] == f"approval-{verdict}"


def test_approval_store_decides_only_on_reply_to_its_message(tmp_path):
    store = host_approval.ApprovalStore(tmp_path / "approvals.db")
    rid = store.create(expires_at=9e12)
    store.attach_messages(rid, [101, 102])
    assert store.decide_by_reply(999, "approve") is False
    assert store.decide_by_reply(102, " APPROVE ") is True
    assert store.decision(rid) == "approved"
    other = store.create(expires_at=9e12)
    store.attach_messages(other, [200])
    assert store.decide_by_reply(200, "yes") is True
    assert store.decision(other) == "denied"
    expired = store.create(expires_at=1.0)
    store.attach_messages(expired, [300])
    assert store.decide_by_reply(300, "approve") is False


def test_request_approval_times_out_to_a_denial(tmp_path):
    store = host_approval.ApprovalStore(tmp_path / "approvals.db")
    now = [1000.0]
    sent = []
    verdict = host_approval.request_approval(
        "run uptime", timeout_seconds=30, store=store,
        send=lambda text: sent.append(text) or [7],
        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + 10),
    )
    assert verdict == "timeout"
    assert "APPROVE" in sent[0] and "run uptime" in sent[0]


def test_request_approval_returns_the_recorded_reply(tmp_path):
    store = host_approval.ApprovalStore(tmp_path / "approvals.db")
    now = [time.time()]  # the store checks expiry against the wall clock

    def sleep(_):
        store.decide_by_reply(7, "approve")
        now[0] += 1

    verdict = host_approval.request_approval(
        "run uptime", timeout_seconds=30, store=store, send=lambda text: [7],
        clock=lambda: now[0], sleep=sleep,
    )
    assert verdict == "approved"


def test_request_approval_denies_when_the_message_cannot_be_sent(tmp_path):
    store = host_approval.ApprovalStore(tmp_path / "approvals.db")
    verdict = host_approval.request_approval("x", timeout_seconds=30, store=store, send=lambda text: [])
    assert verdict == "denied"


# ── logs ─────────────────────────────────────────────────────────────────


def test_logs_tail_filter_and_redaction(config, roots):
    lines = [f"line {i}" for i in range(50)] + ["error bot42:SYNTHETICtoken_abc here"]
    (roots["logs"] / "server.log").write_text("\n".join(lines) + "\n")
    tail = host_tools.logs(config, {"service": "lifeos-api", "lines": 3})["lines"]
    assert tail[:2] == ["line 48", "line 49"] and "SYNTHETIC" not in tail[2]
    assert host_tools.logs(config, {"service": "lifeos-api", "contains": "line 1"})["lines"][0] == "line 1"
    with pytest.raises(HostToolError):
        host_tools.logs(config, {"service": "sshd"})


# ── audit and MCP surface ────────────────────────────────────────────────


def test_write_audit_records_a_hash_not_the_content(config):
    host_tools.call(config, "lifeos_fs_write",
                    {"root": "scratch", "path": "a.md", "content": "synthetic body"}, caller="oauth:client-a")
    entry = _audit(config)[-1]
    assert entry["caller"] == "oauth:client-a" and entry["path"] == "a.md" and entry["sha256"]
    assert "synthetic body" not in config.audit_path.read_text()


def test_refusals_are_tool_errors(config):
    result = host_tools.call(config, "lifeos_fs_read", {"root": "vault", "path": "/etc/passwd"}, caller="c")
    assert result["isError"] is True


def _server(monkeypatch, enabled: bool) -> mcp_server.LifeOSMCPServer:
    from config.settings import settings

    monkeypatch.setattr(settings, "host_tools_enabled", enabled)
    monkeypatch.setattr(
        mcp_server.LifeOSMCPServer, "_load_openapi_spec", lambda self: self._build_tools_fallback(),
    )
    return mcp_server.LifeOSMCPServer()


def test_host_tools_are_registered_only_when_enabled(monkeypatch):
    names = {t["name"] for t in _server(monkeypatch, False).tools}
    assert not names & host_tools.HOST_TOOL_NAMES
    names = {t["name"] for t in _server(monkeypatch, True).tools}
    assert host_tools.HOST_TOOL_NAMES <= names


def test_full_access_dispatch_runs_host_tools_with_its_caller(monkeypatch, config):
    srv = _server(monkeypatch, True)
    srv._host_tools_config = config
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "lifeos_fs_read", "arguments": {"root": "vault", "path": "Work/Plan.md"}}}
    response = mcp_server.dispatch(srv, request, caller="full-access-token")
    assert "synthetic" in response["result"]["content"][0]["text"]
    assert _audit(config)[-1]["caller"] == "full-access-token"


def test_oauth_clients_get_host_tools_only_when_the_operator_lists_them(monkeypatch, config):
    from config.settings import settings

    srv = _server(monkeypatch, True)
    srv._host_tools_config = config
    monkeypatch.setattr(settings, "host_tools_oauth_clients", "client-a, client-b")
    granted = mcp_server.OAuthToolAccess(srv, principal="client-a")
    other = mcp_server.OAuthToolAccess(srv, principal="client-z")
    assert host_tools.HOST_TOOL_NAMES <= {t["name"] for t in granted.tools()}
    assert not host_tools.HOST_TOOL_NAMES & {t["name"] for t in other.tools()}
    assert not other.callable("lifeos_fs_read")
    result = granted.call("lifeos_fs_read", {"root": "vault", "path": "Work/Plan.md"})
    assert "synthetic" in result["content"][0]["text"]
    assert _audit(config)[-1]["caller"] == "oauth:client-a"
    # A grant adds the host tools and nothing else.
    assert granted.allowed - other.allowed == host_tools.HOST_TOOL_NAMES
    annotations = {t["name"]: t["annotations"] for t in granted.tools()}
    assert annotations["lifeos_shell"]["destructiveHint"] is True
    assert annotations["lifeos_fs_read"]["readOnlyHint"] is True


def test_grant_list_is_ignored_while_host_tools_are_disabled(monkeypatch):
    from config.settings import settings

    srv = _server(monkeypatch, False)
    monkeypatch.setattr(settings, "host_tools_oauth_clients", "client-a")
    assert not host_tools.HOST_TOOL_NAMES & mcp_server.OAuthToolAccess(srv, principal="client-a").allowed


def test_agent_worker_registry_leaves_host_tools_out(monkeypatch):
    from api.services.agent_worker.tools import ToolRegistry

    registry = ToolRegistry(lifeos_mcp_server=_server(monkeypatch, True))
    names = {t["name"] for t in registry.definitions()}
    assert not names & host_tools.HOST_TOOL_NAMES
