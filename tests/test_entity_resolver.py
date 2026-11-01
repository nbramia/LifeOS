"""
Tests for EntityResolver.
"""
import pytest
from datetime import datetime, timedelta, timezone

from api.services.person_entity import PersonEntity, PersonEntityStore
from config.people_config import EntityResolutionConfig
from api.services.entity_resolver import (
    EntityResolver,
    ResolutionResult,
)

# Marked per-class/per-function rather than module-level, because
# TestResolveByName::test_create_with_context_inference needs a real
# configured settings.current_work_path (see its own marker below) while
# every other test in this file is fully isolated (temp_store/tmp_path).


class _StoreHoldingLastSeen(PersonEntityStore):
    """Puts a fixture's last_seen in the column, as sync_last_seen() would."""

    def add(self, entity):
        stored = super().add(entity)
        if entity.last_seen:
            conn = self._get_connection()
            conn.execute(
                "UPDATE person_entities SET last_seen = ? WHERE id = ?",
                (entity.last_seen.isoformat(), entity.id),
            )
            conn.commit()
            conn.close()
            self.save()
        return stored


# Module-level fixtures available to all test classes
@pytest.fixture
def temp_store(tmp_path):
    """Create a temporary entity store for testing."""
    db_path = str(tmp_path / "test_entity_resolver.db")
    store = _StoreHoldingLastSeen(db_path)
    yield store


@pytest.fixture
def resolver(temp_store):
    """Create a resolver with temp store."""
    return EntityResolver(temp_store)


@pytest.fixture
def populated_resolver(temp_store):
    """Create a resolver with some existing entities."""
    # Add some test entities
    entities = [
        PersonEntity(
            canonical_name="Alex Johnson",
            emails=["alex@work.example.com"],
            phone_numbers=["+12125550173"],
            phone_primary="+12125550173",
            company="Example Corp",
            category="work",
            vault_contexts=["Work/ExampleCorp/"],
            aliases=["Alex"],
            last_seen=datetime.now() - timedelta(days=5),
        ),
        PersonEntity(
            canonical_name="Sarah Chen",
            emails=["sarah@work.example.com"],
            phone_numbers=["+15551234567"],
            company="Example Corp",
            category="work",
            vault_contexts=["Work/ExampleCorp/"],
            last_seen=datetime.now() - timedelta(days=10),
        ),
        PersonEntity(
            canonical_name="Sarah Miller",
            emails=["sarah@old.example.com"],
            company="Old Corp",
            category="work",
            vault_contexts=["Personal/zArchive/OldCorp/"],
            last_seen=datetime.now() - timedelta(days=100),
        ),
        PersonEntity(
            canonical_name="Taylor",
            emails=["taylor@example.com"],
            phone_numbers=["+15559876543"],
            category="family",
            vault_contexts=["Personal/"],
            last_seen=datetime.now(),
        ),
    ]

    for entity in entities:
        temp_store.add(entity)

    return EntityResolver(temp_store)


@pytest.mark.unit
class TestRecencyBoost:
    """A real interaction within the window adds points. No interaction adds none."""

    @staticmethod
    def _score(store, days_ago):
        store.add(PersonEntity(
            canonical_name="Quinn Example",
            last_seen=None if days_ago is None else datetime.now() - timedelta(days=days_ago),
        ))
        candidates = EntityResolver(store)._score_candidates("Quinn Example", None).candidates
        assert len(candidates) == 1
        return candidates[0].score

    def test_inside_the_window_earns_the_boost(self, tmp_path):
        recent = self._score(
            _StoreHoldingLastSeen(str(tmp_path / "a.db")),
            EntityResolutionConfig.RECENCY_THRESHOLD_DAYS - 1,
        )
        never = self._score(_StoreHoldingLastSeen(str(tmp_path / "b.db")), None)
        assert recent - never == EntityResolutionConfig.RECENCY_BOOST_POINTS

    def test_a_year_is_the_window(self, tmp_path):
        never = self._score(_StoreHoldingLastSeen(str(tmp_path / "never.db")), None)
        inside = self._score(_StoreHoldingLastSeen(str(tmp_path / "inside.db")), 200)
        outside = self._score(_StoreHoldingLastSeen(str(tmp_path / "outside.db")), 370)

        assert inside - never == EntityResolutionConfig.RECENCY_BOOST_POINTS
        assert outside == never

    def test_outside_the_window_earns_nothing(self, tmp_path):
        old = self._score(
            _StoreHoldingLastSeen(str(tmp_path / "a.db")),
            EntityResolutionConfig.RECENCY_THRESHOLD_DAYS + 1,
        )
        never = self._score(_StoreHoldingLastSeen(str(tmp_path / "b.db")), None)
        assert old == never


@pytest.mark.unit
class TestResolveByEmail:
    """Tests for Pass 1: Email anchoring."""

    def test_exact_email_match(self, populated_resolver):
        """Test exact email match returns entity."""
        entity = populated_resolver.resolve_by_email("alex@work.example.com")
        assert entity is not None
        assert entity.canonical_name == "Alex Johnson"

    def test_email_match_case_insensitive(self, populated_resolver):
        """Test email matching is case-insensitive."""
        entity = populated_resolver.resolve_by_email("ALEX@WORK.EXAMPLE.COM")
        assert entity is not None
        assert entity.canonical_name == "Alex Johnson"

    def test_unknown_email_returns_none(self, populated_resolver):
        """Test unknown email returns None."""
        entity = populated_resolver.resolve_by_email("unknown@example.com")
        assert entity is None

    def test_empty_email_returns_none(self, populated_resolver):
        """Test empty/null email returns None."""
        assert populated_resolver.resolve_by_email("") is None
        assert populated_resolver.resolve_by_email(None) is None


@pytest.mark.unit
class TestResolveByPhone:
    """Tests for phone number anchoring."""

    def test_exact_phone_match(self, populated_resolver):
        """Test exact phone match returns entity."""
        entity = populated_resolver.resolve_by_phone("+12125550173")
        assert entity is not None
        assert entity.canonical_name == "Alex Johnson"

    def test_unknown_phone_returns_none(self, populated_resolver):
        """Test unknown phone returns None."""
        entity = populated_resolver.resolve_by_phone("+15555555555")
        assert entity is None

    def test_empty_phone_returns_none(self, populated_resolver):
        """Test empty/null phone returns None."""
        assert populated_resolver.resolve_by_phone("") is None
        assert populated_resolver.resolve_by_phone(None) is None


class TestResolveByName:
    """Tests for Pass 2 & 3: Fuzzy name matching."""

    @pytest.mark.unit
    def test_exact_name_match(self, populated_resolver):
        """Test exact name match."""
        result = populated_resolver.resolve_by_name("Alex Johnson")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"
        assert result.confidence >= 0.9

    @pytest.mark.unit
    def test_alias_match(self, populated_resolver):
        """Test matching by alias."""
        result = populated_resolver.resolve_by_name("Alex")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    @pytest.mark.unit
    def test_fuzzy_match(self, populated_resolver):
        """Test fuzzy name matching."""
        # Slight variation - "J" initial matches "Johnson"
        result = populated_resolver.resolve_by_name("Alex J")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    @pytest.mark.unit
    def test_context_boost_same_context(self, populated_resolver):
        """Test context boost helps disambiguation."""
        # "Sarah" appears in two contexts
        # With ML context, should prefer Sarah Chen
        result = populated_resolver.resolve_by_name(
            "Sarah", context_path="/vault/Work/ExampleCorp/meeting.md"
        )
        assert result is not None
        assert result.entity.canonical_name == "Sarah Chen"

    @pytest.mark.unit
    def test_context_boost_murm_context(self, populated_resolver):
        """Test context boost for Old Corp context."""
        # With Murm context, should prefer Sarah Miller
        result = populated_resolver.resolve_by_name(
            "Sarah", context_path="/vault/Personal/zArchive/OldCorp/notes.md"
        )
        assert result is not None
        assert result.entity.canonical_name == "Sarah Miller"

    @pytest.mark.unit
    def test_unknown_name_no_create(self, populated_resolver):
        """Test unknown name returns None when create_if_missing=False."""
        result = populated_resolver.resolve_by_name("Unknown Person")
        assert result is None

    @pytest.mark.unit
    def test_unknown_name_with_create(self, populated_resolver):
        """Test unknown name creates entity when create_if_missing=True."""
        result = populated_resolver.resolve_by_name(
            "New Person", create_if_missing=True
        )
        assert result is not None
        assert result.is_new is True
        assert result.entity.canonical_name == "New Person"

    @pytest.mark.integration
    def test_create_with_context_inference(self, populated_resolver):
        """Test new entity gets context from path.

        _infer_vault_contexts checks settings.current_work_path first,
        which is real per-operator config (e.g. "Work/ML/"). Unconfigured on
        a clean checkout, it falls through to the generic "Work/" branch —
        so this needs a real configured settings.current_work_path.
        """
        from config.settings import settings
        if settings.current_work_path in ("", "Work/"):
            pytest.skip(
                "settings.current_work_path not configured beyond the generic default "
                "(LIFEOS_CURRENT_WORK_PATH missing from .env)"
            )
        # Use Work/ML path which is a known context pattern
        result = populated_resolver.resolve_by_name(
            "New Colleague",
            context_path="/vault/Work/ML/standup.md",
            create_if_missing=True,
        )
        assert result is not None
        assert result.is_new is True
        # Context path containing "Work/ML" should infer work category and vault context
        assert "Work/ML/" in result.entity.vault_contexts
        assert result.entity.category == "work"


@pytest.mark.unit
class TestResolveMain:
    """Tests for main resolve() method."""

    def test_resolve_with_email_priority(self, populated_resolver):
        """Test email takes priority over name."""
        result = populated_resolver.resolve(
            name="Wrong Name",
            email="alex@work.example.com",
        )
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"
        assert result.match_type == "email_exact"

    def test_resolve_by_name_only(self, populated_resolver):
        """Test resolving by name only."""
        result = populated_resolver.resolve(name="Taylor")
        assert result is not None
        assert result.entity.canonical_name == "Taylor"

    def test_resolve_create_from_email(self, populated_resolver):
        """Test creating entity from unknown email."""
        result = populated_resolver.resolve(
            email="john.doe@newcompany.com",
            create_if_missing=True,
        )
        assert result is not None
        assert result.is_new is True
        assert "john.doe@newcompany.com" in result.entity.emails
        # Name should be extracted from email
        assert "John" in result.entity.canonical_name

    def test_resolve_nothing_found(self, populated_resolver):
        """Test resolve returns None when nothing found."""
        result = populated_resolver.resolve(
            name="Nobody",
            email="nobody@nowhere.com",
            create_if_missing=False,
        )
        assert result is None

    def test_resolve_with_phone_priority(self, populated_resolver):
        """Test phone matching works when email not found."""
        result = populated_resolver.resolve(
            name="Wrong Name",
            phone="+12125550173",
        )
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"
        assert result.match_type == "phone_exact"

    def test_resolve_email_over_phone(self, populated_resolver):
        """Test email takes priority over phone."""
        result = populated_resolver.resolve(
            email="sarah@work.example.com",
            phone="+12125550173",  # Alex's phone
        )
        assert result is not None
        assert result.entity.canonical_name == "Sarah Chen"
        assert result.match_type == "email_exact"

    def test_resolve_create_with_phone(self, populated_resolver):
        """Test creating entity with phone number."""
        result = populated_resolver.resolve(
            name="New Contact",
            phone="+15550001234",
            create_if_missing=True,
        )
        assert result is not None
        assert result.is_new is True
        assert "+15550001234" in result.entity.phone_numbers


@pytest.mark.unit
class TestResolveFromLinkedIn:
    """Tests for LinkedIn-specific resolution."""

    def test_linkedin_email_match(self, populated_resolver):
        """Test LinkedIn resolution with known email."""
        result = populated_resolver.resolve_from_linkedin(
            first_name="Alex",
            last_name="Johnson",
            email="alex@work.example.com",
            company="Example Corp",
            position="CEO",
            linkedin_url="https://linkedin.com/in/alex",
        )

        assert result is not None
        assert result.is_new is False
        assert result.entity.linkedin_url == "https://linkedin.com/in/alex"
        assert result.entity.position == "CEO"
        assert "linkedin" in result.entity.sources

    def test_linkedin_new_person(self, populated_resolver):
        """Test LinkedIn resolution creates new entity."""
        result = populated_resolver.resolve_from_linkedin(
            first_name="John",
            last_name="Smith",
            email="jsmith@work.example.com",
            company="Example Corp",
            position="Engineer",
            linkedin_url="https://linkedin.com/in/jsmith",
        )

        assert result is not None
        assert result.is_new is True
        assert result.entity.canonical_name == "John Smith"
        assert result.entity.company == "Example Corp"
        assert "linkedin" in result.entity.sources

    def test_linkedin_company_context_inference(self, populated_resolver):
        """Test LinkedIn uses company for context inference."""
        result = populated_resolver.resolve_from_linkedin(
            first_name="Jane",
            last_name="Doe",
            email=None,  # No email
            company="Example Corp",
            position="Designer",
            linkedin_url="https://linkedin.com/in/janedoe",
        )

        assert result is not None
        assert result.is_new is True
        # Company is stored on the entity (vault_contexts comes from domain mapping config)
        assert result.entity.company == "Example Corp"

    def test_linkedin_name_match_does_not_mutate_cache_shared_entity(self, temp_store):
        """The name-matching fallback (no email, no company/domain match) must
        not mutate a PersonEntityStore.get_all()-cache-shared object in place.
        resolve_by_name()'s fuzzy path (_score_candidates) returns entities
        straight out of get_all(), so a write here has to refetch a private
        copy by ID before mutating -- otherwise every other reader of the
        cache observes an unpersisted, in-place-mutated entity."""
        resolver = EntityResolver(temp_store)
        person = PersonEntity(
            canonical_name="Alex Johnson",
            emails=["alex@example.com"],
            sources=["gmail"],
        )
        temp_store.add(person)

        # Warm the get_all() cache and hold a reference to the shared object.
        cached_before = temp_store.get_all()
        cached_entity = next(p for p in cached_before if p.id == person.id)
        original_sources = list(cached_entity.sources)
        assert "linkedin" not in original_sources
        assert cached_entity.linkedin_url is None

        # Stub resolve_by_name to return the exact cache-shared object, as
        # _score_candidates() does for real (it iterates store.get_all()).
        # email=None and company=None force resolve_from_linkedin past the
        # email-exact and domain-match branches into this one.
        fake_result = ResolutionResult(
            entity=cached_entity,
            is_new=False,
            confidence=0.9,
            match_type="fuzzy_full_name",
        )
        resolver.resolve_by_name = lambda name, context_path=None, create_if_missing=False: fake_result

        result = resolver.resolve_from_linkedin(
            first_name="Alex",
            last_name="Johnson",
            email=None,
            company=None,
            position="Engineer",
            linkedin_url="https://linkedin.com/in/alexjohnson",
        )

        # The object the test still holds a reference to (as any other
        # concurrent get_all() caller would) must be untouched.
        assert cached_entity.sources == original_sources
        assert cached_entity.linkedin_url is None

        # The write did happen -- through a private copy that got persisted.
        assert result.entity.linkedin_url == "https://linkedin.com/in/alexjohnson"
        assert "linkedin" in result.entity.sources

        # And the cache, once invalidated by the store.update() call inside
        # resolve_from_linkedin, reflects the write on the next get_all().
        refreshed_entity = next(p for p in temp_store.get_all() if p.id == person.id)
        assert refreshed_entity.linkedin_url == "https://linkedin.com/in/alexjohnson"
        assert "linkedin" in refreshed_entity.sources


@pytest.mark.unit
class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_empty_name(self, resolver):
        """Test empty name handling."""
        result = resolver.resolve_by_name("")
        assert result is None

        result = resolver.resolve_by_name("   ")
        assert result is None

    def test_name_normalization(self, populated_resolver):
        """Test that names go through normalization."""
        # "alex" should resolve to "Alex Johnson" via resolve_person_name
        result = populated_resolver.resolve_by_name("alex")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    def test_multiple_add_same_entity(self, resolver):
        """Test that same entity isn't duplicated."""
        # Add entity
        resolver.resolve(
            name="Test Person",
            email="test@example.com",
            create_if_missing=True,
        )

        # Try to add again with same email
        result = resolver.resolve(
            email="test@example.com",
            create_if_missing=True,
        )

        assert result is not None
        assert result.is_new is False

    def test_shared_first_name_does_not_create_a_separate_entity(self, populated_resolver):
        """A bare first name shared by existing people links to no one and adds no one."""
        store = populated_resolver.store
        before = store.count()

        result = populated_resolver.resolve_by_name(
            "Sarah",
            context_path="/vault/Personal/notes.md",
            create_if_missing=True,
        )

        assert result is None
        assert store.count() == before

    def test_extract_name_from_email(self, resolver):
        """Test name extraction from email."""
        result = resolver.resolve(
            email="john.doe@example.com",
            create_if_missing=True,
        )
        assert "John" in result.entity.canonical_name
        assert "Doe" in result.entity.canonical_name

        result = resolver.resolve(
            email="jdoe@example.com",
            create_if_missing=True,
        )
        assert result.entity.canonical_name == "Jdoe"


def _person(store, name, strength=0.0, **kwargs):
    person = PersonEntity(canonical_name=name, last_seen=datetime.now() - timedelta(days=2), **kwargs)
    person.relationship_strength = strength
    return store.add(person)


@pytest.mark.unit
class TestAmbiguousNamesNeverCreate:
    """A name that fits existing people is not the same as a name that fits no one."""

    def test_tie_between_close_people(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        assert EntityResolver(temp_store).resolve_by_name("Robin", create_if_missing=True) is None
        assert temp_store.count() == 2

    def test_tie_between_people_who_are_not_close(self, temp_store):
        _person(temp_store, "Robin Alpha", 10.0)
        _person(temp_store, "Robin Beta", 10.0)

        assert EntityResolver(temp_store).resolve_by_name("Robin", create_if_missing=True) is None
        assert temp_store.count() == 2

    def test_several_weak_candidates_are_a_tie(self, temp_store):
        temp_store.add(PersonEntity(canonical_name="Robin Alpha"))
        temp_store.add(PersonEntity(canonical_name="Robin Beta"))

        assert EntityResolver(temp_store).resolve_by_name("Robin", create_if_missing=True) is None
        assert temp_store.count() == 2

    def test_tie_between_scored_candidates(self, temp_store):
        _person(temp_store, "Robin Alpha")
        _person(temp_store, "Riley Alpha")

        assert EntityResolver(temp_store).resolve_by_name("R Alpha", create_if_missing=True) is None
        assert temp_store.count() == 2

    def test_tie_between_scored_candidates_still_reports_a_best_guess_when_not_creating(self, temp_store):
        _person(temp_store, "Robin Alpha")
        _person(temp_store, "Riley Alpha")

        result = EntityResolver(temp_store).resolve_by_name("R Alpha")

        assert result.match_type == "fuzzy_ambiguous"
        assert result.disambiguation_applied is True
        assert temp_store.count() == 2

    def test_unknown_name_is_still_created(self, temp_store):
        _person(temp_store, "Robin Alpha")

        result = EntityResolver(temp_store).resolve_by_name("Quillon Example", create_if_missing=True)

        assert result.is_new is True
        assert result.match_type == "new_entity"
        assert temp_store.count() == 2

    def test_unknown_bare_name_is_still_created(self, temp_store):
        _person(temp_store, "Robin Alpha")

        result = EntityResolver(temp_store).resolve_by_name("Quillon", create_if_missing=True)

        assert result.is_new is True
        assert temp_store.count() == 2

    def test_a_single_weak_candidate_is_not_a_tie(self, temp_store):
        temp_store.add(PersonEntity(canonical_name="Michael Alpha"))

        result = EntityResolver(temp_store).resolve_by_name("Mike", create_if_missing=True)

        assert result.is_new is True
        assert temp_store.count() == 2

    def test_a_settled_first_name_links_to_the_settled_person(self, temp_store):
        close = _person(temp_store, "Robin Alpha", 60.0)
        _person(temp_store, "Robin Beta", 0.0)

        result = EntityResolver(temp_store).resolve_by_name("Robin", create_if_missing=True)

        assert result.entity.id == close.id
        assert result.is_new is False
        assert temp_store.count() == 2

    def test_an_existing_bare_row_keeps_taking_exact_matches(self, temp_store):
        bare = temp_store.add(PersonEntity(canonical_name="Robin"))
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve_by_name("Robin", create_if_missing=True)

        assert result.entity.id == bare.id
        assert result.match_type == "name_exact"

    def test_unknown_email_still_gets_its_own_person_named_from_the_address(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(
            name="Robin", email="robin.gamma@example.com", create_if_missing=True
        )

        assert result.is_new is True
        assert result.match_type == "email_new"
        assert result.entity.canonical_name == "Robin Gamma"
        assert temp_store.get_by_name("Robin") is None

    def test_single_word_address_goes_by_the_address_not_the_tied_name(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)
        resolver = EntityResolver(temp_store)

        result = resolver.resolve(name="Robin", email="Robin@example.com", create_if_missing=True)

        assert result.is_new is True
        assert result.entity.canonical_name == "robin@example.com"
        assert temp_store.get_by_name("Robin") is None
        assert resolver.resolve_by_name("Robin", create_if_missing=True) is None

    def test_single_word_address_unlike_the_tied_name_goes_by_the_address(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(name="Robin", email="rgamma@example.com", create_if_missing=True)

        assert result.entity.canonical_name == "rgamma@example.com"
        assert temp_store.get_by_name("Rgamma") is None

    def test_address_spelling_out_a_tied_full_name_goes_by_the_address(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Riley Alpha", 30.0)

        result = EntityResolver(temp_store).resolve(name="R Alpha", email="r.alpha@example.com", create_if_missing=True)

        assert result.entity.canonical_name == "r.alpha@example.com"

    def test_blocklisted_email_with_a_tied_name_links_to_no_one(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)
        hidden = temp_store.add(PersonEntity(canonical_name="Quillon Example", emails=["robin.gamma@example.com"]))
        temp_store.hide_person(hidden.id)
        temp_store.delete(hidden.id)

        result = EntityResolver(temp_store).resolve(
            name="Robin", email="robin.gamma@example.com", create_if_missing=True
        )

        assert result is None
        assert temp_store.count() == 2

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_email_with_no_name_is_still_named_from_a_single_word_address(self, temp_store, name):
        result = EntityResolver(temp_store).resolve(name=name, email="robin@example.com", create_if_missing=True)

        assert result.entity.canonical_name == "Robin"

    def test_known_email_wins_over_the_tie(self, temp_store):
        known = _person(temp_store, "Robin Alpha", 30.0, emails=["robin.alpha@example.com"])
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(
            name="Robin", email="robin.alpha@example.com", create_if_missing=True
        )

        assert result.entity.id == known.id

    def test_unknown_phone_still_gets_its_own_person_named_from_the_number(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(
            name="Robin", phone="(212) 555-0173", create_if_missing=True
        )

        assert result.is_new is True
        assert result.match_type == "phone_new"
        assert result.entity.canonical_name == "+12125550173"
        assert result.entity.phone_numbers == ["+12125550173"]
        assert temp_store.get_by_name("Robin") is None
        assert temp_store.count() == 3

    def test_the_number_finds_that_person_next_time(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)
        resolver = EntityResolver(temp_store)
        first = resolver.resolve(name="Robin", phone="+12125550173", create_if_missing=True)

        second = resolver.resolve(name="Robin", phone="+12125550173", create_if_missing=True)

        assert second.entity.id == first.entity.id
        assert second.match_type == "phone_exact"
        assert temp_store.count() == 3

    def test_unknown_email_and_phone_are_named_from_the_address(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(
            name="Robin", email="robin.gamma@example.com", phone="+12125550173", create_if_missing=True
        )

        assert result.match_type == "email_new"
        assert result.entity.canonical_name == "Robin Gamma"
        assert result.entity.phone_numbers == ["+12125550173"]

    def test_tied_name_with_no_identifier_never_creates(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        assert EntityResolver(temp_store).resolve(name="Robin", create_if_missing=True) is None
        assert temp_store.count() == 2

    def test_tied_name_with_an_unusable_phone_never_creates(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        result = EntityResolver(temp_store).resolve(name="Robin", phone="not a phone", create_if_missing=True)

        assert result is None
        assert temp_store.count() == 2

    def test_a_read_only_lookup_never_creates_from_the_phone(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)

        assert EntityResolver(temp_store).resolve(name="Robin", phone="+12125550173") is None
        assert temp_store.count() == 2

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_a_phone_with_no_name_never_creates(self, temp_store, name):
        _person(temp_store, "Robin Alpha", 30.0)

        result = EntityResolver(temp_store).resolve(name=name, phone="+12125550173", create_if_missing=True)

        assert result is None
        assert temp_store.count() == 1

    def test_a_blocklisted_phone_links_to_no_one(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)
        hidden = temp_store.add(PersonEntity(canonical_name="Quillon Example", phone_numbers=["+12125550173"]))
        temp_store.hide_person(hidden.id)
        temp_store.delete(hidden.id)

        result = EntityResolver(temp_store).resolve(name="Robin", phone="+12125550173", create_if_missing=True)

        assert result is None
        assert temp_store.count() == 2

    def test_unknown_name_with_a_phone_is_still_named_from_the_name(self, temp_store):
        _person(temp_store, "Robin Alpha", 30.0)

        result = EntityResolver(temp_store).resolve(
            name="Quillon Example", phone="+12125550173", create_if_missing=True
        )

        assert result.match_type == "new_entity"
        assert result.entity.canonical_name == "Quillon Example"
        assert result.entity.phone_numbers == ["+12125550173"]


@pytest.mark.unit
class TestPlaceholderNames:
    """A write-capable call gives a person named after their identifier a real name."""

    PHONE = "+12125550173"
    EMAIL = "robin@example.com"

    def _by_number(self, store):
        _person(store, "Robin Alpha", 30.0)
        _person(store, "Robin Beta", 30.0)
        resolver = EntityResolver(store)
        return resolver, resolver.resolve(name="Robin", phone=self.PHONE, create_if_missing=True).entity

    def _by_address(self, store):
        _person(store, "Robin Alpha", 30.0)
        _person(store, "Robin Beta", 30.0)
        resolver = EntityResolver(store)
        return resolver, resolver.resolve(name="Robin", email=self.EMAIL, create_if_missing=True).entity

    def test_number_placeholder_takes_a_real_name(self, temp_store):
        resolver, placeholder = self._by_number(temp_store)

        result = resolver.resolve(name="Robin Gamma", phone="(212) 555-0173", create_if_missing=True)

        assert result.entity.id == placeholder.id
        assert result.entity.canonical_name == "Robin Gamma"
        assert result.match_type == "phone_exact"
        stored = temp_store.get_by_id(placeholder.id)
        assert stored.canonical_name == "Robin Gamma"
        assert stored.display_name == "Robin Gamma"

    def test_a_later_mention_of_the_real_name_links_to_them(self, temp_store):
        resolver, placeholder = self._by_number(temp_store)
        resolver.resolve(name="Robin Gamma", phone=self.PHONE, create_if_missing=True)

        result = resolver.resolve(name="Robin Gamma", create_if_missing=True)

        assert result.entity.id == placeholder.id
        assert result.is_new is False
        assert temp_store.count() == 3

    def test_address_placeholder_takes_a_real_name(self, temp_store):
        resolver, placeholder = self._by_address(temp_store)

        result = resolver.resolve(name="Robin Gamma", email="ROBIN@example.com", create_if_missing=True)

        assert result.entity.id == placeholder.id
        assert temp_store.get_by_id(placeholder.id).canonical_name == "Robin Gamma"
        assert temp_store.get_by_name("Robin Gamma").id == placeholder.id

    @pytest.mark.parametrize(
        "name", ["Robin", "Dr Robin", "Robin Jr", "+1 212 555 0173", "Robin 2 Gamma", "robin @example.com", "", None]
    )
    def test_only_a_real_multi_word_name_replaces_a_placeholder(self, temp_store, name):
        resolver, placeholder = self._by_number(temp_store)

        resolver.resolve(name=name, phone=self.PHONE, create_if_missing=True)

        assert temp_store.get_by_id(placeholder.id).canonical_name == self.PHONE
        assert temp_store.get_by_name("Robin") is None

    def test_a_bare_name_does_not_replace_an_address_placeholder(self, temp_store):
        resolver, placeholder = self._by_address(temp_store)

        resolver.resolve(name="Robin", email=self.EMAIL, create_if_missing=True)

        assert temp_store.get_by_id(placeholder.id).canonical_name == self.EMAIL
        assert temp_store.get_by_name("Robin") is None

    def test_a_read_only_lookup_by_number_does_not_rename(self, temp_store):
        resolver, placeholder = self._by_number(temp_store)

        result = resolver.resolve(name="Robin Gamma", phone=self.PHONE)

        assert result.entity.id == placeholder.id
        assert result.entity.canonical_name == self.PHONE
        assert temp_store.get_by_id(placeholder.id).canonical_name == self.PHONE
        assert temp_store.get_by_name("Robin Gamma") is None

    def test_a_read_only_lookup_by_address_does_not_rename(self, temp_store):
        resolver, placeholder = self._by_address(temp_store)

        result = resolver.resolve(name="Robin Gamma", email=self.EMAIL)

        assert result.entity.id == placeholder.id
        assert result.entity.canonical_name == self.EMAIL
        assert temp_store.get_by_id(placeholder.id).canonical_name == self.EMAIL
        assert temp_store.get_by_name("Robin Gamma") is None

    def test_a_real_name_is_never_overwritten(self, temp_store):
        known = _person(temp_store, "Robin Alpha", 30.0, phone_numbers=[self.PHONE], emails=[self.EMAIL])
        resolver = EntityResolver(temp_store)

        resolver.resolve(name="Robin Gamma", phone=self.PHONE, create_if_missing=True)
        resolver.resolve(name="Robin Gamma", email=self.EMAIL, create_if_missing=True)

        assert temp_store.get_by_id(known.id).canonical_name == "Robin Alpha"
        assert temp_store.get_by_name("Robin Gamma") is None

    def test_a_placeholder_for_the_other_identifier_is_left_alone(self, temp_store):
        person = temp_store.add(PersonEntity(
            canonical_name=self.PHONE, phone_numbers=[self.PHONE], emails=[self.EMAIL],
        ))

        EntityResolver(temp_store).resolve(name="Robin Gamma", email=self.EMAIL, create_if_missing=True)

        assert temp_store.get_by_id(person.id).canonical_name == self.PHONE

    def test_a_name_someone_else_holds_is_not_taken(self, temp_store):
        resolver, placeholder = self._by_number(temp_store)
        holder = _person(temp_store, "Robin Gamma", 30.0)

        resolver.resolve(name="Robin Gamma", phone=self.PHONE, create_if_missing=True)

        assert temp_store.get_by_id(placeholder.id).canonical_name == self.PHONE
        assert temp_store.get_by_name("Robin Gamma").id == holder.id

    def test_a_chosen_display_name_stays(self, temp_store):
        person = temp_store.add(PersonEntity(
            canonical_name=self.PHONE, display_name="Robin (gym)", phone_numbers=[self.PHONE],
        ))

        EntityResolver(temp_store).resolve(name="Robin Gamma", phone=self.PHONE, create_if_missing=True)

        stored = temp_store.get_by_id(person.id)
        assert stored.canonical_name == "Robin Gamma"
        assert stored.display_name == "Robin (gym)"

    def test_replacing_a_placeholder_leaves_last_seen_alone(self, temp_store):
        resolver, placeholder = self._by_number(temp_store)
        conn = temp_store._get_connection()
        conn.execute(
            "UPDATE person_entities SET last_seen = ? WHERE id = ?",
            ("2026-01-02T03:04:05+00:00", placeholder.id),
        )
        conn.commit()
        conn.close()
        temp_store.save()

        resolver.resolve(name="Robin Gamma", phone=self.PHONE, create_if_missing=True)

        stored = temp_store.get_by_id(placeholder.id)
        assert stored.canonical_name == "Robin Gamma"
        assert stored.last_seen.isoformat() == "2026-01-02T03:04:05+00:00"


@pytest.mark.unit
class TestMatchName:
    """match_name separates a unique match, a tie, and no match, and never writes."""

    def test_no_match(self, temp_store):
        match = EntityResolver(temp_store).match_name("Quillon Example")
        assert match.result is None
        assert match.tied == []

    def test_unique_match(self, temp_store):
        person = _person(temp_store, "Robin Alpha", 60.0)
        match = EntityResolver(temp_store).match_name("Robin")
        assert match.result.entity.id == person.id
        assert match.tied == []

    def test_first_name_tie_has_no_best_guess(self, temp_store):
        first = _person(temp_store, "Robin Alpha", 30.0)
        second = _person(temp_store, "Robin Beta", 30.0)
        match = EntityResolver(temp_store).match_name("Robin")
        assert match.result is None
        assert {p.id for p in match.tied} == {first.id, second.id}

    def test_scored_tie_keeps_the_top_candidate_and_lists_the_tie(self, temp_store):
        first = _person(temp_store, "Robin Alpha")
        second = _person(temp_store, "Riley Alpha")
        match = EntityResolver(temp_store).match_name("R Alpha")
        assert match.result.match_type == "fuzzy_ambiguous"
        assert {p.id for p in match.tied} == {first.id, second.id}

    def test_a_candidate_outside_the_score_gap_is_not_in_the_tie(self, temp_store):
        close = _person(temp_store, "Robin Alpha", 60.0)
        _person(temp_store, "Riley Alpha", 0.0)
        match = EntityResolver(temp_store).match_name("R Alpha")
        assert match.result.entity.id == close.id
        assert match.tied == []

    def test_a_candidate_exactly_at_the_score_gap_is_not_in_the_tie(self, temp_store):
        # 10 for recency plus 12.5 * 0.4 strength: a lead of exactly 15.
        close = _person(temp_store, "Robin Alpha", 12.5)
        temp_store.add(PersonEntity(canonical_name="Riley Alpha"))
        match = EntityResolver(temp_store).match_name("R Alpha")
        assert match.result.entity.id == close.id
        assert match.tied == []


@pytest.mark.unit
class TestVaultMentionOfAmbiguousName:
    """The vault indexer skips a mention that links to no one and keeps the others."""

    def test_tied_name_records_no_interaction_and_creates_no_person(self, temp_store, tmp_path):
        from unittest.mock import MagicMock

        from api.services.indexer import IndexerService

        known = _person(temp_store, "Quillon Example", 30.0)
        _person(temp_store, "Robin Alpha", 30.0)
        _person(temp_store, "Robin Beta", 30.0)
        indexer = IndexerService.__new__(IndexerService)
        indexer._entity_resolver = EntityResolver(temp_store)
        indexer._interaction_store = MagicMock()
        indexer._interaction_store.add_if_not_exists.return_value = (None, True)
        indexer._source_entity_store = MagicMock()

        affected = indexer._sync_people_to_v2(
            tmp_path / "note.md", ["Robin", "Quillon Example"], datetime(2026, 1, 2)
        )

        assert affected == {known.id}
        assert indexer._interaction_store.add_if_not_exists.call_count == 1
        assert temp_store.count() == 3


@pytest.mark.unit
class TestSyncLoopsSkipNoLink:
    """Sync loops skip a person the resolver returns no link for."""

    def test_gmail_recipient_is_skipped(self):
        from unittest.mock import MagicMock

        from api.services.people_aggregator import sync_gmail_to_v2

        message = MagicMock(
            to="Robin <robin@example.com>, Quillon <quillon@example.com>", message_id="m1", subject="s",
            date=datetime(2026, 1, 2, tzinfo=timezone.utc), snippet="",
        )
        gmail = MagicMock()
        gmail.search.return_value = [message]
        resolver = MagicMock()
        resolver.resolve.side_effect = [None, MagicMock(entity=PersonEntity(id="p1", canonical_name="Quillon"), is_new=False)]
        store = MagicMock()
        store.add_if_not_exists.return_value = (None, True)

        stats = sync_gmail_to_v2(gmail_service=gmail, entity_resolver=resolver, interaction_store=store)

        assert resolver.resolve.call_count == 2
        assert stats["interactions_created"] == 1
        assert store.add_if_not_exists.call_args.args[0].person_id == "p1"

    def test_calendar_attendee_is_skipped(self):
        from unittest.mock import MagicMock

        from api.services.people_aggregator import sync_calendar_to_v2

        event = MagicMock(
            is_all_day=False, attendees=["Robin", "Quillon"], event_id="e1", title="t",
            start_time=datetime(2026, 1, 2, tzinfo=timezone.utc), description="",
        )
        calendar = MagicMock()
        calendar.get_events_in_range.return_value = [event]
        resolver = MagicMock()
        resolver.resolve.side_effect = [None, MagicMock(entity=PersonEntity(id="p1", canonical_name="Quillon"), is_new=False)]
        store = MagicMock()
        store.add_if_not_exists.return_value = (None, True)

        stats = sync_calendar_to_v2(calendar_services=[calendar], entity_resolver=resolver, interaction_store=store)

        assert resolver.resolve.call_count == 2
        assert stats["interactions_created"] == 1
        assert store.add_if_not_exists.call_args.args[0].person_id == "p1"


@pytest.mark.unit
class TestParseName:
    """Tests for the parse_name helper function."""

    def test_simple_two_part_name(self):
        """Test parsing a simple first/last name."""
        from api.services.entity_resolver import parse_name

        result = parse_name("John Smith")
        assert result.first == "John"
        assert result.last == "Smith"
        assert result.middles == []

    def test_three_part_name(self):
        """Test parsing a name with middle name."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Anne Mary Smith")
        assert result.first == "Anne"
        assert result.middles == ["Mary"]
        assert result.last == "Smith"

    def test_first_name_only(self):
        """Test parsing a single name."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Taylor")
        assert result.first == "Taylor"
        assert result.last is None
        assert result.middles == []

    def test_strips_prefix(self):
        """Test that prefixes like Dr., Mr., etc. are stripped."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Dr. John Smith")
        assert result.first == "John"
        assert result.last == "Smith"

        result = parse_name("Mrs. Jane Doe")
        assert result.first == "Jane"
        assert result.last == "Doe"

    def test_strips_suffix(self):
        """Test that suffixes like MD, PhD, Jr are stripped."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Jane Smith MD")
        assert result.first == "Jane"
        assert result.last == "Smith"

        result = parse_name("John Smith Jr")
        assert result.first == "John"
        assert result.last == "Smith"

    def test_strips_multiple_suffixes(self):
        """Test stripping multiple suffixes."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Dr. Mary Katherine Palmer MD PhD")
        assert result.first == "Mary"
        assert result.middles == ["Katherine"]
        assert result.last == "Palmer"

    def test_preserves_original(self):
        """Test that original string is preserved."""
        from api.services.entity_resolver import parse_name

        result = parse_name("Dr. John Smith MD")
        assert result.original == "Dr. John Smith MD"

    def test_empty_string(self):
        """Test handling empty string."""
        from api.services.entity_resolver import parse_name

        result = parse_name("")
        assert result.first == ""
        assert result.last is None

    def test_strips_comma_separated_credentials(self):
        """Test that comma-separated credentials are stripped."""
        from api.services.entity_resolver import parse_name

        # Simple credentials after comma
        result = parse_name("Alice Reed, CLC, CSC")
        assert result.first == "Alice"
        assert result.last == "Reed"
        assert result.middles == []

        # PhD after comma
        result = parse_name("Shengnan Zhao, PhD")
        assert result.first == "Shengnan"
        assert result.last == "Zhao"

        # Multiple credentials
        result = parse_name("Matt Wilhelm, M.P.A.")
        assert result.first == "Matt"
        assert result.last == "Wilhelm"


@pytest.mark.unit
class TestFirstNameStrengthTiebreak:
    """Two first-name candidates with close scores are separated by relationship strength."""

    CONTEXT = "Work/Acme/notes.md"

    def _resolve(self, temp_store, first_strength, second_strength):
        for last, strength in (("Alpha", first_strength), ("Beta", second_strength)):
            person = PersonEntity(canonical_name=f"Robin {last}", vault_contexts=["Work/Acme/"])
            person.relationship_strength = strength
            temp_store.add(person)
        return EntityResolver(temp_store).resolve_by_name("Robin", context_path=self.CONTEXT)

    def test_at_the_close_cutoff_settles_it(self, temp_store):
        result = self._resolve(temp_store, 14.0, 13.9)
        assert result.entity.canonical_name == "Robin Alpha"
        assert result.match_type == "first_name_close_unique"

    def test_below_the_close_cutoff_stays_ambiguous(self, temp_store):
        assert self._resolve(temp_store, 13.9, 13.8) is None

    def test_a_clear_lead_settles_two_close_people(self, temp_store):
        result = self._resolve(temp_store, 50.0, 20.0)
        assert result.entity.canonical_name == "Robin Alpha"
        assert result.match_type == "first_name_relationship_dominant"

    def test_a_smaller_lead_stays_ambiguous(self, temp_store):
        assert self._resolve(temp_store, 49.9, 20.0) is None


@pytest.mark.unit
class TestStrengthBoost:
    """A bare first name needs 40 points: 25 for the name, the rest from strength."""

    def _resolve(self, temp_store, strength):
        for last, value in (("Alpha", strength), ("Beta", 0.0)):
            person = PersonEntity(canonical_name=f"Robin {last}")
            person.relationship_strength = value
            temp_store.add(person)
        return EntityResolver(temp_store).resolve_by_name("Robin")

    def test_strength_that_earns_the_missing_points_resolves(self, temp_store):
        result = self._resolve(temp_store, 25.0)
        assert result.entity.canonical_name == "Robin Alpha"
        assert result.match_type == "first_name_context_clear"

    def test_strength_just_short_of_them_does_not(self, temp_store):
        assert self._resolve(temp_store, 24.9) is None

    def test_the_boost_stops_at_its_cap(self, temp_store):
        """At the cap a stronger person earns no more: 25 name + 25 * 1.5 boost + 10 clear."""
        result = self._resolve(temp_store, 100.0)
        assert result.confidence == pytest.approx(0.725)


@pytest.mark.unit
class TestSingleLetterFirstNameEntity:
    """
    An entity whose first name is a single letter must not swallow every query
    sharing that initial.

    "A Reader" matched ADP, ACP, and ADS, Inc. — all promoted to
    first_name_context_clear and silently linked, because the entire match
    rested on one shared letter.
    """

    def test_first_name_only_query_does_not_match_on_initial_alone(self, temp_store):
        """Without a last name to corroborate, a shared initial is not a match."""
        temp_store.add(PersonEntity(
            canonical_name="A Reader",
            last_seen=datetime.now() - timedelta(days=5),
        ))
        resolver = EntityResolver(temp_store)

        for query in ("ADP", "ACP", "ADS, Inc.", "Andrew"):
            assert resolver.resolve_by_name(query) is None, f"{query} should not match"

    def test_matching_last_name_still_resolves_from_initial(self, temp_store):
        """The legitimate case survives: 'J Smith' is still found by 'John Smith'."""
        temp_store.add(PersonEntity(
            canonical_name="J Smith",
            last_seen=datetime.now() - timedelta(days=5),
        ))
        resolver = EntityResolver(temp_store)

        result = resolver.resolve_by_name("John Smith")
        assert result is not None
        assert result.entity.canonical_name == "J Smith"

    def test_non_matching_last_name_does_not_resolve(self, temp_store):
        """A last name that disagrees is not corroboration."""
        temp_store.add(PersonEntity(
            canonical_name="J Smith",
            last_seen=datetime.now() - timedelta(days=5),
        ))
        resolver = EntityResolver(temp_store)

        assert resolver.resolve_by_name("John Walker") is None


@pytest.mark.unit
class TestStructuredNameMatching:
    """Tests for the new structured name matching in _score_candidates."""

    def test_different_last_names_no_match(self, temp_store):
        """Test that different last names don't match."""
        # This was the original bug: "Mary Katherine Palmer" matched "Jane Smith"
        entity = PersonEntity(
            canonical_name="Jane Smith",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Mary Katherine Palmer")

        assert result is None  # Should NOT match

    def test_same_last_name_different_first_no_match(self, temp_store):
        """Test that same last name but different first doesn't match."""
        entity = PersonEntity(
            canonical_name="Jane Smith",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("John Walker")

        assert result is None  # Different first name

    def test_with_middle_name_matches(self, temp_store):
        """Test that adding a middle name still matches."""
        entity = PersonEntity(
            canonical_name="Mary Smith",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        # Mary Jane Smith should match Mary Smith
        # Query: first=Mary, middle=Jane, last=Smith
        # Entity: first=Mary, last=Smith
        # First names match (Mary=Mary), last names match (Smith=Smith)
        result = resolver.resolve_by_name("Mary Jane Smith")

        # This SHOULD match because first=Mary matches and last=Smith matches
        assert result is not None

    def test_suffix_stripped_matches(self, temp_store):
        """Test that suffixes are stripped before matching."""
        entity = PersonEntity(
            canonical_name="Jane Smith",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Jane Smith MD")

        assert result is not None
        assert result.entity.canonical_name == "Jane Smith"

    def test_initial_matches_full_name(self, temp_store):
        """Test that initial matches full last name."""
        entity = PersonEntity(
            canonical_name="Alex Johnson",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Alex J")

        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    def test_first_name_only_matches(self, temp_store):
        """Test that first name only can match."""
        entity = PersonEntity(
            canonical_name="Ben Calvin",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Ben")

        assert result is not None
        assert result.entity.canonical_name == "Ben Calvin"

    def test_nickname_matches_formal_name(self, temp_store):
        """Test that nicknames match formal names (Ben -> Benjamin)."""
        entity = PersonEntity(
            canonical_name="Benjamin Smith",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Ben Smith")

        assert result is not None
        assert result.entity.canonical_name == "Benjamin Smith"

    def test_formal_name_matches_nickname(self, temp_store):
        """Test that formal names match nicknames (Michael -> Mike)."""
        entity = PersonEntity(
            canonical_name="Mike Johnson",
            last_seen=datetime.now() - timedelta(days=5),
        )
        temp_store.add(entity)

        resolver = EntityResolver(temp_store)
        result = resolver.resolve_by_name("Michael Johnson")

        assert result is not None
        assert result.entity.canonical_name == "Mike Johnson"
