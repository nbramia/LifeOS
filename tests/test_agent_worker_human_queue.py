"""Tests for the agent worker's Human-queue done_when poll tick (#852).

The worker talks to the human queue over the HTTP API (never the in-process
TaskManager), so these tests fake that surface with httpx.MockTransport,
matching the pattern in test_agent_worker_clarifications.py.
"""
from __future__ import annotations

import json

import httpx
import pytest

from api.services.agent_worker.session_store import SessionStore
from api.services.agent_worker.spend_tracker import SpendTracker
from api.services.agent_worker.transcript_store import TranscriptStore
from api.services.agent_worker.worker import Worker, _resolve_json_pointer

pytestmark = pytest.mark.unit


class FakeHumanQueueApi:
    def __init__(self, cards, endpoint_responses=None):
        self.cards = cards
        self.endpoint_responses = endpoint_responses or {}
        self.resolved: list[tuple[str, str]] = []  # (id, note)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/tasks/human-queue":
            return httpx.Response(200, json={"cards": self.cards, "total": len(self.cards)})
        if request.method == "PUT" and path.endswith("/resolve") and "/human-queue/" in path:
            card_id = path.split("/")[-2]
            note = json.loads(request.content or b"{}").get("note", "")
            self.resolved.append((card_id, note))
            return httpx.Response(200, json={"id": card_id, "status": "done"})
        if path in self.endpoint_responses:
            body, status = self.endpoint_responses[path]
            return httpx.Response(status, json=body)
        return httpx.Response(404)


def _make_worker(tmp_path, api):
    transport = httpx.MockTransport(api.handler)
    client = httpx.Client(transport=transport, base_url="http://api")
    store = SessionStore(db_path=tmp_path / "sessions.db")
    return Worker(
        api_base="http://api",
        session_store=store,
        transcript_store=TranscriptStore(transcripts_dir=tmp_path / "transcripts"),
        spend_tracker=SpendTracker(db_path=tmp_path / "sessions.db", daily_cap_dollars=100.0),
        poll_seconds=0.01,
        http_client=client,
    )


def _card(id="c1", done_when=None):
    return {
        "id": id, "title": "X", "key": None, "notes": None, "age_hours": 1.0,
        "source_host": None, "source_cwd": None, "source_session": None,
        "done_when": done_when,
    }


class TestJsonPointer:
    def test_empty_pointer_returns_whole_doc(self):
        assert _resolve_json_pointer({"a": 1}, "") == {"a": 1}
        assert _resolve_json_pointer({"a": 1}, "/") == {"a": 1}

    def test_simple_pointer(self):
        assert _resolve_json_pointer({"status": "ok"}, "/status") == "ok"

    def test_nested_pointer(self):
        assert _resolve_json_pointer({"a": {"b": "c"}}, "/a/b") == "c"

    def test_missing_key_raises(self):
        with pytest.raises(KeyError):
            _resolve_json_pointer({"a": 1}, "/missing")


class TestEndpointDoneWhen:
    def test_passing_check_resolves_with_note(self, tmp_path):
        api = FakeHumanQueueApi(
            cards=[_card(done_when={"type": "endpoint", "path": "/api/example/status", "pointer": "/status", "equals": "ok"})],
            endpoint_responses={"/api/example/status": ({"status": "ok"}, 200)},
        )
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert len(api.resolved) == 1
        assert api.resolved[0][0] == "c1"
        # Tightened: the resolution note must name the exact path+pointer
        # checked and the value it matched, not just contain the word
        # "endpoint" (which every endpoint-type note would trivially do).
        assert api.resolved[0][1] == "Auto-resolved: endpoint /api/example/status/status == 'ok'"

    def test_failing_check_leaves_card_untouched(self, tmp_path):
        api = FakeHumanQueueApi(
            cards=[_card(done_when={"type": "endpoint", "path": "/api/example/status", "pointer": "/status", "equals": "ok"})],
            endpoint_responses={"/api/example/status": ({"status": "expired"}, 200)},
        )
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert api.resolved == []

    def test_erroring_endpoint_leaves_card_untouched(self, tmp_path):
        api = FakeHumanQueueApi(
            cards=[_card(done_when={"type": "endpoint", "path": "/api/example/status", "pointer": "/status", "equals": "ok"})],
            endpoint_responses={},  # 404 on the check target
        )
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert api.resolved == []


class TestFileExistsDoneWhen:
    def test_existing_file_resolves(self, tmp_path):
        flag = tmp_path / "flag"
        flag.write_text("x")
        api = FakeHumanQueueApi(cards=[_card(done_when={"type": "file_exists", "path": str(flag)})])
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert len(api.resolved) == 1
        assert "file_exists" in api.resolved[0][1]

    def test_missing_file_leaves_card_untouched(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        api = FakeHumanQueueApi(cards=[_card(done_when={"type": "file_exists", "path": str(missing)})])
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert api.resolved == []


class TestNoDoneWhen:
    def test_card_without_done_when_is_skipped(self, tmp_path):
        api = FakeHumanQueueApi(cards=[_card(done_when=None)])
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert api.resolved == []


class TestPollThrottle:
    def test_second_call_within_interval_is_a_no_op(self, tmp_path, monkeypatch):
        from config.settings import settings
        # A non-default value (production default is 300) — proves
        # _process_human_queue actually reads settings.human_queue_poll_
        # seconds rather than a hardcoded 300 that would happen to pass
        # the old version of this test too.
        monkeypatch.setattr(settings, "human_queue_poll_seconds", 9999.0)
        flag = tmp_path / "flag"
        flag.write_text("x")
        api = FakeHumanQueueApi(cards=[_card(id="c1", done_when={"type": "file_exists", "path": str(flag)})])
        w = _make_worker(tmp_path, api)

        w._process_human_queue()
        assert len(api.resolved) == 1

        # A second card would resolve too, but the throttle should skip the
        # whole check within the poll interval.
        api.cards = [_card(id="c2", done_when={"type": "file_exists", "path": str(flag)})]
        w._process_human_queue()
        assert len(api.resolved) == 1  # unchanged — still just c1

    def test_call_after_interval_elapses_runs_again(self, tmp_path, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "human_queue_poll_seconds", 9999.0)
        flag = tmp_path / "flag"
        flag.write_text("x")
        api = FakeHumanQueueApi(cards=[_card(id="c1", done_when={"type": "file_exists", "path": str(flag)})])
        w = _make_worker(tmp_path, api)
        w._process_human_queue()
        assert len(api.resolved) == 1

        # Force the throttle clock back so the interval has "elapsed".
        w._last_human_queue_check -= 10000
        api.cards = [_card(id="c2", done_when={"type": "file_exists", "path": str(flag)})]
        w._process_human_queue()
        assert len(api.resolved) == 2

    def test_zero_interval_never_throttles(self, tmp_path, monkeypatch):
        """poll_seconds=0.0 means every call is past the interval — two
        back-to-back calls must both actually list/check cards, not just
        the first."""
        from config.settings import settings
        monkeypatch.setattr(settings, "human_queue_poll_seconds", 0.0)
        flag = tmp_path / "flag"
        flag.write_text("x")
        api = FakeHumanQueueApi(cards=[_card(id="c1", done_when={"type": "file_exists", "path": str(flag)})])
        w = _make_worker(tmp_path, api)

        w._process_human_queue()
        assert len(api.resolved) == 1

        api.cards = [_card(id="c2", done_when={"type": "file_exists", "path": str(flag)})]
        w._process_human_queue()
        assert len(api.resolved) == 2


class TestTickInvokesHumanQueue:
    def test_tick_calls_process_human_queue(self, tmp_path, monkeypatch):
        """Worker.tick() must actually invoke _process_human_queue — the
        throttle tests above call _process_human_queue directly and would
        pass even if tick() never wired it in. Every other step tick()
        takes is stubbed to a no-op so this test isolates just that wiring,
        not the rest of the poll cycle."""
        api = FakeHumanQueueApi(cards=[])
        w = _make_worker(tmp_path, api)
        for step in (
            "_wake_sleeping_sessions",
            "_poll_managed_sessions",
            "_resume_yielded_for_children",
            "_dispatch_spawned_sessions",
            "_process_clarification_answers",
            "_timeout_stale_clarifications",
        ):
            monkeypatch.setattr(w, step, lambda: None)
        monkeypatch.setattr(w, "_list_agent_tasks", lambda: [])

        called = []
        monkeypatch.setattr(w, "_process_human_queue", lambda: called.append(True))

        result = w.tick()

        assert called == [True]
        assert result == 0


class TestMultipleCards:
    def test_one_log_line_per_resolution(self, tmp_path, caplog):
        flag = tmp_path / "flag"
        flag.write_text("x")
        api = FakeHumanQueueApi(cards=[
            _card(id="c1", done_when={"type": "file_exists", "path": str(flag)}),
            _card(id="c2", done_when={"type": "file_exists", "path": str(tmp_path / "missing")}),
            _card(id="c3", done_when={"type": "file_exists", "path": str(flag)}),
        ])
        w = _make_worker(tmp_path, api)
        with caplog.at_level("INFO", logger="api.services.agent_worker.worker"):
            w._process_human_queue()
        assert {c_id for c_id, _ in api.resolved} == {"c1", "c3"}
        resolution_lines = [r for r in caplog.records if "human-queue: resolved" in r.message]
        assert len(resolution_lines) == 2
