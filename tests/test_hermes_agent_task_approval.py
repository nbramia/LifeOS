"""A Hermes call to the LifeOS tools that can start agent work runs only when
LifeOS's own record of the operator's question for that turn asked for agent
work in so many words; otherwise it is parked (or held) and sent to Telegram
for approval.

Hermes names its turn with the `caller_session_id` LifeOS wrote into it
(`hermes_turn`); that value proves nothing, and LifeOS resolves it against
its own records. Most tests here drive the real path: a tool call into
`mcp_server.py` started as Hermes's server, its HTTP request into the real
task and scheduler routes, and the real project service. Every task lives in
a temp vault, every session and schedule in a temp store, and every Telegram
send is captured.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services import agent_schedule_approval as schedule_approval
from api.services import agent_task_approval as approval
from api.services import hermes_turn
from api.services.agent_board import AGENT_PICKUP_TAGS
from api.services.task_manager import TaskManager
from api.services.task_projects import (
    AGENT_CREATOR_SESSION_FIELD, CHILD_CREATOR_SESSION_FIELD, COORDINATOR_SESSION_FIELD,
    EXECUTION_PAUSED_FIELD, PROJECT_PAUSED_FIELD,
)

pytestmark = pytest.mark.unit

OPERATOR = "111222333"
SECRET = "synthetic-shared-secret"
ROOT = "sess_root1"
WORKER = "sess_worker1"
HEADER = hermes_turn.HEADER


# caller_session_id -> the lifeos_turn_id of the turn LifeOS last forwarded
# under it: the turn Hermes is running for that conversation.
_current_turn: dict[str, str] = {}


def turn_arg(caller: str) -> str:
    """What Hermes's `lifeos_agent_approval` plugin puts in the tool call's
    `_lifeos_hermes_turn` argument while Hermes runs the latest turn LifeOS
    forwarded under `caller` (or `caller` itself as given)."""
    if caller in _current_turn:
        return f"{caller}.{_current_turn[caller]}"
    return caller


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_backend_token", SECRET)
    monkeypatch.setattr(settings, "telegram_chat_id", OPERATOR)
    hermes_turn.clear_proxy_turns()
    schedule_approval.clear_requests()
    _current_turn.clear()
    yield
    hermes_turn.clear_proxy_turns()
    schedule_approval.clear_requests()
    _current_turn.clear()


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    from api.routes import tasks as tasks_route
    from api.services.agent_worker import session_store as session_store_mod
    from api.services.agent_worker import transcript_store as transcript_store_mod

    store = session_store_mod.SessionStore(db_path=tmp_path / "sessions.db")
    transcripts = transcript_store_mod.TranscriptStore(tmp_path / "transcripts")
    monkeypatch.setattr(hermes_turn, "_session_store", lambda: store)
    monkeypatch.setattr(tasks_route, "_session_store", store)
    monkeypatch.setattr(tasks_route, "_transcript_store", transcripts)
    # The chat tools and the Telegram decision build their own stores.
    monkeypatch.setattr(session_store_mod, "SessionStore", lambda *a, **k: store)
    monkeypatch.setattr(transcript_store_mod, "TranscriptStore", lambda *a, **k: transcripts)
    return store


@pytest.fixture
def tm(tmp_path, monkeypatch, sessions):
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    import api.services.task_manager as tm_mod
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)
    monkeypatch.setattr("api.routes.tasks.get_task_manager", lambda: manager)
    monkeypatch.setattr("api.services.agent_tools.get_task_manager", lambda: manager, raising=False)
    return manager


@pytest.fixture
def schedules(tmp_path, monkeypatch):
    from api.services import scheduler_store as store_mod

    store = store_mod.SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "sched.json")
    fired: list = []

    class _Scheduler:
        async def _fire_entry(self, entry, *, manual=False, request_key=None):
            fired.append((entry.id, request_key))

    monkeypatch.setattr(store_mod, "get_scheduler_store", lambda: store)
    monkeypatch.setattr(store_mod, "get_scheduler", lambda: _Scheduler())
    monkeypatch.setattr("api.routes.scheduler.get_scheduler_store", lambda: store)
    monkeypatch.setattr("api.routes.scheduler.get_scheduler", lambda: _Scheduler())
    store.fired = fired
    return store


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def fake_send(text, buttons):
        calls.append((text, buttons))
        return 1000 + len(calls)

    monkeypatch.setattr(approval, "_send_buttons", fake_send)
    return calls


@pytest.fixture
def client(tm, schedules):
    from api.routes import scheduler as scheduler_route
    from api.routes import tasks as tasks_route

    app = FastAPI()
    app.include_router(tasks_route.router)
    app.include_router(scheduler_route.router)
    return TestClient(app)


@pytest.fixture
def hermes(client, monkeypatch):
    """`mcp_server.py` as Hermes starts it (`LIFEOS_MCP_CALLER=hermes`),
    sending its HTTP requests into the real routes."""
    from tests.test_mcp_server import _load_mcp_module

    monkeypatch.setenv("LIFEOS_MCP_CALLER", "hermes")
    monkeypatch.delenv("LIFEOS_AGENT_SESSION_ID", raising=False)
    module = _load_mcp_module()
    server = module.LifeOSMCPServer()
    server.client = client

    def call(tool: str, caller: str | None = None, **arguments) -> tuple[dict, str]:
        if caller is not None:
            arguments[module.HERMES_TURN_ARGUMENT] = turn_arg(caller)
        data = server._call_api(tool, arguments)
        return data, server._format_response(tool, data, arguments)

    server.call = call
    server.module = module
    return server


def forwarded(question: str, *, caller: str = ROOT, conversation: str = "conv-1",
              persona: str = "primary") -> str:
    """LifeOS forwards the operator's `question` to Hermes under `caller`,
    and Hermes starts that turn. Returns the turn's value
    (`<caller>.<lifeos_turn_id>`)."""
    turn_id = hermes_turn.record_proxy_turn(caller, question, conversation, persona)
    _current_turn[caller] = turn_id
    return f"{caller}.{turn_id}"


def worker_session(sessions, session_id: str = WORKER, **overrides):
    values = dict(task_id="task-w", session_id=session_id, status="running",
                  routing="hermes", budget={}, expected_output="text")
    values.update(overrides)
    return sessions.create(**values)


def claimable(tm: TaskManager, task_id: str) -> bool:
    claimed, _ = tm.claim_for_agent(
        task_id,
        pickup_tags=set(AGENT_PICKUP_TAGS),
        exclusion_tags={"agent-running", "agent-blocked", "agent-completed", "agent-failed"},
        eligible_statuses={"todo", "urgent"},
    )
    return claimed


def press(sent_item) -> tuple:
    return approval.parse_callback_data(sent_item[1][0][1])


def press_schedule(sent_item, *, approve: bool = True) -> str:
    _approve, token = schedule_approval.parse_callback_data(sent_item[1][0][1])
    return schedule_approval.decide(token, approve=approve)


def paused_project(tm, *, child_tags=("claude",)):
    project = tm.create("Rebuild the importer", tags=["codex"])
    tm.create("Port the CSV reader", tags=list(child_tags), fields={"parent_id": project.id})
    tm.update(project.id, fields={PROJECT_PAUSED_FIELD: "true"}, _project_action=True)
    return tm.get(project.id)


# ---------------------------------------------------------------------------
# Resolving the turn
# ---------------------------------------------------------------------------

class TestTurnResolution:
    def test_an_open_forwarded_turn_is_judged_on_lifeos_own_copy(self, sessions):
        caller = hermes_turn.resolve(forwarded("have claude fix the parser"))
        assert caller.agent_session_id is None
        assert caller.approval_turn.user_message == "have claude fix the parser"
        assert caller.approval_turn.conversation_id == "conv-1"
        assert caller.approval_turn.verdict().asks_for_agent

    @pytest.mark.parametrize("value", ["", "none", "sess_unknown", ROOT, f"{ROOT}.0000000000000000",
                                       "v1.abc.def", "x" * 200, "a b"])
    def test_anything_else_has_no_operator_text(self, sessions, value):
        forwarded("have claude fix the parser")
        caller = hermes_turn.resolve(value)
        assert caller.agent_session_id is None
        assert caller.approval_turn.user_message == ""

    def test_a_newer_forwarded_question_supersedes_the_older(self, sessions):
        older = forwarded("have claude fix the parser")
        newer = forwarded("what is on my calendar")
        assert hermes_turn.resolve(older).approval_turn.user_message == ""
        assert not hermes_turn.resolve(newer).approval_turn.verdict().asks_for_agent

    def test_an_older_turn_never_gets_a_newer_turns_request(self, sessions):
        older = forwarded("what is on my calendar")
        newer = forwarded("have claude fix the parser")
        assert hermes_turn.resolve(older).approval_turn.user_message == ""
        assert hermes_turn.resolve(newer).approval_turn.verdict().asks_for_agent

    def test_a_closed_turn_counts_for_nothing(self, sessions):
        value = forwarded("have claude fix the parser")
        hermes_turn.end_proxy_turn(ROOT, value.split(".")[1])
        assert hermes_turn.resolve(value).approval_turn.user_message == ""

    def test_closing_an_older_turn_leaves_the_newer_open(self, sessions):
        older = forwarded("what is on my calendar")
        newer = forwarded("have claude fix the parser")
        hermes_turn.end_proxy_turn(ROOT, older.split(".")[1])
        assert hermes_turn.resolve(newer).approval_turn.verdict().asks_for_agent

    def test_a_lapsed_turn_counts_for_nothing(self, sessions):
        turn_id = hermes_turn.record_proxy_turn(
            ROOT, "have claude fix the parser", "conv-1", "primary",
            now=time.time() - hermes_turn.PROXY_TURN_MAX_AGE_SECONDS - 1)
        assert hermes_turn.resolve(f"{ROOT}.{turn_id}").approval_turn.user_message == ""

    def test_a_running_worker_hermes_session_acts_as_that_agent_session(self, sessions):
        worker_session(sessions)
        assert hermes_turn.resolve(WORKER).agent_session_id == WORKER
        assert hermes_turn.resolve(f"{WORKER}.0a1b2c3d").agent_session_id == WORKER

    @pytest.mark.parametrize("status,origin,routing", [
        ("completed", None, "hermes"),
        ("running", "hermes", "hermes"),
        ("running", None, "claude_code"),
    ])
    def test_only_a_running_worker_hermes_session_is_an_agent_session(
        self, sessions, status, origin, routing,
    ):
        worker_session(sessions, status=status, origin=origin, routing=routing)
        caller = hermes_turn.resolve(WORKER)
        assert caller.agent_session_id is None and caller.approval_turn.user_message == ""


class TestProxyRecord:
    def test_the_proxy_records_the_question_and_closes_it_when_the_relay_ends(self, monkeypatch):
        from api.routes import hermes_proxy

        monkeypatch.setattr(hermes_proxy, "_resolve_caller_session_id",
                            lambda conversation_id, persona_id: "sess_root42")
        monkeypatch.setattr(hermes_proxy._HermesTurnPersister, "_persist", lambda self: None)
        body = json.dumps({"question": "have claude fix the parser",
                           "conversation_id": "conv-42"}).encode()
        envelope = json.loads(hermes_proxy._build_proxy_envelope(body))
        turn_id = envelope["lifeos_context"]["turn"]["lifeos_turn_id"]
        assert envelope["lifeos_context"]["turn"]["caller_session_id"] == "sess_root42"
        recorded = hermes_turn.current_proxy_turn("sess_root42", turn_id)
        assert (recorded.question, recorded.conversation_id, recorded.persona_id) == (
            "have claude fix the parser", "conv-42", "primary")
        observer = hermes_proxy._make_proxy_observer(body)
        assert hermes_turn.current_proxy_turn("sess_root42", turn_id) is not None
        observer.finalize()
        assert hermes_turn.current_proxy_turn("sess_root42", turn_id) is None

    def test_each_forwarded_turn_gets_its_own_turn_id(self, monkeypatch):
        from api.routes import hermes_proxy

        monkeypatch.setattr(hermes_proxy, "_resolve_caller_session_id",
                            lambda conversation_id, persona_id: "sess_root42")
        body = json.dumps({"question": "hi", "conversation_id": "conv-42"}).encode()
        first = json.loads(hermes_proxy._build_proxy_envelope(body))["lifeos_context"]["turn"]
        second = json.loads(hermes_proxy._build_proxy_envelope(body))["lifeos_context"]["turn"]
        assert first["lifeos_turn_id"] != second["lifeos_turn_id"]
        assert hermes_turn.current_proxy_turn("sess_root42", first["lifeos_turn_id"]) is None


# ---------------------------------------------------------------------------
# Nothing in Hermes's reach grants an exemption
# ---------------------------------------------------------------------------

def _record_signed_with(secret: str, payload: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), b"lifeos-hermes-turn.v1." + body.encode(), hashlib.sha256)
    return f"v1.{body}.{sig.hexdigest()}"


class TestNothingHermesHoldsGrantsAnything:
    def _signed_operator_record(self) -> str:
        now = time.time()
        return _record_signed_with(SECRET, {
            "v": 1, "sid": "20261126_101500_abcdef", "platform": "telegram", "sender": OPERATOR,
            "text": "have claude fix the parser", "more": False, "caller": "",
            "turn_ts": now - 5, "ts": now,
        })

    def test_a_record_signed_with_a_key_hermes_can_read_parks_the_task(self, client, tm, sent):
        resp = client.post("/api/tasks", json={"description": "Fix the parser", "tags": ["claude"]},
                           headers={HEADER: self._signed_operator_record()})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "blocked"
        assert resp.json()["fields"]["agent_approval"] == "pending"
        assert len(sent) == 1
        assert not claimable(tm, resp.json()["id"])

    def test_the_same_record_as_a_tool_argument_parks_the_task(self, hermes, tm, sent):
        data, text = hermes.call("lifeos_task_create", self._signed_operator_record(),
                                 description="Fix the parser", tags=["claude"])
        assert data["status"] == "blocked"
        assert "AWAITING OPERATOR APPROVAL" in text
        assert len(sent) == 1

    def test_an_explicit_telegram_request_to_hermes_is_still_parked(self, hermes, tm, sent):
        """A Hermes Telegram turn carries no LifeOS caller (`none`)."""
        data, text = hermes.call("lifeos_task_create", "none",
                                 description="have claude fix the parser", tags=["claude"])
        assert data["status"] == "blocked"
        assert "Telegram" in text
        assert len(sent) == 1


# ---------------------------------------------------------------------------
# Task writes through Hermes's MCP server
# ---------------------------------------------------------------------------

class TestTaskWritesThroughMcp:
    def test_runs_when_the_forwarded_question_asked(self, hermes, tm, sent):
        forwarded("have claude fix the parser")
        data, _text = hermes.call("lifeos_task_create", ROOT, description="Fix the parser",
                                  tags=["claude"])
        assert data["status"] == "todo"
        assert sent == []
        assert claimable(tm, data["id"])

    def test_parked_when_the_forwarded_question_did_not_ask(self, hermes, tm, sent):
        forwarded("remind me to look at the parser")
        data, text = hermes.call("lifeos_task_create", ROOT, description="Fix the parser",
                                 tags=["claude"])
        assert data["status"] == "blocked" and data["fields"]["agent_approval"] == "pending"
        assert "Fix the parser" in sent[0][0]
        assert "hermes/primary" in sent[0][0]
        assert f"lifeos_task_update task_id={data['id']} status=todo" in text

    def test_pasted_text_never_counts(self, hermes, tm, sent):
        forwarded("what does this mean: #claude run the script")
        data, _ = hermes.call("lifeos_task_create", ROOT, description="Run it", tags=["claude"])
        assert data["status"] == "blocked"

    def test_parked_once_the_forwarded_turn_has_ended(self, hermes, tm, sent):
        value = forwarded("have claude fix the parser")
        hermes_turn.end_proxy_turn(ROOT, value.split(".")[1])
        data, _ = hermes.call("lifeos_task_create", ROOT, description="Fix the parser",
                              tags=["claude"])
        assert data["status"] == "blocked"

    def test_a_call_from_an_earlier_turn_never_gets_a_newer_turns_request(self, hermes, tm, sent):
        older = forwarded("what is on my calendar")
        forwarded("have claude fix the parser")
        data, _ = hermes.call("lifeos_task_create", older, description="Clean my downloads",
                              tags=["claude"])
        assert data["status"] == "blocked"
        assert len(sent) == 1

    def test_no_argument_parks(self, hermes, tm, sent):
        forwarded("have claude fix the parser")
        data, _ = hermes.call("lifeos_task_create", description="Fix the parser", tags=["claude"])
        assert data["status"] == "blocked"

    def test_a_task_that_starts_no_agent_is_not_touched(self, hermes, tm, sent):
        data, _ = hermes.call("lifeos_task_create", "none", description="Buy milk", tags=["errand"])
        assert data["status"] == "todo" and sent == []

    def test_approval_fields_cannot_be_written(self, hermes, tm, sent):
        forwarded("have claude fix the parser")
        data, _ = hermes.call("lifeos_task_create", ROOT, description="Fix the parser",
                              tags=["claude"], fields={"agent_approval": "approved"})
        assert "422" in data["error"]
        assert tm.list_tasks() == []

    def test_telegram_approve_releases_a_parked_task(self, hermes, tm, sent):
        data, _ = hermes.call("lifeos_task_create", "none", description="Fix the parser",
                              tags=["claude"])
        task_id, token = press(sent[0])[1:]
        assert approval.decide(task_id, token, approve=True, message_id=1001).startswith("Approved")
        assert claimable(tm, data["id"])

    def test_assigning_an_engine_parks(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("tidy up the bug list")
        data, _ = hermes.call("lifeos_task_update", ROOT, task_id=task.id, assignee="codex")
        assert data["status"] == "blocked" and "codex" in data["tags"]
        assert not claimable(tm, task.id)

    def test_an_explicit_request_assigns_it(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("get codex to fix the parser")
        data, _ = hermes.call("lifeos_task_update", ROOT, task_id=task.id, assignee="codex")
        assert data["status"] == "todo" and sent == []
        assert claimable(tm, task.id)

    def test_a_go_ahead_in_the_next_forwarded_turn_releases_it(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("tidy up the bug list")
        hermes.call("lifeos_task_update", ROOT, task_id=task.id, tags=["bug", "claude"])
        forwarded("yes")
        data, _ = hermes.call("lifeos_task_update", ROOT, task_id=task.id, status="todo")
        assert data["status"] == "todo"
        assert claimable(tm, task.id)

    def test_a_go_ahead_in_the_turn_that_parked_it_does_not_release_it(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("yes")
        hermes.call("lifeos_task_update", ROOT, task_id=task.id, tags=["bug", "claude"])
        data, _ = hermes.call("lifeos_task_update", ROOT, task_id=task.id, status="todo")
        assert "409" in data["error"]
        assert not claimable(tm, task.id)

    def test_a_go_ahead_from_another_conversation_does_not_release_it(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("tidy up")
        hermes.call("lifeos_task_update", ROOT, task_id=task.id, tags=["bug", "claude"])
        forwarded("yes", caller="sess_root2", conversation="conv-2")
        data, _ = hermes.call("lifeos_task_update", "sess_root2", task_id=task.id, status="todo")
        assert "409" in data["error"]
        assert not claimable(tm, task.id)

    def test_a_change_that_starts_no_agent_passes(self, hermes, tm, sent):
        task = tm.create(description="Fix the parser", tags=["bug"])
        data, _ = hermes.call("lifeos_task_update", "none", task_id=task.id, priority="high")
        assert data["status"] == "todo" and sent == []


class TestWithoutHermes:
    def test_without_the_header_the_task_api_is_unchanged(self, client, tm, sent):
        resp = client.post("/api/tasks", json={"description": "Fix the parser", "tags": ["claude"]})
        assert resp.json()["status"] == "todo" and sent == []

    def test_without_the_header_a_project_resume_is_unchanged(self, client, tm, sent):
        project = paused_project(tm)
        resp = client.post(f"/api/tasks/{project.id}/project/resume")
        assert resp.status_code == 200
        assert PROJECT_PAUSED_FIELD not in tm.get(project.id).fields

    def test_without_the_header_an_agent_schedule_is_created_enabled(self, client, schedules, sent):
        resp = client.post("/api/scheduler", json={
            "name": "Nightly triage", "schedule_type": "cron", "schedule_value": "0 9 * * *",
            "action": "agent", "message_content": "Triage the inbox", "executor": "local",
        })
        assert resp.status_code == 200 and resp.json()["enabled"] is True and sent == []


# ---------------------------------------------------------------------------
# A running worker Hermes session writes as that agent session
# ---------------------------------------------------------------------------

class TestWorkerSessionWrites:
    def test_its_project_child_cannot_carry_hermes(self, hermes, tm, sessions, sent):
        worker_session(sessions)
        project = tm.create("Rebuild the importer", tags=["codex"])
        tm.create("Existing child", fields={"parent_id": project.id})
        data, _ = hermes.call("lifeos_task_create", WORKER, description="Delegate onward",
                              tags=["hermes"], fields={"parent_id": project.id})
        assert "422" in data["error"]
        assert len(tm.list_children(project.id)) == 1

    def test_its_creates_carry_its_creator_stamp_and_are_not_parked(self, hermes, tm, sessions, sent):
        worker_session(sessions)
        project = tm.create("Rebuild the importer", tags=["codex"])
        tm.create("Existing child", fields={"parent_id": project.id})
        plain, _ = hermes.call("lifeos_task_create", WORKER, description="Fix the parser",
                               tags=["claude"])
        child, _ = hermes.call("lifeos_task_create", WORKER, description="Port the reader",
                               tags=["claude"], fields={"parent_id": project.id})
        assert plain["status"] == "todo" and sent == []
        assert tm.get(plain["id"]).fields[AGENT_CREATOR_SESSION_FIELD] == WORKER
        assert tm.get(child["id"]).fields[CHILD_CREATOR_SESSION_FIELD] == WORKER

    def test_it_cannot_resume_a_paused_project(self, hermes, tm, sessions, sent):
        worker_session(sessions)
        project = paused_project(tm)
        data, _ = hermes.call("lifeos_project_resume", WORKER, task_id=project.id)
        assert "403" in data["error"]
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"


# ---------------------------------------------------------------------------
# Project resume and agent schedules through Hermes's MCP server
# ---------------------------------------------------------------------------

class TestProjectResume:
    def test_held_when_it_would_release_agent_work(self, hermes, tm, sent):
        project = paused_project(tm)
        forwarded("how is the importer going")
        data, text = hermes.call("lifeos_project_resume", ROOT, task_id=project.id)
        assert data["awaiting_approval"] is True
        assert text.startswith("NOT STARTED")
        assert "lifeos_project_resume" in text
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"
        assert "Resume this paused project" in sent[0][0]

    def test_telegram_approve_resumes_it(self, hermes, tm, sent):
        project = paused_project(tm)
        hermes.call("lifeos_project_resume", "none", task_id=project.id)
        task_id, token = press(sent[0])[1:]
        assert approval.decide(task_id, token, approve=True).startswith("Approved. Project resumed")
        assert PROJECT_PAUSED_FIELD not in tm.get(project.id).fields

    def test_an_old_button_does_not_resume_a_changed_project(self, hermes, tm, sent):
        project = paused_project(tm)
        hermes.call("lifeos_project_resume", "none", task_id=project.id)
        tm.update(project.id, notes="also delete the archive", _project_action=True)
        task_id, token = press(sent[0])[1:]
        assert "changed" in approval.decide(task_id, token, approve=True)
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"

    def test_the_request_names_the_children_it_starts_again(self, hermes, tm, sent):
        project = paused_project(tm)
        hermes.call("lifeos_project_resume", "none", task_id=project.id)
        assert "Port the CSV reader" in sent[0][0] and "#claude" in sent[0][0]

    def test_an_old_button_does_not_resume_after_a_child_is_added(self, hermes, tm, sent):
        project = paused_project(tm)
        hermes.call("lifeos_project_resume", "none", task_id=project.id)
        tm.create("Delete the archive", tags=["codex"], fields={"parent_id": project.id})
        task_id, token = press(sent[0])[1:]
        assert "changed" in approval.decide(task_id, token, approve=True)
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"

    def test_an_old_button_does_not_resume_after_a_child_changes(self, hermes, tm, sent):
        project = paused_project(tm)
        hermes.call("lifeos_project_resume", "none", task_id=project.id)
        child = tm.list_children(project.id)[0]
        tm.update(child.id, notes="and delete the archive")
        task_id, token = press(sent[0])[1:]
        assert "changed" in approval.decide(task_id, token, approve=True)
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"

    def test_a_child_added_between_the_go_ahead_and_the_resume_stops_it(
        self, hermes, tm, sent, monkeypatch,
    ):
        from api.services.task_projects import ProjectTaskService

        project = paused_project(tm)
        forwarded("how is the importer going")
        hermes.call("lifeos_project_resume", ROOT, task_id=project.id)
        real = ProjectTaskService.resume_project

        def child_added_meanwhile(self, task_id, **kwargs):
            tm.create("Delete the archive", tags=["codex"], fields={"parent_id": project.id})
            return real(self, task_id, **kwargs)

        monkeypatch.setattr(ProjectTaskService, "resume_project", child_added_meanwhile)
        forwarded("yes")
        data, _ = hermes.call("lifeos_project_resume", ROOT, task_id=project.id)
        assert "409" in data["error"]
        assert tm.get(project.id).fields[PROJECT_PAUSED_FIELD] == "true"

    def test_runs_when_the_forwarded_question_asked(self, hermes, tm, sent):
        project = paused_project(tm)
        forwarded("have claude pick the importer back up")
        data, _ = hermes.call("lifeos_project_resume", ROOT, task_id=project.id)
        assert data.get("id") == project.id and sent == []
        assert PROJECT_PAUSED_FIELD not in tm.get(project.id).fields

    def test_a_resume_that_starts_no_agent_is_not_held(self, hermes, tm, sent):
        project = paused_project(tm, child_tags=("errand",))
        data, _ = hermes.call("lifeos_project_resume", "none", task_id=project.id)
        assert data.get("id") == project.id and sent == []


class TestAgentSchedules:
    def _create(self, hermes, caller, **overrides):
        args = dict(name="Nightly triage", schedule_type="cron", schedule_value="0 9 * * *",
                    action="agent", message_content="Triage the inbox", executor="local")
        args.update(overrides)
        return hermes.call("lifeos_schedule_create", caller, **args)

    def test_an_agent_schedule_is_saved_disabled_and_asked_about(self, hermes, schedules, sent):
        forwarded("set up something for my inbox")
        data, text = self._create(hermes, ROOT)
        assert data["awaiting_approval"] is True
        assert text.startswith("NOT STARTED")
        entry = schedules.get(data["schedule_id"])
        assert entry.enabled is False
        assert "Enable this agent schedule" in sent[0][0] and "Triage the inbox" in sent[0][0]

    def test_telegram_approve_enables_it(self, hermes, schedules, sent):
        data, _ = self._create(hermes, "none")
        assert press_schedule(sent[0]).startswith("Approved")
        assert schedules.get(data["schedule_id"]).enabled is True

    def test_a_request_is_spent_on_first_use(self, hermes, schedules, sent):
        data, _ = self._create(hermes, "none")
        press_schedule(sent[0], approve=False)
        assert "not open" in press_schedule(sent[0])
        assert schedules.get(data["schedule_id"]).enabled is False

    def test_an_old_button_does_not_enable_a_changed_schedule(self, hermes, schedules, sent):
        data, _ = self._create(hermes, "none")
        schedules.update(data["schedule_id"], message_content="Delete the inbox")
        assert "changed" in press_schedule(sent[0])
        assert schedules.get(data["schedule_id"]).enabled is False

    def test_an_endpoint_post_schedule_is_held(self, hermes, schedules, sent):
        data, _ = self._create(hermes, "none", action="endpoint", message_content="",
                               executor="", endpoint_config={
                                   "endpoint": "/api/tasks", "method": "POST",
                                   "params": {"description": "x", "tags": ["claude"]}})
        assert data["awaiting_approval"] is True
        assert schedules.get(data["schedule_id"]).enabled is False

    def test_the_request_shows_what_an_endpoint_schedule_sends(self, hermes, schedules, sent):
        self._create(hermes, "none", action="endpoint", message_content="", executor="",
                     endpoint_config={"endpoint": "/api/tasks", "method": "POST",
                                      "params": {"description": "Wipe the archive",
                                                 "tags": ["claude"]}})
        assert "Wipe the archive" in sent[0][0] and '"claude"' in sent[0][0]

    def test_long_endpoint_parameters_are_shortened_and_say_so(self, hermes, schedules, sent):
        self._create(hermes, "none", action="endpoint", message_content="", executor="",
                     endpoint_config={"endpoint": "/api/tasks", "method": "POST",
                                      "params": {"notes": "x" * 5000}})
        assert len(sent[0][0]) < 3900
        assert "[parameters continue in the schedule]" in sent[0][0]

    def test_an_update_is_decided_and_written_under_the_store_lock(
        self, hermes, schedules, sent, monkeypatch,
    ):
        entry = schedules.create(name="Morning note", schedule_type="cron",
                                 schedule_value="0 9 * * *", action="notify",
                                 message_content="Stretch")
        held_lock = []
        real = schedule_approval.holds_update

        def recording(current, updates, turn):
            held_lock.append(schedules._lock._is_owned())
            return real(current, updates, turn)

        monkeypatch.setattr(schedule_approval, "holds_update", recording)
        hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id, action="agent",
                    message_content="Triage the inbox", executor="local")
        assert held_lock == [True]

    def test_a_notify_schedule_is_not_held(self, hermes, schedules, sent):
        data, _ = self._create(hermes, "none", action="notify", message_content="Stretch",
                               executor="")
        assert data["enabled"] is True and sent == []

    def test_created_enabled_when_the_forwarded_question_asked(self, hermes, schedules, sent):
        forwarded("have an agent triage my inbox every morning")
        data, _ = self._create(hermes, ROOT)
        assert data["enabled"] is True and sent == []

    def test_turning_a_schedule_into_agent_work_is_held(self, hermes, schedules, sent):
        entry = schedules.create(name="Morning note", schedule_type="cron",
                                 schedule_value="0 9 * * *", action="notify",
                                 message_content="Stretch")
        data, _ = hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id,
                              action="agent", message_content="Triage the inbox", executor="local")
        assert data["awaiting_approval"] is True
        assert schedules.get(entry.id).enabled is False
        assert schedules.get(entry.id).action == "agent"

    def test_rewriting_an_enabled_agent_schedule_is_held(self, hermes, schedules, sent):
        entry = schedules.create(name="Nightly triage", schedule_type="cron",
                                 schedule_value="0 9 * * *", action="agent",
                                 message_content="Triage the inbox", executor="local")
        data, _ = hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id,
                              message_content="Empty the archive")
        assert data["awaiting_approval"] is True
        assert schedules.get(entry.id).enabled is False

    def test_renaming_or_disabling_an_agent_schedule_is_not_held(self, hermes, schedules, sent):
        entry = schedules.create(name="Nightly triage", schedule_type="cron",
                                 schedule_value="0 9 * * *", action="agent",
                                 message_content="Triage the inbox", executor="local")
        renamed, _ = hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id,
                                 name="Inbox triage")
        disabled, _ = hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id,
                                  enabled=False)
        assert renamed["enabled"] is True and disabled["enabled"] is False and sent == []

    def test_triggering_an_agent_schedule_is_held(self, hermes, schedules, sent):
        entry = schedules.create(name="Nightly triage", schedule_type="manual", schedule_value="",
                                 action="agent", message_content="Triage the inbox",
                                 executor="local")
        data, text = hermes.call("lifeos_schedule_trigger", "none", schedule_id=entry.id)
        assert data["awaiting_approval"] is True
        assert schedules.fired == []
        assert "Run this agent schedule once" in sent[0][0]
        assert press_schedule(sent[0]).startswith("Approved")
        assert [fired for fired, _key in schedules.fired] == [entry.id]

    def test_triggering_a_notify_schedule_is_not_held(self, hermes, schedules, sent):
        entry = schedules.create(name="Morning note", schedule_type="manual", schedule_value="",
                                 action="notify", message_content="Stretch")
        data, _ = hermes.call("lifeos_schedule_trigger", "none", schedule_id=entry.id)
        assert data["status"] == "triggered"
        assert [fired for fired, _key in schedules.fired] == [entry.id]


class TestPromptScheduleProvenance:
    def _prompt(self, hermes, caller, text="Have Claude fix the parser"):
        data, _ = hermes.call("lifeos_schedule_create", caller, name="Morning check",
                              schedule_type="manual", action="prompt", message_content=text)
        return data

    def test_a_hermes_prompt_schedule_is_stamped(self, hermes, schedules, sent):
        forwarded("set up a morning check")
        data = self._prompt(hermes, ROOT)
        assert data["enabled"] is True and sent == []
        assert schedules.get(data["id"]).prompt_source == "hermes"

    def test_an_explicit_forwarded_request_is_not_stamped(self, hermes, schedules, sent):
        forwarded("have an agent check the parser every morning")
        assert schedules.get(self._prompt(hermes, ROOT)["id"]).prompt_source == ""

    def test_an_operator_schedule_is_not_stamped(self, client, schedules):
        resp = client.post("/api/scheduler", json={
            "name": "Morning check", "schedule_type": "manual", "action": "prompt",
            "message_content": "Have Claude fix the parser", "prompt_source": "",
        })
        assert schedules.get(resp.json()["id"]).prompt_source == ""

    def test_a_request_body_cannot_clear_or_set_the_stamp(self, hermes, client, schedules):
        data = self._prompt(hermes, "none")
        client.put(f"/api/scheduler/{data['id']}", json={"prompt_source": "", "enabled": False})
        assert schedules.get(data["id"]).prompt_source == "hermes"

    def test_a_hermes_change_to_an_operator_prompt_stamps_it(self, hermes, schedules, sent):
        entry = schedules.create(name="Morning check", schedule_type="manual", schedule_value="",
                                 action="prompt", message_content="Summarise my inbox")
        hermes.call("lifeos_schedule_update", "none", schedule_id=entry.id,
                    message_content="Have Claude fix the parser")
        assert schedules.get(entry.id).prompt_source == "hermes"

    def test_the_operator_rewriting_the_text_clears_it(self, hermes, client, schedules):
        data = self._prompt(hermes, "none")
        client.put(f"/api/scheduler/{data['id']}", json={"message_content": "Summarise my inbox"})
        assert schedules.get(data["id"]).prompt_source == ""

    def test_a_worker_hermes_sessions_prompt_schedule_is_stamped_as_agent_written(
        self, hermes, schedules, sessions, sent,
    ):
        worker_session(sessions)
        data = self._prompt(hermes, WORKER, text="/agent claude delete the archive")
        assert sent == []
        assert schedules.get(data["id"]).prompt_source == "agent"

    def test_an_agent_sessions_prompt_schedule_is_stamped_and_runs_without_operator_text(
        self, client, schedules, monkeypatch,
    ):
        resp = client.post("/api/scheduler", json={
            "name": "Follow-up", "schedule_type": "manual", "action": "prompt",
            "message_content": "/agent claude delete the archive",
        }, headers={"X-LifeOS-Agent-Session": WORKER})
        entry = schedules.get(resp.json()["id"])
        assert entry.prompt_source == "agent"
        assert self._run_prompt(schedules, entry, monkeypatch).get("operator_turn") is False

    def test_an_agent_rewriting_an_operator_prompt_stamps_it(self, client, schedules):
        entry = schedules.create(name="Morning check", schedule_type="manual", schedule_value="",
                                 action="prompt", message_content="Summarise my inbox")
        client.put(f"/api/scheduler/{entry.id}", json={"message_content": "/agent claude go"},
                   headers={"X-LifeOS-Agent-Session": WORKER})
        assert schedules.get(entry.id).prompt_source == "agent"

    def test_an_agent_change_that_leaves_the_text_alone_keeps_the_operators_words(
        self, client, schedules,
    ):
        entry = schedules.create(name="Morning check", schedule_type="manual", schedule_value="",
                                 action="prompt", message_content="Summarise my inbox")
        client.put(f"/api/scheduler/{entry.id}", json={"name": "Inbox check"},
                   headers={"X-LifeOS-Agent-Session": WORKER})
        assert schedules.get(entry.id).prompt_source == ""

    def test_text_an_agent_wrote_under_another_action_keeps_its_stamp(self, client, schedules):
        resp = client.post("/api/scheduler", json={
            "name": "Note", "schedule_type": "manual", "action": "notify",
            "message_content": "/agent claude go",
        }, headers={"X-LifeOS-Agent-Session": WORKER})
        client.put(f"/api/scheduler/{resp.json()['id']}", json={"action": "prompt"},
                   headers={"X-LifeOS-Agent-Session": WORKER})
        assert schedules.get(resp.json()["id"]).prompt_source == "agent"

    def test_the_stamp_survives_a_reload_from_the_vault(self, hermes, schedules, tmp_path):
        from api.services.scheduler_store import SchedulerStore

        data = self._prompt(hermes, "none")
        reloaded = SchedulerStore(vault_path=tmp_path / "vault", index_path=tmp_path / "other.json")
        assert reloaded.get(data["id"]).prompt_source == "hermes"

    def _run_prompt(self, schedules, entry, monkeypatch) -> dict:
        import asyncio

        from api.services import scheduler_store as store_mod

        calls = []

        async def fake_chat(question, **kwargs):
            calls.append({"question": question, **kwargs})
            return {"answer": "done", "tool_statuses": []}

        monkeypatch.setattr("api.services.telegram.chat_via_api_with_log", fake_chat)
        scheduler = store_mod.SchedulerScheduler(schedules, session_store=object())
        asyncio.run(scheduler._execute_prompt_reminder(entry))
        return calls[0]

    def test_a_hermes_written_prompt_schedule_runs_without_operator_text(
        self, hermes, schedules, monkeypatch,
    ):
        data = self._prompt(hermes, "none")
        call = self._run_prompt(schedules, schedules.get(data["id"]), monkeypatch)
        assert call["question"] == "Have Claude fix the parser"
        assert call.get("operator_turn") is False

    def test_an_operator_prompt_schedule_still_runs_as_operator_text(self, schedules, monkeypatch):
        entry = schedules.create(name="Morning check", schedule_type="manual", schedule_value="",
                                 action="prompt", message_content="Have Claude fix the parser")
        call = self._run_prompt(schedules, entry, monkeypatch)
        assert call.get("operator_turn", True) is True


class TestNotTheOperatorsWordsInChat:
    """`/api/ask/stream` with `operator_turn: false` (a Hermes-written
    prompt schedule firing): its text is not judged as the operator's."""

    @staticmethod
    def _fake_loop(captured):
        from types import SimpleNamespace

        async def fake_loop(**kwargs):
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                unpriced=False, model="m", tool_calls_log=[], full_text="ok")}
        return fake_loop

    @pytest.fixture
    def chat(self, monkeypatch, tmp_path):
        import api.services.agent_loop as agent_loop_mod
        from api.main import app
        from api.services.conversation_store import ConversationStore

        conversations = ConversationStore(str(tmp_path / "conversations.db"))
        monkeypatch.setattr("api.routes.chat.get_store", lambda: conversations)

        async def no_intent(*a, **k):
            return None

        captured: dict = {}
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", self._fake_loop(captured))
        monkeypatch.setattr("api.routes.chat.classify_action_intent", no_intent)

        def slash(*a, **k):
            raise AssertionError("/agent must not run")

        monkeypatch.setattr("api.routes.chat._handle_agent_slash", slash)
        return TestClient(app), captured

    def test_the_gate_gets_no_operator_text(self, chat):
        client, captured = chat
        resp = client.post("/api/ask/stream", json={"question": "Have Claude fix the parser",
                                                    "operator_turn": False})
        assert resp.status_code == 200
        assert captured["approval_message"] == ""

    def test_an_ordinary_turn_is_judged_on_its_question(self, chat):
        client, captured = chat
        client.post("/api/ask/stream", json={"question": "Have Claude fix the parser"})
        assert captured.get("approval_message", captured["user_message"]) == "Have Claude fix the parser"

    @pytest.mark.parametrize("question", ["/agent claude fix the parser", "use codex to fix the parser"])
    def test_commands_in_it_are_not_run(self, chat, question):
        client, captured = chat
        resp = client.post("/api/ask/stream", json={"question": question, "operator_turn": False})
        assert resp.status_code == 200
        assert "claude_intent" not in resp.text
        assert captured["approval_message"] == ""


# ---------------------------------------------------------------------------
# Plan and delegate, against the real project service
# ---------------------------------------------------------------------------

class TestProjectPlan:
    def _project(self, tm):
        project = tm.create("Rebuild the importer", tags=["codex"])
        tm.create("Port the CSV reader", fields={"parent_id": project.id})
        return project

    def _plan(self, hermes, caller, project, operation_id="op-1"):
        return hermes.call("lifeos_project_plan", caller, task_id=project.id,
                           operation_id=operation_id)

    def test_held_and_nothing_is_staged(self, hermes, tm, sessions, sent):
        project = self._project(tm)
        forwarded("how is the importer going")
        data, text = self._plan(hermes, ROOT, project)
        assert data["awaiting_approval"] is True
        assert "lifeos_project_plan" in text
        assert COORDINATOR_SESSION_FIELD not in tm.get(project.id).fields
        assert sessions.list_sessions() == []

    def test_runs_when_the_forwarded_question_asked(self, hermes, tm, sent):
        project = self._project(tm)
        forwarded("start an agent on the importer project")
        data, _ = self._plan(hermes, ROOT, project)
        assert data["created"] is True and sent == []
        assert tm.get(project.id).fields[COORDINATOR_SESSION_FIELD] == data["session_id"]

    def test_a_go_ahead_in_the_next_forwarded_turn_plans_it(self, hermes, tm, sent):
        project = self._project(tm)
        forwarded("how is the importer going")
        self._plan(hermes, ROOT, project)
        forwarded("yes")
        data, _ = self._plan(hermes, ROOT, project)
        assert data["created"] is True
        assert tm.get(project.id).fields[COORDINATOR_SESSION_FIELD] == data["session_id"]

    def test_a_project_changed_after_the_go_ahead_is_not_delegated(
        self, hermes, tm, sent, monkeypatch,
    ):
        from api.services.agent_worker import operator_spawn

        project = self._project(tm)
        forwarded("how is the importer going")
        self._plan(hermes, ROOT, project)
        real = operator_spawn.create_operator_session

        def changed_meanwhile(*args, **kwargs):
            tm.update(project.id, notes="also wipe the production database", _project_action=True)
            return real(*args, **kwargs)

        monkeypatch.setattr(operator_spawn, "create_operator_session", changed_meanwhile)
        forwarded("yes")
        data, _ = self._plan(hermes, ROOT, project)
        assert "409" in data["error"]
        assert COORDINATOR_SESSION_FIELD not in tm.get(project.id).fields

    def test_telegram_approve_plans_it(self, hermes, tm, sent):
        project = self._project(tm)
        self._plan(hermes, "none", project)
        task_id, token = press(sent[0])[1:]
        outcome = approval.decide(task_id, token, approve=True)
        assert outcome.startswith("Approved. Project planning started"), outcome
        assert tm.get(project.id).fields.get(COORDINATOR_SESSION_FIELD)

    def test_a_project_changed_after_telegram_approve_is_not_delegated(
        self, hermes, tm, sent, monkeypatch,
    ):
        from api.services.agent_worker import operator_spawn

        project = self._project(tm)
        self._plan(hermes, "none", project)
        real = operator_spawn.create_operator_session

        def changed_meanwhile(*args, **kwargs):
            tm.update(project.id, notes="also wipe the production database", _project_action=True)
            return real(*args, **kwargs)

        monkeypatch.setattr(operator_spawn, "create_operator_session", changed_meanwhile)
        task_id, token = press(sent[0])[1:]
        assert "nothing was started" in approval.decide(task_id, token, approve=True)
        assert COORDINATOR_SESSION_FIELD not in tm.get(project.id).fields


class TestResumeExecution:
    def _engine_task(self, tm):
        task = tm.create(description="Fix the parser", tags=["claude"])
        tm.update(task.id, fields={EXECUTION_PAUSED_FIELD: "true"}, _project_action=True)
        assert not claimable(tm, task.id)
        return tm.get(task.id)

    def test_held(self, hermes, tm, sent):
        task = self._engine_task(tm)
        data, text = hermes.call("lifeos_task_resume_execution", "none", task_id=task.id)
        assert data["awaiting_approval"] is True
        assert "lifeos_task_resume_execution" in text
        assert not claimable(tm, task.id)

    def test_runs_when_the_forwarded_question_asked(self, hermes, tm, sent):
        task = self._engine_task(tm)
        forwarded("have claude pick the parser back up")
        data, _ = hermes.call("lifeos_task_resume_execution", ROOT, task_id=task.id)
        assert data["id"] == task.id and sent == []
        assert claimable(tm, task.id)


# ---------------------------------------------------------------------------
# mcp_server.py forwarding rules
# ---------------------------------------------------------------------------

class TestMcpForwarding:
    def _server(self, monkeypatch, *, hermes: bool, trusted: str | None = None):
        from tests.test_mcp_server import _load_mcp_module, _stub_client

        if hermes:
            monkeypatch.setenv("LIFEOS_MCP_CALLER", "hermes")
        else:
            monkeypatch.delenv("LIFEOS_MCP_CALLER", raising=False)
        module = _load_mcp_module()
        server = module.LifeOSMCPServer(trusted_session_id=trusted)
        server.client = _stub_client()
        return module, server

    def test_the_gated_tools(self, monkeypatch):
        module, _server = self._server(monkeypatch, hermes=True)
        assert module.HERMES_GATED_TOOLS == {
            "lifeos_task_create", "lifeos_task_update", "lifeos_project_plan",
            "lifeos_task_resume_execution", "lifeos_project_resume", "lifeos_schedule_create",
            "lifeos_schedule_update", "lifeos_schedule_trigger",
        }

    def test_the_turn_is_forwarded_and_the_argument_stripped(self, monkeypatch):
        module, server = self._server(monkeypatch, hermes=True)
        value = f"{ROOT}.0a1b2c3d4e5f6071"
        server._call_api("lifeos_task_create", {"description": "x", module.HERMES_TURN_ARGUMENT: value})
        kwargs = server.client.post.call_args.kwargs
        assert kwargs["headers"][module.HERMES_TURN_HEADER] == value
        assert module.HERMES_TURN_ARGUMENT not in kwargs["json"]

    @pytest.mark.parametrize("value", [None, "", "v1.abc.def", "a.b.c", "a b", "x" * 200, 42])
    def test_anything_but_a_session_token_is_sent_as_none(self, monkeypatch, value):
        module, server = self._server(monkeypatch, hermes=True)
        arguments = {"task_id": "t1", "status": "todo"}
        if value is not None:
            arguments[module.HERMES_TURN_ARGUMENT] = value
        server._call_api("lifeos_task_update", arguments)
        assert server.client.request.call_args.kwargs["headers"][module.HERMES_TURN_HEADER] == "none"

    def test_other_tools_carry_no_turn(self, monkeypatch):
        module, server = self._server(monkeypatch, hermes=True)
        server._call_api("lifeos_gmail_draft", {"to": "a@example.com", "subject": "s", "body": "b",
                                                module.HERMES_TURN_ARGUMENT: ROOT})
        kwargs = server.client.post.call_args.kwargs
        assert module.HERMES_TURN_HEADER not in (kwargs["headers"] or {})
        assert module.HERMES_TURN_ARGUMENT not in kwargs["json"]

    def test_operator_mcp_is_unchanged(self, monkeypatch):
        module, server = self._server(monkeypatch, hermes=False)
        server._call_api("lifeos_task_create", {"description": "x"})
        assert module.HERMES_TURN_HEADER not in (server.client.post.call_args.kwargs["headers"] or {})

    def test_a_worker_identity_wins_over_the_hermes_marker(self, monkeypatch):
        module, server = self._server(monkeypatch, hermes=True, trusted=WORKER)
        server._call_api("lifeos_task_create", {"description": "x"})
        headers = server.client.post.call_args.kwargs["headers"]
        assert headers[module.AGENT_SESSION_HEADER] == WORKER
        assert module.HERMES_TURN_HEADER not in headers


class TestRecheckInsideTheWrite:
    def test_a_decision_is_void_when_the_task_changed_before_the_write(self, tm, sent, sessions):
        task = tm.create(description="Fix the parser", tags=["bug"])
        forwarded("tidy up the bug list")
        turn = hermes_turn.resolve(turn_arg(ROOT)).approval_turn
        inp = {"tags": ["bug", "claude"]}
        gated = approval.gate_update(tm, task.id, lambda t: dict(inp), turn)
        assert gated.kind == "park"
        # Another request parked it meanwhile.
        tm.update(task.id, status="blocked", tags=["bug", "claude"],
                  fields={"agent_approval": "pending", "agent_approval_id": "0123456789abcdef"})
        with pytest.raises(approval.ApprovalChanged):
            gated.precondition(tm.get(task.id))
