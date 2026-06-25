"""Self-restart primitive coverage (#401).

Two layers:

1. Python — `resume_pending()` honors the self-restart marker the detached
   restart primitive writes: the deliberately-killed session is finalized
   quietly (COMPLETED, no "could not be safely resumed" rollback notice), the
   marker is consumed, and a session *without* a marker still rolls back as
   before (regression guard).

2. Bash — `scripts/server.sh restart-worker-detached` sends the final notice
   BEFORE it triggers the restart (so the doctor's "Shipped" notice beats the
   worker's SIGTERM), and `classify-change` picks the worker path for an
   agent_worker-touching diff vs. the api path for an api-only diff. (bash isn't
   pytest-covered elsewhere; these drive the script as a subprocess with stubbed
   systemctl/python on PATH so nothing real is restarted.)
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import (
    AGENT_TAG,
    COMPLETED_TAG,
    RUNNING_TAG,
    Worker,
    _read_self_restart_marker,
    _self_restart_marker_path,
    write_self_restart_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SH = REPO_ROOT / "scripts" / "server.sh"


# ---------------------------------------------------------------------------
# Minimal FakeApi (tag swaps + status updates) — self-contained so this file
# doesn't depend on another test module's helpers.
# ---------------------------------------------------------------------------
class _FakeApi:
    def __init__(self, tasks=None):
        self.tasks = {t["id"]: t for t in (tasks or [])}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/swap-tag"):
            task_id = path.split("/")[-2]
            from_tag = request.url.params.get("from")
            to_tag = request.url.params.get("to")
            task = self.tasks.get(task_id)
            if not task or from_tag not in task.get("tags", []):
                return httpx.Response(200, json={"swapped": False})
            tags = list(task["tags"])
            tags[tags.index(from_tag)] = to_tag
            task["tags"] = tags
            return httpx.Response(200, json={"swapped": True})
        if request.method == "PUT" and path.endswith("/complete"):
            task_id = path.split("/")[-2]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            task["status"] = "done"
            return httpx.Response(200, json=task)
        if request.method == "PUT" and path.startswith("/api/tasks/"):
            task_id = path.split("/")[-1]
            task = self.tasks.get(task_id)
            if not task:
                return httpx.Response(404)
            for k, v in json.loads(request.content or b"{}").items():
                task[k] = v
            return httpx.Response(200, json=task)
        if request.method == "GET" and "/api/tasks/" in path:
            task_id = path.split("/")[-1]
            task = self.tasks.get(task_id)
            return httpx.Response(200, json=task) if task else httpx.Response(404)
        return httpx.Response(404)


def _make_worker(tmp_path: Path, api: _FakeApi) -> Worker:
    client = httpx.Client(transport=httpx.MockTransport(api.handler), base_url="http://api")
    sent: list[str] = []
    w = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        telegram_send=lambda text, chat_id=None: sent.append(text) or True,
        http_client=client,
    )
    w._sent_telegram = sent  # type: ignore[attr-defined]
    return w


# ---------------------------------------------------------------------------
# Marker round-trip
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_marker_write_read_round_trip(tmp_path: Path):
    db = tmp_path / "sessions.db"
    path = write_self_restart_marker(session_ids=["sid-1"], task_ids=["task-1"], db_path=db)
    assert path == _self_restart_marker_path(db)
    assert path.exists()
    sids, tids = _read_self_restart_marker(db)
    assert sids == {"sid-1"} and tids == {"task-1"}


@pytest.mark.unit
def test_corrupt_marker_fails_closed(tmp_path: Path):
    """A garbage marker must never make a real crash look deliberate."""
    db = tmp_path / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _self_restart_marker_path(db).write_text("{not json", encoding="utf-8")
    sids, tids = _read_self_restart_marker(db)
    assert sids == set() and tids == set()


# ---------------------------------------------------------------------------
# resume_pending honors the marker
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_resume_pending_self_restart_finalizes_quietly(tmp_path: Path):
    """A session named in the marker is finalized COMPLETED with NO rollback
    notice — the doctor's own session, killed by the end-of-goal worker bounce,
    must not surface as a spurious failed/rolled-back task (#401 acceptance)."""
    api = _FakeApi(tasks=[
        {"id": "doctor_task", "description": "doctor run", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    w = _make_worker(tmp_path, api)
    session = w.session_store.create(
        task_id="doctor_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )
    write_self_restart_marker(
        session_ids=[session.session_id], db_path=w.session_store.db_path,
    )

    n = w.resume_pending()

    assert n == 1
    # No operator notice at all — the doctor already sent its "Shipped" [NOTIFY].
    assert w._sent_telegram == [], f"expected no rollback notice; got {w._sent_telegram}"
    refreshed = w.session_store.get("doctor_task")
    assert refreshed.status == STATUS_COMPLETED
    # Tag advanced to completed, NOT rolled back to #agent.
    assert COMPLETED_TAG in api.tasks["doctor_task"]["tags"]
    assert AGENT_TAG not in api.tasks["doctor_task"]["tags"]
    # Vault checkbox advanced to done ([x]) too — not left stuck at in_progress
    # ([/]). The quiet-finalize path must call _complete_task like every other
    # COMPLETED path (#401 review).
    assert api.tasks["doctor_task"]["status"] == "done"
    # Transcript records the deliberate restart, not a resume_failed.
    kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
    assert "resume_self_restart" in kinds
    assert "resume_failed" not in kinds
    # Marker consumed.
    assert not _self_restart_marker_path(w.session_store.db_path).exists()


@pytest.mark.unit
def test_resume_pending_marker_by_task_id(tmp_path: Path):
    """The primitive can name the run by task_id when it lacks the session_id."""
    api = _FakeApi(tasks=[
        {"id": "doctor_task", "description": "d", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    w = _make_worker(tmp_path, api)
    session = w.session_store.create(
        task_id="doctor_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )
    write_self_restart_marker(task_ids=["doctor_task"], db_path=w.session_store.db_path)

    w.resume_pending()

    assert w.session_store.get("doctor_task").status == STATUS_COMPLETED
    assert w._sent_telegram == []
    _ = session  # session_id path not exercised here; task_id match is enough


@pytest.mark.unit
def test_resume_pending_without_marker_still_rolls_back(tmp_path: Path):
    """Regression guard: a crash with no marker must still FAIL + notify, so
    the self-restart path can't silently swallow real crashes (#401)."""
    api = _FakeApi(tasks=[
        {"id": "crashed_task", "description": "c", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    w = _make_worker(tmp_path, api)
    session = w.session_store.create(
        task_id="crashed_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )

    n = w.resume_pending()

    assert n == 1
    assert len(w._sent_telegram) == 1
    assert "could not be safely resumed" in w._sent_telegram[0]
    assert AGENT_TAG in api.tasks["crashed_task"]["tags"]
    kinds = [e["kind"] for e in w.transcript_store.read(session.session_id)]
    assert "resume_failed" in kinds


@pytest.mark.unit
def test_resume_pending_marker_only_quiets_named_session(tmp_path: Path):
    """A marker for the doctor's session must NOT quiet an unrelated crashed
    session resumed in the same startup pass."""
    api = _FakeApi(tasks=[
        {"id": "doctor_task", "description": "d", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
        {"id": "other_task", "description": "o", "status": "in_progress",
         "tags": [RUNNING_TAG, "cloud"]},
    ])
    w = _make_worker(tmp_path, api)
    doctor = w.session_store.create(
        task_id="doctor_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )
    w.session_store.create(
        task_id="other_task", routing="claude", status=STATUS_RUNNING,
        expected_output="text",
    )
    write_self_restart_marker(session_ids=[doctor.session_id], db_path=w.session_store.db_path)

    w.resume_pending()

    assert w.session_store.get("doctor_task").status == STATUS_COMPLETED
    # The unrelated crash still rolled back + notified.
    assert w.session_store.get("other_task").status != STATUS_COMPLETED
    assert any("could not be safely resumed" in m for m in w._sent_telegram)
    assert len(w._sent_telegram) == 1  # exactly the one real crash


# ---------------------------------------------------------------------------
# Bash primitive — ordering + classification (subprocess-driven, fully stubbed)
# ---------------------------------------------------------------------------
def _stub_bin(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


@pytest.mark.unit
def test_restart_worker_detached_sends_notice_before_restart(tmp_path: Path):
    """The final notice must be flushed BEFORE the restart is triggered, so the
    doctor's "Shipped" [NOTIFY] beats the worker's SIGTERM (#401 acceptance)."""
    if not SERVER_SH.exists():
        pytest.skip("server.sh not present")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    order = tmp_path / "order.log"
    # Stub the venv python: a marker-write call records "mark"; the notice-send
    # heredoc records "notify". We distinguish by argv ("--mark-self-restart").
    _stub_bin(bindir, "fake-python", f'''
for a in "$@"; do
  if [ "$a" = "--mark-self-restart" ]; then echo mark >> "{order}"; exit 0; fi
done
echo notify >> "{order}"
exit 0
''')
    # Stub sudo/systemd-run/systemctl/setsid/nohup so nothing real restarts; each
    # records "restart" exactly once via the systemctl call they wrap.
    _stub_bin(bindir, "systemctl", f'echo restart >> "{order}"\nexit 0\n')
    _stub_bin(bindir, "sudo", 'exec "$@"\n')
    _stub_bin(bindir, "systemd-run", '''
# Drop flags, exec the wrapped command (systemctl restart ...).
while [[ "$1" == --* ]]; do shift; done
exec "$@"
''')
    _stub_bin(bindir, "setsid", 'exec "$@"\n')
    _stub_bin(bindir, "nohup", 'exec "$@"\n')

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    # Point VENV_PYTHON at our stub by overriding HOME so $HOME/.venvs/... resolves
    # into the stub tree.
    venv_py = tmp_path / ".venvs" / "lifeos" / "bin"
    venv_py.mkdir(parents=True)
    (venv_py / "python").symlink_to(bindir / "fake-python")
    env["HOME"] = str(tmp_path)

    result = subprocess.run(
        ["bash", str(SERVER_SH), "restart-worker-detached",
         "--session", "sid-1", "--notify", "Shipped PR #999"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    steps = order.read_text().split()
    # notify must come before restart; mark must come before restart.
    assert "notify" in steps and "restart" in steps, steps
    assert steps.index("notify") < steps.index("restart"), steps
    assert "mark" in steps and steps.index("mark") < steps.index("restart"), steps


@pytest.mark.unit
def test_classify_change_picks_worker_vs_api(tmp_path: Path):
    """classify-change prints 'worker' for an agent_worker diff, 'api' otherwise."""
    if not SERVER_SH.exists():
        pytest.skip("server.sh not present")
    repo = tmp_path / "repo"
    (repo / "api" / "services" / "agent_worker").mkdir(parents=True)
    (repo / "api" / "routes").mkdir(parents=True)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "server.sh").write_text(SERVER_SH.read_text(), encoding="utf-8")

    def _git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    (repo / "api" / "routes" / "x.py").write_text("# base\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")

    def _classify(rng):
        r = subprocess.run(
            ["bash", str(scripts / "server.sh"), "classify-change", rng],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    # api-only change
    (repo / "api" / "routes" / "x.py").write_text("# changed\n")
    _git("add", "-A")
    _git("commit", "-qm", "api change")
    assert _classify("HEAD~1..HEAD") == "api"

    # agent_worker change
    (repo / "api" / "services" / "agent_worker" / "worker.py").write_text("# w\n")
    _git("add", "-A")
    _git("commit", "-qm", "worker change")
    assert _classify("HEAD~1..HEAD") == "worker"


@pytest.mark.unit
def test_server_sh_known_subcommands_still_parse(tmp_path: Path):
    """The new subcommands didn't break the dispatcher: an unknown command
    still prints usage listing both new commands and exits non-zero, and the
    script passes `bash -n` syntax check."""
    if not SERVER_SH.exists():
        pytest.skip("server.sh not present")
    syntax = subprocess.run(["bash", "-n", str(SERVER_SH)],
                            capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    usage = subprocess.run(["bash", str(SERVER_SH), "definitely-not-a-command"],
                           capture_output=True, text=True, timeout=30)
    assert usage.returncode == 1
    assert "restart-worker-detached" in usage.stdout
    assert "classify-change" in usage.stdout
    assert "verify-deployed" in usage.stdout


@pytest.mark.unit
def test_verify_deployed_checks_worktree_and_head(tmp_path: Path):
    """verify-deployed (#419) exits 0 only when the checkout is a real work tree
    whose HEAD matches the expected sha (or origin/main); else exits 1. This is
    the doctor's guard against reporting "Shipped" after a silently-failed pull
    (e.g. a bare/misconfigured checkout)."""
    if not SERVER_SH.exists():
        pytest.skip("server.sh not present")
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "server.sh").write_text(SERVER_SH.read_text(), encoding="utf-8")

    def _git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()

    def _verify(*extra):
        return subprocess.run(
            ["bash", str(scripts / "server.sh"), "verify-deployed", *extra],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )

    # exact full sha → deployed, exit 0
    r = _verify(head)
    assert r.returncode == 0, r.stderr
    assert "deployed" in r.stdout

    # abbreviated sha (prefix match, like git) → deployed, exit 0
    assert _verify(head[:8]).returncode == 0

    # wrong sha → not-deployed, exit 1
    r = _verify("0" * 40)
    assert r.returncode == 1
    assert "not-deployed" in r.stderr

    # ambiguously-short explicit sha → rejected (non-zero), never a false match
    r = _verify("5")
    assert r.returncode != 0
    assert "too short" in r.stderr

    # no arg + no origin/main yet → cannot resolve → exit 1 (the "fetch first" branch)
    r = _verify()
    assert r.returncode == 1
    assert "origin/main" in r.stderr

    # bare repo (core.bare=true) can't pull/checkout → not a work tree → exit 1
    _git("config", "core.bare", "true")
    r = _verify(head)
    assert r.returncode == 1
    assert "work tree" in r.stderr
    _git("config", "core.bare", "false")

    # no arg → compares HEAD against origin/main; point origin/main at HEAD → deployed
    _git("update-ref", "refs/remotes/origin/main", head)
    r = _verify()
    assert r.returncode == 0, r.stderr
    assert "deployed" in r.stdout
