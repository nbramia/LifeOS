"""
Tests for iMessage integration.
"""
import os
import pytest
import resource
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from api.services.imessage import (
    IMessageStore,
    IMessageRecord,
    apple_timestamp_to_datetime,
    datetime_to_apple_timestamp,
    extract_text_from_attributed_body,
    join_imessages_to_entities,
    resolve_entity_id,
    resolve_entity_id_confidence,
)


class TestAppleTimestampConversion:
    """Tests for Apple timestamp conversion functions."""

    def test_apple_timestamp_to_datetime(self):
        """Test converting Apple timestamp to datetime."""
        # Apple timestamp for 2024-01-15 12:00:00 UTC
        # Unix epoch + offset + date
        apple_ts = 726840000_000_000_000  # nanoseconds

        dt = apple_timestamp_to_datetime(apple_ts)

        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        # Exact day depends on timezone, just verify it's in January 2024
        assert dt.tzinfo == timezone.utc

    def test_apple_timestamp_zero_returns_none(self):
        """Test that zero timestamp returns None."""
        assert apple_timestamp_to_datetime(0) is None
        assert apple_timestamp_to_datetime(None) is None

    def test_datetime_to_apple_timestamp_roundtrip(self):
        """Test roundtrip conversion."""
        original = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        apple_ts = datetime_to_apple_timestamp(original)
        converted = apple_timestamp_to_datetime(apple_ts)

        # Allow 1 second tolerance for floating point
        assert abs((converted - original).total_seconds()) < 1


class TestIMessageStore:
    """Tests for IMessageStore."""

    @pytest.fixture
    def temp_store(self):
        """Create a temporary store for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = IMessageStore(f.name)
            yield store
            Path(f.name).unlink(missing_ok=True)

    def test_store_creates_schema(self, temp_store):
        """Test that store creates proper schema."""
        import sqlite3

        with sqlite3.connect(temp_store.storage_path) as conn:
            # Check tables exist
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor}

            assert "messages" in tables
            assert "sync_state" in tables

    def test_sync_state_tracking(self, temp_store):
        """Test sync state ROWID tracking."""
        # Initial state should be 0
        assert temp_store._get_last_synced_rowid() == 0

        # Update and verify
        temp_store._set_last_synced_rowid(12345)
        assert temp_store._get_last_synced_rowid() == 12345

        # Update again
        temp_store._set_last_synced_rowid(99999)
        assert temp_store._get_last_synced_rowid() == 99999

    def test_insert_and_query_messages(self, temp_store):
        """Test inserting and querying messages."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Hello!", now.isoformat(), 1, "+15551234567", "+15551234567", "iMessage"),
            (2, "Hi there", now.isoformat(), 0, "+15551234567", "+15551234567", "iMessage"),
            (3, "How are you?", now.isoformat(), 1, "+15557654321", "+15557654321", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Query by phone
        messages = temp_store.get_messages_for_phone("+15551234567")
        assert len(messages) == 2

        messages = temp_store.get_messages_for_phone("+15557654321")
        assert len(messages) == 1

    def test_statistics(self, temp_store):
        """Test statistics collection."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test 1", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
            (2, "Test 2", now.isoformat(), 0, "+15552222222", "+15552222222", "SMS"),
            (3, "Test 3", now.isoformat(), 0, "+15553333333", "+15553333333", "SMS"),
        ]
        temp_store._insert_batch(batch)

        stats = temp_store.get_statistics()

        assert stats["total_messages"] == 3
        assert stats["by_service"]["iMessage"] == 1
        assert stats["by_service"]["SMS"] == 2
        assert stats["sent"] == 1
        assert stats["received"] == 2
        assert stats["unique_contacts"] == 3

    def test_update_entity_mappings(self, temp_store):
        """Test updating entity mappings."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test 1", now.isoformat(), 1, "+15551234567", "+15551234567", "iMessage"),
            (2, "Test 2", now.isoformat(), 0, "+15551234567", "+15551234567", "iMessage"),
            (3, "Test 3", now.isoformat(), 0, "+15559876543", "+15559876543", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Update mappings
        phone_to_entity = {
            "+15551234567": "entity-123",
            "+15559876543": "entity-456",
        }
        updated = temp_store.update_entity_mappings(phone_to_entity)

        assert updated == 3

        # Verify by querying
        messages = temp_store.get_messages_for_entity("entity-123")
        assert len(messages) == 2

        messages = temp_store.get_messages_for_entity("entity-456")
        assert len(messages) == 1

    def test_search_messages(self, temp_store):
        """Test message search."""
        # Insert test messages
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Hello world!", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
            (2, "Goodbye world!", now.isoformat(), 0, "+15551111111", "+15551111111", "iMessage"),
            (3, "Something else", now.isoformat(), 0, "+15552222222", "+15552222222", "SMS"),
        ]
        temp_store._insert_batch(batch)

        # Search for "world"
        results = temp_store.search_messages("world")
        assert len(results) == 2

        # Search with phone filter
        results = temp_store.search_messages("world", phone="+15551111111")
        assert len(results) == 2

        # Search for non-existent
        results = temp_store.search_messages("foobar")
        assert len(results) == 0

    def test_clear_data(self, temp_store):
        """Test clearing data for full resync."""
        # Insert some data
        now = datetime.now(timezone.utc)
        batch = [
            (1, "Test", now.isoformat(), 1, "+15551111111", "+15551111111", "iMessage"),
        ]
        temp_store._insert_batch(batch)
        temp_store._set_last_synced_rowid(1000)

        # Verify data exists
        assert temp_store.get_statistics()["total_messages"] == 1
        assert temp_store._get_last_synced_rowid() == 1000

        # Clear data
        temp_store._clear_data()

        # Verify cleared
        assert temp_store.get_statistics()["total_messages"] == 0
        assert temp_store._get_last_synced_rowid() == 0


class TestQueryMessagesDateFilters:
    """Regression tests for date-bound handling in query_messages.

    Guards the orchestrator's get_message_history path, which passes date
    bounds as 'YYYY-MM-DD' strings (previously crashed on str.isoformat) and
    issues single-day queries for 'yesterday'/'on <date>' (previously returned
    nothing because a date-only end collapsed to midnight). Timestamps are at
    11:00/13:00 UTC so the target day resolves correctly for any realistic
    local timezone.
    """

    @pytest.fixture
    def store_with_dated_msgs(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = IMessageStore(f.name)
            batch = [
                (1, "day before", "2024-06-14T12:00:00+00:00", 1, "+15550000001", "+15550000001", "iMessage"),
                (2, "target morning", "2024-06-15T11:00:00+00:00", 0, "+15550000001", "+15550000001", "iMessage"),
                (3, "target afternoon", "2024-06-15T13:00:00+00:00", 1, "+15550000001", "+15550000001", "iMessage"),
                (4, "day after", "2024-06-16T12:00:00+00:00", 0, "+15550000001", "+15550000001", "iMessage"),
            ]
            store._insert_batch(batch)
            store.update_entity_mappings({"+15550000001": "entity-date"})
            yield store
            Path(f.name).unlink(missing_ok=True)

    def test_string_bounds_do_not_crash(self, store_with_dated_msgs):
        # Regression: string bounds previously raised AttributeError on .isoformat()
        msgs = store_with_dated_msgs.query_messages(
            entity_id="entity-date", start_date="2024-06-01", end_date="2024-06-30"
        )
        assert len(msgs) == 4

    def test_single_day_string_is_inclusive(self, store_with_dated_msgs):
        # 'on 2024-06-15' / 'yesterday': start == end, date-only -> whole local day
        msgs = store_with_dated_msgs.query_messages(
            entity_id="entity-date", start_date="2024-06-15", end_date="2024-06-15"
        )
        assert {m.text for m in msgs} == {"target morning", "target afternoon"}

    def test_date_only_start_excludes_earlier(self, store_with_dated_msgs):
        msgs = store_with_dated_msgs.query_messages(
            entity_id="entity-date", start_date="2024-06-15"
        )
        assert {m.text for m in msgs} == {"target morning", "target afternoon", "day after"}

    def test_datetime_bounds_still_work(self, store_with_dated_msgs):
        msgs = store_with_dated_msgs.query_messages(
            entity_id="entity-date",
            start_date=datetime(2024, 6, 15, tzinfo=timezone.utc),
            end_date=datetime(2024, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
        )
        assert {m.text for m in msgs} == {"target morning", "target afternoon"}

    def test_unparseable_bound_is_ignored_not_emptying(self, store_with_dated_msgs):
        # A malformed date must not crash and must not silently drop every row.
        msgs = store_with_dated_msgs.query_messages(
            entity_id="entity-date", start_date="not-a-date"
        )
        assert len(msgs) == 4


class TestResolveEntityId:
    """resolve_entity_id / resolve_entity_id_confidence (#346).

    imessage.py used to define resolve_entity_id twice: a people_aggregator-
    based version and an entity_resolver-based version that silently shadowed
    it (the second definition wins in Python). These tests pin the surviving,
    entity_resolver-based behavior, and the confidence signal it now surfaces
    separately — a bare id can't tell a confident match from an ambiguous one.
    """

    ENTITY_ID = "11111111-2222-3333-4444-555555555555"

    def test_valid_uuid_passes_through_without_resolving(self, monkeypatch):
        """A UUID never needs the resolver — hitting it would be wasted work
        and, if the resolver were ever wrong, could substitute an unrelated
        person for an id that was already correct."""
        calls = []
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: calls.append(True),
        )
        assert resolve_entity_id(self.ENTITY_ID) == self.ENTITY_ID
        assert calls == []

    def test_valid_uuid_confidence_is_never_ambiguous(self):
        resolved_id, ambiguous = resolve_entity_id_confidence(self.ENTITY_ID)
        assert resolved_id == self.ENTITY_ID
        assert ambiguous is False

    def test_name_slug_resolves_via_entity_resolver(self, monkeypatch):
        result = SimpleNamespace(
            entity=SimpleNamespace(id=self.ENTITY_ID), match_type="name_exact"
        )
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: SimpleNamespace(resolve=lambda **kw: result),
        )
        assert resolve_entity_id("robin-doe") == self.ENTITY_ID

    def test_name_slug_converts_hyphens_to_spaces(self, monkeypatch):
        seen = {}

        def fake_resolve(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                entity=SimpleNamespace(id=self.ENTITY_ID), match_type="name_exact"
            )

        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: SimpleNamespace(resolve=fake_resolve),
        )
        resolve_entity_id("robin-alex-doe")
        assert seen["name"] == "robin alex doe"

    def test_no_match_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: SimpleNamespace(resolve=lambda **kw: None),
        )
        assert resolve_entity_id("nobody-here") is None
        assert resolve_entity_id_confidence("nobody-here") == (None, False)

    def test_confident_exact_match_is_not_ambiguous(self, monkeypatch):
        result = SimpleNamespace(
            entity=SimpleNamespace(id=self.ENTITY_ID), match_type="name_exact"
        )
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: SimpleNamespace(resolve=lambda **kw: result),
        )
        resolved_id, ambiguous = resolve_entity_id_confidence("robin-doe")
        assert resolved_id == self.ENTITY_ID
        assert ambiguous is False

    def test_fuzzy_ambiguous_match_is_flagged(self, monkeypatch):
        """entity_resolver.resolve() can return match_type='fuzzy_ambiguous'
        (confidence reduced to 0.7) when two candidates scored too close to
        call. resolve_entity_id() discards this entirely — making an uncertain
        match indistinguishable from an exact one — so callers whose output
        can expose the wrong person's data must use the confidence variant."""
        result = SimpleNamespace(
            entity=SimpleNamespace(id=self.ENTITY_ID),
            match_type="fuzzy_ambiguous",
            confidence=0.7,
            disambiguation_applied=True,
        )
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: SimpleNamespace(resolve=lambda **kw: result),
        )
        resolved_id, ambiguous = resolve_entity_id_confidence("robin-doe")
        assert resolved_id == self.ENTITY_ID
        assert ambiguous is True
        # resolve_entity_id() still returns just the id for callers that don't
        # need the confidence signal.
        assert resolve_entity_id("robin-doe") == self.ENTITY_ID


class TestResolveEntityIdMalformedInput:
    """entity_id is filled in by an LLM tool call, so None, non-string types,
    and empty/whitespace strings all arrive in practice. Before the guard,
    `uuid.UUID(None)` raised TypeError and `uuid.UUID(123)`/`uuid.UUID([])`/
    `uuid.UUID({})` raised AttributeError (no `.replace()`) — neither is the
    ValueError this function otherwise treats as "not a UUID, try the
    resolver" — so the tool crashed instead of returning its normal
    could-not-resolve response.
    """

    @pytest.mark.parametrize(
        "bad_entity_id", [None, 123, [], {}, "", "   "],
    )
    def test_malformed_entity_id_does_not_raise(self, bad_entity_id, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "api.services.entity_resolver.get_entity_resolver",
            lambda: calls.append(True),
        )
        assert resolve_entity_id_confidence(bad_entity_id) == (None, False)
        assert resolve_entity_id(bad_entity_id) is None
        # None of these should even reach the resolver.
        assert calls == []


class TestIMessageRecord:
    """Tests for IMessageRecord dataclass."""

    def test_create_record(self):
        """Test creating an IMessageRecord."""
        record = IMessageRecord(
            rowid=1,
            text="Hello!",
            timestamp=datetime.now(timezone.utc),
            is_from_me=True,
            handle="+15551234567",
            handle_normalized="+15551234567",
            service="iMessage",
        )

        assert record.rowid == 1
        assert record.text == "Hello!"
        assert record.is_from_me is True
        assert record.service == "iMessage"
        assert record.person_entity_id is None


class TestExtractTextFromAttributedBody:
    """Tests for attributedBody text extraction."""

    def test_extract_simple_text(self):
        """Test extracting text from simulated attributedBody."""
        # Simulate the format with embedded text
        blob = b"streamtyped\x00\x00\x00NSString\x00Hello world!\x00NSDictionary"
        result = extract_text_from_attributed_body(blob)
        assert result == "Hello world!"

    def test_extract_longer_text_wins(self):
        """Test that longest non-metadata string is returned."""
        blob = b"\x00NSString\x00Hi\x00This is a longer message\x00NSObject"
        result = extract_text_from_attributed_body(blob)
        assert result == "This is a longer message"

    def test_extract_none_for_empty(self):
        """Test that None is returned for empty input."""
        assert extract_text_from_attributed_body(None) is None
        assert extract_text_from_attributed_body(b"") is None

    def test_extract_filters_metadata(self):
        """Test that NS* metadata strings are filtered out."""
        blob = b"NSMutableAttributedString\x00NSString\x00Actual message"
        result = extract_text_from_attributed_body(blob)
        assert result == "Actual message"

    def test_extract_unicode_text(self):
        """Test extraction of unicode text."""
        # Unicode text with accented characters
        blob = "streamtyped\x00\x00Hello café world!\x00NSString".encode("utf-8")
        result = extract_text_from_attributed_body(blob)
        assert result == "Hello café world!"


# Enough messages to span ~30 of the export's 1000-row batches.
SOURCE_MESSAGE_COUNT = 30_000


def _count_open_fds() -> int:
    """Number of file descriptors this process currently holds open."""
    proc_fds = Path("/proc/self/fd")
    if proc_fds.is_dir():  # Linux
        return len(os.listdir(proc_fds))

    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)  # macOS/BSD
    ceiling = 4096 if soft == resource.RLIM_INFINITY else min(soft, 4096)
    open_fds = 0
    for fd in range(ceiling):
        try:
            os.fstat(fd)
        except OSError:
            continue
        open_fds += 1
    return open_fds


def _make_synthetic_chat_db(path: Path, message_count: int) -> None:
    """Build a chat.db-shaped source database with obviously synthetic messages."""
    # 2024-06-15T12:00:00Z in Apple's nanoseconds-since-2001 epoch.
    apple_ts = datetime_to_apple_timestamp(datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc))

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                attributedBody BLOB,
                date INTEGER,
                is_from_me INTEGER,
                handle_id INTEGER,
                service TEXT
            );
        """)
        conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15550001111')")
        conn.executemany(
            "INSERT INTO message (ROWID, text, attributedBody, date, is_from_me, handle_id, service)"
            " VALUES (?, ?, NULL, ?, ?, 1, 'iMessage')",
            [
                (rowid, f"synthetic message {rowid}", apple_ts, rowid % 2)
                for rowid in range(1, message_count + 1)
            ],
        )


class TestExportConnectionLifecycle:
    """Regression tests for the file-descriptor leak in the export path (#647).

    `with sqlite3.connect(...)` is a *transaction* context manager, not a
    closing one: it commits but leaves the connection (and its fds) open. One
    connection per batch exhausted the default macOS `ulimit -n` of 256 partway
    through a first full export, and the resulting OperationalError on the
    *destination* database was reported as missing Full Disk Access on the
    *source*.
    """

    @pytest.fixture
    def temp_store(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = IMessageStore(f.name)
            yield store
            for suffix in ("", "-wal", "-shm"):
                Path(f.name + suffix).unlink(missing_ok=True)

    @pytest.fixture
    def synthetic_source(self, tmp_path):
        """A chat.db stand-in with enough rows to span many 1000-row batches.

        The count matters: it has to exceed the fd headroom the low-limit test
        allows, so a per-batch connection actually exhausts the limit.
        """
        source = tmp_path / "chat.db"
        _make_synthetic_chat_db(source, message_count=SOURCE_MESSAGE_COUNT)
        return source

    def test_insert_batch_does_not_leak_fds(self, temp_store):
        """200 batches must not accumulate 200 connections."""
        now = datetime.now(timezone.utc).isoformat()
        temp_store._insert_batch(
            [(0, "warmup", now, 0, "+15550001111", "+15550001111", "iMessage")]
        )

        before = _count_open_fds()
        for rowid in range(1, 201):
            temp_store._insert_batch(
                [(rowid, f"synthetic {rowid}", now, rowid % 2, "+15550001111", "+15550001111", "iMessage")]
            )
        leaked = _count_open_fds() - before

        assert leaked <= 2, f"{leaked} file descriptors leaked across 200 batches"

    def test_clear_data_does_not_leak_fds(self, temp_store):
        temp_store._clear_data()  # warm up any lazily-created WAL sidecar files

        before = _count_open_fds()
        for _ in range(50):
            temp_store._clear_data()
        leaked = _count_open_fds() - before

        assert leaked <= 2, f"{leaked} file descriptors leaked across 50 _clear_data calls"

    def test_full_export_does_not_leak_fds(self, temp_store, synthetic_source):
        """A multi-batch export holds one destination connection, not one per batch."""
        temp_store.SOURCE_DB_PATH = synthetic_source

        before = _count_open_fds()
        stats = temp_store.export_from_source()
        leaked = _count_open_fds() - before

        assert stats["messages_exported"] == SOURCE_MESSAGE_COUNT
        assert stats["new_last_rowid"] == SOURCE_MESSAGE_COUNT
        assert leaked <= 2, f"{leaked} file descriptors leaked across a multi-batch export"

    def test_full_export_survives_a_low_fd_limit(self, temp_store, synthetic_source):
        """The real failure mode: a full export under a tight `ulimit -n`."""
        temp_store.SOURCE_DB_PATH = synthetic_source

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        headroom = _count_open_fds() + 24
        if soft != resource.RLIM_INFINITY and soft <= headroom:
            pytest.skip("fd limit already tighter than the test's headroom")

        resource.setrlimit(resource.RLIMIT_NOFILE, (headroom, hard))
        try:
            stats = temp_store.export_from_source()
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

        assert stats["messages_exported"] == SOURCE_MESSAGE_COUNT

    def test_destination_failure_is_not_reported_as_full_disk_access(
        self, temp_store, synthetic_source
    ):
        """An OperationalError while writing must surface as itself, not as FDA."""
        temp_store.SOURCE_DB_PATH = synthetic_source

        def failing_insert(batch, conn=None):
            raise sqlite3.OperationalError("unable to open database file")

        temp_store._insert_batch = failing_insert

        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            temp_store.export_from_source()

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses file permissions, so the source cannot be made unreadable",
    )
    def test_unreadable_source_still_reports_full_disk_access(
        self, temp_store, synthetic_source
    ):
        """FDA guidance is still given when the source database is genuinely unreadable."""
        temp_store.SOURCE_DB_PATH = synthetic_source
        synthetic_source.chmod(0o000)
        try:
            with pytest.raises(PermissionError, match="Full Disk Access"):
                temp_store.export_from_source()
        finally:
            synthetic_source.chmod(0o600)

    def test_checkpoint_flushes_wal_so_a_file_copy_is_complete(
        self, temp_store, synthetic_source, tmp_path
    ):
        """A single-file copy of the store must not drop freshly exported rows.

        The store runs in WAL mode, so an export's writes can still be sitting
        in the -wal sidecar. `shutil.copy2` takes only the main database file,
        which is how the Apple export shipped a database missing its newest
        messages (#647).

        SQLite checkpoints on its own when the *last* connection closes, so the
        gap only opens when something else holds the database open — the API
        server, say. This test pins that case open deliberately; without it the
        implicit checkpoint would mask the bug.
        """
        temp_store.SOURCE_DB_PATH = synthetic_source

        with closing(sqlite3.connect(temp_store.storage_path)) as bystander:
            bystander.execute("SELECT COUNT(*) FROM messages").fetchone()

            temp_store.export_from_source()
            temp_store.checkpoint()

            copied = tmp_path / "copy-of-imessage.db"
            shutil.copy2(temp_store.storage_path, copied)  # main file only, no -wal

        with closing(sqlite3.connect(copied)) as conn:
            copied_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        assert copied_count == SOURCE_MESSAGE_COUNT


class TestQueryPathConnectionsClose:
    """Regression tests for #678: the 9 query-path connections now use
    `contextlib.closing`, matching the export-path fix in #647.

    Counting live fds (as TestExportConnectionLifecycle does) can't tell a
    real fix from a no-op here: CPython's refcounting already closes a
    connection the instant its local variable goes out of scope, so a bare
    `with sqlite3.connect(...) as conn:` and a `closing(...)` one look
    identical by fd count. Instead these tests spy on `sqlite3.connect` to
    capture the actual connection object each method opens, then assert
    operating on it raises "closed database" — which only `closing()`
    guarantees deterministically at the end of the `with` block.
    """

    @pytest.fixture
    def temp_store(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            store = IMessageStore(f.name)
            yield store
            for suffix in ("", "-wal", "-shm"):
                Path(f.name + suffix).unlink(missing_ok=True)

    @pytest.fixture
    def captured_connections(self, monkeypatch):
        """Spy on sqlite3.connect as used by imessage.py; return the connections it opens."""
        captured = []
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            captured.append(conn)
            return conn

        monkeypatch.setattr("api.services.imessage.sqlite3.connect", spy_connect)
        return captured

    def _assert_closed(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")

    def test_update_entity_mappings_closes_and_commits(self, temp_store, captured_connections):
        with closing(sqlite3.connect(temp_store.storage_path)) as conn, conn:
            conn.execute(
                "INSERT INTO messages (rowid, text, timestamp, is_from_me, handle,"
                " handle_normalized, service) VALUES"
                " (1, 'hi', '2024-01-01T00:00:00', 0, '+15550001111', '+15550001111', 'iMessage')"
            )
        captured_connections.clear()  # drop the setup connection above

        updated = temp_store.update_entity_mappings({"+15550001111": "entity-1"})

        assert updated == 1
        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

        # A closing() swap drops the commit unless paired with `, conn`; confirm
        # the write actually landed, not just that the connection closed.
        with closing(sqlite3.connect(temp_store.storage_path)) as conn:
            row = conn.execute("SELECT person_entity_id FROM messages WHERE rowid = 1").fetchone()
        assert row[0] == "entity-1"

    def test_update_entity_mappings_by_handle_closes_and_commits(self, temp_store, captured_connections):
        with closing(sqlite3.connect(temp_store.storage_path)) as conn, conn:
            conn.execute(
                "INSERT INTO messages (rowid, text, timestamp, is_from_me, handle,"
                " handle_normalized, service) VALUES"
                " (1, 'hi', '2024-01-01T00:00:00', 0, 'friend@example.com', NULL, 'iMessage')"
            )
        captured_connections.clear()  # drop the setup connection above

        updated = temp_store.update_entity_mappings_by_handle({"friend@example.com": "entity-2"})

        assert updated == 1
        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

        with closing(sqlite3.connect(temp_store.storage_path)) as conn:
            row = conn.execute("SELECT person_entity_id FROM messages WHERE rowid = 1").fetchone()
        assert row[0] == "entity-2"

    def test_get_messages_for_phone_closes(self, temp_store, captured_connections):
        temp_store.get_messages_for_phone("+15550001111")

        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

    def test_get_messages_for_entity_closes(self, temp_store, captured_connections):
        temp_store.get_messages_for_entity("entity-1")

        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

    def test_search_messages_closes(self, temp_store, captured_connections):
        temp_store.search_messages("hello")

        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

    def test_query_messages_closes(self, temp_store, captured_connections):
        temp_store.query_messages()

        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

    def test_get_statistics_closes(self, temp_store, captured_connections):
        temp_store.get_statistics()

        # get_statistics() also calls _get_last_synced_rowid(), which opens
        # its own connection (line 252) — expect both, both closed.
        assert len(captured_connections) == 2
        for conn in captured_connections:
            self._assert_closed(conn)

    def test_get_recent_conversations_closes(self, temp_store, captured_connections):
        temp_store.get_recent_conversations()

        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])

    def test_join_imessages_to_entities_closes(self, temp_store, captured_connections, monkeypatch):
        monkeypatch.setattr("api.services.imessage.get_imessage_store", lambda *a, **kw: temp_store)
        monkeypatch.setattr(
            "api.services.person_entity.get_person_entity_store",
            lambda *a, **kw: SimpleNamespace(get_by_phone=lambda p: None, get_by_email=lambda e: None),
        )

        stats = join_imessages_to_entities()

        assert stats["unique_phones"] == 0
        assert stats["unique_emails"] == 0
        assert len(captured_connections) == 1
        self._assert_closed(captured_connections[0])
