"""
Who a chat message is about, and how sure we can be.

A full name, a deliberate alias or an unscoped override identifies one person. A
lone word does not: it can fit several real people, a row that holds nothing but
that word, or a place or company. Only an identified person's CRM profile is
handed to the model as fact. Anything less is handed over as options, labelled
uncertain, and carries no facts, because wrong context is worse than none.

Resolution tiers:
- ``confident``: the name identifies this person.
- ``conversation``: the name alone does not, but the recent conversation names
  exactly one person it could mean.
- ``likely``: one person is the best fit. Stated as an assumption.
- ``uncertain``: several people fit, or the only fit is a one-word row.
- ``none``: no live person fits.

The chat stream reaches this through ``gather_person_context``, which runs
without an LLM call, is bounded by a short time budget, and returns ``None`` on
any failure so the turn proceeds without a person block.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

from config.relationship_weights import (
    CLEARLY_CLOSER_MIN_STRENGTH_LEAD,
    CLOSE_RELATIONSHIP_MIN_STRENGTH,
)

logger = logging.getLogger(__name__)

CONFIDENT = "confident"
FROM_CONVERSATION = "conversation"
LIKELY = "likely"
UNCERTAIN = "uncertain"
NONE = "none"

# The turn waits at most this long before going ahead with no person block.
PERSON_CONTEXT_TIMEOUT_SECONDS = 0.5

# Facts in the system-prompt block; person_info(lookup) shows more on request.
PROFILE_FACT_LIMIT = 8

# People listed when a name fits several.
MAX_LISTED_CANDIDATES = 5

# Recent messages searched for evidence of who a first name means.
CONVERSATION_LOOKBACK = 6

# Names tried per message, each of which can cost a scan of every person.
MAX_NAMES_PER_MESSAGE = 3

# Capitalized or captured words that are never a person in a question.
_NOT_NAMES = frozenset("""
    i me my mine we us our you your he him his she her hers they them their it its this that these those
    there here what when where who whom whose why how which did do does is are was were am be been can could
    would should shall may might must have has had tell show give find get send let please remind
    and but or if so then also the a an any some all no yes hi hey hello thanks ok okay
    monday tuesday wednesday thursday friday saturday sunday
    january february march april june july august september october november december
    today tomorrow yesterday tonight
""".split())

_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_][\w'’.-]*")
_MY_RELATION = re.compile(r"\bmy\s+([^\W\d_]+)\b", re.IGNORECASE)
_LEADING_MY = re.compile(r"^my\s+", re.IGNORECASE)

# Lowercase words that can sit inside a full name.
_NAME_PARTICLES = frozenset("bin da de del der di du la le van von".split())

# A lone capitalized word right after one of these names a place or a thing:
# "the weather in Austin", "open the Chase account".
_NON_PERSON_LEADS = frozenset(
    "in at near around across inside outside via on of the a an this that these those my our your his her their".split()
)

# Words that can follow the object of an imperative: "Call Robin back", "Ask Robin about it".
# A noun or a verb after it says the capitalized words were a compound or a subject instead.
_FOLLOWS_AN_OBJECT = frozenset(
    "about for to that if and or when on at by in with re regarding before after "
    "tomorrow today tonight now this next back again please i we you my the a an".split()
)

# First words of place names, which stay whole rather than giving up a first name.
_PLACE_PREFIXES = frozenset("new san santa los las saint st fort north south east west".split())

# How a message offered a name, which sets how much it takes to raise it.
_ANCHORED = "anchored"  # The phrasing says it is a person: "tell me about Robin"
_LOOSE = "loose"  # A capitalized word somewhere in the message, or a lowercase capture
_RELATION = "relation"  # "my mom": only an alias or override may answer this


@dataclass
class PersonResolution:
    """What a name in a message identifies."""

    tier: str
    asked: str
    person: Optional[object] = None  # The identified, or best-fitting, PersonEntity
    candidates: list = field(default_factory=list)  # Everyone it could mean, closest first
    answers_to_name: bool = False  # An alias, override or dictionary entry names this person


@dataclass
class PersonContext:
    """The system-prompt block for one turn and what the turn records about it."""

    block: str
    tier: str
    fetch_depth: str
    person_name: Optional[str] = None
    person_id: Optional[str] = None

    def routing_fields(self) -> dict:
        """Fields for the stored assistant message. An unconfirmed person is never carried forward."""
        fields = {"person_tier": self.tier, "fetch_depth": self.fetch_depth}
        if self.person_id:
            fields["person"] = self.person_name
            fields["person_id"] = self.person_id
        return fields


def _strength(person) -> float:
    return person.relationship_strength or 0


def _is_one_word_row(person) -> bool:
    from api.services.entity_resolver import parse_name

    return parse_name(person.canonical_name).last is None


def _goes_by(person, word: str) -> bool:
    """Whether a person's own name or one of their aliases starts with this word."""
    from api.services.entity_resolver import parse_name

    return any(
        (parse_name(name).first or "").lower() == word
        for name in [person.canonical_name, *(person.aliases or [])]
    )


def _clean_name(name: str) -> str:
    return _POSSESSIVE.sub("", _LEADING_MY.sub("", (name or "").strip()))


def _mentioned_in_conversation(people: list, history: Optional[list]) -> list:
    """The candidates the recent messages name, by full name or as a turn's identified person."""
    if not history:
        return []
    recent = history[-CONVERSATION_LOOKBACK:]
    stored_ids = {(getattr(m, "routing", None) or {}).get("person_id") for m in recent}
    text = " ".join(m.content for m in recent if getattr(m, "content", None)).lower()
    return [
        p for p in people
        if p.id in stored_ids
        or (not _is_one_word_row(p) and re.search(rf"\b{re.escape(p.canonical_name.lower())}\b", text))
    ]


def _is_best_fit(people: list) -> bool:
    """Whether the first, closest person fits clearly better than the rest."""
    top = people[0]
    if _is_one_word_row(top):
        return False
    if len(people) == 1:
        return True
    if _strength(top) < CLOSE_RELATIONSHIP_MIN_STRENGTH:
        return False
    runner_up = _strength(people[1])
    return (
        runner_up < CLOSE_RELATIONSHIP_MIN_STRENGTH
        or _strength(top) - runner_up >= CLEARLY_CLOSER_MIN_STRENGTH_LEAD
    )


def _weigh_candidates(asked: str, people: list, history: Optional[list]) -> PersonResolution:
    """Choose a tier for a name that fits people without identifying one by itself."""
    people = sorted(people, key=_strength, reverse=True)
    if not people:
        return PersonResolution(NONE, asked)
    mentioned = _mentioned_in_conversation(people, history)
    if len(mentioned) == 1:
        return PersonResolution(FROM_CONVERSATION, asked, person=mentioned[0], candidates=people)
    if mentioned:
        # Both were just discussed, so neither wins, and neither may fall off the list.
        others = [p for p in people if p not in mentioned]
        return PersonResolution(UNCERTAIN, asked, candidates=[*mentioned, *others])
    if _is_best_fit(people):
        return PersonResolution(LIKELY, asked, person=people[0], candidates=people)
    return PersonResolution(UNCERTAIN, asked, candidates=people)


def _overrides_for(name: str) -> tuple[Optional[object], list]:
    """The override that holds everywhere, if any, and those scoped to a source or note path."""
    from api.services.entity_resolver import get_link_override_store

    matching = [o for o in get_link_override_store().get_all() if o.matches(name)]
    unscoped = next((o for o in matching if not (o.source_type or o.context_pattern)), None)
    return unscoped, [o for o in matching if o.source_type or o.context_pattern]


def _resolve_one_word(resolver, asked: str, canonical: str, history: Optional[list]) -> PersonResolution:
    from api.services.entity_resolver import parse_name

    def live(person_id: str):
        person = resolver.store.get_by_id(person_id)
        return person if person and not person.hidden else None

    unscoped, scoped = _overrides_for(canonical)
    preferred = live(unscoped.preferred_person_id) if unscoped else None
    if preferred:
        return PersonResolution(CONFIDENT, asked, person=preferred, answers_to_name=True)

    word = canonical.lower()
    exact = resolver.store.get_by_name(canonical)
    # An alias that is just someone's own first name is that first name again.
    # Only one they alone are known by says who is meant.
    holder = (
        exact if exact and not exact.hidden and not _is_one_word_row(exact)
        and (parse_name(exact.canonical_name).first or "").lower() != word
        else None
    )
    # A scoped override says what the name means in one source or folder, and chat
    # is neither, so it can put someone forward but never settle it.
    put_forward = [p for p in (live(o.preferred_person_id) for o in scoped) if p]

    people = [c.entity for c in resolver.candidates_for_name(canonical)]
    for person in ([holder] if holder else []) + put_forward:
        if all(p.id != person.id for p in people):
            people.append(person)
    # A one-word row with no real relationship behind it is noise, not a candidate.
    people = [
        p for p in people
        if not p.hidden and (not _is_one_word_row(p) or _strength(p) >= CLOSE_RELATIONSHIP_MIN_STRENGTH)
    ]

    if holder:
        rivals = [
            p for p in people
            if p.id != holder.id
            and _strength(p) >= CLOSE_RELATIONSHIP_MIN_STRENGTH
            and _goes_by(p, word)
        ]
        if not rivals:
            return PersonResolution(CONFIDENT, asked, person=holder, answers_to_name=True)
        people = [holder, *rivals, *(p for p in put_forward if p.id != holder.id and p not in rivals)]

    resolution = _weigh_candidates(asked, people, history)
    resolution.answers_to_name = bool(resolution.person and resolution.person in put_forward)
    return resolution


def _resolve_full_name(resolver, asked: str, canonical: str, history: Optional[list]) -> PersonResolution:
    match = resolver.match_name(asked)
    if match.tied:
        people = [p for p in match.tied if not p.hidden]
        if len(people) == 1:
            return PersonResolution(CONFIDENT, asked, person=people[0])
        return _weigh_candidates(asked, people, history)
    entity = match.result.entity if match.result else None
    if not entity or entity.hidden:
        return PersonResolution(NONE, asked)
    if match.result.match_type == "link_override" and not _overrides_for(canonical)[0]:
        return _weigh_candidates(asked, [entity], history)
    return PersonResolution(CONFIDENT, asked, person=entity)


def resolve_person_mention(name: str, history: Optional[list] = None) -> PersonResolution:
    """
    Work out who a name in a message means, without creating anyone.

    Args:
        name: The name as written ("Robin", "Robin Example", "my mom")
        history: Recent conversation messages, which can settle a first name

    Returns:
        PersonResolution. Hidden and merged people never appear in it.
    """
    from api.services.entity_resolver import get_entity_resolver, parse_name
    from api.services.people import resolve_person_name

    asked = _clean_name(name)
    if not asked:
        return PersonResolution(NONE, name or "")
    resolver = get_entity_resolver()
    canonical = resolve_person_name(asked)
    if parse_name(canonical).last is None:
        return _resolve_one_word(resolver, asked, canonical, history)
    resolution = _resolve_full_name(resolver, asked, canonical, history)
    # One word that reached a full name did so through the people dictionary.
    resolution.answers_to_name = resolution.tier == CONFIDENT and " " not in asked
    return resolution


def format_person_profile(entity, fact_limit: int) -> str:
    """An identified person's identifiers, relationship summary and strongest facts."""
    from api.services.person_facts import get_person_fact_store, rank_facts
    from api.services.relationship_summary import format_relationship_context, get_relationship_summary

    parts = [f"**{entity.canonical_name}** (entity_id: {entity.id})"]

    if entity.emails:
        parts.append(f"Emails: {', '.join(entity.emails)}")
    if entity.phone_numbers:
        parts.append(f"Phones: {', '.join(entity.phone_numbers)}")
    if entity.birthday:
        parts.append(f"Birthday: {entity.birthday}")
    if entity.company or entity.position:
        role = " — ".join(filter(None, [entity.position, entity.company]))
        parts.append(f"Role: {role}")

    # Relationship summary
    rel = get_relationship_summary(entity.id)
    if rel:
        parts.append(format_relationship_context(rel))

    # Person facts. Ranked by confidence rather than the store's category/key
    # order: an alphabetical cut can drop "family: spouse" for a work fact purely
    # on spelling, and the model then answers that it doesn't know.
    fact_store = get_person_fact_store()
    facts = rank_facts(fact_store.get_for_person(entity.id))
    if facts:
        shown = facts[:fact_limit]
        header = "Known facts:"
        if len(facts) > len(shown):
            header = (
                f"Known facts ({len(shown)} of {len(facts)} shown, "
                "highest-confidence first; the rest were truncated for length — "
                "ask for a specific category to see more):"
            )
        parts.append(header + "\n" + "\n".join(
            f"- {f.category}: {f.key} = {f.value}" for f in shown
        ))

    return "\n\n".join(parts)


def _candidate_line(person) -> str:
    """Enough to tell one candidate from another, and no facts about them."""
    from api.services.query_router import QueryRouter

    details = [QueryRouter()._get_disambiguation_context(person)]
    details.append(f"closeness {round(_strength(person))}/100")
    if person.last_seen:
        seen = person.last_seen if person.last_seen.tzinfo else person.last_seen.replace(tzinfo=timezone.utc)
        details.append(f"last contact {(datetime.now(timezone.utc) - seen).days} days ago")
    return f"- {person.canonical_name} (entity_id: {person.id}): {', '.join(details)}"


def _candidate_lines(people: list) -> str:
    lines = [_candidate_line(p) for p in people[:MAX_LISTED_CANDIDATES]]
    if len(people) > MAX_LISTED_CANDIDATES:
        lines.append(f"- and {len(people) - MAX_LISTED_CANDIDATES} less close people with that name")
    return "\n".join(lines)


def describe_unconfirmed(resolution: PersonResolution) -> str:
    """How a likely or uncertain name reads to the model. Never asserts a person."""
    asked = resolution.asked
    if resolution.tier == LIKELY:
        others = [p for p in resolution.candidates if p.id != resolution.person.id]
        text = (
            f"'{asked}' does not identify one person for certain. The best fit by closeness is "
            f"{resolution.person.canonical_name} (entity_id: {resolution.person.id}), but that is "
            "an inference, not a confirmed match."
        )
        if others:
            text += f"\nOthers it could mean:\n{_candidate_lines(others)}"
        return text + (
            f"\nIf you go on with {resolution.person.canonical_name}, say so in your answer "
            f"(\"Assuming you mean {resolution.person.canonical_name}, ...\") so a wrong guess is "
            f"visible. Looking up '{asked}' again returns this same text; look someone up by "
            "their full name for their details."
        )
    return (
        f"'{asked}' does not identify one person. People it could mean, closest first:\n"
        f"{_candidate_lines(resolution.candidates)}\n"
        "Do not present any of them as the person asked about. Settle it from the rest of the "
        "message and the conversation; if nothing does, ask which one is meant, naming these "
        f"options. Looking up '{asked}' again returns this same list; look someone up by their "
        "full name for their details."
    )


_DEPTH_GUIDANCE = {
    "deep": (
        "The user asked for the full picture: search every channel this person is active on "
        "before answering."
    ),
    "shallow": (
        "This is a quick lookup: answer from the profile above if it covers the question, "
        "before searching anything else."
    ),
}


def _render_block(resolution: PersonResolution, depth: str) -> str:
    if resolution.tier in (LIKELY, UNCERTAIN):
        return f"## Person in this message (not confirmed)\n\n{describe_unconfirmed(resolution)}"

    person = resolution.person
    basis = (
        f"'{resolution.asked}' is taken to be {person.canonical_name} from the recent conversation, "
        "not from the name alone. Say so if the user may mean someone else."
        if resolution.tier == FROM_CONVERSATION
        else f"'{resolution.asked}' is {person.canonical_name}."
    )
    sections = [
        "## Person in this message",
        basis,
        format_person_profile(person, PROFILE_FACT_LIMIT),
        "Use this entity_id and these identifiers directly; person_info(lookup) would return "
        "the same profile.",
    ]
    if depth in _DEPTH_GUIDANCE:
        sections.append(_DEPTH_GUIDANCE[depth])
    return "\n\n".join(sections)


def _capitalized_spans(question: str) -> list[tuple[str, int, int]]:
    """Runs of adjacent capitalized words, as (text, start, end), broken at words that are never names."""
    spans: list[tuple[str, int, int]] = []
    words: list[str] = []
    start = end = 0

    def flush() -> None:
        while words and words[-1] in _NAME_PARTICLES:
            words.pop()
        if words:
            spans.append((" ".join(words), start, end))
        words.clear()

    for match in _WORD.finditer(question):
        # An initial keeps its full stop; anywhere else a full stop ends the name.
        word = match.group(0).rstrip("-'’")
        if len(word) > 2:
            word = word.rstrip(".")
        is_name_word = word[:1].isupper() and re.split(r"['’]", word)[0].lower() not in _NOT_NAMES
        continues = (
            (is_name_word or word in _NAME_PARTICLES)
            and words
            and not question[end:match.start()].strip()
            and not _POSSESSIVE.search(words[-1])
        )
        if continues:
            words.append(word)
        else:
            flush()
            if is_name_word:
                words.append(word)
            start = match.start()
        end = match.start() + len(word)
    flush()
    return spans


def _starts_a_sentence(question: str, start: int) -> bool:
    before = question[:start].rstrip()
    return not before or before[-1] in ".?!:\n"


def _follows_a_non_person_lead(question: str, start: int) -> bool:
    before = question[:start].split()
    return bool(before) and before[-1].lower() in _NON_PERSON_LEADS


def _is_leading_verb(question: str, words: list[str], end: int) -> bool:
    """
    Whether a sentence's first capitalized word is a verb in front of a name, not part of it.

    A known first name is part of the name ("Nancy Drew"). For a lone word after
    it, the rest of the sentence decides: "Call Robin back" fits an imperative,
    "Lemon Sage tea" and "Nancy Drew is a classic" do not, and neither does a
    possessive ("Michael Jordan's stats").
    """
    from config.nickname_lookup import get_name_variants

    if words[0].lower() in _PLACE_PREFIXES or get_name_variants(words[0]):
        return False
    if len(words) > 2:
        return True
    following = re.match(r"\s*([^\W\d_]*)", question[end:]).group(1)
    return not _POSSESSIVE.search(words[1]) and (not following or following.lower() in _FOLLOWS_AN_OBJECT)


def _names_in_span(question: str, text: str, start: int, end: int) -> Iterator[str]:
    """What a run of capitalized words could name, the whole run first."""
    words = text.split()
    possessive = bool(_POSSESSIVE.search(text))
    if not _starts_a_sentence(question, start):
        if len(words) > 1 or possessive or not _follows_a_non_person_lead(question, start):
            yield _clean_name(text)
        return
    # A sentence's first word is capitalized regardless. It may be a name, or a
    # verb in front of one ("Call Robin Alpha", "Ask Robin"), and alone it counts
    # only as a possessive.
    if len(words) > 1 or possessive:
        yield _clean_name(text)
    if len(words) > 1 and _is_leading_verb(question, words, end):
        yield _clean_name(" ".join(words[1:]))


def _mentioned_names(question: str) -> Iterator[tuple[str, str]]:
    """Names a message might carry, as (name, kind), the most trustworthy first."""
    from api.services.query_router import QueryRouter

    spans = _capitalized_spans(question)

    extracted = QueryRouter()._extract_person_name(question)
    if extracted and extracted.lower() not in _NOT_NAMES:
        # The capture stops at two plain ASCII words; the message may hold more of the name.
        fuller = next((text for text, _, _ in spans if text.lower().startswith(extracted.lower())), None)
        if fuller:
            yield fuller, _ANCHORED
        else:
            yield extracted, _ANCHORED if extracted[0].isupper() else _LOOSE

    for match in _MY_RELATION.finditer(question):
        if match.group(1).lower() not in _NOT_NAMES:
            yield match.group(0), _RELATION

    for text, start, end in spans:
        for name in _names_in_span(question, text, start, end):
            yield name, _LOOSE


def _is_reachable_person(person) -> bool:
    """Whether the record shows a person rather than a sender: a phone, messages or meetings."""
    return bool(person.phone_numbers or person.message_count or person.meeting_count)


def _is_worth_raising(resolution: PersonResolution, name: str, kind: str) -> bool:
    """Whether a name offered this way matched someone it is reasonable to mean."""
    if resolution.tier == NONE:
        return False
    if kind == _ANCHORED:
        return True
    if kind == _RELATION:
        return resolution.answers_to_name
    if resolution.tier in (CONFIDENT, FROM_CONVERSATION) or resolution.answers_to_name:
        return True
    # A lone capitalized word is often a company or a place that shares a row with
    # a sender. Only someone actually in touch makes it worth mentioning.
    return any(
        not _is_one_word_row(p)
        and _strength(p) >= CLOSE_RELATIONSHIP_MIN_STRENGTH
        and (" " in name or _is_reachable_person(p))
        for p in resolution.candidates
    )


def _could_be_a_relation(word: str) -> bool:
    """Whether "my <word>" has anyone behind it, without the cost of scoring every person."""
    from api.services.entity_resolver import get_entity_resolver, parse_name
    from api.services.people import resolve_person_name

    canonical = resolve_person_name(word)
    if parse_name(canonical).last is not None or any(_overrides_for(canonical)):
        return True
    exact = get_entity_resolver().store.get_by_name(canonical)
    return bool(exact and not _is_one_word_row(exact))


def build_person_context(question: str, history: Optional[list] = None) -> Optional[PersonContext]:
    """
    Build the person block for a message, or None when it names no one we know.

    Args:
        question: The message, after follow-up expansion
        history: Recent conversation messages

    Returns:
        PersonContext for the first name that resolves, or None
    """
    from api.services.query_router import QueryRouter

    tried: set[str] = set()
    for name, kind in _mentioned_names(question):
        key = _clean_name(name).lower()
        if key in tried or (kind == _RELATION and not _could_be_a_relation(key)):
            continue
        if len(tried) >= MAX_NAMES_PER_MESSAGE:
            return None
        tried.add(key)

        resolution = resolve_person_mention(name, history)
        if not _is_worth_raising(resolution, key, kind):
            continue

        depth = QueryRouter()._determine_fetch_depth(question)
        identified = resolution.tier in (CONFIDENT, FROM_CONVERSATION)
        return PersonContext(
            block=_render_block(resolution, depth),
            tier=resolution.tier,
            fetch_depth=depth,
            person_name=resolution.person.canonical_name if identified else None,
            person_id=resolution.person.id if identified else None,
        )
    return None


async def gather_person_context(question: str, history: Optional[list] = None) -> Optional[PersonContext]:
    """Build the person block off the event loop. Any failure or overrun yields None."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(build_person_context, question, history),
            timeout=PERSON_CONTEXT_TIMEOUT_SECONDS,
        )
    except Exception as e:
        # The type only: the message can carry a name.
        logger.warning("Person context skipped: %s", type(e).__name__)
        return None
