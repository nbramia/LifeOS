"""
Decide whether an email sender is a service or newsletter rather than a person.

classify_sender weighs a sender's address shape, name shape, and mail. It sees one
message for a sender no one has met, or a whole history for a person in the CRM.
The line it draws is a person's own address: a mailbox provider, their own site, or
a local part carrying their name. A role mailbox is a service even when someone with
a name answers it and the owner has replied, and an own address is a service only
when it sends steadily and nothing has ever come back.

Some things put a row out of scope however it looks: family, one-to-one messages or
calls, a phone number, a human's confirmation of an own address, manual overrides and
tags, and mail the owner wrote to an own address. Calendar invites, contact-book
entries, and note mentions don't vouch for a service-shaped row.

A hidden service keeps its interactions, including new mail, and holds its addresses
so no later sync brings it back as a new person. It answers to no name. Only its
service-shaped addresses are blocked. Two-way mail on an own address restores it, and
so does restore_person.
"""
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from api.services.entity_resolver import NON_NAME_WORDS, parse_name
from api.services.person_entity import (
    FAMILY_PERSON_IDS,
    HIDDEN_SERVICE_REASON_PREFIX,
    PersonEntity,
    PersonEntityStore,
    _is_family_member,
    is_hidden_service,
)
from api.services.relationship import TYPE_COWORKER, TYPE_INFERRED
from config.marketing_patterns import (
    AUTOMATED_LOCAL_SUBSTRINGS,
    AUTOMATED_LOCAL_WORDS,
    LIST_HOST_DOMAINS,
    ORGANIZATION_NAME_WORDS,
    PERSONA_NAME_MARKERS,
    PERSONAL_MAIL_DOMAINS,
    RESERVED_DOMAINS,
    RESERVED_TLDS,
    ROLE_LOCAL_COMPOUND_WORDS,
    ROLE_LOCAL_WORDS,
    SENDING_HOST_LABELS,
    SENDING_HOST_SUBSTRINGS,
    is_blocklisted_domain,
)
from config.relationship_weights import (
    CIRCLE_OVERRIDES_BY_ID,
    STRENGTH_OVERRIDES_BY_ID,
    TAG_OVERRIDES_BY_ID,
)
from config.settings import settings

logger = logging.getLogger(__name__)

# Added by a restore. A tag protects its row, so the nightly pass leaves it visible.
CONFIRMED_PERSON_TAG = "confirmed-person"

SIGNAL_AUTOMATED = "automated_address"
SIGNAL_GENERATED = "generated_address"
SIGNAL_INFRASTRUCTURE = "mail_infrastructure_domain"
SIGNAL_LIST_HOST = "mailing_list_host"
SIGNAL_RESERVED = "reserved_domain"
SIGNAL_SENDING_HOST = "sending_host"
SIGNAL_MAIL_BRANDED = "mail_branded_domain"
SIGNAL_ROLE = "role_address"
SIGNAL_BRAND_NAME = "named_after_domain"
SIGNAL_ORG_NAME = "organization_name"
SIGNAL_PERSONA = "brand_persona_name"
SIGNAL_ONE_WORD = "one_word_name"
SIGNAL_NOT_A_NAME = "not_a_name"
SIGNAL_ONE_WAY = "one_way_volume"
SIGNAL_BULK_MAIL = "bulk_mail_headers"

# Messages received, with no thread and no reply ever, before any address stops
# counting as a relationship.
ONE_WAY_MIN_RECEIVED = 10
# How much thread and reply traffic still reads as one-way on an address that isn't
# someone's own. A colleague turns up in threads and gets answers; a mailer doesn't.
ONE_WAY_MAX_THREAD_SHARE = 0.1
ONE_WAY_MAX_SENT_SHARE = 0.05
# Messages before a thin signal is enough: an own-looking address on a sending host,
# an organization's name on an ordinary address, a multi-word name running into the
# domain, or one word that isn't in the address.
THIN_SIGNAL_MIN_RECEIVED = 2
# One word copied into the local part reads like a first name until this much one-way mail.
ONE_WORD_OWN_ADDRESS_MAX_RECEIVED = 5
# Share of the mail checked that has to carry bulk headers, and the fewest messages
# checked before that means anything on a sender with more mail than that.
BULK_MIN_SHARE = 0.6
BULK_MIN_CHECKED = 3
# Share of mail from an own address that makes it count.
OWN_ADDRESS_MIN_SHARE = 0.1
# Share of mail the service-shaped addresses have to carry.
SERVICE_ADDRESS_MIN_SHARE = 0.5
# A calendar event this small is a meeting rather than an invitation to a crowd.
REAL_MEETING_MAX_ATTENDEES = 5

# The nightly pass hides new arrivals. More than this at once means the one-time
# repair hasn't been reviewed and run, which is a human's call.
NIGHTLY_MAX_HIDES = 50

INSTITUTIONAL_TLDS = frozenset({"edu", "gov", "mil"})
INSTITUTIONAL_SECOND_LEVEL = frozenset({"ac", "edu", "gov", "mil"})
COUNTRY_SECOND_LEVEL = frozenset({"co", "com", "net", "org", *INSTITUTIONAL_SECOND_LEVEL})

_NOT_A_NAME_CHARACTERS = re.compile(r"[\d@/\\<>|=*_#®™©&]")
_PERSONA_PAREN = re.compile(r"\(.+\)")
_WORD = re.compile(r"[^\W\d_]+")
_FIRST_DOT_LAST = re.compile(r"[a-z]{3,}\.[a-z]{3,}")
_DOMAIN_LIKE_NAME = re.compile(r"\b[^\W\d_]{2,}\.(com|org|net|io|co|ai|me)\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\d{8,}")
_LETTERS_AND_DIGITS = re.compile(r"[a-z]+|\d+")
_ORGANIZATION_WORDS = NON_NAME_WORDS | ORGANIZATION_NAME_WORDS


@dataclass(frozen=True)
class AddressShape:
    """What one address's shape says about its owner."""

    local: str
    domain: str
    signals: tuple[str, ...]
    personal_provider: bool = False

    @property
    def automated(self) -> bool:
        return bool({SIGNAL_AUTOMATED, SIGNAL_LIST_HOST, SIGNAL_RESERVED} & set(self.signals))

    @property
    def generated(self) -> bool:
        return SIGNAL_GENERATED in self.signals

    @property
    def infrastructure(self) -> bool:
        return SIGNAL_INFRASTRUCTURE in self.signals

    @property
    def sending(self) -> bool:
        return bool({SIGNAL_SENDING_HOST, SIGNAL_MAIL_BRANDED} & set(self.signals))

    @property
    def role(self) -> bool:
        return SIGNAL_ROLE in self.signals


@dataclass(frozen=True)
class SenderEvidence:
    """A sender's name, addresses with message counts, and how their mail moved."""

    name: str
    addresses: Mapping[str, int]
    received: int = 1
    thread: int = 0
    sent_to: Mapping[str, int] = field(default_factory=dict)
    bulk: int = 0
    checked: int = 0

    @property
    def sent(self) -> int:
        return sum(self.sent_to.values())

    @property
    def has_history(self) -> bool:
        return bool(self.thread or self.sent)


@dataclass(frozen=True)
class SenderVerdict:
    """A classification, the signals that fired, and the rule that decided it."""

    is_service: bool
    signals: tuple[str, ...] = ()
    reason: str = ""
    own_addresses: tuple[str, ...] = ()
    wrote_to_own_address: bool = False


# ------------------------------------------------------------------- address


def _split_address(address: str) -> tuple[str, str]:
    cleaned = address.strip().strip("<>").casefold()
    local, _, domain = cleaned.rpartition("@")
    return local, domain


def _registered_labels(domain: str) -> tuple[list[str], str]:
    """A domain's host labels below the registered name, and that name's own label."""
    labels = domain.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in COUNTRY_SECOND_LEVEL:
        return labels[:-3], labels[-3]
    if len(labels) >= 2:
        return labels[:-2], labels[-2]
    return [], domain


def _host_labels(domain: str) -> list[str]:
    """Every label of a domain above its public suffix."""
    subdomains, registered = _registered_labels(domain)
    return [*subdomains, registered]


def _is_institutional(domain: str) -> bool:
    labels = domain.split(".")
    if labels[-1] in INSTITUTIONAL_TLDS:
        return True
    return len(labels) >= 2 and len(labels[-1]) == 2 and labels[-2] in INSTITUTIONAL_SECOND_LEVEL


def _is_personal_provider(domain: str) -> bool:
    if domain in PERSONAL_MAIL_DOMAINS:
        return True
    # Regional forms of a provider: yahoo.co.uk, outlook.de, hotmail.fr.
    labels = domain.split(".")
    regional = (len(labels) == 2 and len(labels[1]) == 2) or (
        len(labels) == 3 and len(labels[2]) == 2 and labels[1] in COUNTRY_SECOND_LEVEL
    )
    return regional and f"{labels[0]}.com" in PERSONAL_MAIL_DOMAINS


def _has_role_word(local: str) -> bool:
    base = local.split("+", 1)[0]
    words = [word for word in re.split(r"[^a-z0-9]+", base) if word]
    if any(re.sub(r"\d+$", "", word) in ROLE_LOCAL_WORDS for word in words):
        return True
    return any(
        word.startswith(compound) or word.endswith(compound)
        for word in words
        for compound in ROLE_LOCAL_COMPOUND_WORDS
    )


def _is_sending_host(subdomains: list[str]) -> bool:
    for label in subdomains:
        stripped = re.sub(r"\d+$", "", label)
        if len(stripped) == 1 or stripped in SENDING_HOST_LABELS:
            return True
        if any(part in label for part in SENDING_HOST_SUBSTRINGS):
            return True
    return False


def _looks_generated(word: str) -> bool:
    """A machine's token mixes letters and digits; jane1980 changes over only once."""
    runs = len(_LETTERS_AND_DIGITS.findall(word))
    return word.isalnum() and ((len(word) >= 10 and runs >= 4) or (len(word) >= 12 and runs >= 3))


def address_shape(address: str) -> AddressShape:
    """Read what an address's local part and domain say, with no regard to the name."""
    local, domain = _split_address(address)
    signals: list[str] = []
    joined = re.sub(r"[^a-z0-9]", "", local)
    words = set(re.split(r"[^a-z0-9]+", local))
    provider = _is_personal_provider(domain)
    # "Mailer" is a surname too, so it takes more than a person's own mailbox to mean a bot.
    automated_word = words & AUTOMATED_LOCAL_WORDS and not provider and not _FIRST_DOT_LAST.fullmatch(local)
    if any(token in joined for token in AUTOMATED_LOCAL_SUBSTRINGS) or automated_word:
        signals.append(SIGNAL_AUTOMATED)

    if provider:
        return AddressShape(local, domain, tuple(signals), personal_provider=True)

    if _LONG_NUMBER.search(local) or any(_looks_generated(word) for word in words):
        signals.append(SIGNAL_GENERATED)

    subdomains, registered = _registered_labels(domain)
    registered_domain = domain.removeprefix(".".join(subdomains) + ".") if subdomains else domain
    if registered_domain in RESERVED_DOMAINS or domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        signals.append(SIGNAL_RESERVED)
    if registered_domain in LIST_HOST_DOMAINS:
        signals.append(SIGNAL_LIST_HOST)
    elif is_blocklisted_domain(f"x@{domain}"):
        signals.append(SIGNAL_INFRASTRUCTURE)
    if not _is_institutional(domain):
        if _is_sending_host(subdomains):
            signals.append(SIGNAL_SENDING_HOST)
        if re.search(r"(^|-)e?mail|e?mail($|-)", registered):
            signals.append(SIGNAL_MAIL_BRANDED)
    # A mailbox named after its own host belongs to the company, not to someone there.
    if _has_role_word(local) or local.split("+", 1)[0] in {*subdomains, registered}:
        signals.append(SIGNAL_ROLE)
    return AddressShape(local, domain, tuple(signals))


# ---------------------------------------------------------------------- name


def _name_words(name: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(name or "")]


def name_signals(name: str, domains: Iterable[str] = ()) -> list[str]:
    """What a sender's name says about whether a person holds it."""
    signals: list[str] = []
    stripped = (name or "").strip()
    words = _name_words(stripped)
    tokens = {token.casefold().strip(".,'\"()") for token in stripped.split()}
    # "Last, First" is an ordinary name; "First, Title & Title" presents an office.
    is_persona = bool(
        tokens & PERSONA_NAME_MARKERS or _PERSONA_PAREN.search(stripped) or ("," in stripped and "&" in stripped)
    )
    if not words or (_NOT_A_NAME_CHARACTERS.search(stripped) and not is_persona):
        signals.append(SIGNAL_NOT_A_NAME)
    if is_persona:
        signals.append(SIGNAL_PERSONA)
    if (
        set(words) & _ORGANIZATION_WORDS
        or (words[:1] == ["the"] and len(words) > 1)
        or _DOMAIN_LIKE_NAME.search(stripped)
    ):
        signals.append(SIGNAL_ORG_NAME)
    if len(words) == 1:
        signals.append(SIGNAL_ONE_WORD)
    compact = "".join(words)
    labels = {label for domain in domains for label in _host_labels(domain)}
    normalized = {label.replace("-", "") for label in labels}
    if set(words) & labels or any(
        len(label) >= 4 and compact.startswith(label) for label in normalized
    ):
        signals.append(SIGNAL_BRAND_NAME)
    return signals


def _reads_as_organization(names: list[str]) -> bool:
    return bool({SIGNAL_ORG_NAME, SIGNAL_BRAND_NAME, SIGNAL_NOT_A_NAME, SIGNAL_ONE_WORD, SIGNAL_PERSONA} & set(names))


def _carries_name(shape: AddressShape, name: str) -> bool:
    """Whether the local part spells a word of the name, or starts like one."""
    local = re.sub(r"[^a-z]", "", shape.local.split("+", 1)[0])
    return any(
        (len(word) >= 3 and word in local) or (len(word) >= 4 and local[:4] == word[:4])
        for word in _name_words(name)
    )


def _on_own_site(shape: AddressShape, name: str, names: list[str]) -> bool:
    """A person's full name, or their surname, is the domain: someone's own site."""
    words = _name_words(name)
    if len(words) < 2 or {SIGNAL_ORG_NAME, SIGNAL_NOT_A_NAME, SIGNAL_PERSONA} & set(names):
        return False
    label = _registered_labels(shape.domain)[1].replace("-", "")
    return label in ("".join(words), words[-1])


def _is_own_address(shape: AddressShape, evidence: SenderEvidence, names: list[str]) -> bool:
    """A mailbox provider, the sender's own site, or a local part carrying their name."""
    name = evidence.name
    if shape.automated or shape.generated:
        return False
    if shape.personal_provider:
        return True
    if _on_own_site(shape, name, names):
        # A small company's name reads the same, so its role mailbox takes real history.
        return not shape.role or bool(evidence.sent) or evidence.thread > 1
    if shape.role:
        return False
    if shape.sending and _reads_as_organization(names):
        return False
    # first.last reads as a person whatever the row happens to be called, unless
    # one half is the company's own name.
    if _FIRST_DOT_LAST.fullmatch(shape.local) and not set(shape.local.split(".")) & set(shape.domain.split(".")):
        return True
    words = _name_words(name)
    # A company's name first on its own domain, or one word on it, is a brand. Two
    # words where the second is the domain were caught above as someone's site.
    if SIGNAL_BRAND_NAME in names and (len(words) == 1 or words[0] in _host_labels(shape.domain)):
        return False
    if SIGNAL_ORG_NAME in names:
        # "Sarah Church" reads as an organization by one word. Her first name alone is
        # an address, a company's word in it isn't, and the rest takes real history.
        tokens = {token for token in re.split(r"[^a-z0-9]+", shape.local) if token}
        if tokens & _ORGANIZATION_WORDS:
            return False
        bare = re.sub(r"[^a-z]", "", shape.local)
        if bare in set(words) - _ORGANIZATION_WORDS:
            return True
        return evidence.has_history and _carries_name(shape, name)
    if SIGNAL_ONE_WORD in names and evidence.received >= ONE_WORD_OWN_ADDRESS_MAX_RECEIVED and not evidence.has_history:
        return False
    return _carries_name(shape, name)


# ------------------------------------------------------------------- verdict


def classify_sender(evidence: SenderEvidence) -> SenderVerdict:
    """
    Judge a sender from their addresses, name, and mail.

    Own addresses are judged by what came back: steady mail with no thread and no
    reply makes them a service, and so does a run of messages on a dedicated sending
    host. Anything else about them leaves a person. Every other address is judged by
    its shape, and a name or a reply doesn't change that.
    """
    shapes = {address: address_shape(address) for address in evidence.addresses}
    total = sum(evidence.addresses.values()) or 1
    names = name_signals(evidence.name, [shape.domain for shape in shapes.values()])
    fired = list(dict.fromkeys([s for shape in shapes.values() for s in shape.signals] + names))
    words = _name_words(evidence.name)

    own = tuple(a for a, s in shapes.items() if _is_own_address(s, evidence, names))
    wrote_to_own = any(
        (address == "" and own) or _is_own_address(address_shape(address), evidence, names)
        for address in evidence.sent_to
    )

    def verdict(is_service: bool, reason: str) -> SenderVerdict:
        return SenderVerdict(is_service, tuple(fired), reason, own, wrote_to_own)

    if not shapes:
        return verdict(False, "no_address")

    def share(predicate) -> float:
        return sum(evidence.addresses[a] for a, s in shapes.items() if predicate(s)) / total

    own_share = sum(evidence.addresses[a] for a in own) / total
    has_own = own_share >= OWN_ADDRESS_MIN_SHARE
    mostly_one_way = (
        evidence.thread <= evidence.received * ONE_WAY_MAX_THREAD_SHARE
        and evidence.sent <= evidence.received * ONE_WAY_MAX_SENT_SHARE
    )
    never_answered = not evidence.thread and not evidence.sent

    # A person turns up in threads, or writes from their own mailbox as well as the
    # address their newsletter goes out on. Neither is bulk however that mail reads.
    has_own_mailbox = len(shapes) > 1 and share(lambda s: s.personal_provider) >= OWN_ADDRESS_MIN_SHARE
    enough_checked = evidence.checked >= min(evidence.received, BULK_MIN_CHECKED) and evidence.checked
    if (
        enough_checked
        and evidence.bulk / evidence.checked >= BULK_MIN_SHARE
        and evidence.thread <= evidence.received * ONE_WAY_MAX_THREAD_SHARE
        and not has_own_mailbox
    ):
        fired.append(SIGNAL_BULK_MAIL)
        return verdict(True, "bulk_mail")

    if has_own:
        if never_answered and evidence.received >= ONE_WAY_MIN_RECEIVED:
            fired.append(SIGNAL_ONE_WAY)
            return verdict(True, "one_way_volume")
        if (
            share(lambda s: s.sending) >= SERVICE_ADDRESS_MIN_SHARE
            and evidence.received >= THIN_SIGNAL_MIN_RECEIVED
            and mostly_one_way
        ):
            return verdict(True, "sending_host")
        return verdict(False, "own_address")

    for predicate, reason in (
        (lambda s: s.automated, "automated_address"),
        (lambda s: s.generated, "generated_address"),
        (lambda s: s.infrastructure, "mail_infrastructure"),
        (lambda s: s.sending, "sending_host"),
        (lambda s: s.role, "role_address"),
    ):
        if share(predicate) >= SERVICE_ADDRESS_MIN_SHARE:
            return verdict(True, reason)
    if share(lambda s: s.automated or s.generated or s.infrastructure or s.sending or s.role) >= SERVICE_ADDRESS_MIN_SHARE:
        return verdict(True, "mixed_service_addresses")

    if " via " in f" {evidence.name.casefold()} ":
        return verdict(True, "relay_address")

    if evidence.received >= ONE_WAY_MIN_RECEIVED and mostly_one_way:
        fired.append(SIGNAL_ONE_WAY)
        return verdict(True, "one_way_volume")

    if SIGNAL_BRAND_NAME in names and evidence.received and (
        {SIGNAL_ONE_WORD, SIGNAL_ORG_NAME, SIGNAL_PERSONA} & set(names)
        or any(words[0] in _host_labels(s.domain) for s in shapes.values())
        or evidence.received >= THIN_SIGNAL_MIN_RECEIVED
    ):
        return verdict(True, "named_after_domain")
    if evidence.received >= THIN_SIGNAL_MIN_RECEIVED:
        if SIGNAL_ORG_NAME in names:
            return verdict(True, "organization_name")
        if words and mostly_one_way and {SIGNAL_NOT_A_NAME, SIGNAL_ONE_WORD} & set(names):
            return verdict(True, "not_a_person_name")
    return verdict(False, "no_service_evidence")


def _known_holder(store: PersonEntityStore, email: str, name: Optional[str]) -> Optional[PersonEntity]:
    """A row holding this address, or a visible person of this full name reachable by phone."""
    holder = store.get_by_email(email)
    if holder:
        return holder
    if name and parse_name(name).last:
        namesake = store.get_by_name(name)
        if namesake and not namesake.hidden and namesake.phone_numbers:
            return namesake
    return None


def is_known_sender(store: PersonEntityStore, email: str, name: Optional[str] = None) -> bool:
    """Whether this mail has a person or a hidden service to go to, whatever its address looks like."""
    holder = _known_holder(store, email, name)
    return bool(holder) and (not holder.hidden or is_hidden_service(holder))


def is_unknown_service_sender(
    store: PersonEntityStore, email: str, name: Optional[str] = None, bulk: bool = False
) -> bool:
    """
    Whether mail from this address should create no person and count for no one.

    An address someone holds is never in question here, nor is someone with a phone
    number writing under their full name. Otherwise a blocked address or one message's
    service evidence is enough. Name matching doesn't run first, so a service's mail
    can't land on someone who happens to share its display name.
    """
    if not email or _known_holder(store, email, name):
        return False
    if store.is_blocked(email):
        return True
    return classify_sender(SenderEvidence(
        name or "", {email: 1}, received=1, bulk=int(bulk), checked=1,
    )).is_service


# ---------------------------------------------------------------- protection

# One-to-one channels, read from interactions so groups and bots don't count.
CHANNEL_SOURCES = frozenset({"imessage", "phone", "photos", "slack", "whatsapp"})
# Source entities a person's own identity produces, with no invitation or import to explain them.
IDENTITY_ENTITY_SOURCES = frozenset({"imessage", "linkedin", "phone", "photos"})
AUTOMATIC_PERSON_SOURCES = (
    CHANNEL_SOURCES | IDENTITY_ENTITY_SOURCES
    | {"calendar", "contacts", "gmail", "granola", "meeting", "phone_contacts", "vault"}
)


def protection_reasons(person: PersonEntity) -> list[str]:
    """What on the row itself means a human or a one-to-one channel vouches for it."""
    reasons: list[str] = []
    if person.id == settings.my_person_id:
        reasons.append("self")
    if person.category in ("family", "self") or person.id in FAMILY_PERSON_IDS or _is_family_member(person.canonical_name):
        reasons.append("family")
    if person.id in STRENGTH_OVERRIDES_BY_ID:
        reasons.append("strength_override")
    if person.id in CIRCLE_OVERRIDES_BY_ID:
        reasons.append("circle_override")
    if person.id in TAG_OVERRIDES_BY_ID:
        reasons.append("tag_override")
    if person.phone_numbers:
        reasons.append("phone_number")
    if person.photo_count:
        reasons.append("photos")
    for label, value in (
        ("tags", person.tags), ("notes", person.notes), ("birthday", person.birthday),
        ("linkedin_url", person.linkedin_url), ("company", person.company), ("position", person.position),
    ):
        if value:
            reasons.append(label)
    if set(person.sources) - AUTOMATIC_PERSON_SOURCES:
        reasons.append("manual_source")
    return reasons


@dataclass
class SenderDecision:
    """One row, its evidence, and the outcome."""

    person: PersonEntity
    addresses: dict[str, int]
    received: int
    sent: int
    thread: int
    verdict: SenderVerdict
    protected_by: list[str]

    @property
    def will_hide(self) -> bool:
        return self.verdict.is_service and not self.protected_by


@dataclass
class _Mail:
    addresses: Counter = field(default_factory=Counter)
    sent_to: Counter = field(default_factory=Counter)
    received: int = 0
    thread: int = 0
    bulk: int = 0
    checked: int = 0
    real_meeting: bool = False
    human_links: set = field(default_factory=set)


def _connect_read_only(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=60.0)


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """
    A query's rows, or none on an install that hasn't created the table.

    Any other failure raises. These queries find what protects a row, so reading a
    locked database as "nothing protects anyone" would hide real people.
    """
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return []


def _load_people(crm_db: Path, merged_ids: Mapping[str, str], where: str = "hidden = 0") -> list[PersonEntity]:
    conn = _connect_read_only(crm_db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            PersonEntityStore._row_to_entity(None, row)
            for row in conn.execute(f"SELECT * FROM person_entities WHERE {where}")
            if row["id"] not in merged_ids
        ]
    finally:
        conn.close()


def _recipient_of(source_id: Optional[str]) -> str:
    """The address in a sent message's ``<message>:<recipient>`` source id, if it has one."""
    _, _, recipient = (source_id or "").partition(":")
    return recipient


def _load_interaction_evidence(interactions_db: Path, mail: dict, reasons: dict) -> None:
    conn = _connect_read_only(interactions_db)
    try:
        for person_id, source, arrow, count, one_to_one, small_meeting in conn.execute(
            "SELECT person_id, source_type, substr(title, 1, 1), COUNT(*), "
            "  source_type NOT IN ('whatsapp', 'slack') OR (source_type = 'whatsapp' AND attendee_count IS NULL) "
            "    OR (source_type = 'slack' AND title LIKE 'Slack DM%'), "
            f"  source_type = 'calendar' AND attendee_count BETWEEN 1 AND {REAL_MEETING_MAX_ATTENDEES} "
            "FROM interactions "
            "WHERE NOT (source_type = 'gmail' AND source_id LIKE '%:%' AND source_id NOT LIKE '%:cc:%') "
            "  AND NOT (source_type = 'gmail' AND substr(title, 1, 1) = '→') "
            "GROUP BY 1, 2, 3, 5, 6"
        ):
            if source == "gmail":
                if arrow == "↔":
                    mail[person_id].thread += count
                else:
                    mail[person_id].received += count
            elif source in CHANNEL_SOURCES:
                if one_to_one:
                    reasons[person_id].add(f"interaction:{source}")
            elif source == "meeting" or small_meeting:
                mail[person_id].real_meeting = True
        sent: dict = defaultdict(Counter)
        for person_id, arrow, source_id in conn.execute(
            "SELECT person_id, substr(title, 1, 1), source_id FROM interactions WHERE source_type = 'gmail' "
            "AND (substr(title, 1, 1) = '→' OR (source_id LIKE '%:%' AND source_id NOT LIKE '%:cc:%'))"
        ):
            if arrow not in ("←", "↔"):
                sent[person_id][_recipient_of(source_id)] += 1
            elif arrow == "↔":
                mail[person_id].thread += 1
            else:
                mail[person_id].received += 1
        for person_id, recipients in sent.items():
            mail[person_id].sent_to.update(recipients)
    finally:
        conn.close()


def _load_crm_evidence(crm_db: Path, merged_ids: Mapping[str, str], mail: dict, reasons: dict) -> None:
    conn = _connect_read_only(crm_db)
    try:
        for person_id, source in _rows(
            conn, "SELECT DISTINCT canonical_person_id, source_type FROM source_entities "
            "WHERE canonical_person_id IS NOT NULL",
        ):
            if source in IDENTITY_ENTITY_SOURCES:
                reasons[person_id].add(f"source_entity:{source}")
        for person_id, address in _rows(
            conn, "SELECT DISTINCT canonical_person_id, lower(observed_email) FROM source_entities "
            "WHERE canonical_person_id IS NOT NULL AND link_status NOT IN ('auto', 'rejected')",
        ):
            mail[person_id].human_links.add(address)
        for person_id, address, count, bulk, checked in _rows(
            conn,
            "SELECT canonical_person_id, lower(observed_email), COUNT(*), "
            "SUM(json_extract(metadata, '$.bulk') = 1), "
            "SUM(json_extract(metadata, '$.bulk') IS NOT NULL) "
            "FROM source_entities WHERE source_type = 'gmail' AND canonical_person_id IS NOT NULL "
            "AND observed_email IS NOT NULL AND (metadata IS NULL OR json_valid(metadata)) "
            "GROUP BY 1, 2",
        ):
            record = mail[person_id]
            record.addresses[address] += count
            record.bulk += bulk or 0
            record.checked += checked or 0
        # A sent message's source entity holds its recipient even with no interaction.
        for person_id, source_id in _rows(
            conn,
            "SELECT canonical_person_id, source_id FROM source_entities "
            "WHERE source_type = 'gmail' AND canonical_person_id IS NOT NULL "
            "AND source_id LIKE '%:%' AND source_id NOT LIKE '%:cc:%'",
        ):
            recipient = _recipient_of(source_id)
            if not mail[person_id].sent_to[recipient]:
                mail[person_id].sent_to[recipient] = 1
        for (person_id,) in _rows(conn, "SELECT person_id FROM person_facts WHERE confirmed_by_user = 1"):
            reasons[person_id].add("confirmed_fact")
        for preferred, rejected in _rows(
            conn, "SELECT preferred_person_id, rejected_person_id FROM link_overrides"
        ):
            reasons[preferred].add("link_override")
            reasons[rejected].add("link_override")
        for person_a, person_b, kind in _rows(
            conn, "SELECT person_a_id, person_b_id, relationship_type FROM relationships"
        ):
            if kind not in (TYPE_INFERRED, TYPE_COWORKER):
                reasons[person_a].add(f"relationship:{kind}")
                reasons[person_b].add(f"relationship:{kind}")
        for person_a, person_b in _rows(
            conn, "SELECT person_a_id, person_b_id FROM entity_review_queue WHERE status = 'kept'"
        ):
            reasons[person_a].add("review_kept")
            reasons[person_b].add("review_kept")
    finally:
        conn.close()
    for primary in set(merged_ids.values()):
        reasons[primary].add("merge_target")


def load_merged_ids(path: Path) -> dict[str, str]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _addresses_of(person: PersonEntity, record: _Mail) -> dict[str, int]:
    addresses = dict(record.addresses)
    for email in person.emails:
        addresses.setdefault(email.lower(), 1)
    return addresses


def _evidence_of(
    person: PersonEntity, record: _Mail, addresses: Mapping[str, int], sample: Mapping[str, int]
) -> SenderEvidence:
    return SenderEvidence(
        person.canonical_name or "",
        addresses,
        received=record.received,
        thread=record.thread,
        sent_to=dict(record.sent_to),
        bulk=record.bulk + sample.get("bulk", 0),
        checked=record.checked + sample.get("checked", 0),
    )


def _load_evidence(crm_db: Path, interactions_db: Path, merged_ids: Mapping[str, str]) -> tuple[dict, dict]:
    mail: dict[str, _Mail] = defaultdict(_Mail)
    reasons: dict[Optional[str], set] = defaultdict(set)
    _load_interaction_evidence(interactions_db, mail, reasons)
    _load_crm_evidence(crm_db, merged_ids, mail, reasons)
    return mail, reasons


def decide_senders(
    crm_db: Path,
    interactions_db: Path,
    merged_ids_path: Path,
    bulk_samples: Optional[Mapping[str, Mapping[str, int]]] = None,
) -> list[SenderDecision]:
    """
    Judge every visible person who has email, from read-only connections.

    bulk_samples gives a row's message headers checked outside what is stored, as
    {person_id: {"checked": n, "bulk": m}}.
    """
    merged_ids = load_merged_ids(merged_ids_path)
    mail, reasons = _load_evidence(crm_db, interactions_db, merged_ids)

    decisions = []
    for person in _load_people(crm_db, merged_ids):
        record = mail.get(person.id) or _Mail()
        addresses = _addresses_of(person, record)
        if not addresses or not (record.received or record.thread or record.sent_to or record.addresses):
            continue
        evidence = _evidence_of(person, record, addresses, (bulk_samples or {}).get(person.id, {}))
        verdict = classify_sender(evidence)
        protected_by = sorted(reasons.get(person.id, set())) + protection_reasons(person)
        if verdict.wrote_to_own_address:
            protected_by.append("wrote_to")
        if verdict.own_addresses and record.real_meeting:
            protected_by.append("real_meeting")
        # A confirmed link says this message belongs to this row. That vouches for a
        # person on their own address, and only for the row's name on a service's.
        names = name_signals(evidence.name, [_split_address(a)[1] for a in addresses])
        if any(
            address is None or _is_own_address(address_shape(address), evidence, names)
            for address in record.human_links
        ):
            protected_by.append("human_link")
        decisions.append(SenderDecision(
            person, addresses, record.received, evidence.sent, record.thread, verdict, protected_by,
        ))
    return decisions


# ---------------------------------------------------------------------- hide


def hide_service_person(store: PersonEntityStore, decision: SenderDecision) -> None:
    """
    Hide a service and clear its relationship value.

    The row takes on the service-shaped addresses it wrote from, unless someone else
    holds one, so their mail keeps finding it. Those are blocked. An own address that
    a newsletter merged into the row is never blocked.
    """
    own = set(decision.verdict.own_addresses)
    row = store.get_by_id(decision.person.id)
    for address in decision.addresses:
        if address not in own and store.get_by_email(address) is None:
            row.add_email(address)
    store.update(row)
    store.hide_person(row.id, HIDDEN_SERVICE_REASON_PREFIX + decision.verdict.reason, keep_unblocked=own)
    hidden = store.get_by_id(row.id)
    hidden.relationship_strength = 0.0
    hidden.is_peripheral_contact = True
    hidden.dunbar_circle = None
    store.update(hidden)


def restore_person(store: PersonEntityStore, person_id: str) -> Optional[PersonEntity]:
    """
    Undo a hide, unblock the row's addresses, and tag it so it stays visible.

    The next strength run gives it back a score and a circle, and the next relationship
    discovery rebuilds its inferred relationships from the interactions it kept.
    """
    restored = store.unhide_person(person_id)
    if restored is None:
        return None
    restored.add_tag(CONFIRMED_PERSON_TAG)
    store.update(restored)
    return restored


def restore_people_who_wrote_back(store: PersonEntityStore, interactions_db: Path) -> int:
    """Show any hidden service again once the owner and they have mail on an own address."""
    merged_ids = load_merged_ids(PersonEntityStore.MERGED_IDS_PATH)
    crm_db = Path(store.db_path)
    mail, _ = _load_evidence(crm_db, interactions_db, merged_ids)
    restored = 0
    hidden_services = f"hidden_reason LIKE '{HIDDEN_SERVICE_REASON_PREFIX}%'"
    for person in _load_people(crm_db, merged_ids, where=hidden_services):
        record = mail.get(person.id) or _Mail()
        evidence = _evidence_of(person, record, _addresses_of(person, record), {})
        if classify_sender(evidence).wrote_to_own_address and (record.received or record.thread):
            restore_person(store, person.id)
            restored += 1
    return restored


def hide_service_senders(store: Optional[PersonEntityStore] = None) -> dict:
    """
    Keep the people list current: restore anyone who wrote back, then hide new services.

    Runs before scoring, so a new service takes no score or circle slot. Holds back and
    warns when there are too many at once for a nightly pass to decide unreviewed.
    scripts/repair_service_senders.py is the reviewed path.
    """
    from api.services.interaction_store import get_interaction_db_path
    from api.services.person_entity import get_person_entity_store

    store = store or get_person_entity_store()
    interactions_db = Path(get_interaction_db_path())
    restored = restore_people_who_wrote_back(store, interactions_db)
    decisions = [d for d in decide_senders(
        Path(store.db_path), interactions_db, PersonEntityStore.MERGED_IDS_PATH,
    ) if d.will_hide]
    if len(decisions) > NIGHTLY_MAX_HIDES:
        logger.warning(
            "Service-sender pass held back: %d rows exceed the nightly limit of %d. "
            "Review and run scripts/repair_service_senders.py.",
            len(decisions), NIGHTLY_MAX_HIDES,
        )
        return {"hidden": 0, "held_back": len(decisions), "restored": restored}
    for decision in decisions:
        hide_service_person(store, decision)
    return {"hidden": len(decisions), "held_back": 0, "restored": restored}
