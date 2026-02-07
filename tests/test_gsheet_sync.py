"""
Tests for Google Sheets sync service.
"""
import pytest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime

# Mark all tests as unit tests
pytestmark = pytest.mark.unit


class TestRowHash:
    """Test row hash computation."""

    def test_same_row_produces_same_hash(self):
        """Same row data should produce identical hash."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row = {"Timestamp": "1/28/2025 10:00:00", "Field1": "Value1"}
        hash1 = service._compute_row_hash(row, config)
        hash2 = service._compute_row_hash(row, config)

        assert hash1 == hash2

    def test_different_timestamps_produce_different_hash(self):
        """Different timestamps should produce different hashes."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row1 = {"Timestamp": "1/28/2025 10:00:00", "Field1": "Value1"}
        row2 = {"Timestamp": "1/28/2025 11:00:00", "Field1": "Value1"}

        hash1 = service._compute_row_hash(row1, config)
        hash2 = service._compute_row_hash(row2, config)

        assert hash1 != hash2

    def test_hash_is_16_chars(self):
        """Hash should be truncated to 16 characters."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row = {"Timestamp": "1/28/2025 10:00:00", "Field1": "Value1"}
        hash_val = service._compute_row_hash(row, config)

        assert len(hash_val) == 16


class TestTimestampParsing:
    """Test timestamp parsing from various formats."""

    def test_parses_us_date_format(self):
        """Should parse M/D/YYYY H:MM:SS format."""
        from api.services.gsheet_sync import GSheetSyncService

        service = GSheetSyncService.__new__(GSheetSyncService)

        dt, date_str = service._parse_timestamp("1/28/2025 10:30:45")

        assert date_str == "2025-01-28"
        assert dt.hour == 10
        assert dt.minute == 30

    def test_parses_iso_format(self):
        """Should parse YYYY-MM-DD HH:MM:SS format."""
        from api.services.gsheet_sync import GSheetSyncService

        service = GSheetSyncService.__new__(GSheetSyncService)

        dt, date_str = service._parse_timestamp("2025-01-28 14:30:00")

        assert date_str == "2025-01-28"
        assert dt.hour == 14

    def test_handles_invalid_format(self):
        """Should return today's date for invalid format."""
        from api.services.gsheet_sync import GSheetSyncService

        service = GSheetSyncService.__new__(GSheetSyncService)

        dt, date_str = service._parse_timestamp("invalid date")

        # Should return today's date as fallback
        today = datetime.now().strftime("%Y-%m-%d")
        assert date_str == today


class TestRowParsing:
    """Test row parsing into JournalEntry."""

    def test_parses_valid_row(self):
        """Should parse a row with all fields."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row = {
            "Timestamp": "1/28/2025 10:00:00",
            "How are you feeling?": "Good",
            "What did you eat?": "Salad",
        }

        entry = service._parse_row(row, config)

        assert entry is not None
        assert entry.date_str == "2025-01-28"
        assert "How are you feeling?" in entry.fields
        assert entry.fields["How are you feeling?"] == "Good"

    def test_skips_row_without_timestamp(self):
        """Should return None for rows without timestamp."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row = {"Field1": "Value1", "Field2": "Value2"}

        entry = service._parse_row(row, config)

        assert entry is None

    def test_skips_row_with_only_timestamp(self):
        """Should return None for rows with no data fields."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig

        service = GSheetSyncService.__new__(GSheetSyncService)
        config = SheetConfig(
            sheet_id="test",
            name="Test",
            account="personal",
            range="Sheet1",
            timestamp_column="Timestamp",
            outputs={},
            field_mappings={},
        )

        row = {"Timestamp": "1/28/2025 10:00:00"}

        entry = service._parse_row(row, config)

        assert entry is None


class TestEntryFormatting:
    """Test Markdown formatting of entries."""

    def test_formats_simple_field(self):
        """Should format field with label and value."""
        from api.services.gsheet_sync import GSheetSyncService, JournalEntry

        service = GSheetSyncService.__new__(GSheetSyncService)
        entry = JournalEntry(
            row_hash="abc123",
            timestamp=datetime.now(),
            date_str="2025-01-28",
            fields={"Emotional State": "Feeling great"},
            raw_row={},
        )

        md = service._format_entry_markdown(entry)

        assert "**Emotional State:** Feeling great" in md

    def test_formats_list_field(self):
        """Should format comma-separated values as bullet list."""
        from api.services.gsheet_sync import GSheetSyncService, JournalEntry

        service = GSheetSyncService.__new__(GSheetSyncService)
        entry = JournalEntry(
            row_hash="abc123",
            timestamp=datetime.now(),
            date_str="2025-01-28",
            fields={"Food": "Coffee, Salad, Water, Sandwich"},
            raw_row={},
        )

        md = service._format_entry_markdown(entry)

        assert "**Food:**" in md
        assert "- Coffee" in md
        assert "- Salad" in md
        assert "- Water" in md

    def test_strips_question_mark_from_label(self):
        """Should remove trailing ? from field labels."""
        from api.services.gsheet_sync import GSheetSyncService, JournalEntry

        service = GSheetSyncService.__new__(GSheetSyncService)
        entry = JournalEntry(
            row_hash="abc123",
            timestamp=datetime.now(),
            date_str="2025-01-28",
            fields={"How are you feeling?": "Good"},
            raw_row={},
        )

        md = service._format_entry_markdown(entry)

        assert "**How are you feeling:** Good" in md
        assert "?" not in md


class TestSyncStateTracking:
    """Test SQLite state tracking for synced rows."""

    def test_marks_and_checks_synced(self):
        """Should mark rows as synced and detect them."""
        from api.services.gsheet_sync import GSheetSyncService, JournalEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            # Create service with test paths
            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(db_path)):
                    service = GSheetSyncService(vault_path=vault_path, db_path=str(db_path))

            entry = JournalEntry(
                row_hash="test123",
                timestamp=datetime.now(),
                date_str="2025-01-28",
                fields={"Field": "Value"},
                raw_row={"Field": "Value"},
            )

            # Should not be synced initially
            assert not service._is_row_synced("sheet1", "test123")

            # Mark as synced
            service._mark_row_synced("sheet1", entry)

            # Should be synced now
            assert service._is_row_synced("sheet1", "test123")

    def test_different_sheets_tracked_separately(self):
        """Should track rows separately per sheet."""
        from api.services.gsheet_sync import GSheetSyncService, JournalEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(db_path)):
                    service = GSheetSyncService(vault_path=vault_path, db_path=str(db_path))

            entry = JournalEntry(
                row_hash="hash123",
                timestamp=datetime.now(),
                date_str="2025-01-28",
                fields={"Field": "Value"},
                raw_row={},
            )

            # Mark synced for sheet1
            service._mark_row_synced("sheet1", entry)

            # Should be synced for sheet1
            assert service._is_row_synced("sheet1", "hash123")

            # Should NOT be synced for sheet2
            assert not service._is_row_synced("sheet2", "hash123")


class TestConfigLoading:
    """Test configuration loading from YAML."""

    def test_loads_valid_config(self):
        """Should load sheet configs from YAML."""
        from api.services.gsheet_sync import GSheetSyncService

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            config_content = """
sync_enabled: true
sheets:
  - sheet_id: "test123"
    name: "Test Sheet"
    account: "personal"
    range: "Form Responses 1"
    timestamp_column: "Timestamp"
    outputs:
      rolling_document:
        enabled: true
        path: "Journal.md"
"""
            config_path.write_text(config_content)

            with patch("api.services.gsheet_sync.CONFIG_PATH", config_path):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            assert len(service.configs) == 1
            assert service.configs[0].sheet_id == "test123"
            assert service.configs[0].name == "Test Sheet"

    def test_handles_missing_config(self):
        """Should handle missing config file gracefully."""
        from api.services.gsheet_sync import GSheetSyncService

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            assert len(service.configs) == 0

    def test_skips_disabled_sync(self):
        """Should skip loading when sync_enabled is false."""
        from api.services.gsheet_sync import GSheetSyncService

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            vault_path = Path(tmpdir) / "vault"
            vault_path.mkdir()

            config_content = """
sync_enabled: false
sheets:
  - sheet_id: "test123"
    name: "Test Sheet"
"""
            config_path.write_text(config_content)

            with patch("api.services.gsheet_sync.CONFIG_PATH", config_path):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            assert len(service.configs) == 0


class TestDailyNoteAppend:
    """Test appending to daily notes."""

    def test_appends_to_existing_section(self):
        """Should append content under existing section header."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig, JournalEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            daily_note = vault_path / "Daily Notes" / "2025-01-28.md"
            daily_note.parent.mkdir(parents=True)
            daily_note.write_text("""---
tags:
  - dailyNote
---

## Notes

Some notes here.

## Meetings

- Meeting 1

## Tasks

- Task 1
""")

            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            config = SheetConfig(
                sheet_id="test",
                name="Test",
                account="personal",
                range="Sheet1",
                timestamp_column="Timestamp",
                outputs={
                    "daily_notes": {
                        "enabled": True,
                        "path_pattern": "Daily Notes/{date}.md",
                        "section_header": "## Daily Journal",
                        "insert_after": "## Meetings",
                        "create_if_missing": False,
                    }
                },
                field_mappings={},
            )

            entry = JournalEntry(
                row_hash="abc123",
                timestamp=datetime.now(),
                date_str="2025-01-28",
                fields={"Mood": "Great"},
                raw_row={},
            )

            service._append_to_daily_note(entry, config)

            content = daily_note.read_text()
            assert "## Daily Journal" in content
            assert "**Mood:** Great" in content
            # Section should be after Meetings but before Tasks
            meetings_idx = content.find("## Meetings")
            journal_idx = content.find("## Daily Journal")
            tasks_idx = content.find("## Tasks")
            assert meetings_idx < journal_idx < tasks_idx

    def test_skips_if_file_missing_and_create_disabled(self):
        """Should skip if daily note doesn't exist and create_if_missing is False."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig, JournalEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            daily_note = vault_path / "Daily Notes" / "2025-01-28.md"

            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            config = SheetConfig(
                sheet_id="test",
                name="Test",
                account="personal",
                range="Sheet1",
                timestamp_column="Timestamp",
                outputs={
                    "daily_notes": {
                        "enabled": True,
                        "path_pattern": "Daily Notes/{date}.md",
                        "section_header": "## Daily Journal",
                        "create_if_missing": False,
                    }
                },
                field_mappings={},
            )

            entry = JournalEntry(
                row_hash="abc123",
                timestamp=datetime.now(),
                date_str="2025-01-28",
                fields={"Mood": "Great"},
                raw_row={},
            )

            # Should not raise, just skip
            service._append_to_daily_note(entry, config)

            # File should not exist
            assert not daily_note.exists()

    def test_avoids_duplicate_entries(self):
        """Should not add same entry twice."""
        from api.services.gsheet_sync import GSheetSyncService, SheetConfig, JournalEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            daily_note = vault_path / "Daily Notes" / "2025-01-28.md"
            daily_note.parent.mkdir(parents=True)
            daily_note.write_text("""---
tags:
  - dailyNote
---

## Meetings

- Meeting 1
""")

            with patch("api.services.gsheet_sync.CONFIG_PATH", Path(tmpdir) / "nonexistent.yaml"):
                with patch("api.services.gsheet_sync.get_gsheet_sync_db_path", return_value=str(Path(tmpdir) / "test.db")):
                    service = GSheetSyncService(vault_path=vault_path)

            config = SheetConfig(
                sheet_id="test",
                name="Test",
                account="personal",
                range="Sheet1",
                timestamp_column="Timestamp",
                outputs={
                    "daily_notes": {
                        "enabled": True,
                        "path_pattern": "Daily Notes/{date}.md",
                        "section_header": "## Daily Journal",
                        "insert_after": "## Meetings",
                        "create_if_missing": False,
                    }
                },
                field_mappings={},
            )

            entry = JournalEntry(
                row_hash="abc123",
                timestamp=datetime.now(),
                date_str="2025-01-28",
                fields={"Mood": "Great"},
                raw_row={},
            )

            # Append twice
            service._append_to_daily_note(entry, config)
            service._append_to_daily_note(entry, config)

            content = daily_note.read_text()
            # Should only appear once
            assert content.count("**Mood:** Great") == 1
