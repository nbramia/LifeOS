"""Tests for operator-facing delivery through the `hermes send` CLI
(api/services/hermes_notify.py).

The subprocess is stubbed at the `subprocess.run` boundary in every test —
nothing here ever spawns the real binary or sends a real message. The
contract under test is the one `hermes send --help` documents: exit 0 plus a
JSON object carrying the delivered message's `chat_id`/`message_id`, and any
other outcome meaning "this channel could not deliver".
"""
import json
import subprocess

import pytest

from api.services import hermes_notify

pytestmark = pytest.mark.unit


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_OK_PAYLOAD = {
    "success": True,
    "platform": "telegram",
    "chat_id": "5550001111",
    "message_id": "590",
    "note": "Sent to telegram home channel",
    "mirrored": True,
}


@pytest.fixture
def stub_binary(monkeypatch):
    monkeypatch.setattr(hermes_notify.shutil, "which", lambda name: "/fake/bin/hermes")


def _stub_run(monkeypatch, result, recorder=None):
    def _run(cmd, **kwargs):
        if recorder is not None:
            recorder.append((cmd, kwargs))
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(hermes_notify.subprocess, "run", _run)


def test_successful_send_returns_the_message_identity(monkeypatch, stub_binary):
    _stub_run(monkeypatch, _FakeCompleted(stdout=json.dumps(_OK_PAYLOAD)))
    delivery = hermes_notify.send_via_hermes("task finished")
    assert delivery is not None
    assert delivery.chat_id == "5550001111"
    assert delivery.message_id == "590"


def test_message_body_is_piped_on_stdin_not_argv(monkeypatch, stub_binary):
    calls = []
    _stub_run(monkeypatch, _FakeCompleted(stdout=json.dumps(_OK_PAYLOAD)), calls)
    hermes_notify.send_via_hermes("a body with spaces and 'quotes'")
    (cmd, kwargs), = calls
    assert cmd == ["/fake/bin/hermes", "send", "--to", "telegram", "--json"]
    assert kwargs["input"] == "a body with spaces and 'quotes'"


def test_subprocess_env_carries_no_anthropic_or_claude_credentials(monkeypatch, stub_binary):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-token")
    monkeypatch.setenv("HOME", "/home/synthetic")
    calls = []
    _stub_run(monkeypatch, _FakeCompleted(stdout=json.dumps(_OK_PAYLOAD)), calls)
    hermes_notify.send_via_hermes("hello")
    (_, kwargs), = calls
    env = kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["HOME"] == "/home/synthetic"


def test_missing_binary_degrades_to_none_without_raising(monkeypatch):
    monkeypatch.setattr(hermes_notify.shutil, "which", lambda name: None)
    monkeypatch.setattr(hermes_notify.os.path, "isfile", lambda path: False)

    def _explode(*args, **kwargs):  # pragma: no cover — must never be reached
        raise AssertionError("no subprocess may be spawned without a binary")
    monkeypatch.setattr(hermes_notify.subprocess, "run", _explode)

    assert hermes_notify.send_via_hermes("hello") is None


def test_nonzero_exit_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, _FakeCompleted(returncode=1, stderr="delivery failed"))
    assert hermes_notify.send_via_hermes("hello") is None


def test_unparseable_output_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, _FakeCompleted(stdout="not json at all"))
    assert hermes_notify.send_via_hermes("hello") is None


def test_reported_failure_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, _FakeCompleted(
        stdout=json.dumps({"success": False, "error": "no such target"}),
    ))
    assert hermes_notify.send_via_hermes("hello") is None


def test_missing_message_identity_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, _FakeCompleted(
        stdout=json.dumps({"success": True, "platform": "telegram"}),
    ))
    assert hermes_notify.send_via_hermes("hello") is None


def test_timeout_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, subprocess.TimeoutExpired(cmd="hermes", timeout=1))
    assert hermes_notify.send_via_hermes("hello") is None


def test_spawn_error_degrades_to_none(monkeypatch, stub_binary):
    _stub_run(monkeypatch, OSError("permission denied"))
    assert hermes_notify.send_via_hermes("hello") is None


def test_failure_log_does_not_echo_the_message_body(monkeypatch, stub_binary, caplog):
    _stub_run(monkeypatch, _FakeCompleted(returncode=1, stderr="chat 5550001111 rejected"))
    with caplog.at_level("WARNING"):
        assert hermes_notify.send_via_hermes("therapy notes for Alex Doe") is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "therapy notes" not in logged
    assert "5550001111" not in logged
