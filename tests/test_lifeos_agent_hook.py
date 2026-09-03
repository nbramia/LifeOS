"""Tests for scripts/lifeos-agent-hook.sh (issue #849).

Runs the script via subprocess against a stub HTTP server (a bare
`http.server` in a thread on an ephemeral port) and asserts on what it
posted. Every subprocess call clears LIFEOS_AGENT_HOOK_TOKEN and
LIFEOS_API_URL from the environment and points LIFEOS_AGENT_HOOK_ENV at a
scratch path, so the operator's real token/URL (this box's real .env and
~/.config/lifeos/agent-hook.env) can never leak into a test run or be
read by one.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lifeos-agent-hook.sh"
# Resolved once from the real environment, not from a test's (possibly
# PATH-stripped) subprocess env — the interpreter invoking the script must
# always be found even when a test is exercising a missing-jq/curl PATH.
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.unit


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            body = None
        _CapturingHandler.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"registered": true}')

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def stub_server():
    _CapturingHandler.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _CapturingHandler.requests
    finally:
        server.shutdown()
        server.server_close()


def _dead_port() -> int:
    """A port nothing is listening on: bind then immediately close."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _base_env(tmp_path: Path, **overrides) -> dict:
    env = dict(os.environ)
    env.pop("LIFEOS_AGENT_HOOK_TOKEN", None)
    env.pop("LIFEOS_API_URL", None)
    env.pop("LIFEOS_TASK_ID", None)
    env.pop("WEZTERM_PANE", None)
    env.pop("XDG_RUNTIME_DIR", None)
    # Never let the script fall back to the operator's real config file.
    env["LIFEOS_AGENT_HOOK_ENV"] = str(tmp_path / "no-such-agent-hook.env")
    env.update(overrides)
    return env


def _run(argv, stdin: str, env: dict, timeout: float = 5.0):
    return subprocess.run(
        [BASH, str(SCRIPT), *argv],
        input=stdin.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=timeout,
    )


@pytest.mark.unit
def test_bash_syntax_check():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, timeout=5)
    assert r.returncode == 0, r.stderr.decode()


@pytest.mark.unit
def test_posts_session_start_with_expected_fields(stub_server, tmp_path):
    server, requests = stub_server
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="test-token-value",
        LIFEOS_API_URL=f"http://127.0.0.1:{server.server_port}",
        LIFEOS_TASK_ID="task-77",
    )
    stdin = json.dumps({
        "session_id": "hook-sid-1",
        "cwd": str(tmp_path),
        "transcript_path": "/tmp/does-not-matter.jsonl",
        "source": "startup",
    })
    r = _run(["claude_code", "session_start"], stdin, env)
    assert r.returncode == 0, r.stderr.decode()
    assert r.stdout == b""

    assert len(requests) == 1
    req = requests[0]
    assert req["path"] == "/api/agents/cli-sessions/events"
    assert req["headers"]["Authorization"] == "Bearer test-token-value"
    body = req["body"]
    assert body["engine"] == "claude_code"
    assert body["event"] == "session_start"
    assert body["session_id"] == "hook-sid-1"
    assert body["cwd"] == str(tmp_path)
    assert body["task_id"] == "task-77"
    assert "host" in body and body["host"]
    # No git repo in tmp_path -> branch resolves to empty.
    assert body["branch"] == ""
    # Outside wezterm -> no pane fields at all.
    assert "pane_id" not in body
    assert "wezterm_pid" not in body


@pytest.mark.unit
def test_posts_user_prompt_submit_with_prompt_preview(stub_server, tmp_path):
    server, requests = stub_server
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL=f"http://127.0.0.1:{server.server_port}",
    )
    stdin = json.dumps({
        "session_id": "hook-sid-2",
        "cwd": str(tmp_path),
        "prompt": "please refactor the widget",
    })
    r = _run(["claude_code", "user_prompt_submit"], stdin, env)
    assert r.returncode == 0
    body = requests[0]["body"]
    assert body["event"] == "user_prompt_submit"
    assert body["prompt_preview"] == "please refactor the widget"


@pytest.mark.unit
def test_git_branch_resolved_from_cwd(stub_server, tmp_path):
    server, requests = stub_server
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "synthetic-branch", str(repo)],
                    check=True, capture_output=True)
    # An unborn branch (no commits yet) makes `rev-parse --abbrev-ref HEAD`
    # print the literal "HEAD" on some git versions — commit once so the
    # branch ref actually resolves.
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "synthetic"],
        check=True, capture_output=True,
    )
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL=f"http://127.0.0.1:{server.server_port}",
    )
    stdin = json.dumps({"session_id": "hook-sid-3", "cwd": str(repo)})
    r = _run(["codex", "session_start"], stdin, env)
    assert r.returncode == 0
    body = requests[0]["body"]
    assert body["branch"] == "synthetic-branch"


@pytest.mark.unit
def test_pane_fields_present_only_when_wezterm_pane_set(stub_server, tmp_path):
    server, requests = stub_server
    rtdir = tmp_path / "xdg"
    sockdir = rtdir / "wezterm"
    sockdir.mkdir(parents=True)
    my_pid = os.getpid()  # always alive for the duration of this test
    (sockdir / f"gui-sock-{my_pid}").write_text("", encoding="utf-8")

    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL=f"http://127.0.0.1:{server.server_port}",
        WEZTERM_PANE="42",
        XDG_RUNTIME_DIR=str(rtdir),
    )
    stdin = json.dumps({"session_id": "hook-sid-4", "cwd": str(tmp_path)})
    r = _run(["claude_code", "stop"], stdin, env)
    assert r.returncode == 0, r.stderr.decode()
    body = requests[0]["body"]
    assert body["pane_id"] == 42
    assert body["wezterm_pid"] == my_pid


@pytest.mark.unit
def test_exits_0_when_api_unreachable_within_two_seconds(tmp_path):
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL=f"http://127.0.0.1:{_dead_port()}",
    )
    stdin = json.dumps({"session_id": "hook-sid-5", "cwd": str(tmp_path)})
    start = time.monotonic()
    r = _run(["claude_code", "session_start"], stdin, env, timeout=5.0)
    elapsed = time.monotonic() - start
    assert r.returncode == 0
    assert r.stdout == b""
    assert elapsed < 2.0


@pytest.mark.unit
def test_exits_0_when_jq_and_curl_missing(tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL="http://127.0.0.1:1",
        PATH=str(empty_bin),
    )
    stdin = json.dumps({"session_id": "hook-sid-6", "cwd": str(tmp_path)})
    r = _run(["claude_code", "session_start"], stdin, env)
    assert r.returncode == 0
    assert r.stdout == b""


@pytest.mark.unit
def test_exits_0_and_makes_no_request_when_no_token_configured(stub_server, tmp_path):
    server, requests = stub_server
    env = _base_env(
        tmp_path,
        LIFEOS_API_URL=f"http://127.0.0.1:{server.server_port}",
        # LIFEOS_AGENT_HOOK_TOKEN deliberately absent, and the env file
        # points at a nonexistent path, so no token is discoverable.
    )
    stdin = json.dumps({"session_id": "hook-sid-7", "cwd": str(tmp_path)})
    r = _run(["claude_code", "session_start"], stdin, env)
    assert r.returncode == 0
    assert r.stdout == b""
    assert requests == []


@pytest.mark.unit
def test_exits_0_on_empty_stdin(tmp_path):
    env = _base_env(
        tmp_path,
        LIFEOS_AGENT_HOOK_TOKEN="tok",
        LIFEOS_API_URL="http://127.0.0.1:1",
    )
    r = _run(["claude_code", "session_start"], "", env)
    assert r.returncode == 0
    assert r.stdout == b""


@pytest.mark.unit
def test_env_file_supplies_token_when_environment_unset(stub_server, tmp_path):
    """Values already in the environment take precedence over the file —
    but when the environment has none, the file alone is enough."""
    server, requests = stub_server
    env_file = tmp_path / "agent-hook.env"
    env_file.write_text(
        f'LIFEOS_API_URL=http://127.0.0.1:{server.server_port}\n'
        f'LIFEOS_AGENT_HOOK_TOKEN=file-token-value\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("LIFEOS_AGENT_HOOK_TOKEN", None)
    env.pop("LIFEOS_API_URL", None)
    env["LIFEOS_AGENT_HOOK_ENV"] = str(env_file)
    stdin = json.dumps({"session_id": "hook-sid-8", "cwd": str(tmp_path)})
    r = _run(["claude_code", "session_start"], stdin, env)
    assert r.returncode == 0, r.stderr.decode()
    assert len(requests) == 1
    assert requests[0]["headers"]["Authorization"] == "Bearer file-token-value"


@pytest.mark.unit
def test_environment_token_wins_over_env_file(stub_server, tmp_path):
    env_file = tmp_path / "agent-hook.env"
    env_file.write_text(
        "LIFEOS_API_URL=http://127.0.0.1:1\n"
        "LIFEOS_AGENT_HOOK_TOKEN=file-token-should-be-overridden\n",
        encoding="utf-8",
    )
    server, requests = stub_server
    env = dict(os.environ)
    env["LIFEOS_AGENT_HOOK_ENV"] = str(env_file)
    env["LIFEOS_API_URL"] = f"http://127.0.0.1:{server.server_port}"
    env["LIFEOS_AGENT_HOOK_TOKEN"] = "env-token-value"
    stdin = json.dumps({"session_id": "hook-sid-9", "cwd": str(tmp_path)})
    r = _run(["claude_code", "session_start"], stdin, env)
    assert r.returncode == 0, r.stderr.decode()
    assert requests[0]["headers"]["Authorization"] == "Bearer env-token-value"
