"""Synthetic tests for handing an addressed Pebble capture to a configured command."""
import json
import stat
import sys

import pytest

from api.services.pebble_capture import CaptureLedger, PebbleCaptureConsumer
from api.services.pebble_capture_watcher import PebbleCaptureWatcher
from api.services.pebble_handoff import (
    PebbleHandoff,
    PebbleHandoffError,
    configured_handoff,
    handoff_message,
    run_handoff,
)
from api.services.scheduler_store import SchedulerStore
from api.services.task_manager import TaskManager
from tests.test_pebble_capture import _TASK_EVIDENCE, _Classifier, _frame, _payload

pytestmark = pytest.mark.unit

PHRASE = "Chief of Staff"


@pytest.mark.parametrize(("text", "message"), [
    ("Chief of Staff, book the synthetic room.", "book the synthetic room."),
    ("chief of staff book the synthetic room", "book the synthetic room"),
    ("Chief of staff: book the synthetic room.", "book the synthetic room."),
    ("Chief-of-staff — book the synthetic room.", "book the synthetic room."),
    ("CHIEF OF STAFF. Book the synthetic room.", "Book the synthetic room."),
    ("Um, chief of staff, book the synthetic room.", "book the synthetic room."),
    ("Okay so, Chief of Staff, book the synthetic room.", "book the synthetic room."),
    ("Hey chief of staff... book the synthetic room.", "book the synthetic room."),
    ('"Chief of Staff, book the synthetic room."', 'book the synthetic room."'),
    ("Chief of Staff, line one.\nLine two.", "line one.\nLine two."),
])
def test_phrase_at_the_start_is_a_handoff(text, message):
    assert handoff_message(text, PHRASE) == message


@pytest.mark.parametrize("text", [
    "I told the chief of staff to book the synthetic room.",
    "Add a task for the Chief of Staff to book the synthetic room.",
    "Chief of Staff's calendar is full this week.",
    "Chiefs of staff book rooms.",
    "Chief of Staffing plans are due.",
    "Chief of Staff.",
    "Book the synthetic room.",
    "Chief staff, book the synthetic room.",
])
def test_mentions_possessives_and_empty_messages_are_ordinary_captures(text):
    assert handoff_message(text, PHRASE) is None


def test_phrase_is_operator_configured_not_built_in():
    assert handoff_message("Assistant, water the synthetic plants.", "Assistant") == "water the synthetic plants."
    assert handoff_message("Chief of Staff, water the plants.", "Assistant") is None


def test_handoff_is_disabled_unless_phrase_and_command_are_both_set():
    assert configured_handoff("", "/bin/true") is None
    assert configured_handoff(PHRASE, "  ") is None
    assert configured_handoff("...", "/bin/true") is None
    assert configured_handoff(f" {PHRASE} ", "/bin/true") == PebbleHandoff(PHRASE, "/bin/true")


def _recording_command(tmp_path, exit_code=0):
    """A real executable that records its stdin and hand-off environment."""
    out = tmp_path / "received.jsonl"
    script = tmp_path / "receive"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(out)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'stdin': sys.stdin.read(),\n"
        "        'at': os.environ.get('LIFEOS_HANDOFF_RECORDED_AT'),\n"
        "        'key': os.environ.get('LIFEOS_HANDOFF_KEY')}) + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), out


def _received(out):
    return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []


def test_run_handoff_pipes_the_message_and_capture_metadata(tmp_path):
    command, out = _recording_command(tmp_path)
    run_handoff(command, "book the synthetic room", recorded_at="2030-01-01T10:00:00Z", key="k1")
    assert _received(out) == [{"stdin": "book the synthetic room", "at": "2030-01-01T10:00:00Z", "key": "k1"}]


def test_run_handoff_raises_when_the_command_fails_or_is_missing(tmp_path):
    command, _ = _recording_command(tmp_path, exit_code=3)
    with pytest.raises(PebbleHandoffError, match="exited 3"):
        run_handoff(command, "x", recorded_at="t", key="k")
    with pytest.raises(PebbleHandoffError, match="failed to run"):
        run_handoff(str(tmp_path / "missing"), "x", recorded_at="t", key="k")


class _FakeHandoff:
    """Matches like the real hand-off; records sends, or fails while ``fail`` is set."""

    def __init__(self, phrase=PHRASE):
        self.phrase = phrase
        self.sent = []
        self.fail = False

    def message(self, text):
        return handoff_message(text, self.phrase)

    def send(self, message, *, recorded_at, key):
        if self.fail:
            raise PebbleHandoffError("synthetic hand-off failure")
        self.sent.append((message, recorded_at, key))


@pytest.fixture
def stores(tmp_path):
    vault = tmp_path / "vault"
    return (
        CaptureLedger(tmp_path / "ledger.sqlite"),
        TaskManager(vault_path=vault, index_path=tmp_path / "tasks.json"),
        SchedulerStore(vault_path=vault, index_path=tmp_path / "schedules.json"),
    )


def _addressed():
    return {**_payload(), "final_text": "Chief of Staff, add a task to handle the synthetic review."}


def _consumer(stores, handoff, *, apply=True):
    ledger, tasks, schedules = stores
    classifier = _Classifier([{"kind": "task", "index": 0, "title": "Synthetic review", **_TASK_EVIDENCE}])
    return PebbleCaptureConsumer(ledger, tasks, schedules, classifier, apply=apply, handoff=handoff), classifier


@pytest.mark.asyncio
async def test_addressed_capture_is_sent_once_and_files_no_task(stores):
    handoff = _FakeHandoff()
    consumer, classifier = _consumer(stores, handoff)
    assert await consumer.process(_addressed()) == "complete"
    assert await consumer.process(_addressed()) == "complete"
    assert [m for m, _, _ in handoff.sent] == ["add a task to handle the synthetic review."]
    assert handoff.sent[0][1] == "2030-01-01T10:00:00Z"
    assert handoff.sent[0][2].startswith("pebble:")
    assert classifier.calls == 0
    assert stores[1].list_tasks() == []
    assert stores[2].list_all() == []


@pytest.mark.asyncio
async def test_ordinary_capture_is_classified_and_filed_as_before(stores):
    handoff = _FakeHandoff()
    consumer, classifier = _consumer(stores, handoff)
    assert await consumer.process(_payload()) == "complete"
    assert handoff.sent == []
    assert classifier.calls == 1
    assert len(stores[1].list_tasks()) == 1


@pytest.mark.asyncio
async def test_without_a_configured_handoff_the_phrase_is_ordinary_text(stores):
    consumer, classifier = _consumer(stores, None)
    assert await consumer.process(_addressed()) == "complete"
    assert classifier.calls == 1
    assert len(stores[1].list_tasks()) == 1


@pytest.mark.asyncio
async def test_failed_handoff_stays_pending_and_is_retried_never_filed(stores):
    handoff = _FakeHandoff()
    handoff.fail = True
    consumer, classifier = _consumer(stores, handoff)
    with pytest.raises(PebbleHandoffError):
        await consumer.process(_addressed())
    with pytest.raises(PebbleHandoffError):
        await consumer.process(_addressed())
    assert stores[1].list_tasks() == []
    assert classifier.calls == 0
    handoff.fail = False
    assert await consumer.process(_addressed()) == "complete"
    assert len(handoff.sent) == 1
    assert stores[1].list_tasks() == []


@pytest.mark.asyncio
async def test_dry_run_plans_the_handoff_without_sending(stores):
    handoff = _FakeHandoff()
    consumer, _ = _consumer(stores, handoff, apply=False)
    assert await consumer.process(_addressed()) == "dry_run"
    assert handoff.sent == []
    assert [a.kind for a in stores[0].load_plan(_identity())] == ["handoff"]


def _identity():
    from api.services.pebble_capture import ready_result
    return ready_result(_addressed())[0]


@pytest.mark.asyncio
async def test_crash_after_send_is_never_resent(stores, monkeypatch):
    ledger = stores[0]
    handoff = _FakeHandoff()
    consumer, _ = _consumer(stores, handoff)
    original = ledger.record_effect

    def crash(*args):
        raise RuntimeError("synthetic crash after send")

    monkeypatch.setattr(ledger, "record_effect", crash)
    with pytest.raises(RuntimeError, match="after send"):
        await consumer.process(_addressed())
    monkeypatch.setattr(ledger, "record_effect", original)
    with ledger._connect() as db:
        db.execute("UPDATE pebble_effects SET claimed_at=0")
    assert await consumer.process(_addressed()) == "complete"
    assert len(handoff.sent) == 1


@pytest.mark.asyncio
async def test_stale_claim_with_unknown_send_outcome_is_held_not_resent(stores):
    ledger = stores[0]
    handoff = _FakeHandoff()
    consumer, _ = _consumer(stores, handoff, apply=False)
    await consumer.process(_addressed())
    action = ledger.load_plan(_identity())[0]
    assert ledger.claim_effect(_identity(), action) is not None
    with ledger._connect() as db:
        db.execute("UPDATE pebble_effects SET claimed_at=0")
    consumer.apply = True
    assert await consumer.process(_addressed()) == "ambiguous_outcome"
    assert handoff.sent == []


def test_watcher_hands_an_addressed_capture_to_a_real_command(tmp_path, stores):
    command, out = _recording_command(tmp_path)
    ledger, tasks, schedules = stores
    classifier = _Classifier([{"kind": "task", "index": 0, "title": "Synthetic review", **_TASK_EVIDENCE}])
    consumer = PebbleCaptureConsumer(
        ledger, tasks, schedules, classifier, apply=True,
        handoff=configured_handoff(PHRASE, command),
    )
    archive = tmp_path / "vault" / "LifeOS" / "Log" / "Pebble"
    archive.mkdir(parents=True)
    path = archive / "2030-01-01-raw.md"
    ordinary = {**_payload(), "capture_id": "capture-2"}
    path.write_text(_frame(_addressed()) + "\n" + _frame(ordinary) + "\n")
    watcher = PebbleCaptureWatcher(archive, consumer)
    watcher.process_file(path)
    watcher.process_file(path)
    received = _received(out)
    assert [r["stdin"] for r in received] == ["add a task to handle the synthetic review."]
    assert received[0]["at"] == "2030-01-01T10:00:00Z"
    assert [t.description for t in tasks.list_tasks()] == ["Synthetic review"]
    assert classifier.calls == 1
