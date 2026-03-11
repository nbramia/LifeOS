"""Tests for Apple data pipeline: contacts plist parsing, staleness alerting."""
import json
import logging
import plistlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Contacts plist parsing
# ---------------------------------------------------------------------------

class TestContactsPlistParsing:
    """Test _parse_abcdp_labeled and _parse_abcdp_contact from apple_data_export."""

    def _get_parse_funcs(self):
        """Import the parsing functions."""
        from scripts.apple_data_export import _parse_abcdp_labeled, _parse_abcdp_contact
        return _parse_abcdp_labeled, _parse_abcdp_contact

    def test_parse_labeled_email(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {
            "identifiers": ["abc-123"],
            "labels": ["_$!<Home>!$_"],
            "primary": "abc-123",
            "values": ["jane@example.com"],
        }
        result = parse_labeled(field)
        assert result == [{"label": "Home", "value": "jane@example.com"}]

    def test_parse_labeled_multiple_phones(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {
            "identifiers": ["a", "b"],
            "labels": ["_$!<Mobile>!$_", "_$!<Work>!$_"],
            "primary": "a",
            "values": ["5551234567", "5559876543"],
        }
        result = parse_labeled(field)
        assert len(result) == 2
        assert result[0]["label"] == "Mobile"
        assert result[1]["value"] == "5559876543"

    def test_parse_labeled_none(self):
        parse_labeled, _ = self._get_parse_funcs()
        assert parse_labeled(None) == []
        assert parse_labeled({}) == []

    def test_parse_labeled_empty_values_skipped(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {"values": ["", "valid@test.com"], "labels": ["", ""]}
        result = parse_labeled(field)
        assert len(result) == 1
        assert result[0]["value"] == "valid@test.com"

    def test_parse_labeled_missing_label_defaults_to_other(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {"values": ["test@test.com"], "labels": [""]}
        result = parse_labeled(field)
        assert result[0]["label"] == "other"

    def test_parse_labeled_fewer_labels_than_values(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {"values": ["a@b.com", "c@d.com"], "labels": ["_$!<Home>!$_"]}
        result = parse_labeled(field)
        assert len(result) == 2
        assert result[0]["label"] == "Home"
        assert result[1]["label"] == "other"

    def test_parse_labeled_none_labels(self):
        parse_labeled, _ = self._get_parse_funcs()
        field = {"values": ["test@test.com"], "labels": None}
        result = parse_labeled(field)
        assert len(result) == 1
        assert result[0]["label"] == "other"

    def test_parse_contact_first_name_only(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {"First": "Jane"}
        result = parse_contact(plist, "uuid-fn")
        assert result["full_name"] == "Jane"
        assert result["family_name"] == ""

    def test_parse_contact_date_birthday(self):
        """Birthday stored as date (not datetime) should still be captured."""
        from datetime import date
        _, parse_contact = self._get_parse_funcs()
        plist = {"First": "Test", "Birthday": date(1990, 6, 15)}
        result = parse_contact(plist, "uuid-db")
        assert result["birthday"] == "1990-06-15"

    def test_parse_contact_basic(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {"First": "Jane", "Last": "Doe", "Organization": "Acme Corp"}
        result = parse_contact(plist, "test-uuid-123")
        assert result["identifier"] == "test-uuid-123"
        assert result["given_name"] == "Jane"
        assert result["family_name"] == "Doe"
        assert result["full_name"] == "Jane Doe"
        assert result["organization"] == "Acme Corp"

    def test_parse_contact_org_only(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {"Organization": "Some Company"}
        result = parse_contact(plist, "uuid-1")
        assert result["full_name"] == "Some Company"
        assert result["given_name"] == ""

    def test_parse_contact_no_name_skipped(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {"ABPersonFlags": 0}
        result = parse_contact(plist, "uuid-2")
        assert result is None

    def test_parse_contact_with_email_and_phone(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {
            "First": "John",
            "Last": "Smith",
            "Email": {
                "identifiers": ["e1"],
                "labels": ["_$!<Home>!$_"],
                "values": ["john@example.com"],
            },
            "Phone": {
                "identifiers": ["p1"],
                "labels": ["_$!<Mobile>!$_"],
                "values": ["5551234567"],
            },
        }
        result = parse_contact(plist, "uuid-3")
        assert len(result["emails"]) == 1
        assert result["emails"][0]["value"] == "john@example.com"
        assert len(result["phones"]) == 1

    def test_parse_contact_with_birthday(self):
        _, parse_contact = self._get_parse_funcs()
        bday = datetime(1990, 6, 15, tzinfo=timezone.utc)
        plist = {"First": "Test", "Birthday": bday}
        result = parse_contact(plist, "uuid-4")
        assert result["birthday"] == bday.isoformat()

    def test_parse_contact_without_birthday(self):
        _, parse_contact = self._get_parse_funcs()
        plist = {"First": "Test"}
        result = parse_contact(plist, "uuid-5")
        assert result["birthday"] is None


class TestContactsExport:
    """Test the full export_contacts function with synthetic .abcdp files."""

    def test_export_contacts_reads_abcdp_files(self, tmp_path):
        """Create synthetic .abcdp files and verify export_contacts reads them."""
        from scripts.apple_data_export import export_contacts

        export_dir = tmp_path / "exports"
        export_dir.mkdir()

        # The function uses: Path.home() / "Library" / "Application Support" / "AddressBook"
        lib_ab = tmp_path / "Library" / "Application Support" / "AddressBook"
        sources_dir = lib_ab / "Sources" / "SOURCE-1" / "Metadata"
        sources_dir.mkdir(parents=True)

        for i, (first, last, email) in enumerate([
            ("Alice", "Anderson", "alice@example.com"),
            ("Bob", "Brown", "bob@example.com"),
        ]):
            plist_data = {"First": first, "Last": last, "Email": {
                "identifiers": [f"e{i}"], "labels": [""], "values": [email],
            }}
            with open(sources_dir / f"UUID-{i:04d}:ABPerson.abcdp", "wb") as f:
                plistlib.dump(plist_data, f)

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["status"] == "ok"
        assert result["count"] == 2

        # Verify the output JSON
        with open(export_dir / "contacts.json") as f:
            data = json.load(f)
        assert data["count"] == 2
        names = {c["full_name"] for c in data["contacts"]}
        assert names == {"Alice Anderson", "Bob Brown"}

    def test_export_contacts_deduplicates(self, tmp_path):
        """Same UUID in multiple sources should only appear once."""
        from scripts.apple_data_export import export_contacts

        lib_ab = tmp_path / "Library" / "Application Support" / "AddressBook"
        export_dir = tmp_path / "exports"
        export_dir.mkdir()

        # Same UUID in two different sources
        for src in ["SOURCE-A", "SOURCE-B"]:
            meta_dir = lib_ab / "Sources" / src / "Metadata"
            meta_dir.mkdir(parents=True)
            plist_data = {"First": "Charlie", "Last": "Clone"}
            with open(meta_dir / "SAME-UUID:ABPerson.abcdp", "wb") as f:
                plistlib.dump(plist_data, f)

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["count"] == 1

    def test_export_contacts_corrupt_file_skipped(self, tmp_path):
        """Corrupt .abcdp files should be skipped with a warning, not crash."""
        from scripts.apple_data_export import export_contacts

        lib_ab = tmp_path / "Library" / "Application Support" / "AddressBook"
        export_dir = tmp_path / "exports"
        export_dir.mkdir()

        sources_dir = lib_ab / "Sources" / "SOURCE-1" / "Metadata"
        sources_dir.mkdir(parents=True)

        # One valid, one corrupt
        valid = {"First": "Valid", "Last": "Contact"}
        with open(sources_dir / "VALID-UUID:ABPerson.abcdp", "wb") as f:
            plistlib.dump(valid, f)
        with open(sources_dir / "BAD-UUID:ABPerson.abcdp", "wb") as f:
            f.write(b"this is not a plist")

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["status"] == "ok"
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# Staleness alerting
# ---------------------------------------------------------------------------

class TestStalenessAlerting:
    """Test check_manifest staleness detection."""

    def _write_manifest(self, import_dir: Path, exported_at: str):
        import_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"exported_at": exported_at, "hostname": "test-host", "results": {}}
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

    def test_fresh_data_no_warning(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        self._write_manifest(import_dir, fresh.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.INFO):
                result = check_manifest()

        assert result is not None
        assert "fresh" in caplog.text.lower()
        assert "WARNING" not in caplog.text.split("fresh")[0]  # No warning before "fresh"

    def test_stale_48h_warning(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        stale = datetime.now(timezone.utc) - timedelta(hours=50)
        self._write_manifest(import_dir, stale.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.WARNING):
                check_manifest()

        assert any("50h old" in r.message for r in caplog.records if r.levelno >= logging.WARNING)

    def test_stale_7d_critical(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        very_stale = datetime.now(timezone.utc) - timedelta(days=10)
        self._write_manifest(import_dir, very_stale.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.DEBUG):
                check_manifest()

        assert any(r.levelno >= logging.CRITICAL for r in caplog.records)
        assert any("10 days old" in r.message for r in caplog.records)

    def test_missing_manifest(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir()

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            result = check_manifest()

        assert result is None

    def test_bad_timestamp_handled(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, "not-a-date")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert any("Cannot parse" in r.message for r in caplog.records)

    def test_naive_timestamp_treated_as_utc(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        # Naive timestamp (no timezone) — should still compute staleness, not error
        naive = datetime.now(timezone.utc) - timedelta(hours=1)
        self._write_manifest(import_dir, naive.strftime("%Y-%m-%dT%H:%M:%S"))

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.INFO):
                result = check_manifest()

        assert result is not None
        assert "fresh" in caplog.text.lower()
        assert not any("Cannot parse" in r.message for r in caplog.records)
