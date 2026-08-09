"""Tests for api/services/whatsapp.py — pure helpers and JSON-based processing.

Replaces tests/test_sync_whatsapp.py. The wacli-calling logic now lives in
scripts/apple_data_export.py (Mac-only) and is exercised end-to-end on the
Mac Mini, not in unit tests. The processing logic — entity resolution and
interaction creation — operates on parsed dicts and is tested here.
"""
import sqlite3
from unittest.mock import MagicMock, patch

from api.services.whatsapp import (
    SOURCE_WHATSAPP,
    extract_phone_from_jid,
    is_group_jid,
    normalize_phone,
    parse_message_timestamp,
    process_whatsapp_contacts,
    process_whatsapp_messages,
    resolve_lid_phone,
)
from api.services.source_entity import SourceEntity


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    """Tests for phone number normalization."""

    def test_us_10_digit(self):
        assert normalize_phone("5551234567") == "+15551234567"

    def test_us_with_country_code(self):
        assert normalize_phone("15551234567") == "+15551234567"

    def test_with_formatting(self):
        assert normalize_phone("(555) 123-4567") == "+15551234567"
        assert normalize_phone("555-123-4567") == "+15551234567"

    def test_international(self):
        assert normalize_phone("+442071234567") == "+442071234567"

    def test_empty(self):
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""

    def test_short_number(self):
        assert normalize_phone("12345") == ""


class TestExtractPhoneFromJid:
    """Tests for JID phone extraction."""

    def test_standard_jid(self):
        assert extract_phone_from_jid("15551234567@s.whatsapp.net") == "+15551234567"

    def test_international_jid(self):
        assert extract_phone_from_jid("442071234567@s.whatsapp.net") == "+442071234567"

    def test_empty_jid(self):
        assert extract_phone_from_jid("") == ""
        assert extract_phone_from_jid(None) == ""

    def test_group_jid_returns_empty(self):
        assert extract_phone_from_jid("123456789@g.us") == ""

    def test_lid_jid_returns_empty(self):
        # LID JIDs aren't real phone numbers; resolution goes via lid_phones map
        assert extract_phone_from_jid("164712046162027@lid") == ""


class TestIsGroupJid:
    """Tests for group JID detection."""

    def test_individual_jid(self):
        assert is_group_jid("15551234567@s.whatsapp.net") is False

    def test_group_jid(self):
        assert is_group_jid("123456789012345@g.us") is True

    def test_empty(self):
        assert is_group_jid("") is False
        assert is_group_jid(None) is False


class TestResolveLidPhone:
    """Unit tests for resolve_lid_phone()."""

    def test_standard_lid(self):
        lid_phones = {"164712046162027": "12125550142"}
        assert resolve_lid_phone("164712046162027@lid", lid_phones) == "+12125550142"

    def test_lid_with_colon_suffix(self):
        """LID JID with :N device suffix still resolves."""
        lid_phones = {"164712046162027": "12125550142"}
        assert resolve_lid_phone("164712046162027:0@lid", lid_phones) == "+12125550142"

    def test_lid_not_in_map(self):
        lid_phones = {"164712046162027": "12125550142"}
        assert resolve_lid_phone("999999999999@lid", lid_phones) == ""

    def test_non_lid_jid(self):
        """Non-LID JID returns empty string regardless of map contents."""
        lid_phones = {"15551234567": "15551234567"}
        assert resolve_lid_phone("15551234567@s.whatsapp.net", lid_phones) == ""

    def test_empty_input(self):
        assert resolve_lid_phone("", {}) == ""
        assert resolve_lid_phone(None, {}) == ""


class TestParseMessageTimestamp:
    """Tests for parse_message_timestamp()."""

    def test_iso_string(self):
        ts = parse_message_timestamp("2026-02-25T10:00:00+00:00")
        assert ts.year == 2026 and ts.month == 2 and ts.day == 25

    def test_iso_string_with_z(self):
        ts = parse_message_timestamp("2026-02-25T10:00:00Z")
        assert ts.year == 2026

    def test_unix_epoch(self):
        # 1735689600 = 2025-01-01 00:00:00 UTC
        ts = parse_message_timestamp(1735689600)
        assert ts.year == 2025 and ts.month == 1 and ts.day == 1

    def test_invalid_falls_back_to_now(self):
        ts = parse_message_timestamp("not a timestamp")
        # Should not raise, returns current time
        assert ts is not None


# ---------------------------------------------------------------------------
# Message processing — operates on dicts, only the interaction store is mocked
# ---------------------------------------------------------------------------

def _make_interactions_db(tmp_path, existing_ids=None) -> str:
    """Create a minimal interactions.db with optional pre-existing rows."""
    db = str(tmp_path / "interactions.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY, person_id TEXT, timestamp TEXT,
            source_type TEXT, title TEXT, snippet TEXT,
            source_link TEXT, source_id TEXT, created_at TEXT
        )
    """)
    for sid in (existing_ids or []):
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at) "
            "VALUES (?, '', '', 'whatsapp', '', '', NULL, ?, '')",
            (sid, sid),
        )
    conn.commit()
    conn.close()
    return db


def _mock_resolver_returning(person_id: str) -> MagicMock:
    """Build a resolver that always resolves to a person with the given id."""
    entity = MagicMock()
    entity.id = person_id
    result = MagicMock()
    result.entity = entity
    resolver = MagicMock()
    resolver.resolve.return_value = result
    return resolver


class TestProcessWhatsAppMessagesLidResolution:
    """LID (linked device) message resolution via push_name and lid_phones."""

    def test_lid_with_push_name_resolves(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = _mock_resolver_returning("person-hannah")

        messages = [{
            "msg_id": "msg001",
            "chat_jid": "120363001@g.us",
            "chat_name": "Test Group",
            "sender_jid": "246698660126807@lid",
            "sender_name": "",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 0,
        }]
        lid_contacts = [{"jid": "246698660126807@lid", "push_name": "Hannah"}]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[{"group_jid": "120363001@g.us", "user_jid": "246698660126807@lid"}],
                lid_contacts=lid_contacts,
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["resolved_lid"] == 1
        assert stats["skipped_lid"] == 0
        resolver.resolve.assert_called_with(
            name="Hannah", phone=None, create_if_missing=True,
        )

    def test_lid_without_push_name_skipped(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = MagicMock()

        messages = [{
            "msg_id": "msg002",
            "chat_jid": "120363001@g.us",
            "chat_name": "Test Group",
            "sender_jid": "999999999999@lid",
            "sender_name": "",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 0,
        }]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[{"group_jid": "120363001@g.us", "user_jid": "999999999999@lid"}],
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["skipped_lid"] == 1
        assert stats["resolved_lid"] == 0
        resolver.resolve.assert_not_called()

    def test_non_lid_resolves_by_phone(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = _mock_resolver_returning("person-jordan")

        messages = [{
            "msg_id": "msg003",
            "chat_jid": "15551234567@s.whatsapp.net",
            "chat_name": "Jordan",
            "sender_jid": "15551234567@s.whatsapp.net",
            "sender_name": "Jordan",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 0,
        }]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[],
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["interactions_created"] == 1
        resolver.resolve.assert_called_with(
            name="Jordan", phone="+15551234567", create_if_missing=True,
        )


class TestProcessWhatsAppMessagesOutgoingGroup:
    """Outgoing group messages fan out to each resolved participant."""

    def test_outgoing_group_creates_per_participant(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)

        # Two participants: one phone-based, one LID with push_name
        entity_a = MagicMock(id="person-phone")
        entity_b = MagicMock(id="person-alice")
        result_a = MagicMock(entity=entity_a)
        result_b = MagicMock(entity=entity_b)
        resolver = MagicMock()
        resolver.resolve.side_effect = [result_a, result_b]

        messages = [{
            "msg_id": "msg010",
            "chat_jid": "120363001@g.us",
            "chat_name": "Ski Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        group_participants = [
            {"group_jid": "120363001@g.us", "user_jid": "15559876543@s.whatsapp.net"},
            {"group_jid": "120363001@g.us", "user_jid": "111111111@lid"},
        ]
        lid_contacts = [{"jid": "111111111@lid", "push_name": "Alice"}]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=group_participants,
                lid_contacts=lid_contacts,
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["outgoing_group_created"] == 2
        assert stats["interactions_created"] == 2
        calls = resolver.resolve.call_args_list
        assert len(calls) == 2
        # Both calls must be non-creating
        for call in calls:
            assert call.kwargs.get("create_if_missing") is False
        # First call: phone-based participant — resolver receives the E.164 phone
        assert calls[0].kwargs.get("phone") == "+15559876543"
        # Second call: LID with push_name only — should be phone=None (not "") so
        # the resolver gets the explicit "no phone" signal. Mirrors the 1:1 path.
        assert calls[1].kwargs.get("name") == "Alice"
        assert calls[1].kwargs.get("phone") is None

    def test_outgoing_group_skips_unknown_participants(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = MagicMock()
        resolver.resolve.return_value = None  # Not found

        messages = [{
            "msg_id": "msg011",
            "chat_jid": "120363001@g.us",
            "chat_name": "Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        group_participants = [{"group_jid": "120363001@g.us", "user_jid": "15559876543@s.whatsapp.net"}]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=group_participants,
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["outgoing_group_created"] == 0
        assert stats["interactions_created"] == 0

    def test_outgoing_group_deduplication(self, tmp_path):
        """Source IDs prevent duplicate participant interactions."""
        int_db = _make_interactions_db(
            tmp_path,
            existing_ids=["whatsapp_msg012:15559876543@s.whatsapp.net"],
        )
        resolver = MagicMock()

        messages = [{
            "msg_id": "msg012",
            "chat_jid": "120363001@g.us",
            "chat_name": "Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        group_participants = [{"group_jid": "120363001@g.us", "user_jid": "15559876543@s.whatsapp.net"}]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=group_participants,
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["outgoing_group_created"] == 0
        resolver.resolve.assert_not_called()

    def test_large_group_skipped(self, tmp_path):
        """Groups with >LARGE_GROUP_THRESHOLD participants are skipped entirely."""
        int_db = _make_interactions_db(tmp_path)
        resolver = MagicMock()

        messages = [{
            "msg_id": "msg013",
            "chat_jid": "120363001@g.us",
            "chat_name": "Large Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        # 25 participants > threshold of 20
        group_participants = [
            {"group_jid": "120363001@g.us", "user_jid": f"{i}@s.whatsapp.net"}
            for i in range(25)
        ]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=group_participants,
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["skipped_large_group"] == 1
        assert stats["outgoing_group_created"] == 0
        resolver.resolve.assert_not_called()

    def test_outgoing_group_skips_self(self, tmp_path):
        """Outgoing group fan-out should never create a self-referential interaction."""
        int_db = _make_interactions_db(tmp_path)

        # Resolver will resolve the participant to my_person_id
        resolver = _mock_resolver_returning("me")

        messages = [{
            "msg_id": "msg014",
            "chat_jid": "120363001@g.us",
            "chat_name": "Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        group_participants = [{"group_jid": "120363001@g.us", "user_jid": "15559876543@s.whatsapp.net"}]

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=group_participants,
                lid_contacts=[],
                lid_phones={},
                dry_run=False,
                my_person_id="me",
            )

        assert stats["outgoing_group_created"] == 0


class TestProcessWhatsAppMessagesLidPhone:
    """LID phone resolution via the whatsmeow lid_phones map."""

    def test_lid_resolved_by_phone(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = _mock_resolver_returning("person-jonathan")

        messages = [{
            "msg_id": "msg_lid_phone_01",
            "chat_jid": "120363001@g.us",
            "chat_name": "Test Group",
            "sender_jid": "164712046162027@lid",
            "sender_name": "",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 0,
        }]
        lid_contacts = [{"jid": "164712046162027@lid", "push_name": "Jonathan"}]
        lid_phones = {"164712046162027": "12125550142"}

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[{"group_jid": "120363001@g.us", "user_jid": "164712046162027@lid"}],
                lid_contacts=lid_contacts,
                lid_phones=lid_phones,
                dry_run=False,
                my_person_id="me",
            )

        assert stats["resolved_lid_phone"] == 1
        resolver.resolve.assert_called_with(
            name="Jonathan", phone="+12125550142", create_if_missing=True,
        )

    def test_lid_falls_back_to_name_without_phone(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = _mock_resolver_returning("person-hannah")

        messages = [{
            "msg_id": "msg_lid_name_01",
            "chat_jid": "120363001@g.us",
            "chat_name": "Test Group",
            "sender_jid": "999888777666@lid",
            "sender_name": "",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 0,
        }]
        lid_contacts = [{"jid": "999888777666@lid", "push_name": "Hannah"}]
        lid_phones = {}  # Empty — LID has no phone mapping

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[{"group_jid": "120363001@g.us", "user_jid": "999888777666@lid"}],
                lid_contacts=lid_contacts,
                lid_phones=lid_phones,
                dry_run=False,
                my_person_id="me",
            )

        assert stats["resolved_lid_phone"] == 0
        assert stats["resolved_lid"] == 1
        resolver.resolve.assert_called_with(
            name="Hannah", phone=None, create_if_missing=True,
        )

    def test_outgoing_group_lid_resolved_by_phone(self, tmp_path):
        int_db = _make_interactions_db(tmp_path)
        resolver = _mock_resolver_returning("person-jonathan")

        messages = [{
            "msg_id": "msg_out_lid_phone_01",
            "chat_jid": "120363001@g.us",
            "chat_name": "Ski Group",
            "sender_jid": "me@s.whatsapp.net",
            "sender_name": "Me",
            "ts": "2026-02-25T10:00:00+00:00",
            "from_me": 1,
        }]
        lid_contacts = [{"jid": "164712046162027@lid", "push_name": "Jonathan"}]
        lid_phones = {"164712046162027": "12125550142"}

        with patch("api.services.whatsapp.get_entity_resolver", return_value=resolver), \
             patch("api.services.whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("api.services.person_stats.refresh_person_stats"):
            stats = process_whatsapp_messages(
                messages=messages,
                group_participants=[{"group_jid": "120363001@g.us", "user_jid": "164712046162027@lid"}],
                lid_contacts=lid_contacts,
                lid_phones=lid_phones,
                dry_run=False,
                my_person_id="my-person-id",
            )

        assert stats["resolved_lid_phone"] == 1
        assert stats["outgoing_group_created"] == 1
        resolver.resolve.assert_called_with(
            name="Jonathan", phone="+12125550142", create_if_missing=False,
        )


# ---------------------------------------------------------------------------
# Contact processing — LID contacts becoming source entities (#503)
# ---------------------------------------------------------------------------

class _FakePerson:
    """Minimal PersonEntity stand-in with real (not MagicMock) list attrs.

    process_whatsapp_contacts mutates person.phone_numbers/sources in place
    and compares person.source_entity_count, so a MagicMock's auto-generated
    attributes won't behave correctly here.
    """

    def __init__(self, person_id: str):
        self.id = person_id
        self.phone_numbers = []
        self.phone_primary = None
        self.sources = []
        self.source_entity_count = 0


def _mock_contact_stores(get_by_source_result=None, resolved_person: str = "person-x"):
    """Build source_store/person_store/resolver mocks for process_whatsapp_contacts.

    get_by_source_result: value (or side_effect callable) returned by
    source_store.get_by_source — None means no existing entity anywhere.
    """
    source_store = MagicMock()
    if callable(get_by_source_result):
        source_store.get_by_source.side_effect = get_by_source_result
    else:
        source_store.get_by_source.return_value = get_by_source_result
    source_store.add.side_effect = lambda entity: entity
    source_store.count_for_person.return_value = 1

    person_store = MagicMock()

    person = _FakePerson(resolved_person)
    result = MagicMock(entity=person, is_new=False)
    resolver = MagicMock()
    resolver.resolve.return_value = result

    return source_store, person_store, resolver, person


class TestProcessWhatsAppContactsLid:
    """LID contacts (#503) must produce source entities, deduped against classic ones."""

    def test_lid_with_name_and_resolvable_phone_creates_entity(self):
        source_store, person_store, resolver, person = _mock_contact_stores(
            get_by_source_result=None, resolved_person="person-priya",
        )
        lid_contacts = [{"jid": "246698660126807@lid", "push_name": "Priya"}]
        lid_phones = {"246698660126807": "12125550142"}

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(
                contacts=[], lid_contacts=lid_contacts, lid_phones=lid_phones, dry_run=False,
            )

        assert stats["lid_contacts_read"] == 1
        assert stats["lid_entities_created"] == 1
        assert stats["source_entities_created"] == 1
        assert stats["lid_skipped"] == 0
        assert stats["lid_merged_into_classic"] == 0
        resolver.resolve.assert_called_once_with(
            name="Priya", phone="+12125550142", create_if_missing=True,
        )
        added_entity = source_store.add.call_args.args[0]
        assert added_entity.source_id == "whatsapp_246698660126807@lid"
        assert added_entity.observed_name == "Priya"
        assert added_entity.observed_phone == "+12125550142"

    def test_lid_without_push_name_skipped(self):
        source_store, person_store, resolver, _ = _mock_contact_stores()
        lid_contacts = [{"jid": "246698660126807@lid", "push_name": ""}]

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(
                contacts=[], lid_contacts=lid_contacts, lid_phones={}, dry_run=False,
            )

        assert stats["lid_skipped"] == 1
        assert stats["lid_entities_created"] == 0
        resolver.resolve.assert_not_called()

    def test_lid_with_unresolvable_phone_skipped(self):
        source_store, person_store, resolver, _ = _mock_contact_stores()
        lid_contacts = [{"jid": "999999999999@lid", "push_name": "Bob"}]

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(
                contacts=[], lid_contacts=lid_contacts, lid_phones={}, dry_run=False,
            )

        assert stats["lid_skipped"] == 1
        assert stats["lid_entities_created"] == 0
        resolver.resolve.assert_not_called()

    def test_lid_matching_classic_contact_updates_classic_no_duplicate(self):
        classic_source_id = "whatsapp_12125550142@s.whatsapp.net"
        existing_classic = SourceEntity(
            source_type=SOURCE_WHATSAPP,
            source_id=classic_source_id,
            observed_name="Priya Old Name",
            observed_phone="+12125550142",
            canonical_person_id="person-priya",
        )

        def get_by_source(source_type, source_id):
            return existing_classic if source_id == classic_source_id else None

        source_store, person_store, resolver, person = _mock_contact_stores(
            get_by_source_result=get_by_source, resolved_person="person-priya",
        )
        lid_contacts = [{"jid": "246698660126807@lid", "push_name": "Priya"}]
        lid_phones = {"246698660126807": "12125550142"}

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(
                contacts=[], lid_contacts=lid_contacts, lid_phones=lid_phones, dry_run=False,
            )

        assert stats["lid_merged_into_classic"] == 1
        assert stats["lid_entities_created"] == 0
        assert stats["source_entities_created"] == 0
        assert stats["source_entities_updated"] == 1
        source_store.add.assert_not_called()
        source_store.update.assert_called_once()
        updated_entity = source_store.update.call_args.args[0]
        assert updated_entity.source_id == classic_source_id
        assert updated_entity.observed_name == "Priya"

    def test_lid_idempotent_second_run_updates_not_creates(self):
        lid_source_id = "whatsapp_246698660162027@lid"
        existing_lid_entity = SourceEntity(
            source_type=SOURCE_WHATSAPP,
            source_id=lid_source_id,
            observed_name="Priya",
            observed_phone="+12125550142",
            canonical_person_id="person-priya",
        )

        def get_by_source(source_type, source_id):
            # No classic entity anywhere; the LID entity from a prior run exists.
            return existing_lid_entity if source_id == lid_source_id else None

        source_store, person_store, resolver, person = _mock_contact_stores(
            get_by_source_result=get_by_source, resolved_person="person-priya",
        )
        lid_contacts = [{"jid": "246698660162027@lid", "push_name": "Priya"}]
        lid_phones = {"246698660162027": "12125550142"}

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(
                contacts=[], lid_contacts=lid_contacts, lid_phones=lid_phones, dry_run=False,
            )

        assert stats["lid_entities_created"] == 0
        assert stats["source_entities_created"] == 0
        assert stats["source_entities_updated"] == 1
        assert stats["lid_merged_into_classic"] == 0
        source_store.add.assert_not_called()
        source_store.update.assert_called_once()

    def test_classic_contact_unaffected_by_lid_processing(self):
        """Baseline: a classic contact with no LID entries still creates as before."""
        source_store, person_store, resolver, person = _mock_contact_stores(
            get_by_source_result=None, resolved_person="person-jordan",
        )
        contacts = [{"JID": "15551234567@s.whatsapp.net", "Phone": "15551234567", "Name": "Jordan"}]

        with patch("api.services.whatsapp.get_source_entity_store", return_value=source_store), \
             patch("api.services.whatsapp.get_person_entity_store", return_value=person_store), \
             patch("api.services.whatsapp.get_entity_resolver", return_value=resolver):
            stats = process_whatsapp_contacts(contacts=contacts, dry_run=False)

        assert stats["source_entities_created"] == 1
        assert stats["lid_contacts_read"] == 0
        assert stats["lid_entities_created"] == 0
        assert stats["skipped"] == 0
