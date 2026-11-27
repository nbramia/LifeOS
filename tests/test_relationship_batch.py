"""Tests for RelationshipStore.batch() and its use by relationship discovery."""
import math
import sqlite3
import threading
from unittest.mock import patch

import pytest

from api.services import relationship as relationship_module
from api.services import relationship_discovery
from api.services.relationship import Relationship, RelationshipStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return RelationshipStore(db_path=str(tmp_path / "crm.db"))


class _Counter:
    """Counts connections opened and COMMIT statements executed through the store."""

    def __init__(self):
        self.connections = 0
        self.commits = 0


@pytest.fixture
def counter():
    real_connect = sqlite3.connect
    c = _Counter()

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        c.connections += 1

        def trace(statement):
            if statement.strip().upper().startswith("COMMIT"):
                c.commits += 1

        conn.set_trace_callback(trace)
        return conn

    with patch.object(relationship_module.sqlite3, "connect", counting_connect):
        yield c


def _rel(i: int) -> Relationship:
    return Relationship(person_a_id=f"a{i:05d}", person_b_id=f"b{i:05d}")


def _write_n(store: RelationshipStore, n: int) -> None:
    for i in range(n):
        rel = store.add(_rel(i))
        rel.shared_events_count = 1
        store.update(rel)


def test_batch_commits_in_bounded_chunks(store, counter):
    n, commit_every = 100, 25
    with store.batch(commit_every=commit_every):
        _write_n(store, n)  # 2n writes
    assert counter.connections == 1
    assert counter.commits <= math.ceil(2 * n / commit_every) + 1
    assert store.count() == n


def test_unbatched_writes_commit_per_row(store, counter):
    """Outside a batch every write commits on its own connection."""
    _write_n(store, 10)
    assert counter.commits == 20
    assert counter.connections == 20


def test_get_between_sees_uncommitted_row_in_batch(store, tmp_path):
    with store.batch():
        store.add(_rel(1))
        assert store.get_between("a00001", "b00001") is not None
        other = sqlite3.connect(store.db_path)
        try:
            assert other.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
        finally:
            other.close()
    assert store.get_between("a00001", "b00001") is not None


def test_add_or_update_and_increment_inside_batch(store, counter):
    with store.batch():
        store.add_or_update(_rel(1))
        store.add_or_update(_rel(1))
        store.increment_shared_event("a00002", "b00002")
        store.increment_shared_event("a00002", "b00002")
        store.increment_shared_thread("a00002", "b00002")
        assert len(store.get_for_person("a00002")) == 1
        assert store.delete_for_person("a00001") == 1
    assert counter.connections == 1
    rel = store.get_between("a00002", "b00002")
    assert rel.shared_events_count == 2
    assert rel.shared_threads_count == 1
    assert store.get_between("a00001", "b00001") is None


def test_exception_rolls_back_open_chunk_only(store):
    with pytest.raises(RuntimeError):
        with store.batch(commit_every=10):
            for i in range(25):
                store.add(_rel(i))
            raise RuntimeError("boom")
    # Two full chunks (20 rows) were committed; the open chunk (5) was rolled back.
    assert store.count() == 20
    # The store is usable again with normal per-call behavior.
    store.add(_rel(1000))
    assert store.count() == 21
    assert getattr(store._local, "batch", None) is None


def test_other_thread_gets_its_own_connection(store):
    seen = {}

    def worker():
        seen["conn_is_batch"] = store._acquire() is store._local.__dict__.get("batch", {}).get("conn")
        seen["batch_state"] = getattr(store._local, "batch", None)
        seen["count"] = store.count()

    with store.batch(commit_every=1000):
        store.add(_rel(1))
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
    assert seen["batch_state"] is None
    assert seen["conn_is_batch"] is False
    assert seen["count"] == 0  # the other thread cannot see the batch's uncommitted row
    assert store.count() == 1


def test_nested_batch_joins_outer(store, counter):
    with store.batch(commit_every=1000):
        store.add(_rel(1))
        with store.batch(commit_every=1):
            store.add(_rel(2))
        assert store._local.batch is not None
        store.add(_rel(3))
    assert counter.connections == 1
    assert store.count() == 3


def test_open_connection_is_independent_of_batch(store):
    with store.batch():
        conn = store.open_connection()
        try:
            assert conn is not store._local.batch["conn"]
        finally:
            conn.close()
        store.add(_rel(1))


class _Person:
    def __init__(self, pid, hidden):
        self.id = pid
        self.hidden = hidden


class _People:
    def __init__(self, people):
        self._people = people

    def get_all(self, include_hidden=False):
        return list(self._people)


PHASES = [
    "discover_from_calendar",
    "discover_from_calendar_direct",
    "discover_from_email_threads",
    "discover_from_vault_comments",
    "discover_from_messaging_groups",
    "discover_from_imessage_direct",
    "discover_from_whatsapp_direct",
    "discover_from_phone_calls",
    "discover_from_slack_direct",
    "discover_linkedin_connections",
    "discover_from_shared_photos",
]


def test_run_full_discovery_batches_phases_then_cleans_hidden_people(store, counter, tmp_path):
    people = _People([_Person("a00001", True)] + [_Person(f"p{i}", False) for i in range(4)])
    observed = {}

    def writing_phase(*args, **kwargs):
        store.add(_rel(1))  # touches hidden person a00001
        store.add(_rel(2))
        probe = sqlite3.connect(store.db_path)
        try:
            observed.setdefault(
                "visible_to_other_connection",
                probe.execute("SELECT COUNT(*) FROM relationships").fetchone()[0],
            )
        finally:
            probe.close()
        return [object()]

    patches = [
        patch.object(relationship_discovery, name, return_value=[]) for name in PHASES
    ]
    patches[0] = patch.object(relationship_discovery, PHASES[0], side_effect=writing_phase)
    with patch.object(relationship_discovery, "get_relationship_store", return_value=store), \
         patch.object(relationship_discovery, "get_person_entity_store", return_value=people), \
         patch("api.services.source_entity.get_crm_db_path", return_value=store.db_path):
        for p in patches:
            p.start()
        try:
            result = relationship_discovery.run_full_discovery()
        finally:
            for p in patches:
                p.stop()

    assert observed["visible_to_other_connection"] == 0  # phase ran inside the batch
    assert result["by_source"]["calendar"] == 1
    # Hidden-person cleanup ran after the batch committed and closed.
    assert store.get_between("a00001", "b00001") is None
    assert store.get_between("a00002", "b00002") is not None
    assert getattr(store._local, "batch", None) is None


def _run_discovery_with_phases(store, people, first_phase, second_phase=None, crm_path=None):
    patches = [patch.object(relationship_discovery, n, return_value=[]) for n in PHASES]
    patches[0] = patch.object(relationship_discovery, PHASES[0], side_effect=first_phase)
    if second_phase is not None:
        patches[1] = patch.object(relationship_discovery, PHASES[1], side_effect=second_phase)
    with patch.object(relationship_discovery, "get_relationship_store", return_value=store), \
         patch.object(relationship_discovery, "get_person_entity_store", return_value=people), \
         patch("api.services.source_entity.get_crm_db_path", side_effect=crm_path or (lambda: store.db_path)):
        for p in patches:
            p.start()
        try:
            return relationship_discovery.run_full_discovery()
        finally:
            for p in patches:
                p.stop()


def test_write_lock_is_released_between_phases(store):
    """Writes left uncommitted by one phase must not block other writers during the next."""
    people = _People([_Person("p1", False)])
    outcome = {}

    def phase_one(*args, **kwargs):
        for i in range(3):
            store.add(_rel(i))
        return []

    def phase_two(*args, **kwargs):
        other = sqlite3.connect(store.db_path, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("DELETE FROM relationships WHERE id = 'none'")
            other.commit()
            outcome["error"] = None
        except sqlite3.OperationalError as exc:
            outcome["error"] = str(exc)
        finally:
            other.close()
        return []

    _run_discovery_with_phases(store, people, phase_one, phase_two)
    assert outcome["error"] is None
    assert store.count() == 3


def test_batch_commits_when_chunk_is_old(store, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(relationship_module.time, "monotonic", lambda: clock["t"])

    def committed_rows():
        other = sqlite3.connect(store.db_path)
        try:
            return other.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        finally:
            other.close()

    with store.batch(commit_every=1000):
        store.add(_rel(1))
        clock["t"] += 0.5
        store.add(_rel(2))
        assert committed_rows() == 0  # chunk is young and small
        clock["t"] += 1.0
        store.add(_rel(3))
        assert committed_rows() == 3  # chunk aged past the limit
        store.add(_rel(4))
        assert committed_rows() == 3  # a new chunk started


def test_hidden_people_cleanup_runs_after_batches_close(store):
    people = _People([_Person("a00001", True)] + [_Person(f"p{i}", False) for i in range(4)])
    seen = {}

    def crm_path():
        seen["batch_during_cleanup"] = getattr(store._local, "batch", None)
        return store.db_path

    def phase_one(*args, **kwargs):
        store.add(_rel(1))
        return []

    _run_discovery_with_phases(store, people, phase_one, crm_path=crm_path)
    assert "batch_during_cleanup" in seen  # the cleanup ran
    assert seen["batch_during_cleanup"] is None
    assert store.get_between("a00001", "b00001") is None
