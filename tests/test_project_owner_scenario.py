"""End-to-end persistent-project-owner scenario against a real instance.

One narrative, run against a real `api.main:app` served by an owned
isolated candidate (`./scripts/server.sh test-instance run`), never the
operator's server: an agent turns its own ordinary task into a project
through handoff, the operator is notified once, children run and land in
review/failed states, the project's persistent owner is woken (coalesced)
and accepts and rejects children through its attested tool, the project is
paused and resumed, a child's pull request lands on the project's
integration branch when the owner accepts it, and the owner merges that
branch and completes the project.

Lane: `integration` + `requires_server` puts this module in the `server`
lane (`scripts/test_lane_registry.py`), which `verify_candidate.py` runs
inside its own owned test instance. The hosted merge gate runs `fast-unit`
and `browser-free` only, so this scenario is local/opt-in coverage on top
of the per-issue unit suites, not a merge blocker.

What is real here
-----------------
* The API: a real FastAPI process with its own vault, task index, and
  SQLite stores. Every task read the worker performs is an HTTP call to it,
  and every project lifecycle route (plan, pause, resume, complete, handoff
  finalize, claim, swap-tag) is the real endpoint.
* The worker: a real `Worker`, driven one `tick()` at a time, with the real
  handoff, notice, and owner-wake reconcilers and the real claim/dispatch
  path.
* The attested tools: real `lifeos_agent_project_handoff` and
  `lifeos_agent_project_owner` handlers, called with the caller's real live
  attempt/turn, through the same `inter_agent.dispatch` seam the in-process
  executor uses.
* Git: a scratch repository with a local bare repository standing in for
  `origin`. Worktree provisioning, the integration branch's creation on
  `origin`, the child branch, its merge into the integration branch, and
  the integration branch's own merge into the default branch are all real
  git operations on that bare repository.

What is stood in for, and therefore is NOT proven here
------------------------------------------------------
* No agent model runs. The `claude_code` and `local` executors are
  stand-ins that honor the real contract (`begin_executor_turn`,
  `mark_executor_turn_running`, recording a CLI session id) and script
  their tool calls. Prompt quality, and whether a real agent would choose
  these calls, is out of scope.
* No GitHub. `gh` is a stand-in on `PATH` backed by the scratch bare
  repository: `pr create`, `pr view`, `pr merge`, and the `api repos/...`
  compare/branch lookups all resolve against local git. Branch protection,
  required checks, and review policy are therefore not exercised — only
  the sequencing LifeOS itself owns.
* No Telegram. `telegram_send` is captured in a list.
* The isolated instance runs with every background job disabled, including
  the task file watcher that keeps the API's in-memory index in step with
  another process's vault writes. `_sync_server_index` below stands in for
  that watcher.
* The owner wake's quiet window is shortened so the coalescing assertion
  finishes in seconds; the shipped default is `_OWNER_WAKE_QUIET_SECONDS`.
* Only the `claude_code` owner route is exercised. The `local`, `remote`,
  Hermes, and Managed continuations, the no-native-handle fallback, and the
  restart carve-out are covered by `tests/test_agent_worker_project_owner.py`.
* The worker runs inside the test process rather than as the deployed
  service, so systemd supervision and restart behavior are out of scope.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from api.services.agent_worker import git_worktree
from api.services.agent_worker import worker as worker_module
from api.services.agent_worker.execution import ExecutionSpec
from api.services.agent_worker.inter_agent import Caps, InterAgentContext, dispatch
from api.services.agent_worker.local_executor import ExecutorOutcome
from api.services.agent_worker.session_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SessionStore,
)
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _SynchronousPool
from api.services.task_manager import get_task_manager
from api.services.task_projects import (
    CHILD_ORIGIN_AGENT,
    CHILD_ORIGIN_FIELD,
    COORDINATOR_SESSION_FIELD,
    HANDOFF_OPERATION_FIELD,
    INTEGRATION_BRANCH_FIELD,
    LAST_HANDOFF_OPERATION_FIELD,
    PARENT_ID_FIELD,
    PROJECT_PAUSED_FIELD,
    owner_state,
)
from config.settings import settings

pytestmark = [pytest.mark.integration, pytest.mark.requires_server]

# Short enough to keep the coalescing assertions in seconds, long enough
# that "two events arrive inside one window" is a real wait rather than a
# zero-length one that would pass even with no debounce at all.
_QUIET_SECONDS = 2
_SYNTHETIC_SLUG = "synthetic-org/synthetic-repo"
_PR_HOST = "https://git.invalid"


# ---------------------------------------------------------------------------
# Scratch repository with a local bare origin
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo_with_origin(root: Path, name: str = "repo") -> tuple[Path, Path]:
    """A working repo plus a local bare "origin" remote with main pushed —
    real git, no network access."""
    origin = root / f"{name}-origin.git"
    origin.mkdir()
    assert _git(origin, "init", "-q", "--bare", "-b", "main").returncode == 0

    repo = root / name
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "synthetic@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "Synthetic Operator").returncode == 0
    (repo / "README.md").write_text("synthetic\n")
    assert _git(repo, "add", "README.md").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "init").returncode == 0
    assert _git(repo, "remote", "add", "origin", str(origin)).returncode == 0
    push = _git(repo, "push", "-q", "origin", "main")
    assert push.returncode == 0, push.stderr
    return repo, origin


_FAKE_GH = '''#!@PYTHON@
"""Stand-in `gh`, backed by the scenario's local bare origin.

Only the subcommands LifeOS itself issues are implemented; anything else
exits non-zero so an unexpected call is loud rather than silently green.
"""
import json
import os
import subprocess
import sys

STATE = os.environ["SCENARIO_GH_STATE"]
ORIGIN = os.environ["SCENARIO_GH_ORIGIN"]
CLONE = os.environ["SCENARIO_GH_CLONE"]


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=CLONE, capture_output=True, text=True,
    )


def load():
    with open(STATE) as fh:
        return json.load(fh)


def save(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh)


def log(argv):
    state = load()
    state["calls"].append(argv)
    save(state)


def find_by_url(state, url):
    for pr in state["prs"]:
        if pr["url"] == url:
            return pr
    return None


def main(argv):
    log(argv)
    if argv[:2] == ["pr", "view"]:
        state = load()
        target = argv[2]
        if target.startswith("http"):
            pr = find_by_url(state, target)
            if pr is None:
                print("no pull requests found (HTTP 404)", file=sys.stderr)
                return 1
            print(json.dumps({"baseRefName": pr["base"], "state": pr["state"]}))
            return 0
        pr = next((p for p in state["prs"] if p["head"] == target), None)
        if pr is None:
            print("no pull requests found for branch", file=sys.stderr)
            return 1
        if "--jq" in argv and argv[argv.index("--jq") + 1] == ".state":
            print(pr["state"])
            return 0
        print(json.dumps({"url": pr["url"]}))
        return 0

    if argv[:2] == ["pr", "create"]:
        state = load()
        base = argv[argv.index("--base") + 1]
        head = argv[argv.index("--head") + 1]
        number = len(state["prs"]) + 1
        url = "@PR_HOST@/@SLUG@/pull/%d" % number
        state["prs"].append(
            {"number": number, "base": base, "head": head, "url": url, "state": "OPEN"}
        )
        save(state)
        print(url)
        return 0

    if argv[:2] == ["pr", "merge"]:
        state = load()
        pr = find_by_url(state, argv[2])
        if pr is None:
            print("no such pull request", file=sys.stderr)
            return 1
        fetch = git("fetch", "-q", "origin")
        if fetch.returncode != 0:
            print(fetch.stderr, file=sys.stderr)
            return 1
        for step in (
            ["checkout", "-q", "-B", pr["base"], "origin/" + pr["base"]],
            ["merge", "-q", "--no-ff", "-m", "merge %s" % pr["head"], "origin/" + pr["head"]],
            ["push", "-q", "origin", pr["base"]],
        ):
            result = git(*step)
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                return 1
        pr["state"] = "MERGED"
        save(state)
        return 0

    if argv[0] == "api":
        path = argv[1]
        git("fetch", "-q", "--prune", "origin")
        # A branch name can itself contain "/", so only the first four
        # segments are structural.
        parts = path.split("/", 4)
        if len(parts) == 3:  # repos/<owner>/<repo>
            print("main")
            return 0
        if len(parts) == 5 and parts[3] == "branches":
            listed = git("ls-remote", "--heads", "origin", parts[4])
            if listed.returncode != 0 or not listed.stdout.strip():
                print("gh: Branch not found (HTTP 404)", file=sys.stderr)
                return 1
            print("{}")
            return 0
        if len(parts) == 5 and parts[3] == "compare" and "..." in parts[4]:
            base, head = parts[4].split("...", 1)
            counted = git("rev-list", "--count", "origin/%s..origin/%s" % (base, head))
            if counted.returncode != 0:
                print(counted.stderr or "compare failed (HTTP 404)", file=sys.stderr)
                return 1
            print(counted.stdout.strip() or "0")
            return 0
    print("unsupported stand-in gh invocation: %r" % (argv,), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _install_fake_gh(tmp_path: Path, origin: Path, monkeypatch) -> Path:
    """Put the stand-in `gh` ahead of any real one and point it at `origin`."""
    import sys

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "gh"
    script.write_text(
        _FAKE_GH.replace("@PYTHON@", sys.executable)
        .replace("@PR_HOST@", _PR_HOST)
        .replace("@SLUG@", _SYNTHETIC_SLUG)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    clone = tmp_path / "gh-clone"
    assert _git(tmp_path, "clone", "-q", str(origin), str(clone)).returncode == 0
    assert _git(clone, "config", "user.email", "synthetic@example.invalid").returncode == 0
    assert _git(clone, "config", "user.name", "Synthetic Reviewer").returncode == 0

    state = tmp_path / "gh-state.json"
    state.write_text(json.dumps({"prs": [], "calls": []}))

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SCENARIO_GH_STATE", str(state))
    monkeypatch.setenv("SCENARIO_GH_ORIGIN", str(origin))
    monkeypatch.setenv("SCENARIO_GH_CLONE", str(clone))
    return state


# ---------------------------------------------------------------------------
# Stand-in executors — each honors the real executor's turn contract
# ---------------------------------------------------------------------------

class _ScriptedExecutor:
    """Runs a scripted action for the session's task, then returns a scripted
    outcome.

    Honors what the real executors do before anything else: it opens its own
    executor turn (`begin_executor_turn`) and marks it running, so a caller
    that hands it a stale session raises exactly as `LocalExecutor.execute`
    or `ClaudeCodeExecutor.execute` would. The `claude_code` variant also
    records a CLI session id on its first turn, which is what makes every
    later wake a native `-r` resume rather than a fresh start.
    """

    def __init__(self, *, spawn_event: str | None = None):
        self.session_store: SessionStore | None = None
        self.transcript_store: TranscriptStore | None = None
        # The kind the real CLI executors append when a subprocess actually
        # starts. `Worker._confirm_resume_or_requeue` counts these to decide
        # whether a resume really launched — without it every queued wake
        # would stay undelivered and the owner would be reopened forever.
        self.spawn_event = spawn_event
        # task_id -> list of callables taking the live Session
        self.script: dict[str, list] = {}
        # task_id -> list of ExecutorOutcome factories taking the live Session
        self.outcomes: dict[str, list] = {}
        self.turns: list[dict] = []

    def _default_outcome(self, session) -> ExecutorOutcome:
        return ExecutorOutcome(
            status=STATUS_COMPLETED,
            final_text="synthetic turn complete",
            # A CLI completion only counts when the run earned a positive
            # signal (`completion_signal.has_positive_completion_signal`);
            # a real Claude Code completion emits a `[NOTIFY]` summary, so
            # this stand-in reports one rather than sidestepping the check.
            notifications_sent=1,
            session_id=session.session_id,
            attempt_id=session.attempt_id,
            turn_id=session.turn_id,
            executor=session.routing,
        )

    def _run_turn(self, session, payload):
        session = self.session_store.begin_executor_turn(
            session.task_id, "execute", session=session,
        )
        assert self.session_store.mark_executor_turn_running(
            session.task_id, session.attempt_id, session.turn_id,
        )
        session = self.session_store.get(session.task_id)
        if self.spawn_event:
            self.transcript_store.append(
                session.session_id, self.spawn_event, {"synthetic": True},
            )
            if not session.claude_code_session_id:
                self.session_store.set_claude_code_session_id(
                    session.task_id, f"cli-{session.task_id}",
                )
                session = self.session_store.get(session.task_id)
        self.turns.append({"task_id": session.task_id, "payload": payload})
        actions = self.script.get(session.task_id) or []
        if actions:
            actions.pop(0)(session)
        queued = self.outcomes.get(session.task_id) or []
        factory = queued.pop(0) if queued else None
        outcome = factory(session) if factory else self._default_outcome(session)
        # The real executors persist their own terminal status before
        # returning (see `ClaudeCodeExecutor`/`LocalExecutor`); a stub that
        # skipped this would leave every session RUNNING forever and hide
        # exactly the "is the owner mid-turn?" state the reconciler keys on.
        self.session_store.update_status(
            session.task_id, outcome.status,
            attempt_id=session.attempt_id, turn_id=session.turn_id,
        )
        return outcome

    def execute(self, session, request):
        return self._run_turn(session, request)

    def resume(self, session, message, working_dir=None):
        return self._run_turn(session, message)


def _queue(mapping: dict, key: str, value) -> None:
    mapping.setdefault(key, []).append(value)


_GOLDEN_PREFLIGHT = json.dumps({
    "budget": {"wall_seconds": 3600, "max_tokens": 100000, "max_dollars": 1.0},
    # Every task in this scenario carries an explicit engine tag, and a tag
    # always wins over preflight's own choice (`preflight._apply_tag_overrides`),
    # so this stand-in never decides a route.
    "routing": "local",
    "routing_reason": "synthetic preflight",
    "routing_explicit": True,
    "expected_output": "text",
    "ambiguity": None,
    "sane": True,
    "sane_reason": "",
})


# ---------------------------------------------------------------------------
# The scenario harness
# ---------------------------------------------------------------------------

class _Scenario:
    def __init__(self, base_url: str, tmp_path: Path, monkeypatch):
        self.base_url = base_url
        self.api = httpx.Client(base_url=base_url, timeout=60.0)
        self.tmp_path = tmp_path
        self.repo, self.origin = _init_repo_with_origin(tmp_path)
        self.gh_state = _install_fake_gh(tmp_path, self.origin, monkeypatch)

        self.notices: list[str] = []
        self.sessions = SessionStore()
        self.transcripts = TranscriptStore()
        self.manager = get_task_manager()

        self.cli_executor = _ScriptedExecutor(spawn_event="claude_code_spawn")
        self.local_executor = _ScriptedExecutor()
        for stub in (self.cli_executor, self.local_executor):
            stub.session_store = self.sessions
            stub.transcript_store = self.transcripts

        def _send(text, chat_id=None, bot=None):
            self.notices.append(text)
            return True

        self.worker = Worker(
            api_base=base_url,
            session_store=self.sessions,
            transcript_store=self.transcripts,
            spend_tracker=SpendTracker(
                db_path=tmp_path / "spend.db", daily_cap_dollars=100.0,
            ),
            poll_seconds=0.01,
            telegram_send=_send,
            telegram_send_with_id=lambda text, **_kwargs: (
                self.notices.append(text), [1],
            )[1],
            http_client=httpx.Client(base_url=base_url, timeout=60.0),
            preflight_caller=lambda prompt: _GOLDEN_PREFLIGHT,
            local_executor=self.local_executor,
            claude_code_executor=self.cli_executor,
            cli_pool=_SynchronousPool(),
        )

    # -- API helpers -------------------------------------------------------

    def create_task(self, description: str, **body) -> dict:
        response = self.api.post(
            "/api/tasks", json={"description": description, "context": "Scenario", **body},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def task(self, task_id: str) -> dict:
        response = self.api.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def tasks(self) -> list[dict]:
        response = self.api.get("/api/tasks")
        assert response.status_code == 200, response.text
        return response.json()["tasks"]

    def children(self, project_id: str) -> dict[str, dict]:
        return {
            task["description"]: task
            for task in self.tasks()
            if (task.get("fields") or {}).get(PARENT_ID_FIELD) == project_id
        }

    def sync_server_index(self) -> None:
        """Make the API re-read the vault after an in-process vault write.

        The attested tools and the project service run in this process, the
        same way they run in the deployed worker process — both write the
        vault directly. The deployed API notices through its task file
        watcher; an isolated candidate has every background job disabled, so
        this stands in for that watcher. `create_or_find_by_operation`
        rebuilds the index from Markdown before it looks the key up, so
        repeating one stable key refreshes the server without creating
        anything after the first call.
        """
        response = self.api.post("/api/tasks", json={
            "description": "Synthetic index sync",
            "context": "ScenarioSync",
            "operation_key": "scenario-index-sync",
        })
        assert response.status_code == 200, response.text

    def swap_tag(self, task_id: str, from_tag: str, to_tag: str) -> None:
        response = self.api.post(
            f"/api/tasks/{task_id}/swap-tag", params={"from": from_tag, "to": to_tag},
        )
        assert response.status_code == 200, response.text
        assert response.json()["swapped"], response.text

    # -- worker helpers ----------------------------------------------------

    def tick(self) -> None:
        self.worker.tick()
        self.sync_server_index()

    def wake_owner(self, project_id: str) -> int:
        """Run wake passes until one fires, then the dispatch pass that
        delivers it — what a sequence of real `tick()`s does.

        The first pass after an event only anchors the quiet window; the
        wake itself lands on a later pass, once the window has elapsed.
        """
        for _ in range(4):
            self.await_quiet_window(project_id)
            woken = self.worker._reconcile_project_owners()
            if woken:
                self.worker._dispatch_spawned_sessions()
                self.sync_server_index()
                return woken
        self.sync_server_index()
        return 0

    def await_quiet_window(self, project_id: str) -> None:
        state = self.sessions.get_project_owner_state(project_id) or {}
        anchor = state.get("first_unseen_at")
        if not anchor:
            return
        while int(time.time()) - anchor < _QUIET_SECONDS:
            time.sleep(0.1)

    def owner_turn_count(self, project_id: str) -> int:
        """Turns this project's own owner session has run — a per-project
        measure, unlike `_reconcile_project_owners`'s return value, which
        counts wakes across every project the instance holds."""
        owner_task_id = self.owner_session(project_id).task_id
        return sum(1 for turn in self.cli_executor.turns if turn["task_id"] == owner_task_id)

    def owner_debug(self, project_id: str) -> dict:
        owner = self.owner_session(project_id)
        return {
            "owner_status": owner.status,
            "owner_attempt": owner.attempt_id,
            "state": self.sessions.get_project_owner_state(project_id),
            "children": {
                task["id"]: owner_state(task)
                for task in self.tasks()
                if (task.get("fields") or {}).get(PARENT_ID_FIELD) == project_id
            },
            "paused": (self.task(project_id).get("fields") or {}).get(PROJECT_PAUSED_FIELD),
        }

    def owner_session(self, project_id: str):
        session_id = (self.task(project_id).get("fields") or {})[COORDINATOR_SESSION_FIELD]
        return self.sessions.get_by_session_id(session_id)

    def owner_context(self, project_id: str) -> InterAgentContext:
        # The attested tools read the vault through this process's own
        # `TaskManager`, exactly as they do in the deployed worker. Project
        # lifecycle writes the API made (handoff finalize, pause, resume)
        # land in Markdown, so refresh from it before the owner reads.
        self.manager.rebuild_index()
        owner = self.owner_session(project_id)
        return InterAgentContext(
            session_store=self.sessions,
            transcript_store=self.transcripts,
            caller_session_id=owner.session_id,
            caps=Caps(),
            caller_attempt_id=owner.attempt_id,
            caller_turn_id=owner.turn_id,
            task_manager=self.manager,
        )

    # -- git/gh helpers ----------------------------------------------------

    def origin_branches(self) -> dict[str, str]:
        listed = _git(self.repo, "ls-remote", "--heads", str(self.origin))
        branches = {}
        for line in listed.stdout.splitlines():
            sha, ref = line.split("\t")
            branches[ref.removeprefix("refs/heads/")] = sha
        return branches

    def gh_calls(self) -> list[list[str]]:
        return json.loads(self.gh_state.read_text())["calls"]

    def pull_requests(self) -> list[dict]:
        return json.loads(self.gh_state.read_text())["prs"]

    def close(self) -> None:
        self.api.close()
        self.worker._http.close()


@pytest.fixture
def scenario(candidate_base_url, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_hosts", {}, raising=False)
    monkeypatch.setattr(worker_module, "_OWNER_WAKE_QUIET_SECONDS", _QUIET_SECONDS)
    built = _Scenario(candidate_base_url, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


_LIFECYCLE_TAGS = {
    "agent-running", "agent-completed", "agent-failed",
    "agent-blocked", "agent-budget-exceeded", "accepted",
}

_HANDOFF_CHILDREN = [
    {"key": "build", "description": "Build the synthetic widget", "assignee": "claude"},
    {"key": "review", "description": "Review the synthetic widget", "assignee": "local"},
    {"key": "qa", "description": "Check the synthetic widget", "assignee": "local"},
    {"key": "docs", "description": "Document the synthetic widget", "assignee": "me"},
    {"key": "release", "description": "Release the synthetic widget", "assignee": "me"},
    {"key": "retro", "description": "Retro the synthetic widget", "assignee": "me"},
]


def _project_notices(scenario: _Scenario) -> list[str]:
    return [text for text in scenario.notices if text.startswith("🗂")]


def test_persistent_project_owner_end_to_end(scenario):
    """Handoff → one notice → child events → coalesced owner wake → owner
    accept/reject → pause → resume → integration-branch merge → completion."""
    s = scenario

    # -- an agent turns its own ordinary task into a project ---------------
    source = s.create_task(
        "Ship the synthetic widget", tags=["claude"], fields={"working_dir": str(s.repo)},
    )
    project_id = source["id"]
    handoff: list[dict] = []
    while_staged: list[dict] = []

    def _stage_handoff(session):
        handoff.append(dispatch(
            InterAgentContext(
                session_store=s.sessions, transcript_store=s.transcripts,
                caller_session_id=session.session_id, caps=Caps(),
                caller_attempt_id=session.attempt_id, caller_turn_id=session.turn_id,
                task_manager=s.manager,
            ),
            "lifeos_agent_project_handoff",
            {"operation_id": "synthetic-widget-v1", "children": _HANDOFF_CHILDREN},
        ))
        s.sync_server_index()
        # Observed from inside the source turn, before the worker has seen it
        # stop: the children exist but nothing may start yet.
        staged_children = s.children(project_id)
        while_staged.append({
            "parent": s.task(project_id),
            "children": staged_children,
            "claims": [
                s.api.post(f"/api/tasks/{child['id']}/claim-agent").json()["claimed"]
                for child in staged_children.values()
            ],
            "notices": list(_project_notices(s)),
        })

    _queue(s.cli_executor.script, project_id, _stage_handoff)
    s.tick()

    assert handoff and handoff[0].get("ok"), handoff
    assert handoff[0]["state"] == "staged"
    assert handoff[0]["runnable"] is False
    assert handoff[0]["stop_now"] is True

    staged = while_staged[0]
    assert staged["parent"]["fields"][HANDOFF_OPERATION_FIELD] == "synthetic-widget-v1"
    assert staged["parent"]["status"] != "done", "handoff must not complete the source task"
    assert staged["claims"] == [False] * len(_HANDOFF_CHILDREN), (
        "a staged child cannot be claimed until the source turn is seen to stop"
    )
    assert staged["notices"] == [], "a staged handoff announces nothing"

    children = s.children(project_id)
    assert len(children) == len(_HANDOFF_CHILDREN)
    assert all(
        child["fields"].get(CHILD_ORIGIN_FIELD) == CHILD_ORIGIN_AGENT
        for child in children.values()
    ), "every handoff child is agent-attributed"

    # -- agents cannot buy the operator a metered route --------------------
    agent_header = {"X-LifeOS-Agent-Session": "synthetic-agent-session"}
    hermes_attempt = s.api.post(
        "/api/tasks",
        json={"description": "Hand this to Hermes", "context": "Scenario",
              "tags": ["hermes"], "fields": {"parent_id": project_id}},
        headers=agent_header,
    )
    assert hermes_attempt.status_code == 422
    assert hermes_attempt.json()["detail"]["code"] == "hermes_delegation_forbidden"
    metered_attempt = s.api.post(
        "/api/tasks",
        json={"description": "Pay for a cloud turn", "context": "Scenario",
              "tags": ["cloud-sonnet"], "fields": {"parent_id": project_id}},
        headers=agent_header,
    )
    assert metered_attempt.status_code == 422
    assert metered_attempt.json()["detail"]["code"] == "api_billing_blocked"

    # -- the worker saw the source turn stop, so the project is activated --
    project = s.task(project_id)
    assert project["fields"][LAST_HANDOFF_OPERATION_FIELD] == "synthetic-widget-v1"
    assert HANDOFF_OPERATION_FIELD not in project["fields"]
    integration_branch = project["fields"][INTEGRATION_BRANCH_FIELD]
    owner_session_id = project["fields"][COORDINATOR_SESSION_FIELD]
    assert _project_notices(s) == [], (
        "the notice reconciler had not yet seen the activation this tick"
    )

    build_id = children["Build the synthetic widget"]["id"]
    review_id = children["Review the synthetic widget"]["id"]
    qa_id = children["Check the synthetic widget"]["id"]
    docs_id = children["Document the synthetic widget"]["id"]
    release_id = children["Release the synthetic widget"]["id"]
    retro_id = children["Retro the synthetic widget"]["id"]

    def _write_widget(session):
        working_dir = ExecutionSpec.from_dict(session.execution_spec).working_dir
        Path(working_dir, "widget.txt").write_text("synthetic widget\n")

    _queue(s.cli_executor.script, build_id, _write_widget)
    _queue(s.local_executor.outcomes, review_id, lambda session: ExecutorOutcome(
        status=STATUS_FAILED, final_text="synthetic failure",
        session_id=session.session_id, attempt_id=session.attempt_id,
        turn_id=session.turn_id, executor=session.routing,
    ))

    # The activation tick: the operator is notified, the owner's first turn
    # runs, and the children are claimed and run.
    s.tick()

    # -- exactly one activation notice, reporting the fan-out too ----------
    fired = _project_notices(s)
    assert len(fired) == 1, fired
    assert "became a project" in fired[0]
    assert f"{len(_HANDOFF_CHILDREN)} agent-created children" in fired[0]
    s.worker._reconcile_project_notices()
    assert _project_notices(s) == fired, "the activation notice fires exactly once"

    # -- the coding child branched from the project's integration branch ---
    assert integration_branch in s.origin_branches()
    build_worktree = ExecutionSpec.from_dict(
        s.sessions.get(build_id).execution_spec,
    ).working_dir
    marker = git_worktree._read_worker_marker(
        build_worktree, runner=None, timeout=git_worktree.DEFAULT_TIMEOUT,
    )
    assert marker["base_branch"] == integration_branch
    build_outcome = s.sessions.get_card_outcome(build_id)
    assert build_outcome["pr_urls"], "the coding child opened a pull request"
    build_pr_url = build_outcome["pr_urls"][0]
    opened = [pr for pr in s.pull_requests() if pr["url"] == build_pr_url]
    assert opened and opened[0]["base"] == integration_branch

    # -- children reached review/failed, plus one blocked ------------------
    s.swap_tag(docs_id, "me", "agent-blocked")
    s.sync_server_index()
    states = {
        task["id"]: owner_state(task)
        for task in s.tasks()
        if (task.get("fields") or {}).get(PARENT_ID_FIELD) == project_id
    }
    assert states[build_id] == "awaiting_review"
    assert states[qa_id] == "awaiting_review"
    assert states[review_id] == "failed"
    assert states[docs_id] == "blocked"

    # -- the owner is woken once, for everything that accumulated ----------
    assert s.worker._reconcile_project_owners() == 0, "the first pass only anchors"
    assert s.worker._reconcile_project_owners() == 0, "still inside the quiet window"

    concurrency_probe: list[int] = []
    first_turn: list[dict] = []

    def _owner_first_turn(_session):
        # A second wake pass while this turn is live must find nothing to do —
        # one owner turn at a time is the whole point of the CAS.
        concurrency_probe.append(s.worker._reconcile_project_owners())
        ctx = s.owner_context(project_id)
        first_turn.append(dispatch(ctx, "lifeos_agent_project_owner", {
            "action": "accept_child", "project_id": project_id, "child_task_id": build_id,
        }))
        first_turn.append(dispatch(ctx, "lifeos_agent_project_owner", {
            "action": "reject_child", "project_id": project_id, "child_task_id": qa_id,
            "note": "Re-run the synthetic check against the integration branch.",
        }))
        s.sync_server_index()

    _queue(s.cli_executor.script, s.owner_session(project_id).task_id, _owner_first_turn)
    assert s.wake_owner(project_id) == 1, s.owner_debug(project_id)

    wake_message = next(
        turn["payload"] for turn in reversed(s.cli_executor.turns)
        if turn["task_id"] == s.owner_session(project_id).task_id
    )
    for child_id in (build_id, qa_id, review_id, docs_id):
        assert child_id in wake_message, "the wake coalesces every accumulated event"
    assert concurrency_probe == [0], "no second owner turn starts while one is live"
    assert all(result.get("ok") for result in first_turn), first_turn

    # -- accept merged the child's pull request into the integration branch
    accepted = s.task(build_id)
    assert "accepted" in accepted["tags"]
    assert accepted["fields"]["review_accepted_by"] == f"owner:{owner_session_id}"
    assert [pr for pr in s.pull_requests() if pr["url"] == build_pr_url][0]["state"] == "MERGED"
    merged_log = _git(s.repo, "fetch", "-q", "origin")
    assert merged_log.returncode == 0
    listed = _git(s.repo, "ls-tree", "--name-only", f"origin/{integration_branch}")
    assert "widget.txt" in listed.stdout.split(), listed.stdout

    rejected = s.task(qa_id)
    assert "agent-running" in rejected["tags"]
    assert "Project owner note:" in (rejected.get("notes") or "")

    # the owner's own session never stamps lifecycle tags on the project card
    assert not set(s.task(project_id)["tags"]) & _LIFECYCLE_TAGS

    # -- pause: no new child starts, a running child still finishes --------
    assert s.api.post(f"/api/tasks/{project_id}/project/pause", json={}).status_code == 200
    assert s.task(project_id)["fields"][PROJECT_PAUSED_FIELD] == "true"
    refused = s.api.post(
        f"/api/tasks/{project_id}/project/resume", headers=agent_header,
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "agent_resume_forbidden"

    s.swap_tag(release_id, "me", "local")
    s.sync_server_index()
    claim = s.api.post(f"/api/tasks/{release_id}/claim-agent")
    assert claim.status_code == 409, claim.text
    assert "paused" in claim.json()["detail"]

    s.tick()  # the rejected child's follow-up turn runs; nothing new starts
    assert s.sessions.get(release_id) is None, "a paused project starts no new child"
    assert owner_state(s.task(qa_id)) == "awaiting_review", (
        "a child already under way finishes its turn and lands in review"
    )
    owner_turns_before = s.owner_turn_count(project_id)
    deadline = time.time() + _QUIET_SECONDS + 1
    while time.time() < deadline:
        # Past the quiet window too: the events that piled up while paused
        # stay undelivered until the project is resumed.
        s.worker._reconcile_project_owners()
        time.sleep(0.2)
    s.worker._dispatch_spawned_sessions()
    assert s.owner_turn_count(project_id) == owner_turns_before, s.owner_debug(project_id)

    # -- resume: one wake for everything that piled up while paused --------
    s.swap_tag(release_id, "local", "me")
    s.swap_tag(docs_id, "agent-blocked", "me")
    s.sync_server_index()
    for resolved in (docs_id, release_id, retro_id):
        assert s.api.put(f"/api/tasks/{resolved}/complete").status_code == 200
    assert s.api.post(f"/api/tasks/{project_id}/project/resume").status_code == 200

    second_turn: list[dict] = []
    operator_completion: list[int] = []

    def _owner_second_turn(_session):
        ctx = s.owner_context(project_id)
        second_turn.append(dispatch(ctx, "lifeos_agent_project_owner", {
            "action": "accept_child", "project_id": project_id, "child_task_id": qa_id,
        }))
        s.sync_server_index()
        # The operator's own Complete stays refused while the owner's turn is
        # live — the project is never finished behind the owner's back.
        operator_completion.append(s.api.post(
            f"/api/tasks/{project_id}/project/complete",
            json={"acknowledge_cancelled_children": True},
        ).status_code)
        second_turn.append(dispatch(ctx, "lifeos_agent_project_owner", {
            "action": "complete_project", "project_id": project_id,
            "acknowledge_cancelled_children": True,
        }))
        s.sync_server_index()

    _queue(s.cli_executor.script, s.owner_session(project_id).task_id, _owner_second_turn)
    assert s.wake_owner(project_id) == 1, s.owner_debug(project_id)
    assert second_turn[0].get("ok"), second_turn
    assert operator_completion == [409]
    assert second_turn[1]["ok"] is False
    assert second_turn[1]["error"] == "integration_unmerged"
    assert f"integration branch {integration_branch!r} has" in second_turn[1]["message"], (
        second_turn[1]["message"]
    )
    assert "not yet merged into 'main'" in second_turn[1]["message"]
    assert s.task(project_id)["status"] != "done"

    # -- the owner merges the integration branch, then completes -----------
    clone = Path(os.environ["SCENARIO_GH_CLONE"])
    assert _git(clone, "fetch", "-q", "origin").returncode == 0
    assert _git(clone, "checkout", "-q", "-B", "main", "origin/main").returncode == 0
    assert _git(
        clone, "merge", "-q", "--no-ff", "-m", "merge integration",
        f"origin/{integration_branch}",
    ).returncode == 0
    assert _git(clone, "push", "-q", "origin", "main").returncode == 0

    plan = s.api.post(
        f"/api/tasks/{project_id}/project/plan", json={"operation_id": "synthetic-widget-v2"},
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["wake_requested"] is True
    assert plan.json()["session_id"] == owner_session_id, "Plan wakes the existing owner"

    third_turn: list[dict] = []

    def _owner_third_turn(_session):
        third_turn.append(dispatch(s.owner_context(project_id), "lifeos_agent_project_owner", {
            "action": "complete_project", "project_id": project_id,
            "acknowledge_cancelled_children": True,
        }))
        s.sync_server_index()

    _queue(s.cli_executor.script, s.owner_session(project_id).task_id, _owner_third_turn)
    assert s.wake_owner(project_id) == 1
    assert third_turn[0].get("ok"), third_turn
    assert third_turn[0]["status"] == "done"

    finished = s.task(project_id)
    assert finished["status"] == "done"
    assert not set(finished["tags"]) & _LIFECYCLE_TAGS, (
        "the owner session never projects lifecycle tags onto the project card"
    )
    assert s.sessions.get_by_session_id(owner_session_id).routing == "claude_code", (
        "owner wakes stay on the route the owner already ran on"
    )
    assert not any(
        tag.lower().lstrip("#").startswith("cloud")
        for task in s.tasks() for tag in (task.get("tags") or [])
    ), "nothing in this project was routed to a metered API engine"
    assert len(_project_notices(s)) == 1, "still exactly one project notice"


def test_owner_completes_once_a_deleted_integration_branch_reads_as_merged(scenario):
    """A repository whose merge process deletes the integration branch
    satisfies the completion gate: a branch that is gone reads as merged,
    not as a failed check."""
    s = scenario
    parent = s.create_task("Retire the synthetic adapter", tags=["claude"])
    project_id = parent["id"]
    child = s.create_task(
        "Port the synthetic adapter", tags=["codex"],
        fields={"parent_id": project_id},
    )
    plan = s.api.post(
        f"/api/tasks/{project_id}/project/plan", json={"operation_id": "retire-adapter-v1"},
    )
    assert plan.status_code == 200, plan.text
    branch = s.task(project_id)["fields"][INTEGRATION_BRANCH_FIELD]

    # The branch existed and carried the child's pull request, then the
    # repository's own merge process merged and deleted it.
    assert _git(s.repo, "push", "-q", "origin", f"origin/main:refs/heads/{branch}").returncode == 0
    assert branch in s.origin_branches()
    s.sessions.record_card_outcome(
        child["id"], session_id="synthetic-child-session", engine_label="codex",
        summary="Synthetic port complete.", branch="feat/synthetic-child",
        pr_urls=[f"{_PR_HOST}/{_SYNTHETIC_SLUG}/pull/7"],
    )
    assert s.api.put(f"/api/tasks/{child['id']}/complete").status_code == 200
    assert _git(s.repo, "push", "-q", "origin", "--delete", branch).returncode == 0
    assert branch not in s.origin_branches()

    owner = s.owner_session(project_id)
    owner = s.sessions.begin_executor_turn(owner.task_id, "execute", session=owner)
    assert s.sessions.mark_executor_turn_running(
        owner.task_id, owner.attempt_id, owner.turn_id,
    )
    result = dispatch(s.owner_context(project_id), "lifeos_agent_project_owner", {
        "action": "complete_project", "project_id": project_id,
    })
    assert result.get("ok"), result
    s.sync_server_index()
    assert s.task(project_id)["status"] == "done"


def test_a_cancelled_project_never_wakes_its_owner(scenario):
    """Cancellation wins: once a project is cancelled, no child event —
    however loud — reopens its owner."""
    s = scenario
    parent = s.create_task("Abandon the synthetic migration", tags=["claude"])
    project_id = parent["id"]
    child = s.create_task(
        "Draft the synthetic migration", tags=["codex"],
        fields={"parent_id": project_id},
    )
    plan = s.api.post(
        f"/api/tasks/{project_id}/project/plan", json={"operation_id": "abandon-migration-v1"},
    )
    assert plan.status_code == 200, plan.text
    owner_task_id = s.owner_session(project_id).task_id
    s.sessions.update_status(
        owner_task_id, STATUS_COMPLETED,
        attempt_id=s.owner_session(project_id).attempt_id,
        turn_id=s.owner_session(project_id).turn_id,
    )
    s.worker._reconcile_project_owners()  # establish the baseline row

    cancelled = s.api.post(
        f"/api/tasks/{project_id}/project/cancel",
        json={"confirm": True, "operation_id": "abandon-migration-cancel-1"},
    )
    assert cancelled.status_code == 200, cancelled.text
    s.sync_server_index()
    assert s.task(project_id)["status"] == "cancelled"
    assert s.task(child["id"])["status"] == "cancelled"

    turns_before = s.owner_turn_count(project_id)
    deadline = time.time() + _QUIET_SECONDS + 1
    while time.time() < deadline:
        s.worker._reconcile_project_owners()
        time.sleep(0.2)
    s.worker._dispatch_spawned_sessions()
    assert s.owner_turn_count(project_id) == turns_before, (
        "a cancelled project's owner is never resumed"
    )
