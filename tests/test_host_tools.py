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

CHAT = "chat-synthetic-1"


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sender:
    def __init__(self):
        self.sent: list[str] = []
        self.next_id = 100

    def __call__(self, text: str) -> list[int]:
        self.sent.append(text)
        self.next_id += 1
        return [self.next_id]


@pytest.fixture
def clock() -> Clock:
    return Clock(time.time())


@pytest.fixture
def sender() -> Sender:
    return Sender()


@pytest.fixture
def store(tmp_path) -> host_approval.ApprovalStore:
    return host_approval.ApprovalStore(tmp_path / "approvals.db")


@pytest.fixture
def gated(config, store, sender, clock) -> HostToolsConfig:
    config.approval_enabled = True
    config.approvals = host_approval.ApprovalGate(
        timeout_seconds=86400, chat_id=CHAT, store=store, send=sender, clock=clock,
    )
    return config


def _approve(store, sender, clock, *, text="APPROVE", chat=CHAT):
    with store._connect() as conn:  # the newest request's message
        message_id = int(conn.execute(
            "SELECT message_ids FROM host_approval_requests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0].strip("[]"))
    return store.decide_by_reply(message_id, text, chat_id=chat, now=clock.now)


def test_gate_off_by_default_refuses_without_asking(config, runner, run_as_ok, sender):
    assert config.approval_enabled is False
    with pytest.raises(HostToolError, match="refused"):
        host_tools.shell(config, {"argv": ["uptime"]}, caller="c")
    assert sender.sent == [] and runner.calls == []


def test_request_shows_the_exact_command_and_waits(gated, runner, run_as_ok, sender):
    argv = ["grep", "-c", "a_b*c [x]", "Work/Plan.md"]
    result = host_tools.shell(gated, {"argv": argv, "cwd_root": "vault"}, caller="oauth:client-a")
    assert result["rule"] == "pending-approval" and result["exit_code"] is None
    assert runner.calls == []
    message = sender.sent[0]
    assert "grep -c 'a_b*c [x]' Work/Plan.md" in message  # unshortened, markup untouched
    assert "Caller: oauth:client-a" in message
    assert "Runs as: lifeos-tools" in message
    assert "Directory: vault/." in message
    assert "Asked: " in message and "Expires: " in message and "Command hash: " in message
    assert _audit(gated)[-1]["decision"] == "approval-pending"


def test_approved_command_runs_once_as_the_low_privilege_account(gated, runner, run_as_ok, sender, store, clock):
    call = {"argv": ["uptime"]}
    host_tools.shell(gated, call, caller="oauth:client-a")
    clock.now += 5 * 3600  # approved hours later
    row = _approve(store, sender, clock)
    assert row.state == "approved" and row.approver == f"telegram:{CHAT}"
    clock.now += 60
    result = host_tools.shell(gated, call, caller="oauth:client-a")
    assert result["rule"] == "approved" and result["exit_code"] == 0
    assert runner.calls[0]["argv"] == ["sudo", "-n", "-u", "lifeos-tools", "--", "uptime"]
    record = _audit(gated)[-1]
    assert record["decision"] == "approved" and record["request_id"] == row.id
    assert record["approver"] == f"telegram:{CHAT}" and record["request_age_s"] > 0
    assert "ran (exit 0)" in sender.sent[-1] and "Asked 5h 1m ago" in sender.sent[-1]
    # Spent: the same call asks again rather than running a second time.
    again = host_tools.shell(gated, call, caller="oauth:client-a")
    assert again["rule"] == "pending-approval" and len(runner.calls) == 1


def test_waiting_request_is_not_sent_twice(gated, runner, run_as_ok, sender):
    for _ in range(3):
        result = host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
        assert result["rule"] == "pending-approval"
    assert len(sender.sent) == 1 and runner.calls == []


def test_approval_is_bound_to_caller_argv_and_directory(gated, runner, run_as_ok, sender, store, clock):
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="oauth:client-a")
    _approve(store, sender, clock)
    for call, caller in [
        ({"argv": ["uptime", "-p"]}, "oauth:client-a"),
        ({"argv": ["uptime"], "cwd_root": "vault"}, "oauth:client-a"),
        ({"argv": ["uptime"]}, "oauth:client-b"),
    ]:
        assert host_tools.shell(gated, call, caller=caller)["rule"] == "pending-approval"
    assert runner.calls == []
    assert host_tools.shell(gated, {"argv": ["uptime"]}, caller="oauth:client-a")["rule"] == "approved"


def test_only_the_configured_chat_can_decide(gated, run_as_ok, sender, store, clock):
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    assert _approve(store, sender, clock, chat="chat-someone-else") is None
    assert _approve(store, sender, clock).state == "approved"
    # A second reply to the same message is not a new decision.
    assert _approve(store, sender, clock) is None


def test_denied_request_is_reported_once_then_can_be_asked_again(gated, runner, run_as_ok, sender, store, clock):
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    assert _approve(store, sender, clock, text="no").state == "denied"
    with pytest.raises(HostToolError, match="refused request"):
        host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    assert _audit(gated)[-1]["decision"] == "approval-denied"
    assert host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")["rule"] == "pending-approval"
    assert len(sender.sent) == 2 and runner.calls == []


def test_requests_and_unused_approvals_expire_and_are_logged(gated, runner, run_as_ok, sender, store, clock):
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    host_tools.shell(gated, {"argv": ["hostname"]}, caller="c")
    _approve(store, sender, clock)  # approves the newest (hostname); its caller never comes back
    clock.now += 86400 + 1
    assert _approve(store, sender, clock) is None
    host_tools.shell(gated, {"argv": ["date"]}, caller="c")  # any later call sweeps
    decisions = [r["decision"] for r in _audit(gated)]
    assert "approval-expired-pending" in decisions and "approval-expired-approved" in decisions
    assert runner.calls == []


def test_expiry_can_be_turned_off(gated, runner, run_as_ok, sender, store, clock):
    gated.approvals.timeout_seconds = None
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    assert "Expires: never" in sender.sent[0]
    clock.now += 400 * 86400
    assert _approve(store, sender, clock).state == "approved"
    assert host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")["rule"] == "approved"


def test_approval_timeout_setting():
    assert host_tools.approval_timeout(0) is None
    assert host_tools.approval_timeout(-1) is None
    assert host_tools.approval_timeout(5) == 60
    assert host_tools.approval_timeout(86400) == 86400


def test_two_racing_calls_spend_one_approval(store, clock):
    rid = store.create(caller="c", command_hash="h", chat_id=CHAT, expires_at=None, now=clock.now)
    store.attach_messages(rid, [9])
    store.decide_by_reply(9, "approve", chat_id=CHAT, now=clock.now)
    assert store.consume(rid, "other-hash", now=clock.now) is False
    assert store.consume(rid, "h", now=clock.now) is True
    assert store.consume(rid, "h", now=clock.now) is False


@pytest.mark.parametrize("argv, why", [
    (["bash", "-c", "uptime"], "never run through approval"),
    (["python3.12", "-V"], "never run through approval"),
    (["curl", "https://example.invalid"], "never run through approval"),
    (["git", "push", "origin"], "never run through approval"),
    (["cat", "/etc/hostname"], "absolute"),
    (["cat", "~/notes"], "absolute"),
    (["cat", "--file=/etc/hostname"], "absolute"),
    (["cat", "../outside/secret.txt"], "'..'"),
    (["cat", ".env"], "denied path"),
    (["cat", "data/session.pickle"], "denied path"),
    (["wc", "-l", "data/notes.txt"], "'data/'"),
])
def test_dangerous_commands_are_refused_without_asking(gated, runner, run_as_ok, sender, argv, why):
    with pytest.raises(HostToolError, match=why):
        host_tools.shell(gated, {"argv": argv}, caller="c")
    assert sender.sent == [] and runner.calls == []
    assert _audit(gated)[-1]["decision"] == "refused"


def test_symlinked_argument_out_of_a_root_is_refused(gated, roots, runner, run_as_ok, sender):
    (roots["lifeos"] / "escape").symlink_to(roots["outside"], target_is_directory=True)
    with pytest.raises(HostToolError, match="outside the readable roots"):
        host_tools.shell(gated, {"argv": ["cat", "escape/secret.txt"]}, caller="c")
    assert sender.sent == []


def test_too_long_or_unsendable_requests_are_refused(gated, run_as_ok, sender, monkeypatch):
    with pytest.raises(HostToolError, match="too long"):
        host_tools.shell(gated, {"argv": ["echo", "x" * 4000]}, caller="c")
    gated.approvals.send = lambda text: []
    with pytest.raises(HostToolError, match="could not be sent"):
        host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")


def test_pending_requests_per_caller_are_capped(gated, run_as_ok, sender):
    for i in range(host_approval.MAX_PENDING_PER_CALLER):
        host_tools.shell(gated, {"argv": ["echo", str(i)]}, caller="c")
    with pytest.raises(HostToolError, match="already waiting"):
        host_tools.shell(gated, {"argv": ["echo", "more"]}, caller="c")
    assert host_tools.shell(gated, {"argv": ["echo", "more"]}, caller="other")["rule"] == "pending-approval"


def test_gate_refuses_without_a_configured_chat(config, store, run_as_ok):
    config.approval_enabled = True
    config.approvals = host_approval.ApprovalGate(timeout_seconds=60, chat_id="", store=store)
    with pytest.raises(HostToolError, match="TELEGRAM_CHAT_ID"):
        host_tools.shell(config, {"argv": ["uptime"]}, caller="c")


def test_default_sender_sends_plain_text(monkeypatch):
    from api.services import telegram

    calls = []
    monkeypatch.setattr(
        telegram, "send_message_capture_ids",
        lambda text, **kw: calls.append((text, kw)) or [1],
    )
    send = host_approval._default_sender(CHAT)
    assert send("a_b *c*") == [1]
    assert calls == [("a_b *c*", {"chat_id": CHAT, "parse_mode": None})]


def test_operator_reply_text_names_the_request_and_its_age(store, clock):
    rid = store.create(caller="oauth:client-a", command_hash="h", chat_id=CHAT, expires_at=None, now=clock.now - 7200)
    store.attach_messages(rid, [5])
    row = store.decide_by_reply(5, "approve", chat_id=CHAT, now=clock.now)
    text = host_approval.approval_reply_text(row, now=clock.now)
    assert rid[:8] in text and "2h 0m ago" in text and "oauth:client-a" in text


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


def test_host_write_sent_as_a_notification_runs_once_and_is_audited(monkeypatch, config, roots, caplog):
    import logging

    from fastapi.testclient import TestClient

    srv = _server(monkeypatch, True)
    srv._host_tools_config = config
    client = TestClient(mcp_server.build_http_app(srv, bearer_token="synthetic-token"))
    caplog.set_level(logging.INFO, logger=mcp_server.logger.name)
    notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {
        "name": "lifeos_fs_write",
        "arguments": {"root": "scratch", "path": "n/a.md", "content": "once\n", "mode": "append"},
    }}
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-token"}, json=notification)
    assert resp.status_code == 202
    assert resp.content == b""
    assert (roots["scratch"] / "n" / "a.md").read_text() == "once\n"
    writes = [r for r in _audit(config) if r.get("tool") == "lifeos_fs_write"]
    assert len(writes) == 1
    assert writes[0]["caller"] == "full-access-token"
    lines = [r for r in caplog.records if r.getMessage().startswith("mcp audit:")]
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING
    assert "caller=full-access-token" in message
    assert "tool=lifeos_fs_write" in message
    assert "host_write=True" in message
    assert "failed=False" in message


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


# ── git path arguments, vault commit literalness, audit ordering ─────────


@pytest.mark.parametrize("argv", [
    ["git", "diff", ".env", "api/main.py"],
    ["git", "diff", "api/main.py", "data/notes.txt"],
    ["git", "show", "HEAD:.env"],
    ["git", "show", "HEAD:data/notes.txt"],
    ["git", "show", "HEAD:./data/notes.txt"],
    ["git", "log", "--", "data/notes.txt"],
    ["git", "log", "--", ".env"],
    ["git", "log", "--", "../outside/secret.txt"],
    ["git", "diff", "--no-index", "api/main.py", "api/main.py"],
    ["git", "log", ":(top)data"],
])
def test_git_path_arguments_stay_inside_the_readable_roots(config, runner, run_as_ok, argv):
    with pytest.raises(HostToolError, match="refused"):
        host_tools.shell(config, {"argv": argv}, caller="c")
    assert runner.calls == []


def test_git_path_arguments_are_confined_from_a_cwd_that_is_not_a_repo(config, runner, run_as_ok):
    with pytest.raises(HostToolError, match="refused"):
        host_tools.shell(config, {"argv": ["git", "diff", "server.log", "../.env"], "cwd_root": "logs"}, caller="c")
    host_tools.shell(config, {"argv": ["git", "diff", "a.log", "b.log"], "cwd_root": "logs"}, caller="c")
    assert runner.calls[0]["cwd"].endswith("logs")


def test_git_revisions_and_in_root_paths_are_allowed(config, runner, run_as_ok):
    host_tools.shell(config, {"argv": ["git", "show", "HEAD:api/main.py"]}, caller="c")
    host_tools.shell(config, {"argv": ["git", "log", "-n5", "main..HEAD", "--", "api"]}, caller="c")
    assert "--literal-pathspecs" in runner.calls[0]["argv"]
    assert len(runner.calls) == 2


def test_vault_commit_takes_the_path_literally(config, roots):
    vault = roots["vault"]
    _git("init", "-q", cwd=vault)
    _git("config", "user.email", "test@example.com", cwd=vault)
    _git("config", "user.name", "Synthetic Tester", cwd=vault)
    (vault / "Work" / "Other.md").write_text("one\n")
    _git("add", "-A", cwd=vault)
    _git("commit", "-q", "-m", "init", cwd=vault)
    (vault / "Work" / "Other.md").write_text("dirty\n")
    config.vault_autocommit = True

    host_tools.fs_write(config, {"root": "vault", "path": "Work/*.md", "content": "star\n"})

    assert _git("show", "--name-only", "--format=", "HEAD", cwd=vault).strip() == "Work/*.md"
    assert "Work/Other.md" in _git("status", "--porcelain", cwd=vault)


def test_shell_records_the_command_before_it_runs(config, run_as_ok):
    seen = []

    def runner(argv, **kwargs):
        seen.append(_audit(config))
        raise RuntimeError("process went away")

    config.runner = runner
    with pytest.raises(RuntimeError):
        host_tools.shell(config, {"argv": ["systemctl", "restart", "lifeos-mcp-http"]}, caller="c")
    started = seen[0][-1]
    assert started["decision"] == "started" and started["rule"] == "systemctl restart"
    assert started["argv"] == ["systemctl", "restart", "lifeos-mcp-http"] and started["caller"] == "c"


@pytest.mark.parametrize("argv", [
    ["git", "-c", "core.pager=sh", "log"],
    ["git", "-C", "api", "log"],
    ["git", "--git-dir=x", "log"],
])
def test_git_options_before_the_subcommand_are_not_sent_for_approval(gated, runner, run_as_ok, sender, argv):
    with pytest.raises(HostToolError, match="before the subcommand"):
        host_tools.shell(gated, {"argv": argv}, caller="c")
    assert sender.sent == [] and runner.calls == []


@pytest.mark.parametrize("command", [
    "find", "systemd-run", "sed", "make", "tar", "awk", "perl", "xargs", "watch",
    "nohup", "setsid", "chroot", "flock",
])
def test_command_runners_are_not_sent_for_approval(gated, runner, run_as_ok, sender, command):
    with pytest.raises(HostToolError, match="never run through approval"):
        host_tools.shell(gated, {"argv": [command, "x"]}, caller="c")
    assert sender.sent == [] and runner.calls == []


def test_reading_a_log_file_redacts_tokens(config, roots):
    (roots["logs"] / "server.log").write_text("ok\nerror bot42:SYNTHETICtoken_abc here\n")
    content = host_tools.fs_read(config, {"root": "logs", "path": "server.log"})["content"]
    assert "SYNTHETIC" not in content and "ok" in content


# ── revision paths, audit redaction, denylist coverage ───────────────────


@pytest.mark.parametrize("argv", [
    ["git", "show", "HEAD:.env"],
    ["git", "show", "HEAD:data/notes.txt"],
    ["git", "show", "HEAD:../outside.txt"],
    ["git", "show", "HEAD:./data/notes.txt"],
])
def test_git_revision_paths_refused_by_the_allowlist_are_not_sent_for_approval(
    gated, runner, run_as_ok, sender, argv,
):
    with pytest.raises(HostToolError, match="Not sent for approval"):
        host_tools.shell(gated, {"argv": argv}, caller="c")
    assert sender.sent == [] and runner.calls == []


TOKEN = "bot42:SYNTHETICtoken_abc"


def test_audit_records_redact_tokens_in_argv_and_reasons(gated, run_as_ok):
    host_tools.shell(gated, {"argv": ["git", "log", f"--since={TOKEN}"]}, caller="c")
    with pytest.raises(HostToolError):
        host_tools.shell(gated, {"argv": ["ls", f"../{TOKEN}"]}, caller="c")
    with pytest.raises(HostToolError):
        host_tools.shell(gated, {"argv": ["cat", f"../{TOKEN}"]}, caller="c")
    assert "SYNTHETIC" not in gated.audit_path.read_text()
    records = _audit(gated)
    assert [r["decision"] for r in records] == ["started", "git log", "refused", "refused"]
    assert "bot<REDACTED>" in records[0]["argv"][2]


def test_command_does_not_run_when_the_started_record_cannot_be_written(config, runner, run_as_ok, tmp_path):
    config.audit_path = tmp_path  # a directory: the append fails
    with pytest.raises(HostToolError, match="audit log"):
        host_tools.shell(config, {"argv": ["ls"]}, caller="c")
    assert runner.calls == []


def test_fs_read_redacts_tokens_whichever_root_alias_is_used(config, roots):
    (roots["logs"] / "server.log").write_text(f"error {TOKEN} here\n")
    for root, path in [("logs", "server.log"), ("lifeos", "logs/server.log")]:
        content = host_tools.fs_read(config, {"root": root, "path": path})["content"]
        assert "SYNTHETIC" not in content and "here" in content


ENV_AND_CERT_NAMES = [".envrc", ".env.local", ".env.production", "prod.env", "server.crt", "ca.cer", "tls.pem"]


@pytest.mark.parametrize("name", ENV_AND_CERT_NAMES)
def test_env_and_certificate_names_are_denied_for_every_tool(config, roots, runner, run_as_ok, name):
    (roots["vault"] / name).write_text("synthetic\n")
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_read(config, {"root": "vault", "path": name})
    with pytest.raises(HostToolError, match="not available"):
        host_tools.fs_write(config, {"root": "vault", "path": name, "content": "x"})
    assert name not in {e["name"] for e in host_tools.fs_list(config, {"root": "vault"})["entries"]}
    for argv in (["ls", name], ["tail", name]):
        with pytest.raises(HostToolError, match="refused"):
            host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="c")
    assert runner.calls == []


# ── approval concurrency, account binding, vault literal paths, listener chat ──


def test_concurrent_calls_spend_one_approval(tmp_path, clock):
    import threading

    store = host_approval.ApprovalStore(tmp_path / "race.db")
    for round_no in range(25):
        digest = f"h{round_no}"
        rid = store.create(caller="c", command_hash=digest, chat_id=CHAT, expires_at=None, now=clock.now)
        store.attach_messages(rid, [round_no + 1])
        store.decide_by_reply(round_no + 1, "approve", chat_id=CHAT, now=clock.now)
        gate = host_approval.ApprovalGate(
            timeout_seconds=None, chat_id=CHAT, store=store, send=lambda text: [1], clock=clock,
        )
        barrier = threading.Barrier(8)
        outcomes: list[str] = []

        def attempt():
            barrier.wait()
            outcomes.append(gate.check(caller="c", command_hash=digest, details="d").outcome)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("run") == 1, outcomes


def test_approval_is_bound_to_the_run_as_account(gated, runner, run_as_ok, sender, store, clock):
    host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")
    _approve(store, sender, clock)
    gated.run_as = "other-tools"
    assert host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")["rule"] == "pending-approval"
    assert len(sender.sent) == 2 and runner.calls == []
    gated.run_as = "lifeos-tools"
    assert host_tools.shell(gated, {"argv": ["uptime"]}, caller="c")["rule"] == "approved"


def test_vault_commit_treats_pathspec_magic_in_a_file_name_literally(config, roots):
    vault = roots["vault"]
    _git("init", "-q", cwd=vault)
    _git("config", "user.email", "test@example.com", cwd=vault)
    _git("config", "user.name", "Synthetic Tester", cwd=vault)
    (vault / "other.md").write_text("one\n")
    _git("add", "-A", cwd=vault)
    _git("commit", "-q", "-m", "init", cwd=vault)
    (vault / "other.md").write_text("dirty\n")
    config.vault_autocommit = True

    host_tools.fs_write(config, {"root": "vault", "path": ":(glob)*.md", "content": "magic\n"})

    assert _git("show", "--name-only", "--format=", "HEAD", cwd=vault).strip() == ":(glob)*.md"
    assert "other.md" in _git("status", "--porcelain", cwd=vault)


class _ListenerSettings:
    telegram_chat_id = CHAT


@pytest.fixture
def approval_listener(tmp_path, monkeypatch):
    from api.services import telegram

    db = tmp_path / "listener-approvals.db"
    store = host_approval.ApprovalStore(db)
    monkeypatch.setattr(host_approval, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(host_approval, "ApprovalStore", lambda: store)
    monkeypatch.setattr(host_tools, "_audit_path", lambda: tmp_path / "listener-audit.jsonl")
    monkeypatch.setattr(telegram, "settings", _ListenerSettings)
    replies: list[str] = []
    monkeypatch.setattr(telegram, "send_message_capture_ids", lambda text, **kw: replies.append(text) or [1])

    def listener(chat_id: str):
        obj = object.__new__(telegram.TelegramBotListener)
        obj._chat_id = chat_id
        return obj

    return SimpleNamespace(store=store, listener=listener, replies=replies)


def _open_request(store, chat: str, message_id: int = 7) -> str:
    rid = store.create(caller="c", command_hash="h", chat_id=chat, expires_at=None)
    store.attach_messages(rid, [message_id])
    return rid


def test_listener_decides_only_for_the_configured_chat(approval_listener):
    store = approval_listener.store
    other = _open_request(store, "chat-other")
    assert approval_listener.listener("chat-other")._maybe_decide_host_approval(7, "APPROVE") is False
    assert store.get(other).state == "pending"

    mine = _open_request(store, CHAT, message_id=8)
    assert approval_listener.listener(CHAT)._maybe_decide_host_approval(8, "APPROVE") is True
    assert store.get(mine).state == "approved"
    assert approval_listener.replies and "Approved request" in approval_listener.replies[0]
