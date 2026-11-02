"""Context-aware resolution of first-name mentions in vault notes."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.services import mention_resolver as mr
from api.services.interaction_store import Interaction, InteractionStore
from api.services.link_override import LinkOverride
from api.services.mention_resolver import (
    MentionResolver,
    NoteContext,
    build_note_context,
    is_ambiguous_mention,
    note_domain,
)
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.relationship import Relationship, RelationshipStore
from api.services.source_entity import SourceEntity, SourceEntityStore
from config import relationship_weights as w
from config.employment_history import EmploymentHistory, load_employment_history, parse_employment_history

pytestmark = pytest.mark.unit

NOTE_DAY = date(2024, 6, 15)
EMPLOYMENT = parse_employment_history({
    "employers": [
        {
            "name": "Sample Labs",
            "email_domains": ["samplelabs.example"],
            "start": "2023-01",
            "end": None,
            "vault_folders": ["Work/Sample/"],
        },
        {
            "name": "Example Analytics",
            "email_domains": ["example-analytics.example"],
            "start": "2019-01",
            "end": "2022-12",
            "vault_folders": ["Archive/Analytics/"],
        },
    ]
})


class World:
    """A CRM on disk that the resolver reads from."""

    def __init__(self, tmp_path: Path):
        self.crm_db = tmp_path / "crm.db"
        self.interaction_db = tmp_path / "interactions.db"
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.people = PersonEntityStore(str(self.crm_db))
        self.sources = SourceEntityStore(str(self.crm_db))
        self.relationships = RelationshipStore(str(self.crm_db))
        self.interactions = InteractionStore(str(self.interaction_db))

    def person(self, name, **fields) -> PersonEntity:
        return self.people.add(PersonEntity(canonical_name=name, **fields))

    def contact(self, person, day=NOTE_DAY, source="gmail", account=None, title="t", attendees=None):
        self.interactions.add(Interaction(
            id=f"i{self.interactions.count()}",
            person_id=person.id,
            timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
            source_type=source,
            title=title,
            source_id=f"{source}-{self.interactions.count()}",
            source_account=account,
            attendee_count=attendees,
        ))

    def resolver(self, employment=EMPLOYMENT, overrides=(), my_person_id=None, owner_name=None):
        return MentionResolver(
            self.people.get_all(),
            crm_db=self.crm_db,
            interaction_db=self.interaction_db,
            vault_root=self.vault,
            overrides=overrides,
            employment=employment,
            my_person_id=my_person_id,
            owner_name=owner_name,
        )


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def context(rel_path="Other/note.md", **fields):
    return NoteContext(rel_path=rel_path, **fields)


def signals_for(decision, person):
    return next(c.signals for c in decision.candidates if c.entity.id == person.id)


class TestAmbiguity:
    @pytest.mark.parametrize("name,expected", [
        ("Robin", True),
        ("Robin G", True),
        ("Robin G.", True),
        ("R Gamma", True),
        ("Robin Gamma", False),
        ("Robin Q Gamma", False),
    ])
    def test_ambiguous_forms(self, name, expected):
        assert is_ambiguous_mention(name) is expected


class TestCandidates:
    def test_bare_row_is_never_a_target_however_strong_its_context(self, world):
        bare = world.person("Robin")
        world.contact(bare)
        decision = world.resolver().resolve(
            "Robin", context(text="robin", note_date=NOTE_DAY, co_mentioned_ids=frozenset({bare.id}))
        )
        assert decision.entity is None
        assert decision.reason == mr.REASON_NO_CANDIDATE
        assert decision.candidates == []

    def test_real_person_is_chosen_over_a_bare_row_of_the_same_name(self, world):
        world.person("Robin")
        real = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin", context(text="robin gamma spoke"))
        assert decision.entity.id == real.id

    def test_hidden_people_are_not_given_to_the_resolver_and_owner_is_never_chosen(self, world):
        owner = world.person("Robin Owner")
        world.person("Robin Gamma")
        decision = world.resolver(my_person_id=owner.id).resolve("Robin", context(text="robin gamma"))
        assert decision.entity is None
        assert decision.reason == mr.REASON_OWNER

    def test_owner_name_setting_also_stands_for_the_owner(self, world):
        world.person("Robin Gamma")
        decision = world.resolver(owner_name="Robin").resolve("Robin", context(text="robin gamma"))
        assert decision.reason == mr.REASON_OWNER

    def test_non_person_row_is_not_a_candidate(self, world):
        world.person("Example Billing Team", emails=["billing@example.com"])
        assert world.resolver().resolve("Example", context()).reason == mr.REASON_NO_CANDIDATE

    def test_one_word_row_with_only_an_email_is_not_a_candidate(self, world):
        world.person("Quillon", emails=["quillon@example.com"])
        assert world.resolver().resolve("Quillon", context()).reason == mr.REASON_NO_CANDIDATE

    def test_one_word_contact_with_a_phone_is_a_candidate(self, world):
        person = world.person("Quillon", phone_numbers=["+12125550100"])
        decision = world.resolver().resolve("Quillon", context())
        assert decision.candidates[0].entity.id == person.id

    def test_nickname_counts_only_when_nobody_goes_by_the_name_given(self, world):
        michael = world.person("Michael Gamma")
        decision = world.resolver().resolve("Mike", context())
        assert signals_for(decision, michael)["name_nickname"] == w.MENTION_WEIGHT_NICKNAME

        world.person("Mike Delta")
        decision = world.resolver().resolve("Mike", context())
        assert michael.id not in [c.entity.id for c in decision.candidates]

    def test_initial_narrows_to_matching_last_name(self, world):
        world.person("Robin Alpha")
        gamma = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin G", context())
        assert [c.entity.id for c in decision.candidates] == [gamma.id]
        assert signals_for(decision, gamma)["name_initial"] == w.MENTION_WEIGHT_INITIAL

    def test_first_initial_with_last_name(self, world):
        world.person("Sam Gamma")
        robin = world.person("Robin Gamma")
        decision = world.resolver().resolve("R Gamma", context())
        assert [c.entity.id for c in decision.candidates] == [robin.id]

    def test_alias_earns_points(self, world):
        person = world.person("Robin Gamma", aliases=["Rob"])
        decision = world.resolver().resolve("Rob", context())
        assert signals_for(decision, person)["name_alias"] == w.MENTION_WEIGHT_ALIAS


class TestDecision:
    def test_unique_name_with_no_context_does_not_link(self, world):
        person = world.person("Quillon Example")
        decision = world.resolver().resolve("Quillon", context())
        assert decision.entity is None
        assert decision.reason == mr.REASON_BELOW_MINIMUM
        assert signals_for(decision, person)["only_candidate"] == w.MENTION_WEIGHT_ONLY_CANDIDATE

    def test_minimum_score_is_inclusive(self, world, monkeypatch):
        person = world.person("Quillon Example")
        reachable = w.MENTION_WEIGHT_FIRST_NAME + w.MENTION_WEIGHT_ONLY_CANDIDATE
        monkeypatch.setattr(mr, "MENTION_MIN_SCORE", reachable)
        assert world.resolver().resolve("Quillon", context()).entity.id == person.id
        monkeypatch.setattr(mr, "MENTION_MIN_SCORE", reachable + 1)
        assert world.resolver().resolve("Quillon", context()).entity is None

    def test_minimum_lead_is_inclusive(self, world, monkeypatch):
        winner = world.person("Robin Gamma")
        world.person("Robin Delta")
        monkeypatch.setattr(mr, "MENTION_MIN_SCORE", 1)
        note = context(text="robin gamma")
        monkeypatch.setattr(mr, "MENTION_MIN_LEAD", w.MENTION_WEIGHT_FULL_NAME_IN_NOTE)
        assert world.resolver().resolve("Robin", note).entity.id == winner.id
        monkeypatch.setattr(mr, "MENTION_MIN_LEAD", w.MENTION_WEIGHT_FULL_NAME_IN_NOTE + 1)
        decision = world.resolver().resolve("Robin", note)
        assert decision.entity is None
        assert decision.reason == mr.REASON_NARROW_LEAD

    def test_equally_strong_candidates_link_to_no_one(self, world):
        world.person("Robin Gamma")
        world.person("Robin Delta")
        decision = world.resolver().resolve("Robin", context(text="robin gamma and robin delta"))
        assert decision.entity is None
        assert decision.reason == mr.REASON_NARROW_LEAD

    def test_decision_is_deterministic(self, world):
        world.person("Robin Gamma")
        world.person("Robin Delta")
        resolver = world.resolver()
        first = resolver.resolve("Robin", context(text="robin gamma"))
        second = resolver.resolve("Robin", context(text="robin gamma"))
        assert [c.entity.id for c in first.candidates] == [c.entity.id for c in second.candidates]
        assert first.score == second.score

    def test_confidence_scales_with_score_and_is_capped(self, world):
        person = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin", context(text="robin gamma"))
        assert decision.entity.id == person.id
        assert decision.confidence == pytest.approx(decision.score / 100)
        assert decision.match_type == mr.MENTION_MATCH_TYPE


class TestOverrides:
    def override(self, preferred, rejected=None, context_pattern=None):
        return LinkOverride(
            id="o1", name_pattern="Robin", source_type=None, context_pattern=context_pattern,
            preferred_person_id=preferred, rejected_person_id=rejected, reason=None,
        )

    def test_override_wins_without_scoring(self, world):
        preferred = world.person("Robin Gamma")
        world.person("Robin Delta")
        decision = world.resolver(overrides=[self.override(preferred.id)]).resolve("Robin", context())
        assert decision.entity.id == preferred.id
        assert decision.match_type == mr.OVERRIDE_MATCH_TYPE

    def test_override_pointing_at_a_bare_row_links_to_no_one(self, world):
        bare = world.person("Robin")
        world.person("Robin Gamma")
        decision = world.resolver(overrides=[self.override(bare.id)]).resolve("Robin", context(text="robin gamma"))
        assert decision.entity is None

    def test_rejected_person_is_not_a_candidate(self, world):
        rejected = world.person("Robin Gamma")
        decision = world.resolver(overrides=[self.override("gone", rejected.id)]).resolve(
            "Robin", context(text="robin gamma")
        )
        assert decision.entity is None
        assert decision.candidates == [] or rejected.id not in [c.entity.id for c in decision.candidates]

    def test_override_scoped_to_another_folder_does_not_apply(self, world):
        preferred = world.person("Robin Gamma")
        decision = world.resolver(
            overrides=[self.override(preferred.id, context_pattern="Work/Other/")]
        ).resolve("Robin", context("Personal/note.md"))
        assert decision.entity is None


class TestContextSignals:
    def test_full_name_in_note(self, world):
        person = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin", context(text="spoke with robin gamma today"))
        assert signals_for(decision, person)["full_name_in_note"] == w.MENTION_WEIGHT_FULL_NAME_IN_NOTE

    def test_full_name_must_be_a_whole_phrase(self, world):
        person = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin", context(text="robin gammaray"))
        assert "full_name_in_note" not in signals_for(decision, person)

    def test_email_address_in_note(self, world):
        person = world.person("Robin Gamma", emails=["rg@example.com"])
        decision = world.resolver().resolve("Robin", context(text="cc rg@example.com"))
        assert signals_for(decision, person)["full_name_in_note"] == w.MENTION_WEIGHT_FULL_NAME_IN_NOTE

    def test_person_named_path(self, world):
        person = world.person("Robin Gamma")
        decision = world.resolver().resolve("Robin", context("People/Robin Gamma/one on one.md"))
        assert signals_for(decision, person)["person_path"] == w.MENTION_WEIGHT_PERSON_PATH

    def test_folder_affinity_counts_full_name_links_only(self, world):
        full = world.person("Robin Gamma")
        first_only = world.person("Robin Delta")
        for i in range(w.MENTION_FOLDER_MIN_LINKS):
            for person, observed in ((full, "Robin Gamma"), (first_only, "Robin")):
                world.sources.add(SourceEntity(
                    source_type="vault",
                    source_id=f"{world.vault}/Work/Team/n{i}.md:{observed}",
                    observed_name=observed,
                    canonical_person_id=person.id,
                ), validate_person=False)
        decision = world.resolver().resolve("Robin", context("Work/Team/new.md"))
        assert signals_for(decision, full)["folder_links"] == w.MENTION_WEIGHT_FOLDER_LINKS
        assert "folder_links" not in signals_for(decision, first_only)

    def test_folder_affinity_needs_enough_links(self, world):
        person = world.person("Robin Gamma")
        world.sources.add(SourceEntity(
            source_type="vault", source_id=f"{world.vault}/Work/Team/n.md:Robin Gamma",
            observed_name="Robin Gamma", canonical_person_id=person.id,
        ), validate_person=False)
        decision = world.resolver().resolve("Robin", context("Work/Team/new.md"))
        assert "folder_links" not in signals_for(decision, person)

    def test_domain_match_and_mismatch_in_a_work_note(self, world):
        colleague = world.person("Robin Gamma", category="work")
        friend = world.person("Robin Delta")
        world.contact(friend, day=date(2010, 1, 1), source="imessage")
        decision = world.resolver().resolve("Robin", context(domain="work"))
        assert signals_for(decision, colleague)["domain_match"] == w.MENTION_WEIGHT_DOMAIN_MATCH
        assert signals_for(decision, friend)["domain_mismatch"] == w.MENTION_PENALTY_DOMAIN_MISMATCH

    def test_domain_signals_in_a_personal_note(self, world):
        colleague = world.person("Robin Gamma", category="work")
        friend = world.person("Robin Delta")
        relative = world.person("Robin Epsilon", category="family")
        world.contact(friend, day=date(2010, 1, 1), source="whatsapp")
        decision = world.resolver().resolve("Robin", context(domain="personal"))
        assert signals_for(decision, colleague)["domain_mismatch"] == w.MENTION_PENALTY_DOMAIN_MISMATCH
        assert signals_for(decision, friend)["domain_match"] == w.MENTION_WEIGHT_DOMAIN_MATCH
        assert signals_for(decision, relative)["family_in_personal"] == w.MENTION_WEIGHT_FAMILY_IN_PERSONAL

    def test_no_domain_signal_when_the_note_has_no_domain(self, world):
        colleague = world.person("Robin Gamma", category="work")
        decision = world.resolver().resolve("Robin", context())
        assert "domain_match" not in signals_for(decision, colleague)

    def test_work_account_contact_is_work_evidence(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, day=date(2010, 1, 1), source="gmail", account="work")
        decision = world.resolver().resolve("Robin", context(domain="work"))
        assert signals_for(decision, person)["domain_match"] == w.MENTION_WEIGHT_DOMAIN_MATCH

    def test_dictionary_category(self, world, monkeypatch):
        person = world.person("Robin Gamma", category="family")
        monkeypatch.setattr(mr, "get_person_category", lambda name: "family")
        decision = world.resolver().resolve("Robin", context())
        assert signals_for(decision, person)["dictionary_category"] == w.MENTION_WEIGHT_DICTIONARY_CATEGORY


class TestEmployerSignals:
    SAMPLE = EMPLOYMENT.employers[0]
    ANALYTICS = EMPLOYMENT.employers[1]

    def test_email_at_the_employer(self, world):
        person = world.person("Robin Gamma", emails=["robin@samplelabs.example"])
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert signals_for(decision, person)["employer_email"] == w.MENTION_WEIGHT_EMPLOYER_EMAIL

    def test_email_at_a_different_employer_counts_against(self, world):
        person = world.person("Robin Gamma", emails=["robin@example-analytics.example"])
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert signals_for(decision, person)["other_employer"] == w.MENTION_PENALTY_OTHER_EMPLOYER

    def test_work_account_history_during_the_tenure(self, world):
        person = world.person("Robin Gamma")
        for _ in range(w.MENTION_EMPLOYER_HISTORY_MIN_INTERACTIONS):
            world.contact(person, day=date(2023, 6, 1), account="work")
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert signals_for(decision, person)["employer_history"] == w.MENTION_WEIGHT_EMPLOYER_HISTORY

    def test_history_outside_the_tenure_does_not_count(self, world):
        person = world.person("Robin Gamma")
        for _ in range(w.MENTION_EMPLOYER_HISTORY_MIN_INTERACTIONS):
            world.contact(person, day=date(2020, 6, 1), account="work")
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert "employer_history" not in signals_for(decision, person)

    def test_personal_account_history_does_not_count(self, world):
        person = world.person("Robin Gamma")
        for _ in range(w.MENTION_EMPLOYER_HISTORY_MIN_INTERACTIONS):
            world.contact(person, day=date(2023, 6, 1), account="personal")
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert "employer_history" not in signals_for(decision, person)

    def test_company_field(self, world):
        person = world.person("Robin Gamma", company="Sample Labs Inc")
        decision = world.resolver().resolve("Robin", context(employers=(self.SAMPLE,)))
        assert signals_for(decision, person)["employer_company"] == w.MENTION_WEIGHT_EMPLOYER_COMPANY

    def test_note_date_picks_the_employer_of_that_date(self, world):
        then_colleague = world.person("Robin Gamma", emails=["robin@example-analytics.example"])
        world.person("Robin Delta", emails=["robin@samplelabs.example"])
        note = build_note_context(
            "Work/Misc/note.md", {}, "", note_date=date(2020, 5, 1), tag_domain=None, employment=EMPLOYMENT,
        )
        decision = world.resolver().resolve("Robin", note)
        assert signals_for(decision, then_colleague)["employer_email"] == w.MENTION_WEIGHT_EMPLOYER_EMAIL

    def test_employer_folder_wins_over_the_date(self):
        note = build_note_context(
            "Archive/Analytics/note.md", {}, "", note_date=date(2024, 5, 1), tag_domain=None, employment=EMPLOYMENT,
        )
        assert note.employers == (self.ANALYTICS,)

    def test_personal_note_has_no_employer(self):
        note = build_note_context(
            "Personal/note.md", {}, "", note_date=date(2024, 5, 1), tag_domain=None, employment=EMPLOYMENT,
        )
        assert note.employers == ()

    def test_no_employment_history_degrades_to_no_employer_signal(self, world):
        person = world.person("Robin Gamma", emails=["robin@samplelabs.example"])
        world.contact(person)
        empty = EmploymentHistory()
        note = build_note_context(
            "Work/Sample/note.md", {}, "", note_date=NOTE_DAY, tag_domain=None, employment=empty,
        )
        assert note.employers == ()
        decision = world.resolver(employment=empty).resolve("Robin", note)
        signals = signals_for(decision, person)
        assert not {"employer_email", "employer_history", "employer_company", "other_employer"} & set(signals)
        assert signals["interaction_near"] > 0


class TestInteractionSignals:
    @pytest.mark.parametrize("offset,expected", [
        (0, 25), (7, 25), (8, 15), (30, 15), (31, 8), (90, 8),
    ])
    def test_closest_contact_sets_the_tier(self, world, offset, expected):
        person = world.person("Robin Gamma")
        world.contact(person, day=NOTE_DAY - timedelta(days=offset))
        decision = world.resolver().resolve("Robin", context(note_date=NOTE_DAY))
        assert signals_for(decision, person)["interaction_near"] == expected

    def test_contact_after_the_note_counts_too(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, day=NOTE_DAY + timedelta(days=3))
        decision = world.resolver().resolve("Robin", context(note_date=NOTE_DAY))
        assert signals_for(decision, person)["interaction_near"] == 25

    def test_contact_outside_every_window_earns_nothing(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, day=NOTE_DAY - timedelta(days=91))
        decision = world.resolver().resolve("Robin", context(note_date=NOTE_DAY))
        assert "interaction_near" not in signals_for(decision, person)

    def test_note_mentions_are_not_contact(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, source="vault")
        decision = world.resolver().resolve("Robin", context(note_date=NOTE_DAY))
        assert "interaction_near" not in signals_for(decision, person)

    def test_undated_note_earns_no_interaction_signal(self, world):
        person = world.person("Robin Gamma")
        world.contact(person)
        decision = world.resolver().resolve("Robin", context())
        assert "interaction_near" not in signals_for(decision, person)

    def _meeting(self, **fields):
        return context(note_date=NOTE_DAY, is_meeting=True, title="Budget review 2024-06-15", **fields)

    def test_attendee_of_the_meeting_the_note_records(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, source="calendar", title="Budget review", attendees=4)
        decision = world.resolver().resolve("Robin", self._meeting())
        assert signals_for(decision, person)["meeting_same_day"] == w.MENTION_WEIGHT_MEETING_SAME_DAY

    def test_a_different_meeting_that_day_is_not_this_one(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, source="calendar", title="Hiring sync", attendees=4)
        decision = world.resolver().resolve("Robin", self._meeting())
        assert "meeting_same_day" not in signals_for(decision, person)

    def test_a_large_event_is_not_evidence(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, source="calendar", title="Budget review",
                      attendees=w.MENTION_MEETING_MAX_ATTENDEES + 1)
        decision = world.resolver().resolve("Robin", self._meeting())
        assert "meeting_same_day" not in signals_for(decision, person)

    def test_a_note_that_is_no_meeting_earns_no_attendee_signal(self, world):
        person = world.person("Robin Gamma")
        world.contact(person, source="calendar", title="Budget review", attendees=4)
        decision = world.resolver().resolve(
            "Robin", context(note_date=NOTE_DAY, title="Budget review")
        )
        assert "meeting_same_day" not in signals_for(decision, person)


class TestCoMentionSignal:
    def _relate(self, world, one, other, **counts):
        low, high = sorted((one.id, other.id))
        world.relationships.add(Relationship(person_a_id=low, person_b_id=high, **counts))

    def test_weak_and_strong_pairs(self, world):
        target = world.person("Robin Gamma")
        strong = world.person("Quillon Example")
        weak = world.person("Tamsin Example")
        self._relate(
            world, target, strong, shared_events_count=200, shared_messages_count=100,
            last_seen_together=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )
        self._relate(world, target, weak, shared_events_count=1)

        decision = world.resolver().resolve(
            "Robin", context(co_mentioned_ids=frozenset({strong.id}))
        )
        assert signals_for(decision, target)["co_mention"] == w.MENTION_WEIGHT_COMENTION_STRONG

        decision = world.resolver().resolve(
            "Robin", context(co_mentioned_ids=frozenset({weak.id}))
        )
        assert signals_for(decision, target)["co_mention"] == w.MENTION_WEIGHT_COMENTION_WEAK

    def test_co_mentions_are_capped(self, world):
        target = world.person("Robin Gamma")
        others = [world.person(f"Quillon Example{letter}") for letter in "abcdefgh"]
        for other in others:
            self._relate(
                world, target, other, shared_events_count=200, shared_messages_count=100,
                last_seen_together=datetime(2024, 6, 1, tzinfo=timezone.utc),
            )
        decision = world.resolver().resolve(
            "Robin", context(co_mentioned_ids=frozenset(o.id for o in others))
        )
        assert signals_for(decision, target)["co_mention"] == w.MENTION_COMENTION_CAP

    def test_no_relationship_earns_nothing(self, world):
        target = world.person("Robin Gamma")
        stranger = world.person("Quillon Example")
        decision = world.resolver().resolve(
            "Robin", context(co_mentioned_ids=frozenset({stranger.id}))
        )
        assert "co_mention" not in signals_for(decision, target)


class TestNoteContext:
    @pytest.mark.parametrize("rel,tag,expected", [
        ("Work/Sample/note.md", None, "work"),
        ("Work/Sample/note.md", "money_admin", "work"),
        ("Personal/note.md", None, "personal"),
        ("Personal/note.md", "work", None),
        ("Personal/note.md", "relationships", "personal"),
        ("Archive/Analytics/note.md", None, "work"),
        ("Other/note.md", "work", "work"),
        ("Other/note.md", "health", "personal"),
        ("Other/note.md", "mixed", None),
        ("Other/note.md", None, None),
    ])
    def test_note_domain(self, rel, tag, expected):
        assert note_domain(rel, tag, EMPLOYMENT) == expected

    def test_frontmatter_people_count_as_note_text(self):
        note = build_note_context(
            "Other/n.md", {"people": ["Robin Gamma"]}, "Body", note_date=None, tag_domain=None, employment=EMPLOYMENT,
        )
        assert "robin gamma" in note.text

    def test_meeting_flag_and_title(self):
        note = build_note_context(
            "Other/Budget review.md", {"type": "meeting"}, "", note_date=None, tag_domain=None, employment=EMPLOYMENT,
        )
        assert note.is_meeting is True
        assert note.title == "Budget review"


class TestEmploymentHistoryLoading:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_employment_history(tmp_path / "absent.json").employers == ()

    def test_malformed_file_is_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_employment_history(path).employers == ()

    def test_entry_without_a_name_is_dropped(self):
        history = parse_employment_history({"employers": [{"email_domains": ["a.example"]}, {"name": "Kept"}]})
        assert [e.name for e in history.employers] == ["Kept"]

    def test_month_bounds_cover_the_whole_month(self):
        employer = parse_employment_history(
            {"employers": [{"name": "E", "start": "2020-02", "end": "2020-03"}]}
        ).employers[0]
        assert employer.active_on(date(2020, 2, 1))
        assert employer.active_on(date(2020, 3, 31))
        assert not employer.active_on(date(2020, 1, 31))
        assert not employer.active_on(date(2020, 4, 1))

    def test_null_end_is_current(self):
        employer = parse_employment_history({"employers": [{"name": "E", "start": "2020-02", "end": None}]}).employers[0]
        assert employer.active_on(date(2099, 1, 1))

    def test_no_start_date_never_matches_a_date_but_owns_folders(self):
        history = parse_employment_history({"employers": [{"name": "E", "vault_folders": ["Work/E/"]}]})
        assert history.on_date(date(2020, 1, 1)) == ()
        assert history.for_path("Work/E/note.md") == history.employers
        assert history.for_path("Work/Elsewhere/note.md") == ()

    def test_overlapping_tenures_all_count(self):
        history = parse_employment_history({"employers": [
            {"name": "A", "start": "2020-01", "end": "2021-01"},
            {"name": "B", "start": "2020-06", "end": "2022-01"},
        ]})
        assert [e.name for e in history.on_date(date(2020, 7, 1))] == ["A", "B"]

    def test_example_file_parses(self):
        example = Path(__file__).resolve().parent.parent / "config" / "employment_history.example.json"
        assert load_employment_history(example).employers
