"""Tests for relationship metrics calculation."""
import math
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.services import relationship_metrics as rm
from api.services.interaction_store import (
    Interaction,
    InteractionStore,
    StrengthBucket,
    StrengthInputs,
)
from api.services.person_entity import PersonEntity
from api.services.relationship_metrics import (
    compute_diversity_score,
    compute_lifetime_frequency_score,
    compute_recency_multiplier,
    compute_recency_score,
    compute_recent_frequency_score,
    compute_relationship_strength,
    compute_strength_components,
    compute_strength_for_person,
)
from config.relationship_weights import (
    DIVERSITY_WEIGHT,
    INTERACTION_TYPE_WEIGHTS,
    LIFETIME_FREQUENCY_TARGET,
    LIFETIME_FREQUENCY_WEIGHT,
    MIN_INTERACTIONS_FOR_FULL_RECENCY,
    RECENCY_HALF_LIFE_DAYS,
    RECENCY_WEIGHT,
    RECENT_FREQUENCY_HALF_LIFE_DAYS,
    RECENT_FREQUENCY_TARGET,
    RECENT_FREQUENCY_WEIGHT,
    WHATSAPP_GROUP_RECEIVED_MULTIPLIER,
    WHATSAPP_GROUP_SENT_MULTIPLIER,
    get_interaction_weight,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()


def _day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).date().isoformat()


class TestApprovedValues:
    """The formula's constants are a decision, not a tuning detail."""

    def test_component_weights(self):
        assert RECENCY_WEIGHT == 0.25
        assert RECENT_FREQUENCY_WEIGHT == 0.55
        assert LIFETIME_FREQUENCY_WEIGHT == 0.15
        assert DIVERSITY_WEIGHT == 0.05

    def test_decay_and_targets(self):
        assert RECENCY_HALF_LIFE_DAYS == 45
        assert RECENT_FREQUENCY_HALF_LIFE_DAYS == 90
        assert RECENT_FREQUENCY_TARGET == 600
        assert LIFETIME_FREQUENCY_TARGET == 2500

    def test_whatsapp_group_multipliers(self):
        assert WHATSAPP_GROUP_SENT_MULTIPLIER == 0.15
        assert WHATSAPP_GROUP_RECEIVED_MULTIPLIER == 0.35


class TestRecencyScore:
    """Tests for recency score calculation."""

    def test_none_last_interaction(self):
        """Test that no interaction scores 0."""
        assert compute_recency_score(None, NOW) == 0.0

    def test_just_now(self):
        """Test an interaction this instant scores 1."""
        assert compute_recency_score(NOW, NOW) == 1.0

    def test_one_half_life(self):
        """Test the score halves after one half-life."""
        then = NOW - timedelta(days=RECENCY_HALF_LIFE_DAYS)
        assert compute_recency_score(then, NOW) == pytest.approx(0.5)

    def test_two_half_lives(self):
        """Test the decay is exponential rather than linear."""
        then = NOW - timedelta(days=2 * RECENCY_HALF_LIFE_DAYS)
        assert compute_recency_score(then, NOW) == pytest.approx(0.25)

    def test_never_reaches_a_cliff(self):
        """Test a very old interaction is small but not clipped to 0."""
        then = NOW - timedelta(days=400)
        assert 0.0 < compute_recency_score(then, NOW) < 0.01

    def test_future_timestamp_scores_as_now(self):
        """Test a timestamp ahead of now can't push the score above 1."""
        assert compute_recency_score(NOW + timedelta(days=30), NOW) == 1.0

    def test_naive_datetime(self):
        """Test a naive timestamp is read as UTC."""
        naive = (NOW - timedelta(days=RECENCY_HALF_LIFE_DAYS)).replace(tzinfo=None)
        assert compute_recency_score(naive, NOW) == pytest.approx(0.5)


class TestRecencyMultiplier:
    """Tests for the low-interaction recency discount."""

    def test_zero_interactions(self):
        assert compute_recency_multiplier(0) == 0.0

    def test_scales_linearly(self):
        assert compute_recency_multiplier(5) == pytest.approx(5 / MIN_INTERACTIONS_FOR_FULL_RECENCY)

    def test_full_credit_at_minimum(self):
        assert compute_recency_multiplier(MIN_INTERACTIONS_FOR_FULL_RECENCY) == 1.0
        assert compute_recency_multiplier(MIN_INTERACTIONS_FOR_FULL_RECENCY + 100) == 1.0


class TestRecentFrequencyScore:
    """Tests for the decayed frequency score."""

    def test_zero(self):
        assert compute_recent_frequency_score(0) == 0.0

    def test_negative(self):
        assert compute_recent_frequency_score(-5) == 0.0

    def test_log_scaled(self):
        expected = math.log(1 + 24) / math.log(1 + RECENT_FREQUENCY_TARGET)
        assert compute_recent_frequency_score(24) == pytest.approx(expected)

    def test_reaches_one_at_target(self):
        assert compute_recent_frequency_score(RECENT_FREQUENCY_TARGET) == pytest.approx(1.0)

    def test_capped_above_target(self):
        assert compute_recent_frequency_score(RECENT_FREQUENCY_TARGET * 5) == 1.0


class TestLifetimeFrequencyScore:
    """Tests for the undecayed frequency score."""

    def test_zero(self):
        assert compute_lifetime_frequency_score(0) == 0.0

    def test_log_scaled(self):
        expected = math.log(1 + 100) / math.log(1 + LIFETIME_FREQUENCY_TARGET)
        assert compute_lifetime_frequency_score(100) == pytest.approx(expected)

    def test_reaches_one_at_target(self):
        assert compute_lifetime_frequency_score(LIFETIME_FREQUENCY_TARGET) == pytest.approx(1.0)

    def test_capped_above_target(self):
        assert compute_lifetime_frequency_score(LIFETIME_FREQUENCY_TARGET * 5) == 1.0


class TestDiversityScore:
    """Tests for diversity score calculation."""

    def test_no_sources(self):
        """Test with no sources."""
        score = compute_diversity_score([])
        assert score == 0.0

    def test_single_source(self):
        """Test with single source."""
        score = compute_diversity_score(["gmail"])
        # Should be small (1/total_sources)
        assert 0.0 < score < 0.2

    def test_multiple_sources(self):
        """Test with multiple sources."""
        score = compute_diversity_score(["gmail", "calendar", "slack"])
        assert score > 0.2

    def test_duplicate_sources(self):
        """Test that duplicates are counted once."""
        score_with_dups = compute_diversity_score(["gmail", "gmail", "gmail"])
        score_single = compute_diversity_score(["gmail"])
        assert score_with_dups == score_single


class TestRelationshipStrength:
    """Tests for combining component scores."""

    def test_zero_everything(self):
        assert compute_relationship_strength(0.0, 0.0, 0.0, 0.0) == 0.0

    def test_all_components_full(self):
        assert compute_relationship_strength(1.0, 1.0, 1.0, 1.0) == pytest.approx(100.0)

    @pytest.mark.parametrize(
        ("components", "points"),
        [
            ((1.0, 0.0, 0.0, 0.0), 25.0),
            ((0.0, 1.0, 0.0, 0.0), 55.0),
            ((0.0, 0.0, 1.0, 0.0), 15.0),
            ((0.0, 0.0, 0.0, 1.0), 5.0),
        ],
    )
    def test_each_component_carries_its_weight(self, components, points):
        assert compute_relationship_strength(*components) == pytest.approx(points)


class TestWhatsAppGroupWeights:
    """Group messages weigh less than direct ones, and sent least of all."""

    def test_direct_keeps_the_full_weight(self):
        assert get_interaction_weight("whatsapp", None) == INTERACTION_TYPE_WEIGHTS["whatsapp"]

    def test_group_sent(self):
        assert get_interaction_weight("whatsapp", "whatsapp_group_sent") == pytest.approx(
            INTERACTION_TYPE_WEIGHTS["whatsapp"] * 0.15
        )

    def test_group_received(self):
        assert get_interaction_weight("whatsapp", "whatsapp_group_received") == pytest.approx(
            INTERACTION_TYPE_WEIGHTS["whatsapp"] * 0.35
        )

    def test_other_weights_are_unchanged(self):
        assert get_interaction_weight("imessage") == 1.5
        assert get_interaction_weight("gmail", "gmail_sent", "personal") == 2.4
        assert get_interaction_weight("calendar", "calendar_1on1", "personal") == 18.0


class TestStrengthComponents:
    """Turning aggregated interactions into component scores."""

    @staticmethod
    def _person(**kwargs) -> PersonEntity:
        return PersonEntity(id="p1", canonical_name="Pat Example", **kwargs)

    def _components(self, *buckets, last=NOW, person=None):
        inputs = StrengthInputs(last_interaction=last, buckets=list(buckets))
        return compute_strength_components(person or self._person(), inputs, NOW)

    def test_no_interactions(self):
        c = compute_strength_components(self._person(), StrengthInputs(), NOW)
        assert (c.recency, c.recent_frequency, c.lifetime_frequency) == (0.0, 0.0, 0.0)

    def test_recent_weight_decays_by_half_life(self):
        today = self._components(StrengthBucket("imessage", None, None, TODAY, 100))
        aged = self._components(
            StrengthBucket("imessage", None, None, _day(RECENT_FREQUENCY_HALF_LIFE_DAYS), 100)
        )
        assert today.decayed_weight == pytest.approx(150.0)
        assert aged.decayed_weight == pytest.approx(75.0)

    def test_lifetime_weight_does_not_decay(self):
        aged = self._components(StrengthBucket("imessage", None, None, _day(2000), 100))
        assert aged.lifetime_weight == pytest.approx(150.0)
        assert aged.decayed_weight < 1.0

    def test_buckets_sum_with_their_own_weights(self):
        c = self._components(
            StrengthBucket("imessage", None, None, TODAY, 10),
            StrengthBucket("gmail", "gmail_sent", "personal", TODAY, 10),
        )
        assert c.lifetime_weight == pytest.approx(10 * 1.5 + 10 * 2.4)

    def test_group_subtypes_set_the_weight(self):
        direct = self._components(StrengthBucket("whatsapp", None, None, TODAY, 100))
        sent = self._components(StrengthBucket("whatsapp", "whatsapp_group_sent", None, TODAY, 100))
        received = self._components(StrengthBucket("whatsapp", "whatsapp_group_received", None, TODAY, 100))
        assert direct.decayed_weight == pytest.approx(150.0)
        assert sent.decayed_weight == pytest.approx(22.5)
        assert received.decayed_weight == pytest.approx(52.5)

    def test_low_interaction_discount_applies_to_recency(self):
        few = self._components(StrengthBucket("imessage", None, None, TODAY, 5))
        many = self._components(StrengthBucket("imessage", None, None, TODAY, 15))
        assert few.recency == pytest.approx(5 / 15)
        assert many.recency == pytest.approx(1.0)

    def test_recency_ignores_last_seen(self):
        """A sync refreshing last_seen says nothing about the relationship."""
        person = self._person(last_seen=NOW)
        c = self._components(
            StrengthBucket("imessage", None, None, _day(450), 50),
            last=NOW - timedelta(days=450),
            person=person,
        )
        assert c.recency == pytest.approx(0.5 ** 10)

    def test_last_seen_alone_earns_no_recency(self):
        person = self._person(last_seen=NOW)
        c = compute_strength_components(person, StrengthInputs(), NOW)
        assert c.recency == 0.0

    def test_diversity_combines_interactions_and_person_sources(self):
        person = self._person(sources=["contacts", "linkedin"])
        c = self._components(StrengthBucket("imessage", None, None, TODAY, 1), person=person)
        assert c.sources == ["contacts", "imessage", "linkedin"]
        assert c.diversity == pytest.approx(3 / 14)


@pytest.fixture
def interaction_store(tmp_path, monkeypatch):
    store = InteractionStore(str(tmp_path / "interactions.db"), strict=False)
    monkeypatch.setattr(rm, "get_interaction_store", lambda: store)
    return store


@pytest.fixture
def no_relationships(monkeypatch):
    """The multiplier lookup finds no relationship with the owner."""
    from api.services import relationship as relationship_module
    from config.settings import settings

    monkeypatch.setattr(settings, "my_person_id", "owner-id", raising=False)
    monkeypatch.setattr(
        relationship_module,
        "get_relationship_store",
        lambda: SimpleNamespace(get_between=lambda *a: None),
    )


def _add(store, person_id, when, source_type="imessage", source_id=None, attendee_count=None, title="Hello"):
    store.add(Interaction(
        id=str(uuid.uuid4()),
        person_id=person_id,
        timestamp=when,
        source_type=source_type,
        title=title,
        source_id=source_id or str(uuid.uuid4()),
        attendee_count=attendee_count,
    ))


class TestStrengthForPerson:
    """The formula end to end against a real interaction store."""

    def test_future_rows_count_for_nothing(self, interaction_store, no_relationships):
        now = datetime.now(timezone.utc)
        person = PersonEntity(id="p1", canonical_name="Pat Example")
        for offset in range(20):
            _add(interaction_store, "p1", now + timedelta(days=30 + offset), source_type="calendar",
                 attendee_count=1)
        assert compute_strength_for_person(person, now=now) == 0.0

    def test_formula_with_known_inputs(self, interaction_store, no_relationships):
        now = datetime.now(timezone.utc)
        person = PersonEntity(id="p1", canonical_name="Pat Example")
        for _ in range(20):
            _add(interaction_store, "p1", now - timedelta(days=90, minutes=1))
        recent = 0.5 ** (90 / 90) * 30
        expected = 100 * (
            0.25 * 0.5 ** (90 / 45)
            + 0.55 * math.log(1 + recent) / math.log(601)
            + 0.15 * math.log(1 + 30) / math.log(2501)
            + 0.05 * (1 / 14)
        )
        assert compute_strength_for_person(person, now=now) == pytest.approx(expected, abs=0.1)

    def test_family_and_linkedin_multipliers_apply_and_cap(self, interaction_store, monkeypatch):
        from api.services import relationship as relationship_module
        from api.services.relationship import TYPE_FAMILY
        from config.settings import settings

        monkeypatch.setattr(settings, "my_person_id", "owner-id", raising=False)
        monkeypatch.setattr(
            relationship_module,
            "get_relationship_store",
            lambda: SimpleNamespace(get_between=lambda *a: SimpleNamespace(
                is_linkedin_connection=True, relationship_type=TYPE_FAMILY,
            )),
        )
        full = StrengthInputs(
            last_interaction=NOW,
            buckets=[StrengthBucket("calendar", "calendar_1on1", "personal", TODAY, 1000)],
        )
        person = PersonEntity(id="p1", canonical_name="Pat Example", sources=list(rm.SOURCE_TYPES))
        assert compute_strength_for_person(person, full, NOW) == 100.0

        modest = StrengthInputs(
            last_interaction=NOW,
            buckets=[StrengthBucket("imessage", None, None, TODAY, 15)],
        )
        person = PersonEntity(id="p2", canonical_name="Pat Example")
        c = compute_strength_components(person, modest, NOW)
        base = compute_relationship_strength(c.recency, c.recent_frequency, c.lifetime_frequency, c.diversity)
        assert compute_strength_for_person(person, modest, NOW) == pytest.approx(base * 1.03 * 1.05, abs=0.05)


class _MemoryPersonStore:
    """The slice of PersonEntityStore that update_all_strengths touches."""

    def __init__(self, people):
        self.people = {p.id: p for p in people}

    def get_all(self, include_hidden=False):
        return [p for p in self.people.values() if include_hidden or not p.hidden]

    def get_by_id(self, person_id):
        return self.people.get(person_id)

    def update(self, person):
        self.people[person.id] = person

    def save(self):
        pass


@pytest.fixture
def person_store(monkeypatch):
    def install(people):
        store = _MemoryPersonStore(people)
        monkeypatch.setattr(rm, "get_person_entity_store", lambda: store)
        monkeypatch.setattr(rm, "compute_person_category", lambda person: person.category)
        monkeypatch.setattr(rm, "TAG_OVERRIDES_BY_ID", {})
        return store
    return install


class TestUpdateAllStrengths:
    """The nightly pass over everyone."""

    def test_matches_scoring_one_person_at_a_time(self, interaction_store, no_relationships, person_store):
        now = datetime.now(timezone.utc)
        for person_id, rows in (("p1", 20), ("p2", 3), ("p3", 40)):
            for n in range(rows):
                _add(interaction_store, person_id, now - timedelta(days=n * 7, minutes=5))
        _add(interaction_store, "p1", now - timedelta(days=2), source_type="whatsapp",
             source_id="whatsapp_m1:peer@s.whatsapp.net", attendee_count=4)
        _add(interaction_store, "p3", now + timedelta(days=9), source_type="calendar", attendee_count=1)

        people = [PersonEntity(id=pid, canonical_name=f"Person {pid}") for pid in ("p1", "p2", "p3", "p4")]
        store = person_store(people)
        rm.update_all_strengths()

        for person_id in ("p1", "p2", "p3", "p4"):
            single = compute_strength_for_person(PersonEntity(id=person_id, canonical_name="x"))
            assert store.people[person_id].relationship_strength == pytest.approx(single, abs=0.11)
        assert store.people["p4"].relationship_strength == 0.0
        assert store.people["p4"].is_peripheral_contact is True

    def test_owner_keeps_self_strength(self, interaction_store, no_relationships, person_store):
        store = person_store([PersonEntity(id="owner-id", canonical_name="Owner")])
        rm.update_all_strengths()
        assert store.people["owner-id"].relationship_strength == rm.SELF_STRENGTH

    def test_strength_override_wins(self, interaction_store, no_relationships, person_store, monkeypatch):
        monkeypatch.setattr(rm, "STRENGTH_OVERRIDES_BY_ID", {"p1": 42.0})
        store = person_store([PersonEntity(id="p1", canonical_name="Pat Example")])
        rm.update_all_strengths()
        assert store.people["p1"].relationship_strength == 42.0
        assert store.people["p1"].is_peripheral_contact is False


class TestDunbarCircles:
    """Who consumes a slot in the ranked circles."""

    @staticmethod
    def _ranked(count, strength=50.0, **kwargs):
        people = []
        for n in range(count):
            person = PersonEntity(
                id=f"{kwargs.get('category', 'personal')}-{n}", canonical_name=f"Person {n}",
                category=kwargs.get("category", "personal"), hidden=kwargs.get("hidden", False),
            )
            person.relationship_strength = strength - n * 0.01
            people.append(person)
        return people

    def test_work_people_do_not_consume_slots(self):
        work = self._ranked(5, strength=90.0, category="work")
        personal = self._ranked(6)
        store = _MemoryPersonStore(work + personal)
        rm.compute_all_dunbar_circles(store)
        assert store.people["personal-4"].dunbar_circle == 1
        assert store.people["personal-5"].dunbar_circle == 2
        assert store.people["work-0"].dunbar_circle == 1

    def test_circle_override_does_not_consume_a_slot(self, monkeypatch):
        monkeypatch.setattr(rm, "CIRCLE_OVERRIDES_BY_ID", {"personal-0": 0})
        store = _MemoryPersonStore(self._ranked(6))
        rm.compute_all_dunbar_circles(store)
        assert store.people["personal-0"].dunbar_circle == 0
        assert store.people["personal-5"].dunbar_circle == 1

    def test_hidden_people_do_not_consume_slots(self):
        """Hidden people aren't rescored, so their stored strength can be arbitrarily stale."""
        hidden = self._ranked(5, strength=99.0, hidden=True)
        for person in hidden:
            person.id = f"hidden-{person.id}"
        personal = self._ranked(6)
        store = _MemoryPersonStore(hidden + personal)
        rm.compute_all_dunbar_circles(store)
        assert store.people["personal-0"].dunbar_circle == 1
        assert store.people["personal-4"].dunbar_circle == 1
        assert store.people["personal-5"].dunbar_circle == 2


class TestStrengthBreakdown:
    """The breakdown endpoint reports the formula actually in use."""

    def test_components_match_the_score(self, interaction_store, no_relationships):
        now = datetime.now(timezone.utc)
        for n in range(20):
            _add(interaction_store, "p1", now - timedelta(days=n, minutes=1))
        _add(interaction_store, "p1", now + timedelta(days=5), source_type="calendar", attendee_count=1)
        person = PersonEntity(id="p1", canonical_name="Pat Example", last_seen=now)

        data = rm.get_strength_breakdown(person)

        assert set(data) == {
            "overall_strength", "multiplier", "recency", "recent_frequency", "lifetime_frequency", "diversity",
        }
        assert (data["recency"]["weight"], data["recent_frequency"]["weight"],
                data["lifetime_frequency"]["weight"], data["diversity"]["weight"]) == (0.25, 0.55, 0.15, 0.05)
        total = sum(data[k]["weighted_score"] for k in ("recency", "recent_frequency", "lifetime_frequency", "diversity"))
        assert data["overall_strength"] == pytest.approx(total * 100, abs=0.1)
        assert data["lifetime_frequency"]["raw_interaction_count"] == 20
        assert "calendar_1on1" not in data["lifetime_frequency"]["interactions_by_type"]
        assert data["recency"]["days_since_last"] == 0

    def test_reports_group_subtypes(self, interaction_store, no_relationships):
        now = datetime.now(timezone.utc)
        _add(interaction_store, "p1", now - timedelta(days=1), source_type="whatsapp",
             source_id="whatsapp_m1:peer@s.whatsapp.net", attendee_count=4)
        person = PersonEntity(id="p1", canonical_name="Pat Example")

        by_type = rm.get_strength_breakdown(person)["lifetime_frequency"]["interactions_by_type"]

        assert by_type["whatsapp_group_sent"]["weight"] == pytest.approx(0.225)


class TestPairStrengthIsIndependent:
    """Pair strength keeps its own formula and constants."""

    @pytest.mark.parametrize(
        ("kwargs", "days_ago", "expected"),
        [
            ({"shared_events_count": 10}, 0, 63),
            ({"shared_events_count": 5, "shared_threads_count": 5, "shared_messages_count": 10}, 100, 62),
            ({"shared_whatsapp_count": 2}, 300, 20),
            ({"shared_phone_calls_count": 50, "shared_slack_count": 10, "is_linkedin_connection": True}, 20, 91),
            ({"shared_photos_count": 400}, 0, 30),
        ],
    )
    def test_pinned_values(self, kwargs, days_ago, expected):
        from api.services.relationship import Relationship

        relationship = Relationship(
            id="r1", person_a_id="a", person_b_id="b",
            last_seen_together=datetime.now(timezone.utc) - timedelta(days=days_ago),
            **kwargs,
        )
        assert relationship.pair_strength == expected


class TestSelfStrength:
    """The CRM owner's strength is anchored, not computed.

    Regression guard: computing it from interactions scores the owner against
    *themselves* -- self-addressed mail, self-invited events, notes they appear
    in -- which put the owner at 94.3 on a real install, ranking them below
    their own partner in their own CRM.
    """

    def _owner(self, person_id="owner-id"):
        from api.services.person_entity import PersonEntity
        return PersonEntity(id=person_id, canonical_name="Owner", display_name="Owner")

    def test_owner_is_pinned_to_max(self, monkeypatch):
        from config.settings import settings
        from api.services import relationship_metrics as rm

        person = self._owner()
        monkeypatch.setattr(settings, "my_person_id", person.id, raising=False)
        assert rm.compute_strength_for_person(person) == rm.SELF_STRENGTH
        assert rm.SELF_STRENGTH == 100.0

    def test_owner_check_precedes_interaction_lookup(self, monkeypatch):
        """The short-circuit must not depend on the interaction store at all."""
        from config.settings import settings
        from api.services import relationship_metrics as rm

        person = self._owner()
        monkeypatch.setattr(settings, "my_person_id", person.id, raising=False)

        def _boom():
            raise AssertionError("interaction store must not be consulted for the owner")

        monkeypatch.setattr(rm, "get_interaction_store", _boom)
        assert rm.compute_strength_for_person(person) == rm.SELF_STRENGTH

    def test_non_owner_is_not_pinned(self, monkeypatch):
        """Everyone else still goes through the normal computation."""
        from config.settings import settings
        from api.services import relationship_metrics as rm

        monkeypatch.setattr(settings, "my_person_id", "somebody-else", raising=False)
        person = self._owner(person_id="not-the-owner")
        # No interactions -> a real computation yields far below the anchor.
        assert rm.compute_strength_for_person(person) < rm.SELF_STRENGTH

    def test_unset_owner_id_does_not_pin_everyone(self, monkeypatch):
        """A blank my_person_id must not make every person the owner."""
        from config.settings import settings
        from api.services import relationship_metrics as rm

        monkeypatch.setattr(settings, "my_person_id", "", raising=False)
        person = self._owner(person_id="")
        assert rm.compute_strength_for_person(person) < rm.SELF_STRENGTH
