"""
Who a chat message is about, and how sure the system prompt may be.

Runs the real resolver, store, override and summary modules against a throwaway
database. Every person here is synthetic.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.services.conversation_context import (
    ConversationContext,
    expand_followup_with_context,
    extract_context_from_history,
)
from api.services.link_override import LinkOverride
from api.services.person_context import (
    CONFIDENT,
    FROM_CONVERSATION,
    LIKELY,
    NONE,
    PERSON_CONTEXT_TIMEOUT_SECONDS,
    PROFILE_FACT_LIMIT,
    UNCERTAIN,
    build_person_context,
    describe_unconfirmed,
    gather_person_context,
    resolve_person_mention,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def crm(tmp_path, monkeypatch):
    """Point the real CRM stores at a throwaway database; nothing is stubbed out."""
    from api.services import entity_resolver, interaction_store, person_entity, person_facts
    from api.services.entity_resolver import EntityResolver
    from api.services.interaction_store import InteractionStore
    from api.services.link_override import LinkOverrideStore
    from api.services.person_entity import PersonEntityStore
    from api.services.person_facts import PersonFactStore

    store = PersonEntityStore(str(tmp_path / "crm.db"))
    overrides = LinkOverrideStore(tmp_path / "crm.db")
    monkeypatch.setattr(entity_resolver, "get_link_override_store", lambda: overrides)
    monkeypatch.setattr(person_entity, "_entity_store", store)
    monkeypatch.setattr(entity_resolver, "_entity_resolver", EntityResolver(store))
    monkeypatch.setattr(interaction_store, "_interaction_store", InteractionStore(str(tmp_path / "interactions.db")))
    monkeypatch.setattr(person_facts, "_fact_store", PersonFactStore(str(tmp_path / "crm.db")))
    store.overrides = overrides
    return store


def add_person(store, name, strength=30.0, aliases=None, company=None, hidden=False, days_ago=2, messages=0):
    from api.services.person_entity import PersonEntity

    person = PersonEntity(canonical_name=name, aliases=aliases or [], company=company)
    person.relationship_strength = strength
    person.hidden = hidden
    person.message_count = messages
    stored = store.add(person)
    conn = store._get_connection()
    conn.execute(
        "UPDATE person_entities SET last_seen = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(), stored.id),
    )
    conn.commit()
    conn.close()
    store.save()
    return stored


def add_fact(person, value="Allergic to synthetic peanuts"):
    from api.services.person_facts import PersonFact, get_person_fact_store

    get_person_fact_store().upsert(PersonFact(
        person_id=person.id, category="health", key="allergy", value=value, confidence=0.9,
    ))


def add_override(store, name, person, source_type=None):
    store.overrides.add(LinkOverride(
        id=None, name_pattern=name, source_type=source_type, context_pattern=None,
        preferred_person_id=person.id, rejected_person_id=None, reason="synthetic",
    ))


def message(role, content, routing=None):
    return SimpleNamespace(role=role, content=content, routing=routing, created_at=None)


class TestFullNamesAndAliasesStayConfident:
    def test_full_name_is_confident_despite_a_namesake(self, crm):
        wanted = add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Robin Beta", 90.0)

        resolution = resolve_person_mention("Robin Alpha")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == wanted.id

    def test_exact_alias_is_confident(self, crm):
        wanted = add_person(crm, "Robin Alpha", 5.0, aliases=["Birdie"])

        resolution = resolve_person_mention("Birdie")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == wanted.id

    def test_my_prefix_reaches_the_same_alias(self, crm):
        wanted = add_person(crm, "Parent Example", 80.0, aliases=["Mom"])

        assert resolve_person_mention("my mom").person.id == wanted.id
        assert resolve_person_mention("Mom's").person.id == wanted.id

    def test_a_people_dictionary_alias_counts_as_the_full_name(self, crm, monkeypatch):
        from api.services import people

        wanted = add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Birdie Beta", 90.0)
        monkeypatch.setitem(people.ALIAS_MAP, "birdie", "Robin Alpha")

        resolution = resolve_person_mention("Birdie")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == wanted.id

    def test_a_distant_namesake_does_not_unseat_an_alias(self, crm):
        holder = add_person(crm, "Robin Alpha", 50.0, aliases=["Birdie"])
        add_person(crm, "Birdie Beta", 13.9)

        resolution = resolve_person_mention("Birdie")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == holder.id

    def test_a_hidden_person_is_not_identified_by_their_alias(self, crm):
        add_person(crm, "Robin Secret", 50.0, aliases=["Birdie"], hidden=True)

        assert resolve_person_mention("Birdie").tier == NONE

    def test_alias_shared_with_a_close_person_is_no_longer_certain(self, crm):
        add_person(crm, "Robin Alpha", 50.0, aliases=["Birdie"])
        add_person(crm, "Birdie Beta", 45.0)

        assert resolve_person_mention("Birdie").tier == UNCERTAIN

    def test_an_alias_that_is_just_their_own_first_name_settles_nothing(self, crm):
        person = add_person(crm, "Austin Alpha", 50.0, aliases=["Austin"])

        resolution = resolve_person_mention("Austin")

        assert resolution.tier == LIKELY
        assert resolution.person.id == person.id


class TestLinkOverrides:
    def test_override_resolves_a_word_nobody_else_answers_to(self, crm):
        parent = add_person(crm, "Parent Example", 80.0)
        add_override(crm, "Dad", parent)

        resolution = resolve_person_mention("Dad")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == parent.id

    def test_override_beats_a_one_word_row_of_the_same_name(self, crm):
        parent = add_person(crm, "Parent Example", 80.0)
        add_person(crm, "Mom", 0.0)
        add_override(crm, "Mom", parent)

        assert resolve_person_mention("Mom").person.id == parent.id

    def test_override_settles_a_first_name_two_people_share(self, crm):
        chosen = add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Robin Beta", 90.0)
        add_override(crm, "Robin", chosen)

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == chosen.id

    def test_a_source_scoped_override_identifies_a_word_nobody_else_answers_to(self, crm):
        parent = add_person(crm, "Parent Example", 80.0)
        add_override(crm, "Dad", parent, source_type="imessage")

        resolution = resolve_person_mention("Dad")

        assert resolution.tier == LIKELY
        assert resolution.person.id == parent.id

    def test_a_source_scoped_override_does_not_settle_a_shared_first_name_in_chat(self, crm):
        chosen = add_person(crm, "Robin Alpha", 60.0)
        rival = add_person(crm, "Robin Beta", 55.0)
        add_override(crm, "Robin", chosen, source_type="imessage")

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == UNCERTAIN
        assert {p.id for p in resolution.candidates} == {chosen.id, rival.id}

    def test_override_to_a_hidden_person_identifies_no_one(self, crm):
        parent = add_person(crm, "Parent Example", 80.0, hidden=True)
        add_override(crm, "Dad", parent)

        assert resolve_person_mention("Dad").tier == NONE


class TestFirstNames:
    def test_two_close_people_with_one_first_name_are_uncertain(self, crm):
        first = add_person(crm, "Robin Alpha", 60.0)
        second = add_person(crm, "Robin Beta", 55.0)

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == UNCERTAIN
        assert resolution.person is None
        assert [p.id for p in resolution.candidates] == [first.id, second.id]

    def test_candidates_are_ordered_by_closeness_not_by_when_they_were_added(self, crm):
        second = add_person(crm, "Robin Beta", 55.0)
        first = add_person(crm, "Robin Alpha", 60.0)

        assert [p.id for p in resolve_person_mention("Robin").candidates] == [first.id, second.id]

    def test_the_likely_one_is_the_closest_whenever_they_were_added(self, crm):
        add_person(crm, "Robin Beta", 20.0)
        close = add_person(crm, "Robin Alpha", 90.0)

        assert resolve_person_mention("Robin").person.id == close.id

    def test_a_clearly_closer_person_is_only_the_likely_one(self, crm):
        close = add_person(crm, "Robin Alpha", 90.0)
        other = add_person(crm, "Robin Beta", 20.0)

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == LIKELY
        assert resolution.person.id == close.id
        assert other.id in [p.id for p in resolution.candidates]

    @pytest.mark.parametrize(("top", "second", "tier"), [(50.0, 20.0, LIKELY), (50.0, 20.1, UNCERTAIN)])
    def test_the_closeness_lead_decides_between_likely_and_uncertain(self, crm, top, second, tier):
        add_person(crm, "Robin Alpha", top)
        add_person(crm, "Robin Beta", second)

        assert resolve_person_mention("Robin").tier == tier

    def test_a_close_person_ahead_of_a_distant_namesake_is_only_likely(self, crm):
        close = add_person(crm, "Robin Alpha", 14.0)
        add_person(crm, "Robin Beta", 13.9)

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == LIKELY
        assert resolution.person.id == close.id

    def test_the_only_person_with_a_first_name_is_still_only_likely(self, crm):
        person = add_person(crm, "Quillon Zzyx", 90.0)

        resolution = resolve_person_mention("Quillon")

        assert resolution.tier == LIKELY
        assert resolution.person.id == person.id

    def test_the_only_person_with_a_first_name_who_is_distant_is_only_likely(self, crm):
        add_person(crm, "Quillon Zzyx", 13.9)

        assert resolve_person_mention("Quillon").tier == LIKELY

    def test_a_one_word_row_is_never_asserted_even_when_exact(self, crm):
        row = add_person(crm, "Quillon", 40.0)

        resolution = resolve_person_mention("Quillon")

        assert resolution.tier == UNCERTAIN
        assert resolution.person is None
        assert [p.id for p in resolution.candidates] == [row.id]

    def test_a_distant_one_word_row_does_not_stand_in_for_a_real_person(self, crm):
        real = add_person(crm, "Quillon Zzyx", 40.0)
        add_person(crm, "Quillon", 0.0)

        resolution = resolve_person_mention("Quillon")

        assert resolution.tier == LIKELY
        assert [p.id for p in resolution.candidates] == [real.id]

    def test_a_distant_one_word_row_alone_identifies_no_one(self, crm):
        add_person(crm, "Quillon", 13.9)

        assert resolve_person_mention("Quillon").tier == NONE

    def test_a_tie_between_distant_people_is_uncertain_not_settled(self, crm):
        add_person(crm, "Robin Alpha", 5.0)
        add_person(crm, "Robin Beta", 5.0)

        assert resolve_person_mention("Robin").tier == UNCERTAIN

    def test_an_unknown_name_is_none_and_creates_no_one(self, crm):
        add_person(crm, "Robin Alpha", 30.0)

        assert resolve_person_mention("Quillon Zzyx").tier == NONE
        assert resolve_person_mention("Quillon").tier == NONE
        assert crm.count() == 1


class TestScoredTies:
    def test_an_initial_that_fits_two_close_people_is_uncertain(self, crm):
        add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Riley Alpha", 30.0)

        assert resolve_person_mention("R Alpha").tier == UNCERTAIN

    def test_an_initial_with_one_close_person_is_only_likely(self, crm):
        close = add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Riley Alpha", 0.0)

        resolution = resolve_person_mention("R Alpha")

        assert resolution.tier == LIKELY
        assert resolution.person.id == close.id


class TestHiddenAndMerged:
    def test_a_hidden_full_name_identifies_no_one(self, crm):
        add_person(crm, "Robin Alpha", 60.0, hidden=True)

        assert resolve_person_mention("Robin Alpha").tier == NONE

    def test_a_hidden_person_is_never_a_candidate(self, crm):
        visible = add_person(crm, "Robin Alpha", 30.0)
        hidden = add_person(crm, "Robin Beta", 90.0, hidden=True)

        resolution = resolve_person_mention("Robin")

        assert resolution.person.id == visible.id
        assert hidden.id not in [p.id for p in resolution.candidates]

    def test_a_merged_name_resolves_to_the_survivor(self, crm):
        from api.services.person_entity import PersonEntity

        survivor = add_person(crm, "Robin Alpha", 60.0)
        merged = crm.add(PersonEntity(canonical_name="Robyn Alphabet", aliases=[]))
        crm._merged_ids[merged.id] = survivor.id
        crm._bump_get_all_cache_generation()

        resolution = resolve_person_mention("Robyn Alphabet")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == survivor.id

    def test_a_merged_row_is_never_a_candidate(self, crm):
        from api.services.person_entity import PersonEntity

        survivor = add_person(crm, "Robin Alpha", 60.0)
        merged = PersonEntity(canonical_name="Robin Beta", aliases=[])
        merged.relationship_strength = 90.0
        merged = crm.add(merged)
        crm._merged_ids[merged.id] = survivor.id
        crm._bump_get_all_cache_generation()

        resolution = resolve_person_mention("Robin")

        assert merged.id not in [p.id for p in resolution.candidates]
        assert resolution.person.id == survivor.id


class TestConversationSettlesAFirstName:
    def test_a_full_name_earlier_in_the_conversation_settles_it(self, crm):
        wanted = add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        history = [message("user", "When did I last see Robin Beta?"), message("assistant", "Two days ago.")]

        resolution = resolve_person_mention("Robin", history)

        assert resolution.tier == FROM_CONVERSATION
        assert resolution.person.id == wanted.id

    def test_the_person_a_previous_turn_resolved_settles_it(self, crm):
        wanted = add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        history = [
            message("user", "Who is that?"),
            message("assistant", "They run the choir.", routing={"person": "Robin Beta", "person_id": wanted.id}),
        ]

        resolution = resolve_person_mention("Robin", history)

        assert resolution.tier == FROM_CONVERSATION
        assert resolution.person.id == wanted.id

    def test_two_names_in_the_conversation_settle_nothing(self, crm):
        add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        history = [message("assistant", "Did you mean Robin Alpha or Robin Beta?")]

        assert resolve_person_mention("Robin", history).tier == UNCERTAIN

    def test_a_name_older_than_the_lookback_does_not_count(self, crm):
        add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        history = [message("user", "Robin Beta called")] + [message("user", "ok")] * 6

        assert resolve_person_mention("Robin", history).tier == UNCERTAIN

    def test_a_name_inside_a_longer_name_does_not_count(self, crm):
        add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        history = [message("user", "Robin Betancourt called")]

        assert resolve_person_mention("Robin", history).tier == UNCERTAIN


class TestBlockContents:
    def test_a_confident_block_asserts_the_person_and_carries_facts(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0, company="Tidewater Press")
        add_fact(person)

        context = build_person_context("Who is Robin Alpha?")

        assert context.tier == CONFIDENT
        assert "'Robin Alpha' is Robin Alpha." in context.block
        assert person.id in context.block
        assert "Tidewater Press" in context.block
        assert "Allergic to synthetic peanuts" in context.block
        assert "not confirmed" not in context.block

    def test_an_uncertain_block_names_everyone_and_asserts_no_one(self, crm):
        first = add_person(crm, "Robin Alpha", 60.0, company="Tidewater Press")
        second = add_person(crm, "Robin Beta", 55.0, company="Lantern Works")
        add_fact(first, "First Robin's private fact")
        add_fact(second, "Second Robin's private fact")

        context = build_person_context("tell me about Robin")

        assert context.tier == UNCERTAIN
        assert "not confirmed" in context.block
        assert "does not identify one person" in context.block
        assert "Tidewater Press" in context.block and "Lantern Works" in context.block
        assert first.id in context.block and second.id in context.block
        assert "'Robin' is" not in context.block
        assert "private fact" not in context.block

    def test_a_likely_block_names_the_assumption_without_facts(self, crm):
        close = add_person(crm, "Robin Alpha", 90.0)
        add_person(crm, "Robin Beta", 20.0)
        add_fact(close, "Close Robin's private fact")

        context = build_person_context("tell me about Robin")

        assert context.tier == LIKELY
        assert "not confirmed" in context.block
        assert "Assuming you mean Robin Alpha" in context.block
        assert "Robin Beta" in context.block
        assert "private fact" not in context.block

    def test_an_unidentified_person_is_not_carried_forward(self, crm):
        add_person(crm, "Robin Alpha", 90.0)
        add_person(crm, "Robin Beta", 20.0)

        fields = build_person_context("tell me about Robin").routing_fields()

        assert fields["person_tier"] == LIKELY
        assert "person" not in fields and "person_id" not in fields

    def test_an_identified_person_is_carried_forward(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0)

        fields = build_person_context("Who is Robin Alpha?").routing_fields()

        assert fields["person"] == "Robin Alpha"
        assert fields["person_id"] == person.id

    def test_a_conversation_identified_block_says_where_it_came_from(self, crm):
        wanted = add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)
        add_fact(wanted, "Beta's fact")

        context = build_person_context(
            "tell me about Robin", [message("user", "Robin Beta called me")]
        )

        assert context.tier == FROM_CONVERSATION
        assert "from the recent conversation" in context.block
        assert "Beta's fact" in context.block

    def test_facts_in_the_block_are_capped_and_say_so(self, crm):
        from api.services.person_facts import PersonFact, get_person_fact_store

        person = add_person(crm, "Robin Alpha", 60.0)
        for i in range(PROFILE_FACT_LIMIT + 3):
            get_person_fact_store().upsert(PersonFact(
                person_id=person.id, category="topics", key=f"topic_{i}", value=f"Topic {i}", confidence=0.8,
            ))

        block = build_person_context("Who is Robin Alpha?").block

        assert block.count("- topics: ") == PROFILE_FACT_LIMIT
        assert f"{PROFILE_FACT_LIMIT} of {PROFILE_FACT_LIMIT + 3} shown" in block

    def test_candidates_are_capped_and_the_rest_are_counted(self, crm):
        for suffix in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"):
            add_person(crm, f"Robin {suffix}", 30.0)

        block = describe_unconfirmed(resolve_person_mention("Robin"))

        assert block.count("(entity_id:") == 5
        assert "and 2 less close people" in block

    def test_a_block_never_holds_a_hidden_namesake(self, crm):
        add_person(crm, "Robin Alpha", 30.0)
        add_person(crm, "Robin Secret", 90.0, hidden=True)

        assert "Secret" not in build_person_context("tell me about Robin").block


class TestFetchDepth:
    def test_a_request_for_the_full_picture_widens_the_search(self, crm):
        add_person(crm, "Robin Alpha", 3.0)

        context = build_person_context("catch me up on Robin Alpha")

        assert context.fetch_depth == "deep"
        assert "search every channel" in context.block

    def test_a_quick_lookup_is_answered_from_the_profile_first(self, crm):
        add_person(crm, "Robin Alpha", 90.0)

        context = build_person_context("what is the phone number for Robin Alpha")

        assert context.fetch_depth == "shallow"
        assert "quick lookup" in context.block

    def test_closeness_alone_does_not_widen_the_search(self, crm):
        add_person(crm, "Robin Alpha", 95.0)

        context = build_person_context("who is Robin Alpha")

        assert context.fetch_depth == "normal"
        assert "search every channel" not in context.block
        assert "quick lookup" not in context.block

    def test_an_unconfirmed_person_gets_no_depth_guidance(self, crm):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        block = build_person_context("catch me up on Robin").block

        assert "search every channel" not in block


class TestFindingTheNameInAMessage:
    @pytest.mark.parametrize("question", [
        "Who is Robin Alpha?",
        "what did Robin Alpha say about the budget",
        "Did Robin Alpha reply?",
        "Robin Alpha called me",
        "Robin Alpha's birthday?",
        "tell me about the choir and Robin Alpha",
    ])
    def test_a_full_name_is_found_in_any_phrasing(self, crm, question):
        person = add_person(crm, "Robin Alpha", 60.0)

        context = build_person_context(question)

        assert context.person_id == person.id

    @pytest.mark.parametrize("question", [
        "What's on my calendar on Tuesday?",
        "Will it rain tomorrow?",
        "summarize the Q3 report",
        "who is the president",
        "tell me about it",
        "How do I reset my password?",
    ])
    def test_a_message_about_no_one_gets_no_block(self, crm, question):
        add_person(crm, "Robin Alpha", 60.0, aliases=["Will"])
        add_person(crm, "Shea Example", 60.0)

        assert build_person_context(question) is None

    def test_a_first_word_is_not_read_as_a_name_unless_possessive(self, crm):
        add_person(crm, "Parent Example", 80.0, aliases=["Mom"])

        assert build_person_context("Mom called") is None
        assert build_person_context("Mom's birthday?").tier == CONFIDENT

    def test_a_capitalized_word_mid_sentence_needs_someone_close(self, crm):
        add_person(crm, "Acme Support", 2.0, messages=5)

        assert build_person_context("Did Acme charge me twice?") is None

    def test_a_capitalized_word_mid_sentence_needs_someone_in_touch(self, crm):
        add_person(crm, "Acme Support", 40.0, messages=0)

        assert build_person_context("Did Acme charge me twice?") is None

    def test_two_senders_sharing_a_word_mid_sentence_are_not_raised(self, crm):
        add_person(crm, "Acme Support", 40.0, messages=0)
        add_person(crm, "Acme Billing", 35.0, messages=0)

        assert build_person_context("Did Acme charge me twice?") is None

    def test_a_pronoun_captured_as_a_name_is_not_read_as_one(self, crm):
        add_person(crm, "Shea Example", 60.0, messages=5)

        assert build_person_context("who is she") is None

    def test_a_first_name_mid_sentence_raises_someone_in_touch(self, crm):
        person = add_person(crm, "Robin Alpha", 40.0, messages=5)
        add_person(crm, "Robin Beta", 35.0, messages=0)

        context = build_person_context("what did Robin say about the budget")

        assert context.tier == UNCERTAIN
        assert person.id in context.block

    def test_a_lowercase_capture_is_held_to_the_same_bar(self, crm):
        add_person(crm, "Flight Deals", 40.0, messages=0)
        reachable = add_person(crm, "Robin Alpha", 40.0, messages=5)

        assert build_person_context("who was on the call with flight") is None
        context = build_person_context("what did i discuss with robin")
        assert context.tier == LIKELY
        assert reachable.id in context.block

    def test_my_plus_a_word_only_reaches_an_alias_or_override(self, crm):
        add_person(crm, "Emil Alpha", 80.0, messages=5)

        assert build_person_context("what's in my email today") is None
        assert build_person_context("what's in my emil today") is None

    def test_my_plus_a_word_reaches_an_override(self, crm):
        parent = add_person(crm, "Parent Example", 80.0)
        add_override(crm, "dad", parent)

        assert build_person_context("what did my dad say").person_id == parent.id

    @pytest.mark.parametrize("name", ["José Example", "Mary-Anne O'Example", "Anna Maria van Example", "Robin Q. Alpha"])
    @pytest.mark.parametrize("template", [
        "prep me for my meeting with {}",
        "tell me about {}",
        "what did {} say about the budget",
    ])
    def test_a_longer_or_accented_full_name_is_read_whole(self, crm, name, template):
        person = add_person(crm, name, 40.0)
        add_person(crm, " ".join(name.split()[:2]) + " Other", 90.0)

        assert build_person_context(template.format(name)).person_id == person.id

    def test_a_full_stop_ends_a_name(self, crm):
        person = add_person(crm, "Robin Alpha", 40.0)

        assert build_person_context("I saw Robin Alpha. Quillon called").person_id == person.id

    def test_an_anchored_name_is_raised_even_for_someone_distant(self, crm):
        add_person(crm, "Quillon Zzyx", 2.0)

        assert build_person_context("who is Quillon").tier == LIKELY

    def test_a_relation_reaches_its_alias(self, crm):
        parent = add_person(crm, "Parent Example", 80.0, aliases=["Mom"])

        assert build_person_context("what did my mom say about the trip").person_id == parent.id

    def test_a_later_name_is_found_when_an_earlier_phrase_matches_no_one(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0)

        assert build_person_context("meeting with the team and Robin Alpha").person_id == person.id


class TestFollowUps:
    def test_the_person_a_turn_resolved_is_read_back(self):
        history = [message("assistant", "ok", routing={"person": "Robin Alpha", "person_id": "id-1"})]

        assert extract_context_from_history(history).last_person_name == "Robin Alpha"

    def test_a_pronoun_is_replaced_as_a_whole_word_only(self):
        context = ConversationContext(
            last_person_name="Robin Alpha",
            last_query_time=datetime.now(timezone.utc),
        )

        assert expand_followup_with_context("what's the plan", context) == "what's the plan"
        assert expand_followup_with_context("what did he say", context) == "what did Robin Alpha say"


class TestDegradation:
    async def test_a_failing_resolver_yields_no_context_and_logs_no_name(self, crm, caplog):
        from api.services import entity_resolver

        class Broken:
            def match_name(self, name):
                raise RuntimeError(f"cannot read {name}")

        with patch.object(entity_resolver, "_entity_resolver", Broken()):
            result = await gather_person_context("Who is Quillon Zzyx?")

        assert result is None
        assert "RuntimeError" in caplog.text
        assert "Quillon" not in caplog.text

    async def test_a_slow_lookup_is_abandoned_within_the_budget(self, crm):
        import time

        import api.services.person_context as module

        def slow(*args):
            time.sleep(PERSON_CONTEXT_TIMEOUT_SECONDS + 0.5)
            return "too late"

        loop = asyncio.get_running_loop()
        with patch.object(module, "build_person_context", slow):
            started = loop.time()
            result = await gather_person_context("Who is Robin Alpha?")
            elapsed = loop.time() - started

        assert result is None
        assert elapsed < PERSON_CONTEXT_TIMEOUT_SECONDS + 0.3

    async def test_a_slow_lookup_leaves_the_event_loop_free(self, crm):
        import time

        import api.services.person_context as module

        def slow(*args):
            time.sleep(0.2)

        ticks = []

        async def tick():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(tick())
        with patch.object(module, "build_person_context", slow):
            await gather_person_context("Who is Robin Alpha?")
        ticker.cancel()

        assert len(ticks) >= 5

    async def test_cancelling_the_turn_is_not_swallowed(self, crm):
        import time

        import api.services.person_context as module

        with patch.object(module, "build_person_context", lambda *a: time.sleep(0.3)):
            task = asyncio.create_task(gather_person_context("Who is Robin Alpha?"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    def test_resolving_logs_no_name(self, crm, caplog):
        add_person(crm, "Robin Alpha", 60.0)

        with caplog.at_level(logging.DEBUG, logger="api.services.person_context"):
            build_person_context("Who is Robin Alpha?")

        assert "Robin" not in caplog.text


class TestScopedOverrides:
    """Chat has no note path and is not a source, so a scoped rule can only put someone forward."""

    def _add_scoped(self, store, name, person, source_type=None, context_pattern=None):
        store.overrides.add(LinkOverride(
            id=None, name_pattern=name, source_type=source_type, context_pattern=context_pattern,
            preferred_person_id=person.id, rejected_person_id=None, reason="synthetic",
        ))

    @pytest.mark.parametrize("scope", [
        {"context_pattern": "Work/ML/"},
        {"source_type": "vault"},
        {"source_type": "imessage", "context_pattern": "Family/"},
    ])
    def test_it_does_not_assert_with_no_rival_in_sight(self, crm, scope):
        person = add_person(crm, "Robin Workmate", 60.0)
        add_fact(person, "Workmate's private fact")
        self._add_scoped(crm, "Robin", person, **scope)

        context = build_person_context("Tell me about Robin")

        assert context.tier == LIKELY
        assert context.person_id is None
        assert "private fact" not in context.block

    def test_it_does_not_assert_a_word_nobody_else_answers_to(self, crm):
        person = add_person(crm, "Parent Example", 60.0)
        self._add_scoped(crm, "Pops", person, context_pattern="Family/")

        resolution = resolve_person_mention("Pops")

        assert resolution.tier == LIKELY
        assert resolution.person.id == person.id

    def test_it_does_not_assert_a_full_name_either(self, crm):
        person = add_person(crm, "Robin Workmate", 60.0)
        self._add_scoped(crm, "Rob Unlisted", person, context_pattern="Work/ML/")

        resolution = resolve_person_mention("Rob Unlisted")

        assert resolution.tier == LIKELY
        assert resolution.person.id == person.id

    def test_an_unscoped_override_on_a_full_name_still_asserts(self, crm):
        person = add_person(crm, "Robin Workmate", 60.0)
        self._add_scoped(crm, "Rob Unlisted", person)

        assert resolve_person_mention("Rob Unlisted").tier == CONFIDENT

    def test_an_unscoped_rule_wins_when_both_exist(self, crm):
        scoped = add_person(crm, "Robin Workmate", 60.0)
        unscoped = add_person(crm, "Robin Homebody", 30.0)
        self._add_scoped(crm, "Robin", scoped, context_pattern="Work/ML/")
        self._add_scoped(crm, "Robin", unscoped)

        resolution = resolve_person_mention("Robin")

        assert resolution.tier == CONFIDENT
        assert resolution.person.id == unscoped.id

    def test_it_puts_forward_someone_the_name_would_not_otherwise_find(self, crm):
        person = add_person(crm, "Parent Example", 60.0)
        self._add_scoped(crm, "Pops", person, source_type="imessage")

        context = build_person_context("what did my pops say")

        assert context.tier == LIKELY
        assert person.id in context.block

    def test_a_hidden_person_is_not_put_forward(self, crm):
        person = add_person(crm, "Parent Example", 60.0, hidden=True)
        self._add_scoped(crm, "Pops", person, source_type="imessage")

        assert resolve_person_mention("Pops").tier == NONE


class TestTwoPeopleInOneConversation:
    @pytest.mark.parametrize("build_history", [
        lambda a, b: [message("user", "Tell me about Robin Alpha"), message("user", "and Robin Beta")],
        lambda a, b: [
            message("user", "Who is that?"),
            message("assistant", "ok", routing={"person": "Robin Alpha", "person_id": a.id}),
            message("user", "Who is the other one?"),
            message("assistant", "ok", routing={"person": "Robin Beta", "person_id": b.id}),
        ],
        lambda a, b: [
            message("user", "Robin Alpha called"),
            message("assistant", "ok", routing={"person": "Robin Beta", "person_id": b.id}),
        ],
    ])
    @pytest.mark.parametrize("question", ["Is Robin free Friday?", "Text Robin that I'm late", "tell me about Robin"])
    def test_neither_is_settled_and_both_are_offered(self, crm, build_history, question):
        a = add_person(crm, "Robin Alpha", 40.0, messages=5)
        b = add_person(crm, "Robin Beta", 40.0, messages=5)
        add_fact(a, "Alpha's private fact")
        add_fact(b, "Beta's private fact")

        context = build_person_context(question, build_history(a, b))

        assert context.tier == UNCERTAIN
        assert context.person_id is None
        assert a.id in context.block and b.id in context.block
        assert "private fact" not in context.block

    def test_a_lead_in_closeness_does_not_decide_between_them(self, crm):
        add_person(crm, "Robin Alpha", 90.0)
        add_person(crm, "Robin Beta", 20.0)
        history = [message("user", "Robin Alpha and Robin Beta both wrote")]

        assert resolve_person_mention("Robin", history).tier == UNCERTAIN

    def test_both_stay_listed_however_many_are_closer(self, crm):
        for suffix in ("Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota"):
            add_person(crm, f"Robin {suffix}", 80.0)
        a = add_person(crm, "Robin Alpha", 15.0)
        b = add_person(crm, "Robin Beta", 14.0)
        history = [message("user", "Robin Alpha and Robin Beta both wrote")]

        block = describe_unconfirmed(resolve_person_mention("Robin", history))

        assert a.id in block and b.id in block

    def test_a_turn_that_named_no_candidate_does_not_count(self, crm):
        wanted = add_person(crm, "Robin Alpha", 40.0)
        add_person(crm, "Robin Beta", 40.0)
        other = add_person(crm, "Casey Beta", 40.0)
        history = [
            message("user", "Robin Alpha called"),
            message("assistant", "ok", routing={"person": "Casey Beta", "person_id": other.id}),
        ]

        assert resolve_person_mention("Robin", history).person.id == wanted.id


ORDINARY_QUESTIONS = [
    "What's the weather in Austin",
    "Open the Chase account",
    "Is it raining in Paris today?",
    "Open my Chase statement",
    "Did the Target order ship?",
    "How far is Dallas from Houston?",
    "Book a table at Madison for two",
    "What time is it in Sydney?",
    "Any delays on Delta this week?",
    "Is the Apple store open?",
    "Show me the Jordan itinerary",
    "How is traffic near Charlotte?",
    "What's the capital of Georgia",
    "Remind me about the Victoria trip",
    "Pay the Amazon bill",
    "Was there snow around Denver?",
]
PLACE_AND_COMPANY_CONTACTS = [
    "Austin", "Chase", "Paris", "Target", "Dallas", "Houston", "Madison", "Sydney", "Delta",
    "Apple", "Jordan", "Charlotte", "Georgia", "Victoria", "Amazon", "Denver",
]
CAPITALIZED_NON_PERSON_QUESTIONS = [
    "Summarize the Q3 Report",
    "What does Obsidian do with attachments?",
    "Is Slack down?",
    "How do I use the Claude Code CLI?",
    "Search Drive for the Tax Return",
    "When does Daylight Saving end?",
    "What did I decide about New York?",
    "San Antonio weather this week",
    "New Jersey tolls",
    "Find the Google Doc about pricing",
    "Did the Deploy finish on Friday?",
    "Remind me Monday about the Board Meeting",
    "Will it rain on Saturday?",
    "Can Gemma run offline?",
    "Why is Chroma slow?",
    "Add milk to my Shopping List",
]


class TestPlacesAndCompanies:
    @pytest.fixture
    def contacts(self, crm):
        people = []
        for first in PLACE_AND_COMPANY_CONTACTS:
            person = add_person(crm, f"{first} Alpha", 70.0, messages=20)
            add_fact(person, f"{first}'s private fact")
            people.append(person)
        return people

    @pytest.mark.parametrize("question", ORDINARY_QUESTIONS)
    def test_a_lone_word_is_never_asserted_with_facts(self, crm, contacts, question):
        context = build_person_context(question)

        if context:
            assert context.person_id is None
            assert "private fact" not in context.block
            assert "routing" not in context.routing_fields()
            assert "person" not in context.routing_fields()

    @pytest.mark.parametrize("question", [
        "What's the weather in Austin",
        "Open the Chase account",
        "Is it raining in Paris today?",
        "Open my Chase statement",
        "Did the Target order ship?",
        "What time is it in Sydney?",
        "Any delays on Delta this week?",
        "What's the capital of Georgia",
        "Was there snow around Denver?",
    ])
    def test_a_place_or_thing_lead_raises_nothing(self, crm, contacts, question):
        assert build_person_context(question) is None

    def test_a_possessive_after_a_lead_is_still_a_person(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0, messages=5)

        assert person.id in build_person_context("what's in Robin's latest email").block

    def test_a_person_mentioned_earlier_is_still_settled_by_their_first_name(self, crm):
        person = add_person(crm, "Austin Alpha", 60.0, messages=5)
        add_fact(person, "Austin's private fact")

        context = build_person_context(
            "Did Austin reply?", [message("user", "When did I last see Austin Alpha?")]
        )

        assert context.tier == FROM_CONVERSATION
        assert "private fact" in context.block

    def test_a_deliberate_alias_is_asserted_mid_sentence(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0, aliases=["Birdie"])

        assert build_person_context("did Birdie call back").person_id == person.id

    def test_an_unscoped_override_is_asserted_mid_sentence(self, crm):
        person = add_person(crm, "Parent Example", 60.0)
        add_override(crm, "Pops", person)

        assert build_person_context("did Pops call back").person_id == person.id

    def test_a_people_dictionary_name_is_raised_mid_sentence(self, crm, monkeypatch):
        from api.services import people

        person = add_person(crm, "Robin Alpha", 30.0)
        monkeypatch.setitem(people.ALIAS_MAP, "birdie", "Robin Alpha")

        assert build_person_context("did Birdie call back").person_id == person.id


class TestOrdinaryQuestionsRaiseNothing:
    @pytest.mark.parametrize("question", CAPITALIZED_NON_PERSON_QUESTIONS)
    def test_capitalized_words_alone_do_not_make_a_person(self, crm, question):
        add_person(crm, "Robin Alpha", 60.0, messages=5)
        add_person(crm, "Slack Support", 40.0)
        add_person(crm, "Google Alerts", 40.0)
        add_person(crm, "Antonio Beta", 60.0, messages=5)
        add_person(crm, "Jersey Gamma", 60.0, messages=5)
        add_person(crm, "Claude Delta", 40.0)

        assert build_person_context(question) is None


class TestSentenceInitialVerbs:
    @pytest.mark.parametrize("verb", ["Call", "Text", "Ask", "Meet", "Email", "Ping", "Message", "Invite", "Thank", "Congratulate"])
    @pytest.mark.parametrize("tail", ["", " tomorrow", " for lunch", " about the budget"])
    def test_a_full_name_after_a_verb_is_identified(self, crm, verb, tail):
        person = add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 90.0)

        assert build_person_context(f"{verb} Robin Alpha{tail}").person_id == person.id

    @pytest.mark.parametrize("verb", ["Call", "Text", "Ask", "Meet", "Ping", "Invite"])
    def test_a_first_name_after_a_verb_is_raised_without_asserting(self, crm, verb):
        person = add_person(crm, "Casey Alpha", 60.0, messages=5)

        context = build_person_context(f"{verb} Casey")

        assert context.tier == LIKELY
        assert person.id in context.block

    def test_a_later_sentence_counts_too(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0)

        assert build_person_context("I'm running late. Call Robin Alpha").person_id == person.id

    def test_a_full_name_opening_the_sentence_wins_over_dropping_its_first_word(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Alpha Beta", 90.0, messages=5)

        assert build_person_context("Robin Alpha called").person_id == person.id

    @pytest.mark.parametrize("question", ["New Jersey", "San Antonio?", "Los Angeles for a week"])
    def test_a_place_name_keeps_its_second_word(self, crm, question):
        add_person(crm, "Jersey Alpha", 60.0, messages=5)
        add_person(crm, "Antonio Alpha", 60.0, messages=5)
        add_person(crm, "Angeles Alpha", 60.0, messages=5)

        assert build_person_context(question) is None


class TestTheNameBudget:
    @pytest.mark.parametrize("question", [
        "Summarize my email, my calendar and my tasks, and tell me whether Robin Alpha replied",
        "Check my inbox, my notes, my reminders, my drive and my texts for Robin Alpha",
    ])
    def test_my_phrases_do_not_use_it_up(self, crm, question):
        person = add_person(crm, "Robin Alpha", 60.0)

        assert build_person_context(question).person_id == person.id

    def test_my_phrases_with_nothing_behind_them_cost_no_scan(self, crm):
        from api.services import entity_resolver

        add_person(crm, "Robin Alpha", 60.0)
        resolver = entity_resolver._entity_resolver
        with patch.object(resolver, "candidates_for_name", side_effect=AssertionError("scored")):
            assert build_person_context("what's in my email and my calendar") is None

    def test_it_still_bounds_the_names_tried(self, crm):
        add_person(crm, "Robin Alpha", 60.0)

        assert build_person_context("did Xavier or Yolanda or Zelda or Quillon or Robin Alpha call") is None


class TestRemainingSignals:
    def _add_override(self, store, name, person_id, source_type=None):
        store.overrides.add(LinkOverride(
            id=None, name_pattern=name, source_type=source_type, context_pattern=None,
            preferred_person_id=person_id, rejected_person_id=None, reason="synthetic",
        ))

    def test_a_scoped_override_to_someone_deleted_is_ignored(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0)
        self._add_override(crm, "Robin", "deleted-id", source_type="vault")

        resolution = resolve_person_mention("Robin")

        assert [p.id for p in resolution.candidates] == [person.id]

    def test_a_scoped_override_stays_listed_beside_an_alias_and_its_rival(self, crm):
        add_person(crm, "Robin Alpha", 50.0, aliases=["Birdie"])
        add_person(crm, "Birdie Beta", 45.0)
        put_forward = add_person(crm, "Casey Gamma", 5.0)
        self._add_override(crm, "Birdie", put_forward.id, source_type="vault")

        resolution = resolve_person_mention("Birdie")

        assert put_forward.id in [p.id for p in resolution.candidates]

    def test_an_alias_on_a_one_word_row_is_not_asserted(self, crm):
        row = add_person(crm, "Quillon", 40.0, aliases=["Birdie"])

        resolution = resolve_person_mention("Birdie")

        assert resolution.tier != CONFIDENT
        assert resolution.person is None or resolution.person.id != row.id

    def test_a_people_dictionary_name_answers_my_relation(self, crm, monkeypatch):
        from api.services import people

        person = add_person(crm, "Parent Example", 30.0)
        monkeypatch.setitem(people.ALIAS_MAP, "pops", "Parent Example")

        assert build_person_context("what did my pops say").person_id == person.id

    def test_conversation_settles_a_contact_with_no_messages_mid_sentence(self, crm):
        person = add_person(crm, "Robin Alpha", 60.0, messages=0)

        context = build_person_context(
            "did Robin reply", [message("user", "When did I last see Robin Alpha?")]
        )

        assert context.person_id == person.id

    def test_a_scoped_override_raises_a_contact_with_no_messages_mid_sentence(self, crm):
        person = add_person(crm, "Parent Example", 60.0, messages=0)
        self._add_override(crm, "Pops", person.id, source_type="imessage")

        context = build_person_context("did Pops call back")

        assert context.tier == LIKELY
        assert person.id in context.block

    def test_my_plus_a_one_word_row_costs_no_scan(self, crm):
        from api.services import entity_resolver

        add_person(crm, "Phone", 40.0)
        resolver = entity_resolver._entity_resolver
        with patch.object(resolver, "candidates_for_name", side_effect=AssertionError("scored")):
            assert build_person_context("what's on my phone") is None


class TestTwoCapitalizedWordsAreNotAVerbAndAName:
    @pytest.mark.parametrize("question", [
        "Nancy Drew is a classic series",
        "Nancy Drew books",
        "Nancy Drew?",
        "Nancy Drew",
        "Michael Drew's stats",
        "Lemon Drew tea",
        "Lantern Drew is lovely",
        "Lantern Drew's cooking",
        "Lantern Drew's",
        "Lantern Drew's?",
    ])
    def test_a_conversation_does_not_turn_the_last_word_into_its_person(self, crm, question):
        person = add_person(crm, "Drew Alpha", 60.0, messages=5)
        add_fact(person, "Drew's private fact")
        history = [message("user", "Who is Drew Alpha?"), message("assistant", "A colleague.")]

        assert build_person_context(question, history) is None

    @pytest.mark.parametrize("question", [
        "Michael Jordan's stats",
        "Lemon Sage tea",
        "Lemon Sage is lovely",
        "Nancy Sage",
        "Rosemary Sage butter",
        "Jack Jordan shoes",
    ])
    def test_nor_does_it_raise_a_contact_with_that_first_name(self, crm, question):
        add_person(crm, "Jordan Alpha", 60.0, messages=5)
        add_person(crm, "Sage Beta", 60.0, messages=5)

        assert build_person_context(question) is None

    @pytest.mark.parametrize("question", [
        "Call Casey",
        "Text Casey",
        "Ask Casey.",
        "Ping Casey?",
        "Call Casey back",
        "Ask Casey about the budget",
        "Text Casey that I'm late",
        "Text Casey I'm late",
        "Email Casey the doc",
        "Meet Casey for lunch",
        "Ring Casey tomorrow",
        "Thank Casey, please",
    ])
    def test_an_imperative_still_reaches_a_first_name(self, crm, question):
        person = add_person(crm, "Casey Alpha", 60.0, messages=5)

        assert person.id in build_person_context(question).block

    @pytest.mark.parametrize("question", ["Call Robin Alpha", "Text Robin Alpha budget numbers", "Meet Robin Alpha's team"])
    def test_an_imperative_still_reaches_a_full_name(self, crm, question):
        person = add_person(crm, "Robin Alpha", 60.0)

        assert build_person_context(question).person_id == person.id

    def test_an_imperative_still_reaches_an_alias(self, crm):
        parent = add_person(crm, "Parent Example", 60.0, aliases=["Mom"])

        assert build_person_context("Ask Mom").person_id == parent.id

    def test_a_known_first_name_does_not_give_up_a_full_name_in_front_of_it(self, crm):
        add_person(crm, "Anne Alpha", 60.0)

        assert build_person_context("Mary Anne Alpha called") is None
