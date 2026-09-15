"""The reporting channel a Hermes-assigned task notifies through.

A board card assigned to the Hermes engine runs inside Hermes, so its
operator-facing notices belong in the Hermes Telegram DM rather than on
LifeOS's own primary bot. These tests cover the channel being recorded at
claim time, one-way notices being delivered through `hermes send`, the
logged fallback when that CLI is missing or failing, and the question path
staying answerable.

`hermes send` is stubbed at `api/services/hermes_notify.send_via_hermes` in
every test — nothing here spawns the real binary or sends a real message.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from api.services.agent_worker.session_store import (
    STATUS_CLAIMED,
    SessionStore,
)
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker
from api.services.hermes_notify import HERMES_CHANNEL, HermesDelivery

pytestmark = pytest.mark.unit


class _FakeApi:
    """Just enough of /api/tasks for the claim path."""

    def __init__(self, tasks):
        self.tasks = {t["id"]: t for t in tasks}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/claim-agent"):
            task = self.tasks[path.split("/")[-2]]
            tags = list(task["tags"])
            consumed = "agent" in tags
            if consumed:
                tags[tags.index("agent")] = "agent-running"
            else:
                tags.append("agent-running")
            task["tags"] = tags
            return httpx.Response(200, json={"claimed": True, "consumed_queue_tag": consumed})
        if request.method == "POST" and path.endswith("/remove-tag"):
            task = self.tasks[path.split("/")[-2]]
            tag = request.url.params.get("tag")
            task["tags"] = [t for t in task["tags"] if t != tag]
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and "/api/tasks/" in path:
            task = self.tasks.get(path.split("/")[-1])
            if task is None:
                return httpx.Response(404)
            return httpx.Response(200, json=task)
        return httpx.Response(404)


@pytest.fixture
def sent_hermes(monkeypatch):
    """Capture every `hermes send` call and report a successful delivery."""
    calls: list[str] = []

    def _send(text, **kwargs):
        calls.append(text)
        return HermesDelivery(chat_id="5550001111", message_id=str(590 + len(calls)))

    import api.services.agent_worker.worker as worker_mod
    monkeypatch.setattr(worker_mod.hermes_notify, "send_via_hermes", _send)
    return calls


@pytest.fixture
def failing_hermes(monkeypatch):
    """Stand in for a missing or failing `hermes` binary."""
    calls: list[str] = []

    def _send(text, **kwargs):
        calls.append(text)
        return None

    import api.services.agent_worker.worker as worker_mod
    monkeypatch.setattr(worker_mod.hermes_notify, "send_via_hermes", _send)
    return calls


def _make_worker(tmp_path: Path, tasks=()):
    api = _FakeApi(list(tasks))
    client = httpx.Client(transport=httpx.MockTransport(api.handler), base_url="http://api")
    telegram: list[tuple[str, str | None]] = []
    telegram_ids: list[str] = []

    def _send(text, chat_id=None, bot=None):
        telegram.append((text, bot))
        return True

    def _send_with_id(text, chat_id=None, bot=None):
        telegram_ids.append(text)
        return [1000 + len(telegram_ids)]

    worker = Worker(
        api_base="http://api",
        session_store=SessionStore(db_path=tmp_path / "sessions.db"),
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        poll_seconds=0.01,
        telegram_send=_send,
        telegram_send_with_id=_send_with_id,
        http_client=client,
    )
    worker._telegram_calls = telegram  # type: ignore[attr-defined]
    worker._telegram_id_calls = telegram_ids  # type: ignore[attr-defined]
    worker._fake_api = api  # type: ignore[attr-defined]
    return worker


def _task(task_id: str, assignee: str) -> dict:
    return {
        "id": task_id,
        "description": "Summarize the weekly report",
        "status": "todo",
        "tags": [assignee],
        "fields": {},
    }


# ---------------------------------------------------------------------------
# Claim records the channel
# ---------------------------------------------------------------------------

def test_claiming_a_hermes_assigned_task_records_the_hermes_channel(tmp_path):
    worker = _make_worker(tmp_path, [_task("t-hermes", "hermes")])
    assert worker._claim("t-hermes") is True
    session = worker.session_store.get("t-hermes")
    assert session.status == STATUS_CLAIMED
    assert session.bot == HERMES_CHANNEL


def test_claiming_a_claude_assigned_task_leaves_the_channel_unset(tmp_path):
    worker = _make_worker(tmp_path, [_task("t-claude", "claude")])
    assert worker._claim("t-claude") is True
    assert worker.session_store.get("t-claude").bot is None


def test_reassigning_a_task_away_from_hermes_clears_the_stale_channel(tmp_path):
    """A reassignment rearm re-derives the channel from the *current*
    assignee rather than carrying over a prior claim's — otherwise a task
    once assigned to Hermes and later reassigned elsewhere would keep
    reporting through a channel its new engine has nothing to do with."""
    worker = _make_worker(tmp_path, [_task("t-1", "claude")])
    worker.session_store.create(task_id="t-1", status="completed", bot=HERMES_CHANNEL)
    assert worker._claim("t-1") is True
    assert worker.session_store.get("t-1").bot is None


def test_reassigning_a_task_into_hermes_sets_the_channel_fresh(tmp_path):
    worker = _make_worker(tmp_path, [_task("t-1", "hermes")])
    worker.session_store.create(task_id="t-1", status="completed", bot=None)
    assert worker._claim("t-1") is True
    assert worker.session_store.get("t-1").bot == HERMES_CHANNEL


# ---------------------------------------------------------------------------
# One-way notices route to Hermes
# ---------------------------------------------------------------------------

def _hermes_session(worker, task_id="t-1"):
    worker.session_store.create(task_id=task_id, status=STATUS_CLAIMED, routing="hermes")
    worker.session_store.set_bot_if_unset(task_id, HERMES_CHANNEL)
    return worker.session_store.get(task_id)


def test_progress_notice_goes_to_hermes_not_the_primary_bot(tmp_path, sent_hermes):
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._send_session_message(session, "halfway through the report")
    assert any("halfway through the report" in text for text in sent_hermes)
    assert worker._telegram_calls == []
    assert worker._telegram_id_calls == []


def test_terminal_notice_goes_to_hermes_not_the_primary_bot(tmp_path, sent_hermes):
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._notify_terminal(session, "task finished", label="Summarize the weekly report")
    assert any("task finished" in text for text in sent_hermes)
    assert worker._telegram_calls == []


def test_a_session_without_the_hermes_channel_still_uses_telegram(tmp_path, sent_hermes):
    worker = _make_worker(tmp_path)
    worker.session_store.create(task_id="t-2", status=STATUS_CLAIMED, routing="local")
    session = worker.session_store.get("t-2")
    worker._notify_terminal(session, "task finished", label="Summarize the weekly report")
    assert sent_hermes == []
    assert worker._telegram_id_calls


# ---------------------------------------------------------------------------
# Degradation when the binary is missing or failing
# ---------------------------------------------------------------------------

def test_failing_hermes_binary_falls_back_to_the_primary_bot(tmp_path, failing_hermes, caplog):
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    with caplog.at_level("WARNING"):
        worker._notify_terminal(session, "task finished", label="Summarize the weekly report")
    assert failing_hermes, "the Hermes channel is attempted before the fallback"
    bodies = [text for text, _ in worker._telegram_calls]
    assert any("task finished" in text for text in bodies), "the task still reports somewhere"
    assert any("hermes" in record.getMessage().lower() for record in caplog.records)


def test_failing_hermes_binary_does_not_raise_out_of_the_worker(tmp_path, monkeypatch):
    import api.services.agent_worker.worker as worker_mod

    def _explode(text, **kwargs):
        raise OSError("hermes exploded")

    monkeypatch.setattr(worker_mod.hermes_notify, "send_via_hermes", _explode)
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._notify_terminal(session, "task finished", label="Summarize the weekly report")
    bodies = [text for text, _ in worker._telegram_calls]
    assert any("task finished" in text for text in bodies)


# ---------------------------------------------------------------------------
# Questions stay answerable
# ---------------------------------------------------------------------------

def test_a_question_stays_on_the_primary_bot_while_hermes_questions_are_off(
    tmp_path, sent_hermes, monkeypatch,
):
    from config.settings import settings
    monkeypatch.setattr(settings, "hermes_task_questions", False, raising=False)
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._mark_blocked(session, {"id": "t-1", "description": "Weekly report"}, "which week?")
    assert sent_hermes == []
    assert any("which week?" in text for text in worker._telegram_id_calls)


def test_a_question_delivered_to_hermes_is_anchored_by_its_message_identity(
    tmp_path, sent_hermes, monkeypatch,
):
    from api.services.hermes_question_thread_store import get_question_thread_store
    from config.settings import settings

    monkeypatch.setattr(settings, "hermes_task_questions", True, raising=False)

    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._mark_blocked(session, {"id": "t-1", "description": "Weekly report"}, "which week?")

    assert any("which week?" in text for text in sent_hermes)
    open_question = worker.session_store.get_open_question_by_session_id(session.session_id)
    assert open_question is not None
    assert open_question["bot"] == HERMES_CHANNEL
    assert get_question_thread_store().lookup("5550001111", "591") == open_question["id"]


def test_a_question_falls_back_to_the_primary_bot_when_hermes_delivery_fails(
    tmp_path, failing_hermes, monkeypatch,
):
    from config.settings import settings
    monkeypatch.setattr(settings, "hermes_task_questions", True, raising=False)
    worker = _make_worker(tmp_path)
    session = _hermes_session(worker)
    worker._mark_blocked(session, {"id": "t-1", "description": "Weekly report"}, "which week?")
    assert failing_hermes
    assert any("which week?" in text for text in worker._telegram_id_calls)


# ---------------------------------------------------------------------------
# Inheritance is unaffected
# ---------------------------------------------------------------------------

def test_a_spawned_child_inherits_its_callers_channel(tmp_path):
    worker = _make_worker(tmp_path)
    parent = _hermes_session(worker, task_id="t-parent")
    child = worker.session_store.create(
        task_id="t-child",
        status=STATUS_CLAIMED,
        parent_session_id=parent.session_id,
        root_session_id=parent.root_session_id,
        bot=parent.bot,
    )
    assert child.bot == HERMES_CHANNEL
