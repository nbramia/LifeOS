"""Synthetic tests for Pebble frame authentication (MAC line after the frame)."""
import hashlib
import json
import logging

import pytest

from api.services.pebble_capture import (
    FRAME_BAD_SIGNATURE,
    FRAME_SIGNED,
    FRAME_UNSIGNED,
    FRAME_UNVERIFIED,
    CaptureLedger,
    PebbleCaptureConsumer,
    PebbleCaptureError,
    frame_mac,
    load_frame_key,
    parse_framed_blocks,
    parse_frames,
    ready_result,
)
from api.services.pebble_capture_watcher import PebbleCaptureWatcher
from api.services.scheduler_store import SchedulerStore
from api.services.task_manager import TaskManager
from tests.test_pebble_capture import _TASK_EVIDENCE, _Classifier, _frame, _payload

pytestmark = pytest.mark.unit

KEY = b"synthetic-frame-key-0123456789abcdef"
OTHER_KEY = b"another-synthetic-key-0123456789ab"
WATCHER_LOG = "api.services.pebble_capture_watcher"


def _signed(payload, key=KEY):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _frame(payload) + f"\n<!-- pebble-capture-v1-mac hmac-sha256={frame_mac(key, body)} -->"


def _forged(payload):
    """What any vault writer can produce: a well-formed frame with a freshly
    computed sha256, and a MAC line made without the key."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    fake_mac = hashlib.sha256(b"pebble-capture-v1\0" + body).hexdigest()
    return _frame(payload) + f"\n<!-- pebble-capture-v1-mac hmac-sha256={fake_mac} -->"


def _forged_payload():
    payload = _payload()
    payload["capture_id"] = "synthetic-forged-1"
    payload["final_text"] = "Add a task to handle the synthetic forged review."
    return payload


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "vault" / "LifeOS" / "Log" / "Pebble"
    path.mkdir(parents=True)
    return path


def _consumer(tmp_path, *, apply=True, title="Synthetic review"):
    vault = tmp_path / "vault"
    tasks = TaskManager(vault_path=vault, index_path=tmp_path / "tasks.json")
    classifier = _Classifier([{"kind": "task", "index": 0, "title": title, **_TASK_EVIDENCE}])
    consumer = PebbleCaptureConsumer(
        CaptureLedger(tmp_path / "ledger.sqlite"), tasks,
        SchedulerStore(vault_path=vault, index_path=tmp_path / "schedules.json"),
        classifier, apply=apply,
    )
    return consumer, classifier, tasks


def test_mac_matches_the_producer_known_answer_vector():
    """The same vector is pinned in Pebble's tests/test_vault.py."""
    record = {"schema_version": 1, "kind": "capture", "capture_id": "synthetic-capture-001", "revision": 0}
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    assert frame_mac(KEY, body) == "de43d097df698f5114b4ccfd98f6373e41e4209e9bad596da7b346300a5e0aae"


def test_parser_reports_each_signature_state():
    payload = _payload()
    signed = _signed(payload)
    assert [(f.payload, f.signature) for f in parse_frames(signed, KEY)] == [(payload, FRAME_SIGNED)]
    assert [f.signature for f in parse_frames(_frame(payload), KEY)] == [FRAME_UNSIGNED]
    assert [f.signature for f in parse_frames(_signed(payload, OTHER_KEY), KEY)] == [FRAME_BAD_SIGNATURE]
    assert [f.signature for f in parse_frames(signed, None)] == [FRAME_UNVERIFIED]
    # The MAC must immediately follow its own end marker.
    detached = signed.replace("\n<!-- pebble-capture-v1-mac", "\n\n<!-- pebble-capture-v1-mac")
    assert [f.signature for f in parse_frames(detached, KEY)] == [FRAME_UNSIGNED]
    # Signed frames stay readable by the payload-only reader.
    assert parse_framed_blocks(signed) == [payload]


def test_edited_payload_with_recomputed_digest_fails_the_original_mac():
    payload = _payload()
    signed = _signed(payload)
    mac_line = signed.rsplit("\n", 1)[1]
    edited = {**payload, "final_text": "Add a task to handle a different synthetic review."}
    tampered = _frame(edited) + "\n" + mac_line
    (record,) = parse_frames(tampered, KEY)
    assert record.payload == edited
    assert record.signature == FRAME_BAD_SIGNATURE


@pytest.mark.parametrize("document", [_frame(_forged_payload()), _forged(_forged_payload())])
def test_forged_frame_is_refused_when_signatures_are_required(tmp_path, archive, caplog, document):
    consumer, classifier, tasks = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(document)
    watcher = PebbleCaptureWatcher(archive, consumer, frame_key=KEY, require_signature=True)
    with caplog.at_level(logging.WARNING, logger=WATCHER_LOG):
        watcher.process_file(path)
        watcher.process_file(path)
    assert classifier.calls == 0
    assert tasks.list_tasks() == []
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refusals) == 1
    assert "synthetic" not in refusals[0].getMessage()


def test_required_signature_refuses_everything_unadmitted_without_a_key(tmp_path, archive):
    consumer, classifier, tasks = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_signed(_payload()))
    PebbleCaptureWatcher(archive, consumer, frame_key=None, require_signature=True).process_file(path)
    assert classifier.calls == 0
    assert tasks.list_tasks() == []


def test_signed_frame_is_filed_when_signatures_are_required(tmp_path, archive):
    consumer, classifier, tasks = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_signed(_payload()) + "\n\n## readable view\n")
    PebbleCaptureWatcher(archive, consumer, frame_key=KEY, require_signature=True).process_file(path)
    assert classifier.calls == 1
    assert [task.description for task in tasks.list_tasks()] == ["Synthetic review"]


def test_unsigned_frame_is_filed_with_one_warning_while_signatures_are_optional(tmp_path, archive, caplog):
    consumer, classifier, tasks = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_frame(_payload()))
    watcher = PebbleCaptureWatcher(archive, consumer, frame_key=KEY, require_signature=False)
    with caplog.at_level(logging.WARNING, logger=WATCHER_LOG):
        watcher.process_file(path)
        watcher.process_file(path)
    assert classifier.calls == 1
    assert [task.description for task in tasks.list_tasks()] == ["Synthetic review"]
    warnings = [r for r in caplog.records if "without a verified signature" in r.getMessage()]
    assert len(warnings) == 1


def test_completed_unsigned_history_is_silent_and_not_rerun(tmp_path, archive, caplog):
    consumer, classifier, tasks = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_frame(_payload()))
    PebbleCaptureWatcher(archive, consumer).process_file(path)
    assert classifier.calls == 1
    caplog.clear()

    for required in (False, True):
        watcher = PebbleCaptureWatcher(archive, consumer, frame_key=KEY, require_signature=required)
        with caplog.at_level(logging.WARNING, logger=WATCHER_LOG):
            watcher.process_file(path)
    assert classifier.calls == 1
    assert len(tasks.list_tasks()) == 1
    assert not [r for r in caplog.records if r.name == WATCHER_LOG]


def test_unsigned_capture_planned_before_signatures_were_required_still_finishes(tmp_path, archive):
    """A plan recorded while signatures were optional is not stranded, and a
    forged frame beside it is still refused."""
    planner, classifier, _ = _consumer(tmp_path, apply=False)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_frame(_payload()))
    PebbleCaptureWatcher(archive, planner, frame_key=KEY).process_file(path)
    assert classifier.calls == 1

    consumer, later_classifier, tasks = _consumer(tmp_path, apply=True)
    path.write_text(_frame(_payload()) + "\n" + _frame(_forged_payload()))
    PebbleCaptureWatcher(archive, consumer, frame_key=KEY, require_signature=True).process_file(path)
    assert later_classifier.calls == 0
    assert [task.description for task in tasks.list_tasks()] == ["Synthetic review"]
    assert consumer.ledger.completed(ready_result(_payload())[0])


def test_admission_needs_the_exact_planned_payload(tmp_path, archive):
    planner, _, _ = _consumer(tmp_path, apply=False)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_frame(_payload()))
    PebbleCaptureWatcher(archive, planner, frame_key=KEY).process_file(path)
    edited = {**_payload(), "final_text": "Add a task to handle the synthetic edited review."}
    assert planner.ledger.admitted(_payload())
    assert not planner.ledger.admitted(edited)


def _key_file(tmp_path, content=b"0" * 64 + b"\n", mode=0o600):
    path = tmp_path / "frame.key"
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_frame_key_loading(tmp_path):
    assert load_frame_key("") is None
    assert load_frame_key(str(_key_file(tmp_path))) == b"0" * 64
    with pytest.raises(PebbleCaptureError, match="private"):
        load_frame_key(str(_key_file(tmp_path, mode=0o644)))
    with pytest.raises(PebbleCaptureError, match="too short"):
        load_frame_key(str(_key_file(tmp_path, content=b"short")))
    link = tmp_path / "linked.key"
    link.symlink_to(_key_file(tmp_path))
    with pytest.raises(PebbleCaptureError, match="regular file"):
        load_frame_key(str(link))
    with pytest.raises(OSError):
        load_frame_key(str(tmp_path / "absent.key"))


def test_report_counts_what_requiring_signatures_would_refuse(tmp_path, archive):
    from scripts.pebble_frame_report import report

    consumer, _, _ = _consumer(tmp_path)
    path = archive / "2030-01-01-raw.md"
    path.write_text(_frame(_payload()))
    PebbleCaptureWatcher(archive, consumer).process_file(path)
    signed = {**_payload(), "capture_id": "synthetic-signed-1"}
    path.write_text("\n".join([_frame(_payload()), _signed(signed), _forged(_forged_payload())]))
    counts = report(archive, tmp_path / "ledger.sqlite", KEY)
    assert counts == {
        "key_loaded": True,
        "bad_signature.new": 1,
        "signed.new": 1,
        "unsigned.complete": 1,
        "would_refuse": 1,
    }
