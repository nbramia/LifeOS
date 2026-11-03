"""
Decide whether an email sender is a service or newsletter rather than a person.

classify_sender weighs a sender's address shape, name shape, and mail. It sees one
message for a sender no one has met, or a whole history for a person in the CRM.

protection_reasons lists everything that puts a row out of scope however it looks:
anything two-way or from a human. A protected row is never hidden.

A hidden service keeps its interactions, its address stays blocklisted so no later
sync brings it back as a new person, and restore_person undoes all of it. Only the
person row and its relationship value change; mail stays searchable in Gmail.
"""
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from api.services.entity_resolver import NON_NAME_WORDS
from api.services.person_entity import (
    FAMILY_PERSON_IDS,
    PersonEntity,
    PersonEntityStore,
    _is_family_member,
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

# Every row hidden here has a hidden_reason starting with this. Consistency
# verification keeps those rows' interactions.
HIDDEN_REASON_PREFIX = "service sender: "

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

# Messages a shared mailbox has to send, with no reply ever, before a person's name
# on it stops counting. A one-off from a named person at support@ can be personal.
ROLE_ADDRESS_MIN_RECEIVED = 5
# The same for a person's own-looking address on a dedicated sending host.
SENDING_HOST_MIN_RECEIVED = 3
# An organization's name on an ordinary address.
ORGANIZATION_MIN_RECEIVED = 3
# So many one-way messages from an address with no sign of a person behind it, and
# the share of shared threads it may have. A colleague turns up in threads; a mailer doesn't.
ONE_WAY_MIN_RECEIVED = 40
ONE_WAY_MAX_THREAD_SHARE = 0.1
# Share of the mail checked that has to carry bulk headers.
BULK_MIN_SHARE = 0.6
# Share of mail from a personal-looking address that makes the sender a person.
PERSONAL_ADDRESS_MIN_SHARE = 0.1
# Share of mail the service-shaped addresses have to carry.
SERVICE_ADDRESS_MIN_SHARE = 0.5

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
_DOMAIN_LIKE_NAME = re.compile(r"\b[^\W\d_]{2,}\.(com|org|net|io|co|ai)\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\d{8,}")
_LETTERS_AND_DIGITS = re.compile(r"[a-z]+|\d+")


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
    """A sender's name, addresses with message counts, and how their mail arrived."""

    name: str
    addresses: Mapping[str, int]
    received: int = 1
    thread: int = 0
    bulk: int = 0
    checked: int = 0


@dataclass(frozen=True)
class SenderVerdict:
    """A classification, the signals that fired, and the rule that decided it."""

    is_service: bool
    signals: tuple[str, ...] = ()
    reason: str = ""


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
    if any(token in joined for token in AUTOMATED_LOCAL_SUBSTRINGS) or words & AUTOMATED_LOCAL_WORDS:
        signals.append(SIGNAL_AUTOMATED)

    if _is_personal_provider(domain):
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
        set(words) & (NON_NAME_WORDS | ORGANIZATION_NAME_WORDS)
        or (words[:1] == ["the"] and len(words) > 1)
        or _DOMAIN_LIKE_NAME.search(stripped)
    ):
        signals.append(SIGNAL_ORG_NAME)
    if len(words) == 1:
        signals.append(SIGNAL_ONE_WORD)
    compact = "".join(words)
    labels = {label for domain in domains for label in _host_labels(domain)}
    normalized = {label.replace("-", "") for label in labels}
    if compact and (compact in normalized or set(words) & labels or any(
        len(label) >= 4 and compact.startswith(label) for label in normalized
    )):
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


def _is_personal_address(shape: AddressShape, name: str, names: list[str], received: int = 0) -> bool:
    """A mailbox provider, or an ordinary address carrying someone's own name."""
    if shape.automated or shape.generated:
        return False
    if shape.personal_provider:
        return True
    if shape.sending or shape.role:
        return False
    # first.last reads as a person whatever the row happens to be called, unless
    # one half is the company's own name.
    if _FIRST_DOT_LAST.fullmatch(shape.local) and not set(shape.local.split(".")) & set(shape.domain.split(".")):
        return True
    if SIGNAL_ORG_NAME in names:
        return False
    # A one-word name on its own domain is a brand; two words on one is someone's site.
    if {SIGNAL_BRAND_NAME, SIGNAL_ONE_WORD} <= set(names):
        return False
    # So is one word on steady one-way mail. A placeholder name copied from the
    # address reads the same as a first name, and people don't write that way unanswered.
    if SIGNAL_ONE_WORD in names and received >= ROLE_ADDRESS_MIN_RECEIVED:
        return False
    return _carries_name(shape, name)


# ------------------------------------------------------------------- verdict


def classify_sender(evidence: SenderEvidence) -> SenderVerdict:
    """
    Judge a sender from their addresses, name, and mail.

    An automated address decides alone, and so does mail carrying bulk headers.
    After those a personal-looking address makes the sender a person. Below that the
    address has to be service-shaped, and the weaker its evidence, the more the name
    and the history have to agree: a person's name on a role address takes repeated
    one-way mail.
    """
    shapes = {address: address_shape(address) for address in evidence.addresses}
    total = sum(evidence.addresses.values()) or 1
    names = name_signals(evidence.name, [shape.domain for shape in shapes.values()])
    fired = list(dict.fromkeys([s for shape in shapes.values() for s in shape.signals] + names))

    if not shapes:
        return SenderVerdict(False, tuple(fired), "no_address")

    def share(predicate) -> float:
        return sum(evidence.addresses[a] for a, s in shapes.items() if predicate(s)) / total

    def service(reason: str) -> SenderVerdict:
        return SenderVerdict(True, tuple(fired), reason)

    def person(reason: str) -> SenderVerdict:
        return SenderVerdict(False, tuple(fired), reason)

    org_like = _reads_as_organization(names)
    one_way = evidence.thread <= evidence.received * ONE_WAY_MAX_THREAD_SHARE

    if share(lambda s: s.automated) >= SERVICE_ADDRESS_MIN_SHARE:
        return service("automated_address")

    # A person turns up in threads, or writes from their own mailbox as well as the
    # address their newsletter goes out on. Neither is bulk however that mail reads.
    has_own_mailbox = len(shapes) > 1 and share(lambda s: s.personal_provider) >= PERSONAL_ADDRESS_MIN_SHARE
    if (
        evidence.checked
        and evidence.bulk / evidence.checked >= BULK_MIN_SHARE
        and one_way
        and not has_own_mailbox
    ):
        fired.append(SIGNAL_BULK_MAIL)
        return service("bulk_mail")

    if share(lambda s: _is_personal_address(s, evidence.name, names, evidence.received)) >= PERSONAL_ADDRESS_MIN_SHARE:
        return person("personal_address")

    # "Name via Platform": someone writing through a relay, whose address is shared.
    if " via " in f" {evidence.name.casefold()} " and share(lambda s: s.sending) < SERVICE_ADDRESS_MIN_SHARE:
        return person("person_via_relay")

    if share(lambda s: s.generated) >= SERVICE_ADDRESS_MIN_SHARE:
        return service("generated_address")

    if share(lambda s: s.infrastructure) >= SERVICE_ADDRESS_MIN_SHARE:
        return service("mail_infrastructure")

    if evidence.received >= ONE_WAY_MIN_RECEIVED and one_way:
        fired.append(SIGNAL_ONE_WAY)
        return service("one_way_volume")

    if share(lambda s: s.sending) >= SERVICE_ADDRESS_MIN_SHARE:
        carries_name = any(_carries_name(s, evidence.name) for s in shapes.values())
        if org_like or not carries_name or evidence.received >= SENDING_HOST_MIN_RECEIVED:
            return service("sending_host")
        return person("person_name_on_sending_host")

    if share(lambda s: s.role) >= SERVICE_ADDRESS_MIN_SHARE:
        if org_like or evidence.received >= ROLE_ADDRESS_MIN_RECEIVED:
            return service("role_address")
        return person("person_name_on_role_address")

    if SIGNAL_BRAND_NAME in names and evidence.received:
        return service("named_after_domain")
    if SIGNAL_ORG_NAME in names and evidence.received >= ORGANIZATION_MIN_RECEIVED:
        return service("organization_name")
    return person("no_service_evidence")


def is_unknown_service_sender(
    store: PersonEntityStore, email: str, name: Optional[str] = None, bulk: bool = False
) -> bool:
    """
    Whether mail from this address should create no person and count for no one.

    An address someone already holds is never in question here. Its mail is theirs,
    and the nightly pass judges the whole row. Otherwise a blocked address or one
    message's service evidence is enough. Name matching doesn't run first, so a
    service's mail can't land on someone who happens to share its display name.
    """
    if not email or store.get_by_email(email):
        return False
    if store.is_blocked(email):
        return True
    return classify_sender(SenderEvidence(
        name or "", {email.lower(): 1}, received=1, bulk=int(bulk), checked=1,
    )).is_service


# ---------------------------------------------------------------- protection

# Channels someone reaches a real person on, or a human maintains.
CONTACT_INTERACTION_SOURCES = frozenset({"calendar", "imessage", "phone", "photos", "slack", "whatsapp"})
CONTACT_ENTITY_SOURCES = CONTACT_INTERACTION_SOURCES | {"contacts", "linkedin", "phone_contacts"}
# Note mentions count for a person-shaped name only. Notes mention companies too.
MENTION_SOURCES = frozenset({"meeting", "vault"})
AUTOMATIC_PERSON_SOURCES = CONTACT_ENTITY_SOURCES | MENTION_SOURCES | {"gmail", "granola"}


def protection_reasons(person: PersonEntity) -> list[str]:
    """What on the row itself means a human or a two-way channel vouches for it."""
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
    received: int = 0
    sent: int = 0
    thread: int = 0
    bulk: int = 0
    checked: int = 0
    mentions: set = field(default_factory=set)


def _connect_read_only(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=60.0)


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """A query's rows, or none on an install that hasn't created the table."""
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def _load_people(crm_db: Path, merged_ids: Mapping[str, str]) -> list[PersonEntity]:
    conn = _connect_read_only(crm_db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            PersonEntityStore._row_to_entity(None, row)
            for row in conn.execute("SELECT * FROM person_entities WHERE hidden = 0")
            if row["id"] not in merged_ids
        ]
    finally:
        conn.close()


def _load_interaction_evidence(interactions_db: Path, mail: dict, reasons: dict) -> None:
    conn = _connect_read_only(interactions_db)
    try:
        for person_id, source, arrow, recipient_shaped, count in conn.execute(
            "SELECT person_id, source_type, substr(title, 1, 1), "
            "source_id LIKE '%:%' AND source_id NOT LIKE '%:cc:%', COUNT(*) "
            "FROM interactions GROUP BY 1, 2, 3, 4"
        ):
            if source == "gmail":
                if arrow == "→" or (arrow not in ("←", "↔") and recipient_shaped):
                    mail[person_id].sent += count
                elif arrow == "↔":
                    mail[person_id].thread += count
                else:
                    mail[person_id].received += count
            elif source in CONTACT_INTERACTION_SOURCES:
                reasons[person_id].add(f"interaction:{source}")
            elif source in MENTION_SOURCES:
                mail[person_id].mentions.add(source)
    finally:
        conn.close()


def _load_crm_evidence(crm_db: Path, merged_ids: Mapping[str, str], mail: dict, reasons: dict) -> None:
    conn = _connect_read_only(crm_db)
    try:
        for person_id, source, status in _rows(
            conn,
            "SELECT DISTINCT canonical_person_id, source_type, link_status FROM source_entities "
            "WHERE canonical_person_id IS NOT NULL",
        ):
            if source in CONTACT_ENTITY_SOURCES:
                reasons[person_id].add(f"source_entity:{source}")
            elif source in MENTION_SOURCES:
                mail[person_id].mentions.add(source)
            if status != "auto":
                reasons[person_id].add(f"link_status:{status}")
        for person_id, address, count, recipient, bulk, checked in _rows(
            conn,
            "SELECT canonical_person_id, lower(observed_email), COUNT(*), "
            "SUM(source_id LIKE '%:%' AND source_id NOT LIKE '%:cc:%'), "
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
            if recipient:
                reasons[person_id].add("wrote_to")
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
    mail: dict[str, _Mail] = defaultdict(_Mail)
    reasons: dict[Optional[str], set] = defaultdict(set)
    _load_interaction_evidence(interactions_db, mail, reasons)
    _load_crm_evidence(crm_db, merged_ids, mail, reasons)

    decisions = []
    for person in _load_people(crm_db, merged_ids):
        record = mail.get(person.id) or _Mail()
        addresses = dict(record.addresses)
        for email in person.emails:
            addresses.setdefault(email.lower(), 1)
        if not addresses or not (record.received or record.thread or record.addresses):
            continue
        sample = (bulk_samples or {}).get(person.id, {})
        verdict = classify_sender(SenderEvidence(
            person.canonical_name or "",
            addresses,
            received=record.received,
            thread=record.thread,
            bulk=record.bulk + sample.get("bulk", 0),
            checked=record.checked + sample.get("checked", 0),
        ))
        protected_by = sorted(reasons.get(person.id, set())) + protection_reasons(person)
        if record.sent and "wrote_to" not in protected_by:
            protected_by.append("wrote_to")
        if record.mentions and not _reads_as_organization(name_signals(person.canonical_name, _domains(addresses))):
            protected_by.append("mentioned_in_notes")
        decisions.append(SenderDecision(
            person, addresses, record.received, record.sent, record.thread, verdict, protected_by,
        ))
    return decisions


def _domains(addresses: Iterable[str]) -> list[str]:
    return [_split_address(address)[1] for address in addresses]


# ---------------------------------------------------------------------- hide


def hide_service_person(store: PersonEntityStore, decision: SenderDecision) -> None:
    """Hide a service, block every address it wrote from, and clear its relationship value."""
    reason = HIDDEN_REASON_PREFIX + decision.verdict.reason
    held_elsewhere = {
        address for address in decision.addresses
        if (holder := store.get_by_email(address)) is not None and holder.id != decision.person.id
    }
    store.hide_person(
        decision.person.id,
        reason,
        extra_emails=[address for address in decision.addresses if address not in held_elsewhere],
    )
    hidden = store.get_by_id(decision.person.id)
    hidden.relationship_strength = 0.0
    hidden.is_peripheral_contact = True
    hidden.dunbar_circle = None
    store.update(hidden)


def restore_person(store: PersonEntityStore, person_id: str, crm_db: Path) -> Optional[PersonEntity]:
    """
    Undo a hide, unblock the row's addresses, and tag it so it stays visible.

    The next strength run gives it back a score and a circle.
    """
    conn = _connect_read_only(crm_db)
    try:
        observed = [row[0] for row in conn.execute(
            "SELECT DISTINCT lower(observed_email) FROM source_entities "
            "WHERE canonical_person_id = ? AND observed_email IS NOT NULL",
            (person_id,),
        )]
    finally:
        conn.close()
    restored = store.unhide_person(person_id, extra_identifiers=observed)
    if restored is None:
        return None
    restored.add_tag(CONFIRMED_PERSON_TAG)
    store.update(restored)
    return restored


def hide_service_senders(store: Optional[PersonEntityStore] = None) -> dict:
    """
    Hide services that reached the CRM since the last run, so they earn no score or circle.

    Holds back and warns when there are too many at once for a nightly pass to
    decide unreviewed. scripts/repair_service_senders.py is the reviewed path.
    """
    from api.services.interaction_store import get_interaction_db_path
    from api.services.person_entity import get_person_entity_store

    store = store or get_person_entity_store()
    decisions = [d for d in decide_senders(
        Path(store.db_path), Path(get_interaction_db_path()), PersonEntityStore.MERGED_IDS_PATH,
    ) if d.will_hide]
    if len(decisions) > NIGHTLY_MAX_HIDES:
        logger.warning(
            "Service-sender pass held back: %d rows exceed the nightly limit of %d. "
            "Review and run scripts/repair_service_senders.py.",
            len(decisions), NIGHTLY_MAX_HIDES,
        )
        return {"hidden": 0, "held_back": len(decisions)}
    for decision in decisions:
        hide_service_person(store, decision)
    return {"hidden": len(decisions), "held_back": 0}
