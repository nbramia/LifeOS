"""
Tests for EntityResolver.
"""
import pytest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.entity_resolver import (
    EntityResolver,
    ResolutionCandidate,
    ResolutionResult,
    get_entity_resolver,
)


# Module-level fixtures available to all test classes
@pytest.fixture
def temp_store():
    """Create a temporary entity store for testing."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        store = PersonEntityStore(f.name)
        yield store
        Path(f.name).unlink(missing_ok=True)


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
            phone_numbers=["+19012295017"],
            phone_primary="+19012295017",
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


class TestResolveByPhone:
    """Tests for phone number anchoring."""

    def test_exact_phone_match(self, populated_resolver):
        """Test exact phone match returns entity."""
        entity = populated_resolver.resolve_by_phone("+19012295017")
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

    def test_exact_name_match(self, populated_resolver):
        """Test exact name match."""
        result = populated_resolver.resolve_by_name("Alex Johnson")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"
        assert result.confidence >= 0.9

    def test_alias_match(self, populated_resolver):
        """Test matching by alias."""
        result = populated_resolver.resolve_by_name("Alex")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    def test_fuzzy_match(self, populated_resolver):
        """Test fuzzy name matching."""
        # Slight variation - "J" initial matches "Johnson"
        result = populated_resolver.resolve_by_name("Alex J")
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"

    def test_context_boost_same_context(self, populated_resolver):
        """Test context boost helps disambiguation."""
        # "Sarah" appears in two contexts
        # With ML context, should prefer Sarah Chen
        result = populated_resolver.resolve_by_name(
            "Sarah", context_path="/vault/Work/ExampleCorp/meeting.md"
        )
        assert result is not None
        assert result.entity.canonical_name == "Sarah Chen"

    def test_context_boost_murm_context(self, populated_resolver):
        """Test context boost for Old Corp context."""
        # With Murm context, should prefer Sarah Miller
        result = populated_resolver.resolve_by_name(
            "Sarah", context_path="/vault/Personal/zArchive/OldCorp/notes.md"
        )
        assert result is not None
        assert result.entity.canonical_name == "Sarah Miller"

    def test_unknown_name_no_create(self, populated_resolver):
        """Test unknown name returns None when create_if_missing=False."""
        result = populated_resolver.resolve_by_name("Unknown Person")
        assert result is None

    def test_unknown_name_with_create(self, populated_resolver):
        """Test unknown name creates entity when create_if_missing=True."""
        result = populated_resolver.resolve_by_name(
            "New Person", create_if_missing=True
        )
        assert result is not None
        assert result.is_new is True
        assert result.entity.canonical_name == "New Person"

    def test_create_with_context_inference(self, populated_resolver):
        """Test new entity gets context from path."""
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
            phone="+19012295017",
        )
        assert result is not None
        assert result.entity.canonical_name == "Alex Johnson"
        assert result.match_type == "phone_exact"

    def test_resolve_email_over_phone(self, populated_resolver):
        """Test email takes priority over phone."""
        result = populated_resolver.resolve(
            email="sarah@work.example.com",
            phone="+19012295017",  # Alex's phone
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

    def test_disambiguation_creates_separate_entities(self, populated_resolver):
        """Test that ambiguous names can create separate entities."""
        # First, resolve Sarah in one context
        result1 = populated_resolver.resolve_by_name(
            "Sarah",
            context_path="/vault/Work/ExampleCorp/meeting.md",
        )
        assert result1 is not None

        # Then create a new Sarah in a completely different context
        # This should potentially create a disambiguated entity
        result2 = populated_resolver.resolve_by_name(
            "Sarah",
            context_path="/vault/Personal/notes.md",
            create_if_missing=True,
        )
        assert result2 is not None

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
        result = parse_name("Sarah Long, CLC, CSC")
        assert result.first == "Sarah"
        assert result.last == "Long"
        assert result.middles == []

        # PhD after comma
        result = parse_name("Shengnan Zhao, PhD")
        assert result.first == "Shengnan"
        assert result.last == "Zhao"

        # Multiple credentials
        result = parse_name("Matt Wilhelm, M.P.A.")
        assert result.first == "Matt"
        assert result.last == "Wilhelm"


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
