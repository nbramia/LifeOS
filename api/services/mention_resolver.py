"""
Context-aware resolution of first-name mentions in vault notes.

A bare "Robin" in a note could be any Robin. This scores the real people the
mention could mean using signals that can be checked in existing data: the
note's personal or work context, the employer current on its date, real
interaction near that date, who else the note names, and where the note lives.
A mention links only on a clear winner (MENTION_MIN_SCORE, MENTION_MIN_LEAD in
config/relationship_weights.py). A missed link costs little and a wrong one
costs a lot, so anything less resolves to no one.

Every decision is deterministic and carries its score breakdown. No LLM calls.
Scoring never picks a person known only by one word, nor a row that doesn't read
as a person, nor the owner, whose name in their own notes is not a mention of
someone else. A link override is a human decision and can name anyone visible.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from api.services.entity_resolver import is_bare_person, is_creatable_person_name, parse_name
from api.services.link_override import LinkOverride, most_specific_override
from api.services.people import get_person_category
from api.services.person_entity import PersonEntity
from api.services.sqlite_connect import connect_closing
from api.services.vault_listing import vault_relative_path
from config.employment_history import Employer, EmploymentHistory, get_employment_history
from config.nickname_lookup import get_name_variants
from config.relationship_weights import (
    MENTION_COMENTION_CAP,
    MENTION_COMENTION_STRONG_PAIR,
    MENTION_EMPLOYER_HISTORY_MIN_INTERACTIONS,
    MENTION_FOLDER_MIN_LINKS,
    MENTION_INTERACTION_TIERS,
    MENTION_MEETING_DAY_WINDOW,
    MENTION_MEETING_MAX_ATTENDEES,
    MENTION_MEETING_TITLE_OVERLAP,
    MENTION_MIN_LEAD,
    MENTION_MIN_SCORE,
    MENTION_PENALTY_DOMAIN_MISMATCH,
    MENTION_PENALTY_NICKNAME,
    MENTION_PENALTY_OTHER_EMPLOYER,
    MENTION_WEIGHT_ALIAS,
    MENTION_WEIGHT_COMENTION_STRONG,
    MENTION_WEIGHT_COMENTION_WEAK,
    MENTION_WEIGHT_DICTIONARY_CATEGORY,
    MENTION_WEIGHT_DOMAIN_MATCH,
    MENTION_WEIGHT_EMPLOYER_COMPANY,
    MENTION_WEIGHT_EMPLOYER_EMAIL,
    MENTION_WEIGHT_EMPLOYER_HISTORY,
    MENTION_WEIGHT_FAMILY_IN_PERSONAL,
    MENTION_WEIGHT_FIRST_NAME,
    MENTION_WEIGHT_FOLDER_LINKS,
    MENTION_WEIGHT_FULL_NAME_IN_NOTE,
    MENTION_WEIGHT_INITIAL,
    MENTION_WEIGHT_MEETING_SAME_DAY,
    MENTION_WEIGHT_ONLY_CANDIDATE,
    MENTION_WEIGHT_PERSON_PATH,
)

logger = logging.getLogger(__name__)

MENTION_MATCH_TYPE = "mention_context"
OVERRIDE_MATCH_TYPE = "link_override"

# vault_tags.domain values that read as work or as personal. Others ("mixed",
# "tech_projects") say nothing either way.
WORK_TAG_DOMAINS = frozenset({"work"})
PERSONAL_TAG_DOMAINS = frozenset({"home_food", "relationships", "health", "growth", "money_admin"})

MENTION_SOURCE_TYPES = ("vault", "meeting", "granola")
# Sources showing direct personal contact.
_PERSONAL_CONTACT_SOURCES = frozenset({"imessage", "whatsapp", "signal", "phone_call", "phone"})
_WORK_ACCOUNT_SOURCES = ("gmail", "calendar", "slack")

_SQL_CHUNK = 400

REASON_LINKED = "strong_match"
REASON_OVERRIDE = "link_override"
REASON_OWNER = "owner_name"
REASON_NO_CANDIDATE = "no_candidate"
REASON_BELOW_MINIMUM = "below_minimum"
REASON_NARROW_LEAD = "narrow_lead"


def is_ambiguous_mention(name: str) -> bool:
    """A first name alone, or a name with only an initial on one side."""
    parsed = parse_name(name)
    if parsed.last is None:
        return True
    return len(parsed.last.rstrip(".")) == 1 or len(parsed.first.rstrip(".")) == 1


def note_domain(
    rel_path: str,
    tag_domain: Optional[str],
    employment: EmploymentHistory,
) -> Optional[str]:
    """
    Whether a note reads as "work" or "personal", or None when it can't be told.

    The note's folder decides, with an employer's archive folder counting as
    work. The tagged domain fills in for folders that say nothing, and a work
    tag on a note in a personal folder leaves the question open.
    """
    top = rel_path.replace("\\", "/").split("/", 1)[0].casefold()
    if top == "work" or employment.for_path(rel_path):
        return "work"
    if top == "personal":
        return None if tag_domain in WORK_TAG_DOMAINS else "personal"
    if tag_domain in WORK_TAG_DOMAINS:
        return "work"
    if tag_domain in PERSONAL_TAG_DOMAINS:
        return "personal"
    return None


@dataclass(frozen=True)
class NoteContext:
    """What a note offers for reading a mention in it."""

    rel_path: str
    note_date: Optional[date] = None
    domain: Optional[str] = None  # "work", "personal", or None
    employers: tuple[Employer, ...] = ()
    text: str = ""
    co_mentioned_ids: frozenset[str] = frozenset()
    is_meeting: bool = False
    title: str = ""


@dataclass
class ScoredCandidate:
    """One person a mention could mean, with the points behind their score."""

    entity: PersonEntity
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return sum(self.signals.values())


@dataclass
class MentionDecision:
    """The outcome for one mention. ``entity`` is None when it links to no one."""

    mention: str
    reason: str
    entity: Optional[PersonEntity] = None
    score: float = 0.0
    lead: Optional[float] = None
    candidates: list[ScoredCandidate] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return min(self.score / 100.0, 1.0) if self.entity else 0.0

    @property
    def match_type(self) -> Optional[str]:
        if self.reason == REASON_OVERRIDE:
            return OVERRIDE_MATCH_TYPE
        return MENTION_MATCH_TYPE if self.entity else None


def _reads_as_a_person(person: PersonEntity) -> bool:
    """Whether a row can be meant by a first name.

    Rows named after an email sender ("Example Billing", a one-word service
    name) carry identifiers but aren't anyone a note would call by first name.
    A one-word row with a phone number is a person's contact card.
    """
    if parse_name(person.canonical_name).last is None:
        return bool(person.phone_numbers)
    return is_creatable_person_name(person.canonical_name)


def _read_only(db_path: str | Path):
    """Context manager for a read-only connection that closes on the way out."""
    return connect_closing(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30.0)


def _has_whole_phrase(text: str, phrase: str) -> bool:
    """Whether a casefolded phrase appears in text bounded by non-word characters."""
    start = text.find(phrase)
    while start >= 0:
        end = start + len(phrase)
        before_ok = start == 0 or not text[start - 1].isalnum()
        after_ok = end == len(text) or not text[end].isalnum()
        if before_ok and after_ok:
            return True
        start = text.find(phrase, start + 1)
    return False


def _title_words(title: str) -> set[str]:
    """A title's words, without dates or other numbers."""
    return {word for word in re.findall(r"[^\W\d_]+", title.casefold()) if len(word) > 1}


def _day_bound(day: date, end: bool = False) -> str:
    moment = time.max if end else time.min
    return datetime.combine(day, moment, tzinfo=timezone.utc).isoformat()


class MentionResolver:
    """Resolves ambiguous mentions against the real people in the CRM."""

    def __init__(
        self,
        people: Iterable[PersonEntity],
        *,
        crm_db: str | Path,
        interaction_db: str | Path,
        vault_root: Path,
        overrides: Iterable[LinkOverride] = (),
        employment: Optional[EmploymentHistory] = None,
        my_person_id: Optional[str] = None,
        owner_name: Optional[str] = None,
    ):
        """
        Args:
            people: Visible, unmerged people; scoring skips one-word rows
            crm_db: CRM database, read for relationships and folder history
            interaction_db: Interactions database, read for contact history
            vault_root: Vault location, to read stored note paths against
            overrides: Link overrides, which take precedence over scoring
            employment: Employment history (defaults to the configured one)
            my_person_id: The owner's person id
            owner_name: The owner's first name, when no person id is configured
        """
        self._crm_db = crm_db
        self._interaction_db = interaction_db
        self._vault_root = vault_root
        self._overrides = list(overrides)
        self._employment = employment if employment is not None else get_employment_history()

        self._by_id: dict[str, PersonEntity] = {}
        self._by_first: dict[str, list[PersonEntity]] = defaultdict(list)
        self._by_last: dict[str, list[PersonEntity]] = defaultdict(list)
        self._by_alias: dict[str, list[PersonEntity]] = defaultdict(list)
        self._visible: dict[str, PersonEntity] = {}
        self._owner_first = (owner_name or "").strip().casefold() or None
        self._my_person_id = my_person_id

        for person in people:
            if person.id == my_person_id:
                self._owner_first = parse_name(person.canonical_name).first.casefold()
                continue
            self._visible[person.id] = person
            if not is_bare_person(person) and _reads_as_a_person(person):
                self._index_person(person)

        self._folder_links: Optional[dict[str, Counter]] = None
        self._contact_profile: Optional[dict[str, Counter]] = None
        self._employer_history: dict[str, Counter] = {}

    def _index_person(self, person: PersonEntity) -> None:
        self._by_id[person.id] = person
        parsed = parse_name(person.canonical_name)
        if len(parsed.first) > 1 and not any(c.isdigit() or c == "@" for c in parsed.first):
            self._by_first[parsed.first.casefold()].append(person)
        if parsed.last and len(parsed.last) > 1:
            self._by_last[parsed.last.casefold()].append(person)
        for alias in person.aliases:
            folded = alias.strip().casefold()
            if folded and folded != person.canonical_name.casefold():
                self._by_alias[folded].append(person)

    # ------------------------------------------------------------------ decide

    def resolve(self, mention: str, context: NoteContext) -> MentionDecision:
        """
        Decide who a mention means in this note, if anyone clearly.

        Args:
            mention: The name as the note gives it
            context: What the note offers

        Returns:
            A decision whose entity is set only for a link override or a
            candidate clearing both the minimum score and the minimum lead
        """
        parsed = parse_name(mention)
        override = most_specific_override(
            self._overrides, mention, source_type=None, context_path=context.rel_path
        )
        if override:
            preferred = self._visible.get(override.preferred_person_id)
            if preferred:
                return MentionDecision(mention, REASON_OVERRIDE, preferred, score=100.0)

        if self._owner_first and parsed.first.casefold() == self._owner_first and parsed.last is None:
            return MentionDecision(mention, REASON_OWNER)

        rejected = {override.rejected_person_id} if override else set()
        candidates = [c for c in self._candidates(parsed, mention) if c.entity.id not in rejected]
        if not candidates:
            return MentionDecision(mention, REASON_NO_CANDIDATE)

        self._score(candidates, context, mention)
        ranked = sorted(candidates, key=lambda c: (-c.score, c.entity.id))
        top = ranked[0]
        lead = top.score - ranked[1].score if len(ranked) > 1 else None

        if top.score < MENTION_MIN_SCORE:
            return MentionDecision(mention, REASON_BELOW_MINIMUM, score=top.score, lead=lead, candidates=ranked)
        if lead is not None and lead < MENTION_MIN_LEAD:
            return MentionDecision(mention, REASON_NARROW_LEAD, score=top.score, lead=lead, candidates=ranked)
        return MentionDecision(mention, REASON_LINKED, top.entity, score=top.score, lead=lead, candidates=ranked)

    def _candidates(self, parsed, mention: str) -> list[ScoredCandidate]:
        """Real people the mention could mean, with the points the name earns."""
        first = parsed.first.casefold().rstrip(".")
        last = (parsed.last or "").casefold().rstrip(".")
        found: dict[str, ScoredCandidate] = {}

        def note(person: PersonEntity, signal: str, points: float) -> None:
            found.setdefault(person.id, ScoredCandidate(person)).signals[signal] = points

        if len(first) == 1 and last:
            for person in self._by_last.get(last, []):
                if parse_name(person.canonical_name).first.casefold().startswith(first):
                    note(person, "name_initial", MENTION_WEIGHT_INITIAL)
                    note(person, "name_first", MENTION_WEIGHT_FIRST_NAME)
        else:
            for person in self._by_first.get(first, []):
                note(person, "name_first", MENTION_WEIGHT_FIRST_NAME)
            for person in self._by_alias.get(mention.strip().casefold(), []):
                note(person, "name_alias", MENTION_WEIGHT_ALIAS)
            # A variant is a candidate only when nobody goes by the name as given,
            # and it starts behind: the variant lists run broad.
            if not found:
                for variant in sorted(get_name_variants(first)):
                    for person in self._by_first.get(variant, []):
                        note(person, "name_nickname", MENTION_PENALTY_NICKNAME)
            if len(last) == 1:
                for person_id, candidate in list(found.items()):
                    person_last = parse_name(candidate.entity.canonical_name).last
                    if person_last and person_last.casefold().startswith(last):
                        candidate.signals["name_initial"] = MENTION_WEIGHT_INITIAL
                    else:
                        del found[person_id]

        return list(found.values())

    def _score(self, candidates: list[ScoredCandidate], context: NoteContext, mention: str) -> None:
        if len(candidates) == 1:
            candidates[0].signals["only_candidate"] = MENTION_WEIGHT_ONLY_CANDIDATE

        dictionary_category = get_person_category(mention)
        for candidate in candidates:
            self._add_note_signals(candidate, context, dictionary_category)

        ids = [c.entity.id for c in candidates]
        self._add_interaction_signals(candidates, context, ids)
        self._add_comention_signals(candidates, context, ids)

    def _add_note_signals(
        self, candidate: ScoredCandidate, context: NoteContext, dictionary_category: str
    ) -> None:
        person = candidate.entity
        signals = candidate.signals
        full_name = person.canonical_name.strip().casefold()
        multi_word = parse_name(person.canonical_name).last is not None

        if dictionary_category in ("family", "work") and person.category == dictionary_category:
            signals["dictionary_category"] = MENTION_WEIGHT_DICTIONARY_CATEGORY

        if multi_word and _has_whole_phrase(context.text, full_name):
            signals["full_name_in_note"] = MENTION_WEIGHT_FULL_NAME_IN_NOTE
        elif any(email.casefold() in context.text for email in person.emails):
            signals["full_name_in_note"] = MENTION_WEIGHT_FULL_NAME_IN_NOTE

        if multi_word and _has_whole_phrase(context.rel_path.casefold(), full_name):
            signals["person_path"] = MENTION_WEIGHT_PERSON_PATH

        folder = str(Path(context.rel_path).parent)
        if self._folder_link_counts().get(folder, Counter())[person.id] >= MENTION_FOLDER_MIN_LINKS:
            signals["folder_links"] = MENTION_WEIGHT_FOLDER_LINKS

        self._add_domain_signal(candidate, context)
        self._add_employer_signal(candidate, context)

    def _add_domain_signal(self, candidate: ScoredCandidate, context: NoteContext) -> None:
        if context.domain is None:
            return
        person = candidate.entity
        profile = self._profile(person.id)
        works_with = (
            profile["work"] > 0
            or person.category == "work"
            or bool(self._email_domains(person) & self._employment.email_domains)
        )
        personal = profile["personal"] > 0 or person.category == "family"

        if context.domain == "work":
            if works_with:
                candidate.signals["domain_match"] = MENTION_WEIGHT_DOMAIN_MATCH
            elif personal:
                candidate.signals["domain_mismatch"] = MENTION_PENALTY_DOMAIN_MISMATCH
        elif person.category == "family":
            candidate.signals["family_in_personal"] = MENTION_WEIGHT_FAMILY_IN_PERSONAL
        elif personal:
            candidate.signals["domain_match"] = MENTION_WEIGHT_DOMAIN_MATCH
        elif works_with:
            candidate.signals["domain_mismatch"] = MENTION_PENALTY_DOMAIN_MISMATCH

    def _add_employer_signal(self, candidate: ScoredCandidate, context: NoteContext) -> None:
        if not context.employers:
            return
        person = candidate.entity
        domains = self._email_domains(person)
        company = (person.company or "").strip().casefold()
        if any(domains & e.email_domains for e in context.employers):
            candidate.signals["employer_email"] = MENTION_WEIGHT_EMPLOYER_EMAIL
        elif any(self._employer_contact_count(e)[person.id] >= MENTION_EMPLOYER_HISTORY_MIN_INTERACTIONS
                 for e in context.employers):
            candidate.signals["employer_history"] = MENTION_WEIGHT_EMPLOYER_HISTORY
        elif company and any(_has_whole_phrase(company, e.name.casefold()) for e in context.employers):
            candidate.signals["employer_company"] = MENTION_WEIGHT_EMPLOYER_COMPANY
        elif domains & self._employment.email_domains:
            candidate.signals["other_employer"] = MENTION_PENALTY_OTHER_EMPLOYER

    def _add_interaction_signals(
        self, candidates: list[ScoredCandidate], context: NoteContext, ids: list[str]
    ) -> None:
        if context.note_date is None:
            return
        widest = max(days for days, _ in MENTION_INTERACTION_TIERS)
        closest = self._closest_contact(ids, context.note_date, widest)
        same_day = self._meeting_attendees(ids, context) if context.is_meeting else set()
        for candidate in candidates:
            distance = closest.get(candidate.entity.id)
            if distance is not None:
                for days, points in MENTION_INTERACTION_TIERS:
                    if distance <= days:
                        candidate.signals["interaction_near"] = points
                        break
            if candidate.entity.id in same_day:
                candidate.signals["meeting_same_day"] = MENTION_WEIGHT_MEETING_SAME_DAY

    def _add_comention_signals(
        self, candidates: list[ScoredCandidate], context: NoteContext, ids: list[str]
    ) -> None:
        others = context.co_mentioned_ids - set(ids)
        if not others:
            return
        strengths = self._pair_strengths(ids, others)
        for candidate in candidates:
            pairs = strengths.get(candidate.entity.id, [])
            points = sum(
                MENTION_WEIGHT_COMENTION_STRONG if s >= MENTION_COMENTION_STRONG_PAIR
                else MENTION_WEIGHT_COMENTION_WEAK
                for s in pairs
            )
            if points:
                candidate.signals["co_mention"] = min(points, MENTION_COMENTION_CAP)

    # ------------------------------------------------------------------- data

    @staticmethod
    def _email_domains(person: PersonEntity) -> set[str]:
        return {e.rsplit("@", 1)[1].casefold() for e in person.emails if "@" in e}

    def _profile(self, person_id: str) -> Counter:
        if self._contact_profile is None:
            profiles: dict[str, Counter] = defaultdict(Counter)
            with _read_only(self._interaction_db) as conn:
                for pid, source, account, count in conn.execute(
                    "SELECT person_id, source_type, source_account, COUNT(*) FROM interactions "
                    "GROUP BY person_id, source_type, source_account"
                ):
                    if source in _PERSONAL_CONTACT_SOURCES:
                        profiles[pid]["personal"] += count
                    elif source == "slack" or (source in _WORK_ACCOUNT_SOURCES and account == "work"):
                        profiles[pid]["work"] += count
            self._contact_profile = profiles
        return self._contact_profile.get(person_id, Counter())

    def _folder_link_counts(self) -> dict[str, Counter]:
        """Per folder, how many of its notes name each person in full.

        Only full-name and human-confirmed links count, so a first-name link
        this resolver made can never argue for itself.
        """
        if self._folder_links is None:
            links: dict[str, Counter] = defaultdict(Counter)
            with _read_only(self._crm_db) as conn:
                for source_id, person_id, observed, status in conn.execute(
                    "SELECT source_id, canonical_person_id, observed_name, link_status "
                    "FROM source_entities WHERE source_type IN ('vault', 'meeting', 'granola') "
                    "AND canonical_person_id IS NOT NULL"
                ):
                    if status != "confirmed" and is_ambiguous_mention(observed or ""):
                        continue
                    note_path, _, _ = (source_id or "").rpartition(":")
                    rel = vault_relative_path(note_path, self._vault_root)
                    if rel:
                        links[str(Path(rel).parent)][person_id] += 1
            self._folder_links = links
        return self._folder_links

    def _employer_contact_count(self, employer: Employer) -> Counter:
        """Work-account email, calendar, and Slack contact during a tenure, by person."""
        if employer.name not in self._employer_history:
            counts: Counter = Counter()
            if employer.start:
                with _read_only(self._interaction_db) as conn:
                    end = employer.end or date.today()
                    for pid, count in conn.execute(
                        "SELECT person_id, COUNT(*) FROM interactions "
                        "WHERE (source_type = 'slack' OR (source_type IN ('gmail', 'calendar') "
                        "AND source_account = 'work')) AND timestamp >= ? AND timestamp <= ? "
                        "GROUP BY person_id",
                        (_day_bound(employer.start), _day_bound(end, end=True)),
                    ):
                        counts[pid] = count
            self._employer_history[employer.name] = counts
        return self._employer_history[employer.name]

    def _closest_contact(self, ids: list[str], day: date, window: int) -> dict[str, float]:
        """Days from a note's date to each person's closest real interaction in the window."""
        start = _day_bound(day - timedelta(days=window))
        end = _day_bound(day + timedelta(days=window), end=True)
        reference = _day_bound(day)
        closest: dict[str, float] = {}
        with _read_only(self._interaction_db) as conn:
            for i in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[i:i + _SQL_CHUNK]
                marks = ",".join("?" * len(chunk))
                for pid, distance in conn.execute(
                    f"SELECT person_id, MIN(ABS(julianday(timestamp) - julianday(?))) FROM interactions "
                    f"WHERE person_id IN ({marks}) AND source_type NOT IN ('vault', 'meeting', 'granola') "
                    f"AND timestamp >= ? AND timestamp <= ? GROUP BY person_id",
                    (reference, *chunk, start, end),
                ):
                    if distance is not None:
                        closest[pid] = distance
        return closest

    def _meeting_attendees(self, ids: list[str], context: NoteContext) -> set[str]:
        """People on a small calendar event around a meeting note's date whose
        title agrees with the note's."""
        note_words = _title_words(context.title)
        if not note_words:
            return set()
        day = context.note_date
        start = _day_bound(day - timedelta(days=MENTION_MEETING_DAY_WINDOW))
        end = _day_bound(day + timedelta(days=MENTION_MEETING_DAY_WINDOW), end=True)
        found: set[str] = set()
        with _read_only(self._interaction_db) as conn:
            for i in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[i:i + _SQL_CHUNK]
                marks = ",".join("?" * len(chunk))
                for pid, title in conn.execute(
                    f"SELECT person_id, title FROM interactions WHERE person_id IN ({marks}) "
                    f"AND source_type = 'calendar' AND timestamp >= ? AND timestamp <= ? "
                    f"AND (attendee_count IS NULL OR attendee_count <= ?)",
                    (*chunk, start, end, MENTION_MEETING_MAX_ATTENDEES),
                ):
                    event_words = _title_words(title or "")
                    union = note_words | event_words
                    if union and len(note_words & event_words) / len(union) >= MENTION_MEETING_TITLE_OVERLAP:
                        found.add(pid)
        return found

    def _pair_strengths(self, ids: list[str], others: frozenset[str] | set[str]) -> dict[str, list[float]]:
        """Pair strength between each candidate and each person the note names."""
        from api.services.relationship import Relationship

        wanted = set(ids)
        pairs: dict[str, list[float]] = defaultdict(list)
        try:
            with _read_only(self._crm_db) as conn:
                other_list = sorted(others)
                for i in range(0, len(other_list), _SQL_CHUNK):
                    chunk = other_list[i:i + _SQL_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    for row in conn.execute(
                        f"SELECT * FROM relationships WHERE person_a_id IN ({marks}) OR person_b_id IN ({marks})",
                        (*chunk, *chunk),
                    ):
                        relationship = Relationship.from_row(row)
                        for mine, other in (
                            (relationship.person_a_id, relationship.person_b_id),
                            (relationship.person_b_id, relationship.person_a_id),
                        ):
                            if mine in wanted and other in others:
                                pairs[mine].append(relationship.pair_strength)
        except sqlite3.OperationalError:
            return {}  # A fresh install has no relationships table yet.
        return pairs


def load_link_overrides(crm_db: str | Path) -> list[LinkOverride]:
    """Read link overrides without touching the database."""
    try:
        with _read_only(crm_db) as conn:
            return [
                LinkOverride(
                    id=row[0], name_pattern=row[1], source_type=row[2], context_pattern=row[3],
                    preferred_person_id=row[4], rejected_person_id=row[5], reason=row[6],
                )
                for row in conn.execute(
                    "SELECT id, name_pattern, source_type, context_pattern, preferred_person_id, "
                    "rejected_person_id, reason FROM link_overrides"
                )
            ]
    except sqlite3.OperationalError:
        return []


def load_tag_domain(tags_db: Optional[str | Path], rel_path: str) -> Optional[str]:
    """The tagged domain for one note, or None when there's no store or row."""
    if not tags_db or not Path(tags_db).exists():
        return None
    try:
        with _read_only(tags_db) as conn:
            row = conn.execute("SELECT domain FROM vault_tags WHERE file_path = ?", (rel_path,)).fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def build_note_context(
    rel_path: str,
    frontmatter: dict,
    body: str,
    *,
    note_date: Optional[date],
    tag_domain: Optional[str],
    employment: EmploymentHistory,
    co_mentioned_ids: Iterable[str] = (),
    is_meeting: bool = False,
) -> NoteContext:
    """
    Assemble what a note offers for reading its mentions.

    A note's employer comes from its folder, or, for a dated work note in no
    employer's folder, from whoever employed the owner on that date.

    Args:
        rel_path: Vault-relative note path
        frontmatter: Parsed frontmatter
        body: Note body
        note_date: The note's date, if it has a trustworthy one
        tag_domain: vault_tags.domain for the note, if tagged
        employment: Employment history
        co_mentioned_ids: People the note names in full
        is_meeting: Whether this is a meeting transcript

    Returns:
        The note's context
    """
    domain = note_domain(rel_path, tag_domain, employment)
    employers = employment.for_path(rel_path)
    if not employers and domain == "work" and note_date:
        employers = employment.on_date(note_date)
    people = frontmatter.get("people") or []
    people_text = json.dumps(people, default=str) if people else ""
    return NoteContext(
        rel_path=rel_path,
        note_date=note_date,
        domain=domain,
        employers=employers,
        text=re.sub(r"\s+", " ", f"{people_text} {body}").casefold(),
        co_mentioned_ids=frozenset(co_mentioned_ids),
        is_meeting=is_meeting or str(frontmatter.get("type", "")).casefold() == "meeting",
        title=str(frontmatter.get("title") or Path(rel_path).stem),
    )
