"""
Google Sheets to Obsidian sync service.

Syncs data from Google Sheets (e.g., form responses) to Markdown files in the vault.
Supports both a rolling document with all entries and appending to daily notes.
"""
import hashlib
import json
import logging
import re
import sqlite3
import yaml
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.services.sheets import get_sheets_service
from api.services.google_auth import GoogleAccount
from config.settings import settings

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "gsheet_sync.yaml"


def get_gsheet_sync_db_path() -> str:
    """Get the path to the gsheet sync state database."""
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "gsheet_sync.db")


@dataclass
class SheetConfig:
    """Configuration for a single sheet to sync."""
    sheet_id: str
    name: str
    account: str
    range: str
    timestamp_column: str
    outputs: dict
    field_mappings: dict  # Maps column names to clean YAML keys

    @classmethod
    def from_dict(cls, data: dict) -> "SheetConfig":
        return cls(
            sheet_id=data["sheet_id"],
            name=data.get("name", "Unnamed Sheet"),
            account=data.get("account", "personal"),
            range=data.get("range", "Sheet1"),
            timestamp_column=data.get("timestamp_column", "Timestamp"),
            outputs=data.get("outputs", {}),
            field_mappings=data.get("field_mappings", {}),
        )


@dataclass
class JournalEntry:
    """A single parsed entry from the sheet."""
    row_hash: str
    timestamp: datetime
    date_str: str  # YYYY-MM-DD
    fields: dict[str, str]  # column_name -> value
    raw_row: dict  # Original row data


class GSheetSyncService:
    """
    Syncs Google Sheets data to Obsidian vault.

    Features:
    - Tracks synced rows in SQLite to avoid duplicates
    - Generates rolling document with all entries
    - Appends to existing daily notes
    """

    def __init__(self, vault_path: Optional[Path] = None, db_path: Optional[str] = None):
        """
        Initialize the sync service.

        Args:
            vault_path: Path to Obsidian vault (defaults to settings.vault_path)
            db_path: Path to SQLite database (defaults to data/gsheet_sync.db)
        """
        self.vault_path = vault_path or settings.vault_path
        self.db_path = db_path or get_gsheet_sync_db_path()
        self.configs: list[SheetConfig] = []
        self._load_config()
        self._init_db()

    def _load_config(self):
        """Load sync configuration from YAML file."""
        if not CONFIG_PATH.exists():
            logger.info(f"GSheet sync config not found at {CONFIG_PATH}, skipping")
            return

        try:
            with open(CONFIG_PATH) as f:
                config = yaml.safe_load(f)

            if not config:
                logger.info("GSheet sync config is empty")
                return

            if not config.get("sync_enabled", True):
                logger.info("GSheet sync is disabled in config")
                return

            for sheet in config.get("sheets", []):
                if not sheet.get("sheet_id"):
                    logger.warning(f"Skipping sheet without sheet_id: {sheet}")
                    continue
                self.configs.append(SheetConfig.from_dict(sheet))

            logger.info(f"Loaded {len(self.configs)} GSheet sync configs")

        except Exception as e:
            logger.error(f"Failed to load GSheet sync config: {e}")

    def _init_db(self):
        """Initialize SQLite database for tracking synced rows."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS synced_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sheet_id TEXT NOT NULL,
                    row_hash TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    raw_data TEXT,
                    UNIQUE(sheet_id, row_hash)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_synced_rows_sheet
                ON synced_rows(sheet_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_synced_rows_date
                ON synced_rows(entry_date)
            """)
            conn.commit()

    def _compute_row_hash(self, row: dict, config: SheetConfig) -> str:
        """
        Compute unique hash for a row.

        Uses timestamp + first data value to create uniqueness.
        """
        timestamp = row.get(config.timestamp_column, "")
        # Get first non-timestamp value for additional uniqueness
        other_values = [v for k, v in row.items() if k != config.timestamp_column and v]
        first_value = other_values[0] if other_values else ""
        content = f"{timestamp}|{first_value}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _is_row_synced(self, sheet_id: str, row_hash: str) -> bool:
        """Check if a row has already been synced."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM synced_rows WHERE sheet_id = ? AND row_hash = ?",
                (sheet_id, row_hash)
            )
            return cursor.fetchone() is not None

    def _mark_row_synced(self, sheet_id: str, entry: JournalEntry):
        """Mark a row as synced in the database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO synced_rows
                   (sheet_id, row_hash, entry_date, raw_data)
                   VALUES (?, ?, ?, ?)""",
                (sheet_id, entry.row_hash, entry.date_str, json.dumps(entry.raw_row))
            )
            conn.commit()

    def _get_all_synced_entries(self, sheet_id: str) -> list[dict]:
        """Get all previously synced entries for rebuilding rolling doc."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT entry_date, raw_data FROM synced_rows
                   WHERE sheet_id = ? ORDER BY entry_date DESC""",
                (sheet_id,)
            )
            return [
                {"date": row[0], "data": json.loads(row[1])}
                for row in cursor.fetchall()
            ]

    def _parse_timestamp(self, timestamp_str: str) -> tuple[datetime, str]:
        """
        Parse timestamp from Google Forms format.

        Google Forms typically uses: M/D/YYYY H:MM:SS or MM/DD/YYYY HH:MM:SS

        Returns:
            Tuple of (datetime, date_str as YYYY-MM-DD)
        """
        # Common Google Forms formats
        formats = [
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %I:%M:%S %p",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(timestamp_str, fmt)
                return dt, dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # Fallback: try to extract date from string
        logger.warning(f"Could not parse timestamp: {timestamp_str}, using today")
        now = datetime.now()
        return now, now.strftime("%Y-%m-%d")

    def _parse_row(self, row: dict, config: SheetConfig) -> Optional[JournalEntry]:
        """
        Parse a row into a JournalEntry.

        Args:
            row: Dict of column_name -> value
            config: Sheet configuration

        Returns:
            JournalEntry or None if row is invalid/empty
        """
        timestamp_str = row.get(config.timestamp_column, "")
        if not timestamp_str:
            return None

        timestamp, date_str = self._parse_timestamp(timestamp_str)
        row_hash = self._compute_row_hash(row, config)

        # Extract all fields except timestamp
        fields = {k: v for k, v in row.items() if k != config.timestamp_column and v}

        if not fields:
            return None

        return JournalEntry(
            row_hash=row_hash,
            timestamp=timestamp,
            date_str=date_str,
            fields=fields,
            raw_row=row,
        )

    def _format_entry_markdown(self, entry: JournalEntry) -> str:
        """Format a single entry as Markdown."""
        lines = []
        for label, value in entry.fields.items():
            # Clean up the label (remove "?" etc)
            clean_label = label.rstrip("?").strip()
            # Check if value looks like a list (comma-separated)
            if "," in value and len(value.split(",")) > 2:
                lines.append(f"**{clean_label}:**")
                for item in value.split(","):
                    item = item.strip()
                    if item:
                        lines.append(f"- {item}")
            else:
                lines.append(f"**{clean_label}:** {value}")
        return "\n".join(lines)

    def _update_rolling_document(self, config: SheetConfig, new_entries: list[JournalEntry]):
        """
        Update the rolling document with all entries.

        Rebuilds the entire document each time for consistency.
        """
        rolling_config = config.outputs.get("rolling_document", {})
        if not rolling_config.get("enabled", False):
            return

        output_path = rolling_config.get("path")
        if not output_path:
            return

        # Get all synced entries (including ones we just synced)
        all_entries = self._get_all_synced_entries(config.sheet_id)

        # Also add new entries that aren't yet in DB
        existing_dates = {e["date"] for e in all_entries}
        for entry in new_entries:
            if entry.date_str not in existing_dates:
                all_entries.append({
                    "date": entry.date_str,
                    "data": entry.raw_row
                })

        # Sort by date descending (most recent first)
        all_entries.sort(key=lambda x: x["date"], reverse=True)

        # Build the document
        now = datetime.now(timezone.utc).isoformat()
        sheet_url = f"https://docs.google.com/spreadsheets/d/{config.sheet_id}"

        doc_lines = [
            "---",
            "gsheet_sync: true",
            f'sheet_id: "{config.sheet_id}"',
            f'last_synced: "{now}"',
            "---",
            "",
            "> [!info] Auto-Synced from Google Forms",
            "> This file is automatically synced. **Do not edit locally.**",
            f"> [View Source Sheet]({sheet_url})",
            "",
        ]

        # Group entries by date
        current_date = None
        for entry_data in all_entries:
            date = entry_data["date"]
            row = entry_data["data"]

            if date != current_date:
                if current_date is not None:
                    doc_lines.append("")
                    doc_lines.append("---")
                    doc_lines.append("")
                doc_lines.append(f"## {date}")
                doc_lines.append("")
                current_date = date

            # Format the entry
            entry = self._parse_row(row, config)
            if entry:
                doc_lines.append(self._format_entry_markdown(entry))
                doc_lines.append("")

        # Write to vault
        file_path = Path(self.vault_path) / output_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("\n".join(doc_lines), encoding="utf-8")
        logger.info(f"Updated rolling document: {output_path} ({len(all_entries)} entries)")

    def _append_to_daily_note(self, entry: JournalEntry, config: SheetConfig):
        """
        Append entry to the corresponding daily note.

        Only appends if the daily note exists (unless create_if_missing is True).
        """
        daily_config = config.outputs.get("daily_notes", {})
        if not daily_config.get("enabled", False):
            return

        path_pattern = daily_config.get("path_pattern", "Daily Notes/{date}.md")
        section_header = daily_config.get("section_header", "## Daily Journal")
        insert_after = daily_config.get("insert_after")
        create_if_missing = daily_config.get("create_if_missing", False)

        # Build the file path
        note_path = path_pattern.replace("{date}", entry.date_str)
        file_path = Path(self.vault_path) / note_path

        # Check if file exists
        if not file_path.exists():
            if not create_if_missing:
                logger.debug(f"Daily note not found, skipping: {note_path}")
                return
            else:
                # Create minimal daily note
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(
                    f"---\ntags:\n  - dailyNote\n---\n\n{section_header}\n\n",
                    encoding="utf-8"
                )

        # Read existing content
        content = file_path.read_text(encoding="utf-8")

        # Check if we already added this section
        if section_header in content:
            # Check if this entry's date is already there
            # (simple check - we add date as comment)
            entry_marker = f"<!-- gsheet:{entry.row_hash} -->"
            if entry_marker in content:
                logger.debug(f"Entry already in daily note: {entry.date_str}")
                return

            # Find the section and append to it
            section_idx = content.find(section_header)
            section_end = section_idx + len(section_header)

            # Find the next section (##) or end of file
            next_section = content.find("\n## ", section_end)
            if next_section == -1:
                next_section = len(content)

            # Insert before next section
            entry_content = f"\n{entry_marker}\n{self._format_entry_markdown(entry)}\n"
            content = content[:next_section] + entry_content + content[next_section:]

        else:
            # Need to add the section
            if insert_after and insert_after in content:
                # Find the insert_after section and add after it
                after_idx = content.find(insert_after)
                # Find the next section after insert_after
                next_section = content.find("\n## ", after_idx + len(insert_after))
                if next_section == -1:
                    next_section = len(content)

                entry_content = f"\n\n{section_header}\n\n<!-- gsheet:{entry.row_hash} -->\n{self._format_entry_markdown(entry)}\n"
                content = content[:next_section] + entry_content + content[next_section:]
            else:
                # Append at end
                entry_content = f"\n\n{section_header}\n\n<!-- gsheet:{entry.row_hash} -->\n{self._format_entry_markdown(entry)}\n"
                content = content.rstrip() + entry_content

        # Write back
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Appended to daily note: {note_path}")

    def _create_journal_note(self, entry: JournalEntry, config: SheetConfig):
        """
        Create a standalone journal note with YAML frontmatter.

        Each entry gets its own note file with typed frontmatter for Dataview queries.
        """
        journal_config = config.outputs.get("journal_notes", {})
        if not journal_config.get("enabled", False):
            return

        folder = journal_config.get("folder", "Journal")

        # Build the file path
        note_path = f"{folder}/{entry.date_str}.md"
        file_path = Path(self.vault_path) / note_path

        # Check if already exists (don't overwrite)
        if file_path.exists():
            # Check if this entry is already in the file
            content = file_path.read_text(encoding="utf-8")
            if f"row_hash: {entry.row_hash}" in content:
                logger.debug(f"Journal note already has this entry: {note_path}")
                return

        # Build YAML frontmatter with mapped fields
        frontmatter = {
            "date": entry.date_str,
            "type": "journal",
            "row_hash": entry.row_hash,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        # Map fields to clean YAML keys
        for column_name, value in entry.fields.items():
            # Use field_mappings if available, otherwise auto-generate key
            if column_name in config.field_mappings:
                yaml_key = config.field_mappings[column_name]
            else:
                # Auto-generate: lowercase, replace spaces with underscores
                yaml_key = column_name.lower().replace(" ", "_").replace("?", "").strip("_")

            # Try to convert to appropriate type
            parsed_value = self._parse_field_value(value)
            frontmatter[yaml_key] = parsed_value

        # Build the note content
        lines = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, bool):
                lines.append(f"{key}: {str(value).lower()}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key}: {value}")
            elif isinstance(value, list):
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {item}")
            else:
                # String - quote if it contains special chars
                if any(c in str(value) for c in [':', '#', '{', '}', '[', ']', ',', '&', '*', '!', '|', '>', "'", '"', '%', '@', '`']):
                    lines.append(f'{key}: "{value}"')
                else:
                    lines.append(f"{key}: {value}")
        lines.append("---")
        lines.append("")

        # Add a simple body section
        lines.append(f"# Journal Entry - {entry.date_str}")
        lines.append("")

        # Add readable summary
        for column_name, value in entry.fields.items():
            clean_label = column_name.rstrip("?").strip()
            lines.append(f"**{clean_label}:** {value}")
        lines.append("")

        # Write to vault
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Created journal note: {note_path}")

    def _parse_field_value(self, value: str):
        """
        Parse a field value to appropriate Python type for YAML.

        Only converts pure numbers. Keeps categorical values (like "3-4 drinks") as strings.
        """
        if not value:
            return ""

        # Check for boolean
        lower_val = value.lower().strip()
        if lower_val in ("yes", "true"):
            return True
        if lower_val in ("no", "false"):
            return False

        # Only convert to int if it's PURELY a number (no other text)
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError:
            pass

        # Only convert to float if it's PURELY a number
        try:
            return float(stripped)
        except ValueError:
            pass

        # Keep everything else as string (including "3-4 drinks", "None", etc.)
        return value

    def _sync_sheet(self, config: SheetConfig) -> dict:
        """
        Sync a single sheet.

        Returns:
            Stats dict with new_rows, skipped_rows counts
        """
        # Get account type
        account = GoogleAccount.WORK if config.account == "work" else GoogleAccount.PERSONAL
        sheets = get_sheets_service(account)

        # Fetch all rows
        logger.info(f"Fetching sheet: {config.name} ({config.sheet_id})")
        rows = sheets.get_sheet_with_headers(config.sheet_id, config.range)
        logger.info(f"Found {len(rows)} rows in sheet")

        # Parse and filter new entries
        new_entries = []
        skipped = 0

        for row in rows:
            entry = self._parse_row(row, config)
            if not entry:
                skipped += 1
                continue

            if self._is_row_synced(config.sheet_id, entry.row_hash):
                skipped += 1
                continue

            new_entries.append(entry)

        logger.info(f"Found {len(new_entries)} new entries, {skipped} skipped")

        if not new_entries:
            # Still update rolling doc in case it needs rebuild
            self._update_rolling_document(config, [])
            return {"new_rows": 0, "skipped_rows": skipped}

        # Update rolling document (includes all entries)
        self._update_rolling_document(config, new_entries)

        # Create standalone journal notes with YAML frontmatter
        for entry in new_entries:
            try:
                self._create_journal_note(entry, config)
            except Exception as e:
                logger.error(f"Failed to create journal note for {entry.date_str}: {e}")

        # Append to daily notes
        for entry in new_entries:
            try:
                self._append_to_daily_note(entry, config)
            except Exception as e:
                logger.error(f"Failed to append to daily note for {entry.date_str}: {e}")

        # Mark entries as synced
        for entry in new_entries:
            self._mark_row_synced(config.sheet_id, entry)

        return {"new_rows": len(new_entries), "skipped_rows": skipped}

    def sync_all(self) -> dict:
        """
        Sync all configured sheets.

        Returns:
            Stats dict with counts: synced, failed, skipped, sheets
        """
        stats = {
            "synced": 0,
            "failed": 0,
            "skipped": 0,
            "sheets": [],
        }

        if not self.configs:
            logger.info("No sheets configured for GSheet sync")
            return stats

        for config in self.configs:
            try:
                result = self._sync_sheet(config)
                stats["synced"] += result["new_rows"]
                stats["skipped"] += result["skipped_rows"]
                stats["sheets"].append({
                    "name": config.name,
                    "sheet_id": config.sheet_id,
                    "status": "success",
                    "new_rows": result["new_rows"],
                    "skipped_rows": result["skipped_rows"],
                })
                logger.info(f"Synced {config.name}: {result['new_rows']} new, {result['skipped_rows']} skipped")

            except Exception as e:
                stats["failed"] += 1
                stats["sheets"].append({
                    "name": config.name,
                    "sheet_id": config.sheet_id,
                    "status": "failed",
                    "error": str(e),
                })
                logger.error(f"Failed to sync {config.name}: {e}")

        logger.info(f"GSheet sync complete: {stats['synced']} synced, {stats['failed']} failed")
        return stats


# Singleton instance
_gsheet_sync_service: Optional[GSheetSyncService] = None


def get_gsheet_sync_service() -> GSheetSyncService:
    """Get or create the GSheet sync service singleton."""
    global _gsheet_sync_service
    if _gsheet_sync_service is None:
        _gsheet_sync_service = GSheetSyncService()
    return _gsheet_sync_service


def sync_gsheets() -> dict:
    """
    Convenience function for nightly sync.

    Returns:
        Stats dict from sync operation
    """
    service = get_gsheet_sync_service()
    return service.sync_all()
