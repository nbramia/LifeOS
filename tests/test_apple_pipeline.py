"""Tests for Apple data pipeline: contacts plist parsing, phone import, staleness alerting."""
import json
import logging
import plistlib
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Export/import directory name reconciliation — issue #785
# ---------------------------------------------------------------------------

class TestExportImportDirectoryNamesAgree:
    """Export and import must agree on the shared handoff directory name so
    a single-machine deployment (both steps on the same host, no rsync)
    works with no manual symlink."""

    def test_export_and_import_dirs_resolve_to_the_same_path(self):
        from scripts.apple_data_export import EXPORT_DIR
        from scripts.apple_data_import import IMPORT_DIR

        assert EXPORT_DIR == IMPORT_DIR


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
# Contacts .abcddb (SQLite AddressBook) parsing — issue #514
# ---------------------------------------------------------------------------

def _build_abcddb(
    path: Path,
    contacts: list[dict],
    contact_ent: int = 22,
    other_ent: int = 19,
    other_ent_name: str = "ABCDGroup",
):
    """Build a synthetic AddressBook-v22.abcddb with the real table/column
    shape (verified read-only against the real schema) but entirely
    synthetic data. Each item in `contacts` is a dict with keys: pk, uid,
    first, last, org, nickname, job_title, department, birthday (seconds
    since Core Data epoch or None), phones (list of (label, value)),
    emails (list of (label, value)).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR)")
    conn.execute("INSERT INTO Z_PRIMARYKEY VALUES (?, 'ABCDContact')", (contact_ent,))
    conn.execute("INSERT INTO Z_PRIMARYKEY VALUES (?, ?)", (other_ent, other_ent_name))
    conn.execute(
        """CREATE TABLE ZABCDRECORD (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZUNIQUEID VARCHAR,
            ZFIRSTNAME VARCHAR, ZLASTNAME VARCHAR, ZORGANIZATION VARCHAR,
            ZNICKNAME VARCHAR, ZJOBTITLE VARCHAR, ZDEPARTMENT VARCHAR,
            ZBIRTHDAY TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZLABEL VARCHAR, ZFULLNUMBER VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZLABEL VARCHAR, ZADDRESS VARCHAR)"
    )

    for c in contacts:
        conn.execute(
            """INSERT INTO ZABCDRECORD
               (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION,
                ZNICKNAME, ZJOBTITLE, ZDEPARTMENT, ZBIRTHDAY)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                c["pk"], c.get("ent", contact_ent), c["uid"],
                c.get("first"), c.get("last"), c.get("org"),
                c.get("nickname"), c.get("job_title"), c.get("department"),
                c.get("birthday"),
            ),
        )
        for label, value in c.get("phones", []):
            conn.execute(
                "INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZLABEL, ZFULLNUMBER) VALUES (?,?,?)",
                (c["pk"], label, value),
            )
        for label, value in c.get("emails", []):
            conn.execute(
                "INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZLABEL, ZADDRESS) VALUES (?,?,?)",
                (c["pk"], label, value),
            )

    # An unrelated group row (different Z_ENT) with no phone/email — must
    # never surface as a contact.
    conn.execute(
        "INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME) VALUES (999, ?, 'group-uid', 'Not A Person')",
        (other_ent,),
    )
    conn.commit()
    conn.close()


class TestAbcddbContactsParsing:
    """Test _fetch_abcddb_contacts against a synthetic AddressBook-v22.abcddb."""

    def test_fetch_basic_contact_with_phone_and_email(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        db_path = tmp_path / "AddressBook-v22.abcddb"
        _build_abcddb(db_path, [
            {
                "pk": 1, "uid": "SYNTH-UID-1", "first": "Alex", "last": "Chen",
                "org": "Example Corp", "job_title": "Engineer",
                "phones": [("_$!<Mobile>!$_", "+15555550101")],
                "emails": [("_$!<Work>!$_", "alex.chen@example.com")],
            },
        ])

        contacts = _fetch_abcddb_contacts(db_path)

        assert len(contacts) == 1
        c = contacts[0]
        assert c["full_name"] == "Alex Chen"
        assert c["organization"] == "Example Corp"
        assert c["job_title"] == "Engineer"
        assert c["identifier"] == "abcddb:SYNTH-UID-1"
        assert c["phones"] == [{"label": "Mobile", "value": "+15555550101"}]
        assert c["emails"] == [{"label": "Work", "value": "alex.chen@example.com"}]

    def test_fetch_excludes_non_contact_entity_rows(self, tmp_path):
        """Rows in ZABCDRECORD whose Z_ENT isn't the looked-up ABCDContact
        entity (groups, containers, ...) must not be treated as contacts."""
        from scripts.apple_data_export import _fetch_abcddb_contacts

        db_path = tmp_path / "AddressBook-v22.abcddb"
        _build_abcddb(db_path, [
            {"pk": 1, "uid": "SYNTH-UID-2", "first": "Priya", "last": "Kapoor"},
        ])

        contacts = _fetch_abcddb_contacts(db_path)

        names = {c["full_name"] for c in contacts}
        assert names == {"Priya Kapoor"}
        assert "Not A Person" not in names

    def test_fetch_org_only_contact(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        db_path = tmp_path / "AddressBook-v22.abcddb"
        _build_abcddb(db_path, [
            {"pk": 1, "uid": "SYNTH-UID-3", "org": "Fictional Widgets Inc"},
        ])

        contacts = _fetch_abcddb_contacts(db_path)

        assert len(contacts) == 1
        assert contacts[0]["full_name"] == "Fictional Widgets Inc"
        assert contacts[0]["given_name"] == ""

    def test_fetch_no_name_or_org_skipped(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        db_path = tmp_path / "AddressBook-v22.abcddb"
        _build_abcddb(db_path, [
            {"pk": 1, "uid": "SYNTH-UID-4"},
        ])

        contacts = _fetch_abcddb_contacts(db_path)
        assert contacts == []

    def test_fetch_birthday_roundtrip(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        core_data_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        birthday_seconds = 100_000_000  # ~3.17 years after the epoch
        expected = (core_data_epoch + timedelta(seconds=birthday_seconds)).isoformat()

        db_path = tmp_path / "AddressBook-v22.abcddb"
        _build_abcddb(db_path, [
            {"pk": 1, "uid": "SYNTH-UID-5", "first": "Sam", "birthday": birthday_seconds},
        ])

        contacts = _fetch_abcddb_contacts(db_path)
        assert contacts[0]["birthday"] == expected

    def test_fetch_missing_file_returns_empty(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        contacts = _fetch_abcddb_contacts(tmp_path / "does-not-exist.abcddb")
        assert contacts == []

    def test_fetch_corrupt_db_returns_empty(self, tmp_path):
        from scripts.apple_data_export import _fetch_abcddb_contacts

        bad_db = tmp_path / "corrupt.abcddb"
        bad_db.write_bytes(b"not a sqlite file")

        contacts = _fetch_abcddb_contacts(bad_db)
        assert contacts == []


class TestContactsDedup:
    """Test _dedupe_contacts, the cross-source merge logic for #514."""

    def _contact(self, full_name="", org="", emails=None, phones=None, **extra):
        c = {
            "identifier": extra.pop("identifier", "id"),
            "given_name": extra.pop("given_name", ""),
            "family_name": extra.pop("family_name", ""),
            "full_name": full_name,
            "nickname": extra.pop("nickname", ""),
            "organization": org,
            "job_title": extra.pop("job_title", ""),
            "department": extra.pop("department", ""),
            "emails": emails or [],
            "phones": phones or [],
            "addresses": [],
            "social_profiles": [],
            "note": "",
            "image_available": False,
            "birthday": extra.pop("birthday", None),
        }
        c.update(extra)
        return c

    def test_merges_same_name_across_sources(self):
        from scripts.apple_data_export import _dedupe_contacts

        icloud = self._contact(
            identifier="icloud-1", full_name="Jordan Lee",
            phones=[{"label": "Mobile", "value": "+15555550111"}],
        )
        exchange = self._contact(
            identifier="exchange-1", full_name="Jordan Lee",
            emails=[{"label": "Work", "value": "jordan.lee@example.com"}],
        )

        result = _dedupe_contacts([icloud, exchange])

        assert len(result) == 1
        merged = result[0]
        assert merged["phones"] == [{"label": "Mobile", "value": "+15555550111"}]
        assert merged["emails"] == [{"label": "Work", "value": "jordan.lee@example.com"}]

    def test_merge_is_case_and_whitespace_insensitive(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(identifier="a", full_name="  Taylor Morgan  ")
        b = self._contact(identifier="b", full_name="taylor   morgan")

        result = _dedupe_contacts([a, b])
        assert len(result) == 1

    def test_does_not_merge_different_names(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(identifier="a", full_name="Morgan Reed")
        b = self._contact(identifier="b", full_name="Casey Reed")

        result = _dedupe_contacts([a, b])
        assert len(result) == 2

    def test_dedup_by_organization_when_no_name(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(identifier="a", org="Fictional Widgets Inc")
        b = self._contact(identifier="b", org="Fictional Widgets Inc")

        result = _dedupe_contacts([a, b])
        assert len(result) == 1

    def test_no_name_or_org_passes_through_unmerged(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(identifier="a")
        b = self._contact(identifier="b")

        result = _dedupe_contacts([a, b])
        assert len(result) == 2

    def test_dedup_backfills_empty_fields(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(identifier="a", full_name="Riley Park", job_title="")
        b = self._contact(identifier="b", full_name="Riley Park", job_title="Analyst")

        result = _dedupe_contacts([a, b])
        assert result[0]["job_title"] == "Analyst"

    def test_dedup_deduplicates_repeated_email_value(self):
        from scripts.apple_data_export import _dedupe_contacts

        a = self._contact(
            identifier="a", full_name="Nina Okafor",
            emails=[{"label": "Home", "value": "nina@example.com"}],
        )
        b = self._contact(
            identifier="b", full_name="Nina Okafor",
            emails=[{"label": "Work", "value": "NINA@EXAMPLE.COM"}],
        )

        result = _dedupe_contacts([a, b])
        assert len(result[0]["emails"]) == 1


class TestContactsExportAbcddbIntegration:
    """End-to-end export_contacts() coverage for the .abcddb path, including
    the .abcdp fallback and the both-empty error case (issue #514)."""

    def _lib_ab(self, tmp_path: Path) -> Path:
        return tmp_path / "Library" / "Application Support" / "AddressBook"

    def test_export_reads_abcddb_when_present(self, tmp_path):
        from scripts.apple_data_export import export_contacts

        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        lib_ab = self._lib_ab(tmp_path)

        _build_abcddb(lib_ab / "AddressBook-v22.abcddb", [])  # empty root DB
        _build_abcddb(
            lib_ab / "Sources" / "SRC-1" / "AddressBook-v22.abcddb",
            [
                {
                    "pk": 1, "uid": "SRC1-UID-1", "first": "Morgan", "last": "Diallo",
                    "phones": [("_$!<Mobile>!$_", "+15555550199")],
                    "emails": [("_$!<Home>!$_", "morgan.diallo@example.com")],
                },
            ],
        )

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["status"] == "ok"
        assert result["count"] == 1

        with open(export_dir / "contacts.json") as f:
            data = json.load(f)
        contact = data["contacts"][0]
        assert contact["full_name"] == "Morgan Diallo"
        assert len(contact["phones"]) == 1
        assert len(contact["emails"]) == 1

    def test_export_dedupes_across_two_source_dbs(self, tmp_path):
        from scripts.apple_data_export import export_contacts

        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        lib_ab = self._lib_ab(tmp_path)

        # Same real contact synced into two account sources (e.g. iCloud +
        # Exchange), each with different contact info.
        _build_abcddb(
            lib_ab / "Sources" / "SRC-A" / "AddressBook-v22.abcddb",
            [{
                "pk": 1, "uid": "SRC-A-UID-1", "first": "Sasha", "last": "Ivanov",
                "phones": [("_$!<Mobile>!$_", "+15555550177")],
            }],
        )
        _build_abcddb(
            lib_ab / "Sources" / "SRC-B" / "AddressBook-v22.abcddb",
            [{
                "pk": 1, "uid": "SRC-B-UID-1", "first": "Sasha", "last": "Ivanov",
                "emails": [("_$!<Work>!$_", "sasha.ivanov@example.com")],
            }],
        )

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["count"] == 1
        with open(export_dir / "contacts.json") as f:
            data = json.load(f)
        contact = data["contacts"][0]
        assert len(contact["phones"]) == 1
        assert len(contact["emails"]) == 1

    def test_abcdp_fallback_used_when_abcddb_yields_nothing(self, tmp_path):
        """If .abcddb files exist but contain no contact rows, .abcdp files
        (if present) must still be read."""
        from scripts.apple_data_export import export_contacts

        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        lib_ab = self._lib_ab(tmp_path)

        _build_abcddb(lib_ab / "AddressBook-v22.abcddb", [])  # no contacts

        meta_dir = lib_ab / "Sources" / "LEGACY-SRC" / "Metadata"
        meta_dir.mkdir(parents=True)
        with open(meta_dir / "LEGACY-UID:ABPerson.abcdp", "wb") as f:
            plistlib.dump({"First": "Dana", "Last": "Osei"}, f)

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["status"] == "ok"
        assert result["count"] == 1
        with open(export_dir / "contacts.json") as f:
            data = json.load(f)
        assert data["contacts"][0]["full_name"] == "Dana Osei"

    def test_both_abcddb_and_abcdp_empty_returns_error(self, tmp_path):
        from scripts.apple_data_export import export_contacts

        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        lib_ab = self._lib_ab(tmp_path)
        _build_abcddb(lib_ab / "AddressBook-v22.abcddb", [])
        (lib_ab / "Sources" / "SRC-EMPTY" / "Metadata").mkdir(parents=True)

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir):
            result = export_contacts(dry_run=False)

        assert result["status"] == "error"
        assert result["count"] == 0
        assert result["path"] == ""
        assert not (export_dir / "contacts.json").exists()


class TestExportResultFinalization:
    """_finalize_result guards against a source reporting "ok" with no
    actual output. Issue #505's secondary finding: export_contacts returned
    {"status": "ok", "count": 0, "path": ""} when no .abcdp files existed —
    indistinguishable from a genuinely healthy empty result."""

    def _finalize(self):
        from scripts.apple_data_export import _finalize_result
        return _finalize_result

    def test_ok_zero_count_empty_path_becomes_error(self):
        finalize = self._finalize()
        result = finalize({"status": "ok", "count": 0, "path": ""})
        assert result["status"] == "error"
        assert "reason" in result

    def test_ok_with_real_output_is_unaffected(self):
        finalize = self._finalize()
        result = finalize({"status": "ok", "count": 3, "path": "/tmp/contacts.json"})
        assert result == {"status": "ok", "count": 3, "path": "/tmp/contacts.json"}

    def test_ok_zero_count_but_written_path_is_unaffected(self):
        """A genuinely empty but successfully-written export (e.g. 0 phone
        calls, but phone_calls.json was still written) must not be flagged —
        only the count-0-AND-path-empty combination means nothing happened."""
        finalize = self._finalize()
        result = finalize({"status": "ok", "count": 0, "path": "/tmp/phone_calls.json"})
        assert result["status"] == "ok"

    def test_non_ok_status_is_unaffected(self):
        finalize = self._finalize()
        result = finalize({"status": "skipped", "reason": "AddressBook not found"})
        assert result == {"status": "skipped", "reason": "AddressBook not found"}

    def test_ok_without_count_or_path_keys_is_unaffected(self):
        """Sources like imessage/photos/whatsapp use different result
        shapes (size_mb, people/faces, contacts/messages) and must not be
        caught by this contacts-shaped check."""
        finalize = self._finalize()
        result = finalize({"status": "ok", "size_mb": 12.3, "path": "/tmp/imessage.db"})
        assert result["status"] == "ok"

    def test_export_contacts_no_abcdp_files_gets_finalized_to_error(self, tmp_path):
        """Full-loop check: export_contacts's own empty-result, run through
        _finalize_result exactly as main() does, stays an error.

        Historically (issue #505) export_contacts returned {"status": "ok",
        "count": 0, "path": ""} here and _finalize_result had to catch it.
        Issue #514 made export_contacts itself report "error" directly when
        neither the .abcddb nor the .abcdp path yields any contacts, so this
        is now a no-op pass-through rather than a state flip."""
        from scripts.apple_data_export import export_contacts

        lib_ab = tmp_path / "Library" / "Application Support" / "AddressBook"
        (lib_ab / "Sources" / "SOURCE-1" / "Metadata").mkdir(parents=True)

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path):
            raw = export_contacts(dry_run=False)

        assert raw["status"] == "error"
        assert raw["count"] == 0
        assert raw["path"] == ""

        finalized = self._finalize()(raw)
        assert finalized["status"] == "error"


class TestContactsImportManifestAware:
    """import_contacts must not report success/skip when the Mac-side
    export marked contacts as errored (issue #505 acceptance criteria:
    "the Linux import surfaces it rather than reporting success")."""

    def _write_contacts(self, import_dir: Path, contacts: list[dict]):
        import_dir.mkdir(parents=True, exist_ok=True)
        with open(import_dir / "contacts.json", "w") as f:
            json.dump({"contacts": contacts, "exported_at": "2026-01-01T00:00:00+00:00"}, f)

    def _errored_manifest(self):
        return {
            "results": {
                "contacts": {
                    "status": "error",
                    "reason": "reported ok with zero count and no output path",
                }
            }
        }

    def test_missing_file_no_manifest_error_is_still_skipped(self, tmp_path):
        """Regression check: unrelated to #505, unchanged behavior."""
        from scripts.apple_data_import import import_contacts

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            result = import_contacts(dry_run=False, manifest=None)

        assert result == {"status": "skipped", "reason": "contacts.json not found"}

    def test_missing_file_with_manifest_error_is_error(self, tmp_path):
        """The exact issue #505 scenario: export never wrote contacts.json
        (zero .abcdp files found), the export-side fix now marks that as an
        error in the manifest, and the import must surface an error instead
        of a benign "skipped"."""
        from scripts.apple_data_import import import_contacts

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            result = import_contacts(dry_run=False, manifest=self._errored_manifest())

        assert result["status"] == "error"
        assert "contacts.json not found" in result["reason"]

    def test_dry_run_with_manifest_error_is_error(self, tmp_path):
        from scripts.apple_data_import import import_contacts

        import_dir = tmp_path / "apple-imports"
        self._write_contacts(import_dir, [{"identifier": "u1", "full_name": "Jane Doe"}])
        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            result = import_contacts(dry_run=True, manifest=self._errored_manifest())

        assert result["status"] == "error"
        assert result["count"] == 1

    def test_execute_with_manifest_error_still_imports_but_flags_error(self, tmp_path):
        """A stale contacts.json from a previous good run should still be
        imported (data is better than nothing) but the run must be flagged
        as errored so it doesn't look like a clean success — mirrors
        import_whatsapp's existing manifest-aware behavior."""
        from scripts.apple_data_import import import_contacts
        from unittest.mock import MagicMock
        from api.services.source_entity import SourceEntityStore

        import_dir = tmp_path / "apple-imports"
        self._write_contacts(import_dir, [{"identifier": "u1", "full_name": "Jane Doe"}])

        se_store = SourceEntityStore(db_path=str(tmp_path / "crm.db"))
        mock_resolver = MagicMock()
        mock_pe_store = MagicMock()

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
             patch("api.services.source_entity.get_source_entity_store", return_value=se_store), \
             patch("api.services.entity_resolver.get_entity_resolver", return_value=mock_resolver), \
             patch("api.services.person_entity.get_person_entity_store", return_value=mock_pe_store):
            result = import_contacts(dry_run=False, manifest=self._errored_manifest())

        assert result["status"] == "error"
        assert result["created"] == 1
        assert "stale contacts.json" in result["reason"]


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

    def test_stale_7d_critical_sets_structured_message(self, tmp_path, caplog):
        """Issue #646 regression guard: the staleness signal must be readable
        from check_manifest()'s return value, not just the log stream. A
        CRITICAL log line lands in the sync log file fine, but it doesn't
        drive record_failure/the run status/the nightly summary — only the
        subprocess exit code does, and a prose-only CRITICAL (the pre-fix
        behavior) never touches that. This test fails against that pre-fix
        behavior: caplog would still show the CRITICAL, but
        manifest["_staleness_critical_message"] wouldn't exist."""
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        very_stale = datetime.now(timezone.utc) - timedelta(days=10)
        self._write_manifest(import_dir, very_stale.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.DEBUG):
                result = check_manifest()

        assert result is not None
        assert "10 days old" in result.get("_staleness_critical_message", "")

    def test_fresh_data_no_structured_staleness_message(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        self._write_manifest(import_dir, fresh.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.INFO):
                result = check_manifest()

        assert result is not None
        assert "_staleness_critical_message" not in result

    def test_stale_48h_no_structured_critical_message(self, tmp_path, caplog):
        """The 48h WARNING tier must not set the critical-only flag."""
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        stale = datetime.now(timezone.utc) - timedelta(hours=50)
        self._write_manifest(import_dir, stale.isoformat())

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert "_staleness_critical_message" not in result

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


# ---------------------------------------------------------------------------
# Agent self-update SHA (issue #509) — export side
# ---------------------------------------------------------------------------

class TestAgentShaExport:
    """_get_agent_sha and the manifest's agent_sha field from apple_data_export."""

    def test_get_agent_sha_returns_stripped_sha(self):
        from scripts.apple_data_export import _get_agent_sha

        fake_result = MagicMock(returncode=0, stdout="abc123def456\n")
        with patch("scripts.apple_data_export.subprocess.run", return_value=fake_result):
            assert _get_agent_sha() == "abc123def456"

    def test_get_agent_sha_none_on_nonzero_exit(self):
        from scripts.apple_data_export import _get_agent_sha

        fake_result = MagicMock(returncode=128, stdout="")
        with patch("scripts.apple_data_export.subprocess.run", return_value=fake_result):
            assert _get_agent_sha() is None

    def test_get_agent_sha_none_on_empty_output(self):
        from scripts.apple_data_export import _get_agent_sha

        fake_result = MagicMock(returncode=0, stdout="\n")
        with patch("scripts.apple_data_export.subprocess.run", return_value=fake_result):
            assert _get_agent_sha() is None

    def test_get_agent_sha_none_when_git_missing(self):
        from scripts.apple_data_export import _get_agent_sha

        with patch("scripts.apple_data_export.subprocess.run", side_effect=FileNotFoundError()):
            assert _get_agent_sha() is None

    def test_main_writes_agent_sha_to_manifest(self, tmp_path, monkeypatch):
        """main() records the checked-out SHA in manifest.json (issue #509) so the
        Linux side can tell which revision produced an export without SSHing."""
        import scripts.apple_data_export as export_mod

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(export_mod, "_get_agent_sha", lambda: "deadbeef1234")
        monkeypatch.setattr(export_mod.sys, "argv", ["apple_data_export.py", "--execute"])

        def fake_source(dry_run=False):
            return {"status": "ok", "count": 1, "path": "fake"}

        for name in (
            "export_contacts",
            "export_imessage",
            "export_phone_calls",
            "export_photos_faces",
            "export_whatsapp",
        ):
            monkeypatch.setattr(export_mod, name, fake_source)

        export_mod.main()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["agent_sha"] == "deadbeef1234"

    def test_main_writes_null_agent_sha_when_undeterminable(self, tmp_path, monkeypatch):
        """A non-git checkout (or missing git binary) must not break the export —
        agent_sha is simply null in that case."""
        import scripts.apple_data_export as export_mod

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(export_mod, "_get_agent_sha", lambda: None)
        monkeypatch.setattr(export_mod.sys, "argv", ["apple_data_export.py", "--execute"])

        def fake_source(dry_run=False):
            return {"status": "ok", "count": 1, "path": "fake"}

        for name in (
            "export_contacts",
            "export_imessage",
            "export_phone_calls",
            "export_photos_faces",
            "export_whatsapp",
        ):
            monkeypatch.setattr(export_mod, name, fake_source)

        export_mod.main()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["agent_sha"] is None


# ---------------------------------------------------------------------------
# Manifest merge on partial (single-source) export runs — issue #786
# ---------------------------------------------------------------------------

class TestManifestMergeOnPartialExport:
    """A single-source export run must merge into the existing manifest
    rather than replacing it, so other sources' last-known status survives.
    A full (no --source) run must remain a full replacement, unchanged."""

    def _fake_source(self, dry_run=False):
        return {"status": "ok", "count": 1, "path": "fake"}

    def test_single_source_run_preserves_other_sources(self, tmp_path, monkeypatch):
        import scripts.apple_data_export as export_mod

        existing_manifest = {
            "exported_at": "2026-01-01T00:00:00+00:00",
            "hostname": "old-host",
            "agent_sha": "oldsha123",
            "results": {
                "contacts": {"status": "ok", "count": 5},
                "imessage": {"status": "ok", "count": 10},
                "phone": {"status": "error", "error": "boom"},
                "photos": {"status": "ok", "count": 2},
                "whatsapp": {"status": "error", "reason": "wacli outdated"},
            },
        }
        (tmp_path / "manifest.json").write_text(json.dumps(existing_manifest))

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(export_mod, "_get_agent_sha", lambda: "newsha456")
        monkeypatch.setattr(
            export_mod.sys, "argv",
            ["apple_data_export.py", "--execute", "--source", "whatsapp"],
        )
        monkeypatch.setattr(export_mod, "export_whatsapp", self._fake_source)

        export_mod.main()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        results = manifest["results"]
        # The run source reflects the new result...
        assert results["whatsapp"] == {"status": "ok", "count": 1, "path": "fake"}
        # ...and every other source's prior entry is untouched.
        assert results["contacts"] == {"status": "ok", "count": 5}
        assert results["imessage"] == {"status": "ok", "count": 10}
        assert results["phone"] == {"status": "error", "error": "boom"}
        assert results["photos"] == {"status": "ok", "count": 2}

    def test_single_source_run_with_no_existing_manifest_writes_just_that_source(
        self, tmp_path, monkeypatch
    ):
        """Fresh install: no manifest yet, single-source run — today's
        behavior for a fresh install must be unchanged."""
        import scripts.apple_data_export as export_mod

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(export_mod, "_get_agent_sha", lambda: "newsha456")
        monkeypatch.setattr(
            export_mod.sys, "argv",
            ["apple_data_export.py", "--execute", "--source", "contacts"],
        )
        monkeypatch.setattr(export_mod, "export_contacts", self._fake_source)

        export_mod.main()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["results"] == {"contacts": {"status": "ok", "count": 1, "path": "fake"}}

    def test_full_run_still_fully_replaces_manifest(self, tmp_path, monkeypatch):
        """Regression guard: an all-sources run (no --source) must remain a
        byte-for-byte full replacement — this is the maintainer's nightly
        path and must not gain merge semantics."""
        import scripts.apple_data_export as export_mod

        existing_manifest = {
            "exported_at": "2026-01-01T00:00:00+00:00",
            "hostname": "old-host",
            "agent_sha": "oldsha123",
            "results": {
                "contacts": {"status": "ok", "count": 999},
                "stale_source_no_longer_run": {"status": "ok", "count": 1},
            },
        }
        (tmp_path / "manifest.json").write_text(json.dumps(existing_manifest))

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(export_mod, "_get_agent_sha", lambda: "newsha456")
        monkeypatch.setattr(export_mod.sys, "argv", ["apple_data_export.py", "--execute"])

        for name in (
            "export_contacts",
            "export_imessage",
            "export_phone_calls",
            "export_photos_faces",
            "export_whatsapp",
        ):
            monkeypatch.setattr(export_mod, name, self._fake_source)

        export_mod.main()

        manifest = json.loads((tmp_path / "manifest.json").read_text())
        # Only the five real sources from this run — the stale leftover key
        # from the old manifest must NOT survive a full run.
        assert set(manifest["results"].keys()) == {
            "contacts", "imessage", "phone", "photos", "whatsapp",
        }
        assert manifest["results"]["contacts"] == {"status": "ok", "count": 1, "path": "fake"}

    def test_single_source_dry_run_does_not_write_manifest(self, tmp_path, monkeypatch):
        """Out of scope for #786: dry-run stays preview-only, no manifest
        write at all, merge or otherwise."""
        import scripts.apple_data_export as export_mod

        existing_manifest = {
            "exported_at": "2026-01-01T00:00:00+00:00",
            "hostname": "old-host",
            "results": {"contacts": {"status": "ok", "count": 5}},
        }
        (tmp_path / "manifest.json").write_text(json.dumps(existing_manifest))

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(
            export_mod.sys, "argv",
            ["apple_data_export.py", "--dry-run", "--source", "whatsapp"],
        )
        monkeypatch.setattr(export_mod, "export_whatsapp", self._fake_source)

        export_mod.main()

        # manifest.json on disk is exactly what it was before — untouched.
        on_disk = json.loads((tmp_path / "manifest.json").read_text())
        assert on_disk == existing_manifest

    def test_single_source_run_with_corrupt_manifest_leaves_it_untouched(
        self, tmp_path, monkeypatch, caplog
    ):
        """A single-source run must never fall back to overwriting an
        unreadable/corrupt manifest with just its own result — that would
        reproduce the exact data-loss bug #786 fixes, just triggered by
        corruption instead of an ordinary single-source run."""
        import scripts.apple_data_export as export_mod

        (tmp_path / "manifest.json").write_text("{not valid json")

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(
            export_mod.sys, "argv",
            ["apple_data_export.py", "--execute", "--source", "whatsapp"],
        )
        monkeypatch.setattr(export_mod, "export_whatsapp", self._fake_source)

        with caplog.at_level(logging.ERROR):
            export_mod.main()

        # The corrupt file on disk is untouched — not replaced with a
        # partial (whatsapp-only) manifest.
        assert (tmp_path / "manifest.json").read_text() == "{not valid json"
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_single_source_run_with_non_dict_results_leaves_it_untouched(
        self, tmp_path, monkeypatch, caplog
    ):
        """Same guard, for a manifest that parses but whose 'results' key
        isn't a dict (unexpected shape) — still must not be overwritten."""
        import scripts.apple_data_export as export_mod

        existing_manifest = {
            "exported_at": "2026-01-01T00:00:00+00:00",
            "hostname": "old-host",
            "results": "not-a-dict",
        }
        (tmp_path / "manifest.json").write_text(json.dumps(existing_manifest))

        monkeypatch.setattr(export_mod.sys, "platform", "darwin")
        monkeypatch.setattr(export_mod, "EXPORT_DIR", tmp_path)
        monkeypatch.setattr(
            export_mod.sys, "argv",
            ["apple_data_export.py", "--execute", "--source", "whatsapp"],
        )
        monkeypatch.setattr(export_mod, "export_whatsapp", self._fake_source)

        with caplog.at_level(logging.ERROR):
            export_mod.main()

        on_disk = json.loads((tmp_path / "manifest.json").read_text())
        assert on_disk == existing_manifest
        assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Agent self-update SHA (issue #509) — import side
# ---------------------------------------------------------------------------

class TestAgentShaImport:
    """check_manifest flags a Mac Mini export whose agent_sha differs from this
    host's main — non-fatal, warning-level only, and silent when the field is
    absent (older manifests) or the local SHA can't be determined."""

    def _write_manifest(self, import_dir: Path, agent_sha=None):
        import_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "hostname": "test-host",
            "results": {},
        }
        if agent_sha is not None:
            manifest["agent_sha"] = agent_sha
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

    def test_get_local_main_sha_returns_stripped_sha(self):
        from scripts.apple_data_import import _get_local_main_sha

        fake_result = MagicMock(returncode=0, stdout="def5678\n")
        with patch("scripts.apple_data_import.subprocess.run", return_value=fake_result):
            assert _get_local_main_sha() == "def5678"

    def test_get_local_main_sha_none_on_failure(self):
        from scripts.apple_data_import import _get_local_main_sha

        with patch("scripts.apple_data_import.subprocess.run", side_effect=FileNotFoundError()):
            assert _get_local_main_sha() is None

    def test_matching_sha_no_warning(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha="abc1234")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value="abc1234"):
            with caplog.at_level(logging.WARNING):
                check_manifest()

        assert not any("differs from" in r.message for r in caplog.records)

    def test_mismatched_sha_warns(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha="abc1234")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value="def5678"):
            with caplog.at_level(logging.WARNING):
                check_manifest()

        assert any(
            r.levelno == logging.WARNING and "differs from" in r.message
            for r in caplog.records
        )

    def test_mismatched_sha_sets_structured_message(self, tmp_path, caplog):
        """Issue #646: run_all_syncs.py reads this drift signal directly off
        the manifest dict (not the subprocess log stream) so it can surface
        in the nightly Telegram/markdown summary — this must be readable
        from the return value, not just caplog."""
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha="abc1234")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value="def5678"):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert "differs from" in result.get("_agent_sha_drift_message", "")

    def test_matching_sha_no_structured_message(self, tmp_path, caplog):
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha="abc1234")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value="abc1234"):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert "_agent_sha_drift_message" not in result

    def test_missing_agent_sha_handled_silently(self, tmp_path, caplog):
        """Manifests written before this change have no agent_sha — must not
        warn or crash."""
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha=None)

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value="def5678"):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert not any("differs from" in r.message for r in caplog.records)

    def test_local_sha_lookup_failure_handled_silently(self, tmp_path, caplog):
        """If the local main SHA can't be determined, skip the comparison
        rather than crash or false-warn."""
        from scripts.apple_data_import import check_manifest

        import_dir = tmp_path / "apple-imports"
        self._write_manifest(import_dir, agent_sha="abc1234")

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
                patch("scripts.apple_data_import._get_local_main_sha", return_value=None):
            with caplog.at_level(logging.WARNING):
                result = check_manifest()

        assert result is not None
        assert not any("differs from" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# WhatsApp export (issue #677) — a failed `wacli sync` must not export as
# status "ok", a 405 "Client outdated" must be diagnosed as client-version
# (not auth), and a genuine auth failure must still be diagnosed as auth.
# ---------------------------------------------------------------------------

class TestWacliFailureDiagnosis:
    """_diagnose_wacli_failure classifies a failed `wacli sync` from its
    combined stdout/stderr. Real sample output from issue #677 (synthetic
    protocol/version numbers only, no personal data)."""

    # Captured 2026-08-24 on a wacli client frozen at 0.5.0 by a silent
    # Homebrew tap rename (steipete/tap -> openclaw/tap) — see issue #677.
    SYNC_405_OUTPUT = (
        "[Client/Socket ERROR] Error reading from websocket: failed to get "
        "reader: failed to read frame header: EOF\n"
        "[Client ERROR] Client outdated (405) connect failure "
        "(client version: 2.3000.1037076227)\n\n"
        "Idle for 30s, exiting.\n"
        "Messages stored: 0"
    )

    # Synthetic — wacli doesn't publish a canonical auth-failure message;
    # this exercises the auth-marker branch without inventing 405 text.
    SYNC_AUTH_OUTPUT = (
        "[Client ERROR] Unauthorized (401): session not authenticated. "
        "Run `wacli auth` to pair a device.\n"
        "Idle for 5s, exiting.\n"
        "Messages stored: 0"
    )

    def _diagnose(self):
        from scripts.apple_data_export import _diagnose_wacli_failure
        return _diagnose_wacli_failure

    def test_405_client_outdated_is_client_version_not_auth(self):
        diagnose = self._diagnose()
        result = diagnose(self.SYNC_405_OUTPUT)

        assert result["diagnosis"] == "client_version"
        assert "auth" not in result["diagnosis"]
        assert "brew upgrade wacli" in result["reason"]
        assert "NOT authentication" in result["reason"]

    def test_genuine_auth_failure_is_still_auth(self):
        """The 405 fix must not overcorrect into misdiagnosing a real auth
        failure as a client-version problem."""
        diagnose = self._diagnose()
        result = diagnose(self.SYNC_AUTH_OUTPUT)

        assert result["diagnosis"] == "auth"
        assert "wacli auth" in result["reason"]

    def test_unrecognized_failure_is_unknown_not_silently_ok(self):
        diagnose = self._diagnose()
        result = diagnose("[Client ERROR] connection refused")

        assert result["diagnosis"] == "unknown"
        assert "connection refused" in result["reason"]

    def test_empty_output_handled(self):
        diagnose = self._diagnose()
        result = diagnose("")
        assert result["diagnosis"] == "unknown"


class TestWacliVersionLookup:
    """_get_wacli_version is best-effort, like _get_agent_sha: any failure
    just means the manifest field is null, never a broken export."""

    def _get_version(self):
        from scripts.apple_data_export import _get_wacli_version
        return _get_wacli_version

    def test_returns_stripped_version(self):
        get_version = self._get_version()
        fake_result = MagicMock(returncode=0, stdout="0.17.1\n", stderr="")
        with patch("scripts.apple_data_export.subprocess.run", return_value=fake_result):
            assert get_version() == "0.17.1"

    def test_none_on_nonzero_exit(self):
        get_version = self._get_version()
        fake_result = MagicMock(returncode=1, stdout="", stderr="unknown flag")
        with patch("scripts.apple_data_export.subprocess.run", return_value=fake_result):
            assert get_version() is None

    def test_none_when_wacli_missing(self):
        get_version = self._get_version()
        with patch("scripts.apple_data_export.subprocess.run", side_effect=FileNotFoundError()):
            assert get_version() is None


class TestNewestMessageTimestamp:
    """_newest_message_timestamp handles wacli's mixed ISO-string/epoch-
    second `ts` formats, same ambiguity as the import-side parser."""

    def _newest(self):
        from scripts.apple_data_export import _newest_message_timestamp
        return _newest_message_timestamp

    def test_picks_latest_iso_timestamp(self):
        newest = self._newest()
        result = newest([
            {"ts": "2026-08-20T10:00:00+00:00"},
            {"ts": "2026-08-24T09:30:00+00:00"},
            {"ts": "2026-08-22T00:00:00+00:00"},
        ])
        assert result == "2026-08-24T09:30:00+00:00"

    def test_picks_latest_epoch_timestamp(self):
        newest = self._newest()
        earlier = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp())
        later = int(datetime(2026, 8, 24, tzinfo=timezone.utc).timestamp())
        result = newest([{"ts": earlier}, {"ts": later}])
        assert result == datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat()

    def test_empty_list_returns_none(self):
        newest = self._newest()
        assert newest([]) is None

    def test_unparseable_timestamps_skipped(self):
        newest = self._newest()
        result = newest([{"ts": "not-a-date"}, {"ts": "2026-08-24T00:00:00+00:00"}])
        assert result == "2026-08-24T00:00:00+00:00"


class TestExportWhatsapp:
    """export_whatsapp's end-to-end status/diagnosis behavior."""

    def _build_wacli_db(self, path: Path, messages: list[tuple]):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE messages (
                msg_id TEXT, chat_jid TEXT, chat_name TEXT, sender_jid TEXT,
                sender_name TEXT, ts TEXT, from_me INTEGER, text TEXT,
                display_text TEXT, media_type TEXT
            );
            CREATE TABLE group_participants (group_jid TEXT, user_jid TEXT);
            CREATE TABLE contacts (jid TEXT, push_name TEXT);
        """)
        for m in messages:
            conn.execute(
                "INSERT INTO messages (msg_id, chat_jid, chat_name, sender_jid, "
                "sender_name, ts, from_me, text, display_text, media_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                m,
            )
        conn.commit()
        conn.close()

    def _msg(self, msg_id: str, ts: str):
        return (msg_id, "15555550100@s.whatsapp.net", "Test Chat",
                "15555550100@s.whatsapp.net", "Tester", ts, 0, "hi", "hi", None)

    def _run_export(
        self,
        tmp_path: Path,
        sync_returncode: int,
        sync_output: str = "",
        messages: list[tuple] | None = None,
        dry_run: bool = False,
        wacli_version: str = "0.17.1",
    ):
        from scripts.apple_data_export import export_whatsapp

        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        wacli_db = tmp_path / ".wacli" / "wacli.db"
        self._build_wacli_db(wacli_db, messages or [])

        sync_result = MagicMock(returncode=sync_returncode, stdout=sync_output, stderr="")
        version_result = MagicMock(returncode=0, stdout=wacli_version, stderr="")
        contacts_result = MagicMock(returncode=0, stdout='{"data": []}', stderr="")

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["wacli", "sync"]:
                return sync_result
            if cmd[:2] == ["wacli", "--version"]:
                return version_result
            if "--json" in cmd:
                return contacts_result
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch("scripts.apple_data_export.Path.home", return_value=tmp_path), \
             patch("scripts.apple_data_export.EXPORT_DIR", export_dir), \
             patch("scripts.apple_data_export.subprocess.run", side_effect=fake_run):
            return export_whatsapp(dry_run=dry_run)

    def test_successful_sync_is_ok(self, tmp_path):
        result = self._run_export(tmp_path, sync_returncode=0)
        assert result["status"] == "ok"
        assert "reason" not in result

    def test_nonzero_sync_exit_marks_error_not_ok(self, tmp_path):
        """Issue #677's core bug: a failing `wacli sync` used to export as
        status "ok" and package whatever stale data was already on disk."""
        result = self._run_export(
            tmp_path, sync_returncode=1, sync_output="[Client ERROR] connection refused",
        )
        assert result["status"] == "error"
        assert result["diagnosis"] == "unknown"
        # The export still runs to completion — stale data is better than
        # none, but the run must not look like a clean success.
        assert result["path"]

    def test_405_client_outdated_is_error_with_client_version_diagnosis(self, tmp_path):
        result = self._run_export(
            tmp_path,
            sync_returncode=1,
            sync_output=TestWacliFailureDiagnosis.SYNC_405_OUTPUT,
        )
        assert result["status"] == "error"
        assert result["diagnosis"] == "client_version"
        assert "brew upgrade wacli" in result["reason"]

    def test_auth_failure_is_error_with_auth_diagnosis(self, tmp_path):
        result = self._run_export(
            tmp_path,
            sync_returncode=1,
            sync_output=TestWacliFailureDiagnosis.SYNC_AUTH_OUTPUT,
        )
        assert result["status"] == "error"
        assert result["diagnosis"] == "auth"

    def test_manifest_records_wacli_version_and_newest_message_timestamp(self, tmp_path):
        result = self._run_export(
            tmp_path,
            sync_returncode=0,
            wacli_version="0.17.1",
            messages=[
                self._msg("m1", "2026-08-20T10:00:00+00:00"),
                self._msg("m2", "2026-08-24T09:30:00+00:00"),
            ],
        )
        assert result["wacli_version"] == "0.17.1"
        assert result["newest_message_at"] == "2026-08-24T09:30:00+00:00"

    def test_dry_run_still_surfaces_sync_error(self, tmp_path):
        result = self._run_export(
            tmp_path,
            sync_returncode=1,
            sync_output=TestWacliFailureDiagnosis.SYNC_405_OUTPUT,
            dry_run=True,
        )
        assert result["status"] == "error"
        assert result["diagnosis"] == "client_version"
        assert result["wacli_version"] == "0.17.1"


# ---------------------------------------------------------------------------
# Phone import — phone number extraction and person resolution
# ---------------------------------------------------------------------------

class TestPhoneNumberExtraction:
    """Test _extract_phone_from_title from apple_data_import."""

    def _get_extract(self):
        from scripts.apple_data_import import _extract_phone_from_title
        return _extract_phone_from_title

    def test_incoming_with_duration(self):
        extract = self._get_extract()
        assert extract("Incoming Phone with +15712824226 (23s)") == "+15712824226"

    def test_missed_call(self):
        extract = self._get_extract()
        assert extract("Incoming Phone (missed) - +18882346268") == "+18882346268"

    def test_outgoing_call(self):
        extract = self._get_extract()
        assert extract("Outgoing Phone with +14155551234 (120s)") == "+14155551234"

    def test_facetime_audio(self):
        extract = self._get_extract()
        assert extract("Incoming FaceTime Audio with +12025250790 (14m 26s)") == "+12025250790"

    def test_no_phone_number(self):
        extract = self._get_extract()
        assert extract("Incoming Phone (missed)") is None

    def test_empty_title(self):
        extract = self._get_extract()
        assert extract("") is None


class TestPhoneImport:
    """Test import_phone_calls with person resolution."""

    def _write_calls(self, import_dir: Path, calls: list[dict]):
        import_dir.mkdir(parents=True, exist_ok=True)
        with open(import_dir / "phone_calls.json", "w") as f:
            json.dump({"calls": calls, "exported_at": "2026-01-01T00:00:00+00:00"}, f)

    def _wire_phone_import(self, tmp_path, import_dir, mock_resolver):
        """Common test scaffolding: real ``SourceEntityStore`` on tmp_path,
        mock interaction store + entity resolver. Returns the
        ``SourceEntityStore`` and a ``MagicMock`` interaction store so
        callers can inspect both sides of the dual-write."""
        from unittest.mock import MagicMock
        from api.services.source_entity import SourceEntityStore

        se_store = SourceEntityStore(db_path=str(tmp_path / "crm.db"))
        mock_interaction_store = MagicMock()
        mock_interaction_store.get_by_source.return_value = None

        # PersonEntityStore is consulted indirectly via SourceEntityStore.add
        # for the ``validate_person`` path; pass a permissive mock that
        # claims every person id exists.
        mock_person_store = MagicMock()
        mock_person_store.get_canonical_id.side_effect = lambda pid: pid
        mock_person_store.get_by_id.side_effect = lambda pid: MagicMock(id=pid)

        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("scripts.apple_data_import.IMPORT_DIR", import_dir))
        stack.enter_context(patch(
            "api.services.interaction_store.get_interaction_store",
            return_value=mock_interaction_store,
        ))
        stack.enter_context(patch(
            "api.services.entity_resolver.get_entity_resolver",
            return_value=mock_resolver,
        ))
        stack.enter_context(patch(
            "api.services.source_entity.get_source_entity_store",
            return_value=se_store,
        ))
        stack.enter_context(patch(
            "api.services.person_entity.get_person_entity_store",
            return_value=mock_person_store,
        ))
        return se_store, mock_interaction_store, stack

    def test_resolved_call_imported_and_linked(self, tmp_path):
        """Call with a phone matching a known person creates a linked
        source_entity + the Interaction."""
        from scripts.apple_data_import import import_phone_calls
        from unittest.mock import MagicMock

        import_dir = tmp_path / "apple-imports"
        self._write_calls(import_dir, [{
            "id": "call-1",
            "source_id": "SRC-1",
            "source_type": "phone",
            "person_id": "",
            "timestamp": "2026-01-15T10:00:00+00:00",
            "title": "Incoming Phone with +15551234567 (30s)",
        }])

        mock_person = MagicMock()
        mock_person.id = "person-abc"
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = MagicMock(entity=mock_person)

        se_store, mock_interaction_store, stack = self._wire_phone_import(
            tmp_path, import_dir, mock_resolver,
        )
        with stack:
            result = import_phone_calls(dry_run=False)

        assert result["status"] == "ok"
        assert result["imported"] == 1
        assert result["unresolved"] == 0
        # Resolver was called with the new shape.
        mock_resolver.resolve.assert_called_once()
        kwargs = mock_resolver.resolve.call_args.kwargs
        assert kwargs == {"phone": "+15551234567", "create_if_missing": False}
        # Interaction was created with the resolved person.
        added = mock_interaction_store.add_if_not_exists.call_args[0][0]
        assert added.person_id == "person-abc"
        # source_entity exists AND is linked.
        se = se_store.get_by_source("phone", "phone_+15551234567")
        assert se is not None
        assert se.canonical_person_id == "person-abc"

    def test_unresolved_call_stores_orphan_source_entity(self, tmp_path):
        """Issue #226 policy: an unresolved phone observation still produces
        an unlinked source_entity (``canonical_person_id IS NULL``), so
        ``link_source_entities`` can retro-link it once the matching Contact
        / email arrives. No Interaction is created — those require a person.
        """
        from scripts.apple_data_import import import_phone_calls
        from unittest.mock import MagicMock

        import_dir = tmp_path / "apple-imports"
        self._write_calls(import_dir, [{
            "id": "call-2",
            "source_id": "SRC-2",
            "source_type": "phone",
            "person_id": "",
            "timestamp": "2026-01-15T10:00:00+00:00",
            "title": "Incoming Phone (missed) - +18005551234",
        }])

        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = None  # no match

        se_store, mock_interaction_store, stack = self._wire_phone_import(
            tmp_path, import_dir, mock_resolver,
        )
        with stack:
            result = import_phone_calls(dry_run=False)

        # The call is counted as unresolved AND no interaction is added…
        assert result["imported"] == 0
        assert result["unresolved"] == 1
        mock_interaction_store.add_if_not_exists.assert_not_called()
        # …but the observation IS captured as an unlinked source_entity,
        # ready for ``link_source_entities`` to pick up next time around.
        assert result["source_entities_created"] == 1
        se = se_store.get_by_source("phone", "phone_+18005551234")
        assert se is not None, "orphan source_entity should exist for unknown caller"
        assert se.canonical_person_id in (None, ""), \
            "unresolved phone observations must NOT auto-link to any person"
        assert se.observed_phone == "+18005551234"

    def test_multiple_calls_from_unknown_caller_dedup_to_one_se(self, tmp_path):
        """Multiple calls from the same unknown number in a single batch
        produce exactly one source_entity (deduped via seen_phones) and
        increment orphan_observations once per call (so dashboards can see
        how many calls weren't linked tonight, not just how many unique
        numbers)."""
        from scripts.apple_data_import import import_phone_calls
        from unittest.mock import MagicMock

        import_dir = tmp_path / "apple-imports"
        self._write_calls(import_dir, [
            {
                "id": f"call-{i}",
                "source_id": f"SRC-{i}",
                "source_type": "phone",
                "person_id": "",
                "timestamp": f"2026-01-15T10:0{i}:00+00:00",
                "title": "Incoming Phone - +18005550199",
            }
            for i in range(3)
        ])

        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = None  # never matches

        se_store, mock_interaction_store, stack = self._wire_phone_import(
            tmp_path, import_dir, mock_resolver,
        )
        with stack:
            result = import_phone_calls(dry_run=False)

        assert result["imported"] == 0
        assert result["source_entities_created"] == 1, \
            "seen_phones should dedup three calls from the same number to one SE"
        assert result["orphan_observations"] == 3, \
            "every call (not just the first) counts as an orphan observation"
        mock_interaction_store.add_if_not_exists.assert_not_called()

        # Exactly one row landed in source_entities for this number.
        rows = []
        for c_phone in ("+18005550199",):
            row = se_store.get_by_source("phone", f"phone_{c_phone}")
            if row is not None:
                rows.append(row)
        assert len(rows) == 1
        assert rows[0].canonical_person_id in (None, "")

    def test_no_phone_in_title_skipped(self, tmp_path):
        """Call with no extractable phone number should be skipped."""
        from scripts.apple_data_import import import_phone_calls
        from unittest.mock import MagicMock

        import_dir = tmp_path / "apple-imports"
        self._write_calls(import_dir, [{
            "id": "call-3",
            "source_id": "SRC-3",
            "source_type": "phone",
            "person_id": "",
            "timestamp": "2026-01-15T10:00:00+00:00",
            "title": "Incoming Phone (missed)",
        }])

        mock_store = MagicMock()
        mock_store.get_by_source.return_value = None
        mock_resolver = MagicMock()

        with patch("scripts.apple_data_import.IMPORT_DIR", import_dir), \
             patch("api.services.interaction_store.get_interaction_store", return_value=mock_store), \
             patch("api.services.entity_resolver.get_entity_resolver", return_value=mock_resolver):
            result = import_phone_calls(dry_run=False)

        assert result["imported"] == 0
        assert result["unresolved"] == 1
        mock_resolver.resolve_by_phone.assert_not_called()


# ---------------------------------------------------------------------------
# main() — critical staleness must fail the run (issue #646)
# ---------------------------------------------------------------------------

class TestManifestStalenessFailsRun:
    """Regression for issue #646's headline defect: a manifest whose every
    source reports ok, but whose exported_at is older than
    STALENESS_CRITICAL_HOURS, must fail the run — not just log a CRITICAL
    that lands in the sync log file but never drives record_failure, the
    run status, or the nightly summary (only the subprocess exit code
    does). This is the exact "27/27 succeeded" scenario from the linked
    outage: a dead Mac Mini export agent left a stale-but-formally-healthy
    manifest in place for 10 nights."""

    def _stub_source(self, dry_run=False, **kwargs):
        return {"status": "ok", "created": 0}

    def test_stale_manifest_with_all_sources_ok_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        import scripts.apple_data_import as import_mod

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        very_stale = datetime.now(timezone.utc) - timedelta(days=10)
        manifest = {
            "exported_at": very_stale.isoformat(),
            "hostname": "test-host",
            "results": {
                "contacts": {"status": "ok"},
                "imessage": {"status": "ok"},
                "phone": {"status": "ok"},
                "photos": {"status": "ok"},
                "whatsapp": {"status": "ok"},
            },
        }
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        monkeypatch.setattr(import_mod, "IMPORT_DIR", import_dir)
        for name in (
            "import_contacts", "import_imessage", "import_phone_calls",
            "import_photos_faces", "import_whatsapp", "import_health",
        ):
            monkeypatch.setattr(import_mod, name, self._stub_source)
        monkeypatch.setattr(import_mod.sys, "argv", ["apple_data_import.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            import_mod.main()

        assert exc_info.value.code == 1
        # The failure must be visible in the printed results, not just logs —
        # this is what run_all_syncs.py's caller sees on top of the exit code.
        printed = capsys.readouterr().out
        assert "manifest_staleness" in printed

    def test_fresh_manifest_with_all_sources_ok_does_not_exit(self, tmp_path, monkeypatch):
        """Sanity check: the new staleness gate must not false-positive on a
        healthy, fresh export."""
        import scripts.apple_data_import as import_mod

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        manifest = {
            "exported_at": fresh.isoformat(),
            "hostname": "test-host",
            "results": {
                "contacts": {"status": "ok"},
                "imessage": {"status": "ok"},
                "phone": {"status": "ok"},
                "photos": {"status": "ok"},
                "whatsapp": {"status": "ok"},
            },
        }
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        monkeypatch.setattr(import_mod, "IMPORT_DIR", import_dir)
        for name in (
            "import_contacts", "import_imessage", "import_phone_calls",
            "import_photos_faces", "import_whatsapp", "import_health",
        ):
            monkeypatch.setattr(import_mod, name, self._stub_source)
        monkeypatch.setattr(import_mod.sys, "argv", ["apple_data_import.py", "--execute"])

        # main() calls sys.exit(1) only on error; a clean run falls off the
        # end of the function without exiting, so no SystemExit is raised.
        import_mod.main()

    def test_per_source_error_still_fails_run_alongside_fresh_staleness(self, tmp_path, monkeypatch):
        """Keep the existing per-source status=='error' behaviour working
        unchanged (issue #646 acceptance criteria: don't regress it while
        fixing staleness)."""
        import scripts.apple_data_import as import_mod

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        manifest = {
            "exported_at": fresh.isoformat(),
            "hostname": "test-host",
            "results": {
                "whatsapp": {"status": "error", "reason": "wacli not installed"},
            },
        }
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        monkeypatch.setattr(import_mod, "IMPORT_DIR", import_dir)

        def _erroring_whatsapp(dry_run=False, **kwargs):
            return {"status": "error", "reason": "wacli not installed"}

        monkeypatch.setattr(import_mod, "import_contacts", self._stub_source)
        monkeypatch.setattr(import_mod, "import_imessage", self._stub_source)
        monkeypatch.setattr(import_mod, "import_phone_calls", self._stub_source)
        monkeypatch.setattr(import_mod, "import_photos_faces", self._stub_source)
        monkeypatch.setattr(import_mod, "import_whatsapp", _erroring_whatsapp)
        monkeypatch.setattr(import_mod, "import_health", self._stub_source)
        monkeypatch.setattr(import_mod.sys, "argv", ["apple_data_import.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            import_mod.main()

        assert exc_info.value.code == 1


class TestManifestErrorOverridesLocalSuccess:
    """Issue #646 follow-up: import_contacts/import_whatsapp check the
    manifest internally (_manifest_source_errored) and already report
    "error" themselves when the Mac-side export failed. imessage, phone,
    photos, and health do NOT — they only look at whether their input file
    exists. If a stale file from a prior successful export is still on
    disk, the local import happily reprocesses it and reports "ok",
    silently masking a Mac-side export failure that check_manifest()'s
    per-source walk only logs (never structurally surfaced before this
    fix). main() must override that back to "error" and still fail the
    run — the exact same "logged but not structured" trap as staleness,
    just for a different signal."""

    def _run_with_manifest_error(self, tmp_path, monkeypatch, source_name, stub_result):
        import scripts.apple_data_import as import_mod

        # Subdirectory keyed by source_name so a test iterating over
        # multiple sources (each calling this helper against the same
        # tmp_path) doesn't collide on an already-existing import_dir.
        import_dir = tmp_path / source_name / "apple-imports"
        import_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        manifest = {
            "exported_at": fresh.isoformat(),
            "hostname": "test-host",
            "results": {
                source_name: {"status": "error", "reason": "export tool crashed"},
            },
        }
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        monkeypatch.setattr(import_mod, "IMPORT_DIR", import_dir)

        func_names = {
            "contacts": "import_contacts",
            "imessage": "import_imessage",
            "phone": "import_phone_calls",
            "photos": "import_photos_faces",
            "whatsapp": "import_whatsapp",
            "health": "import_health",
        }
        for name, func_name in func_names.items():
            if name == source_name:
                monkeypatch.setattr(
                    import_mod, func_name,
                    lambda dry_run=False, **kw: dict(stub_result),
                )
            else:
                monkeypatch.setattr(
                    import_mod, func_name,
                    lambda dry_run=False, **kw: {"status": "ok"},
                )
        monkeypatch.setattr(import_mod.sys, "argv", ["apple_data_import.py", "--execute"])
        return import_mod

    def test_stale_imessage_db_reported_ok_still_fails_run(self, tmp_path, monkeypatch):
        """imessage isn't manifest-aware — a stale imessage.db copy would
        otherwise report 'ok' and mask the Mac-side failure entirely."""
        import_mod = self._run_with_manifest_error(
            tmp_path, monkeypatch, "imessage", {"status": "ok", "size_mb": 12.0},
        )

        with pytest.raises(SystemExit) as exc_info:
            import_mod.main()

        assert exc_info.value.code == 1

    def test_photos_and_health_also_covered(self, tmp_path, monkeypatch):
        for source_name in ("photos", "health"):
            import_mod = self._run_with_manifest_error(
                tmp_path, monkeypatch, source_name, {"status": "ok"},
            )
            with pytest.raises(SystemExit) as exc_info:
                import_mod.main()
            assert exc_info.value.code == 1

    def test_override_preserves_stat_keys_unit_level(self):
        """Unit-level check of the override behavior itself: status flips
        to error, reason is set, and any stat keys the local import
        produced (e.g. "imported") survive rather than being discarded —
        mirrors import_contacts/import_whatsapp's own established pattern
        of overriding status while keeping the stats."""
        results = {"phone": {"status": "ok", "imported": 5, "source_entities_created": 2}}
        manifest_results = {"phone": {"status": "error", "reason": "export tool crashed"}}

        # Reproduce main()'s override loop in isolation.
        for name in results:
            manifest_entry = manifest_results.get(name)
            if not isinstance(manifest_entry, dict) or manifest_entry.get("status") != "error":
                continue
            existing = results.get(name)
            if isinstance(existing, dict) and existing.get("status") == "error":
                continue
            reason = manifest_entry.get("reason") or "Mac export reported error"
            if isinstance(existing, dict):
                existing["status"] = "error"
                existing["reason"] = reason
            else:
                results[name] = {"status": "error", "reason": reason}

        assert results["phone"]["status"] == "error"
        assert results["phone"]["reason"] == "export tool crashed"
        assert results["phone"]["imported"] == 5
        assert results["phone"]["source_entities_created"] == 2

    def test_no_override_when_source_not_attempted_this_run(self, tmp_path, monkeypatch):
        """--source contacts must not spuriously fail on an unrelated
        manifest-reported phone error (existing behavior, unchanged)."""
        import scripts.apple_data_import as import_mod

        import_dir = tmp_path / "apple-imports"
        import_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        manifest = {
            "exported_at": fresh.isoformat(),
            "hostname": "test-host",
            "results": {
                "phone": {"status": "error", "reason": "export tool crashed"},
                "contacts": {"status": "ok"},
            },
        }
        with open(import_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        monkeypatch.setattr(import_mod, "IMPORT_DIR", import_dir)
        monkeypatch.setattr(
            import_mod, "import_contacts",
            lambda dry_run=False, **kw: {"status": "ok", "created": 1},
        )
        monkeypatch.setattr(import_mod.sys, "argv", ["apple_data_import.py", "--execute", "--source", "contacts"])

        # Must not raise — only "contacts" was selected, "phone"'s manifest
        # error is out of scope for this invocation.
        import_mod.main()
