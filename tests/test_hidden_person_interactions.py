"""Sync paths must not insert interactions for hidden people.

Consistency verification deletes interactions that belong to hidden people, so
a sync that inserts them re-creates the rows every night. These tests cover the
Gmail, Calendar, phone, and WhatsApp insert paths against synthetic databases
and mocked sources, and check that insert counters report rows actually written.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.services.gmail import EmailMessage
from api.services.google_auth import GoogleAccount
from api.services.interaction_store import InteractionStore

pytestmark = pytest.mark.unit


def _entity(person_id: str, hidden: bool = False) -> MagicMock:
    entity = MagicMock()
    entity.id = person_id
    entity.hidden = hidden
    entity.canonical_name = f"Synthetic {person_id}"
    entity.phone_numbers = []
    entity.phone_primary = None
    entity.sources = ["phone"]
    return entity


def _resolver_by(attr: str, people: dict) -> MagicMock:
    """Resolver that returns the entity registered for the given email/phone."""

    def resolve(**kwargs):
        entity = people.get(kwargs.get(attr))
        if entity is None:
            return None
        return MagicMock(entity=entity, confidence=1.0, match_type="exact")

    resolver = MagicMock()
    resolver.resolve.side_effect = resolve
    return resolver


def _source_store() -> MagicMock:
    store = MagicMock()
    store.add_or_update.return_value = (MagicMock(), True)
    return store


def _rows(db_path: str, source_type: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT person_id, source_id FROM interactions WHERE source_type = ? "
            "ORDER BY source_id",
            (source_type,),
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def interactions_db(tmp_path):
    path = str(tmp_path / "interactions.db")
    InteractionStore(db_path=path, strict=False)
    return path


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _gmail_email(message_id, sender, to=None, cc=None):
    return EmailMessage(
        message_id=message_id,
        thread_id=f"t_{message_id}",
        subject="Synthetic subject",
        sender=sender,
        sender_name="Synthetic Sender",
        date=datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc),
        snippet="snippet",
        source_account="personal",
        to=to,
        cc=cc,
    )


def _run_gmail(interactions_db, emails, people, tmp_path, source_store=None, dry_run=False,
               batch_size=100):
    from api.services.gmail_skip_cache import GmailSkipCache
    from scripts.sync_gmail_calendar_interactions import sync_gmail_interactions

    gmail = MagicMock()
    gmail.service.users().messages().list().execute.return_value = {
        "messages": [{"id": e.message_id} for e in emails]
    }
    by_id = {e.message_id: e for e in emails}
    gmail.get_messages_batch.side_effect = lambda ids, **kw: {i: by_id[i] for i in ids}
    cache = GmailSkipCache(db_path=str(tmp_path / "skip.db"))

    with patch("scripts.sync_gmail_calendar_interactions.GmailService", return_value=gmail), \
         patch("scripts.sync_gmail_calendar_interactions.get_gmail_skip_cache", return_value=cache), \
         patch("scripts.sync_gmail_calendar_interactions.get_interaction_db_path",
               return_value=interactions_db), \
         patch("scripts.sync_gmail_calendar_interactions.get_source_entity_store",
               return_value=source_store or _source_store()), \
         patch("scripts.sync_gmail_calendar_interactions.get_entity_resolver",
               return_value=_resolver_by("email", people)):
        return sync_gmail_interactions(
            account_type=GoogleAccount.PERSONAL, dry_run=dry_run, days_back=30,
            batch_size=batch_size,
        )


class TestGmailHiddenPeople:
    def test_hidden_sender_gets_no_interaction(self, interactions_db, tmp_path):
        people = {
            "ghost@example.com": _entity("ghost", hidden=True),
            "dana@example.com": _entity("dana"),
        }
        emails = [
            _gmail_email("m1", "ghost@example.com"),
            _gmail_email("m2", "dana@example.com"),
        ]

        stats = _run_gmail(interactions_db, emails, people, tmp_path)

        assert _rows(interactions_db, "gmail") == [("dana", "m2")]
        assert stats["inserted"] == 1

    def test_hidden_cc_participant_is_skipped_but_sender_is_kept(
        self, interactions_db, tmp_path
    ):
        people = {
            "dana@example.com": _entity("dana"),
            "ghost@example.com": _entity("ghost", hidden=True),
            "lee@example.com": _entity("lee"),
        }
        emails = [_gmail_email(
            "m1", "dana@example.com", to="me@example.com",
            cc="ghost@example.com, lee@example.com",
        )]

        _run_gmail(interactions_db, emails, people, tmp_path)

        assert sorted(r[0] for r in _rows(interactions_db, "gmail")) == ["dana", "lee"]

    def test_hidden_recipient_of_a_sent_email_is_skipped(self, interactions_db, tmp_path):
        people = {
            "me@example.com": _entity("me-person"),
            "ghost@example.com": _entity("ghost", hidden=True),
            "lee@example.com": _entity("lee"),
        }
        emails = [_gmail_email(
            "m1", "me@example.com", to="ghost@example.com, lee@example.com",
        )]

        with patch("scripts.sync_gmail_calendar_interactions.settings") as settings:
            settings.my_person_id = "me-person"
            _run_gmail(interactions_db, emails, people, tmp_path)

        assert [r[0] for r in _rows(interactions_db, "gmail")] == ["lee"]

    def test_hidden_sender_source_entity_is_still_linked(self, interactions_db, tmp_path):
        people = {"ghost@example.com": _entity("ghost", hidden=True)}
        store = _source_store()

        _run_gmail(interactions_db, [_gmail_email("m1", "ghost@example.com")], people,
                   tmp_path, source_store=store)

        store.add_or_update.assert_called_once()

    def test_source_entities_created_counts_only_new_entities(self, interactions_db, tmp_path):
        people = {"dana@example.com": _entity("dana")}
        store = MagicMock()
        store.add_or_update.return_value = (MagicMock(), False)

        stats = _run_gmail(interactions_db, [_gmail_email("m1", "dana@example.com")],
                           people, tmp_path, source_store=store)

        assert stats["inserted"] == 1
        assert stats["source_entities_created"] == 0

    def test_inserted_counts_rows_written_not_rows_attempted(self, interactions_db, tmp_path):
        people = {
            "dana@example.com": _entity("dana"),
            "lee@example.com": _entity("lee"),
        }
        emails = [_gmail_email("m1", "dana@example.com"),
                  _gmail_email("m2", "lee@example.com")]

        with patch.object(uuid, "uuid4", lambda: uuid.UUID(int=1)):
            stats = _run_gmail(interactions_db, emails, people, tmp_path)

        assert len(_rows(interactions_db, "gmail")) == 1
        assert stats["inserted"] == 1


    def test_inserted_counts_rows_written_across_mid_run_batches(self, interactions_db, tmp_path):
        people = {f"p{n}@example.com": _entity(f"p{n}") for n in range(3)}
        emails = [_gmail_email(f"m{n}", f"p{n}@example.com") for n in range(3)]

        with patch.object(uuid, "uuid4", lambda: uuid.UUID(int=1)):
            stats = _run_gmail(interactions_db, emails, people, tmp_path, batch_size=1)

        assert len(_rows(interactions_db, "gmail")) == 1
        assert stats["inserted"] == 1


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _run_calendar(interactions_db, events, people, source_store=None, dry_run=False):
    from scripts.sync_gmail_calendar_interactions import sync_calendar_interactions

    calendar = MagicMock()
    calendar.get_events_in_range.return_value = events

    with patch("scripts.sync_gmail_calendar_interactions.CalendarService", return_value=calendar), \
         patch("scripts.sync_gmail_calendar_interactions.get_interaction_db_path",
               return_value=interactions_db), \
         patch("scripts.sync_gmail_calendar_interactions.get_source_entity_store",
               return_value=source_store or _source_store()), \
         patch("scripts.sync_gmail_calendar_interactions.get_entity_resolver",
               return_value=_resolver_by("email", people)):
        return sync_calendar_interactions(
            account_type=GoogleAccount.PERSONAL, dry_run=dry_run, days_back=30
        )


def _event(event_id, attendees):
    from api.services.calendar import CalendarEvent
    start = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
    return CalendarEvent(
        event_id=event_id, title="Synthetic sync", start_time=start,
        end_time=start + timedelta(hours=1), source_account="personal",
        attendees=attendees,
    )


class TestCalendarHiddenPeople:
    def test_hidden_attendee_gets_no_interaction_but_others_do(self, interactions_db):
        people = {
            "dana@example.com": _entity("dana"),
            "ghost@example.com": _entity("ghost", hidden=True),
        }
        events = [_event("ev1", ["dana@example.com", "ghost@example.com"])]

        stats = _run_calendar(interactions_db, events, people)

        assert [r[0] for r in _rows(interactions_db, "calendar")] == ["dana"]
        assert stats["interactions_inserted"] == 1

    def test_hidden_attendee_source_entity_is_still_linked(self, interactions_db):
        people = {"ghost@example.com": _entity("ghost", hidden=True)}
        store = _source_store()

        _run_calendar(interactions_db, [_event("ev1", ["ghost@example.com"])], people,
                      source_store=store)

        store.add_or_update.assert_called_once()
        assert _rows(interactions_db, "calendar") == []

    def test_counters_report_rows_written(self, interactions_db):
        people = {"dana@example.com": _entity("dana"), "lee@example.com": _entity("lee")}
        store = MagicMock()
        store.add_or_update.return_value = (MagicMock(), False)
        events = [_event("ev1", ["dana@example.com", "lee@example.com"])]

        with patch.object(uuid, "uuid4", lambda: uuid.UUID(int=1)):
            stats = _run_calendar(interactions_db, events, people, source_store=store)

        assert len(_rows(interactions_db, "calendar")) == 1
        assert stats["interactions_inserted"] == 1
        assert stats["source_entities_created"] == 0


# ---------------------------------------------------------------------------
# Phone calls
# ---------------------------------------------------------------------------

def _build_call_history(path: str, calls: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ZCALLRECORD (ZUNIQUE_ID TEXT, ZDATE REAL, ZDURATION REAL, "
        "ZADDRESS TEXT, ZNAME TEXT, ZORIGINATED INTEGER, ZANSWERED INTEGER, "
        "ZCALLTYPE INTEGER)"
    )
    conn.executemany("INSERT INTO ZCALLRECORD VALUES (?,?,?,?,?,?,?,?)", calls)
    conn.commit()
    conn.close()


def _run_phone(interactions_db, tmp_path, calls, people):
    from scripts.sync_phone_calls import CORE_DATA_EPOCH, sync_phone_calls

    history = str(tmp_path / "CallHistory.storedata")
    recent = (datetime.now(timezone.utc) - timedelta(days=1) - CORE_DATA_EPOCH).total_seconds()
    _build_call_history(history, [
        (uid, recent + offset, 30.0, address, None, 1, 1, 1)
        for offset, (uid, address) in enumerate(calls)
    ])
    source_store = MagicMock()
    source_store.get_by_source.return_value = None
    source_store.add.side_effect = lambda entity: entity

    with patch("scripts.sync_phone_calls.get_callhistory_db_path", return_value=history), \
         patch("scripts.sync_phone_calls.get_interaction_db_path", return_value=interactions_db), \
         patch("scripts.sync_phone_calls.get_entity_resolver",
               return_value=_resolver_by("phone", people)), \
         patch("scripts.sync_phone_calls.get_source_entity_store", return_value=source_store), \
         patch("scripts.sync_phone_calls.get_person_entity_store", return_value=MagicMock()), \
         patch("api.services.person_stats.refresh_person_stats"):
        return sync_phone_calls(dry_run=False)


class TestPhoneHiddenPeople:
    def test_hidden_caller_gets_no_interaction(self, interactions_db, tmp_path):
        people = {
            "+15550000001": _entity("dana"),
            "+15550000002": _entity("ghost", hidden=True),
        }
        calls = [("call-1", "+15550000001"), ("call-2", "+15550000002")]

        stats = _run_phone(interactions_db, tmp_path, calls, people)

        assert _rows(interactions_db, "phone") == [("dana", "call-1")]
        assert stats["interactions_created"] == 1
        assert stats["hidden_person"] == 1

    def test_counter_reports_rows_written(self, interactions_db, tmp_path):
        people = {"+15550000001": _entity("dana")}
        calls = [("call-1", "+15550000001"), ("call-2", "+15550000001")]

        with patch.object(uuid, "uuid4", lambda: uuid.UUID(int=1)):
            stats = _run_phone(interactions_db, tmp_path, calls, people)

        assert len(_rows(interactions_db, "phone")) == 1
        assert stats["interactions_created"] == 1


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

def _run_whatsapp(interactions_db, messages, people, group_participants=()):
    from api.services.whatsapp import process_whatsapp_messages

    with patch("api.services.whatsapp.get_entity_resolver",
               return_value=_resolver_by("phone", people)), \
         patch("api.services.whatsapp.get_interaction_db_path", return_value=interactions_db), \
         patch("api.services.person_stats.refresh_person_stats"):
        return process_whatsapp_messages(
            messages=messages,
            group_participants=list(group_participants),
            lid_contacts=[],
            lid_phones={},
            dry_run=False,
            my_person_id="me",
        )


def _wa_message(msg_id, chat_jid, from_me=0, sender_jid=None):
    return {
        "msg_id": msg_id,
        "chat_jid": chat_jid,
        "chat_name": "Synthetic chat",
        "sender_jid": sender_jid or chat_jid,
        "sender_name": "Synthetic",
        "ts": "2025-03-01T12:00:00+00:00",
        "from_me": from_me,
        "text": "hello",
    }


class TestWhatsAppHiddenPeople:
    def test_hidden_one_to_one_contact_gets_no_interaction(self, interactions_db):
        people = {
            "+15550000001": _entity("dana"),
            "+15550000002": _entity("ghost", hidden=True),
        }
        messages = [
            _wa_message("w1", "15550000001@s.whatsapp.net"),
            _wa_message("w2", "15550000002@s.whatsapp.net"),
        ]

        stats = _run_whatsapp(interactions_db, messages, people)

        assert _rows(interactions_db, "whatsapp") == [("dana", "whatsapp_w1")]
        assert stats["interactions_created"] == 1
        assert stats["hidden_person"] == 1

    def test_hidden_participant_of_outgoing_group_message_is_skipped(self, interactions_db):
        people = {
            "+15550000001": _entity("dana"),
            "+15550000002": _entity("ghost", hidden=True),
        }
        group = "120363001@g.us"
        participants = [
            {"group_jid": group, "user_jid": "15550000001@s.whatsapp.net"},
            {"group_jid": group, "user_jid": "15550000002@s.whatsapp.net"},
        ]

        stats = _run_whatsapp(
            interactions_db, [_wa_message("w1", group, from_me=1)], people, participants
        )

        assert [r[0] for r in _rows(interactions_db, "whatsapp")] == ["dana"]
        assert stats["interactions_created"] == 1
        assert stats["outgoing_group_created"] == 1

    def test_counter_reports_rows_written(self, interactions_db):
        people = {"+15550000001": _entity("dana")}
        messages = [
            _wa_message("w1", "15550000001@s.whatsapp.net"),
            _wa_message("w2", "15550000001@s.whatsapp.net"),
        ]

        with patch("api.services.whatsapp.uuid.uuid4", lambda: uuid.UUID(int=1)):
            stats = _run_whatsapp(interactions_db, messages, people)

        assert len(_rows(interactions_db, "whatsapp")) == 1
        assert stats["interactions_created"] == 1
