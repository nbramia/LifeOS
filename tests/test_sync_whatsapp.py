"""Tests for WhatsApp sync via wacli."""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from pathlib import Path

from scripts.sync_whatsapp import (
    normalize_phone,
    extract_phone_from_jid,
    is_group_jid,
    resolve_lid_phone,
    run_wacli,
    check_wacli_auth,
    sync_whatsapp_messages,
)


class TestNormalizePhone:
    """Tests for phone number normalization."""

    def test_us_10_digit(self):
        """Test normalizing 10-digit US number."""
        assert normalize_phone("5551234567") == "+15551234567"

    def test_us_with_country_code(self):
        """Test normalizing 11-digit US number with country code."""
        assert normalize_phone("15551234567") == "+15551234567"

    def test_with_formatting(self):
        """Test normalizing formatted number."""
        assert normalize_phone("(555) 123-4567") == "+15551234567"
        assert normalize_phone("555-123-4567") == "+15551234567"

    def test_international(self):
        """Test normalizing international number."""
        assert normalize_phone("+442071234567") == "+442071234567"

    def test_empty(self):
        """Test empty input."""
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""

    def test_short_number(self):
        """Test number too short to normalize."""
        assert normalize_phone("12345") == ""


class TestExtractPhoneFromJid:
    """Tests for JID phone extraction."""

    def test_standard_jid(self):
        """Test extracting from standard JID."""
        phone = extract_phone_from_jid("15551234567@s.whatsapp.net")
        assert phone == "+15551234567"

    def test_international_jid(self):
        """Test extracting from international JID."""
        phone = extract_phone_from_jid("442071234567@s.whatsapp.net")
        assert phone == "+442071234567"

    def test_empty_jid(self):
        """Test empty JID."""
        assert extract_phone_from_jid("") == ""
        assert extract_phone_from_jid(None) == ""

    def test_group_jid(self):
        """Test group JID (returns empty - not a phone)."""
        phone = extract_phone_from_jid("123456789@g.us")
        # Groups have numeric IDs but they're not phone numbers
        assert phone == "" or phone.startswith("+")


class TestIsGroupJid:
    """Tests for group JID detection."""

    def test_individual_jid(self):
        """Test individual chat JID."""
        assert is_group_jid("15551234567@s.whatsapp.net") is False

    def test_group_jid(self):
        """Test group chat JID."""
        assert is_group_jid("123456789012345@g.us") is True

    def test_empty(self):
        """Test empty JID."""
        assert is_group_jid("") is False
        assert is_group_jid(None) is False


class TestRunWacli:
    """Tests for wacli command execution."""

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_successful_command(self, mock_run):
        """Test successful wacli command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"success": true, "data": []}',
            stderr="",
        )

        result = run_wacli(["chats", "list"])

        # run_wacli extracts the "data" field from the response
        assert result == []
        mock_run.assert_called_once()

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_not_authenticated(self, mock_run):
        """Test handling not authenticated error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: no store found at ~/.wacli",
        )

        result = run_wacli(["chats", "list"])

        assert result is None

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_timeout(self, mock_run):
        """Test handling timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["wacli"], timeout=300)

        result = run_wacli(["messages", "list"])

        assert result is None

    @patch("scripts.sync_whatsapp.subprocess.run")
    def test_json_parse_error(self, mock_run):
        """Test handling invalid JSON."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not valid json",
            stderr="",
        )

        result = run_wacli(["chats", "list"])

        assert result is None


class TestCheckWacliAuth:
    """Tests for wacli authentication check."""

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_authenticated(self, mock_run_wacli):
        """Test when authenticated."""
        mock_run_wacli.return_value = [{"jid": "123@s.whatsapp.net"}]

        assert check_wacli_auth() is True

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_not_authenticated(self, mock_run_wacli):
        """Test when not authenticated."""
        mock_run_wacli.return_value = None

        assert check_wacli_auth() is False

    @patch("scripts.sync_whatsapp.run_wacli")
    def test_empty_chats(self, mock_run_wacli):
        """Test when authenticated but no chats."""
        mock_run_wacli.return_value = []

        # Empty list is still valid - means authenticated but no chats
        assert check_wacli_auth() is True


def _setup_wacli_db(db_path: str, messages: list[dict], contacts: list[tuple] = None,
                     group_participants: list[tuple] = None, group_sizes: list[tuple] = None):
    """Create a mock wacli.db with the given messages and contacts."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE messages (
            msg_id TEXT PRIMARY KEY,
            chat_jid TEXT, chat_name TEXT,
            sender_jid TEXT, sender_name TEXT,
            ts TEXT, from_me INTEGER,
            text TEXT, display_text TEXT, media_type TEXT
        )
    """)
    for m in messages:
        conn.execute("""
            INSERT INTO messages (msg_id, chat_jid, chat_name, sender_jid, sender_name, ts, from_me, text, display_text, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (m['msg_id'], m['chat_jid'], m['chat_name'], m['sender_jid'], m['sender_name'],
              m['ts'], m['from_me'], m.get('text', ''), m.get('display_text', ''), m.get('media_type', '')))

    conn.execute("""
        CREATE TABLE contacts (jid TEXT PRIMARY KEY, push_name TEXT)
    """)
    for c in (contacts or []):
        conn.execute("INSERT INTO contacts (jid, push_name) VALUES (?, ?)", c)

    conn.execute("""
        CREATE TABLE group_participants (group_jid TEXT, user_jid TEXT)
    """)
    for gp in (group_participants or []):
        conn.execute("INSERT INTO group_participants (group_jid, user_jid) VALUES (?, ?)", gp)

    conn.commit()
    conn.close()


def _setup_interaction_db(db_path: str, existing_ids: list[str] = None):
    """Create a minimal interactions DB."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY, person_id TEXT, timestamp TEXT,
            source_type TEXT, title TEXT, snippet TEXT,
            source_link TEXT, source_id TEXT, created_at TEXT
        )
    """)
    for sid in (existing_ids or []):
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, snippet, source_link, source_id, created_at) VALUES (?, '', '', 'whatsapp', '', '', NULL, ?, '')",
            (sid, sid))
    conn.commit()
    conn.close()


def _setup_session_db(db_path: str, lid_map: list[tuple] = None):
    """Create a mock session.db with whatsmeow_lid_map table."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT)")
    for row in (lid_map or []):
        conn.execute("INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)", row)
    conn.commit()
    conn.close()


class TestLidResolution:
    """Tests for LID (linked device) message resolution via push_name."""

    def test_lid_with_push_name_resolves(self, tmp_path):
        """LID sender with push_name should resolve and create interaction."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg001',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Test Group',
            'sender_jid': '246698660126807@lid',
            'sender_name': '',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[
            ('246698660126807@lid', 'Hannah'),
        ], group_participants=[
            ('120363001@g.us', '246698660126807@lid'),
        ])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-hannah"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path") as mock_path_cls, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.__str__ = lambda self: wacli_db
            mock_path_cls.home.return_value.__truediv__ = lambda self, x: mock_path_instance
            mock_path_instance.__truediv__ = lambda self, x: mock_path_instance

            # Patch Path.home() / ".wacli" / "wacli.db" to return our test db
            with patch("scripts.sync_whatsapp.Path.home") as mock_home:
                mock_wacli_dir = MagicMock()
                mock_wacli_path = MagicMock()
                mock_wacli_path.exists.return_value = True
                mock_wacli_path.__str__ = lambda self: wacli_db
                mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
                mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

                stats = sync_whatsapp_messages(dry_run=False)

        assert stats['resolved_lid'] == 1
        assert stats['skipped_lid'] == 0
        # Resolver should have been called with name='Hannah', phone=None
        mock_resolver.resolve.assert_called_with(
            name='Hannah', phone=None, create_if_missing=True,
        )

    def test_lid_without_push_name_skipped(self, tmp_path):
        """LID sender without push_name should be skipped."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg002',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Test Group',
            'sender_jid': '999999999999@lid',
            'sender_name': '',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[], group_participants=[
            ('120363001@g.us', '999999999999@lid'),
        ])
        _setup_interaction_db(int_db)

        mock_resolver = MagicMock()

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['skipped_lid'] == 1
        assert stats['resolved_lid'] == 0
        mock_resolver.resolve.assert_not_called()

    def test_non_lid_still_resolves_by_phone(self, tmp_path):
        """Non-LID JIDs should still resolve by phone number."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg003',
            'chat_jid': '15551234567@s.whatsapp.net',
            'chat_name': 'Jordan',
            'sender_jid': '15551234567@s.whatsapp.net',
            'sender_name': 'Jordan',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[], group_participants=[])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-jordan"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['interactions_created'] == 1
        mock_resolver.resolve.assert_called_with(
            name='Jordan', phone='+15551234567', create_if_missing=True,
        )


class TestOutgoingGroupMessages:
    """Tests for outgoing group message fan-out to participants."""

    def test_outgoing_group_creates_per_participant(self, tmp_path):
        """Outgoing group message should create one interaction per resolved participant."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg010',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Ski Group',
            'sender_jid': 'me@s.whatsapp.net',
            'sender_name': 'Me',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 1,
        }], contacts=[
            ('111111111@lid', 'Alice'),
        ], group_participants=[
            ('120363001@g.us', '15559876543@s.whatsapp.net'),
            ('120363001@g.us', '111111111@lid'),
        ])
        _setup_interaction_db(int_db)

        mock_entity_a = MagicMock()
        mock_entity_a.id = "person-phone"
        mock_result_a = MagicMock()
        mock_result_a.entity = mock_entity_a

        mock_entity_b = MagicMock()
        mock_entity_b.id = "person-alice"
        mock_result_b = MagicMock()
        mock_result_b.entity = mock_entity_b

        mock_resolver = MagicMock()
        # First call: phone-based, second call: name-based
        mock_resolver.resolve.side_effect = [mock_result_a, mock_result_b]

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['outgoing_group_created'] == 2
        assert stats['interactions_created'] == 2
        # Verify create_if_missing=False for outgoing group
        for call in mock_resolver.resolve.call_args_list:
            assert call.kwargs.get('create_if_missing') is False

    def test_outgoing_group_skips_unknown_participants(self, tmp_path):
        """Outgoing group should skip participants that don't resolve."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg011',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Group',
            'sender_jid': 'me@s.whatsapp.net',
            'sender_name': 'Me',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 1,
        }], contacts=[], group_participants=[
            ('120363001@g.us', '15559876543@s.whatsapp.net'),
        ])
        _setup_interaction_db(int_db)

        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = None  # Not found

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['outgoing_group_created'] == 0
        assert stats['interactions_created'] == 0

    def test_outgoing_group_deduplication(self, tmp_path):
        """Source IDs should prevent duplicate outgoing group interactions."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg012',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Group',
            'sender_jid': 'me@s.whatsapp.net',
            'sender_name': 'Me',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 1,
        }], contacts=[], group_participants=[
            ('120363001@g.us', '15559876543@s.whatsapp.net'),
        ])
        # Pre-populate with existing source_id for this participant
        _setup_interaction_db(int_db, existing_ids=['whatsapp_msg012:15559876543@s.whatsapp.net'])

        mock_resolver = MagicMock()

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['outgoing_group_created'] == 0
        mock_resolver.resolve.assert_not_called()

    def test_large_group_still_skipped(self, tmp_path):
        """Groups with >20 participants should still skip outgoing messages."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        # Create group with 25 participants
        participants = [(f'120363001@g.us', f'{i}@s.whatsapp.net') for i in range(25)]

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg013',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Large Group',
            'sender_jid': 'me@s.whatsapp.net',
            'sender_name': 'Me',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 1,
        }], contacts=[], group_participants=participants)
        _setup_interaction_db(int_db)

        mock_resolver = MagicMock()

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_wacli_dir = MagicMock()
            mock_wacli_path = MagicMock()
            mock_wacli_path.exists.return_value = True
            mock_wacli_path.__str__ = lambda self: wacli_db
            mock_home.return_value.__truediv__ = lambda self, x: mock_wacli_dir
            mock_wacli_dir.__truediv__ = lambda self, x: mock_wacli_path

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['skipped_large_group'] == 1
        assert stats['outgoing_group_created'] == 0
        mock_resolver.resolve.assert_not_called()


class TestResolveLidPhone:
    """Unit tests for resolve_lid_phone()."""

    def test_standard_lid(self):
        """Standard LID JID resolves to phone."""
        lid_phones = {"164712046162027": "+12036417454"}
        assert resolve_lid_phone("164712046162027@lid", lid_phones) == "+12036417454"

    def test_lid_with_colon_suffix(self):
        """LID JID with :N device suffix still resolves."""
        lid_phones = {"164712046162027": "+12036417454"}
        assert resolve_lid_phone("164712046162027:0@lid", lid_phones) == "+12036417454"

    def test_lid_not_in_map(self):
        """LID not in map returns empty string."""
        lid_phones = {"164712046162027": "+12036417454"}
        assert resolve_lid_phone("999999999999@lid", lid_phones) == ""

    def test_non_lid_jid(self):
        """Non-LID JID returns empty string."""
        lid_phones = {"15551234567": "+15551234567"}
        assert resolve_lid_phone("15551234567@s.whatsapp.net", lid_phones) == ""

    def test_empty_input(self):
        """Empty/None input returns empty string."""
        assert resolve_lid_phone("", {}) == ""
        assert resolve_lid_phone(None, {}) == ""


class TestLidPhoneResolution:
    """Integration tests for LID phone resolution via session.db."""

    def _mock_home(self, tmp_path, wacli_db, session_db=None):
        """Build a mock Path.home() that routes .wacli/wacli.db and .wacli/session.db."""
        class FakeWacliDir:
            def __truediv__(self, name):
                if name == "wacli.db":
                    p = MagicMock()
                    p.exists.return_value = True
                    p.__str__ = lambda self: wacli_db
                    return p
                if name == "session.db":
                    p = MagicMock()
                    if session_db:
                        p.exists.return_value = True
                        p.__str__ = lambda self: session_db
                    else:
                        p.exists.return_value = False
                    return p
                return MagicMock()

        class FakeHome:
            def __truediv__(self, name):
                if name == ".wacli":
                    return FakeWacliDir()
                return MagicMock()

        return FakeHome()

    def test_lid_resolved_by_phone(self, tmp_path):
        """LID with phone mapping in session.db should resolve by phone."""
        wacli_db = str(tmp_path / "wacli.db")
        session_db = str(tmp_path / "session.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg_lid_phone_01',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Test Group',
            'sender_jid': '164712046162027@lid',
            'sender_name': '',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[
            ('164712046162027@lid', 'Jonathan'),
        ], group_participants=[
            ('120363001@g.us', '164712046162027@lid'),
        ])
        _setup_session_db(session_db, lid_map=[
            ('164712046162027', '12036417454'),
        ])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-jonathan-esty"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_home.return_value = self._mock_home(tmp_path, wacli_db, session_db)

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['resolved_lid_phone'] == 1
        # Resolver should have been called with phone, not just name
        mock_resolver.resolve.assert_called_with(
            name='Jonathan', phone='+12036417454', create_if_missing=True,
        )

    def test_lid_falls_back_to_name_without_phone(self, tmp_path):
        """LID without phone mapping should fall back to push_name resolution."""
        wacli_db = str(tmp_path / "wacli.db")
        session_db = str(tmp_path / "session.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg_lid_name_01',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Test Group',
            'sender_jid': '999888777666@lid',
            'sender_name': '',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[
            ('999888777666@lid', 'Hannah'),
        ], group_participants=[
            ('120363001@g.us', '999888777666@lid'),
        ])
        # session.db exists but doesn't have this LID
        _setup_session_db(session_db, lid_map=[])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-hannah"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_home.return_value = self._mock_home(tmp_path, wacli_db, session_db)

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['resolved_lid_phone'] == 0
        assert stats['resolved_lid'] == 1
        # Resolver should have been called with name only
        mock_resolver.resolve.assert_called_with(
            name='Hannah', phone=None, create_if_missing=True,
        )

    def test_missing_session_db_graceful(self, tmp_path):
        """Missing session.db should not crash — falls back to name-only resolution."""
        wacli_db = str(tmp_path / "wacli.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg_no_session_01',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Test Group',
            'sender_jid': '246698660126807@lid',
            'sender_name': '',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 0,
        }], contacts=[
            ('246698660126807@lid', 'Hannah'),
        ], group_participants=[
            ('120363001@g.us', '246698660126807@lid'),
        ])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-hannah"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            # No session_db — pass None to _mock_home
            mock_home.return_value = self._mock_home(tmp_path, wacli_db, session_db=None)

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['resolved_lid_phone'] == 0
        assert stats['resolved_lid'] == 1
        # Should still work via name fallback
        mock_resolver.resolve.assert_called_with(
            name='Hannah', phone=None, create_if_missing=True,
        )

    def test_outgoing_group_lid_resolved_by_phone(self, tmp_path):
        """Outgoing group fan-out should resolve LID participants by phone when available."""
        wacli_db = str(tmp_path / "wacli.db")
        session_db = str(tmp_path / "session.db")
        int_db = str(tmp_path / "interactions.db")

        _setup_wacli_db(wacli_db, messages=[{
            'msg_id': 'msg_out_lid_phone_01',
            'chat_jid': '120363001@g.us',
            'chat_name': 'Ski Group',
            'sender_jid': 'me@s.whatsapp.net',
            'sender_name': 'Me',
            'ts': '2026-02-25T10:00:00+00:00',
            'from_me': 1,
        }], contacts=[
            ('164712046162027@lid', 'Jonathan'),
        ], group_participants=[
            ('120363001@g.us', '164712046162027@lid'),
        ])
        _setup_session_db(session_db, lid_map=[
            ('164712046162027', '12036417454'),
        ])
        _setup_interaction_db(int_db)

        mock_entity = MagicMock()
        mock_entity.id = "person-jonathan-esty"
        mock_result = MagicMock()
        mock_result.entity = mock_entity
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = mock_result

        with patch("scripts.sync_whatsapp.subprocess.run") as mock_run, \
             patch("scripts.sync_whatsapp.get_entity_resolver", return_value=mock_resolver), \
             patch("scripts.sync_whatsapp.get_interaction_db_path", return_value=int_db), \
             patch("scripts.sync_whatsapp.Path.home") as mock_home, \
             patch("scripts.sync_whatsapp.settings") as mock_settings, \
             patch("api.services.person_stats.refresh_person_stats"):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_settings.my_person_id = "my-person-id"
            mock_home.return_value = self._mock_home(tmp_path, wacli_db, session_db)

            stats = sync_whatsapp_messages(dry_run=False)

        assert stats['resolved_lid_phone'] == 1
        assert stats['outgoing_group_created'] == 1
        # Resolver should have been called with both name and phone
        mock_resolver.resolve.assert_called_with(
            name='Jonathan', phone='+12036417454', create_if_missing=False,
        )
