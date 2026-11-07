"""
Tests for the Dailies database journal source (api/services/dailies_db.py and
its wiring in GSheetSyncService). Every row comes from a fake row source with
obviously synthetic values; no test connects to a database.
"""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest

from api.routes.journal import parse_emotion_chain
from api.services import dailies_db
from api.services.gsheet_sync import GSheetSyncService, JournalEntry, SheetConfig

pytestmark = pytest.mark.unit

DSN = "postgresql://synthetic-reader@db.invalid/synthetic"
SYNTHETIC_SUBJECT = "synthetic|subject-0001"


def make_row(**overrides) -> dict:
    row = {
        "id": 987654,
        "subject": SYNTHETIC_SUBJECT,
        "revision": 3,
        "source": "home",
        "mirror_status": "mirrored",
        "mirror_row": 4242,
        "mirror_timestamp": "1/2/2030 9:08:07",
        "raw": None,
        "entry_date": date(2030, 1, 2),
        "logged_at": datetime(2030, 1, 2, 14, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2030, 1, 2, 14, 0, tzinfo=timezone.utc),
        "connection_taylor": True,
        "connection_malea": False,
        "resonant": "Synthetic resonant text about a lighthouse",
        "word": "Zephyr",
        "sleep_hours": Decimal("7.0"),
        "alcohol": "1-2",
        "caffeine": "2",
        "food": "healthy",
        "exercise": "mid",
        "mood": 6,
        "stress": 4,
        "body": 5,
        "feelings": [["Bad", "Tired", "Sleepy"]],
        "feeling_status": "chosen",
    }
    row.update(overrides)
    return row


@pytest.fixture
def service(tmp_path, monkeypatch):
    config_path = tmp_path / "gsheet_sync.yaml"
    config_path.write_text("dailies_db:\n  enabled: true\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("config.settings.settings.dailies_database_url", DSN)
    with patch("api.services.gsheet_sync.CONFIG_PATH", config_path):
        svc = GSheetSyncService(vault_path=vault, db_path=str(tmp_path / "state.db"))
    return svc


def note_path(svc, date_str="2030-01-02") -> Path:
    return Path(svc.vault_path) / "Personal" / "Journal" / f"{date_str}.md"


def read_fm(svc, date_str="2030-01-02") -> dict:
    return dict(frontmatter.loads(note_path(svc, date_str).read_text(encoding="utf-8")).metadata)


def sync(svc, rows):
    return svc._sync_dailies_db(fetch_rows=lambda dsn: rows)


class TestFieldMapping:
    def test_every_field_maps_to_the_form_strings(self, service):
        sync(service, [make_row()])
        fm = read_fm(service)
        assert fm["date"] == date(2030, 1, 2)
        assert fm["type"] == "journal"
        assert fm["resonant_moment"] == "Synthetic resonant text about a lighthouse"
        assert fm["connection_taylor"] is True
        assert fm["connection_malea"] is False
        assert fm["one_word"] == "Zephyr"
        assert fm["exercise"] == "Mid"
        assert fm["eating"] == "Ate healthily"
        assert fm["sleep"] == 7
        assert fm["mood"] == 6
        assert fm["stress"] == 4
        assert fm["body"] == 5
        assert fm["caffeine"] == "2 cups"
        assert fm["alcohol"] == "1-2 drinks"
        assert fm["feeling"] == "Bad"
        assert fm["bad_feeling"] == "Tired"
        assert fm["tired_feeling"] == "Sleepy"
        assert fm["feelings"] == ["Bad > Tired > Sleepy"]

    def test_half_hour_sleep_is_a_float(self, service):
        sync(service, [make_row(sleep_hours=Decimal("6.5"))])
        assert read_fm(service)["sleep"] == 6.5

    @pytest.mark.parametrize("column,value,key,expected", [
        ("food", "binge", "eating", "Binge-snacked"),
        ("food", "unhealthy", "eating", "Ate unhealthily"),
        ("food", "very_healthy", "eating", "Ate especially healthily"),
        ("exercise", "none", "exercise", "None"),
        ("exercise", "low", "exercise", "Low"),
        ("exercise", "high", "exercise", "High"),
        ("alcohol", "0", "alcohol", 0),
        ("alcohol", "3-4", "alcohol", "3-4 drinks"),
        ("alcohol", "5+", "alcohol", "5+ drinks"),
    ])
    def test_enum_values(self, service, column, value, key, expected):
        sync(service, [make_row(**{column: value})])
        assert read_fm(service)[key] == expected

    @pytest.mark.parametrize("value,expected", [
        ("0", 0), ("1", "1 cup"), ("2", "2 cups"), ("3+", "3+ cups"),
        ("1-2", "1-2 cups"), ("3-4", "3-4 cups"), ("5+", "5+ cups"),
    ])
    def test_new_and_legacy_caffeine_keep_their_own_values(self, service, value, expected):
        sync(service, [make_row(caffeine=value)])
        assert read_fm(service)["caffeine"] == expected

    def test_null_columns_are_omitted(self, service):
        sync(service, [make_row(connection_malea=None, word=None, caffeine=None, mood=None)])
        fm = read_fm(service)
        for key in ("connection_malea", "one_word", "caffeine", "mood"):
            assert key not in fm

    def test_quotes_and_line_breaks_survive_as_valid_yaml(self, service):
        text = 'Synthetic "quoted" words\nand a second line: ok'
        sync(service, [make_row(resonant=text)])
        assert read_fm(service)["resonant_moment"] == text

    def test_no_internal_ids_subject_or_mirror_bookkeeping(self, service):
        sync(service, [make_row()])
        content = note_path(service).read_text(encoding="utf-8")
        fm = read_fm(service)
        for leaked in ("987654", SYNTHETIC_SUBJECT, "4242", "1/2/2030 9:08:07", "mirror", "revision", "subject"):
            assert leaked not in content
        for key in ("id", "subject", "revision", "source", "logged_at", "updated_at", "raw"):
            assert key not in fm

    def test_select_never_reads_internal_columns(self):
        sql = dailies_db.SELECT_SQL.lower()
        for column in ("id,", " raw", "mirror_", "logged_at"):
            assert column not in sql


class TestImportedHistory:
    """Imported Form rows are a lossy reconstruction; only rows written or
    edited in Home may replace an existing note."""

    def _sheet_note(self, service) -> bytes:
        path = note_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ndate: 2030-01-02\nrow_hash: sheetrow\nfeeling: Bad\nbored_feelings: Not sure\n---\n",
                        encoding="utf-8")
        return path.read_bytes()

    @pytest.mark.parametrize("source", ["form", "form-old"])
    def test_unedited_imported_row_leaves_the_note_byte_for_byte(self, service, source):
        before = self._sheet_note(service)
        result = sync(service, [make_row(source=source, revision=1)])
        assert note_path(service).read_bytes() == before
        assert result["new_rows"] == 0

    def test_unedited_imported_row_writes_no_note_for_a_missing_date(self, service):
        sync(service, [make_row(source="form", revision=1)])
        assert not note_path(service).exists()

    def test_imported_row_edited_in_home_is_written(self, service):
        self._sheet_note(service)
        result = sync(service, [make_row(source="form", revision=2)])
        assert result["new_rows"] == 1
        assert read_fm(service)["feelings"] == ["Bad > Tired > Sleepy"]

    def test_native_home_row_is_written_at_its_first_revision(self, service):
        result = sync(service, [make_row(source="home", revision=1)])
        assert result["new_rows"] == 1


class TestWriteFailures:
    def _failing_writer(self, service, monkeypatch):
        def boom(entry, config):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(service, "_create_journal_note", boom)

    def test_failed_write_is_counted_and_marks_the_source_failed(self, service, monkeypatch):
        self._failing_writer(service, monkeypatch)
        result = sync(service, [make_row()])
        assert result["status"] == "failed"
        assert result["failed"] == 1
        assert result["new_rows"] == 0

    def test_failed_write_fails_the_run(self, service, monkeypatch):
        self._failing_writer(service, monkeypatch)
        monkeypatch.setattr(dailies_db, "fetch_rows", lambda dsn: [make_row()])
        stats = service.sync_all()
        assert stats["failed"] == 1
        assert stats["status"] == "error"
        assert stats["reason"] == "dailies_db_note_write_failed"


class TestFeelings:
    def _sheet_note_fm(self, tmp_path, fields: dict) -> dict:
        """The note the sheet source writes for an equivalent row, using the
        Daily Journal sheet's field mappings for the feeling columns."""
        svc = GSheetSyncService.__new__(GSheetSyncService)
        svc.vault_path = tmp_path / "sheet_vault"
        config = SheetConfig(
            sheet_id="synthetic", name="Daily Journal", account="personal", range="R",
            timestamp_column="Timestamp",
            outputs={"journal_notes": {"enabled": True, "folder": "Personal/Journal"}},
            field_mappings={"Feelings": "feeling", "Bad feelings": "bad_feeling", "Tired feelings": "tired_feeling"},
        )
        entry = JournalEntry(row_hash="abc", timestamp=datetime(2030, 1, 2), date_str="2030-01-02",
                             fields=fields, raw_row=fields)
        svc._create_journal_note(entry, config)
        path = svc.vault_path / "Personal" / "Journal" / "2030-01-02.md"
        return dict(frontmatter.loads(path.read_text(encoding="utf-8")).metadata)

    @pytest.mark.parametrize("path,sheet_fields", [
        (["Bad", "Tired", "Sleepy"], {"Feelings": "Bad", "Bad feelings": "Tired", "Tired feelings": "Sleepy"}),
        (["Angry", "Let down", "Betrayed"], {"Feelings": "Angry", "Angry feelings": "Let down", "Let down feelings": "Betrayed"}),
        (["Happy", "Content", "Free"], {"Feelings": "Happy", "Happy feelings": "Content", "Content feelings": "Free"}),
    ])
    def test_three_level_chain_matches_a_sheet_sourced_note(self, service, tmp_path, path, sheet_fields):
        sync(service, [make_row(feelings=[path])])
        db_chain = parse_emotion_chain(read_fm(service))
        assert db_chain == path
        assert db_chain == parse_emotion_chain(self._sheet_note_fm(tmp_path, sheet_fields))

    def test_amazed_tertiary_chains_under_amazed(self, service):
        sync(service, [make_row(feelings=[["Surprised", "Amazed", "In awe"]])])
        assert parse_emotion_chain(read_fm(service)) == ["Surprised", "Amazed", "In awe"]

    def test_multiple_feelings_list_every_path_and_first_is_primary(self, service):
        paths = [["Fearful", "Anxious", "Worried"], ["Happy", "Proud"], ["Sad"]]
        sync(service, [make_row(feelings=paths)])
        fm = read_fm(service)
        assert fm["feelings"] == ["Fearful > Anxious > Worried", "Happy > Proud", "Sad"]
        assert parse_emotion_chain(fm) == ["Fearful", "Anxious", "Worried"]
        # Later paths never leak into the primary chain's keys.
        assert "happy_feelings" not in fm

    @pytest.mark.parametrize("status,expected", [("not_sure", "Not sure"), ("none", "None")])
    def test_not_sure_and_none(self, service, status, expected):
        sync(service, [make_row(feeling_status=status, feelings=[])])
        fm = read_fm(service)
        assert fm["feeling"] == expected
        assert "feelings" not in fm
        assert parse_emotion_chain(fm) == [expected]

    def test_no_status_writes_no_feeling(self, service):
        sync(service, [make_row(feeling_status=None, feelings=[])])
        assert "feeling" not in read_fm(service)


class TestDates:
    def test_late_evening_entry_keeps_its_new_york_date(self, service):
        # 23:30 in New York on Mar 4 is already Mar 5 in UTC.
        stamp = datetime(2030, 3, 5, 4, 30, tzinfo=timezone.utc)
        sync(service, [make_row(entry_date=date(2030, 3, 4), logged_at=stamp, updated_at=stamp)])
        assert note_path(service, "2030-03-04").exists()
        assert not note_path(service, "2030-03-05").exists()
        assert read_fm(service, "2030-03-04")["date"] == date(2030, 3, 4)

    def test_entry_filed_after_midnight_for_the_previous_day(self, service):
        # 00:10 in New York on Mar 5, filed for Mar 4.
        stamp = datetime(2030, 3, 5, 5, 10, tzinfo=timezone.utc)
        sync(service, [make_row(entry_date=date(2030, 3, 4), logged_at=stamp, updated_at=stamp)])
        assert note_path(service, "2030-03-04").exists()
        assert not note_path(service, "2030-03-05").exists()

    def test_string_entry_date_is_accepted(self, service):
        sync(service, [make_row(entry_date="2030-03-04")])
        assert note_path(service, "2030-03-04").exists()

    def test_malformed_entry_date_writes_nothing(self, service):
        sync(service, [make_row(entry_date="../../escape")])
        assert not any((Path(service.vault_path)).rglob("*.md"))


class TestChangeDetection:
    def test_unchanged_row_does_not_touch_the_file(self, service):
        import os

        rows = [make_row()]
        assert sync(service, rows)["new_rows"] == 1
        path = note_path(service)
        before = path.read_bytes()
        os.utime(path, (1_000_000_000, 1_000_000_000))
        result = sync(service, rows)
        assert result == {"status": "success", "new_rows": 0, "skipped_rows": 1, "failed": 0}
        assert path.read_bytes() == before
        assert path.stat().st_mtime == 1_000_000_000

    def test_db_version_replaces_a_sheet_rewrite_of_the_same_date(self, service):
        # Overlap night: the sheet source rewrites a date the database source
        # already synced; the next database pass must put its version back.
        rows = [make_row(feelings=[["Happy", "Proud"], ["Sad"]])]
        sync(service, rows)
        sheet_fields = {"Feelings": "Happy", "Happy feelings": "Proud"}
        sheet_config = SheetConfig(
            sheet_id="synthetic-sheet", name="Daily Journal", account="personal", range="R",
            timestamp_column="Timestamp",
            outputs={"journal_notes": {"enabled": True, "folder": "Personal/Journal"}},
            field_mappings={"Feelings": "feeling"},
        )
        service._create_journal_note(
            JournalEntry(row_hash="sheet0001", timestamp=datetime(2030, 1, 2), date_str="2030-01-02",
                         fields=sheet_fields, raw_row=sheet_fields),
            sheet_config,
        )
        assert "feelings" not in read_fm(service)
        result = sync(service, rows)
        assert result["new_rows"] == 1
        assert read_fm(service)["feelings"] == ["Happy > Proud", "Sad"]

    def test_sync_state_feeds_trend_taxonomy_with_wheel_branches_only(self, service):
        from api.routes.journal_trends import _derive_taxonomy_labels

        sync(service, [make_row(feelings=[["Angry", "Let down", "Betrayed"], ["Happy"]])])
        assert sorted(_derive_taxonomy_labels(service.db_path)) == ["Angry", "Let down"]

    def test_newer_update_time_rewrites_the_note(self, service):
        sync(service, [make_row()])
        later = datetime(2030, 1, 3, 9, 0, tzinfo=timezone.utc)
        result = sync(service, [make_row(updated_at=later, word="Nimbus")])
        assert result["new_rows"] == 1
        assert read_fm(service)["one_word"] == "Nimbus"


class TestCleanSkip:
    def _assert_untouched_skip(self, service, caplog, result, reason):
        assert result["status"] == "skipped"
        assert result["reason"] == reason
        assert note_path(service).read_text(encoding="utf-8") == "existing note"
        assert reason in caplog.text
        assert "Synthetic resonant" not in caplog.text

    def _existing(self, service):
        path = note_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing note", encoding="utf-8")

    def test_missing_credential_skips_without_connecting(self, service, monkeypatch, caplog):
        self._existing(service)
        monkeypatch.setattr("config.settings.settings.dailies_database_url", "")

        def boom(dsn):
            raise AssertionError("must not connect without a credential")

        with caplog.at_level(logging.INFO):
            result = service._sync_dailies_db(fetch_rows=boom)
        self._assert_untouched_skip(service, caplog, result, "dailies_db_not_configured")

    def test_unreachable_database_skips_and_logs_no_detail(self, service, caplog):
        self._existing(service)

        def unreachable(dsn):
            raise OSError("Synthetic resonant detail that must never be logged")

        with caplog.at_level(logging.INFO):
            result = service._sync_dailies_db(fetch_rows=unreachable)
        self._assert_untouched_skip(service, caplog, result, "dailies_db_unreachable")

    def test_rows_from_two_members_skip(self, service, caplog):
        self._existing(service)
        rows = [make_row(), make_row(subject="synthetic|subject-0002", entry_date=date(2030, 1, 3))]
        with caplog.at_level(logging.INFO):
            result = sync(service, rows)
        self._assert_untouched_skip(service, caplog, result, "dailies_db_multiple_members")
        assert not note_path(service, "2030-01-03").exists()

    def test_sync_all_reports_skip_when_only_source_is_unconfigured(self, service, monkeypatch):
        monkeypatch.setattr("config.settings.settings.dailies_database_url", "")
        stats = service.sync_all()
        assert stats["status"] == "skipped"
        assert stats["reason"] == "dailies_db_not_configured"

    def test_sync_all_runs_the_database_source(self, service, monkeypatch):
        monkeypatch.setattr(dailies_db, "fetch_rows", lambda dsn: [make_row()])
        stats = service.sync_all()
        assert "status" not in stats
        assert stats["synced"] == 1
        assert stats["dailies_db"] == {"status": "success"}
        assert note_path(service).exists()


class TestReadOnlyConnection:
    def test_fetch_rows_runs_one_select_in_a_read_only_transaction(self, monkeypatch):
        import psycopg

        calls = []

        class FakeConn:
            def __init__(self):
                self._read_only = False

            @property
            def read_only(self):
                return self._read_only

            @read_only.setter
            def read_only(self, value):
                calls.append(("read_only", value))
                self._read_only = value

            def execute(self, sql, *args):
                calls.append(("execute", sql, self._read_only))
                return self

            def fetchall(self):
                return [make_row()]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_connect(dsn, **kwargs):
            calls.append(("connect", dsn))
            return FakeConn()

        monkeypatch.setattr(psycopg, "connect", fake_connect)
        rows = dailies_db.fetch_rows(DSN)
        assert rows == [make_row()]
        executes = [c for c in calls if c[0] == "execute"]
        assert len(executes) == 1
        assert executes[0][1] is dailies_db.SELECT_SQL
        assert executes[0][2] is True  # read-only was set before the statement ran
        assert dailies_db.SELECT_SQL.strip().upper().startswith("SELECT")
        assert ";" not in dailies_db.SELECT_SQL
