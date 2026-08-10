"""Tests for Apple data pipeline: contacts plist parsing, phone import, staleness alerting."""
import json
import logging
import plistlib
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock


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
