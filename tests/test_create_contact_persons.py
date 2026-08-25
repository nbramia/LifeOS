"""
Tests for scripts/create_contact_persons.py (#700).

A contacts SourceEntity never creates a PersonEntity on its own, and
link_imessage_entities.py only links handles to *existing* people. So a
contact who only ever texts (no WhatsApp, no email correspondence) could
never get a person entity, no matter how deep the iMessage history. This
script closes that gap: a contacts SE with enough iMessage evidence (phone
or email handle) gets a person created and both the contact SE and the
matching message rows linked to it.

All fixtures use synthetic names/numbers and tmp_path-backed databases —
never the real data/ directory. The EntityResolver's link-override lookup
(api.services.entity_resolver.get_link_override_store) is monkeypatched to a
tmp_path db too, since its default constructor points at the real
data/crm.db.
"""
import sqlite3
from contextlib import closing

import pytest

from api.services.entity_resolver import EntityResolver
from api.services.imessage import IMessageStore
from api.services.link_override import LinkOverrideStore
from api.services.person_entity import PersonEntityStore, compute_person_category
from api.services.source_entity import (
    SourceEntityStore,
    create_contacts_source_entity,
)

from scripts.create_contact_persons import create_contact_persons

pytestmark = pytest.mark.unit

THRESHOLD = 5


@pytest.fixture(autouse=True)
def _isolate_link_override_store(tmp_path, monkeypatch):
    """EntityResolver.resolve_by_name() calls get_link_override_store()
    unconditionally when no exact match is found. Its default db_path is the
    real data/crm.db -- redirect it to a tmp_path db for every test in this
    file so nothing here can touch real data.
    """
    store = LinkOverrideStore(db_path=tmp_path / "link_overrides.db")
    monkeypatch.setattr(
        "api.services.entity_resolver.get_link_override_store", lambda: store
    )


@pytest.fixture
def person_store(tmp_path, monkeypatch):
    store = PersonEntityStore(str(tmp_path / "crm.db"))
    # source_store.link_to_person() and .add() resolve merge chains via the
    # global get_person_entity_store() singleton internally -- point it at
    # this same tmp_path store so linking lands in the store the test reads.
    monkeypatch.setattr(
        "api.services.person_entity.get_person_entity_store", lambda *a, **kw: store
    )
    return store


@pytest.fixture
def source_store(tmp_path):
    return SourceEntityStore(str(tmp_path / "crm.db"))


@pytest.fixture
def resolver(person_store):
    return EntityResolver(entity_store=person_store)


@pytest.fixture
def imessage_db_path(tmp_path):
    path = str(tmp_path / "imessage.db")
    IMessageStore(path)  # creates schema
    return path


def _insert_messages(db_path, handle, handle_normalized, count, start_rowid=1):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for i in range(count):
            conn.execute(
                """
                INSERT INTO messages
                    (rowid, text, timestamp, is_from_me, handle, handle_normalized, service)
                VALUES (?, ?, ?, 0, ?, ?, 'iMessage')
                """,
                (start_rowid + i, f"msg {i}", f"2026-01-{i+1:02d}T00:00:00+00:00", handle, handle_normalized),
            )


def _add_contact(source_store, name=None, phone=None, email=None, contact_id="c1"):
    se = create_contacts_source_entity(contact_id, name=name, email=email, phone=phone)
    return source_store.add(se)


def _run(source_store, person_store, resolver, imessage_db_path, dry_run=False, min_messages=THRESHOLD):
    return create_contact_persons(
        dry_run=dry_run,
        min_messages=min_messages,
        source_store=source_store,
        person_store=person_store,
        resolver=resolver,
        imessage_db_path=imessage_db_path,
    )


class TestPhoneEvidenceCreation:
    def test_phone_with_enough_messages_creates_and_links_person(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        phone = "+15550001111"
        contact = _add_contact(source_store, name="Jordan Rivera", phone=phone)
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD)

        stats = _run(source_store, person_store, resolver, imessage_db_path)

        assert stats["persons_created"] == 1
        assert stats["messages_linked"] == THRESHOLD

        updated = source_store.get_by_source("contacts", contact.source_id)
        assert updated.canonical_person_id is not None

        person = person_store.get_by_id(updated.canonical_person_id)
        assert person is not None
        assert person.canonical_name == "Jordan Rivera"

        with closing(sqlite3.connect(imessage_db_path)) as conn:
            rows = conn.execute(
                "SELECT person_entity_id FROM messages WHERE handle_normalized = ?", (phone,)
            ).fetchall()
        assert all(r[0] == person.id for r in rows)


class TestEmailEvidenceCreation:
    def test_email_only_contact_with_enough_messages_creates_person(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        """Motivating case: a phone-less, email-only contact (#700)."""
        email = "avery.chen@example.com"
        contact = _add_contact(source_store, name="Avery Chen", email=email)
        # Handle case differs from the stored (lowercased) email to mirror
        # real chat.db data and confirm matching is case-insensitive.
        _insert_messages(imessage_db_path, "Avery.Chen@Example.com", None, THRESHOLD)

        stats = _run(source_store, person_store, resolver, imessage_db_path)

        assert stats["persons_created"] == 1
        updated = source_store.get_by_source("contacts", contact.source_id)
        assert updated.canonical_person_id is not None

        person = person_store.get_by_id(updated.canonical_person_id)
        assert person.canonical_name == "Avery Chen"

        with closing(sqlite3.connect(imessage_db_path)) as conn:
            rows = conn.execute(
                "SELECT person_entity_id FROM messages WHERE LOWER(handle) = ?", (email,)
            ).fetchall()
        assert all(r[0] == person.id for r in rows)


class TestBelowThreshold:
    def test_below_threshold_creates_nothing(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        phone = "+15550002222"
        contact = _add_contact(source_store, name="Sam Okafor", phone=phone)
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD - 1)

        stats = _run(source_store, person_store, resolver, imessage_db_path)

        assert stats["persons_created"] == 0
        assert stats["no_evidence"] == 1
        updated = source_store.get_by_source("contacts", contact.source_id)
        assert updated.canonical_person_id is None
        assert person_store.get_all() == []


class TestNoContactRecord:
    def test_messages_alone_never_create_a_person(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        """Address book ≠ CRM: interaction evidence with no contacts SE at
        all must never create a person -- this script only ever iterates
        unlinked contacts SEs.
        """
        phone = "+15550003333"
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD * 5)

        stats = _run(source_store, person_store, resolver, imessage_db_path)

        assert stats["contacts_checked"] == 0
        assert stats["persons_created"] == 0
        assert person_store.get_all() == []


class TestIdempotency:
    def test_rerun_does_not_duplicate(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        phone = "+15550004444"
        contact = _add_contact(source_store, name="Riley Fontaine", phone=phone)
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD)

        first = _run(source_store, person_store, resolver, imessage_db_path)
        assert first["persons_created"] == 1

        second = _run(source_store, person_store, resolver, imessage_db_path)
        assert second["persons_created"] == 0
        assert second["contacts_checked"] == 0  # already linked, no longer "unlinked"

        assert len(person_store.get_all()) == 1
        updated = source_store.get_by_source("contacts", contact.source_id)
        assert updated.canonical_person_id is not None


class TestExistingLinkedUntouched:
    def test_already_linked_contact_is_byte_identical(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        phone = "+15550005555"
        # Person + link already exist before this script ever runs (e.g.
        # created via WhatsApp import, as in the issue's control case).
        existing_result = resolver.resolve(name="Morgan Ito", phone=phone, create_if_missing=True)
        person = existing_result.entity
        contact = _add_contact(source_store, name="Morgan Ito", phone=phone)
        source_store.link_to_person(contact.id, person.id, confidence=0.95)

        before = source_store.get_by_source("contacts", contact.source_id)
        # Plenty of evidence present, but should never be re-processed.
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD * 10)

        stats = _run(source_store, person_store, resolver, imessage_db_path)

        assert stats["contacts_checked"] == 0
        after = source_store.get_by_source("contacts", contact.source_id)
        assert after.canonical_person_id == before.canonical_person_id
        assert after.link_confidence == before.link_confidence
        assert after.linked_at == before.linked_at
        assert len(person_store.get_all()) == 1


class TestCategorizationReachable:
    def test_created_person_gets_family_category_on_standard_recompute(
        self, source_store, person_store, resolver, imessage_db_path, monkeypatch
    ):
        """This script must not duplicate family/work categorization logic --
        it only has to make sure the person exists and is linked so the
        normal compute_person_category() pass (run on every person during
        the nightly "strengths" step) can categorize it like anyone else.
        """
        phone = "+15550006666"
        _add_contact(source_store, name="Casey Whitfield", phone=phone)
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD)

        stats = _run(source_store, person_store, resolver, imessage_db_path)
        assert stats["persons_created"] == 1

        person = person_store.get_all()[0]
        assert person.category == "unknown"  # not categorized at creation time

        import api.services.person_entity as person_entity_mod
        monkeypatch.setattr(person_entity_mod, "FAMILY_LAST_NAMES", {"whitfield"})
        monkeypatch.setattr(person_entity_mod, "FAMILY_EXACT_NAMES", set())
        monkeypatch.setattr(person_entity_mod, "FAMILY_PERSON_IDS", set())

        category = compute_person_category(person, source_entities=[])
        assert category == "family"


class TestDryRun:
    def test_dry_run_creates_nothing(
        self, source_store, person_store, resolver, imessage_db_path
    ):
        phone = "+15550007777"
        contact = _add_contact(source_store, name="Devon Marsh", phone=phone)
        _insert_messages(imessage_db_path, phone, phone, THRESHOLD)

        stats = _run(source_store, person_store, resolver, imessage_db_path, dry_run=True)

        assert stats["persons_created"] == 1  # reported, but not persisted
        assert person_store.get_all() == []
        updated = source_store.get_by_source("contacts", contact.source_id)
        assert updated.canonical_person_id is None
