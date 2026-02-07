"""Tests for Apple Contacts integration service."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api.services.apple_contacts import (
    AppleContact,
    create_contact_source_entity,
    SOURCE_CONTACTS,
)


class TestAppleContact:
    """Tests for AppleContact dataclass."""

    def test_create_contact(self):
        """Test basic contact creation."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            organization="Acme Inc",
        )

        assert contact.identifier == "ABC-123"
        assert contact.given_name == "John"
        assert contact.family_name == "Doe"

    def test_display_name_full_name(self):
        """Test display name with full_name."""
        contact = AppleContact(
            identifier="ABC-123",
            full_name="Dr. John Doe",
            given_name="John",
            family_name="Doe",
        )

        assert contact.display_name == "Dr. John Doe"

    def test_display_name_parts(self):
        """Test display name constructed from parts."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
        )

        assert contact.display_name == "John Doe"

    def test_display_name_organization_fallback(self):
        """Test display name falls back to organization."""
        contact = AppleContact(
            identifier="ABC-123",
            organization="Acme Inc",
        )

        assert contact.display_name == "Acme Inc"

    def test_display_name_email_fallback(self):
        """Test display name falls back to email."""
        contact = AppleContact(
            identifier="ABC-123",
            emails=[{"label": "work", "value": "john@example.com"}],
        )

        assert contact.display_name == "john@example.com"

    def test_display_name_identifier_fallback(self):
        """Test display name falls back to identifier."""
        contact = AppleContact(identifier="ABC-123")

        assert contact.display_name == "ABC-123"

    def test_primary_email(self):
        """Test primary email extraction."""
        contact = AppleContact(
            identifier="ABC-123",
            emails=[
                {"label": "work", "value": "john@work.com"},
                {"label": "home", "value": "john@home.com"},
            ],
        )

        assert contact.primary_email == "john@work.com"

    def test_primary_email_none(self):
        """Test primary email when none available."""
        contact = AppleContact(identifier="ABC-123")
        assert contact.primary_email is None

    def test_primary_phone(self):
        """Test primary phone extraction."""
        contact = AppleContact(
            identifier="ABC-123",
            phones=[
                {"label": "mobile", "value": "+1-555-0100"},
                {"label": "work", "value": "+1-555-0200"},
            ],
        )

        assert contact.primary_phone == "+1-555-0100"

    def test_to_dict(self):
        """Test serialization to dict."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            organization="Acme Inc",
            job_title="Engineer",
            emails=[{"label": "work", "value": "john@example.com"}],
        )

        data = contact.to_dict()
        assert data["identifier"] == "ABC-123"
        assert data["display_name"] == "John Doe"
        assert data["organization"] == "Acme Inc"
        assert len(data["emails"]) == 1

    def test_to_dict_with_birthday(self):
        """Test serialization with birthday."""
        birthday = datetime(1990, 5, 15, tzinfo=timezone.utc)
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            birthday=birthday,
        )

        data = contact.to_dict()
        assert data["birthday"] == birthday.isoformat()


class TestCreateContactSourceEntity:
    """Tests for create_contact_source_entity factory function."""

    def test_basic_contact(self):
        """Test creating entity from basic contact."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            emails=[{"label": "work", "value": "john@example.com"}],
            phones=[{"label": "mobile", "value": "+1-555-0100"}],
        )

        entity = create_contact_source_entity(contact)

        assert entity.source_type == SOURCE_CONTACTS
        assert entity.source_id == "ABC-123"
        assert entity.observed_name == "John Doe"
        assert entity.observed_email == "john@example.com"
        assert entity.observed_phone == "+1-555-0100"

    def test_metadata_fields(self):
        """Test that metadata includes contact fields."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            nickname="Johnny",
            organization="Acme Inc",
            job_title="Senior Engineer",
            department="R&D",
        )

        entity = create_contact_source_entity(contact)

        assert entity.metadata["given_name"] == "John"
        assert entity.metadata["family_name"] == "Doe"
        assert entity.metadata["nickname"] == "Johnny"
        assert entity.metadata["organization"] == "Acme Inc"
        assert entity.metadata["job_title"] == "Senior Engineer"
        assert entity.metadata["department"] == "R&D"

    def test_multiple_emails_phones(self):
        """Test with multiple emails and phones."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            emails=[
                {"label": "work", "value": "john@work.com"},
                {"label": "home", "value": "john@home.com"},
            ],
            phones=[
                {"label": "mobile", "value": "+1-555-0100"},
                {"label": "work", "value": "+1-555-0200"},
            ],
        )

        entity = create_contact_source_entity(contact)

        # Primary values used for main fields
        assert entity.observed_email == "john@work.com"
        assert entity.observed_phone == "+1-555-0100"

        # All values in metadata
        assert len(entity.metadata["emails"]) == 2
        assert len(entity.metadata["phones"]) == 2

    def test_long_note_truncated(self):
        """Test that long notes are truncated in metadata."""
        long_note = "x" * 1000
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            note=long_note,
        )

        entity = create_contact_source_entity(contact)

        assert len(entity.metadata["note"]) < len(long_note)
        assert entity.metadata["note"].endswith("...")

    def test_birthday_in_metadata(self):
        """Test birthday is included in metadata."""
        birthday = datetime(1990, 5, 15, tzinfo=timezone.utc)
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            birthday=birthday,
        )

        entity = create_contact_source_entity(contact)

        assert entity.metadata["birthday"] == birthday.isoformat()

    def test_social_profiles(self):
        """Test social profiles in metadata."""
        contact = AppleContact(
            identifier="ABC-123",
            given_name="John",
            family_name="Doe",
            social_profiles=[
                {"service": "Twitter", "username": "@johndoe", "url": ""},
                {"service": "LinkedIn", "username": "johndoe", "url": "https://linkedin.com/in/johndoe"},
            ],
        )

        entity = create_contact_source_entity(contact)

        assert len(entity.metadata["social_profiles"]) == 2
        assert entity.metadata["social_profiles"][0]["service"] == "Twitter"
