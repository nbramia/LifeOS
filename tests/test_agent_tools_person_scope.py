"""
Tests for scope disclosure on the person surface (person_info + briefings).

Regression context: same bug class as tests/test_agent_tools_scope_widening.py.
The person surface presented narrow or truncated views as complete ones:

- The briefing only ever read 90 days of interactions, the caller could not
  widen it, and an empty window was collapsed to "no interaction history
  available" — printed right next to a real last-contact date, so the answer
  contradicted itself and read as "the records are thin".
- person_info showed the first 15 facts in alphabetical order with no hint that
  it had cut anything, so "who is their spouse?" could be answered "I don't
  know" because `family` sorted past the cut.
- Briefings dropped facts below a confidence floor without saying so.
- `days_since_contact` carries 999 as a never-contacted sentinel; printed
  verbatim it reads as a real gap of about 2.7 years.

These tests pin the fix: widen when the caller stated no window, honour a stated
one exactly, order and disclose truncated facts, disclose the confidence floor,
and never render the sentinel as a number.

All people here are obviously synthetic.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import api.services.agent_tools as at
import api.services.briefings as briefings
import api.services.entity_resolver as entity_resolver_mod
import api.services.person_facts as person_facts_mod
import api.services.relationship_summary as rel_mod
from api.services.agent_tools import (
    _BRIEFING_MAX_DAYS,
    _PERSON_FACT_LIMIT,
    TOOL_DEFINITIONS,
    _briefing_person,
    _lookup_person,
)
from api.services.briefings import (
    FACT_CONFIDENCE_FLOOR,
    _INTERACTION_LADDER_DAYS,
    BriefingsService,
)
from api.services.interaction_store import (
    NO_INTERACTIONS_PREFIX,
    format_window_label,
)
from api.services.person_facts import PersonFact, rank_facts
from api.services.relationship_summary import (
    NEVER_CONTACTED_DAYS,
    ChannelActivity,
    RelationshipSummary,
    format_relationship_context,
)

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")

# The store's default window; the widest ladder rung resolves to this.
_FULL_WINDOW_DAYS = 3650


def _assert_no_fault_language(text: str, label: str) -> None:
    lowered = text.lower()
    for word in FAULT_WORDS:
        assert word not in lowered, f"{label} implies a fault: {word!r}"


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


# ---------------------------------------------------------------------------
# person_info action="lookup"
# ---------------------------------------------------------------------------

def _fact(category: str, key: str, value: str, confidence: float, confirmed=False):
    return PersonFact(
        person_id="person-quill",
        category=category,
        key=key,
        value=value,
        confidence=confidence,
        confirmed_by_user=confirmed,
    )


@pytest.fixture
def fake_person(monkeypatch):
    """Stub the three service boundaries person_info(lookup) reads.

    Records the name the resolver was asked for, and lets a test set the facts
    and the relationship summary. `format_relationship_context` is deliberately
    left real: the sentinel rendering is part of what these tests pin.
    """
    state = SimpleNamespace(
        entity=SimpleNamespace(
            id="person-quill",
            canonical_name="Marigold Quill",
            emails=["marigold.quill@example.invalid"],
            phone_numbers=[],
            birthday=None,
            company="Tidewater Press",
            position="Editor",
        ),
        resolved=True,
        resolve_calls=[],
        facts=[],
        summary=None,
    )

    class FakeResolver:
        def resolve(self, name=None, email=None):
            state.resolve_calls.append({"name": name, "email": email})
            if not state.resolved:
                return None
            return SimpleNamespace(entity=state.entity)

    class FakeFactStore:
        def get_for_person(self, person_id, include_shared=True):
            return list(state.facts)

    monkeypatch.setattr(entity_resolver_mod, "get_entity_resolver", lambda: FakeResolver())
    monkeypatch.setattr(person_facts_mod, "get_person_fact_store", lambda: FakeFactStore())
    monkeypatch.setattr(rel_mod, "get_relationship_summary", lambda pid: state.summary)
    return state


def _summary(days_since: int, last_interaction: datetime | None) -> RelationshipSummary:
    return RelationshipSummary(
        person_id="person-quill",
        person_name="Marigold Quill",
        relationship_strength=41.0,
        channels=[ChannelActivity("gmail", 2, last_interaction, False)],
        active_channels=[],
        primary_channel="gmail",
        total_interactions_90d=2,
        last_interaction=last_interaction,
        days_since_contact=days_since,
    )


class TestLookupNoMatch:
    """A miss must name what was searched, without implying a fault.

    The match threshold itself is unchanged — a loose threshold would merge real
    people, which is worse than a miss. Only the message improves.
    """

    def test_names_the_search_term(self, fake_person):
        fake_person.resolved = False
        out = _lookup_person({"name": "Thistlewaite Barrowman"})
        assert "'Thistlewaite Barrowman'" in out

    def test_names_the_term_actually_sent_to_the_resolver(self, fake_person):
        fake_person.resolved = False
        _lookup_person({"name": "Thistlewaite Barrowman"})
        assert [c["name"] for c in fake_person.resolve_calls] == [
            "Thistlewaite Barrowman"
        ]

    def test_suggests_a_next_step(self, fake_person):
        fake_person.resolved = False
        out = _lookup_person({"name": "Thistlewaite Barrowman"})
        assert "email" in out.lower()

    def test_miss_carries_no_fault_language(self, fake_person):
        fake_person.resolved = False
        out = _lookup_person({"name": "Thistlewaite Barrowman"})
        _assert_no_fault_language(out, "person lookup miss")

    def test_empty_resolver_result_is_also_a_miss(self, fake_person):
        """A result object with no entity is the other shape a miss arrives in."""
        fake_person.resolved = True
        fake_person.entity = None
        out = _lookup_person({"name": "Thistlewaite Barrowman"})
        assert "No person found matching 'Thistlewaite Barrowman'." in out


class TestLookupFactOrdering:
    """Facts are cut by confidence, not by spelling, and the cut is disclosed."""

    def _alphabetically_late_but_certain(self) -> list:
        """A high-confidence `family` fact buried under low-confidence noise.

        `work`/`travel`/`topics` all sort after `family`, so the pre-fix
        alphabetical slice kept the noise and dropped the spouse.
        """
        noise = [
            _fact("travel", f"trip_{i:02d}", f"Mentioned a trip to Region {i}", 0.31)
            for i in range(_PERSON_FACT_LIMIT + 5)
        ]
        return noise + [
            _fact("family", "spouse_name", "Spouse is Rowan Quill", 0.95),
        ]

    def test_high_confidence_fact_survives_the_cut(self, fake_person):
        fake_person.facts = self._alphabetically_late_but_certain()
        out = _lookup_person({"name": "Marigold Quill"})
        assert "Spouse is Rowan Quill" in out

    def test_facts_are_ordered_by_confidence(self, fake_person):
        fake_person.facts = [
            _fact("work", "role", "Runs the poetry imprint", 0.9),
            _fact("family", "sibling", "Has a sibling in Lowmarsh", 0.4),
            _fact("topics", "hobby", "Restores fountain pens", 0.7),
        ]
        out = _lookup_person({"name": "Marigold Quill"})
        assert out.index("Runs the poetry imprint") < out.index("Restores fountain pens")
        assert out.index("Restores fountain pens") < out.index("Has a sibling in Lowmarsh")

    def test_truncation_states_shown_and_total(self, fake_person):
        total = _PERSON_FACT_LIMIT + 27
        fake_person.facts = [
            _fact("topics", f"note_{i:02d}", f"Discussed subject {i}", 0.5)
            for i in range(total)
        ]
        out = _lookup_person({"name": "Marigold Quill"})
        assert f"{_PERSON_FACT_LIMIT} of {total} shown" in out

    def test_truncation_says_how_the_shown_set_was_chosen(self, fake_person):
        fake_person.facts = [
            _fact("topics", f"note_{i:02d}", f"Discussed subject {i}", 0.5)
            for i in range(_PERSON_FACT_LIMIT + 1)
        ]
        out = _lookup_person({"name": "Marigold Quill"})
        assert "highest-confidence first" in out

    def test_shown_count_matches_the_limit(self, fake_person):
        fake_person.facts = [
            _fact("topics", f"note_{i:02d}", f"Discussed subject {i}", 0.5)
            for i in range(_PERSON_FACT_LIMIT + 9)
        ]
        out = _lookup_person({"name": "Marigold Quill"})
        assert out.count("Discussed subject") == _PERSON_FACT_LIMIT

    def test_no_truncation_note_when_everything_fits(self, fake_person):
        fake_person.facts = [_fact("work", "role", "Runs the poetry imprint", 0.9)]
        out = _lookup_person({"name": "Marigold Quill"})
        assert "Known facts:" in out
        assert "shown" not in out

    def test_exactly_at_the_limit_is_not_reported_as_truncated(self, fake_person):
        fake_person.facts = [
            _fact("topics", f"note_{i:02d}", f"Discussed subject {i}", 0.5)
            for i in range(_PERSON_FACT_LIMIT)
        ]
        out = _lookup_person({"name": "Marigold Quill"})
        assert "shown" not in out

    def test_low_confidence_facts_are_still_shown_when_they_fit(self, fake_person):
        """lookup has no confidence floor — only the briefing does."""
        fake_person.facts = [_fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2)]
        out = _lookup_person({"name": "Marigold Quill"})
        assert "Maybe moved to Lowmarsh" in out


class TestFactRanking:
    """rank_facts is the shared ordering; the store's own order is untouched."""

    def test_orders_by_descending_confidence(self):
        facts = [
            _fact("a", "one", "low", 0.2),
            _fact("z", "two", "high", 0.9),
            _fact("m", "three", "mid", 0.5),
        ]
        assert [f.value for f in rank_facts(facts)] == ["high", "mid", "low"]

    def test_user_confirmed_fact_counts_as_full_confidence(self):
        """The user asserted it, which outranks any extraction score."""
        facts = [
            _fact("z", "extracted", "high extraction", 0.95),
            _fact("a", "confirmed", "user said so", 0.10, confirmed=True),
        ]
        ranked = rank_facts(facts)
        assert ranked[0].value == "user said so"

    def test_ties_fall_back_to_category_then_key(self):
        facts = [
            _fact("work", "b", "work b", 0.5),
            _fact("family", "z", "family z", 0.5),
            _fact("work", "a", "work a", 0.5),
        ]
        assert [f.value for f in rank_facts(facts)] == [
            "family z", "work a", "work b",
        ]

    def test_missing_confidence_does_not_raise(self):
        fact = _fact("topics", "x", "no score", 0.5)
        fact.confidence = None
        assert rank_facts([fact])[0] is fact

    def test_input_list_is_not_mutated(self):
        facts = [_fact("a", "one", "low", 0.2), _fact("z", "two", "high", 0.9)]
        rank_facts(facts)
        assert [f.value for f in facts] == ["low", "high"]


class TestNeverContactedSentinel:
    """999 is a marker, not a measurement. It must never reach the output."""

    def test_lookup_states_no_contact_on_record(self, fake_person):
        fake_person.summary = _summary(NEVER_CONTACTED_DAYS, None)
        out = _lookup_person({"name": "Marigold Quill"})
        assert "no contact on record" in out

    def test_lookup_never_prints_the_sentinel(self, fake_person):
        fake_person.summary = _summary(NEVER_CONTACTED_DAYS, None)
        out = _lookup_person({"name": "Marigold Quill"})
        assert str(NEVER_CONTACTED_DAYS) not in out

    def test_never_contacted_carries_no_fault_language(self, fake_person):
        fake_person.summary = _summary(NEVER_CONTACTED_DAYS, None)
        out = _lookup_person({"name": "Marigold Quill"})
        _assert_no_fault_language(out, "never-contacted lookup")

    def test_real_gap_is_still_printed(self, fake_person):
        fake_person.summary = _summary(41, _days_ago(41))
        out = _lookup_person({"name": "Marigold Quill"})
        assert "**Days since contact**: 41" in out

    def test_a_genuine_999_day_gap_is_printed(self):
        """The date is the authority, not the magnitude.

        A real gap can equal the sentinel, so keying off the number alone would
        suppress a true 999-day answer — the same class of error inverted.
        """
        summary = _summary(NEVER_CONTACTED_DAYS, _days_ago(NEVER_CONTACTED_DAYS))
        out = format_relationship_context(summary)
        assert f"**Days since contact**: {NEVER_CONTACTED_DAYS}" in out
        assert "no contact on record" not in out

    def test_summary_without_a_date_reports_no_contact(self):
        out = format_relationship_context(_summary(NEVER_CONTACTED_DAYS, None))
        assert "no contact on record" in out

    def test_contact_on_record_tracks_the_date(self):
        assert _summary(NEVER_CONTACTED_DAYS, None).contact_on_record is False
        assert _summary(NEVER_CONTACTED_DAYS, _days_ago(999)).contact_on_record is True
        assert _summary(3, None).contact_on_record is True

    def test_indexed_profile_does_not_invent_a_recency_bucket(self):
        """The sentinel used to bucket into "over a year ago" — a fabricated
        interval written into the searchable corpus, where a later retrieval
        reads it as an established fact.
        """
        from api.services.person_entity import PersonEntity
        from api.services.person_indexer import generate_person_document

        person = PersonEntity(id="person-quill", canonical_name="Marigold Quill")
        doc = generate_person_document(person, _summary(NEVER_CONTACTED_DAYS, None))
        assert "over a year ago" not in doc
        assert "Last contact: none on record" in doc

    def test_indexed_profile_still_buckets_a_real_gap(self):
        from api.services.person_entity import PersonEntity
        from api.services.person_indexer import generate_person_document

        person = PersonEntity(id="person-quill", canonical_name="Marigold Quill")
        doc = generate_person_document(person, _summary(400, _days_ago(400)))
        assert "over a year ago" in doc


# ---------------------------------------------------------------------------
# person_info action="briefing"
# ---------------------------------------------------------------------------

@pytest.fixture
def briefing_env(monkeypatch):
    """A BriefingsService with every boundary faked, capturing the prompt.

    The interaction-store fake honours the real contract: an empty window comes
    back as NO_INTERACTIONS_PREFIX plus the window label, which is what the
    briefing keys its widening off. `oldest_days = None` means nothing on record.
    """
    state = SimpleNamespace(
        prompt=None,
        history_calls=[],
        oldest_days=3,
        raise_on_history=False,
        facts=[],
        notes=[],
        tasks=[],
        entity_email="marigold.quill@example.invalid",
    )

    class FakeInteractionStore:
        def format_interaction_history(self, person_id, days_back=None, limit=None):
            state.history_calls.append(
                {"person_id": person_id, "days_back": days_back, "limit": limit}
            )
            if state.raise_on_history:
                raise RuntimeError("interaction store unreachable")
            window = _FULL_WINDOW_DAYS if days_back is None else days_back
            if state.oldest_days is None or state.oldest_days > window:
                return f"{NO_INTERACTIONS_PREFIX} in {format_window_label(days_back)}._"
            return (
                f"**Summary:** 4 interactions | Last: {state.oldest_days} days ago\n"
                "\n### Recent Activity\n- 📧 Sep 02: Re: Community garden rota"
            )

    class FakeFactStore:
        def get_for_person(self, person_id, include_shared=True):
            return list(state.facts)

    class FakeHybridSearch:
        def search(self, query=None, top_k=None):
            return list(state.notes)

    class FakeTaskManager:
        def list_tasks(self, query=None, status=None):
            return list(state.tasks)

    class FakeIMessageStore:
        def get_messages_for_entity(self, entity_id, limit=None):
            return []

    class FakeResolver:
        def resolve(self, name=None, email=None):
            from api.services.person_entity import PersonEntity

            return SimpleNamespace(
                entity=PersonEntity(
                    id="person-quill",
                    canonical_name="Marigold Quill",
                    display_name="Marigold Quill",
                    emails=[state.entity_email] if state.entity_email else [],
                    company="Tidewater Press",
                    position="Editor",
                    category="work",
                    last_seen=_days_ago(150),
                    sources=["gmail"],
                )
            )

    class FakeSynthesizer:
        async def get_response(self, prompt, max_tokens=None):
            state.prompt = prompt
            return "## Marigold Quill — Briefing\n\nSynthesised."

    monkeypatch.setattr(person_facts_mod, "get_person_fact_store", lambda: FakeFactStore())
    monkeypatch.setattr(briefings, "get_synthesizer", lambda: FakeSynthesizer())

    state.service = BriefingsService(
        hybrid_search=FakeHybridSearch(),
        task_manager=FakeTaskManager(),
        entity_resolver=FakeResolver(),
        interaction_store=FakeInteractionStore(),
        imessage_store=FakeIMessageStore(),
    )
    return state


def _windows(state) -> list:
    return [c["days_back"] for c in state.history_calls]


class TestBriefingInteractionWidening:
    """A quiet quarter is not an absence of history."""

    async def test_interaction_older_than_the_default_window_is_found(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "Community garden rota" in briefing_env.prompt

    async def test_does_not_assert_that_no_history_exists(self, briefing_env):
        """The exact collapse this issue exists to remove."""
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "No interaction history available" not in briefing_env.prompt
        # The pre-fix 90-day miss reached the prompt as an unnamed window, which
        # is the same claim wearing different words.
        assert "the specified time period" not in briefing_env.prompt
        assert NO_INTERACTIONS_PREFIX not in briefing_env.prompt

    async def test_ladder_rungs_match_the_declared_ladder(self, briefing_env):
        briefing_env.oldest_days = None
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert _windows(briefing_env) == list(_INTERACTION_LADDER_DAYS)

    async def test_stops_at_the_first_rung_that_hits(self, briefing_env):
        briefing_env.oldest_days = 3
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert _windows(briefing_env) == [_INTERACTION_LADDER_DAYS[0]]

    async def test_widening_is_disclosed_on_a_hit(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "Nothing in the last 90 days" in briefing_env.prompt
        assert "widened to the last 365 days" in briefing_env.prompt

    async def test_no_widening_note_when_the_first_rung_hits(self, briefing_env):
        briefing_env.oldest_days = 3
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "widened to" not in briefing_env.prompt

    async def test_context_records_the_windows_tried(self, briefing_env):
        briefing_env.oldest_days = 150
        context = briefing_env.service.gather_context("Marigold Quill")
        assert context.interaction_windows_tried == [
            "the last 90 days", "the last 365 days",
        ]


class TestBriefingEmptyWindowStatesItsScope:
    async def test_empty_result_names_every_window_searched(self, briefing_env):
        briefing_env.oldest_days = None
        await briefing_env.service.generate_briefing("Marigold Quill")
        for days in _INTERACTION_LADDER_DAYS:
            assert format_window_label(days) in briefing_env.prompt

    async def test_empty_result_does_not_collapse_to_no_history(self, briefing_env):
        briefing_env.oldest_days = None
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "No interaction history available" not in briefing_env.prompt
        assert "the specified time period" not in briefing_env.prompt

    async def test_empty_window_carries_no_fault_language(self, briefing_env):
        briefing_env.oldest_days = None
        await briefing_env.service.generate_briefing("Marigold Quill")
        section = briefing_env.service._format_interaction_section(
            briefing_env.service.gather_context("Marigold Quill")
        )
        _assert_no_fault_language(section, "empty interaction window")

    async def test_unsearched_index_is_distinguished_from_an_empty_one(self, briefing_env):
        """No entity resolved means the index was never consulted at all."""
        context = briefing_env.service.gather_context("Marigold Quill")
        context.entity_id = None
        context.interaction_windows_tried = []
        context.interaction_history = ""
        section = briefing_env.service._format_interaction_section(context)
        assert "was not searched" in section
        _assert_no_fault_language(section, "unsearched interaction index")

    async def test_a_store_fault_is_stated_as_a_fault(self, briefing_env):
        """The one case that must NOT read as an absence."""
        briefing_env.raise_on_history = True
        context = briefing_env.service.gather_context("Marigold Quill")
        assert context.interaction_lookup_failed is True
        section = briefing_env.service._format_interaction_section(context)
        assert "could not be read" in section


class TestBriefingCallerSuppliedWindow:
    async def test_stated_window_is_honoured_exactly(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing(
            "Marigold Quill", interaction_days=30
        )
        assert _windows(briefing_env) == [30]

    async def test_stated_window_is_not_widened(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing(
            "Marigold Quill", interaction_days=30
        )
        assert "widened to" not in briefing_env.prompt

    async def test_empty_stated_window_names_that_window(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing(
            "Marigold Quill", interaction_days=30
        )
        assert "the last 30 days" in briefing_env.prompt

    async def test_empty_stated_window_does_not_claim_wider_ones(self, briefing_env):
        briefing_env.oldest_days = 150
        await briefing_env.service.generate_briefing(
            "Marigold Quill", interaction_days=30
        )
        assert "the last 365 days" not in briefing_env.prompt


class TestBriefingToolSchema:
    def _person_info(self) -> dict:
        return next(t for t in TOOL_DEFINITIONS if t["name"] == "person_info")

    def test_schema_advertises_the_window(self):
        props = self._person_info()["input_schema"]["properties"]
        assert "interaction_days" in props

    def test_window_is_an_integer_and_optional(self):
        tool = self._person_info()
        assert tool["input_schema"]["properties"]["interaction_days"]["type"] == "integer"
        assert "interaction_days" not in tool["input_schema"]["required"]

    def test_description_states_the_default_and_the_widening(self):
        desc = self._person_info()["description"]
        assert "90 days" in desc
        assert "widens" in desc

    def test_window_description_states_its_bounds(self):
        prop = self._person_info()["input_schema"]["properties"]["interaction_days"]
        assert f"1-{_BRIEFING_MAX_DAYS}" in prop["description"]


@pytest.fixture
def fake_briefing_service(monkeypatch):
    """Capture what the tool passes down to BriefingsService."""
    state = SimpleNamespace(calls=[], result={"status": "success", "briefing": "## Brief"})

    class FakeService:
        async def generate_briefing(self, person_name, email=None, interaction_days=None):
            state.calls.append(
                {
                    "person_name": person_name,
                    "email": email,
                    "interaction_days": interaction_days,
                }
            )
            return state.result

    monkeypatch.setattr(briefings, "get_briefings_service", lambda: FakeService())
    return state


class TestBriefingToolWindowArgument:
    """A window is a scope, so a bad one is dropped rather than clamped.

    Clamping would brief on a period nobody asked for; treating it as unstated
    falls back to the ladder, and the drop is disclosed either way.
    """

    async def test_omitted_window_reaches_the_service_as_unstated(
        self, fake_briefing_service
    ):
        await _briefing_person({"name": "Marigold Quill"})
        assert fake_briefing_service.calls[0]["interaction_days"] is None

    async def test_stated_window_is_passed_through(self, fake_briefing_service):
        await _briefing_person({"name": "Marigold Quill", "interaction_days": 45})
        assert fake_briefing_service.calls[0]["interaction_days"] == 45

    async def test_numeric_string_is_coerced(self, fake_briefing_service):
        await _briefing_person({"name": "Marigold Quill", "interaction_days": "45"})
        assert fake_briefing_service.calls[0]["interaction_days"] == 45

    @pytest.mark.parametrize(
        "bad", [0, -30, "soon", 10**9, _BRIEFING_MAX_DAYS + 1, None.__class__]
    )
    async def test_bad_window_is_treated_as_unstated(self, fake_briefing_service, bad):
        await _briefing_person({"name": "Marigold Quill", "interaction_days": bad})
        assert fake_briefing_service.calls[0]["interaction_days"] is None

    async def test_dropped_window_is_disclosed(self, fake_briefing_service):
        out = await _briefing_person({"name": "Marigold Quill", "interaction_days": -30})
        assert "Ignored interaction_days" in out
        assert "NOT scoped to it" in out

    async def test_valid_window_is_not_reported_as_ignored(self, fake_briefing_service):
        out = await _briefing_person({"name": "Marigold Quill", "interaction_days": 45})
        assert "Ignored interaction_days" not in out

    async def test_the_bound_itself_is_accepted(self, fake_briefing_service):
        await _briefing_person(
            {"name": "Marigold Quill", "interaction_days": _BRIEFING_MAX_DAYS}
        )
        assert fake_briefing_service.calls[0]["interaction_days"] == _BRIEFING_MAX_DAYS


class TestBriefingToolStatusHandling:
    """Thin data is an absence; only a real fault may be reported as one."""

    async def test_limited_data_is_not_reported_as_a_failure(
        self, fake_briefing_service
    ):
        fake_briefing_service.result = {
            "status": "limited",
            "message": "I have limited information about Marigold Quill.",
        }
        out = await _briefing_person({"name": "Marigold Quill"})
        _assert_no_fault_language(out, "limited briefing")

    async def test_limited_data_keeps_its_own_message(self, fake_briefing_service):
        fake_briefing_service.result = {
            "status": "limited",
            "message": "I have limited information about Marigold Quill.",
        }
        out = await _briefing_person({"name": "Marigold Quill"})
        assert "limited information about Marigold Quill" in out

    async def test_not_found_names_the_person_searched(self, fake_briefing_service):
        fake_briefing_service.result = {"status": "not_found", "message": ""}
        out = await _briefing_person({"name": "Thistlewaite Barrowman"})
        assert "'Thistlewaite Barrowman'" in out
        _assert_no_fault_language(out, "not-found briefing")

    async def test_a_real_fault_is_still_stated_plainly(self, fake_briefing_service):
        fake_briefing_service.result = {
            "status": "error",
            "message": "synthesiser timed out",
        }
        out = await _briefing_person({"name": "Marigold Quill"})
        assert "Briefing failed" in out


class TestBriefingLimitedStatus:
    """Interactions and facts count as data for the early bail-out.

    Without them a person with years of messages but no vault note got "I have
    limited information", which threw away the interaction history entirely.
    """

    async def test_interactions_alone_are_enough_to_brief(self, briefing_env):
        briefing_env.entity_email = None
        briefing_env.oldest_days = 3
        result = await briefing_env.service.generate_briefing("Marigold Quill")
        assert result["status"] == "success"

    async def test_facts_alone_are_enough_to_brief(self, briefing_env):
        briefing_env.entity_email = None
        briefing_env.oldest_days = None
        briefing_env.facts = [_fact("work", "role", "Runs the poetry imprint", 0.9)]
        result = await briefing_env.service.generate_briefing("Marigold Quill")
        assert result["status"] == "success"

    async def test_a_truly_empty_record_names_what_was_searched(self, briefing_env):
        briefing_env.entity_email = None
        briefing_env.oldest_days = None
        result = await briefing_env.service.generate_briefing("Marigold Quill")
        assert result["status"] == "limited"
        assert "the last 90 days" in result["message"]

    async def test_limited_message_carries_no_fault_language(self, briefing_env):
        briefing_env.entity_email = None
        briefing_env.oldest_days = None
        result = await briefing_env.service.generate_briefing("Marigold Quill")
        _assert_no_fault_language(result["message"], "limited briefing message")


class TestBriefingFactConfidenceFloor:
    """A floor that thins the set silently presents a partial set as complete."""

    def test_facts_below_the_floor_are_counted(self, briefing_env):
        briefing_env.facts = [
            _fact("work", "role", "Runs the poetry imprint", 0.9),
            _fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2),
        ]
        context = briefing_env.service.gather_context("Marigold Quill")
        assert context.facts_withheld_low_confidence == 1
        assert len(context.person_facts) == 1

    async def test_withheld_facts_are_disclosed(self, briefing_env):
        briefing_env.facts = [
            _fact("work", "role", "Runs the poetry imprint", 0.9),
            _fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2),
            _fact("topics", "rumour2", "Maybe changed jobs", 0.1),
        ]
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "2 further fact(s)" in briefing_env.prompt
        assert str(FACT_CONFIDENCE_FLOOR) in briefing_env.prompt

    async def test_withheld_facts_never_leak_their_content(self, briefing_env):
        briefing_env.facts = [
            _fact("work", "role", "Runs the poetry imprint", 0.9),
            _fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2),
        ]
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "Maybe moved to Lowmarsh" not in briefing_env.prompt

    async def test_no_disclosure_when_the_floor_drops_nothing(self, briefing_env):
        briefing_env.facts = [_fact("work", "role", "Runs the poetry imprint", 0.9)]
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "further fact(s)" not in briefing_env.prompt

    async def test_all_facts_withheld_is_not_reported_as_no_facts(self, briefing_env):
        briefing_env.facts = [
            _fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2),
            _fact("topics", "rumour2", "Maybe changed jobs", 0.1),
        ]
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "No facts extracted yet" not in briefing_env.prompt
        assert "2 lower-confidence fact(s)" in briefing_env.prompt

    async def test_no_facts_at_all_still_says_so(self, briefing_env):
        briefing_env.facts = []
        await briefing_env.service.generate_briefing("Marigold Quill")
        assert "No facts extracted yet" in briefing_env.prompt

    async def test_briefing_facts_are_ranked_by_confidence(self, briefing_env):
        briefing_env.facts = [
            _fact("work", "role", "Runs the poetry imprint", 0.65),
            _fact("family", "spouse_name", "Spouse is Rowan Quill", 0.95),
        ]
        context = briefing_env.service.gather_context("Marigold Quill")
        assert [f["value"] for f in context.person_facts] == [
            "Spouse is Rowan Quill", "Runs the poetry imprint",
        ]

    async def test_floor_disclosure_carries_no_fault_language(self, briefing_env):
        briefing_env.facts = [_fact("topics", "rumour", "Maybe moved to Lowmarsh", 0.2)]
        context = briefing_env.service.gather_context("Marigold Quill")
        section = briefing_env.service._format_facts_section(context)
        _assert_no_fault_language(section, "withheld-facts disclosure")


class TestInteractionStoreEmptyMessage:
    """The store's empty message is the contract the briefing widens on."""

    def test_empty_message_names_the_window(self):
        from api.services.interaction_store import InteractionStore

        store = InteractionStore.__new__(InteractionStore)
        store.get_for_person = lambda *a, **k: []
        store.get_interaction_counts = lambda *a, **k: {}
        store.get_last_interaction = lambda *a, **k: None
        out = store.format_interaction_history("person-quill", days_back=90)
        assert "the last 90 days" in out
        assert out.startswith(NO_INTERACTIONS_PREFIX)

    def test_empty_message_describes_an_unbounded_window(self):
        from api.services.interaction_store import InteractionStore

        store = InteractionStore.__new__(InteractionStore)
        store.get_for_person = lambda *a, **k: []
        store.get_interaction_counts = lambda *a, **k: {}
        store.get_last_interaction = lambda *a, **k: None
        out = store.format_interaction_history("person-quill")
        assert "the full history on record" in out

    def test_empty_message_carries_no_fault_language(self):
        from api.services.interaction_store import InteractionStore

        store = InteractionStore.__new__(InteractionStore)
        store.get_for_person = lambda *a, **k: []
        store.get_interaction_counts = lambda *a, **k: {}
        store.get_last_interaction = lambda *a, **k: None
        out = store.format_interaction_history("person-quill", days_back=90)
        _assert_no_fault_language(out, "empty interaction history")


class TestPersonSurfaceEmptiesCarryNoFaultLanguage:
    """Blanket ban, no exceptions, across every empty path on this surface."""

    def test_lookup_miss(self, fake_person):
        fake_person.resolved = False
        _assert_no_fault_language(
            _lookup_person({"name": "Thistlewaite Barrowman"}), "lookup miss"
        )

    def test_lookup_with_no_facts_and_no_contact(self, fake_person):
        fake_person.facts = []
        fake_person.summary = _summary(NEVER_CONTACTED_DAYS, None)
        _assert_no_fault_language(
            _lookup_person({"name": "Marigold Quill"}), "empty lookup"
        )

    def test_lookup_with_truncated_facts(self, fake_person):
        fake_person.facts = [
            _fact("topics", f"note_{i:02d}", f"Discussed subject {i}", 0.5)
            for i in range(_PERSON_FACT_LIMIT + 4)
        ]
        _assert_no_fault_language(
            _lookup_person({"name": "Marigold Quill"}), "truncated lookup"
        )

    async def test_briefing_with_an_empty_interaction_index(self, briefing_env):
        briefing_env.oldest_days = None
        briefing_env.facts = []
        context = briefing_env.service.gather_context("Marigold Quill")
        _assert_no_fault_language(
            briefing_env.service._format_interaction_section(context),
            "empty briefing interactions",
        )
        _assert_no_fault_language(
            briefing_env.service._format_facts_section(context), "empty briefing facts"
        )

    async def test_briefing_tool_on_a_thin_record(self, fake_briefing_service):
        fake_briefing_service.result = {"status": "limited", "message": ""}
        _assert_no_fault_language(
            await _briefing_person({"name": "Marigold Quill"}), "thin briefing"
        )

    def test_tool_handler_is_still_registered(self):
        """The tool must remain reachable — a rename here breaks the surface."""
        assert at._TOOL_HANDLERS["person_info"] is at._tool_person_info
